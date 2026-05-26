"""Implementation of the `whoami` MCP tool.

Returns the lead's bootstrap identity. Under in-process Agent
Teams operon's MCP is a singleton in the lead's claude process
and serves every teammate's tool call through one stdio
transport; the B.0 probe (Land 6 era) empirically confirmed
Anthropic's runtime does NOT propagate teammate identity via
MCP ``_meta``/``clientInfo``.

Land 7 reversed Land 6's ``caller_name`` argument: identity
queries for teammates now go through the inbox-channel protocol
(``inbox_reader.py`` + ``query_protocol.py``). A teammate that
needs its own identity does ``SendMessage(to="operon",
text="[OPERON_QUERY] whoami")`` and reads the reply from its
inbox. The runtime stamps the inbox entry's ``from`` field
server-side and a teammate cannot spoof it -- that is the trust
anchor identity now uses.

This tool itself returns ONLY the lead's identity (no
``caller_name`` argument). A teammate that calls it via the MCP
proxy sees the lead's bootstrap data, which is technically
correct: the singleton MCP IS the lead.
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
            "Return the LEAD's canonical identity: {name, role, "
            "workflow_id, current_phase, cwd, session_id}. "
            "TEAMMATES: do NOT call this for your own identity -- "
            "operon's MCP is the lead's singleton, so this surface "
            "reports the lead. Instead, send a SendMessage to the "
            "operon team-member with text '[OPERON_QUERY] whoami'; "
            "operon will write the verified reply to your inbox in "
            "a subsequent turn."
        ),
        inputSchema=INPUT_SCHEMA,
    )


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `whoami`.

    Returns the lead's bootstrap identity. Per Land 7 the
    teammate-aware path runs through the inbox-channel protocol
    rather than an MCP argument.
    """
    del arguments  # this tool takes no inputs
    try:
        result = identity.whoami()
    except identity.IdentityError as exc:
        result = {"error": "identity_unbound", "reason": str(exc)}
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
