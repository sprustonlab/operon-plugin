"""Implementation of the `whoami` MCP tool.

Returns the canonical identity tuple for the calling team member.

Land 6 (caller_name): under in-process Agent Teams, operon's MCP
runs as a singleton in the lead's claude process and serves every
teammate's tool call through one stdio transport. The B.0 probe
(commits ``b1571bf`` + ``2b4a7c3``, rolled back in this commit)
empirically confirmed Anthropic's runtime does NOT forward
teammate-identifying metadata via the JSON-RPC `_meta` field.
Operon distinguishes callers via the operon-controlled
``[OPERON IDENTITY]`` directive injected into every Agent spawn's
first turn by ``plugins/operon-plugin/hooks/pretooluse.py`` (Land 5
WA1 branch). The directive instructs the teammate to pass
``caller_name=<name>`` on every ``mcp__operon__*`` call. Operon
verifies the supplied name against the team roster at
``~/.claude/teams/<team>/config.json`` ``members[]`` before
trusting it (impersonation defense in
``identity.resolve_caller_identity``). When ``caller_name`` is
omitted -- the lead's own calls -- the response describes the
lead's bootstrap identity, preserving prior behavior.
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
    "properties": {
        "caller_name": {
            "type": "string",
            "description": (
                "Optional. Operon team-member name supplied by the "
                "teammate's LLM per the [OPERON IDENTITY] spawn-time "
                "directive. Verified against the team roster; an "
                "unknown name falls back to the lead's identity with "
                "a warning log. Omit (or pass empty) when the LEAD's "
                "LLM calls this tool."
            ),
        },
    },
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor advertised in `tools/list`."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Return the canonical identity of the calling team "
            "member: {name, role, workflow_id, current_phase, cwd, "
            "session_id, source, ...}. When called from a teammate, "
            "pass caller_name=<your team-member name> so operon "
            "can resolve your identity from the team roster. When "
            "called from the lead, omit caller_name -- you receive "
            "the lead's bootstrap identity. Identity is verified "
            "against ~/.claude/teams/<team>/config.json members[]; "
            "unknown caller_name values fall back to the lead's "
            "identity with a warning."
        ),
        inputSchema=INPUT_SCHEMA,
    )


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `whoami`.

    Resolves the caller's identity via
    :func:`identity.resolve_caller_identity`, which delegates to the
    bootstrap (lead) identity when ``caller_name`` is omitted and to
    the team roster when supplied. Never raises -- the resolver
    returns a None-filled stub if the bootstrap identity is itself
    unbound.
    """
    args = arguments or {}
    caller_name = args.get("caller_name")
    if not isinstance(caller_name, str):
        caller_name = None
    result = identity.resolve_caller_identity(caller_name)
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
