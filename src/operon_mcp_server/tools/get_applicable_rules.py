"""`get_applicable_rules` MCP tool. All-visible per SPEC §7.1.

Phase 5 scope: returns the `advance_checks` list for the current
(role, phase) projection from the workflow manifest, rendered as a
human-readable markdown block. This is the SAME `## Constraints`
projection that `spawn_agent` will eventually inject into spawned
agents (Phase 6); for Phase 5 it serves as the "what gates this
phase?" introspection tool.

Actual Rule enforcement (PreToolUse hook + four-valued state machine)
lands in Phase 6. This Phase 5 tool deliberately exposes only the
declarative projection.

Cross-Agent gate (SPEC §7 row): `agent_name` is permitted only when
the caller is Coordinator OR the named target was spawned by the
caller (chain-of-trust via `_handles/<handle>.json` `spawned_by`).
For Phase 5 we keep the surface simple -- only the caller's own role
is supported; the cross-Agent gate is documented but not yet wired
(it requires the role-scoped Rule projection that Phase 6 builds).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import mcp.types as mcp_types

from .. import identity, rules, workflow

#: MCP tool name. Visible to All per SPEC §7.1.
TOOL_NAME = "get_applicable_rules"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent_name": {
            "type": "string",
            "description": (
                "Optional. Name of another Agent to inspect "
                "(Coordinator only / chain-of-trust per SPEC §7). For "
                "Phase 5 only the caller's own role is supported; this "
                "parameter is accepted but returns a structured error "
                "if it names another Agent."
            ),
        },
        "include_skipped": {
            "type": "boolean",
            "description": (
                "Reserved for Phase 6 (full audit view including "
                "scope/disabled/shadowed Rules). Phase 5 ignores this."
            ),
            "default": False,
        },
    },
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (All-visible)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Return the applicable constraints (advance checks for the "
            "current phase) for the caller's (role, phase) projection, "
            "rendered as markdown. All roles; cross-Agent inspection "
            "lands in Phase 6."
        ),
        inputSchema=INPUT_SCHEMA,
    )


def _resolve_caller() -> tuple[str, str, str]:
    """Return (agent_name, role, handle) from env-anchored handle.

    Raises `ValueError` if identity is not bound.
    """
    handle = identity.read_env_handle()
    if handle is None:
        raise ValueError(
            f"Environment variable '{identity.ENV_HANDLE_VAR}' is not set; "
            "get_applicable_rules requires an env-anchored identity."
        )
    record = identity.read_handle_file(handle)
    if record is None:
        raise ValueError(
            f"No handle record at _handles/{handle}.json; cannot resolve role."
        )
    name = record.get("agent_name")
    role = record.get("role")
    if not isinstance(name, str) or not name:
        raise ValueError("Handle record missing 'agent_name'.")
    if not isinstance(role, str) or not role:
        raise ValueError("Handle record missing 'role'.")
    return name, role, handle


def _render_constraints_markdown(
    *,
    workflow_id: str,
    current_phase: str,
    role: str,
    advance_checks: list[dict[str, Any]],
    applicable_rules: list[dict[str, Any]] | None = None,
    active_tokens: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """Render the advance-checks + applicable Rules as a `## Constraints` block."""
    lines: list[str] = [
        f"## Constraints (workflow={workflow_id}, phase={current_phase}, role={role})",
        "",
    ]
    # Advance checks
    if advance_checks:
        lines.append("### Advance checks for this phase (AND semantics)")
        lines.append("")
        for i, ck in enumerate(advance_checks, 1):
            t = ck.get("check_type", "<unknown>")
            params_desc = ck.get("params_description", "")
            if params_desc:
                lines.append(f"{i}. `{t}` -- {params_desc}")
            else:
                lines.append(f"{i}. `{t}`")
        lines.append("")
    else:
        lines.append("(no advance checks declared for this phase)")
        lines.append("")
    # Applicable Rules (Phase 6)
    if applicable_rules:
        lines.append("### Active guardrail Rules for (role, phase)")
        lines.append("")
        for r in applicable_rules:
            rid = r.get("id", "<unknown>")
            tier = r.get("tier", "?")
            enf = r.get("enforcement", "?")
            pat = r.get("detect_pattern") or "*any*"
            triggers = "/".join(r.get("trigger") or ["?"])
            msg = (r.get("message") or "").strip()
            lines.append(f"- `{rid}` [{tier}/{enf}] on `{triggers}` (detect: `{pat}`)")
            if msg:
                lines.append(f"  -- {msg}")
        lines.append("")
    elif applicable_rules is not None:
        lines.append("(no guardrail Rules apply to this (role, phase))")
        lines.append("")
    # Phase 9: active escape tokens held by the caller
    if active_tokens is not None:
        acks = active_tokens.get("acks") or []
        overrides = active_tokens.get("overrides") or []
        lines.append("### Active escape tokens")
        lines.append("")
        if not acks and not overrides:
            lines.append("No active acks or overrides.")
        else:
            if acks:
                lines.append("**Acks (TTL):**")
                for t in acks:
                    rid = t.get("rule_id", "<unknown>")
                    rem = t.get("seconds_remaining")
                    rem_s = f"{rem}s remaining" if rem is not None else "no TTL"
                    reason = (t.get("reason") or "").strip()
                    suffix = f" -- {reason}" if reason else ""
                    lines.append(f"- `{rid}` ({rem_s}){suffix}")
                lines.append("")
            if overrides:
                lines.append("**Overrides (one-shot):**")
                for t in overrides:
                    rid = t.get("rule_id", "<unknown>")
                    one = " one-shot" if t.get("one_shot") else ""
                    reason = (t.get("reason") or "").strip()
                    suffix = f" -- {reason}" if reason else ""
                    lines.append(f"- `{rid}`{one}{suffix}")
                lines.append("")
    return "\n".join(lines).rstrip()


def _do_get(args: dict[str, Any]) -> dict[str, Any]:
    requested_name = args.get("agent_name")

    name, role, handle = _resolve_caller()

    if requested_name is not None and requested_name != name:
        return {
            "error": "cross_agent_not_implemented",
            "reason": (
                "Phase 5 supports only the caller's own role. "
                "Cross-Agent inspection (Coordinator / chain-of-trust) "
                "lands in Phase 6."
            ),
            "requested": requested_name,
            "caller": name,
        }

    state = workflow.read_phase_state()
    workflow_id = state.get("workflow_id")
    current_phase = state.get("current_phase")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise workflow.WorkflowError(
            "phase_state.json missing non-empty 'workflow_id'."
        )
    if not isinstance(current_phase, str) or not current_phase:
        raise workflow.WorkflowError(
            "phase_state.json missing non-empty 'current_phase'."
        )

    decl = workflow.load_workflow(workflow_id)
    phase = decl.phase(current_phase)
    if phase is None:
        raise workflow.WorkflowError(
            f"Current phase {current_phase!r} not in workflow "
            f"{workflow_id!r} manifest at {decl.source_path}."
        )

    advance_checks_payload: list[dict[str, Any]] = []
    for ck in phase.advance_checks:
        # Surface a short human-readable description of params per
        # check type to help the LLM reason about what it needs to
        # produce before advance.
        desc = ""
        if ck.type == "file-exists-check":
            paths_listed = ck.params.get("paths") or [ck.params.get("path")]
            desc = f"path(s): {paths_listed}"
        elif ck.type == "file-content-check":
            desc = (
                f"pattern={ck.params.get('pattern')!r} in "
                f"path(s)={ck.params.get('paths') or [ck.params.get('path')]}"
            )
        elif ck.type == "command-output-check":
            desc = (
                f"command={ck.params.get('command')!r} matches "
                f"pattern={ck.params.get('pattern')!r}"
            )
        elif ck.type == "manual-confirm":
            desc = f"prompt={ck.params.get('prompt') or ck.params.get('question')!r}"
        elif ck.type == "artifact-dir-ready-check":
            desc = "set_artifact_dir(...) has been called"
        advance_checks_payload.append(
            {
                "check_type": ck.type,
                "params": ck.params,
                "params_description": desc,
                "on_failure": ck.on_failure,
            }
        )

    # Phase 6: project the merged guardrail Rule list against the
    # caller's (role, phase). Workflow-embedded rules layer on top of
    # the 3-tier rules.yaml (plugin > user > project).
    workflow_manifest_dict: dict[str, Any] | None = None
    try:
        import yaml as _yaml

        wf_data = _yaml.safe_load(decl.source_path.read_text(encoding="utf-8"))
        if isinstance(wf_data, dict):
            workflow_manifest_dict = wf_data
    except Exception:
        workflow_manifest_dict = None

    try:
        all_rules = rules.load_merged_rules(
            workflow_manifest=workflow_manifest_dict,
            workflow_source=decl.source_path,
        )
    except rules.RulesError as exc:
        all_rules = []
        rules_error = str(exc)
    else:
        rules_error = ""

    applicable_rules: list[dict[str, Any]] = []
    for r in all_rules:
        # Apply the same role+phase filters that `_evaluate` uses, so
        # the LLM sees exactly the rules that could fire for it.
        if rules._role_filter_skips(r, role):
            continue
        if rules._phase_filter_skips(r, current_phase):
            continue
        applicable_rules.append(
            {
                "id": r.id,
                "tier": r.tier,
                "trigger": list(r.trigger),
                "enforcement": r.enforcement,
                "detect_pattern": (
                    r.detect_pattern.pattern if r.detect_pattern else None
                ),
                "detect_field": r.detect_field,
                "exclude_pattern": (
                    r.exclude_pattern.pattern if r.exclude_pattern else None
                ),
                "message": r.message,
                "roles": list(r.roles),
                "phases": list(r.phases),
            }
        )

    # Phase 9: active escape tokens (acks + overrides) held by the caller.
    # Lazy-GCs expired ack tokens during the scan (same policy as the hook).
    try:
        ack_tokens, override_tokens = rules.list_active_tokens(
            agent_handle=handle,
        )
    except rules.RulesError:
        ack_tokens, override_tokens = [], []

    now = datetime.now(timezone.utc)

    def _seconds_remaining(expires_at: str | None) -> int | None:
        if not expires_at:
            return None
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        delta = (exp - now).total_seconds()
        return max(0, int(delta))

    acks_payload = [
        {
            "rule_id": t.rule_id,
            "issued_at": t.issued_at,
            "expires_at": t.expires_at,
            "seconds_remaining": _seconds_remaining(t.expires_at),
            "one_shot": t.one_shot,
            "reason": t.reason,
        }
        for t in ack_tokens
    ]
    overrides_payload = [
        {
            "rule_id": t.rule_id,
            "issued_at": t.issued_at,
            "expires_at": t.expires_at,
            "seconds_remaining": _seconds_remaining(t.expires_at),
            "one_shot": t.one_shot,
            "reason": t.reason,
        }
        for t in override_tokens
    ]
    active_tokens_payload: dict[str, list[dict[str, Any]]] = {
        "acks": acks_payload,
        "overrides": overrides_payload,
    }

    markdown = _render_constraints_markdown(
        workflow_id=workflow_id,
        current_phase=current_phase,
        role=role,
        advance_checks=advance_checks_payload,
        applicable_rules=applicable_rules,
        active_tokens=active_tokens_payload,
    )

    payload: dict[str, Any] = {
        "caller": name,
        "role": role,
        "workflow_id": workflow_id,
        "current_phase": current_phase,
        "advance_checks": advance_checks_payload,
        "applicable_rules": applicable_rules,
        "active_tokens": active_tokens_payload,
        "markdown": markdown,
    }
    if rules_error:
        payload["rules_load_error"] = rules_error
    return payload


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `get_applicable_rules`."""
    args = arguments or {}
    try:
        result = _do_get(args)
    except ValueError as exc:
        result = {"error": "identity_unbound", "reason": str(exc)}
    except workflow.WorkflowError as exc:
        result = {"error": "workflow_error", "reason": str(exc)}
    except identity.IdentityError as exc:
        result = {"error": "identity_error", "reason": str(exc)}
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
