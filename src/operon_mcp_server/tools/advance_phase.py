"""`advance_phase` MCP tool (Coordinator-only).

Per SPEC §7 `advance_phase` row + §11.1. Runs the current phase's
advance checks in declaration order with AND semantics (short-circuit
on first failure). On all-pass, atomically rewrites `phase_state.json`
to the next phase and appends an `advance_history` entry.

Coordinator-only per SPEC §7.1. Identity is env-anchored (the LLM
cannot supply identity claims via tool arguments).

Per SPEC §11 `manual-confirm` row, the elicitation transport is
`session.elicit_form` from the MCP SDK -- the elicitation closure
that `workflow.run_advance_checks` injects into manual-confirm
checks does ONE thing: send the elicitation request from the
Coordinator's MCP subprocess (which is the user's foreground
session, so the dialog renders directly) and translate
`result.action == "accept" + result.content["confirm"] is True` back
to a boolean `passed`.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types
from mcp.server.lowlevel.server import request_ctx

from .. import mailbox, roster, workflow
from . import spawn_agent as spawn_agent_tool

#: MCP tool name. Coordinator-only per SPEC §7.1.
TOOL_NAME = "advance_phase"

#: Elicitation form schema for `manual-confirm` checks per SPEC §11.
_MANUAL_CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirm": {
            "type": "boolean",
            "title": "Approve advance?",
        }
    },
    "required": ["confirm"],
}

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (Coordinator-only)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Advance the active operon-session's current phase to the "
            "next phase per the workflow manifest. Runs the current "
            "phase's advance_checks in order (AND semantics, "
            "short-circuit on first failure). On all-pass, atomically "
            "rewrites phase_state.json and notifies every other Agent "
            "via mailbox envelope. Coordinator-only."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class AdvancePhaseError(RuntimeError):
    """Raised on validation or write failures; becomes a tool error."""


def _require_coordinator() -> tuple[str, str]:
    """Return (Coordinator agent_name, role); reject non-Coordinator.

    Phase 13: role is returned alongside name so the caller_brief
    renderer can scope the markdown lookup to the right tier.
    """
    try:
        record = spawn_agent_tool._require_coordinator()
    except spawn_agent_tool.SpawnAgentError as exc:
        raise AdvancePhaseError(str(exc)) from exc
    name = record.get("agent_name")
    if not isinstance(name, str) or not name:
        raise AdvancePhaseError(
            "Coordinator handle record is missing 'agent_name' field."
        )
    role = record.get("role")
    if not isinstance(role, str) or not role:
        raise AdvancePhaseError(
            "Coordinator handle record is missing 'role' field."
        )
    return name, role


def _build_elicit_closure():
    """Return an async callable that issues a manual-confirm elicitation.

    Closes over `request_ctx.get().session.elicit_form` so the
    closure can be passed into `checks/builtins.py` (which is leaf-tier
    and never imports the MCP SDK). The returned coroutine takes the
    check's `prompt` string and returns a bool indicating user
    approval.
    """
    ctx = request_ctx.get()
    session = ctx.session

    async def _elicit(prompt: str) -> bool:
        result = await session.elicit_form(
            message=prompt, requestedSchema=_MANUAL_CONFIRM_SCHEMA
        )
        # ElicitResult has `action` (accept/decline/cancel) and
        # `content` (the form data on accept).
        action = getattr(result, "action", None)
        if action != "accept":
            return False
        content = getattr(result, "content", None) or {}
        return bool(content.get("confirm"))

    return _elicit


def _notify_other_agents(
    coordinator_name: str,
    from_phase: str,
    to_phase: str,
) -> list[str]:
    """Drop one `kind=deliver_message` envelope into each non-Coordinator
    Agent's inbox per SPEC §11.1 step 4.

    Returns list of agent names notified. Best-effort: per-target write
    failures are logged but do not roll back the advance (the advance
    is already committed by this point).
    """
    notified: list[str] = []
    try:
        rows = roster.read_roster()
    except roster.RosterError:
        return notified
    for row in rows:
        target = row.get("name")
        if not isinstance(target, str) or not target:
            continue
        if target == coordinator_name:
            continue
        envelope = mailbox.build_envelope(
            sender=coordinator_name,
            target=target,
            kind=mailbox.KIND_DELIVER_MESSAGE,
            payload={
                "message_text": (
                    f"Phase advanced from {from_phase!r} to "
                    f"{to_phase!r}. Re-read phase_state.json / "
                    f"get_applicable_rules for the new constraints."
                ),
                "requires_answer": False,
                "_kind_hint": "phase_advance_notification",
            },
        )
        try:
            mailbox.write_envelope(
                envelope, target_agent=target, kind=mailbox.KIND_DELIVER_MESSAGE
            )
            notified.append(target)
        except mailbox.MailboxError:
            # Skip silently; the audit trail of the advance is in
            # phase_state.json.advance_history.
            continue
    return notified


async def _do_advance() -> dict[str, Any]:
    coord_name, coord_role = _require_coordinator()

    state = workflow.read_phase_state()
    workflow_id = state.get("workflow_id")
    current_phase = state.get("current_phase")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise AdvancePhaseError(
            "phase_state.json missing non-empty 'workflow_id'."
        )
    if not isinstance(current_phase, str) or not current_phase:
        raise AdvancePhaseError(
            "phase_state.json missing non-empty 'current_phase'."
        )

    try:
        decl = workflow.load_workflow(workflow_id)
    except workflow.WorkflowError as exc:
        raise AdvancePhaseError(str(exc)) from exc

    phase_decl = decl.phase(current_phase)
    if phase_decl is None:
        raise AdvancePhaseError(
            f"Current phase {current_phase!r} not found in workflow "
            f"{workflow_id!r} (manifest at {decl.source_path})."
        )

    next_phase = decl.next_phase_after(current_phase)
    if next_phase is None:
        return {
            "advanced": False,
            "from": current_phase,
            "to": None,
            "outcomes": [],
            "reason": (
                f"Current phase {current_phase!r} is the last phase in "
                f"workflow {workflow_id!r}; no advance possible."
            ),
        }

    # Run advance checks. Inject seam params:
    # - workflow_root for relative path resolution + command cwd
    # - state.json path for artifact-dir-ready-check
    # - elicitation closure for manual-confirm
    workflow_root = decl.source_path.parent
    state_path = workflow.state_file()
    elicit = _build_elicit_closure()

    outcomes = await workflow.run_advance_checks(
        phase_decl.advance_checks,
        workflow_root=workflow_root,
        state_path=state_path,
        elicit=elicit,
    )

    all_passed = all(o.passed for o in outcomes)
    outcomes_payload = [
        {"check_type": o.check_type, "passed": o.passed, "evidence": o.evidence}
        for o in outcomes
    ]

    if not all_passed:
        return {
            "advanced": False,
            "from": current_phase,
            "to": next_phase,
            "outcomes": outcomes_payload,
            "reason": "One or more advance checks did not pass.",
        }

    # All checks passed -- commit the advance.
    history_entry = workflow.commit_advance(
        workflow_id=workflow_id,
        current_phase=current_phase,
        next_phase=next_phase,
        triggered_by=coord_name,
    )

    # Best-effort notification to non-Coordinator Agents per §11.1 step 4.
    notified = _notify_other_agents(coord_name, current_phase, next_phase)

    # Phase 13 Finding 3: render the caller's role brief for the
    # NEW phase so the Coordinator's LLM gets per-phase context.
    brief = spawn_agent_tool.assemble_caller_brief(
        workflow_id, coord_role, next_phase
    )
    if brief is None:
        brief = spawn_agent_tool.absent_caller_brief(
            workflow_id,
            coord_role,
            next_phase,
            reason=(
                f"No {coord_role}/{next_phase}.md or "
                f"{coord_role}/identity.md in any tier for workflow "
                f"{workflow_id!r}; caller will operate without a brief."
            ),
        )

    return {
        "advanced": True,
        "from": current_phase,
        "to": next_phase,
        "at": history_entry.get("at"),
        "outcomes": outcomes_payload,
        "notified": notified,
        "caller_brief": brief,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `advance_phase`."""
    del arguments  # no inputs
    result = await _do_advance()
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
