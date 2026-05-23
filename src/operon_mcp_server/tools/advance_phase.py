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

from .. import inbox, paths, subagent_install, workflow
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
            "rewrites phase_state.json and broadcasts per-role phase "
            "briefs to every team member's inbox file via the "
            "inbox-write primitive. Coordinator-only."
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
        raise AdvancePhaseError("Coordinator handle record is missing 'role' field.")
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


# -- Land 3: team inbox broadcast -----------------------------------------


def _read_phase_brief(
    workflow_root: Any, role: str, new_phase: str
) -> tuple[str, str] | None:
    """Resolve the per-role phase brief for ``new_phase``.

    Lookup order (matches Land 3 dispatch's priority rules):

      1. ``<workflow_root>/<role>/<new_phase>.md`` -- role-specific
         brief for the destination phase.
      2. ``<workflow_root>/<new_phase>.md`` -- a phase-level brief
         shared across roles (used as the fallback for members
         whose name does not match a role directory, e.g. the
         lead with name ``"team-lead"``).

    Returns ``(body_text, source_path_str)`` on first hit, or
    ``None`` if neither file exists. Bodies are returned verbatim
    -- no frontmatter stripping (briefs in operon's workflow
    library do not currently carry frontmatter; if that changes,
    transform here).
    """
    role_path = workflow_root / role / f"{new_phase}.md"
    if role_path.is_file():
        try:
            return role_path.read_text(encoding="utf-8"), str(role_path)
        except OSError:
            pass
    phase_path = workflow_root / f"{new_phase}.md"
    if phase_path.is_file():
        try:
            return phase_path.read_text(encoding="utf-8"), str(phase_path)
        except OSError:
            pass
    return None


def _build_brief_map(
    workflow_root: Any,
    members: list[dict[str, Any]],
    from_phase: str,
    new_phase: str,
    excluded: set[str],
) -> dict[str, tuple[str, str]]:
    """Compute per-member ``(text, source)`` brief mapping for the
    team broadcast.

    For each non-excluded member with a non-empty name:

      * Try the role-specific then phase-level lookup via
        :func:`_read_phase_brief`. If a body is found, that
        ``(body, source)`` pair is the recipient's brief.
      * Otherwise the member gets the fallback one-liner
        documented in the dispatch: ``"Phase advanced: <old> ->
        <new> (no brief content found)"``. ``source`` is the
        sentinel ``"<fallback>"`` so the response distinguishes
        real-file briefs from generated ones.

    A small ``[operon:phase-advance <new_phase>]`` tag is
    prepended to every body so the recipient's session can
    recognise the brief as operon-originated phase context
    (mirrors v2.9 plan section 5 step 4d).
    """
    brief_map: dict[str, tuple[str, str]] = {}
    fallback_body = (
        f"Phase advanced: {from_phase} -> {new_phase} (no brief content found)."
    )
    fallback_source = "<fallback>"
    for m in members:
        name = m.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in excluded:
            continue
        looked_up = _read_phase_brief(workflow_root, name, new_phase)
        if looked_up is None:
            body, source = fallback_body, fallback_source
        else:
            body, source = looked_up
        tagged = f"[operon:phase-advance {new_phase}]\n\n{body.rstrip()}\n"
        brief_map[name] = (tagged, source)
    return brief_map


def _broadcast_phase_brief(
    workflow_root: Any,
    from_phase: str,
    new_phase: str,
) -> dict[str, Any]:
    """Land 3: deliver per-role phase briefs to every team member's
    inbox after a successful phase advance.

    Resolves the team name from the active operon run via
    :func:`paths.active_run_dir` (Land 1 v2 convention:
    team_name == run_name). Reads the team roster fresh from the
    runtime-owned team config (no cache, per v2.9 plan section
    4.6) and writes one inbox entry per recipient via
    :func:`inbox.broadcast_to_team`. ``operon`` is excluded (it is
    the writer); the lead is included so the user's foreground
    session also sees the brief landed.

    Returns the manifest documented in the dispatch::

        {
          "team": "<team_name>",
          "recipients": [
            {"name": "...", "inbox_path": "...", "retries": int,
             "brief_source": "<path or <fallback>>"},
            ...
          ],
          "skipped": ["operon", ...],
          "errors": [{"name": "...", "error": "..."}, ...],
        }

    On any unrecoverable resolution failure (no active run, no
    team config, etc.) returns a structured ``error`` key in the
    manifest rather than raising -- the phase IS advanced by the
    time we get here, so a broadcast failure must not roll it back.
    """
    try:
        run_dir = paths.active_run_dir()
    except paths.OperonPathError as exc:
        return {
            "team": None,
            "recipients": [],
            "skipped": [],
            "errors": [{"name": "<active-run>", "error": str(exc)}],
        }
    team_name = run_dir.name
    excluded = {"operon"}
    members = inbox.read_team_members(team_name)
    brief_map = _build_brief_map(
        workflow_root=workflow_root,
        members=members,
        from_phase=from_phase,
        new_phase=new_phase,
        excluded=excluded,
    )

    def text_for(name: str) -> str | None:
        pair = brief_map.get(name)
        return pair[0] if pair else None

    result = inbox.broadcast_to_team(
        team_name=team_name,
        text_for=text_for,
        exclude_names=tuple(excluded),
    )

    # Fold per-recipient brief_source into the recipients records.
    for rec in result["recipients"]:
        name = rec.get("name")
        pair = brief_map.get(name) if isinstance(name, str) else None
        rec["brief_source"] = pair[1] if pair else None
    return result


