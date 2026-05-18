"""Implementation of the `close_agent` MCP tool (Coordinator-only).

Per SPEC.md section 7 (`close_agent` row) and section 7.1
(Coordinator-only visibility). Writes a `kind=close` control envelope
into the target's `mailbox/<target>/control/<id>.json` AND directly
invokes `claude stop <session_id>` against the target's background
session to ensure termination. The target's row is then removed from
`agents.json`.

Per SPEC §7 `close_agent` row: the calling Agent cannot close itself,
and the last remaining Agent cannot be closed (otherwise the operon-run
has no Agents and is effectively dead, which is a Coordinator-protocol
error, not a `close_agent` outcome).

`claude stop` operates on `session_id` without channel routing, so the
Coordinator can invoke it directly across sessions per SPEC §7.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

import mcp.types as mcp_types

from .. import mailbox, roster
from . import message_agent as message_agent_tool
from . import spawn_agent as spawn_agent_tool

_log = logging.getLogger(__name__)

#: MCP tool name. Coordinator-only per SPEC §7.1.
TOOL_NAME = "close_agent"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Target Agent name. Must be a current roster entry, "
                "must not be the calling Agent, and must not be the "
                "last remaining Agent."
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
            "Terminate another Agent and remove its row from agents.json. "
            "Writes a kind=close control envelope, invokes `claude stop "
            "<session_id>` against the target's background session, and "
            "removes the roster row. Cannot close the calling Agent or "
            "the last remaining Agent."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class CloseAgentError(RuntimeError):
    """Raised on validation or write failures; surfaces as a tool error."""


def _require_coordinator() -> str:
    """Resolve caller; require role=coordinator. Returns the caller name."""
    try:
        record = spawn_agent_tool._require_coordinator()
    except spawn_agent_tool.SpawnAgentError as exc:
        raise CloseAgentError(str(exc)) from exc
    name = record.get("agent_name")
    if not isinstance(name, str) or not name:
        raise CloseAgentError(
            "Coordinator handle record is missing the 'agent_name' field."
        )
    return name


def _do_close(args: dict[str, Any]) -> dict[str, Any]:
    """Core close logic."""
    target = args.get("name")
    if not (isinstance(target, str) and target):
        raise CloseAgentError("'name' must be a non-empty string")

    sender = _require_coordinator()

    if target == sender:
        raise CloseAgentError(
            "Cannot close yourself; close_agent must target another Agent."
        )

    # Roster validation
    try:
        rows = roster.read_roster()
    except roster.RosterError as exc:
        raise CloseAgentError(str(exc)) from exc

    target_row: dict[str, Any] | None = None
    for row in rows:
        if row.get("name") == target:
            target_row = row
            break
    if target_row is None:
        raise CloseAgentError(
            f"Target agent {target!r} is not in the roster for the active run."
        )
    if len(rows) <= 1:
        raise CloseAgentError(
            "Cannot close the last remaining Agent; an operon-run must "
            "retain at least one Agent."
        )

    session_id = target_row.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        # Defensive: the roster row schema validator already enforces
        # this. If it slips through, prefer to surface the inconsistency
        # rather than skip the kill.
        raise CloseAgentError(
            f"Target roster row for {target!r} has no session_id; cannot stop."
        )

    # 1) Write the close envelope first. If the kill succeeds but the
    #    envelope is missing, the target had no chance to clean up; if
    #    the envelope is written but the kill fails, the target sees
    #    the envelope and self-terminates via its own watch loop.
    envelope = mailbox.build_envelope(
        sender=sender,
        target=target,
        kind=mailbox.KIND_CLOSE,
        payload={},
    )
    try:
        envelope_path = mailbox.write_envelope(
            envelope,
            target_agent=target,
            kind=mailbox.KIND_CLOSE,
        )
    except mailbox.MailboxError as exc:
        raise CloseAgentError(str(exc)) from exc

    # 2) Invoke `claude stop <daemonShort>`. Best-effort: failures are
    #    captured into the result but do not roll back the envelope or
    #    skip the roster removal (the target's own watch loop will
    #    handle self-stop via the envelope).
    #
    #    Carryover #4 (Phase 5 carryovers): `claude stop` takes the
    #    8-char daemonShort, NOT the full session_id UUID. Empirical:
    #    `claude stop <full-uuid>` errors "No job matching <uuid>".
    #    The daemonShort is the first 8 chars of the session_id
    #    (verified against `~/.claude/jobs/<short>/state.json`'s
    #    `sessionId` field for multiple spawns), so we derive it
    #    here rather than storing it separately in agents.json.
    daemon_short = session_id.split("-", 1)[0]
    stop_result: dict[str, Any] = {
        "invoked": True,
        "daemon_short": daemon_short,
    }
    try:
        proc = subprocess.run(
            ["claude", "stop", daemon_short],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        stop_result["returncode"] = proc.returncode
        # Truncate to keep the tool reply payload bounded.
        if proc.stdout:
            stop_result["stdout"] = proc.stdout.strip()[:512]
        if proc.stderr:
            stop_result["stderr"] = proc.stderr.strip()[:512]
    except FileNotFoundError as exc:
        stop_result["invoked"] = False
        stop_result["error"] = f"claude binary not found: {exc}"
    except subprocess.TimeoutExpired:
        stop_result["error"] = "claude stop timed out after 15s"
    except OSError as exc:
        stop_result["error"] = f"claude stop failed: {exc}"

    # 3) Remove the target row from agents.json. This is the
    #    Coordinator-only writer per SPEC §6.6; we are the Coordinator.
    try:
        roster.remove_agent(target)
    except roster.RosterError as exc:
        # The kill may have already succeeded; surfacing the removal
        # failure lets the user diagnose without losing the kill.
        _log.warning("close_agent: roster.remove_agent(%r) failed: %s", target, exc)
        return {
            "closed": False,
            "target": target,
            "session_id": session_id,
            "envelope_path": str(envelope_path),
            "stop": stop_result,
            "error": f"roster removal failed: {exc}",
        }

    return {
        "closed": True,
        "target": target,
        "session_id": session_id,
        "envelope_path": str(envelope_path),
        "stop": stop_result,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `close_agent`."""
    args = arguments or {}
    # Defensive: surface roster lookup errors as tool errors rather than
    # generic CloseAgentError messages.
    try:
        result = _do_close(args)
    except message_agent_tool.MessageAgentError as exc:
        raise CloseAgentError(str(exc)) from exc
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
