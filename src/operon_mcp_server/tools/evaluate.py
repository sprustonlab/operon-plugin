"""`evaluate` MCP tool. HIDDEN per SPEC §7.1 (hook-only).

Invoked by the `PreToolUse` hook via `type: mcp_tool`. The hook passes
only what Claude Code surfaces in the PreToolUse payload (`tool_name`,
`tool_input`). This tool derives the rest:

- caller's `role` from env-anchored `OPERON_AGENT_HANDLE` ->
  `_handles/<handle>.json`
- `current_phase` from `phase_state.json`
- merged Rule list from `<plugin>/rules.yaml` + user-tier +
  project-tier + active workflow's `rules:` section

Calls the pure `_evaluate(tool_name, tool_input, role, phase, rules)`
function, appends to `guardrail_log.jsonl` for log-tier matches, and
returns a hook-decision JSON payload per SPEC §8:

- `deny` rule fired -> `permissionDecision: "deny"` with the rule
  message as `permissionDecisionReason`
- `warn` rule fired -> `permissionDecision: "ask"` with the rule
  message as `permissionDecisionReason`
- `log` rule fired -> write audit event, return ALLOW (no
  `permissionDecision` field)
- no rule matched -> return ALLOW (no `permissionDecision`)

Phase 6 scope: no override/ack token consumption (Phase 7). A `deny`
match returns a structured deny here; Phase 7 will check for a
matching `overrides/<command_hash>.json` token first and bypass the
deny if granted.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp.types as mcp_types

from .. import identity, rules, workflow

_log = logging.getLogger(__name__)

#: MCP tool name. HIDDEN per SPEC §7.1 (never in `tools/list`).
TOOL_NAME = "evaluate"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_name": {
            "type": "string",
            "description": "Name of the tool Claude is about to invoke.",
        },
        "tool_input": {
            "type": "object",
            "description": "The tool's arguments object.",
            "additionalProperties": True,
        },
    },
    "required": ["tool_name"],
    "additionalProperties": True,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor.

    Server-wide hidden-from-`tools/list` flag is enforced in
    `server._TOOL_VISIBILITY`; this descriptor exists for bookkeeping
    + hook dispatch only.
    """
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Hook-only. PreToolUse evaluation against the merged "
            "guardrail Rule list. Returns hook-decision JSON; not "
            "advertised to the LLM."
        ),
        inputSchema=INPUT_SCHEMA,
    )


def _resolve_caller_identity() -> tuple[str | None, str | None, str | None]:
    """Return (agent_name, role, current_phase). Any field may be None
    if the chain breaks; the rules engine tolerates None and treats
    role-scoped rules as not-matching."""
    handle = identity.read_env_handle()
    if handle is None:
        return None, None, None
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        _log.warning("evaluate: handle read failed: %s", exc)
        return None, None, None
    if record is None:
        return None, None, None
    name = record.get("agent_name")
    role = record.get("role")

    # current_phase comes from phase_state.json; tolerate absence
    # (means the workflow has not yet been activated, in which case
    # phase-scoped rules don't fire).
    current_phase: str | None = None
    try:
        state = workflow.read_phase_state()
        cp = state.get("current_phase")
        if isinstance(cp, str) and cp:
            current_phase = cp
    except workflow.WorkflowError:
        current_phase = None

    return (
        name if isinstance(name, str) and name else None,
        role if isinstance(role, str) and role else None,
        current_phase,
    )