async def _do_advance() -> dict[str, Any]:
    coord_name, coord_role = _require_coordinator()

    state = workflow.read_phase_state()
    workflow_id = state.get("workflow_id")
    current_phase = state.get("current_phase")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise AdvancePhaseError("phase_state.json missing non-empty 'workflow_id'.")
    if not isinstance(current_phase, str) or not current_phase:
        raise AdvancePhaseError("phase_state.json missing non-empty 'current_phase'.")

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

    # 2026-05-23 (Boaz directive): fail-loud if ANY member of the
    # active team config carries an agentType that is not a defined
    # role in the active workflow. The manual walkthrough surfaced
    # that TeamCreate's default lead agentType ("team-lead") does
    # NOT match the project_team workflow's "coordinator" role:
    # operon's broadcast looks up
    # ``workflows/<wf>/team-lead/<phase>.md``, finds nothing, and
    # silently degrades the lead's brief. We refuse to advance
    # rather than degrade silently; the user (or LLM) must
    # recreate the team with TeamCreate(agent_type=<valid role>).
    # Operon itself ("operon" member; synthetic external slot) is
    # NEVER an offender -- it is not a workflow participant. The
    # check runs BEFORE advance_checks so an invalid roster does
    # not pester the user with manual-confirm elicitations only to
    # then refuse the commit anyway.
    try:
        team_name = paths.active_run_dir().name
    except paths.OperonPathError as exc:
        raise AdvancePhaseError(
            f"Cannot resolve active operon run for roster check: {exc}"
        ) from exc
    try:
        valid_roles = subagent_install.discover_role_names(workflow_id)
    except subagent_install.SubagentInstallError as exc:
        raise AdvancePhaseError(
            f"Cannot enumerate workflow {workflow_id!r} role set: {exc}"
        ) from exc
    valid_role_set = set(valid_roles)
    members = inbox.read_team_members(team_name)
    offenders: list[dict[str, Any]] = []
    for m in members:
        name = m.get("name")
        if not isinstance(name, str) or not name:
            continue
        # `operon` is a synthetic external member, not a workflow
        # participant. Its agentType is intentionally the
        # `operon-stub` placeholder; do not flag it.
        if name == "operon":
            continue
        agent_type = m.get("agentType")
        if isinstance(agent_type, str) and agent_type in valid_role_set:
            continue
        offenders.append(
            {
                "name": name,
                "agentType": agent_type if isinstance(agent_type, str) else None,
            }
        )
    if offenders:
        return {
            "advanced": False,
            "error": "members_not_in_workflow_roster",
            "offenders": offenders,
            "valid_roles": sorted(valid_role_set),
            "active_workflow": workflow_id,
            "team_name": team_name,
            "suggested_action": (
                "Recreate the team with "
                f"TeamCreate(team_name={team_name!r}, "
                "agent_type=<one of valid_roles>) for the offending "
                "member. The team config is currently misaligned "
                "with the active workflow's role definitions; no "
                "per-(role, phase) brief lookup will match for the "
                "offending member."
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

    # Agent Teams Pivot Land 3 / Land 4: deliver per-role phase
    # briefs to every team member's inbox file via the inbox-write
    # primitive. Land 4 removed the legacy _notify_other_agents
    # mailbox-substrate broadcast that used to run alongside this
    # call -- inbox.broadcast_to_team is now the sole delivery
    # path. The broadcast is best-effort: per-recipient write
    # failures are captured in the returned manifest and do NOT
    # roll back the phase advance (already committed above).
    team_broadcast = _broadcast_phase_brief(
        workflow_root=workflow_root,
        from_phase=current_phase,
        new_phase=next_phase,
    )

    # Phase 13 Finding 3: render the caller's role brief for the
    # NEW phase so the Coordinator's LLM gets per-phase context.
    brief = spawn_agent_tool.assemble_caller_brief(workflow_id, coord_role, next_phase)
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
        "caller_brief": brief,
        # Land 3: per-role phase brief broadcast manifest.
        # Land 4 dropped the legacy "notified" field (legacy mailbox
        # substrate is gone; team_broadcast is the only delivery
        # surface).
        "team_broadcast": team_broadcast,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `advance_phase`."""
    del arguments  # no inputs
    result = await _do_advance()
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
