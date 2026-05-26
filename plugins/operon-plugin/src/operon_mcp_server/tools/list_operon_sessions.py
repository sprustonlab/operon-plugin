"""`list_operon_sessions` MCP tool. Visible to All per SPEC §7.1.

Enumerates every operon-session directory under `<project>/.operon/`
and returns a summary per run. Used by the `/restore` skill's picker
flow and by the LLM for "what runs exist?" introspection.

Read-only; never mutates state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

from .. import paths, workflow

TOOL_NAME = "list_operon_sessions"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "List every operon-session under <project>/.operon/, sorted "
            "by last_active_at descending. Returns run_name, workflow_id, "
            "current_phase, created_at, last_active_at, agent_count, "
            "alive_agent_count, and is_active. Read-only."
        ),
        inputSchema=INPUT_SCHEMA,
    )


def _file_mtime_iso(path: Path) -> str | None:
    """Return path's mtime as ISO-8601, or None if not a file."""
    if not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _summarize_run(run_dir: Path, active_run_name: str | None) -> dict[str, Any] | None:
    """Return a summary dict for a run, or None if the dir is malformed."""
    phase_state_path = run_dir / "phase_state.json"
    if not phase_state_path.is_file():
        # Not a valid run directory (e.g., just a manifest copy).
        return None

    try:
        phase_state = json.loads(phase_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(phase_state, dict):
        return None

    workflow_id = phase_state.get("workflow_id")
    current_phase = phase_state.get("current_phase")
    phase_started_at = phase_state.get("phase_started_at")

    # Agent counts
    agents_path = run_dir / "agents.json"
    rows: list[dict[str, Any]] = []
    if agents_path.is_file():
        try:
            data = json.loads(agents_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows = [r for r in data if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError):
            rows = []

    alive_count = 0
    for r in rows:
        if r.get("role") == "coordinator":
            # Coordinator counts as alive iff its handle file exists,
            # but we don't probe ~/.claude/jobs for it (the Coordinator
            # is the user's foreground session, not a bg job).
            alive_count += 1
            continue
        sid = r.get("session_id", "")
        if not isinstance(sid, str) or not sid:
            continue
        short = sid.split("-", 1)[0]
        if workflow.is_bg_session_alive(short):
            alive_count += 1

    # `created_at` and `last_active_at`: best-effort from the
    # `state.json` (if set_artifact_dir was called) and the most
    # recent mtime of phase_state.json / agents.json.
    created_at: str | None = None
    state_path = run_dir / "state.json"
    if state_path.is_file():
        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state_data, dict):
                ca = state_data.get("created_at")
                if isinstance(ca, str):
                    created_at = ca
        except (OSError, json.JSONDecodeError):
            created_at = None

    last_active_at = phase_started_at
    for cand_path in (phase_state_path, agents_path):
        mt = _file_mtime_iso(cand_path)
        if mt and (last_active_at is None or mt > last_active_at):
            last_active_at = mt

    return {
        "run_name": run_dir.name,
        "workflow_id": workflow_id,
        "current_phase": current_phase,
        "created_at": created_at,
        "last_active_at": last_active_at,
        "agent_count": len(rows),
        "alive_agent_count": alive_count,
        "is_active": run_dir.name == active_run_name,
    }


def _do_list() -> dict[str, Any]:
    # Read the active pointer (best-effort; may not exist).
    try:
        op_dir = paths.operon_dir()
    except paths.OperonPathError as exc:
        return {"sessions": [], "error": str(exc)}

    active_run_name: str | None = None
    active_path = op_dir / paths.ACTIVE_POINTER_FILENAME
    if active_path.is_file():
        try:
            data = json.loads(active_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                v = data.get("active_run_name")
                if isinstance(v, str) and v:
                    active_run_name = v
        except (OSError, json.JSONDecodeError):
            active_run_name = None

    sessions: list[dict[str, Any]] = []
    for run_dir in workflow.list_run_dirs():
        summary = _summarize_run(run_dir, active_run_name)
        if summary is not None:
            sessions.append(summary)

    # Sort by last_active_at desc; None values sort last.
    sessions.sort(
        key=lambda s: s.get("last_active_at") or "", reverse=True
    )
    return {
        "sessions": sessions,
        "active_run_name": active_run_name,
        "count": len(sessions),
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    del arguments
    result = _do_list()
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
