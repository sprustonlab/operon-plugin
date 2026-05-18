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
from typing import Any

import mcp.types as mcp_types

from .. import identity, workflow

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


def _resolve_caller() -> tuple[str, str]:
    """Return (agent_name, role) from env-anchored handle.

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
    return name, role


def _render_constraints_markdown(
    *, workflow_id: str, current_phase: str, role: str,
    advance_checks: list[dict[str, Any]],
) -> str:
    """Render the advance-checks list as a `## Constraints` markdown block."""
    lines: list[str] = [
        f"## Constraints (workflow={workflow_id}, phase={current_phase}, role={role})",
        "",
    ]
    if not advance_checks:
        lines.append("(no advance checks declared for this phase)")
        return "\n".join(lines)
    lines.append("Advance checks for this phase (AND semantics):")
    lines.append("")
    for i, ck in enumerate(advance_checks, 1):
        t = ck.get("check_type", "<unknown>")
        params_desc = ck.get("params_description", "")
        if params_desc:
            lines.append(f"{i}. `{t}` -- {params_desc}")
        else:
            lines.append(f"{i}. `{t}`")
    return "\n".join(lines)


def _do_get(args: dict[str, Any]) -> dict[str, Any]:
    requested_name = args.get("agent_name")

    name, role = _resolve_caller()

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

    markdown = _render_constraints_markdown(
        workflow_id=workflow_id,
        current_phase=current_phase,
        role=role,
        advance_checks=advance_checks_payload,
    )

    return {
        "caller": name,
        "role": role,
        "workflow_id": workflow_id,
        "current_phase": current_phase,
        "advance_checks": advance_checks_payload,
        "markdown": markdown,
    }


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
