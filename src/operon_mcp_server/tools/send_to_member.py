"""`send_to_member` MCP tool. All-visible.

Land 2 of the Agent Teams Pivot
(``docs/AGENT_TEAMS_PIVOT_PLAN.md`` v2.9 section 4.3 component 3,
section 8 Land 2 -- inbox-write primitive surface only;
advance_phase brief delivery is Land 3, not here).

Wraps :func:`operon_mcp_server.inbox.write_to_member_inbox` in
an MCP tool surface so the lead's LLM can write an operon-authored
entry to a team member's inbox file. Inputs:

  * ``name`` : the recipient member name as it appears in
    ``~/.claude/teams/<team>/config.json``'s ``members[].name``.
  * ``text`` : the message body.

The tool resolves the team name from the active operon run
(``<project>/.operon/_active.json``); team_name == run_name by
the Land-1 v2 convention (the operon run_name is also the
Anthropic team name the user passed to ``TeamCreate``).

Empirical hypothesis this tool proves or disproves: the
Anthropic runtime delivers ANY well-formed inbox entry to the
recipient teammate, regardless of which process wrote the bytes.
If operon writes to ``inboxes/<recipient>.json`` and the
recipient's session subsequently shows the message, the inbox
substrate is the shared transport plan section 4.3 component 3
assumes. If delivery does NOT happen, that's an empirical
finding -- the primitive still ships; we capture the result and
revise the design.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp.types as mcp_types

from .. import inbox, paths

_log = logging.getLogger(__name__)

#: MCP tool name. All-visible per Land 2 (any role may need to
#: send a free-form message into the team substrate).
TOOL_NAME = "send_to_member"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Recipient team-member name (must match a "
                "members[].name in ~/.claude/teams/<team>/"
                "config.json)."
            ),
        },
        "text": {
            "type": "string",
            "description": "Message body to deliver to the recipient.",
        },
    },
    "required": ["name", "text"],
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (All-visible)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Write an operon-authored entry to a team member's "
            "inbox file at ~/.claude/teams/<team>/inboxes/"
            "<name>.json. The team name is the active operon "
            "run name (set by activate_workflow, which requires "
            "TeamCreate to have run first). The Anthropic runtime "
            "delivers the entry on the recipient's next turn "
            "boundary. Returns "
            "{success: bool, recipient: str, inbox_path: str, "
            "retries: int} on success or "
            "{success: false, error: 'no_active_operon_run' | "
            "'inbox_write_failed', ...} on failure. All roles."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class SendToMemberError(RuntimeError):
    """Raised on validation or write failures inside this tool."""


def _resolve_team_name() -> str | None:
    """Return the active operon run name (== team name), or ``None``
    if there is no active operon run.

    Mirrors the resolution pattern in ``tools/activate_workflow.py``:
    use ``paths.active_run_dir().name``. Treats every
    ``OperonPathError`` as "no active run" since the caller's
    structured-error response is the appropriate downstream
    treatment.
    """
    try:
        run_dir = paths.active_run_dir()
    except paths.OperonPathError:
        return None
    return run_dir.name


def _do_send(args: dict[str, Any]) -> dict[str, Any]:
    name = args.get("name")
    text = args.get("text")
    if not (isinstance(name, str) and name):
        raise SendToMemberError("'name' must be a non-empty string.")
    if not (isinstance(text, str) and text):
        raise SendToMemberError("'text' must be a non-empty string.")

    team_name = _resolve_team_name()
    if team_name is None:
        return {
            "success": False,
            "error": "no_active_operon_run",
            "message": (
                "No active operon run. Call activate_workflow "
                "(which requires TeamCreate first) before "
                "send_to_member."
            ),
        }

    entry = inbox.build_operon_entry(team_name=team_name, text=text)
    try:
        result = inbox.write_to_member_inbox(
            team_name=team_name,
            recipient_name=name,
            entry=entry,
        )
    except inbox.InboxWriteError as exc:
        return {
            "success": False,
            "error": "inbox_write_failed",
            "message": str(exc),
            "team": team_name,
            "recipient": name,
        }
    return {
        "success": True,
        "recipient": name,
        "team": team_name,
        "inbox_path": result["inbox_path"],
        "entries_after_write": result["entries_after_write"],
        "retries": result["retries"],
        "entry": entry,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `send_to_member`."""
    args = arguments or {}
    try:
        result = _do_send(args)
    except SendToMemberError as exc:
        result = {"success": False, "error": "validation_failed", "message": str(exc)}
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
