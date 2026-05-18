"""`activate_workflow` MCP tool (Coordinator-only).

Per SPEC §7 `activate_workflow` row + §11 + §17. Creates a new
operon-run directory under `<project>/.operon/<run_name>/` and
bootstraps `phase_state.json`, `_active.json`, an empty
`agents.json`, and the empty mailbox / _handles subtrees.

run_name validation (SPEC §7):
- Reject characters: `/`, `\`, `:`, `*`, `?`, `<`, `>`, `|`, `"`
- Reject leading `.`
- Reject empty / longer than 50 chars
- Reject collision with an existing run directory

Identity gate: Coordinator-only per SPEC §7.1.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

from .. import identity, paths, workflow
from . import spawn_agent as spawn_agent_tool

#: MCP tool name. Coordinator-only per SPEC §7.1.
TOOL_NAME = "activate_workflow"

#: Filesystem-unsafe characters disallowed in `run_name` (SPEC §7).
_DISALLOWED_RUN_NAME_CHARS = frozenset('/\\:*?<>|"')

#: Cap on run_name length to keep paths sane on Windows MAX_PATH.
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
                "Human-readable name for this operon-session. Becomes "
                "the subdirectory under <project>/.operon/. Must be "
                "filesystem-safe (no /, \\, :, *, ?, <, >, |, \"), "
                "not start with `.`, non-empty, <=50 chars, and not "
                "collide with an existing run."
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
            "Create a new operon-session: validates run_name, loads "
            "the workflow manifest via the 3-tier loader, creates "
            "<project>/.operon/<run_name>/{phase_state.json, agents.json, "
            "_handles/, mailbox/}, and updates <project>/.operon/"
            "_active.json to point at the new run. Coordinator-only."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class ActivateWorkflowError(RuntimeError):
    """Raised on validation or write failures; becomes a tool error."""


def _validate_run_name(run_name: str) -> None:
    """Raise `ActivateWorkflowError` if `run_name` fails any SPEC §7 rule."""
    if not run_name:
        raise ActivateWorkflowError("'run_name' must be a non-empty string")
    if len(run_name) > _MAX_RUN_NAME_LEN:
        raise ActivateWorkflowError(
            f"'run_name' exceeds {_MAX_RUN_NAME_LEN} chars (got {len(run_name)})"
        )
    if run_name.startswith("."):
        raise ActivateWorkflowError("'run_name' may not start with '.'")
    bad = sorted(c for c in run_name if c in _DISALLOWED_RUN_NAME_CHARS)
    if bad:
        raise ActivateWorkflowError(
            f"'run_name' contains disallowed character(s): {''.join(bad)!r}"
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

    Returns `(handle_path, roster_path)` for diagnostics. Safe to
    call before _active.json is swapped: writes go to the new
    run-dir's `_handles/` and `agents.json` directly via path
    composition that does NOT depend on `_active.json`.
    """
    # Handle file: copy verbatim from the env-anchored record. The
    # record may have been read from the OLD run-dir's _handles/,
    # but its content (handle, agent_name, role, etc.) is the same
    # identity the Coordinator has had since spawn -- we just need
    # it to exist in the new run-dir's _handles/ subtree so that
    # the post-swap path lookup resolves it.
    handles_dir = new_run_dir / paths.HANDLES_DIRNAME
    handles_dir.mkdir(parents=True, exist_ok=True)
    handle_path = handles_dir / f"{handle}.json"
    handle_payload = json.dumps(coord_record, indent=2, ensure_ascii=False)
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
        "workflow_id": coord_record.get("workflow_id", ""),
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


def _do_activate(args: dict[str, Any]) -> dict[str, Any]:
    workflow_id = args.get("workflow_id")
    run_name = args.get("run_name")
    if not (isinstance(workflow_id, str) and workflow_id):
        raise ActivateWorkflowError("'workflow_id' must be a non-empty string")
    if not isinstance(run_name, str):
        raise ActivateWorkflowError("'run_name' must be a string")
    _validate_run_name(run_name)

    coord_record = _require_coordinator()
    coord_handle = identity.read_env_handle()  # always present after _require_coordinator
    if not coord_handle:
        # Defensive: _require_coordinator already validated identity,
        # so reaching this branch is a contract violation.
        raise ActivateWorkflowError(
            "Coordinator identity is missing OPERON_AGENT_HANDLE; "
            "_require_coordinator should have caught this."
        )

    # Resolve the manifest before creating any directories so a
    # missing-workflow failure does not leave a half-created run on
    # disk.
    try:
        decl = workflow.load_workflow(workflow_id)
    except workflow.WorkflowError as exc:
        raise ActivateWorkflowError(str(exc)) from exc
    first_phase = decl.first_phase_id
    if first_phase is None:
        raise ActivateWorkflowError(
            f"Workflow {workflow_id!r} declares no phases; cannot activate."
        )

    try:
        operon_dir = paths.operon_dir()
    except paths.OperonPathError as exc:
        # No .operon ancestor: we're being asked to activate the very
        # first run. Create `.operon/` under the process cwd.
        operon_dir = Path.cwd() / paths.OPERON_DIRNAME
        operon_dir.mkdir(parents=True, exist_ok=True)
        _ = exc  # silence linter

    run_dir = operon_dir / run_name
    if run_dir.exists():
        raise ActivateWorkflowError(
            f"Operon-session directory '{run_dir}' already exists. "
            f"Choose a different run_name."
        )

    # Bootstrap the run-dir subtree.
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / paths.HANDLES_DIRNAME).mkdir(parents=True, exist_ok=True)
    (run_dir / "mailbox").mkdir(parents=True, exist_ok=True)

    # Carryover #2: copy the Coordinator's handle file + seed the new
    # roster with a Coordinator row BEFORE swapping `_active.json`,
    # so subsequent tool calls from the same MCP subprocess can
    # resolve identity through the new run-dir.
    _copy_coordinator_handle_and_roster_row(
        new_run_dir=run_dir, handle=coord_handle, coord_record=coord_record,
    )

    # Write `_active.json` pointing at the new run.
    active_path = operon_dir / paths.ACTIVE_POINTER_FILENAME
    active_payload = json.dumps(
        {"active_run_name": run_name, "set_at": _now_iso()},
        indent=2,
        ensure_ascii=False,
    )
    active_tmp = active_path.with_name(
        f"{active_path.name}.tmp.{run_name}.{_now_iso().replace(':', '')}"
    )
    try:
        active_tmp.write_text(active_payload, encoding="utf-8")
        os.replace(active_tmp, active_path)
    except OSError as exc:
        try:
            active_tmp.unlink()
        except OSError:
            pass
        raise ActivateWorkflowError(
            f"Failed to write _active.json: {exc}"
        ) from exc

    # Write the initial phase_state.json AFTER _active.json so any
    # subsequent path lookup (which reads _active) finds a coherent
    # run.
    try:
        workflow.write_initial_phase_state(workflow_id, first_phase)
    except workflow.WorkflowError as exc:
        raise ActivateWorkflowError(str(exc)) from exc

    return {
        "run_name": run_name,
        "workflow_id": workflow_id,
        "current_phase": first_phase,
        "run_dir": str(run_dir),
        "tier": decl.tier,
        "manifest_path": str(decl.source_path),
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `activate_workflow`."""
    args = arguments or {}
    result = _do_activate(args)
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
