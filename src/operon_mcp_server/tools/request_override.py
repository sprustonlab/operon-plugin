"""`request_override` MCP tool. All-visible per SPEC §7.1; runtime-
gated to Coordinator-only.

Phase 7 implementation. The LLM calls this in response to a deny-tier
rule fire (the PreToolUse hook reports `permissionDecision: deny`
with a reason suggesting `mcp__operon__request_override`). This tool:

  1. Resolves caller identity from env-anchored OPERON_AGENT_HANDLE.
  2. Coordinator-only at runtime. Workers receive a structured
     `{"approved": false, "reason": "workers_cannot_request_override",
     ...}` and are told to message the Coordinator instead. The tool
     is listed as All-visible per SPEC §7.1 so the LLM sees it in
     tools/list and gets a clear error rather than tool-not-found.
  3. Validates that `rule_id` exists in the merged rules and has
     `enforcement: deny` (or matches a fail-closed hardcoded rule_id).
  4. Issues `elicitation/create` with a yes/no schema; surfaces the
     LLM's reason verbatim so the user can make an informed call.
  5. On accept + `approve: true`: writes a ONE-SHOT override token to
     `<run-dir>/overrides/<handle>/<rule_id>-<uuid4>.json`. The
     PreToolUse hook consumes (unlinks) the token on next match.
  6. On decline OR `approve: false`: returns
     `{"approved": false, "reason": "user_declined"}`. No token
     written.
  7. Audit log: `type=override_requested` BEFORE the elicit, then
     `type=override_granted` or `type=override_declined` AFTER the
     user's decision.

Workers/Coordinator distinction is intentional per claudechic's
brief. Routing worker override requests through the Coordinator's
mailbox is a future enhancement (SPEC §9); for Phase 7 we take the
simple path.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

from .. import elicit, identity, rules, workflow

TOOL_NAME = "request_override"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rule_id": {
            "type": "string",
            "description": (
                "Id of the deny-tier rule being overridden. Must "
                "match an entry in the merged rules.yaml + workflow "
                "rules, OR a hardcoded fail-closed rule id like "
                "'no_rm_rf'."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "Free-form explanation surfaced verbatim to the user "
                "in the elicitation dialog. Be specific so the user "
                "can decide; vague reasons should be expected to be "
                "declined."
            ),
        },
    },
    "required": ["rule_id", "reason"],
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Request user approval to override a deny-tier guardrail "
            "Rule on the next retry of the gated tool. Coordinator-"
            "only at runtime (workers receive a structured error; "
            "ask the Coordinator instead). Fires elicitation/create "
            "with the LLM's reason; on accept writes a one-shot "
            "override token that the PreToolUse hook consumes on the "
            "next matching rule fire."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class RequestOverrideError(RuntimeError):
    """Raised on validation failures; surfaces as a tool error."""


#: Hardcoded fail-closed rule ids that the override flow honors
#: regardless of rules.yaml state. Must match
#: `hooks/pretooluse.py._FAILCLOSED_DENY` rule_ids. The pretooluse
#: hook also checks for override tokens against fail-closed matches
#: with audit-tag `overridden_failclosed`.
_FAILCLOSED_RULE_IDS = frozenset({"no_rm_rf"})


def _resolve_caller() -> tuple[str, str, str, str | None]:
    """Return (handle, agent_name, role, current_phase). Raises if
    identity is unresolvable."""
    handle = identity.read_env_handle()
    if handle is None:
        raise RequestOverrideError(
            f"environment variable '{identity.ENV_HANDLE_VAR}' is not set; "
            "request_override requires an env-anchored identity."
        )
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        raise RequestOverrideError(str(exc)) from exc
    if record is None:
        raise RequestOverrideError(
            f"no handle record at _handles/{handle}.json"
        )
    name = record.get("agent_name")
    role = record.get("role")
    if not isinstance(name, str) or not name:
        raise RequestOverrideError(
            f"handle record for {handle!r} missing 'agent_name'"
        )
    if not isinstance(role, str) or not role:
        raise RequestOverrideError(
            f"handle record for {handle!r} missing 'role'"
        )

    current_phase: str | None = None
    try:
        state = workflow.read_phase_state()
        cp = state.get("current_phase")
        if isinstance(cp, str) and cp:
            current_phase = cp
    except workflow.WorkflowError:
        current_phase = None

    return handle, name, role, current_phase


def _validate_deny_rule(rule_id: str) -> None:
    """Confirm `rule_id` is a known deny-tier rule (rules.yaml entry
    OR fail-closed hardcoded set). Raises on mismatch."""
    if rule_id in _FAILCLOSED_RULE_IDS:
        return

    workflow_manifest = None
    workflow_source = None
    try:
        state = workflow.read_phase_state()
        wid = state.get("workflow_id")
        if isinstance(wid, str) and wid:
            decl = workflow.load_workflow(wid)
            import yaml as _yaml
            data = _yaml.safe_load(decl.source_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                workflow_manifest = data
                workflow_source = decl.source_path
    except (workflow.WorkflowError, OSError, Exception):
        workflow_manifest = None

    try:
        rule_list = rules.load_merged_rules(
            workflow_manifest=workflow_manifest,
            workflow_source=workflow_source,
        )
    except rules.RulesError as exc:
        raise RequestOverrideError(
            f"rules.yaml load failed: {exc}"
        ) from exc

    for r in rule_list:
        if r.id == rule_id:
            if r.enforcement != "deny":
                raise RequestOverrideError(
                    f"rule {rule_id!r} has enforcement={r.enforcement!r}; "
                    f"override only applies to deny rules. Use "
                    f"acknowledge_warning for warn rules."
                )
            return
    raise RequestOverrideError(
        f"rule {rule_id!r} not found in merged rules or fail-closed set"
    )


def _log_event(
    event_type: str,
    outcome: str,
    *,
    rule_id: str,
    agent_name: str,
    role: str | None,
    current_phase: str | None,
    message: str,
) -> None:
    rules.append_log_event(
        rules.build_log_event(
            event_type=event_type,
            outcome=outcome,
            rule_id=rule_id,
            agent=agent_name,
            role=role,
            current_phase=current_phase,
            tool_name="",
            tool_input=None,
            enforcement="deny",
            message=message,
        )
    )


async def _do_request(args: dict[str, Any]) -> dict[str, Any]:
    rule_id = args.get("rule_id")
    reason = args.get("reason", "")
    if not isinstance(rule_id, str) or not rule_id:
        raise RequestOverrideError("'rule_id' must be a non-empty string")
    if not isinstance(reason, str) or not reason:
        raise RequestOverrideError("'reason' must be a non-empty string")

    handle, agent_name, role, current_phase = _resolve_caller()

    if role != "coordinator":
        # Soft reject for non-Coordinator agents. Workers can't render
        # the elicitation dialog (no TTY in bg sessions) and routing
        # the request through the Coordinator's mailbox is a future
        # enhancement. Return a structured payload instead of
        # raising so the LLM gets actionable guidance.
        return {
            "approved": False,
            "reason": "workers_cannot_request_override",
            "detail": (
                "request_override is restricted to the Coordinator at "
                "runtime. Workers cannot render the user elicitation "
                "dialog. Use message_agent(name='Coordinator', ...) "
                "to ask the Coordinator to make the override request "
                "on your behalf."
            ),
            "rule_id": rule_id,
            "your_role": role,
        }

    _validate_deny_rule(rule_id)

    # Audit BEFORE the elicit (so an aborted/timeout elicit still
    # leaves a trace).
    _log_event(
        event_type="override_requested",
        outcome="pending",
        rule_id=rule_id,
        agent_name=agent_name,
        role=role,
        current_phase=current_phase,
        message=reason,
    )

    msg = (
        f"Agent '{agent_name}' (role: {role}) wants to override the "
        f"'{rule_id}' rule.\n\n"
        f"Reason given:\n  {reason}\n\n"
        f"Approve this override?"
    )
    approved = await elicit.confirm(msg)

    if not approved:
        _log_event(
            event_type="override_declined",
            outcome="declined",
            rule_id=rule_id,
            agent_name=agent_name,
            role=role,
            current_phase=current_phase,
            message=reason,
        )
        return {
            "approved": False,
            "reason": "user_declined",
            "rule_id": rule_id,
        }

    # Approved -- write the one-shot token.
    try:
        token_path = rules.write_token(
            kind="override",
            rule_id=rule_id,
            agent_handle=handle,
            reason=reason,
            ttl_seconds=None,
            one_shot=True,
        )
    except rules.RulesError as exc:
        raise RequestOverrideError(str(exc)) from exc

    _log_event(
        event_type="override_granted",
        outcome="overridden",
        rule_id=rule_id,
        agent_name=agent_name,
        role=role,
        current_phase=current_phase,
        message=reason,
    )

    return {
        "approved": True,
        "rule_id": rule_id,
        "token_path": str(token_path),
        "one_shot": True,
        "agent": agent_name,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    args = arguments or {}
    try:
        result = await _do_request(args)
    except RequestOverrideError as exc:
        result = {"approved": False, "error": str(exc)}
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
