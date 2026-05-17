"""Implementation of the `whoami` MCP tool.

Per SPEC.md section 7 (`whoami` row), this tool returns the canonical
identity tuple for the calling MCP subprocess. Per SPEC.md section 7.1
it is visible to All roles.

The handle anchored in `OPERON_AGENT_HANDLE` is the authoritative
identity source; LLM-supplied claims are ignored. All resolution
happens in `operon_mcp_server.identity`.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

from .. import identity

#: MCP tool name; visible in `tools/list` per SPEC section 7.1.
TOOL_NAME = "whoami"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor advertised in `tools/list`."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Return the canonical identity of the calling Agent: "
            "{name, role, workflow_id, current_phase, cwd, session_id}. "
            "Identity is anchored to the OPERON_AGENT_HANDLE env var and "
            "looked up in _handles/<handle>.json -- LLM-supplied claims "
            "are ignored."
        ),
        inputSchema=INPUT_SCHEMA,
    )


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `whoami`.

    `arguments` is ignored (the input schema permits no fields). On
    success returns a single `TextContent` with a JSON-encoded identity
    object. On failure raises `identity.IdentityError` so the MCP
    framework surfaces it as a tool error.
    """
    del arguments  # tool takes no inputs
    result = identity.whoami()
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
