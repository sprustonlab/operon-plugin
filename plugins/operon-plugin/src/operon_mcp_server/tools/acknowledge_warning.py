"""`acknowledge_warning` MCP tool. All-visible per SPEC §7.1.

Phase 7 implementation. The LLM calls this in response to a warn-tier
rule fire (which the PreToolUse hook reports as `permissionDecision:
deny` with reason "call mcp__operon__acknowledge_warning..."). This
tool:

  1. Resolves the calling Agent's identity from env-anchored
     `OPERON_AGENT_HANDLE`.
  2. Validates that `rule_id` exists in the merged rule list AND has
     `enforcement: warn` (defense in depth: the LLM cannot ack a
     deny rule and thereby bypass it -- denies require
     `request_override`, which is gated by user elicitation).
  3. Writes an ack token to
     `<run-dir>/acks/<handle>/<rule_id>-<uuid4>.json` with a 60-second
     TTL.
  4. Audit-logs the ack via `type=ack_issued`.

The PreToolUse hook (`hooks/pretooluse.py`) checks for an active ack
token on every warn-rule fire and converts the deny to allow when
one is found, marking the audit row `outcome=acked`.

No elicitation: the LLM self-acknowledges. The user is NOT involved.
This is by design -- warn rules are advisory, not safety-critical.
For safety-critical (deny) rules see `request_override`.

Workers CAN call this freely (unlike `request_override` which is
Coordinator-only). The 60s TTL keeps each ack scoped to the current
turn so an old ack doesn't accidentally unblock a future warn fire.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

from .. import identity, rules, workflow

TOOL_NAME = "acknowledge_warning"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rule_id": {
            "type": "string",
            "description": (
                "Id of the warn-tier rule being acknowledged. Must "
                "match an entry in the merged rules.yaml + workflow "
                "rules with enforcement=warn."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "Free-form explanation of why the LLM is "
                "acknowledging this warning. Recorded in the token "
                "JSON and the audit log."
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
            "Acknowledge a warn-tier guardrail Rule so the same warn "
            "does not re-fire when the gated tool is retried within "
            "60 seconds. LLM self-acknowledges; no user involvement. "
            "Defense in depth: validates that the rule is warn-tier "
            "and fails for deny-tier rules (use request_override for "
            "those). All-visible per SPEC §7.1; workers can call too."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class AcknowledgeWarningError(RuntimeError):
    """Raised on validation failure; surfaces as a tool error."""


def _resolve_caller() -> tuple[str, str, str | None, str | None]:
    """Return (handle, agent_name, role, current_phase). Raises if
    identity is unresolvable -- ack-without-identity has no token
    naming target.
    """
    handle = identity.read_env_handle()
    if handle is None:
        raise AcknowledgeWarningError(
            f"environment variable '{identity.ENV_HANDLE_VAR}' is not set; "
            "acknowledge_warning requires an env-anchored identity."
        )
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        raise AcknowledgeWarningError(str(exc)) from exc
    if record is None:
        raise AcknowledgeWarningError(
            f"no handle record at _handles/{handle}.json"
        )
    name = record.get("agent_name")
    role = record.get("role")
    if not isinstance(name, str) or not name:
        raise AcknowledgeWarningError(
            f"handle record for {handle!r} missing 'agent_name'"
        )

    current_phase: str | None = None
    try:
        state = workflow.read_phase_state()
        cp = state.get("current_phase")
        if isinstance(cp, str) and cp:
            current_phase = cp
    except workflow.WorkflowError:
        current_phase = None

    return handle, name, role if isinstance(role, str) else None, current_phase


def _find_warn_rule(rule_id: str):
    """Find the named rule in the merged rule list. Returns the Rule
    or raises if not found / not warn-tier."""
    # Load workflow manifest for the rules: block, if any.
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
        # Fall back to plain 3-tier rules.yaml; workflow-embedded
        # rules just won't be visible. This is fine for ack
        # validation because workflow-rules are also limited to the
        # active workflow anyway.
        workflow_manifest = None

    try:
        rule_list = rules.load_merged_rules(
            workflow_manifest=workflow_manifest,
            workflow_source=workflow_source,
        )
    except rules.RulesError as exc:
        raise AcknowledgeWarningError(
            f"rules.yaml load failed: {exc}"
        ) from exc

    for r in rule_list:
        if r.id == rule_id:
            if r.enforcement != "warn":
                raise AcknowledgeWarningError(
                    f"rule {rule_id!r} has enforcement={r.enforcement!r}, "
                    f"not 'warn'. Use request_override for deny rules; "
                    f"log rules need no acknowledgment."
                )
            return r
    raise AcknowledgeWarningError(
        f"rule {rule_id!r} not found in the merged rule list"
    )


def _do_ack(args: dict[str, Any]) -> dict[str, Any]:
    rule_id = args.get("rule_id")
    reason = args.get("reason", "")
    if not isinstance(rule_id, str) or not rule_id:
        raise AcknowledgeWarningError("'rule_id' must be a non-empty string")
    if not isinstance(reason, str):
        raise AcknowledgeWarningError("'reason' must be a string")

    handle, agent_name, role, current_phase = _resolve_caller()
    # Validate (raises if rule_id missing or not warn-tier); return
    # value not used past validation.
    _find_warn_rule(rule_id)

    try:
        token_path = rules.write_token(
            kind="ack",
            rule_id=rule_id,
            agent_handle=handle,
            reason=reason,
            ttl_seconds=rules.ACK_TOKEN_TTL_SECONDS,
            one_shot=False,
        )
    except rules.RulesError as exc:
        raise AcknowledgeWarningError(str(exc)) from exc

    # Audit log: type=ack_issued, outcome=ack.
    rules.append_log_event(
        rules.build_log_event(
            event_type="ack_issued",
            outcome="ack",
            rule_id=rule_id,
            agent=agent_name,
            role=role,
            current_phase=current_phase,
            tool_name="",  # no specific tool at issue time
            tool_input=None,
            enforcement="warn",
            message=reason,
        )
    )

    return {
        "acknowledged": True,
        "rule_id": rule_id,
        "token_path": str(token_path),
        "ttl_seconds": rules.ACK_TOKEN_TTL_SECONDS,
        "agent": agent_name,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    args = arguments or {}
    try:
        result = _do_ack(args)
    except AcknowledgeWarningError as exc:
        result = {"acknowledged": False, "error": str(exc)}
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
