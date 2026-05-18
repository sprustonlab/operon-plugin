#!/usr/bin/env python3
"""PreToolUse hook entrypoint (Phase 6 fix).

Replaces the prior `type: mcp_tool` hook wiring per SPEC §8 + §12.
The mcp_tool form had a chicken-and-egg: every tool call (including
calls TO operon's own MCP tools) fires PreToolUse, which routes via
MCP, which causes recursion / "MCP server not connected" errors
during the connection race. Hookify's reference plugin (Anthropic's
own example) uses `type: command` for exactly this reason.

This script reads the hook input JSON from stdin, evaluates the
merged Rule list against the calling Agent's (role, current_phase)
projection using the SAME `operon_mcp_server.rules` module that the
MCP `evaluate` tool uses, writes a guardrail_log.jsonl audit row,
and emits hook-decision JSON to stdout. Exit code 0 regardless.

Hook input shape (per Claude Code hooks-reference):
    {
      "session_id": "...",
      "hook_event_name": "PreToolUse",
      "tool_name": "Bash",
      "tool_input": {"command": "..."}
    }

Output shape (per Claude Code hooks-reference):
    {
      "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" | "deny" | "ask",
        "permissionDecisionReason": "<message>"
      }
    }

Identity:
- `OPERON_AGENT_HANDLE` env -> `_handles/<handle>.json` for
  agent_name + role
- `phase_state.json` for current_phase
Both are leaf-tier reads; no MCP calls.

Cross-platform per SPEC §2: pathlib, encoding="utf-8", no
platform-gated APIs.

PYTHONPATH expectation: the companion `pretooluse-wrapper`
(bash/cmd) prepends `${CLAUDE_PLUGIN_ROOT}/src` so this script can
`import operon_mcp_server.rules` without `pip install -e .`. The
wrapper also resolves a python with the runtime deps (mcp,
watchdog, yaml) -- only `yaml` is actually needed by this hook
path, but the dep set is the same as the MCP server's so a single
ladder covers both.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

# `operon_mcp_server.identity` reads OPERON_AGENT_HANDLE + the
# handle file; no MCP imports.
from operon_mcp_server import identity, paths, rules, workflow

#: Optional verbose-logging env var, mirrors the MCP server's
#: OPERON_DEBUG. When set to a truthy value, hook diagnostics land
#: on stderr (which Claude Code captures into the transcript).
_DEBUG_ENV = "OPERON_DEBUG"


def _maybe_enable_debug() -> None:
    flag = os.environ.get(_DEBUG_ENV, "").strip().lower()
    if flag in {"", "0", "false", "no"}:
        return
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG,
        format="[pretooluse] %(levelname)s: %(message)s",
    )


_log = logging.getLogger(__name__)


def _allow_output() -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


def _deny_output(message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message or "Blocked by operon-plugin rule.",
        }
    }


def _ask_output(message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": message or "Warned by operon-plugin rule.",
        }
    }


def _resolve_identity() -> tuple[str | None, str | None, str | None]:
    """Return (agent_name, role, current_phase). All-None fallbacks
    are safe: role/phase-scoped rules simply don't match when None.
    """
    handle = identity.read_env_handle()
    if handle is None:
        return None, None, None
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        _log.warning("identity read failed: %s", exc)
        return None, None, None
    if record is None:
        return None, None, None
    name = record.get("agent_name")
    role = record.get("role")

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


def _load_active_workflow_manifest():
    """Return (manifest_dict, source_path) for the active run's
    workflow YAML, or (None, None) if no workflow is active.
    Used to layer workflow-embedded rules on top of 3-tier rules.yaml.
    """
    try:
        state = workflow.read_phase_state()
        wid = state.get("workflow_id")
        if not isinstance(wid, str) or not wid:
            return None, None
        decl = workflow.load_workflow(wid)
    except workflow.WorkflowError:
        return None, None
    try:
        import yaml as _yaml
        data = _yaml.safe_load(decl.source_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    return data, decl.source_path


def _emit(payload: dict[str, Any]) -> None:
    """Write the hook decision JSON to stdout and exit 0."""
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    raise SystemExit(0)


def main() -> None:
    _maybe_enable_debug()

    raw = sys.stdin.read()
    if not raw.strip():
        _log.debug("empty stdin; fail-open")
        _emit(_allow_output())
        return  # unreachable; SystemExit above

    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning("stdin not JSON (%s); fail-open", exc)
        _emit(_allow_output())
        return

    tool_name = hook_input.get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name:
        _log.debug("no tool_name in hook input; fail-open")
        _emit(_allow_output())
        return

    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        # Some hook payloads stringify nested objects.
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
                if not isinstance(tool_input, dict):
                    tool_input = {}
            except json.JSONDecodeError:
                tool_input = {}
        else:
            tool_input = {}

    agent_name, role, current_phase = _resolve_identity()
    _log.debug(
        "tool=%s role=%r phase=%r agent=%r",
        tool_name, role, current_phase, agent_name,
    )

    workflow_manifest, workflow_source = _load_active_workflow_manifest()
    try:
        rule_list = rules.load_merged_rules(
            workflow_manifest=workflow_manifest,
            workflow_source=workflow_source,
        )
    except rules.RulesError as exc:
        _log.warning("rules load failed (%s); fail-open", exc)
        _emit(_allow_output())
        return

    decision = rules._evaluate(
        tool_name,
        tool_input,
        role=role,
        current_phase=current_phase,
        rules=rule_list,
    )

    # Build + write audit log row (best-effort; absent run-dir is OK).
    def _log_row(outcome: str, enforcement: str) -> None:
        try:
            rules.append_log_event(
                rules.build_log_event(
                    event_type="rule_fired_log",
                    outcome=outcome,
                    rule_id=decision.rule_id,
                    agent=agent_name,
                    role=role,
                    current_phase=current_phase,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    enforcement=enforcement,
                    message=decision.message,
                )
            )
        except (paths.OperonPathError, OSError) as exc:
            _log.warning("audit-log append failed: %s", exc)

    if decision.action == "log":
        _log_row("allowed", "log")
        _emit(_allow_output())
        return

    if decision.action == "deny":
        _log_row("blocked", "deny")
        _emit(_deny_output(decision.message))
        return

    if decision.action == "warn":
        _log_row("blocked", "warn")
        _emit(_ask_output(decision.message))
        return

    # action == "allow" (no rule matched)
    _emit(_allow_output())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        # Last-ditch fail-open. Hook errors are non-blocking by Claude
        # Code's contract anyway; we'd rather not block legitimate
        # tool calls when our hook itself crashes. Emit a diagnostic
        # to stderr so the failure is visible.
        sys.stderr.write(f"[pretooluse] fatal: {exc!r}\n")
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }))
        sys.exit(0)
