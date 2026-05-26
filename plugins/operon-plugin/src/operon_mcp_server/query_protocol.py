"""Inbox-channel identity protocol (Land 7).

A teammate that needs operon-introspected information about
itself -- identity, current phase, applicable rules -- cannot
trust the MCP-side tools (``whoami``, ``get_agent_info``,
``get_applicable_rules``) because operon's MCP runs as a
singleton in the lead's claude process; every teammate's MCP
call dereferences the lead's bootstrap identity. The B.0 probe
(Land 6 era) confirmed Anthropic's runtime does NOT propagate
teammate-identifying metadata via MCP ``_meta``/``clientInfo``.

Land 7 routes identity-aware queries through the inbox channel:

  1. Teammate calls ``SendMessage(to="operon",
     text="[OPERON_QUERY] <command>")``. The Anthropic runtime
     stamps the inbox entry's ``from`` field server-side and the
     teammate cannot spoof it -- that is the trust anchor.
  2. ``inbox_reader.py`` polls
     ``~/.claude/teams/<team>/inboxes/operon.json`` and hands new
     entries to :func:`dispatch_query` here.
  3. We parse the command from the entry text, resolve the
     caller via :func:`identity.resolve_caller_identity` (using
     the runtime-stamped ``from``, NOT anything in ``text``),
     invoke the matching underlying logic, and write the reply
     back via :func:`inbox.write_to_member_inbox` to the
     caller's inbox.
  4. The teammate sees the reply on its next turn boundary.

Recognized v1 commands: ``whoami``, ``get_agent_info``,
``get_applicable_rules``. Unknown commands surface a structured
``error`` reply rather than a silent drop, so a misspelled query
is observable in the teammate's inbox.

Latency: each round-trip costs an extra LLM turn on each side
(~5-15 s end-to-end vs ~0.5-1 s for MCP). Boaz accepted that
cost in exchange for the impersonation defense the
runtime-stamped ``from`` provides.

Cross-platform per project rules: ``pathlib.Path``,
``encoding="utf-8"``, ASCII-only.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from . import identity, inbox, paths, workflow

_log = logging.getLogger(__name__)

#: Prefix every operon query entry must carry. Lines that do not
#: start with this prefix are not protocol messages and are
#: silently ignored by the reader (operon's inbox is also the
#: destination for any free-form ``SendMessage(to="operon", ...)``
#: that does not encode a query -- those land in the audit log
#: but produce no reply).
QUERY_PREFIX = "[OPERON_QUERY]"

#: Prefix for operon's reply entries. The teammate's LLM can
#: pattern-match on the prefix to correlate request + response.
REPLY_PREFIX = "[OPERON_REPLY]"

#: v1 command set. Adding a command means adding both an entry
#: here (handler) and a brief mention in the WA1 directive in
#: ``plugins/operon-plugin/hooks/pretooluse.py`` so the teammate's
#: LLM knows the command exists.
_COMMAND_HANDLERS: dict[str, str] = {
    "whoami": "_handle_whoami",
    "get_agent_info": "_handle_get_agent_info",
    "get_applicable_rules": "_handle_get_applicable_rules",
}

#: Regex parsing the first line of a query entry. Captures the
#: command word; remainder of the line and any subsequent lines
#: are ignored (reserved for future args).
_QUERY_RE = re.compile(
    r"^\s*" + re.escape(QUERY_PREFIX) + r"\s+(?P<command>[A-Za-z_][A-Za-z0-9_]*)\b"
)


# -- handlers ----------------------------------------------------------


def _handle_whoami(from_name: str) -> dict[str, Any]:
    """Resolve the teammate's identity from the runtime-stamped
    ``from`` field via :func:`identity.resolve_caller_identity`."""
    return identity.resolve_caller_identity(from_name)


def _handle_get_agent_info(from_name: str) -> dict[str, Any]:
    """Compose identity + current phase + applicable rules for
    ``from_name``. Mirrors the shape ``get_agent_info`` returned in
    pre-Land-7 code, but resolves the caller from the verified
    inbox ``from`` field instead of an MCP argument.
    """
    who = identity.resolve_caller_identity(from_name)
    try:
        state = workflow.read_phase_state()
        run_state = workflow.read_state() if hasattr(workflow, "read_state") else {}
        phase_payload: dict[str, Any] = {
            "workflow_id": state.get("workflow_id"),
            "current_phase": state.get("current_phase"),
            "phase_started_at": state.get("phase_started_at"),
            "advance_history": state.get("advance_history") or [],
            "artifact_dir": (
                run_state.get("artifact_dir") if isinstance(run_state, dict) else None
            ),
        }
    except workflow.WorkflowError as exc:
        phase_payload = {"error": str(exc)}
    rules_payload = _handle_get_applicable_rules(from_name)
    return {
        "whoami": who,
        "phase": phase_payload,
        "rules": rules_payload,
    }


def _handle_get_applicable_rules(from_name: str) -> dict[str, Any]:
    """Build the (role, phase)-scoped rule projection for the
    teammate resolved from ``from_name``. Returns a structured
    payload with ``caller``, ``role``, ``workflow_id``,
    ``current_phase``, and the list of rules whose role/phase
    filters do not skip this caller.

    Does NOT consult the lead-keyed escape-token state (acks /
    overrides) -- those are lead-side singleton-MCP state and
    out of scope for a teammate query.
    """
    who = identity.resolve_caller_identity(from_name)
    role = who.get("role")
    if not isinstance(role, str) or not role:
        return {
            "error": "identity_unresolved",
            "reason": (
                f"resolve_caller_identity({from_name!r}) did not "
                "produce a bound role; cannot project applicable rules."
            ),
        }

    try:
        state = workflow.read_phase_state()
    except workflow.WorkflowError as exc:
        return {"error": "workflow_error", "reason": str(exc)}
    workflow_id = state.get("workflow_id")
    current_phase = state.get("current_phase")
    if not (isinstance(workflow_id, str) and workflow_id):
        return {
            "error": "workflow_error",
            "reason": "phase_state.json missing non-empty 'workflow_id'.",
        }
    if not (isinstance(current_phase, str) and current_phase):
        return {
            "error": "workflow_error",
            "reason": "phase_state.json missing non-empty 'current_phase'.",
        }

    try:
        decl = workflow.load_workflow(workflow_id)
    except workflow.WorkflowError as exc:
        return {"error": "workflow_error", "reason": str(exc)}

    # Project rules by (role, current_phase) using the same filter
    # functions the PreToolUse evaluator uses. Workflow-embedded
    # rules layer on top of 3-tier rules.yaml (plugin > user >
    # project).
    workflow_manifest_dict: dict[str, Any] | None = None
    try:
        import yaml as _yaml

        wf_data = _yaml.safe_load(decl.source_path.read_text(encoding="utf-8"))
        if isinstance(wf_data, dict):
            workflow_manifest_dict = wf_data
    except Exception:  # noqa: BLE001 -- best-effort load
        workflow_manifest_dict = None

    from . import rules as rules_mod

    try:
        all_rules = rules_mod.load_merged_rules(
            workflow_manifest=workflow_manifest_dict,
            workflow_source=decl.source_path,
        )
    except rules_mod.RulesError as exc:
        return {"error": "rules_load_error", "reason": str(exc)}

    applicable: list[dict[str, Any]] = []
    for r in all_rules:
        if rules_mod._role_filter_skips(r, role):
            continue
        if rules_mod._phase_filter_skips(r, current_phase):
            continue
        applicable.append(
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

    return {
        "caller": who.get("name"),
        "role": role,
        "workflow_id": workflow_id,
        "current_phase": current_phase,
        "applicable_rules": applicable,
    }


# -- dispatch ----------------------------------------------------------


def parse_query(text: str) -> str | None:
    """Return the command word from a query entry text, or ``None``
    if the text is not a recognized protocol message. Tolerant of
    leading whitespace and multi-line bodies (only the first line
    is consumed).
    """
    if not isinstance(text, str):
        return None
    first_line = text.splitlines()[0] if text else ""
    match = _QUERY_RE.match(first_line)
    if match is None:
        return None
    return match.group("command")


def dispatch_query(
    team_name: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Handle one inbound inbox entry. Returns a small manifest
    describing what was done so the reader can log it.

    Behaviour by case:

      * ``entry.text`` does not match the query regex: no reply
        sent; returns ``{"dispatched": False, "reason": "not_a_query"}``.
      * Command unknown: writes an ``[OPERON_REPLY] <cmd> {"error":
        "unknown_command", ...}`` to the sender's inbox; returns
        the manifest.
      * Command recognized: invokes the matching handler with the
        ``from`` field as the verified caller name, builds the reply
        body, writes to ``inbox.write_to_member_inbox(team_name,
        <from>, entry)`` with prefix ``[OPERON_REPLY] <cmd>``.

    Defensive: catches handler exceptions and turns them into
    ``{"error": "handler_exception", "exception": "..."}`` replies
    so a buggy handler can't take the reader down.
    """
    from_name = entry.get("from")
    text = entry.get("text", "")
    if not isinstance(from_name, str) or not from_name:
        return {"dispatched": False, "reason": "missing_from_field"}

    command = parse_query(text)
    if command is None:
        return {"dispatched": False, "reason": "not_a_query", "from": from_name}

    handler_name = _COMMAND_HANDLERS.get(command)
    if handler_name is None:
        reply_payload: dict[str, Any] = {
            "error": "unknown_command",
            "command": command,
            "supported_commands": sorted(_COMMAND_HANDLERS.keys()),
        }
    else:
        handler = globals()[handler_name]
        try:
            reply_payload = handler(from_name)
        except Exception as exc:  # noqa: BLE001 -- never let handler crash reader
            _log.exception(
                "query_protocol: handler %s raised on from=%r text=%r",
                handler_name,
                from_name,
                text,
            )
            reply_payload = {
                "error": "handler_exception",
                "command": command,
                "exception": repr(exc),
            }

    reply_text = f"{REPLY_PREFIX} {command} " + json.dumps(reply_payload, default=str)
    reply_entry = inbox.build_operon_entry(team_name=team_name, text=reply_text)
    try:
        result = inbox.write_to_member_inbox(
            team_name=team_name,
            recipient_name=from_name,
            entry=reply_entry,
        )
    except inbox.InboxWriteError as exc:
        _log.warning(
            "query_protocol: failed to write reply to %s's inbox: %s",
            from_name,
            exc,
        )
        return {
            "dispatched": False,
            "reason": "reply_write_failed",
            "command": command,
            "from": from_name,
            "error": str(exc),
        }

    return {
        "dispatched": True,
        "command": command,
        "from": from_name,
        "reply_inbox_path": result["inbox_path"],
        "reply_entries_after_write": result["entries_after_write"],
        "reply_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }


# -- compatibility helper ----------------------------------------------


def supported_commands() -> list[str]:
    """Return the v1 command set for documentation / introspection."""
    return sorted(_COMMAND_HANDLERS.keys())


# Silence unused-import warning for paths (kept available for
# future handlers that need to resolve operon-side paths).
_ = paths
