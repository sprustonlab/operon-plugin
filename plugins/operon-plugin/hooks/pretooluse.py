#!/usr/bin/env python3
"""PreToolUse hook entrypoint (Phase 6 fix + Phase 6 followup).

Replaces the prior `type: mcp_tool` hook wiring per SPEC §8 + §12.
The mcp_tool form had a chicken-and-egg: every tool call (including
calls TO operon's own MCP tools) fires PreToolUse, which routes via
MCP, which causes recursion / "MCP server not connected" errors
during the connection race. Hookify's reference plugin (Anthropic's
own example) uses `type: command` for exactly this reason.

This script implements TWO evaluation passes per the Phase 6
followup brief:

  1. FAIL-CLOSED hardcoded deny set (defense-in-depth). Small,
     curated, catastrophic-class patterns evaluated with stdlib
     `re` ONLY. Fires even when rules.yaml is missing, identity
     is unresolvable, or PyYAML is unavailable. This is the
     unconditional safety gate.

  2. FULL rules engine. Loads the merged 3-tier rules.yaml +
     workflow-embedded rules.yaml block, projects through (role,
     current_phase), runs `operon_mcp_server.rules._evaluate`.
     Handles warn / log / role-scoped / phase-scoped rules.
     Fails-open on rules-load errors (the fail-CLOSED pass above
     has already cleared the catastrophic-class patterns).

Both passes share the same audit-row schema and write to
`guardrail_log.jsonl`. The hardcoded set's `rule_id` mirrors the
corresponding entry in `plugins/operon-plugin/rules.yaml` so a
single source-of-truth grep links them. If `rules.yaml` is hand-
edited to disable a rule that's also in the hardcoded set, the
hardcoded copy STILL FIRES -- this is the intended defense-in-
depth: a deliberate `disabled_rules` entry cannot turn off the
hardcoded safety net.

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
import re
import sys
from typing import Any

# `operon_mcp_server.identity` reads OPERON_AGENT_HANDLE + the
# handle file; no MCP imports.
from operon_mcp_server import identity, paths, rules, workflow


# ===========================================================================
# FAIL-CLOSED HARDCODED DENY SET
# ===========================================================================
# Catastrophic-class deny patterns that MUST fire even when:
#   - rules.yaml is missing / unreadable / malformed
#   - PyYAML import fails (shouldn't given our wrapper, but defense-in-depth)
#   - identity is unresolvable (no OPERON_AGENT_HANDLE, no handle file)
#   - phase_state.json is corrupt
#   - any other failure of the full rules engine
#
# These run BEFORE `rules.load_merged_rules` / `_evaluate`, evaluated with
# stdlib `re` only -- no operon-package imports beyond what's already at the
# top of this file. The full rules engine (which lives in rules.yaml and
# adds role/phase projection) handles everything else; this top set is the
# safety net.
#
# To keep both code paths in sync: each entry's `rule_id` MUST match the
# corresponding entry in `plugins/operon-plugin/rules.yaml`. The same
# pattern + message live in both places. Defense-in-depth: if the rules.yaml
# definition gets disabled via `disabled_rules`, this hardcoded copy still
# fires.
#
# Adding a new entry: this set should stay small (<5) and reserved for
# patterns whose violation is unrecoverable. Lower-stakes rules belong in
# rules.yaml ONLY, not duplicated here.
# ===========================================================================

_FAILCLOSED_DENY: list[dict[str, Any]] = [
    {
        "rule_id": "no_rm_rf",
        "tools": ("Bash",),
        "field": "command",
        # Mirror of `plugins/operon-plugin/rules.yaml` `no_rm_rf` detect.
        # If you change the regex here, grep rules.yaml for "no_rm_rf"
        # and update both.
        "pattern": re.compile(r"rm\s+-rf\s+/"),
        "message": (
            "Dangerous: rm -rf on absolute path. Request override if "
            "intentional. (operon-plugin fail-closed safety gate)"
        ),
    },
]

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


def _failclosed_deny_check(
    tool_name: str, tool_input: dict[str, Any]
) -> tuple[str, str] | None:
    """Run the hardcoded fail-closed deny patterns.

    Returns `(rule_id, message)` if a pattern matches, else None.
    Pure stdlib (`re` only); does not import rules.yaml or invoke
    the full rules engine. Safe to call even when the rest of the
    operon environment is broken.
    """
    for entry in _FAILCLOSED_DENY:
        if tool_name not in entry["tools"]:
            continue
        field = entry["field"]
        val = tool_input.get(field) if isinstance(tool_input, dict) else None
        if not isinstance(val, str):
            continue
        if entry["pattern"].search(val):
            return entry["rule_id"], entry["message"]
    return None


def _try_log_failclosed(
    rule_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    agent_name: str | None,
    role: str | None,
    current_phase: str | None,
    message: str,
) -> None:
    """Best-effort audit-row write for a fail-closed deny. Suppresses
    all errors so a broken audit-log path doesn't block the deny
    itself. The deny still fires regardless."""
    try:
        rules.append_log_event(
            rules.build_log_event(
                event_type="rule_fired_log",
                outcome="blocked",
                rule_id=rule_id,
                agent=agent_name,
                role=role,
                current_phase=current_phase,
                tool_name=tool_name,
                tool_input=tool_input,
                enforcement="deny",
                message=f"[failclosed] {message}",
            )
        )
    except Exception as exc:
        _log.warning("failclosed audit append skipped: %s", exc)


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

    # ---- FAIL-CLOSED PASS ------------------------------------------
    # Hardcoded safety patterns evaluated BEFORE the full rules engine.
    # These fire even when rules.yaml is missing / identity is
    # unresolvable / phase_state is corrupt -- the defense-in-depth
    # gate for catastrophic-class actions. Stdlib `re` only.
    failclosed = _failclosed_deny_check(tool_name, tool_input)

    # Resolve identity / phase NEXT, but only for audit-log enrichment
    # of the failclosed deny (if it fires) and for the full rules
    # engine path below. Identity resolution failure does NOT bypass
    # the deny.
    agent_name, role, current_phase = _resolve_identity()
    _log.debug(
        "tool=%s role=%r phase=%r agent=%r failclosed=%s",
        tool_name, role, current_phase, agent_name,
        bool(failclosed),
    )

    if failclosed is not None:
        rule_id, message = failclosed
        _try_log_failclosed(
            rule_id=rule_id,
            tool_name=tool_name,
            tool_input=tool_input,
            agent_name=agent_name,
            role=role,
            current_phase=current_phase,
            message=message,
        )
        _emit(_deny_output(message))
        return  # unreachable; SystemExit above

    # ---- FULL RULES ENGINE -----------------------------------------
    # The 3-tier rules.yaml + workflow-embedded path, with (role,
    # phase) projection. Fails-open on parse / load errors -- the
    # fail-CLOSED safety net above has already cleared the
    # catastrophic-class patterns by this point.
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
