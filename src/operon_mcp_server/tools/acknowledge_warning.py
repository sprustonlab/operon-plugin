"""`acknowledge_warning` MCP tool. All-visible per SPEC §7.1.

PHASE 6 SCOPE: returns a structured "not implemented" payload. Phase 7
lands the full ack-token write to `acks/<command_hash>.json` per
SPEC §9. This stub exists so the LLM-facing surface is stable.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

TOOL_NAME = "acknowledge_warning"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rule_id": {"type": "string"},
        "tool_name": {"type": "string"},
        "tool_input": {"type": "object", "additionalProperties": True},
    },
    "required": ["rule_id", "tool_name"],
    "additionalProperties": True,
}


def tool_descriptor() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Acknowledge a warn-tier guardrail Rule so the SAME warn "
            "does not re-fire mid-session. PHASE 6 STUB -- returns "
            "'not_implemented'. Phase 7 wires the ack token write."
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
                        "acknowledge_warning is a Phase 7 deliverable "
                        "per SPEC_APPENDIX §F. Phase 6 ships rule "
                        "evaluation; ack-token consumption comes next."
                    ),
                    "echo": {
                        "rule_id": args.get("rule_id"),
                        "tool_name": args.get("tool_name"),
                    },
                }
            ),
        )
    ]
