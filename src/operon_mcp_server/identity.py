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
import os
from pathlib import Path
from typing import Any

from . import paths

#: Name of the env var that anchors per-subprocess identity (SPEC 6.5).
ENV_HANDLE_VAR = "OPERON_AGENT_HANDLE"


class IdentityError(RuntimeError):
    """Raised when identity cannot be resolved for the calling subprocess."""


def read_env_handle() -> str | None:
    """Return the handle from `OPERON_AGENT_HANDLE`, or None if unset.

    An empty-string env value is treated as unset (returns None) so that
    callers do not need to distinguish "missing" from "blank".
    """
    value = os.environ.get(ENV_HANDLE_VAR)
    if not value:
        return None
    return value


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
        raise IdentityError(
            f"Failed to read phase state '{file_path}': {exc}"
        ) from exc
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
