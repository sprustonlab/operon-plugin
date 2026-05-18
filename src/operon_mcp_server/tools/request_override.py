"""`request_override` MCP tool. All-visible per SPEC §7.1.

PHASE 6 SCOPE: returns a structured "not implemented" payload. The
full implementation lands in Phase 7 (override + acknowledge flow per
SPEC §9). This stub exists so the LLM-facing surface is stable: a
worker can call `request_override` today and receive a clear
deferred-feature notice, instead of hitting a "tool not found" error
that might prompt creative workarounds.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

TOOL_NAME = "request_override"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rule_id": {"type": "string"},
        "tool_name": {"type": "string"},
        "tool_input": {"type": "object", "additionalProperties": True},
        "approver": {
            "type": "string",
            "description": (
                "Optional Agent name to route the override request to. "
                "Defaults to 'coordinator' per SPEC §9. Phase 7 wires."
            ),
        },
    },
    "required": ["rule_id", "tool_name"],
    "additionalProperties": True,
}


def tool_descriptor() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Request user override for a deny-tier guardrail Rule. "
            "PHASE 6 STUB -- returns 'not_implemented'. Phase 7 wires "
            "the elicitation + token flow per SPEC §9."
        ),
        inputSchema=INPUT_SCHEMA,
    )


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    args = arguments or {}
    return [
        mcp_types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "status": "not_implemented",
                    "phase_landing": 7,
                    "reason": (
                        "request_override is a Phase 7 deliverable per "
                        "SPEC_APPENDIX §F. Phase 6 ships rule evaluation "
                        "and audit logging only; the override + ack flow "
                        "comes next."
                    ),
                    "echo": {
                        "rule_id": args.get("rule_id"),
                        "tool_name": args.get("tool_name"),
                        "approver": args.get("approver", "coordinator"),
                    },
                }
            ),
        )
    ]
