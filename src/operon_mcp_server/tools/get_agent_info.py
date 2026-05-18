"""`get_agent_info` MCP tool. All-visible per SPEC §7.1.

Aggregator: combines `whoami` + `get_phase` + `get_applicable_rules`
into a single document, so an Agent (or the Coordinator inspecting an
Agent) can pull a full picture without three round-trips.

Phase 5 scope: like `get_applicable_rules`, only the caller's own
view is supported. Cross-Agent inspection (Coordinator /
chain-of-trust gate per SPEC §7) is Phase 6.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

from .. import identity, workflow
from . import get_applicable_rules as gar_tool
from . import get_phase as gp_tool

#: MCP tool name. Visible to All per SPEC §7.1.
TOOL_NAME = "get_agent_info"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent_name": {
            "type": "string",
            "description": (
                "Optional. Name of another Agent to inspect "
                "(Coordinator only / chain-of-trust per SPEC §7). Phase "
                "5 supports only the caller's own info."
            ),
        },
        "compact": {
            "type": "boolean",
            "description": (
                "If true, omit the rendered markdown block and return "
                "only the structured payload. Defaults to false."
            ),
            "default": False,
        },
    },
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (All-visible)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Aggregator: whoami + get_phase + get_applicable_rules in "
            "one document. All roles; cross-Agent inspection lands in "
            "Phase 6."
        ),
        inputSchema=INPUT_SCHEMA,
    )


def _do_get(args: dict[str, Any]) -> dict[str, Any]:
    requested = args.get("agent_name")
    compact = bool(args.get("compact", False))

    # whoami
    try:
        who = identity.whoami()
    except identity.IdentityError as exc:
        return {"error": "identity_unbound", "reason": str(exc)}

    if requested is not None and requested != who["name"]:
        return {
            "error": "cross_agent_not_implemented",
            "reason": (
                "Phase 5 supports only the caller's own info. "
                "Cross-Agent inspection lands in Phase 6."
            ),
            "requested": requested,
            "caller": who["name"],
        }

    # get_phase
    phase_payload: dict[str, Any]
    try:
        phase_payload = gp_tool._do_get()
    except workflow.WorkflowError as exc:
        phase_payload = {"error": str(exc)}

    # get_applicable_rules (calls back into the same module's helper)
    rules_payload: dict[str, Any]
    try:
        rules_payload = gar_tool._do_get({})
    except (ValueError, workflow.WorkflowError, identity.IdentityError) as exc:
        rules_payload = {"error": str(exc)}

    out: dict[str, Any] = {
        "whoami": who,
        "phase": phase_payload,
        "rules": rules_payload,
    }
    if compact and isinstance(rules_payload, dict) and "markdown" in rules_payload:
        # Strip the markdown block for the compact form.
        rules_payload = dict(rules_payload)
        rules_payload.pop("markdown", None)
        out["rules"] = rules_payload
    return out


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `get_agent_info`."""
    args = arguments or {}
    result = _do_get(args)
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
