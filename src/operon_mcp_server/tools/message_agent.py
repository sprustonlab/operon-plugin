"""Implementation of the `message_agent` MCP tool.

Per SPEC.md section 7 (`message_agent` row) and section 7.1 (visible to
All roles, eager-loaded), this tool writes a `deliver_message` envelope
into the target Agent's `mailbox/<target>/inbox/<id>.json`. The
sender's MCP subprocess does NOT push a notification into the target's
session -- `claude/channel` is one-way to its OWN session only
(RESEARCH §J.3). Local delivery is the target's own MCP subprocess's
responsibility via its filesystem-watch loop (`watch.py`, SPEC §6.6).

Identity gate: the sender field in the envelope is read from the
calling MCP subprocess's `OPERON_AGENT_HANDLE` env, NOT from any
LLM-supplied parameter. LLM-claimed senders are ignored.

Phase 4 scope: `requires_answer=true` is recorded in the envelope
payload so the receiver's watch loop can wire the reply-obligation
file in Phase 8. The nudge timer itself ships in Phase 8 (`nudge.py`).
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

from .. import identity, mailbox, roster

#: MCP tool name. Visible to All per SPEC §7.1.
TOOL_NAME = "message_agent"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Target Agent name. Must be a current roster entry "
                "(agents.json) for the active operon-run."
            ),
        },
        "message": {
            "type": "string",
            "description": (
                "Free-form message body delivered to the target's session "
                "via the target's own claude/channel push."
            ),
        },
        "requires_answer": {
            "type": "boolean",
            "description": (
                "If true, the target owes a reply; the target's MCP "
                "subprocess records the obligation in "
                "mailbox/<target>/_pending_reply_to.json and arms the "
                "nudge timer (Phase 8). Defaults to false."
            ),
            "default": False,
        },
    },
    "required": ["name", "message"],
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (All-visible)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Send a message to another Agent in the active operon-run. "
            "Writes a deliver_message envelope into the target's inbox; "
            "the target's own MCP subprocess surfaces the message in "
            "the target's session via its own claude/channel push. The "
            "sender identity is anchored to OPERON_AGENT_HANDLE and "
            "cannot be forged via tool arguments."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class MessageAgentError(RuntimeError):
    """Raised on validation or write failures; surfaces as a tool error."""


def _resolve_sender_name() -> str:
    """Return the calling Agent's name from the env-anchored handle.

    Raises `MessageAgentError` if the subprocess has no bound identity
    (env var unset, handle file missing, or record malformed). Mirrors
    the gate used by `spawn_agent._require_coordinator` but does not
    enforce a specific role -- any bound Agent may send messages.
    """
    handle = identity.read_env_handle()
    if handle is None:
        raise MessageAgentError(
            f"Environment variable '{identity.ENV_HANDLE_VAR}' is not set; "
            "message_agent requires an env-anchored identity (SPEC §6.5)."
        )
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        raise MessageAgentError(str(exc)) from exc
    if record is None:
        raise MessageAgentError(
            f"No handle record at _handles/{handle}.json; cannot resolve sender."
        )
    name = record.get("agent_name")
    if not isinstance(name, str) or not name:
        raise MessageAgentError(
            f"Handle record for '{handle}' missing 'agent_name' field."
        )
    return name


def _validate_target_exists(target: str) -> None:
    """Raise `MessageAgentError` if `target` is not in the active roster."""
    try:
        row = roster.find_agent(target)
    except roster.RosterError as exc:
        raise MessageAgentError(str(exc)) from exc
    if row is None:
        raise MessageAgentError(
            f"Target agent {target!r} is not in the roster (agents.json) "
            "for the active operon-run."
        )


def _do_send(args: dict[str, Any]) -> dict[str, Any]:
    """Core send logic (separated from MCP plumbing for clarity)."""
    target = args.get("name")
    message = args.get("message")
    requires_answer = bool(args.get("requires_answer", False))

    if not (isinstance(target, str) and target):
        raise MessageAgentError("'name' must be a non-empty string")
    if not (isinstance(message, str) and message):
        raise MessageAgentError("'message' must be a non-empty string")

    sender = _resolve_sender_name()
    _validate_target_exists(target)

    envelope = mailbox.build_envelope(
        sender=sender,
        target=target,
        kind=mailbox.KIND_DELIVER_MESSAGE,
        payload={
            "message_text": message,
            "requires_answer": requires_answer,
        },
    )
    written = mailbox.write_envelope(
        envelope,
        target_agent=target,
        kind=mailbox.KIND_DELIVER_MESSAGE,
    )

    # Phase 8: reply detection. If the SENDER's own pending-reply
    # state has any entries from `target`, clear them -- this message
    # IS the reply. Direct write to the sender's own
    # `_pending_reply_to.json` (single-writer: sender's own MCP).
    cleared: list[str] = []
    try:
        from .. import nudge as _nudge

        removed = _nudge.clear_pending_for_sender(
            agent_name=sender,
            sender_name=target,
        )
        cleared = [e.correlation_id for e in removed]
    except Exception:
        # Best-effort: reply detection is observability, not safety.
        # If it fails, the worst case is a stale nudge timer that
        # gets caught by the generation check next fire.
        pass

    result: dict[str, Any] = {
        "correlation_id": envelope["correlation_id"],
        "delivered_to": target,
        "envelope_path": str(written),
    }
    if cleared:
        result["cleared_pending_correlation_ids"] = cleared
    return result


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `message_agent`."""
    args = arguments or {}
    result = _do_send(args)
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
