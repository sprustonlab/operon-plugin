"""Filesystem path resolution for operon-plugin project-local state.

Per SPEC.md section 16, this module is the single source of truth for
resolving paths under `<project>/.operon/`. It does no I/O of its own
beyond reading the `_active.json` pointer in `active_run_dir()` -- all
other functions are pure path composition.

Layout (see SPEC.md section 17):

    <project>/.operon/
        _active.json                 # {active_run_name, set_at}
        <run-name>/
            _handles/
                <handle>.json        # per-Agent identity anchor
            phase_state.json
            agents.json
            ...

Cross-platform: `pathlib.Path` only, UTF-8 on all I/O, no platform
gating (no signal handling or pty here).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: Name of the project-local state directory.
OPERON_DIRNAME = ".operon"

#: Name of the per-project active-run pointer file (SPEC section 17).
ACTIVE_POINTER_FILENAME = "_active.json"

#: Name of the handles subdirectory under each run (SPEC section 17).
HANDLES_DIRNAME = "_handles"


class OperonPathError(RuntimeError):
    """Raised when the project's `.operon/` state cannot be resolved."""


def project_root(start: Path | None = None) -> Path:
    """Locate the project root by walking up from `start` (default: cwd).

    The project root is the nearest ancestor (inclusive) that contains a
    `.operon/` directory. Raises `OperonPathError` if no such ancestor
    exists.

    Parameters
    ----------
    start:
        Starting directory for the walk. Defaults to the current
        working directory.
    """
    here = Path(start) if start is not None else Path(os.getcwd())
    here = here.resolve()
    for candidate in (here, *here.parents):
        if (candidate / OPERON_DIRNAME).is_dir():
            return candidate
    raise OperonPathError(
        f"No '{OPERON_DIRNAME}/' directory found in '{here}' or any ancestor; "
        "operon-plugin needs to be invoked inside a project that has been "
        "initialised with an operon run."
    )


def operon_dir(start: Path | None = None) -> Path:
    """Return `<project>/.operon/`."""
    return project_root(start) / OPERON_DIRNAME


def active_pointer_file(start: Path | None = None) -> Path:
    """Return `<project>/.operon/_active.json`."""
    return operon_dir(start) / ACTIVE_POINTER_FILENAME


def _read_active_run_name(start: Path | None = None) -> str:
    """Read the active run name from `_active.json`.

    Raises `OperonPathError` if the pointer file is missing, malformed,
    or does not contain a non-empty `active_run_name` string.
    """
    pointer = active_pointer_file(start)
    if not pointer.is_file():
        raise OperonPathError(
            f"Active-run pointer not found at '{pointer}'. "
            "Has `activate_workflow` been run for this project?"
        )
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperonPathError(f"Failed to read '{pointer}': {exc}") from exc
    name = data.get("active_run_name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        raise OperonPathError(
            f"'{pointer}' is missing a non-empty 'active_run_name' field."
        )
    return name


def active_run_dir(start: Path | None = None) -> Path:
    """Return `<project>/.operon/<active_run_name>/`.

    Resolves the active run by reading `_active.json`. Raises
    `OperonPathError` if the pointer is missing/malformed or if the
    resolved run directory does not exist.
    """
    name = _read_active_run_name(start)
    run_dir = operon_dir(start) / name
    if not run_dir.is_dir():
        raise OperonPathError(
            f"Active run directory '{run_dir}' does not exist "
            f"(pointed at by '{active_pointer_file(start)}')."
        )
    return run_dir


def handles_dir(start: Path | None = None) -> Path:
    """Return `<run-dir>/_handles/` for the active run."""
    return active_run_dir(start) / HANDLES_DIRNAME


def handle_file(handle: str, start: Path | None = None) -> Path:
    """Return `<run-dir>/_handles/<handle>.json` for the active run.

    Does not validate that the file exists -- callers that need read
    semantics (e.g. `identity.read_handle_file`) check existence.
    """
    if not handle:
        raise OperonPathError("handle must be a non-empty string")
    return handles_dir(start) / f"{handle}.json"


def phase_state_file(start: Path | None = None) -> Path:
    """Return `<run-dir>/phase_state.json` for the active run."""
    return active_run_dir(start) / "phase_state.json"
