"""`restore_operon_session` MCP tool (Coordinator-only).

Per SPEC §7 + §13. Switches `<project>/.operon/_active.json` to point
at a different existing operon-session, destructively closing any
alive worker bg sessions in the CURRENT run first (with user
confirmation via `elicitation/create`).

Two entry modes:

- `run_name` supplied: skip the picker, validate the target, run the
  destructive-confirm + swap.
- `run_name` omitted: discover available sessions via
  `list_operon_sessions._do_list()`, issue a single-select picker
  elicitation, then run the destructive-confirm + swap. This is the
  flow `/restore` skill uses.

The destructive-confirm step exists ONLY when the current active
run has alive worker bg sessions. If the current run is empty or all
workers are already dead, the swap proceeds silently.

Ensures the Coordinator's handle file + roster row exist in the
target run-dir BEFORE swapping `_active.json`, so subsequent tool
calls from the same MCP subprocess can still resolve identity.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

from .. import elicit, identity, paths, workflow
from . import list_operon_sessions as list_tool
from . import spawn_agent as spawn_agent_tool

TOOL_NAME = "restore_operon_session"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_name": {
            "type": "string",
            "description": (
                "Name of the operon-session to restore. If omitted, the "
                "tool lists existing sessions and issues a picker "
                "elicitation."
            ),
        },
    },
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Switch the active operon-session to a different existing "
            "run-dir. Destructive: any alive worker bg sessions in the "
            "CURRENT run are closed first (with user confirmation via "
            "elicitation/create). Coordinator-only. When called with "
            "no run_name, surfaces a picker."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class RestoreOperonSessionError(RuntimeError):
    """Raised on validation or write failures; becomes a tool error."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_coordinator() -> dict[str, Any]:
    try:
        record = spawn_agent_tool._require_coordinator()
    except spawn_agent_tool.SpawnAgentError as exc:
        raise RestoreOperonSessionError(str(exc)) from exc
    return record