def _load_active_workflow_manifest() -> tuple[dict[str, Any] | None, Any]:
    """Best-effort load of the active workflow's manifest as a dict.

    Used to extract workflow-embedded `rules:`. Returns (manifest_dict,
    source_path) on success, (None, None) if no workflow is active or
    the manifest can't be loaded.
    """
    try:
        state = workflow.read_phase_state()
        workflow_id = state.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id:
            return None, None
        decl = workflow.load_workflow(workflow_id)
    except workflow.WorkflowError:
        return None, None
    # We re-read the manifest as a raw dict so we can pull the
    # `rules:` block; `WorkflowDecl` only carries phases.
    try:
        import yaml
        data = yaml.safe_load(decl.source_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    return data, decl.source_path


def _allow_response() -> dict[str, Any]:
    """Hook-decision JSON for an allow (no permission gate)."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


def _deny_response(message: str, rule_id: str | None) -> dict[str, Any]:
    """Hook-decision JSON for a deny."""
    reason = message or (f"Blocked by rule {rule_id!r}" if rule_id else "Blocked.")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _ask_response(message: str, rule_id: str | None) -> dict[str, Any]:
    """Hook-decision JSON for a warn (Claude Code's `ask` is the closest
    primitive)."""
    reason = (
        message
        or (f"Warning: rule {rule_id!r} fired" if rule_id else "Warning.")
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `evaluate`."""
    args = arguments or {}
    tool_name = args.get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name:
        # Unparseable hook payload -> fail open (allow). The hook
        # itself is best-effort; a malformed payload is an upstream
        # bug, not a Rule decision.
        _log.warning("evaluate: missing tool_name in hook payload")
        return [
            mcp_types.TextContent(type="text", text=json.dumps(_allow_response()))
        ]

    tool_input_raw = args.get("tool_input")
    if isinstance(tool_input_raw, dict):
        tool_input = tool_input_raw
    elif isinstance(tool_input_raw, str):
        # Some hook payload encodings stringify nested objects;
        # tolerate JSON-string form.
        try:
            parsed = json.loads(tool_input_raw)
            tool_input = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            tool_input = {}
    else:
        tool_input = {}

    agent_name, role, current_phase = _resolve_caller_identity()

    # Load merged rules. Workflow-embedded rules are layered last
    # (additive; independent id namespace from the 3-tier rules.yaml).
    workflow_manifest, workflow_source = _load_active_workflow_manifest()
    try:
        rule_list = rules.load_merged_rules(
            workflow_manifest=workflow_manifest,
            workflow_source=workflow_source,
        )
    except rules.RulesError as exc:
        # Fail-closed would block all tool calls if rules.yaml is
        # malformed at any tier. Phase 6 leans fail-open + warning
        # log; future hardening may flip this.
        _log.warning("evaluate: failed to load rules, allowing: %s", exc)
        return [
            mcp_types.TextContent(type="text", text=json.dumps(_allow_response()))
        ]

    decision = rules._evaluate(
        tool_name,
        tool_input,
        role=role,
        current_phase=current_phase,
        rules=rule_list,
    )

    # Log + reshape per SPEC §7 evaluate row.
    if decision.action == "log":
        # `log` rules append an audit event but the tool call still
        # proceeds. _evaluate already mapped enforcement="log" to
        # action="log"; we rewrite to "allow" after logging.
        rules.append_log_event(
            rules.build_log_event(
                event_type="rule_fired_log",
                outcome="allowed",
                rule_id=decision.rule_id,
                agent=agent_name,
                role=role,
                current_phase=current_phase,
                tool_name=tool_name,
                tool_input=tool_input,
                enforcement="log",
                message=decision.message,
            )
        )
        return [
            mcp_types.TextContent(type="text", text=json.dumps(_allow_response()))
        ]

    if decision.action == "deny":
        # Phase 6: a deny fire is a hard block. Phase 7 will look up
        # `overrides/<command_hash>.json` first and bypass on grant.
        # For Phase 6 we also log the deny (audit trail) -- the SPEC
        # §17 `guardrail_log.jsonl` writers table assigns deny-on-
        # block events as part of the override/ack lifecycle in
        # Phase 7; for Phase 6 we use the generic `rule_fired_log`
        # type with outcome=blocked so the row carries the same
        # diagnostic fields.
        rules.append_log_event(
            rules.build_log_event(
                event_type="rule_fired_log",
                outcome="blocked",
                rule_id=decision.rule_id,
                agent=agent_name,
                role=role,
                current_phase=current_phase,
                tool_name=tool_name,
                tool_input=tool_input,
                enforcement="deny",
                message=decision.message,
            )
        )
        return [
            mcp_types.TextContent(
                type="text",
                text=json.dumps(_deny_response(decision.message, decision.rule_id)),
            )
        ]

    if decision.action == "warn":
        rules.append_log_event(
            rules.build_log_event(
                event_type="rule_fired_log",
                outcome="blocked",  # warns block until ack consumed (Phase 7)
                rule_id=decision.rule_id,
                agent=agent_name,
                role=role,
                current_phase=current_phase,
                tool_name=tool_name,
                tool_input=tool_input,
                enforcement="warn",
                message=decision.message,
            )
        )
        return [
            mcp_types.TextContent(
                type="text",
                text=json.dumps(_ask_response(decision.message, decision.rule_id)),
            )
        ]

    # action == "allow" (no rule matched).
    return [
        mcp_types.TextContent(type="text", text=json.dumps(_allow_response()))
    ]
