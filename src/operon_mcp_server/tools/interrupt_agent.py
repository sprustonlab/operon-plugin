"""Implementation of the `interrupt_agent` MCP tool (Coordinator-only).

Per SPEC.md section 7 (`interrupt_agent` row) and section 7.1
(Coordinator-only visibility). Writes a `kind=interrupt` control
envelope into `mailbox/<target>/control/<id>.json`. Local delivery is
the target's responsibility:

- The target's MCP subprocess detects the new control file via its
  filesystem-watch loop (`watch.py`, SPEC §6.6) and pushes a
  deny-context notification into its OWN session via its OWN
  `claude/channel`.
- The target's `PreToolUse` hook (Phase 6+) reads the control directory
  at next tool dispatch and returns `permissionDecision: "deny"` with
  `permissionDecisionReason` carrying the optional redirect prompt.

Per the SPEC amendment in this phase: `redirect` is NOT a separate
envelope kind. Redirect prompts ride on the `interrupt` payload at
`payload.redirect_prompt`.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

from .. import mailbox, roster
from . import message_agent as message_agent_tool
from . import spawn_agent as spawn_agent_tool

#: MCP tool name. Coordinator-only per SPEC §7.1.
TOOL_NAME = "interrupt_agent"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Target Agent name. Must be a current roster entry."
            ),
        },
        "prompt": {
            "type": "string",
            "description": (
                "Optional redirect prompt. Available to the model as deny "
                "context but NOT auto-injected as a new user turn."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (Coordinator-only)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Interrupt another Agent at its next tool dispatch. Writes a "
            "kind=interrupt control envelope; the target's MCP subprocess "
            "surfaces a deny-context notification in its own session, "
            "and the target's PreToolUse hook returns deny with the "
            "optional redirect prompt as the reason."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class InterruptAgentError(RuntimeError):
    """Raised on validation or write failures; surfaces as a tool error."""


def _require_coordinator() -> str:
    """Resolve caller; require role=coordinator. Returns the sender name."""
    # Reuse spawn_agent's gate to keep the "Coordinator only" check in
    # one place. _require_coordinator raises SpawnAgentError on failure;
    # we translate so callers see InterruptAgentError.
    try:
        record = spawn_agent_tool._require_coordinator()
    except spawn_agent_tool.SpawnAgentError as exc:
        raise InterruptAgentError(str(exc)) from exc
    name = record.get("agent_name")
    if not isinstance(name, str) or not name:
        raise InterruptAgentError(
            "Coordinator handle record is missing the 'agent_name' field."
        )
    return name


def _do_interrupt(args: dict[str, Any]) -> dict[str, Any]:
    """Core interrupt logic."""
    target = args.get("name")
    prompt = args.get("prompt")
    if not (isinstance(target, str) and target):
        raise InterruptAgentError("'name' must be a non-empty string")
    if prompt is not None and not isinstance(prompt, str):
        raise InterruptAgentError("'prompt', if supplied, must be a string")

    sender = _require_coordinator()

    try:
        message_agent_tool._validate_target_exists(target)
    except message_agent_tool.MessageAgentError as exc:
        raise InterruptAgentError(str(exc)) from exc

    if target == sender:
        raise InterruptAgentError(
            "Cannot interrupt yourself; interrupt_agent must target another Agent."
        )

    payload: dict[str, Any] = {}
    if prompt:
        payload["redirect_prompt"] = prompt

    envelope = mailbox.build_envelope(
        sender=sender,
        target=target,
        kind=mailbox.KIND_INTERRUPT,
        payload=payload,
    )
    written = mailbox.write_envelope(
        envelope,
        target_agent=target,
        kind=mailbox.KIND_INTERRUPT,
    )
    return {
        "correlation_id": envelope["correlation_id"],
        "target": target,
        "envelope_path": str(written),
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `interrupt_agent`."""
    args = arguments or {}
    # Defensive: surface roster lookup errors as tool errors rather than
    # generic InterruptAgentError messages.
    try:
        result = _do_interrupt(args)
    except roster.RosterError as exc:
        raise InterruptAgentError(str(exc)) from exc
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