def _upsert_coordinator_in_target(
    target_run_dir: Path,
    handle: str,
    coord_record: dict[str, Any],
    target_workflow_id: str | None,
) -> None:
    """Ensure the Coordinator's handle file + roster row exist in the
    target run-dir BEFORE we swap `_active.json`.

    Mirrors `activate_workflow._copy_coordinator_handle_and_roster_row`
    but tolerant of an existing handle file (target may already have
    been a Coordinator before; we overwrite with the current record
    so the env-anchored identity round-trips through the new active
    run). Same logic applies to the agents.json row -- de-dupe by
    handle and update in-place.

    Phase 13 Finding 1: if `target_workflow_id` is provided (read from
    the target run-dir's phase_state.json), the upserted handle's
    `workflow_id` is rewritten to that value so `whoami` and
    `get_phase` agree after the swap. If None (target had no parseable
    phase_state.json), preserve the original record's workflow_id.
    """
    handles_dir = target_run_dir / paths.HANDLES_DIRNAME
    handles_dir.mkdir(parents=True, exist_ok=True)

    # Handle file (upsert). Phase 13 Finding 1: rewrite workflow_id.
    handle_path = handles_dir / f"{handle}.json"
    new_record = dict(coord_record)
    if target_workflow_id:
        new_record["workflow_id"] = target_workflow_id
    payload = json.dumps(new_record, indent=2, ensure_ascii=False)
    tmp = handle_path.with_name(
        f"{handle_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, handle_path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RestoreOperonSessionError(
            f"Failed to write Coordinator handle into target run: {exc}"
        ) from exc

    # Roster row (upsert by handle).
    roster_path = target_run_dir / "agents.json"
    rows: list[dict[str, Any]] = []
    if roster_path.is_file():
        try:
            data = json.loads(roster_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows = [r for r in data if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError):
            rows = []
    now = _now_iso()
    coord_row = {
        "name": coord_record.get("agent_name", "Coordinator"),
        "role": coord_record.get("role", "coordinator"),
        "handle": handle,
        "session_id": coord_record.get("session_id", ""),
        # Phase 13 Finding 1: pin to target_workflow_id when available
        # so the roster row stays consistent with the rewritten
        # handle file. Fallback to the record's original workflow_id
        # if target's phase_state.json was unreadable.
        "workflow_id": (
            target_workflow_id
            if target_workflow_id
            else coord_record.get("workflow_id", "")
        ),
        "status": "idle",
        "spawned_at": coord_record.get("spawned_at", now),
        "last_turn_at": now,
    }
    # Drop any prior row with the same handle, append the upsert.
    rows = [r for r in rows if r.get("handle") != handle]
    rows.append(coord_row)

    tmp = roster_path.with_name(
        f"{roster_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    try:
        tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, roster_path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RestoreOperonSessionError(
            f"Failed to upsert Coordinator row in target roster: {exc}"
        ) from exc


async def _maybe_pick_run_name() -> str | None:
    """Use `list_operon_sessions` + `elicit.select_one` to choose a run.

    Returns the chosen run_name, or None if the user declined / no
    sessions exist.
    """
    listing = list_tool._do_list()
    sessions = listing.get("sessions", [])
    if not sessions:
        return None
    # Build picker label per session: "<run_name>  (workflow, phase,
    # alive=N)". Picker enum values are the raw run_name strings so
    # the post-pick lookup is trivial; the label is just the
    # `message` body.
    lines = ["Pick the operon-session to restore:\n"]
    choices: list[str] = []
    for s in sessions:
        rn = s.get("run_name", "?")
        wf = s.get("workflow_id", "?")
        ph = s.get("current_phase", "?")
        alive = s.get("alive_agent_count", 0)
        active_marker = "  [ACTIVE]" if s.get("is_active") else ""
        lines.append(f"  - {rn}  (workflow={wf}, phase={ph}, alive={alive}){active_marker}")
        choices.append(rn)
    msg = "\n".join(lines)
    return await elicit.select_one(msg, choices, title="Operon-session")


async def _do_restore(args: dict[str, Any]) -> dict[str, Any]:
    coord_record = _require_coordinator()
    coord_handle = identity.read_env_handle()
    if not coord_handle:
        raise RestoreOperonSessionError(
            "Coordinator identity is missing OPERON_AGENT_HANDLE."
        )

    # Determine target run_name.
    raw_run_name = args.get("run_name")
    if raw_run_name is None:
        run_name = await _maybe_pick_run_name()
        if run_name is None:
            return {
                "restored": False,
                "reason": "no_selection",
                "detail": "User declined the picker or no sessions are available.",
            }
    else:
        if not isinstance(raw_run_name, str) or not raw_run_name:
            raise RestoreOperonSessionError("'run_name' must be a non-empty string")
        run_name = raw_run_name

    # Resolve operon_dir (must exist; restore needs at least one
    # existing run-dir).
    try:
        op_dir = paths.operon_dir()
    except paths.OperonPathError as exc:
        raise RestoreOperonSessionError(str(exc)) from exc

    target_dir = op_dir / run_name
    if not target_dir.is_dir():
        raise RestoreOperonSessionError(
            f"Operon-session '{run_name}' does not exist at '{target_dir}'."
        )

    target_phase_state = target_dir / "phase_state.json"
    if not target_phase_state.is_file():
        raise RestoreOperonSessionError(
            f"Target run '{run_name}' is malformed: missing phase_state.json."
        )

    # No-op if target is already the active run.
    current_run_dir: Path | None = None
    try:
        current_run_dir = paths.active_run_dir()
    except paths.OperonPathError:
        current_run_dir = None
    if current_run_dir is not None and current_run_dir.name == run_name:
        return {
            "restored": False,
            "reason": "already_active",
            "run_name": run_name,
        }

    # Destructive prelude: alive-worker inspection in CURRENT run.
    killed_workers: list[dict[str, Any]] = []
    alive_workers: list[dict[str, Any]] = []
    if current_run_dir is not None:
        alive_workers = workflow.alive_agents_in_run(current_run_dir)

    if alive_workers:
        names = [w.get("name", "<unknown>") for w in alive_workers]
        msg = (
            f"Restore operon-session '{run_name}'?\n\n"
            f"This will CLOSE these workers from current run "
            f"'{current_run_dir.name if current_run_dir else '?'}':\n"
            + "\n".join(f"  - {n}" for n in names)
        )
        approved = await elicit.confirm(msg)
        if not approved:
            return {
                "restored": False,
                "reason": "user_declined",
                "would_have_killed": names,
                "current_run": current_run_dir.name if current_run_dir else None,
            }
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

    # Phase 13 Finding 1: pre-read target's phase_state.json to get
    # workflow_id; rewrite the upserted handle's workflow_id so
    # `whoami` and `get_phase` agree after the swap.
    target_workflow_id: str | None = None
    try:
        ps_data = json.loads(target_phase_state.read_text(encoding="utf-8"))
        if isinstance(ps_data, dict):
            wid = ps_data.get("workflow_id")
            if isinstance(wid, str) and wid:
                target_workflow_id = wid
    except (OSError, json.JSONDecodeError):
        target_workflow_id = None

    # Upsert the Coordinator into the target run BEFORE swapping
    # `_active.json` so identity resolution survives the switch.
    _upsert_coordinator_in_target(
        target_run_dir=target_dir,
        handle=coord_handle,
        coord_record=coord_record,
        target_workflow_id=target_workflow_id,
    )

    # Atomic swap.
    try:
        workflow.write_active_pointer(op_dir, run_name)
    except workflow.WorkflowError as exc:
        raise RestoreOperonSessionError(str(exc)) from exc

    # Re-read the target's phase state for the return payload.
    try:
        phase_state = json.loads(target_phase_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # The swap already happened; surface the read failure but
        # don't roll back -- the active pointer is now valid.
        return {
            "restored": True,
            "run_name": run_name,
            "previous_run": current_run_dir.name if current_run_dir else None,
            "killed_workers": killed_workers,
            "phase_state_read_error": str(exc),
            "caller_brief": None,
        }
    if not isinstance(phase_state, dict):
        phase_state = {}

    # Roster summary for the response.
    agent_count = 0
    alive_count = 0
    roster_path = target_dir / "agents.json"
    if roster_path.is_file():
        try:
            data = json.loads(roster_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                agent_count = len(data)
                for r in data:
                    if not isinstance(r, dict):
                        continue
                    if r.get("role") == "coordinator":
                        alive_count += 1
                        continue
                    sid = r.get("session_id", "")
                    if isinstance(sid, str) and sid:
                        short = sid.split("-", 1)[0]
                        if workflow.is_bg_session_alive(short):
                            alive_count += 1
        except (OSError, json.JSONDecodeError):
            pass

    # Phase 13 Finding 2: render the caller's role brief for the
    # target run's current phase.
    caller_role = coord_record.get("role", "coordinator")
    restored_workflow_id = phase_state.get("workflow_id")
    restored_phase = phase_state.get("current_phase")
    brief = None
    if (
        isinstance(caller_role, str)
        and caller_role
        and isinstance(restored_workflow_id, str)
        and restored_workflow_id
    ):
        brief = spawn_agent_tool.assemble_caller_brief(
            restored_workflow_id,
            caller_role,
            restored_phase if isinstance(restored_phase, str) else None,
        )
        if brief is None:
            brief = spawn_agent_tool.absent_caller_brief(
                restored_workflow_id,
                caller_role,
                restored_phase if isinstance(restored_phase, str) else None,
                reason=(
                    f"No {caller_role}/identity.md in any tier for workflow "
                    f"{restored_workflow_id!r}; caller will operate without a brief."
                ),
            )

    return {
        "restored": True,
        "run_name": run_name,
        "previous_run": current_run_dir.name if current_run_dir else None,
        "workflow_id": restored_workflow_id,
        "current_phase": restored_phase,
        "agent_count": agent_count,
        "alive_agent_count": alive_count,
        "killed_workers": killed_workers,
        "caller_brief": brief,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    args = arguments or {}
    result = await _do_restore(args)
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]


__all__ = ["TOOL_NAME", "INPUT_SCHEMA", "tool_descriptor", "call", "RestoreOperonSessionError"]
