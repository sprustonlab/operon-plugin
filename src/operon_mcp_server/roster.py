"""Agent roster: read/write `<run-dir>/agents.json`.

Per SPEC.md section 16 (`roster.py` row) and section 17 (`agents.json`
row), this module owns the on-disk roster of Agents for the active
operon-run. It is a single-writer module per SPEC.md section 6.6: only
the Coordinator's MCP subprocess writes here. All writes go through a
temp file + `os.replace` for atomicity; no compare-and-swap or revision
field is used because no other subprocess writes the same file.

Row schema (SPEC.md section 17, `agents.json` row):

    {
      "name": "<agent_name>",
      "role": "<role_id>",
      "handle": "<uuid4>",
      "session_id": "<uuid4>",
      "workflow_id": "<workflow_id>",
      "status": "idle" | "busy" | "closed",
      "spawned_at": "<iso8601>",
      "last_turn_at": "<iso8601>"
    }

The current phase is NOT recorded here (per SPEC.md section 17 amendment);
live phase reads from `<run-dir>/phase_state.json` on demand. The
reply-obligation state lives in `mailbox/<agent>/_pending_reply_to.json`
(SPEC.md section 7.2), not on this row.

Cross-platform: `pathlib.Path` only, UTF-8 on all I/O, `os.replace` for
atomic rename (NOT `Path.rename` which fails on Windows when the target
exists). No platform-gated APIs.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from . import paths

#: Name of the roster file under the active run directory.
ROSTER_FILENAME = "agents.json"

#: Allowed values for the `status` field of a row (SPEC section 17).
_ALLOWED_STATUSES = frozenset({"idle", "busy", "closed"})


class RosterError(RuntimeError):
    """Raised when roster I/O or validation fails."""


def roster_file(start: Path | None = None) -> Path:
    """Return `<run-dir>/agents.json` for the active run."""
    return paths.active_run_dir(start) / ROSTER_FILENAME


def _validate_row(row: dict[str, Any]) -> None:
    """Validate a roster row matches the SPEC section 17 schema.

    Raises `RosterError` on missing required fields or invalid `status`.
    """
    required = (
        "name",
        "role",
        "handle",
        "session_id",
        "workflow_id",
        "status",
        "spawned_at",
        "last_turn_at",
    )
    missing = [k for k in required if k not in row]
    if missing:
        raise RosterError(
            f"Roster row missing required fields: {missing}. Row: {row!r}"
        )
    status = row.get("status")
    if status not in _ALLOWED_STATUSES:
        raise RosterError(
            f"Invalid roster status {status!r}; must be one of "
            f"{sorted(_ALLOWED_STATUSES)}."
        )


def read_roster(start: Path | None = None) -> list[dict[str, Any]]:
    """Read the roster from `<run-dir>/agents.json`.

    Returns the empty list if the file does not yet exist (no Agents
    spawned). Raises `RosterError` if the file exists but is malformed.
    """
    try:
        path = roster_file(start)
    except paths.OperonPathError as exc:
        raise RosterError(str(exc)) from exc
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RosterError(f"Failed to read roster '{path}': {exc}") from exc
    if not isinstance(data, list):
        raise RosterError(
            f"Roster file '{path}' must contain a JSON list, got {type(data).__name__}."
        )
    return data


def _atomic_write_roster(rows: list[dict[str, Any]], start: Path | None = None) -> None:
    """Write `rows` atomically to `<run-dir>/agents.json`.

    Uses temp file + `os.replace` per SPEC section 6.6. The temp file
    name embeds pid + a fresh uuid hex so concurrent writers (in
    principle disallowed by the single-writer contract) cannot collide
    on the temp path.
    """
    try:
        path = roster_file(start)
    except paths.OperonPathError as exc:
        raise RosterError(str(exc)) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    payload = json.dumps(rows, indent=2, ensure_ascii=False)
    try:
        tmp.write_text(payload, encoding="utf-8")
        # os.replace is the cross-platform atomic rename per SPEC section 2.
        os.replace(tmp, path)
    except OSError as exc:
        # Best-effort temp cleanup; failure here is non-fatal.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RosterError(f"Failed to write roster '{path}': {exc}") from exc


def append_agent(row: dict[str, Any], start: Path | None = None) -> None:
    """Append a new row to the roster. Raises if `name` already present."""
    _validate_row(row)
    rows = read_roster(start)
    existing = {r.get("name") for r in rows}
    if row["name"] in existing:
        raise RosterError(
            f"Agent name {row['name']!r} already present in roster; "
            "names must be unique within a run."
        )
    rows.append(row)
    _atomic_write_roster(rows, start)


def find_agent(name: str, start: Path | None = None) -> dict[str, Any] | None:
    """Return the row for `name`, or None if not present."""
    for row in read_roster(start):
        if row.get("name") == name:
            return row
    return None


def find_agent_by_session(
    session_id: str, start: Path | None = None
) -> dict[str, Any] | None:
    """Return the row for `session_id`, or None if not present."""
    for row in read_roster(start):
        if row.get("session_id") == session_id:
            return row
    return None


def update_agent(
    name: str, updates: dict[str, Any], start: Path | None = None
) -> dict[str, Any]:
    """Merge `updates` into the named row. Atomic. Returns the new row.

    Raises `RosterError` if `name` is not in the roster, or if the
    resulting row fails schema validation.
    """
    rows = read_roster(start)
    for i, row in enumerate(rows):
        if row.get("name") == name:
            new_row = {**row, **updates}
            _validate_row(new_row)
            rows[i] = new_row
            _atomic_write_roster(rows, start)
            return new_row
    raise RosterError(f"Agent {name!r} not in roster; cannot update.")


def remove_agent(name: str, start: Path | None = None) -> None:
    """Remove the row for `name` from the roster. No-op if absent.

    Atomic rewrite; reads-then-writes-then-replaces.
    """
    rows = read_roster(start)
    remaining = [r for r in rows if r.get("name") != name]
    if len(remaining) == len(rows):
        return  # absent: no-op
    _atomic_write_roster(remaining, start)
