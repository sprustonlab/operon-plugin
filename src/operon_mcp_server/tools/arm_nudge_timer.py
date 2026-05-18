"""`arm_nudge_timer` MCP tool. HIDDEN per SPEC §7.1.

Phase 8 nudge mechanism. Provides an in-process entry point to run
the nudge fire-or-exhaust check for the caller's pending-reply
state. Equivalent to the Stop hook's signal path, but skips the
control-envelope detour because this tool runs inside the MCP
server's event loop.

Hidden (never advertised in `tools/list`). Available for:
  - future hooks that prefer the type=mcp_tool form for some reason
  - manual testing / introspection from in-process scripts
  - skills that want to programmatically trigger a nudge check

Identity is env-anchored. No external input needed; the tool fires
nudges for whichever agent OPERON_AGENT_HANDLE resolves to.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

from .. import identity, nudge

TOOL_NAME = "arm_nudge_timer"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor.

    Hidden flag is enforced in `server._TOOL_VISIBILITY`; this
    descriptor exists for bookkeeping + future hook dispatch.
    """
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Hook-only. Runs the nudge fire-or-exhaust check for the "
            "calling Agent's pending-reply state. Skips the "
            "Stop-hook control-envelope detour by running in-process."
        ),
        inputSchema=INPUT_SCHEMA,
    )


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `arm_nudge_timer`."""
    del arguments
    handle = identity.read_env_handle()
    if handle is None:
        return [
            mcp_types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "identity_unbound",
                        "reason": (
                            f"environment variable {identity.ENV_HANDLE_VAR!r} "
                            "is not set; arm_nudge_timer requires an "
                            "env-anchored identity."
                        ),
                    }
                ),
            )
        ]
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        return [
            mcp_types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "identity_error",
                        "reason": str(exc),
                    }
                ),
            )
        ]
    if record is None:
        return [
            mcp_types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "no_handle_record",
                        "reason": (f"no handle record at _handles/{handle}.json"),
                    }
                ),
            )
        ]
    agent_name = record.get("agent_name")
    if not isinstance(agent_name, str) or not agent_name:
        return [
            mcp_types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "no_agent_name",
                        "reason": (
                            f"handle record for {handle!r} missing 'agent_name'"
                        ),
                    }
                ),
            )
        ]

    result = nudge.fire_due_nudges(agent_name)
    return [
        mcp_types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "agent": agent_name,
                    **result,
                }
            ),
        )
    ]
