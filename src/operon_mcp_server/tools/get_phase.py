"""`get_phase` MCP tool. All-visible, eager-loaded per SPEC §7.1.

Returns the current phase snapshot from `phase_state.json` plus the
`artifact_dir` from `state.json` (if set). Read-only -- no
identity gate beyond the env-handle resolution used to populate
`callable_by` (informational only).
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

from .. import workflow

#: MCP tool name. Visible to All per SPEC §7.1.
TOOL_NAME = "get_phase"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (All-visible)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Read the active operon-session's phase state. Returns "
            "{workflow_id, current_phase, phase_started_at, "
            "advance_history, artifact_dir}. artifact_dir is null until "
            "set_artifact_dir is called. All roles."
        ),
        inputSchema=INPUT_SCHEMA,
    )


def _do_get() -> dict[str, Any]:
    state = workflow.read_phase_state()
    run_state = workflow.read_state()
    artifact_dir = None
    if isinstance(run_state, dict):
        artifact_dir = run_state.get("artifact_dir")
    return {
        "workflow_id": state.get("workflow_id"),
        "current_phase": state.get("current_phase"),
        "phase_started_at": state.get("phase_started_at"),
        "advance_history": state.get("advance_history") or [],
        "artifact_dir": artifact_dir,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `get_phase`."""
    del arguments  # this tool takes no inputs
    try:
        result = _do_get()
    except workflow.WorkflowError as exc:
        # Surface as structured tool result rather than tool error so
        # the LLM can pattern-match on the message.
        result = {"error": str(exc)}
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
