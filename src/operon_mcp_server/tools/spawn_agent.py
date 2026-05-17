"""Implementation of the `spawn_agent` MCP tool (Coordinator-only).

Per SPEC.md section 7 (`spawn_agent` row), section 6.5 (identity binding
sequence), and section 7.1 (Coordinator-only visibility). This tool is
the single writer of new `_handles/<handle>.json` records and the
matching `agents.json` rows (SPEC section 6.6 single-writer contract).

Execution sequence (matches SPEC section 7 step numbering):

  0. Pre-flight validation: role directory + identity.md exist in at
     least one tier of the 3-tier loader (project > user > plugin).
     On failure: structured tool error, no subprocess spawned, no
     state written.
  1. Read `active_workflow` and `current_phase` from phase_state.json.
  2. Load `<workflow-root>/<role>/identity.md` per the 3-tier loader.
     Parse YAML-ish frontmatter (description, model).
  3. Optionally append `<workflow-root>/<role>/<current_phase>.md`
     body if present. Append a `## Constraints` block listing Rules
     applicable to (role, current_phase) -- empty in Phase 3 (Rules
     land in Phase 6; spawn_agent must not block on missing rules.yaml).
  4. Generate fresh UUIDv4 `handle` and `session_id`. Write
     `_handles/<handle>.json` (atomic via `os.replace`).
  5. Build the `--agents` JSON payload (description, prompt, model only;
     no `tools`/`permissionMode` per the SPEC section 5 simplification).
  6. `subprocess.Popen(["claude", "--bg", "--session-id", <uuid>,
     "--settings", <json>, "--agents", <json>, "--agent", <role>,
     <prompt>], cwd=<project-path>, env=...)`. The env propagates the
     Coordinator's environment with OPERON_AGENT_HANDLE overridden to
     the new agent's handle (SPEC section 6.5 step 3).
  7. Append the row to `agents.json` (Coordinator-only writer, atomic
     via `os.replace`).
  8. Return success payload.

Identity gate: `spawn_agent` is Coordinator-only per SPEC section 7.1.
The implementation reads the caller's role from the env-anchored handle
(`OPERON_AGENT_HANDLE` -> `_handles/<handle>.json` -> `role`) and
rejects non-Coordinator callers with a tool error -- LLM-supplied claims
are not accepted.

Cross-platform: `pathlib.Path`, UTF-8 on all I/O, `os.replace` for atomic
renames, no platform-gated APIs. `subprocess.Popen` is used to launch
`claude --bg`; cwd flows via `Popen(cwd=...)` not via a `--cwd` CLI flag
(`--cwd` is the Agent View read-side filter only -- see SPEC section 6.2).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

from .. import identity, paths, roster

#: MCP tool name. Coordinator-only per SPEC section 7.1.
TOOL_NAME = "spawn_agent"

#: Env var exposing the plugin install root (set by Claude Code in
#: `.mcp.json`'s `${CLAUDE_PLUGIN_ROOT}` expansion). Used to locate the
#: plugin tier of the 3-tier workflow loader.
ENV_PLUGIN_ROOT = "CLAUDE_PLUGIN_ROOT"

#: Coordinator role identifier (SPEC section 5; lowercase snake_case).
COORDINATOR_ROLE = "coordinator"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Human-readable Agent name; must be unique within the active run."
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "Project working directory for the spawned Agent. "
                "Becomes the spawned session's cwd via "
                "subprocess.Popen(cwd=...)."
            ),
        },
        "prompt": {
            "type": "string",
            "description": (
                "Initial user message delivered to the spawned Agent's first turn."
            ),
        },
        "type": {
            "type": "string",
            "description": (
                "Role identifier (lowercase snake_case). Must match a "
                "role directory under "
                "workflows/<active_workflow>/<type>/ in at least one "
                "tier of the project > user > plugin loader."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Optional model override (sonnet, opus, haiku, "
                "inherit). Defaults to the identity.md frontmatter "
                "`model` value, or 'inherit' if unset."
            ),
        },
        "requires_answer": {
            "type": "boolean",
            "description": ("Recorded for Phase 8 nudge-tracking; no-op in Phase 3."),
        },
    },
    "required": ["name", "path", "prompt", "type"],
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (Coordinator-only)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Spawn a new background Agent into the active operon run. "
            "Coordinator-only. Pre-flight-validates the role directory + "
            "identity.md, pre-generates handle and session_id, writes "
            "_handles/<handle>.json and agents.json, then launches "
            "`claude --bg` with the assembled subagent identity."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class SpawnAgentError(RuntimeError):
    """Raised on unrecoverable spawn failures. Surfaces as a tool error."""


class SpawnAgentValidationError(Exception):
    """Pre-flight (§7 step 0) validation failure.

    Carries a SPEC-compliant structured error payload that the tool
    surfaces back to the LLM verbatim instead of as a generic tool
    error. See SPEC section 7 `spawn_agent` row.
    """

    def __init__(self, reason: str, role: str, workflow: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.role = role
        self.workflow = workflow

    def to_payload(self) -> dict[str, str]:
        return {
            "error": "validation_failed",
            "reason": self.reason,
            "role": self.role,
            "workflow": self.workflow,
        }


# -- 3-tier loader helpers ----------------------------------------------


def _workflow_role_tiers(workflow_id: str, role: str) -> list[Path]:
    """Return tier directories in priority order: project > user > plugin.

    Project tier resolves against the current `.operon/` ancestor (the
    Coordinator's MCP subprocess cwd). User tier is `~/.operon/`. Plugin
    tier reads `CLAUDE_PLUGIN_ROOT`; if the env var is unset the plugin
    tier is omitted (the project + user tiers can still satisfy the
    lookup).
    """
    try:
        project_root = paths.project_root()
    except paths.OperonPathError:
        # No .operon ancestor at all -- only user + plugin tiers apply.
        project_root = None

    candidates: list[Path] = []
    if project_root is not None:
        candidates.append(
            project_root / paths.OPERON_DIRNAME / "workflows" / workflow_id / role
        )
    candidates.append(
        Path.home() / paths.OPERON_DIRNAME / "workflows" / workflow_id / role
    )
    plugin_root = os.environ.get(ENV_PLUGIN_ROOT)
    if plugin_root:
        candidates.append(Path(plugin_root) / "workflows" / workflow_id / role)
    return candidates


def _first_existing_dir(tiers: list[Path]) -> Path | None:
    """Return the first directory in `tiers` that exists, or None."""
    for tier in tiers:
        if tier.is_dir():
            return tier
    return None


def _first_existing_file(tiers: list[Path], filename: str) -> Path | None:
    """Return the first `<tier>/<filename>` that exists, or None."""
    for tier in tiers:
        candidate = tier / filename
        if candidate.is_file():
            return candidate
    return None


# -- Frontmatter parsing -------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)$",
    re.DOTALL,
)


def _parse_identity_md(text: str) -> tuple[dict[str, str], str]:
    """Parse identity.md frontmatter + body.

    Returns `({key: value, ...}, body_text)`. If the file has no
    leading `---` frontmatter block, returns `({}, full_text)`.

    Per SPEC section 5 (simplification): we only consume `description`
    and `model`; other keys are tolerated and ignored. The parser is
    intentionally hand-rolled (no PyYAML dep) because the schema is
    minimal: `key: value` per line, no nesting, no lists.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    fm_block = match.group("fm")
    body = match.group("body")
    fields: dict[str, str] = {}
    for raw_line in fm_block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            # Tolerate unparseable lines rather than failing the spawn:
            # SPEC says malformed identity content surfaces as bad agent
            # behavior, not a spawn-time error.
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, body


# -- Prompt assembly -----------------------------------------------------


def _assemble_system_prompt(
    workflow_id: str,
    role: str,
    current_phase: str | None,
) -> tuple[str, dict[str, str]]:
    """Load identity.md (+ optional <phase>.md) and assemble prompt body.

    Returns `(assembled_prompt, frontmatter)`. Mirrors the three-step
    procedure in SPEC section 5:

      1. Load identity.md per the 3-tier loader.
      2. Append `<phase>.md` body if present.
      3. Append `## Constraints` block (EMPTY in Phase 3 -- Rules
         engine lands in Phase 6).
    """
    tiers = _workflow_role_tiers(workflow_id, role)

    identity_file = _first_existing_file(tiers, "identity.md")
    if identity_file is None:
        # Pre-flight should have caught this; defensive double-check.
        raise SpawnAgentError(
            f"identity.md not found for role={role!r}, workflow="
            f"{workflow_id!r} in any tier."
        )
    text = identity_file.read_text(encoding="utf-8")
    frontmatter, identity_body = _parse_identity_md(text)

    parts: list[str] = [identity_body.rstrip()]

    if current_phase:
        phase_file = _first_existing_file(tiers, f"{current_phase}.md")
        if phase_file is not None:
            phase_text = phase_file.read_text(encoding="utf-8")
            # Phase files may have frontmatter too; strip it if present
            # and append only the body (mirrors agent_folders.py).
            _, phase_body = _parse_identity_md(phase_text)
            parts.append(phase_body.rstrip())

    # Step 3: ## Constraints block. Phase 6 will populate this with
    # projected Rules; Phase 3 leaves it intentionally empty. Per the
    # task brief we omit the heading entirely rather than emit an empty
    # section, so the assembled prompt stays clean until Phase 6.
    assembled = "\n\n".join(p for p in parts if p).strip() + "\n"
    return assembled, frontmatter


# -- Coordinator identity check ------------------------------------------


def _require_coordinator() -> dict[str, Any]:
    """Resolve the calling Agent's identity; require role=coordinator.

    Returns the handle record on success. Raises `SpawnAgentError` if
    no identity is bound or if the caller is not the Coordinator. Per
    SPEC section 7.1 + section 6.5: LLM-supplied identity claims are
    ignored; the env-anchored handle is the authoritative source.
    """
    handle = identity.read_env_handle()
    if handle is None:
        raise SpawnAgentError(
            f"Environment variable '{identity.ENV_HANDLE_VAR}' is not "
            "set; spawn_agent requires an env-anchored Coordinator "
            "identity (SPEC 7.1)."
        )
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        raise SpawnAgentError(str(exc)) from exc
    if record is None:
        raise SpawnAgentError(
            f"No handle record at _handles/{handle}.json; cannot verify caller role."
        )
    role = record.get("role")
    if role != COORDINATOR_ROLE:
        raise SpawnAgentError(
            f"spawn_agent is Coordinator-only (SPEC 7.1); caller role is {role!r}."
        )
    return record


# -- _handles/<handle>.json writer ---------------------------------------


def _atomic_write_handle_file(handle: str, record: dict[str, Any]) -> Path:
    """Write `_handles/<handle>.json` atomically. Returns the final path.

    Single-writer per SPEC section 6.6 (Coordinator's MCP subprocess).
    No CAS; temp + os.replace is the full safety mechanism.
    """
    target = paths.handle_file(handle)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    payload = json.dumps(record, indent=2, ensure_ascii=False)
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise SpawnAgentError(f"Failed to write handle file '{target}': {exc}") from exc
    return target


# -- Phase state read ----------------------------------------------------


def _read_phase_state() -> tuple[str, str | None]:
    """Read `(active_workflow, current_phase)` from phase_state.json.

    Returns `(workflow_id, current_phase)`. Raises `SpawnAgentError` if
    the file is missing or lacks `active_workflow`. `current_phase` may
    be None (no advance has happened yet).
    """
    try:
        path = paths.phase_state_file()
    except paths.OperonPathError as exc:
        raise SpawnAgentError(str(exc)) from exc
    if not path.is_file():
        raise SpawnAgentError(
            f"phase_state.json not found at '{path}'; activate_workflow "
            "must run before spawn_agent."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpawnAgentError(f"Failed to read phase state '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise SpawnAgentError(f"phase_state.json '{path}' must contain a JSON object.")
    workflow_id = data.get("active_workflow")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise SpawnAgentError(
            f"phase_state.json '{path}' missing non-empty 'active_workflow'."
        )
    current_phase = data.get("current_phase")
    if current_phase is not None and not isinstance(current_phase, str):
        raise SpawnAgentError(f"'current_phase' in '{path}' must be a string.")
    return workflow_id, current_phase


# -- Pre-flight validation ----------------------------------------------


def _preflight(role: str, workflow_id: str) -> None:
    """Validate role dir + identity.md presence (SPEC §7 step 0)."""
    tiers = _workflow_role_tiers(workflow_id, role)
    if _first_existing_dir(tiers) is None:
        raise SpawnAgentValidationError("role_not_found", role, workflow_id)
    if _first_existing_file(tiers, "identity.md") is None:
        raise SpawnAgentValidationError("identity_missing", role, workflow_id)


# -- subprocess launch ---------------------------------------------------


def _spawn_subprocess(
    *,
    project_path: Path,
    session_id: str,
    handle: str,
    agents_payload: dict[str, Any],
    role: str,
    initial_prompt: str,
) -> subprocess.Popen[bytes]:
    """Invoke `claude --bg` with the assembled identity.

    cwd flows via `Popen(cwd=...)` per SPEC section 6.2. The env merges
    the Coordinator's current environment with the new agent's handle
    overridden, satisfying SPEC section 6.5 step 3 (the new MCP
    subprocess inherits the env at startup).
    """
    settings = json.dumps(
        {"env": {identity.ENV_HANDLE_VAR: handle}},
        ensure_ascii=False,
    )
    agents_json = json.dumps(agents_payload, ensure_ascii=False)
    argv = [
        "claude",
        "--bg",
        "--session-id",
        session_id,
        "--settings",
        settings,
        "--agents",
        agents_json,
        "--agent",
        role,
        initial_prompt,
    ]
    env = dict(os.environ)
    env[identity.ENV_HANDLE_VAR] = handle
    try:
        return subprocess.Popen(
            argv,
            cwd=str(project_path),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, FileNotFoundError) as exc:
        raise SpawnAgentError(
            f"Failed to launch `claude --bg`: {exc}. Is the `claude` binary on PATH?"
        ) from exc


# -- Tool entrypoint -----------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time in ISO-8601, suitable for JSON rows."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _do_spawn(args: dict[str, Any]) -> dict[str, Any]:
    """Core spawn logic (separated from MCP plumbing).

    Returns the success payload. Raises `SpawnAgentValidationError` for
    pre-flight failures and `SpawnAgentError` for runtime failures.
    """
    # 1) Input parsing (basic shape -- the MCP `inputSchema` does the
    #    rest, but the schema is advisory and we cannot trust it 100%).
    name = args.get("name")
    project_path = args.get("path")
    initial_prompt = args.get("prompt")
    role = args.get("type")
    model_override = args.get("model")
    # requires_answer is accepted for API stability but unused in Phase 3
    # (Phase 8 wires up the nudge mechanism that consumes it).
    args.get("requires_answer")

    if not (isinstance(name, str) and name):
        raise SpawnAgentError("'name' must be a non-empty string")
    if not (isinstance(project_path, str) and project_path):
        raise SpawnAgentError("'path' must be a non-empty string")
    if not (isinstance(initial_prompt, str) and initial_prompt):
        raise SpawnAgentError("'prompt' must be a non-empty string")
    if not (isinstance(role, str) and role):
        raise SpawnAgentError("'type' must be a non-empty string")
    if model_override is not None and not isinstance(model_override, str):
        raise SpawnAgentError("'model', if supplied, must be a string")

    project_path_obj = Path(project_path).expanduser().resolve()
    if not project_path_obj.is_dir():
        raise SpawnAgentError(
            f"'path' does not resolve to an existing directory: {project_path_obj}"
        )

    # 2) Coordinator identity gate.
    caller_record = _require_coordinator()
    caller_handle = caller_record.get("handle")

    # 3) Read the active workflow + phase.
    workflow_id, current_phase = _read_phase_state()

    # 4) Pre-flight (§7 step 0) -- structured error on failure.
    _preflight(role, workflow_id)

    # 5) Assemble system prompt from identity.md (+ optional phase.md).
    assembled_prompt, frontmatter = _assemble_system_prompt(
        workflow_id=workflow_id,
        role=role,
        current_phase=current_phase,
    )

    description = frontmatter.get("description", f"{role} role agent")
    model = model_override or frontmatter.get("model") or "inherit"

    # 6) Generate fresh identifiers.
    handle = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    spawned_at = _now_iso()

    # 7) Write _handles/<handle>.json BEFORE spawning (SPEC §6.5 step 1).
    handle_record = {
        "handle": handle,
        "agent_name": name,
        "role": role,
        "workflow_id": workflow_id,
        "spawned_at": spawned_at,
        "session_id": session_id,
        "spawned_by": caller_handle,
    }
    _atomic_write_handle_file(handle, handle_record)

    # 8) Append agents.json row (also before spawn so any race-condition
    #    reads see a consistent picture; if the spawn fails we roll back).
    roster_row = {
        "name": name,
        "role": role,
        "handle": handle,
        "session_id": session_id,
        "workflow_id": workflow_id,
        "status": "idle",
        "spawned_at": spawned_at,
        "last_turn_at": spawned_at,
    }
    try:
        roster.append_agent(roster_row)
    except roster.RosterError as exc:
        # Roll back the handle file so a retry with a different name
        # does not leak orphaned _handles/ entries.
        try:
            paths.handle_file(handle).unlink()
        except OSError:
            pass
        raise SpawnAgentError(str(exc)) from exc

    # 9) Build the --agents payload (SPEC §7 / §5 simplification: no
    #    tools, no permissionMode).
    agents_payload = {
        role: {
            "description": description,
            "prompt": assembled_prompt,
            "model": model,
        }
    }

    # 10) Launch claude --bg. On failure, roll back the roster row and
    #     the handle file so the run state is consistent.
    try:
        _spawn_subprocess(
            project_path=project_path_obj,
            session_id=session_id,
            handle=handle,
            agents_payload=agents_payload,
            role=role,
            initial_prompt=initial_prompt,
        )
    except SpawnAgentError:
        try:
            roster.remove_agent(name)
        except roster.RosterError:
            pass
        try:
            paths.handle_file(handle).unlink()
        except OSError:
            pass
        raise

    # 11) Return success.
    return {
        "agent_name": name,
        "handle": handle,
        "session_id": session_id,
        "role": role,
        "workflow_id": workflow_id,
    }


async def call(
    arguments: dict[str, Any] | None,
) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `spawn_agent`.

    Pre-flight validation (`role_not_found`, `identity_missing`)
    returns a SPEC-compliant structured error payload as a successful
    tool response (so the LLM can inspect `error`/`reason` fields).
    Other failures raise so the MCP framework surfaces a tool error.
    """
    args = arguments or {}
    try:
        result = _do_spawn(args)
    except SpawnAgentValidationError as exc:
        # Structured error per SPEC §7 step 0 -- returned as successful
        # tool response with an `error` field, not as an MCP tool error,
        # so the LLM can pattern-match on `reason` and react.
        return [mcp_types.TextContent(type="text", text=json.dumps(exc.to_payload()))]
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
