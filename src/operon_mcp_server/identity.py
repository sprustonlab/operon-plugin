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

#: Phase 14: process-local fallback for callers that have no env handle
#: set. Populated by `bootstrap.auto_bootstrap_if_needed` at MCP server
#: startup so the subprocess can identify itself as the project's
#: default Coordinator without the user having to export the env var.
#: NOT visible across subprocesses (workers spawned via `spawn_agent`
#: get their handle via env, not via this cache).
_CACHED_HANDLE: str | None = None

#: Phase 14 fix 4: defensive freeze. After
#: `bootstrap.auto_bootstrap_if_needed` resolves the subprocess's
#: identity, it calls `freeze_handle()` which sets this module-global
#: to a one-time-immutable value. Any subsequent attempt to change
#: identity (env mutation, cache override, anything) is observable:
#: `read_env_handle()` keeps returning the frozen value, and
#: `_set_cached_handle()` logs a WARNING with the would-be new value
#: so the bug surface is visible in MCP logs.
#:
#: This is a defense against a real bug Boaz hit where the
#: Coordinator's `whoami` returned a spawned worker's identity. The
#: mutation source was not traceable via static analysis; the freeze
#: stops the bug from manifesting AND captures evidence of the
#: mutation attempt.
_FROZEN_HANDLE: str | None = None


class IdentityError(RuntimeError):
    """Raised when identity cannot be resolved for the calling subprocess."""


def freeze_handle(handle: str) -> None:
    """Pin `handle` as the subprocess's permanent identity (Phase 14 fix 4).

    Called by `bootstrap.auto_bootstrap_if_needed` once it has
    resolved the canonical handle for this MCP subprocess (either via
    env, existing-coordinator discovery, or fresh bootstrap). After
    this call:

      - `read_env_handle()` returns `handle` regardless of env or
        cache state.
      - Any subsequent `_set_cached_handle(other)` logs a WARNING
        with stack info pointing at the mutation site -- that's the
        bug evidence we want.
      - A second `freeze_handle(other)` call with a different value
        is a no-op + ERROR log (idempotent on same value).

    Worker MCP subprocesses also call this with their own env-anchored
    handle, so the freeze is per-subprocess (not cross-process).
    """
    global _FROZEN_HANDLE
    if not isinstance(handle, str) or not handle:
        raise ValueError("freeze_handle: handle must be non-empty string")
    if _FROZEN_HANDLE is None:
        _FROZEN_HANDLE = handle
        _log.info("identity frozen: handle=%s", handle)
        return
    if _FROZEN_HANDLE == handle:
        _log.debug("freeze_handle: idempotent re-freeze of %s", handle)
        return
    # Different value -> bug surface. Log loudly + DO NOT mutate.
    _log.error(
        "identity freeze rejected: already frozen at %r, attempted re-freeze "
        "to %r. Keeping the original value. Investigate the call site -- "
        "this likely indicates a stray bootstrap re-run or identity "
        "mutation bug.",
        _FROZEN_HANDLE,
        handle,
        stack_info=True,
    )


def get_frozen_handle() -> str | None:
    """Return the frozen identity for this subprocess, or None if
    `freeze_handle` has not been called yet (e.g. tests; pre-bootstrap)."""
    return _FROZEN_HANDLE


def _set_cached_handle(handle: str | None) -> None:
    """Set the process-local handle cache (Phase 14).

    Called by `bootstrap.auto_bootstrap_if_needed`. Subsequent
    `read_env_handle()` calls fall through to this value when the env
    var is unset and the frozen handle is also unset. Passing `None`
    clears the cache (used in tests).

    Phase 14 fix 4: if the frozen handle is set and the new cache
    value differs, log a WARNING with stack info. This captures the
    mutation site of the as-yet-untraced identity-swap bug without
    actually letting the mutation take effect on resolution.
    """
    global _CACHED_HANDLE
    if (
        _FROZEN_HANDLE is not None
        and handle is not None
        and handle != _FROZEN_HANDLE
    ):
        _log.warning(
            "_set_cached_handle called with %r after identity was frozen "
            "at %r. The mutation will land in the cache but will NOT "
            "affect read_env_handle() (frozen value takes precedence). "
            "Investigate the call stack -- this is the mutation source "
            "for the identity-swap bug.",
            handle,
            _FROZEN_HANDLE,
            stack_info=True,
        )
    _CACHED_HANDLE = handle


def get_cached_handle() -> str | None:
    """Return the current process-local handle cache value (Phase 14).

    Exposed for diagnostics + tests. Production code uses
    `read_env_handle()` which already consults the cache as a
    fallback.
    """
    return _CACHED_HANDLE


def read_env_handle() -> str | None:
    """Return the handle for this MCP subprocess.

    Resolution order (Phase 14 fix 4):

      1. `_FROZEN_HANDLE` if set (Phase 14 fix 4 defensive freeze).
         Once `bootstrap.auto_bootstrap_if_needed` has resolved the
         canonical identity and called `freeze_handle`, this wins
         over both env and cache so a stray mutation can't flip
         identity mid-flight.
      2. `OPERON_AGENT_HANDLE` env var. Workers spawned via
         `spawn_agent` always have this set; the user may also have
         manually bound a fixture handle.
      3. Process-local cache populated by
         `bootstrap.auto_bootstrap_if_needed` at MCP server startup.

    Returns None when none of the three has a non-empty handle.
    Empty string env values are treated as unset.

    Debug logging (when OPERON_DEBUG is on): every call logs
    (env, cached, frozen, returned) so a misbehaving subprocess's
    identity-resolution chain can be reconstructed from MCP logs.
    """
    env_value = os.environ.get(ENV_HANDLE_VAR)
    if _FROZEN_HANDLE is not None:
        _log.debug(
            "read_env_handle: env=%r cache=%r frozen=%r -> frozen wins",
            env_value,
            _CACHED_HANDLE,
            _FROZEN_HANDLE,
        )
        return _FROZEN_HANDLE
    if env_value:
        _log.debug(
            "read_env_handle: env=%r cache=%r frozen=None -> env wins",
            env_value,
            _CACHED_HANDLE,
        )
        return env_value
    _log.debug(
        "read_env_handle: env=None cache=%r frozen=None -> cache returned",
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
