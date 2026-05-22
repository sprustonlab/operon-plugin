"""Env-anchored identity resolution for operon-plugin MCP subprocesses.

Per SPEC.md sections 6.5 and 16, every MCP subprocess identifies itself
via the `OPERON_AGENT_HANDLE` environment variable set by the
Coordinator at spawn time. This module owns the read paths from the env
to the canonical `(name, role, workflow_id, session_id)` tuple stored
in `<run-dir>/_handles/<handle>.json`. The current phase is sourced
from `<run-dir>/phase_state.json` (single source of truth per
SPEC.md section 11).

The LLM is opaque to its own subprocess environment and cannot forge
the handle, so the env var is the authoritative identity source --
LLM-supplied claims are ignored everywhere downstream.

Errors are surfaced via the `IdentityError` exception so callers
(typically MCP tool implementations) can convert them to MCP tool
errors at the protocol boundary without try-touching every step.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from . import paths

_log = logging.getLogger(__name__)

#: Name of the env var that anchors per-subprocess identity (SPEC 6.5).
ENV_HANDLE_VAR = "OPERON_AGENT_HANDLE"

#: Process-local fallback for callers that have no env handle set.
#: Populated by `bootstrap.auto_bootstrap_if_needed` at MCP server
#: startup so the singleton MCP subprocess can identify itself as
#: the project's default Coordinator without the user having to
#: export the env var.
#:
#: Land 4 (v2.9 plan section 6, identity.py table row) removed the
#: Phase 14 fix 4/5 freeze + drift defenses: under singleton-MCP
#: operon there is exactly one MCP subprocess (the lead's),
#: identity cannot drift, and the defensive logging surfaces
#: served only the multi-process spawn_agent worker pattern that
#: also dies in Land 4. See commit message for the audit trail.
_CACHED_HANDLE: str | None = None


class IdentityError(RuntimeError):
    """Raised when identity cannot be resolved for the calling subprocess."""


def _set_cached_handle(handle: str | None) -> None:
    """Set the process-local handle cache.

    Called by `bootstrap.auto_bootstrap_if_needed`. Subsequent
    `read_env_handle()` calls fall through to this value when the env
    var is unset. Passing `None` clears the cache (used in tests).
    """
    global _CACHED_HANDLE
    _CACHED_HANDLE = handle


def get_cached_handle() -> str | None:
    """Return the current process-local handle cache value.

    Exposed for diagnostics + tests. Production code uses
    `read_env_handle()` which already consults the cache as a
    fallback.
    """
    return _CACHED_HANDLE


def read_env_handle() -> str | None:
    """Return the handle for this MCP subprocess.

    Resolution order:

      1. `OPERON_AGENT_HANDLE` env var (if set, non-empty).
      2. Process-local cache populated by
         `bootstrap.auto_bootstrap_if_needed` at MCP server startup.

    Returns None when neither has a non-empty handle. Empty string
    env values are treated as unset.
    """
    env_value = os.environ.get(ENV_HANDLE_VAR)
    if env_value:
        _log.debug(
            "read_env_handle: env=%r cache=%r -> env wins",
            env_value,
            _CACHED_HANDLE,
        )
        return env_value
    _log.debug(
        "read_env_handle: env=None cache=%r -> cache returned",
        _CACHED_HANDLE,
    )
    return _CACHED_HANDLE


def read_handle_file(handle: str, start: Path | None = None) -> dict[str, Any] | None:
    """Read `_handles/<handle>.json` for the active run.

    Returns the parsed JSON dict if the file exists, or None if it
    does not. Raises `IdentityError` if the file exists but cannot be
    parsed as a JSON object, or if path resolution fails.

    Per SPEC.md section 17 the schema is::

        {handle, agent_name, role, workflow_id, spawned_at,
         spawned_by, session_id}
    """
    try:
        file_path = paths.handle_file(handle, start)
    except paths.OperonPathError as exc:
        raise IdentityError(str(exc)) from exc
    if not file_path.is_file():
        return None
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentityError(f"Failed to read handle file '{file_path}': {exc}") from exc
    if not isinstance(data, dict):
        raise IdentityError(
            f"Handle file '{file_path}' must contain a JSON object, got {type(data).__name__}."
        )
    return data


def _read_current_phase(start: Path | None = None) -> str | None:
    """Read `current_phase` from `<run-dir>/phase_state.json`.

    Returns the phase string, or None if the file is missing or has no
    `current_phase` field. Raises `IdentityError` on parse failure (a
    corrupt phase_state.json is a hard error, not a soft one).
    """
    try:
        file_path = paths.phase_state_file(start)
    except paths.OperonPathError as exc:
        raise IdentityError(str(exc)) from exc
    if not file_path.is_file():
        return None
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentityError(f"Failed to read phase state '{file_path}': {exc}") from exc
    if not isinstance(data, dict):
        raise IdentityError(
            f"Phase state file '{file_path}' must contain a JSON object."
        )
    phase = data.get("current_phase")
    if phase is None:
        return None
    if not isinstance(phase, str):
        raise IdentityError(
            f"'current_phase' in '{file_path}' must be a string, got {type(phase).__name__}."
        )
    return phase


def whoami(start: Path | None = None) -> dict[str, Any]:
    """Compose the canonical identity tuple for the calling subprocess.

    Per SPEC.md section 7 (`whoami` row), returns::

        {name, role, workflow_id, current_phase, cwd, session_id}

    where `name`, `role`, `workflow_id`, and `session_id` come from
    `_handles/<handle>.json`; `current_phase` from
    `<run-dir>/phase_state.json`; `cwd` from `os.getcwd()`.

    Raises `IdentityError` if:

    - the env handle is not set,
    - the handle file does not exist,
    - the handle file is missing required fields.
    """
    handle = read_env_handle()
    if handle is None:
        raise IdentityError(
            f"Environment variable '{ENV_HANDLE_VAR}' is not set; this MCP "
            "subprocess has no bound identity. Was it spawned via spawn_agent?"
        )

    record = read_handle_file(handle, start)
    if record is None:
        raise IdentityError(
            f"No handle record found for '{handle}'. The Coordinator must "
            "write _handles/<handle>.json before spawning this subprocess."
        )

    # SPEC section 17 schema. `agent_name` is the canonical field name;
    # we expose it to the LLM as `name` per the section 7 contract.
    try:
        name = record["agent_name"]
        role = record["role"]
        workflow_id = record["workflow_id"]
        session_id = record.get("session_id")
    except KeyError as exc:
        raise IdentityError(
            f"Handle record for '{handle}' is missing required field: {exc.args[0]}"
        ) from exc

    return {
        "name": name,
        "role": role,
        "workflow_id": workflow_id,
        "current_phase": _read_current_phase(start),
        "cwd": os.getcwd(),
        "session_id": session_id,
    }


# -- Land 6: caller-identity resolution for team-aware tools ------------


#: Member names that are NOT teammate-via-Agent and therefore should
#: not be resolvable via :func:`resolve_caller_identity`.  ``"operon"``
#: is the non-teammate MCP member; ``"team-lead"`` is the lead's own
#: roster slot (the lead's identity is the bootstrap identity, not a
#: team-member identity). A ``caller_name`` matching either falls
#: back to the bootstrap path.
_NON_TEAMMATE_MEMBER_NAMES = frozenset({"operon", "team-lead"})


def _bootstrap_identity(start: Path | None = None) -> dict[str, Any]:
    """Return the lead/bootstrap identity (current ``whoami()`` shape),
    or ``None``-filled stub if the env handle is unbound.

    Defensive wrapper used by :func:`resolve_caller_identity` so the
    teammate-identity fallback never raises.
    """
    try:
        base = whoami(start)
    except IdentityError as exc:
        _log.warning("resolve_caller_identity: bootstrap identity unbound: %s", exc)
        return {
            "name": None,
            "role": None,
            "workflow_id": None,
            "current_phase": None,
            "cwd": os.getcwd(),
            "session_id": None,
        }
    return base


def resolve_caller_identity(
    caller_name: str | None,
    start: Path | None = None,
) -> dict[str, Any]:
    """Land 6: resolve the calling team-member's identity.

    Under in-process Agent Teams (post-pivot) operon's MCP runs in
    the lead's claude process and serves every teammate's tool call
    through one stdio transport. The mcp-1.x SDK exposes
    ``request_ctx.get().meta`` per call, but the B.0 probe (commits
    ``b1571bf`` + ``2b4a7c3``) empirically showed that Anthropic's
    runtime does NOT forward teammate-identifying metadata on the
    JSON-RPC `_meta` field -- both lead-side and teammate-side
    whoami calls landed with identical ``meta.model_extra`` (only a
    ``claudecode/toolUseId``) and identical ``clientInfo``. **Branch
    beta** confirmed.

    The mechanism here is the operon-controlled prompt-injection
    fallback: the PreToolUse hook on the ``Agent`` tool
    (``plugins/operon-plugin/hooks/pretooluse.py``) prepends an
    ``[OPERON IDENTITY] ...`` directive to every spawned teammate's
    first turn instructing it to pass ``caller_name="<name>"`` on
    every ``mcp__operon__*`` tool call. Identity-aware tools
    (``whoami``, ``get_agent_info``, ``get_applicable_rules``) read
    this argument and delegate here.

    Resolution rules:

      * ``caller_name`` is ``None`` / empty / one of the reserved
        non-teammate slot names (``"operon"``, ``"team-lead"``):
        return the bootstrap identity (the lead's), tagged
        ``source="bootstrap"``.
      * ``caller_name`` is non-empty: look up the active team's
        roster from ``~/.claude/teams/<team>/config.json`` (reading
        fresh per v2.9 §4.6 -- no cache). Match by ``name``.
          - If matched AND the member's ``backendType`` is
            ``"in-process"`` (sanity check; in-process Agent Teams
            is the only supported mode), return a merged identity
            dict carrying the team-member fields and the operon-side
            phase/workflow state, tagged ``source="team_roster"``.
          - If no match or wrong backendType: warning log,
            fall back to the bootstrap identity, tagged
            ``source="bootstrap_fallback"``. The team-roster check
            is the impersonation defense: a teammate that supplies
            a ``caller_name`` not in the roster is treated as the
            lead (with the warning surfacing the attempt).

    Always returns a dict with at least:
        ``name, role, workflow_id, current_phase, cwd, session_id,
        source``
    plus when ``source == "team_roster"``:
        ``agent_type, color, agent_id, team_name``.

    Never raises -- failures degrade to ``source="bootstrap_fallback"``
    or a None-filled stub so downstream tools can always render a
    response.
    """
    # Local import avoids a circular cycle at module-load time
    # (inbox.py is a leaf; identity.py imports paths; both can be
    # imported in either order, but the import-here pattern matches
    # how restore_operon_session.py uses inbox.read_team_members).
    from . import inbox

    base = _bootstrap_identity(start)

    if not isinstance(caller_name, str) or not caller_name:
        base["source"] = "bootstrap"
        return base
    if caller_name in _NON_TEAMMATE_MEMBER_NAMES:
        # An LLM claiming to be operon-the-MCP or team-lead-the-lead
        # is conceptually self-impersonation; surface as the lead so
        # the answer is at least factually correct, but tag the
        # source so the audit trail shows the attempt.
        _log.warning(
            "resolve_caller_identity: caller_name=%r is a reserved "
            "non-teammate member; returning lead identity",
            caller_name,
        )
        base["source"] = "bootstrap_fallback"
        return base

    try:
        team_name = paths.active_run_dir(start).name
    except paths.OperonPathError as exc:
        _log.warning(
            "resolve_caller_identity: no active operon run "
            "(caller_name=%r): %s -- falling back to lead identity",
            caller_name,
            exc,
        )
        base["source"] = "bootstrap_fallback"
        return base

    members = inbox.read_team_members(team_name)
    matched: dict[str, Any] | None = None
    for m in members:
        if m.get("name") == caller_name:
            matched = m
            break
    if matched is None:
        _log.warning(
            "resolve_caller_identity: caller_name=%r did not match any "
            "member of team=%r; falling back to lead identity. "
            "(Roster: %s)",
            caller_name,
            team_name,
            [m.get("name") for m in members],
        )
        base["source"] = "bootstrap_fallback"
        return base

    backend = matched.get("backendType")
    if backend != "in-process":
        _log.warning(
            "resolve_caller_identity: caller_name=%r has backendType=%r "
            "(expected 'in-process'); falling back to lead identity",
            caller_name,
            backend,
        )
        base["source"] = "bootstrap_fallback"
        return base

    # Teammate identity. Inherit operon-side fields from the bootstrap
    # (workflow_id, current_phase, session_id, the lead's MCP session)
    # because they describe the operon-process state, which is shared
    # across all callers under singleton-MCP.
    agent_type = matched.get("agentType")
    if not isinstance(agent_type, str) or not agent_type:
        # Per Land 1's subagent_install convention name == agentType,
        # so this is the natural fallback when agentType is missing.
        agent_type = caller_name

    resolved = dict(base)
    resolved.update(
        {
            "name": caller_name,
            "role": agent_type,
            "agent_type": agent_type,
            "color": matched.get("color"),
            "agent_id": matched.get("agentId"),
            "team_name": team_name,
            "cwd": matched.get("cwd") or base.get("cwd"),
            "source": "team_roster",
        }
    )
    return resolved
