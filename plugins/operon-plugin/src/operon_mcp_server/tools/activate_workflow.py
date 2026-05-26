r"""`activate_workflow` MCP tool (Coordinator-only).

Per SPEC §7 `activate_workflow` row + §11 + §17. Creates a new
operon-session directory under `<project>/.operon/<run_name>/` and
bootstraps `phase_state.json`, `_active.json`, an empty
`agents.json`, and the empty mailbox / _handles subtrees.

run_name normalization + validation (SPEC §7):
- Normalize to the canonical slug the Anthropic runtime's TeamCreate
  produces (lowercase; every run of non-[a-z0-9] -> a single `-`;
  strip leading/trailing `-`). This makes the run dir + team paths
  match the directory TeamCreate creates on disk -- see
  `_normalize_run_name`.
- Reject if the slug is empty (no alphanumerics) or > 50 chars.
- Reject collision with an existing run directory.

Identity gate: Coordinator-only per SPEC §7.1.

Phase 6.5 (destructive): if the CURRENT active run has alive worker
bg sessions, this tool first issues an `elicitation/create`
confirmation listing those workers and only proceeds on accept.
User decline returns `{activated: false, reason: "user_declined"}`
without mutating any on-disk state.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

from .. import elicit, identity, paths, subagent_install, workflow
from . import spawn_agent as spawn_agent_tool

#: MCP tool name. Coordinator-only per SPEC §7.1.
TOOL_NAME = "activate_workflow"

#: Cap on run_name length (post-normalization) to keep paths sane on
#: Windows MAX_PATH.
_MAX_RUN_NAME_LEN = 50

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "workflow_id": {
            "type": "string",
            "description": (
                "Identifier of the workflow to activate. Must resolve "
                "via the 3-tier loader (project > user > plugin) to a "
                "manifest YAML."
            ),
        },
        "run_name": {
            "type": "string",
            "description": (
                "Name for this operon-session. Operon normalizes it to "
                "a canonical slug (lowercase; runs of non-alphanumeric "
                "characters become single hyphens; leading/trailing "
                "hyphens stripped) so it matches the team directory "
                "TeamCreate creates -- e.g. 'Allen_CCF_Projection' -> "
                "'allen-ccf-projection'. The slug becomes the "
                "subdirectory under <project>/.operon/ and the team "
                "name. Must contain at least one letter or digit, be "
                "<=50 chars after normalization, and not collide with "
                "an existing run."
            ),
        },
    },
    "required": ["workflow_id", "run_name"],
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (Coordinator-only)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "PREREQUISITE: call Anthropic's `TeamCreate(team_name="
            "<slug>)` MCP tool BEFORE this tool so the Anthropic "
            "runtime's TUI sees the team (Shift+Down). operon "
            "normalizes run_name to a canonical slug (lowercase, "
            "hyphens) and looks for the team under that slug; if the "
            "team is missing it returns {status: 'team_not_created', "
            "next_step: 'TeamCreate(team_name=<slug>)'} with the exact "
            "slug to pass. On success it installs operon's workflow "
            "content (compiles each role's identity.md into "
            "~/.claude/agents/<role>.md as a subagent definition; "
            "writes initial phase_state.json), registers operon as a "
            "team member in ~/.claude/teams/<slug>/config.json, and "
            "sets <project>/.operon/_active.json to the new run. "
            "Coordinator-only."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class ActivateWorkflowError(RuntimeError):
    """Raised on validation or write failures; becomes a tool error."""


def _normalize_run_name(raw: str) -> str:
    """Canonicalize a user-supplied run_name into the slug the Anthropic
    runtime's TeamCreate produces, so operon's run dir + team paths match
    the directory TeamCreate creates on disk.

    The runtime maps a team_name to its directory by lowercasing every
    ASCII alphanumeric and replacing every other character with a single
    `-` (no collapsing of consecutive separators, no trimming) --
    empirically verified 2026-05-26 by probing TeamCreate. A *canonical*
    slug (lowercase, single `-` separators, no leading/trailing/double
    `-`) is therefore a fixed point of that mapping. We emit exactly that
    canonical form: lowercase, collapse every run of non-[a-z0-9] to one
    `-`, strip leading/trailing `-`. Feeding the result to TeamCreate
    yields the identical string back, so `team_config_path(run_name)`
    resolves to the real team directory.

    Idempotent: `_normalize_run_name(_normalize_run_name(x))` equals
    `_normalize_run_name(x)`.
    """
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")


def _validate_run_name(raw: str, normalized: str) -> None:
    """Raise `ActivateWorkflowError` if the run_name is unusable.

    `raw` is the user's input; `normalized` is `_normalize_run_name(raw)`
    (the name actually used on disk and passed to TeamCreate). The only
    hard failures are an empty slug (raw had no alphanumerics) or one
    that exceeds the length cap; everything else is normalized rather
    than rejected.
    """
    if not raw.strip():
        raise ActivateWorkflowError("'run_name' must be a non-empty string")
    if not normalized:
        raise ActivateWorkflowError(
            f"'run_name' {raw!r} normalizes to an empty slug; it must "
            "contain at least one letter or digit."
        )
    if len(normalized) > _MAX_RUN_NAME_LEN:
        raise ActivateWorkflowError(
            f"'run_name' normalizes to {normalized!r}, which exceeds "
            f"{_MAX_RUN_NAME_LEN} chars (got {len(normalized)}); "
            "choose a shorter name."
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_coordinator() -> dict[str, Any]:
    """Reject non-Coordinator callers per SPEC §7.1. Returns the
    caller's handle record so the carryover #2 code can copy it into
    the new run-dir."""
    try:
        record = spawn_agent_tool._require_coordinator()
    except spawn_agent_tool.SpawnAgentError as exc:
        raise ActivateWorkflowError(str(exc)) from exc
    return record


def _copy_coordinator_handle_and_roster_row(
    new_run_dir: Path,
    handle: str,
    coord_record: dict[str, Any],
    new_workflow_id: str,
) -> tuple[Path, Path]:
    """Carryover #2: propagate the Coordinator's identity into the new run.

    `activate_workflow` rotates `_active.json` to point at a new
    run-dir; without copying the Coordinator's handle file into that
    new dir, the very next tool call from the SAME MCP subprocess
    cannot resolve its role (because identity.read_handle_file()
    routes through `paths.active_run_dir()`). We also seed
    `<new-run-dir>/agents.json` with the Coordinator row using the
    same uniform schema as worker rows (Phase 5 prep), so that
    workers spawned from the new run can immediately
    `message_agent("Coordinator", ...)`.

    Phase 13 (Finding 1): the copied handle record's `workflow_id`
    field is rewritten to `new_workflow_id` so `whoami` and
    `get_phase` agree after the swap. The original record's other
    fields (agent_name, role, session_id, spawned_at, spawned_by)
    are preserved verbatim.

    Returns `(handle_path, roster_path)` for diagnostics. Safe to
    call before _active.json is swapped: writes go to the new
    run-dir's `_handles/` and `agents.json` directly via path
    composition that does NOT depend on `_active.json`.
    """
    # Handle file: copy from the env-anchored record but rewrite the
    # workflow_id to the new workflow_id (Phase 13 Finding 1). The
    # rest of the record (handle, agent_name, role, etc.) is the
    # same identity the Coordinator has had since spawn.
    handles_dir = new_run_dir / paths.HANDLES_DIRNAME
    handles_dir.mkdir(parents=True, exist_ok=True)
    handle_path = handles_dir / f"{handle}.json"
    new_record = dict(coord_record)
    new_record["workflow_id"] = new_workflow_id
    handle_payload = json.dumps(new_record, indent=2, ensure_ascii=False)
    tmp = handle_path.with_name(
        f"{handle_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    try:
        tmp.write_text(handle_payload, encoding="utf-8")
        os.replace(tmp, handle_path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ActivateWorkflowError(
            f"Failed to copy Coordinator handle into new run-dir: {exc}"
        ) from exc

    # Roster: seed with one Coordinator row (uniform schema; Phase 5
    # prep). Other Agents from the prior run are NOT copied -- they
    # belonged to a different operon-session and may not exist
    # anymore. A fresh `activate_workflow` is effectively a new
    # collaboration.
    now = _now_iso()
    coord_row = {
        "name": coord_record.get("agent_name", "Coordinator"),
        "role": coord_record.get("role", "coordinator"),
        "handle": handle,
        "session_id": coord_record.get("session_id", ""),
        # Phase 13 Finding 1: pin to new_workflow_id so the roster
        # row stays consistent with the rewritten handle file.
        "workflow_id": new_workflow_id,
        "status": "idle",
        "spawned_at": coord_record.get("spawned_at", now),
        "last_turn_at": now,
    }
    roster_path = new_run_dir / "agents.json"
    roster_payload = json.dumps([coord_row], indent=2, ensure_ascii=False)
    tmp = roster_path.with_name(
        f"{roster_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    try:
        tmp.write_text(roster_payload, encoding="utf-8")
        os.replace(tmp, roster_path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ActivateWorkflowError(
            f"Failed to seed Coordinator row in new run roster: {exc}"
        ) from exc

    return handle_path, roster_path


async def _do_activate(args: dict[str, Any]) -> dict[str, Any]:
    """Destructive activate (Phase 6.5).

    Flow:
      1. Validate inputs + Coordinator role.
      2. Load workflow manifest (early so a missing workflow doesn't
         leave half-created state on disk).
      3. If there is a CURRENT active run with alive worker bg
         sessions, issue an elicitation/create confirmation listing
         the workers that will be killed. User decline -> early
         return, no mutation.
      4. Kill the alive workers via `claude stop <daemonShort>`.
      5. Create the new run-dir subtree, copy the Coordinator handle
         + seed the Coordinator roster row.
      6. Atomically swap `_active.json` to point at the new run.
      7. Write the initial phase_state.json.
      8. Return success with `killed_workers` list for audit.
    """
    workflow_id = args.get("workflow_id")
    run_name = args.get("run_name")
    if not (isinstance(workflow_id, str) and workflow_id):
        raise ActivateWorkflowError("'workflow_id' must be a non-empty string")
    if not isinstance(run_name, str):
        raise ActivateWorkflowError("'run_name' must be a string")
    # Normalize to the canonical slug TeamCreate will produce, then
    # validate. Everything downstream (run_dir, team paths, the
    # team_not_created next_step) uses the slug, so it matches the
    # directory the Anthropic runtime creates on disk.
    normalized_run_name = _normalize_run_name(run_name)
    _validate_run_name(run_name, normalized_run_name)
    run_name = normalized_run_name

    coord_record = _require_coordinator()
    coord_handle = identity.read_env_handle()
    if not coord_handle:
        raise ActivateWorkflowError(
            "Coordinator identity is missing OPERON_AGENT_HANDLE; "
            "_require_coordinator should have caught this."
        )

    try:
        decl = workflow.load_workflow(workflow_id)
    except workflow.WorkflowError as exc:
        raise ActivateWorkflowError(str(exc)) from exc
    first_phase = decl.first_phase_id
    if first_phase is None:
        raise ActivateWorkflowError(
            f"Workflow {workflow_id!r} declares no phases; cannot activate."
        )

    # Land 1 v2 (Boaz's demo 2026-05-21 finding 1): the Anthropic
    # runtime TUI only sees teams created via Anthropic's TeamCreate
    # MCP tool. Operon writing the team config from scratch leaves
    # spawned teammates invisible to Shift+Down. Pre-check the team
    # config exists; if it does not, return a structured error that
    # tells the lead's LLM to call TeamCreate first. Return BEFORE
    # the destructive prelude so we do not kill workers or create
    # operon-side state on behalf of a non-existent team.
    if not subagent_install.team_config_exists(run_name):
        team_path = subagent_install.team_config_path(run_name)
        return {
            "activated": False,
            "status": "team_not_created",
            "error": (
                f"No team exists at {team_path}. Call "
                f"TeamCreate(team_name={run_name!r}) before "
                f"activate_workflow so the Anthropic runtime TUI "
                f"sees the team (Shift+Down)."
            ),
            "next_step": f"TeamCreate(team_name={run_name!r})",
            "workflow_id": workflow_id,
            "run_name": run_name,
        }

    try:
        operon_dir = paths.operon_dir()
    except paths.OperonPathError:
        # No .operon ancestor: bootstrap first run under cwd.
        operon_dir = Path.cwd() / paths.OPERON_DIRNAME
        operon_dir.mkdir(parents=True, exist_ok=True)

    run_dir = operon_dir / run_name
    if run_dir.exists():
        raise ActivateWorkflowError(
            f"Operon-session directory '{run_dir}' already exists. "
            f"Choose a different run_name."
        )

    # --- Destructive prelude: alive-worker inspection + confirm ----
    # Find the current active run (if any) and enumerate its alive
    # workers. Coordinator's own bg session is NOT in this list (the
    # alive_agents_in_run helper excludes role=coordinator).
    killed_workers: list[dict[str, Any]] = []
    current_run_dir: Path | None = None
    alive_workers: list[dict[str, Any]] = []
    try:
        current_run_dir = paths.active_run_dir()
    except paths.OperonPathError:
        current_run_dir = None
    if current_run_dir is not None:
        alive_workers = workflow.alive_agents_in_run(current_run_dir)

    if alive_workers:
        names = [w.get("name", "<unknown>") for w in alive_workers]
        msg = (
            f"Activate workflow '{workflow_id}' as run '{run_name}'?\n\n"
            f"This will CLOSE these workers from current run "
            f"'{current_run_dir.name if current_run_dir else '?'}':\n"
            + "\n".join(f"  - {n}" for n in names)
        )
        approved = await elicit.confirm(msg)
        if not approved:
            return {
                "activated": False,
                "reason": "user_declined",
                "would_have_killed": names,
                "current_run": current_run_dir.name if current_run_dir else None,
            }
        # User approved -- kill the alive workers BEFORE creating the
        # new run-dir, so a kill failure aborts cleanly.
        for w in alive_workers:
            short = w.get("_daemon_short", "")
            stop_result = workflow.kill_bg_session(short)
            killed_workers.append(
                {
                    "name": w.get("name"),
                    "session_id": w.get("session_id"),
                    "stop": stop_result,
                }
            )

    # --- Idempotent creation of the new run -----------------------
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / paths.HANDLES_DIRNAME).mkdir(parents=True, exist_ok=True)
    (run_dir / "mailbox").mkdir(parents=True, exist_ok=True)

    # Carryover #2 (Phase 5): copy Coordinator handle + seed roster
    # row BEFORE swapping `_active.json`. Phase 13 Finding 1: pass
    # workflow_id so the copied handle has the post-swap workflow_id.
    _copy_coordinator_handle_and_roster_row(
        new_run_dir=run_dir,
        handle=coord_handle,
        coord_record=coord_record,
        new_workflow_id=workflow_id,
    )

    # Atomic swap of _active.json via the shared helper.
    try:
        workflow.write_active_pointer(operon_dir, run_name)
    except workflow.WorkflowError as exc:
        raise ActivateWorkflowError(str(exc)) from exc

    try:
        workflow.write_initial_phase_state(workflow_id, first_phase)
    except workflow.WorkflowError as exc:
        raise ActivateWorkflowError(str(exc)) from exc

    # Stamp run ownership from the SessionStart marker: the session that
    # activates the run owns it, so the PreToolUse hook applies this
    # run's workflow-embedded rules only to this session (not to an
    # unrelated session later opened in the same project). Best-effort:
    # a missing/unreadable marker leaves the run unowned rather than
    # failing activation.
    try:
        marker = workflow.read_session_marker()
        owner_sid = marker.get("session_id") if isinstance(marker, dict) else None
        if isinstance(owner_sid, str) and owner_sid:
            workflow.set_owner_session_id(owner_sid)
    except workflow.WorkflowError:
        pass

    # Land 1 of the Agent Teams pivot (v2.9 plan section 8 Land 1):
    # compile every workflow role's identity.md into Anthropic's
    # subagent-definition schema under ~/.claude/agents/<role>.md,
    # install the operon-stub subagent definition, and register
    # operon as a team member in ~/.claude/teams/<team>/config.json.
    # The team name is the operon run_name (the canonical slug
    # normalized above), so it matches the directory TeamCreate created
    # for the same name. The transformer and registration are
    # purely additive -- no legacy code is touched (Land 1 deletes
    # nothing per plan section 6 + section 8 Land 1).
    try:
        teams_manifest = subagent_install.install_for_activation(
            workflow_id=workflow_id,
            team_name=run_name,
        )
    except subagent_install.SubagentInstallError as exc:
        raise ActivateWorkflowError(str(exc)) from exc

    # Phase 13 Finding 2: render the caller's role brief for the new
    # workflow's first phase so the Coordinator's LLM gets the same
    # per-phase context that spawned workers receive.
    caller_role = coord_record.get("role", "coordinator")
    if isinstance(caller_role, str) and caller_role:
        brief = spawn_agent_tool.assemble_caller_brief(
            workflow_id, caller_role, first_phase
        )
        if brief is None:
            brief = spawn_agent_tool.absent_caller_brief(
                workflow_id,
                caller_role,
                first_phase,
                reason=(
                    f"No {caller_role}/identity.md in any tier for workflow "
                    f"{workflow_id!r}; caller will operate without a brief."
                ),
            )
    else:
        brief = None

    return {
        "activated": True,
        "run_name": run_name,
        "workflow_id": workflow_id,
        "current_phase": first_phase,
        "run_dir": str(run_dir),
        "tier": decl.tier,
        "manifest_path": str(decl.source_path),
        "killed_workers": killed_workers,
        "previous_run": current_run_dir.name if current_run_dir else None,
        "caller_brief": brief,
        # Land 1 surfaces: which subagent definitions got written and
        # which team config was registered. Boaz uses this to verify
        # the demo end-to-end.
        "team_install": teams_manifest,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `activate_workflow`."""
    args = arguments or {}
    result = await _do_activate(args)
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
