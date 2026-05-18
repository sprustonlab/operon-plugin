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
import logging
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

from .. import identity, paths, roster

_log = logging.getLogger(__name__)

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
            # behavior, not a spawn-time error. Log so future readers
            # know fields got dropped (B5 carryover from Phase 3).
            _log.debug("identity.md frontmatter: skipped unparseable line %r", raw_line)
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

    # Step 3: ## Constraints block. Per SPEC §5 step 3 the assembled
    # prompt always ends with the heading; the body lists projected
    # Rules and advance checks for (role, current_phase). Phase 6 will
    # populate the body; Phase 3 emits the heading with a placeholder
    # body so the prompt shape is stable across phases (downstream
    # agent behavior can rely on the heading existing).
    constraints_body = "(no Rules apply in this phase)"
    parts.append(f"## Constraints\n\n{constraints_body}")
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
    """Read `(workflow_id, current_phase)` from phase_state.json.

    Returns `(workflow_id, current_phase)`. Raises `SpawnAgentError` if
    the file is missing or lacks `workflow_id`. `current_phase` may be
    None (no advance has happened yet).

    Per SPEC §11 and §6.5 keys table the canonical field name in
    `phase_state.json` is `workflow_id` (not `active_workflow`).
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
    workflow_id = data.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise SpawnAgentError(
            f"phase_state.json '{path}' missing non-empty 'workflow_id'."
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


#: Env var Claude Code sets in each plugin's MCP subprocess to expose
#: the plugin's install root (see `plugins/operon-plugin/.mcp.json`
#: `"command": "${CLAUDE_PLUGIN_ROOT}/bin/operon-mcp-server"`). We
#: re-emit it as the spawned `claude --bg`'s `--plugin-dir` argument
#: so the bg session loads the operon plugin too; without this the
#: spawned worker session has no operon plugin, no MCP server, and
#: no mailbox watch loop (Phase 4 fix v2 root cause).
ENV_PLUGIN_ROOT_VAR = "CLAUDE_PLUGIN_ROOT"

#: Env var that toggles verbose stderr logging in the MCP server. We
#: forward its value (if set in the Coordinator's env) into the
#: spawned worker's `--settings.env` so that `OPERON_DEBUG=1 claude
#: --plugin-dir ...` from the user's shell reaches every spawned
#: worker's MCP subprocess. Without this, only the Coordinator sees
#: debug output and worker boot is invisible.
ENV_DEBUG_VAR = "OPERON_DEBUG"

#: Env var that opts into propagating `--dangerously-load-development-
#: channels=<channel-id>` onto spawned `claude --bg` argv so the worker
#: session registers our `claude/channel` push instead of silently
#: dropping it with "Channel notifications skipped: server ... not in
#: --channels list for this session". Default off because the flag is
#: only safe under one of two preconditions:
#: (a) the user has applied Boaz's local binary patch to Claude Code
#:     2.1.143 that bypasses the DevChannelsDialog TTY-confirm gate
#:     (the gate would otherwise silently kill bg sessions), OR
#: (b) operon-plugin is on Anthropic's approved-channel allowlist (not
#:     true at the time of writing).
#: Without (a) or (b), passing the flag risks silent spawn failure.
#: Boaz turns this on for his patched setup; distribution stays default
#: off until upstream channels-API support is widely available.
ENV_BG_CHANNELS_VAR = "OPERON_BG_CHANNELS"


def _resolve_channel_identifier() -> str | None:
    """Construct `plugin:<plugin-name>@<marketplace-name>` from on-disk JSON.

    Reads `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json` for the
    plugin name and `${CLAUDE_PLUGIN_ROOT}/../../.claude-plugin/
    marketplace.json` for the marketplace name. Avoids hardcoding the
    "operon-plugin" / "operon-plugin-marketplace" string pair in code
    so future renames or third-party redistribution do not require a
    code change.

    Returns the channel identifier string on success, or `None` if the
    files are missing / malformed / lack the required keys. Callers
    treat None as "feature unavailable" and proceed without the
    --dangerously-load-development-channels flag.
    """
    plugin_root_str = os.environ.get(ENV_PLUGIN_ROOT_VAR, "").strip()
    if not plugin_root_str:
        return None
    plugin_root = Path(plugin_root_str)

    plugin_json = plugin_root / ".claude-plugin" / "plugin.json"
    # `<plugin_root>/../..` walks up `plugins/<plugin>/` to the repo
    # root, where the marketplace manifest lives.
    marketplace_json = plugin_root.parent.parent / ".claude-plugin" / "marketplace.json"

    try:
        plugin_data = json.loads(plugin_json.read_text(encoding="utf-8"))
        marketplace_data = json.loads(marketplace_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(plugin_data, dict) or not isinstance(marketplace_data, dict):
        return None

    plugin_name = plugin_data.get("name")
    marketplace_name = marketplace_data.get("name")
    if not (isinstance(plugin_name, str) and plugin_name):
        return None
    if not (isinstance(marketplace_name, str) and marketplace_name):
        return None

    return f"plugin:{plugin_name}@{marketplace_name}"


def _bg_channels_enabled() -> bool:
    """Return True iff the Coordinator's env opts into bg-channels flag.

    Recognized truthy values for `OPERON_BG_CHANNELS`: `1`, `true`,
    `yes`, `on` (case-insensitive). Anything else (including the
    var being unset) is treated as off.
    """
    flag = os.environ.get(ENV_BG_CHANNELS_VAR, "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


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

    Plugin propagation: `claude --bg` does NOT inherit the parent
    session's plugin context, so we explicitly re-emit `--plugin-dir
    $CLAUDE_PLUGIN_ROOT` on the argv. Without it, the spawned bg
    session boots WITHOUT the operon plugin and therefore without an
    operon MCP server -- empirically confirmed by manual `claude --bg`
    probes (Phase 4 fix v2).

    Channel surfacing: the watch loop emits `claude/channel`
    notifications regardless, but Claude Code drops them with the
    message "Channel notifications skipped: server <id> not in
    --channels list for this session" unless the spawned bg session
    has the channel registered. To allow the channel to register we
    pass `--dangerously-load-development-channels=plugin:<plugin>@
    <marketplace>` on the spawn argv (gated by `OPERON_BG_CHANNELS=1`
    so unpatched/unprivileged users default off).

    UPSTREAM CAVEATS observed during Carryover #5 testing on Claude
    Code 2.1.143 with Boaz's `DevChannelsDialog`-bypass binary patch:

    - The dev-channels flag's TTY-confirm dialog was the FIRST gate
      and was silently killing bg sessions; Boaz's binary patch
      bypasses that dialog so the spawn now survives. Confirmed.
    - But there appears to be a SECOND gate further into the bg
      session's channels-registration path that the dialog patch
      does NOT bypass. Empirical observation: even with the flag on
      argv, bg-spawned worker MCP logs report either "plugin ... is
      not on the approved channels allowlist (use --dangerously-load-
      development-channels for local dev)" (recursive instruction,
      flag IS already present) or "you asked for X but the installed
      operon-plugin plugin is from inline" (marketplace-identifier
      mismatch when --plugin-dir is also passed).
    - Mailbox filesystem transport works either way -- envelopes
      move `inbox/` -> `processed/`, audit trail unaffected -- the
      gate only blocks the LLM-visible `<channel>` tag.
    - The plumbing is therefore forward-compatible: when upstream
      patches the second gate (or a marketplace-installed operon
      reaches the approved allowlist), this code path will start
      registering channels correctly with NO code change required.

    The channel identifier `plugin:<plugin>@<marketplace>` is read
    from on-disk manifests (`.claude-plugin/plugin.json` +
    `.claude-plugin/marketplace.json`) via
    `_resolve_channel_identifier()` so renames or third-party
    redistribution do not require a code change.

    Equal-sign syntax (`--flag=value`) is used instead of
    space-separated (`--flag value`) because empirically the
    space-separated form interacts badly with positional-argument
    parsing in some Claude Code versions (the prompt argument was
    observed consuming the channel-list value).

    Settings env: includes `OPERON_AGENT_HANDLE` (always) and
    `OPERON_DEBUG` (if set in the Coordinator's env) so verbose
    logging flows into worker MCP subprocesses without a per-spawn
    toggle.
    """
    settings_env: dict[str, str] = {identity.ENV_HANDLE_VAR: handle}
    coord_debug = os.environ.get(ENV_DEBUG_VAR)
    if coord_debug:
        settings_env[ENV_DEBUG_VAR] = coord_debug
    settings = json.dumps({"env": settings_env}, ensure_ascii=False)
    agents_json = json.dumps(agents_payload, ensure_ascii=False)

    plugin_root = os.environ.get(ENV_PLUGIN_ROOT_VAR, "").strip()

    argv: list[str] = [
        "claude",
        "--bg",
        # NOTE: `--session-id` is documented but `claude --bg` warns
        # "ignoring --session-id (use --resume <id> to continue an
        # existing session)" and generates a fresh session id. Kept
        # here for forward-compat with future bg-respects-id behavior;
        # the pre-generated session id is also written into
        # `_handles/<handle>.json` for `bind_handle` validation
        # idempotence per SPEC §6.5 step 1.
        "--session-id",
        session_id,
        "--settings",
        settings,
        "--agents",
        agents_json,
        "--agent",
        role,
    ]
    if plugin_root:
        argv.extend(["--plugin-dir", plugin_root])
    # Carryover #5: opt-in dev-channels flag. Gated on
    # `OPERON_BG_CHANNELS=1` because the upstream TTY-confirm gate
    # silently kills bg sessions without the binary patch. `=` syntax
    # avoids the prompt-eats-the-channel-list parsing issue observed
    # with the space-separated form.
    if _bg_channels_enabled():
        channel_id = _resolve_channel_identifier()
        if channel_id:
            argv.append(
                f"--dangerously-load-development-channels={channel_id}"
            )
    argv.append(initial_prompt)

    env = dict(os.environ)
    env[identity.ENV_HANDLE_VAR] = handle
    try:
        # stdout/stderr -> DEVNULL: Popen pipe buffers cap at ~64KB on
        # Linux. Without a drainer thread the spawned `claude --bg`
        # blocks on its next write once the buffer fills, deadlocking
        # the new session. Phase 4 may wire a real drainer if we ever
        # need to capture stdout (e.g. parse the `backgrounded · <id>`
        # confirmation line); for Phase 3 the pre-generated session_id
        # makes stdout-parsing unnecessary, so discarding is safe.
        return subprocess.Popen(
            argv,
            cwd=str(project_path),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
    if not isinstance(caller_handle, str) or not caller_handle:
        # Defensive: _require_coordinator already validated the role,
        # but the `handle` field is the chain-of-trust anchor for the
        # new agent's `spawned_by`. A missing/blank value would silently
        # propagate `None` and break later chain-of-trust checks
        # (`get_applicable_rules`, `get_agent_info` cross-Agent gate).
        raise SpawnAgentError(
            "Coordinator handle record is missing the 'handle' field; "
            "cannot anchor spawned_by chain-of-trust."
        )

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
