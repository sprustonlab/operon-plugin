"""Inbox-write primitive for the Anthropic Agent Teams substrate.

Land 2 of the Agent Teams Pivot.

Single function :func:`write_to_member_inbox` appends one entry
to ``~/.claude/teams/<team>/inboxes/<recipient>.json`` -- the
shared inbox file the Anthropic runtime watches. The file is
co-written by the runtime (when teammates invoke SendMessage or
the ``@<agent>`` TUI mention) and by operon (this module). The
two writers cooperate via stat-based optimistic concurrency: no
shared lockfile, no advisory locking; the discipline is

  1. ``os.stat`` the file -- capture ``(st_size, st_mtime_ns)``.
  2. Read the JSON array body. (If the file is absent, treat
     the body as ``[]`` and remember the file-was-absent state.)
  3. Append the new entry to the read body.
  4. ``os.stat`` the file again. If the captured stat tuple
     changed (including absent -> present), the runtime wrote
     during our read-modify window: re-read and retry.
  5. ``os.replace`` a tmp file onto the inbox path atomically.

Bounded retry: 5 attempts. On exhaustion the function raises
:class:`InboxWriteError`; the caller surfaces a structured error
to the lead's LLM rather than silently dropping the entry.

The empirical entry schema (verified 2026-05-21 against
``~/.claude/teams/land1-v2-test/inboxes/team-lead.json``)::

    {
      "from":      "operon",
      "text":      "<body>",
      "timestamp": "2026-05-21T20:40:24.677Z",
      "color":     "magenta",
      "read":      false
    }

The runtime makes no provenance check on the bytes -- a
well-formed entry from any writer is delivered to the
recipient teammate. The bytes ARE the protocol.

Cross-platform per project rules: ``pathlib.Path``,
``encoding="utf-8"``, ``os.replace`` (not ``Path.rename``),
ASCII-only.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: Anthropic's teams directory root.
TEAMS_DIR = Path.home() / ".claude" / "teams"

#: Default retry bound for the stat-retry loop (v2.9 plan section
#: 3.3 rule 1: "Bounded retry: at most 5 attempts.").
DEFAULT_MAX_RETRIES = 5

#: Brief backoff between retries. Pure latency softener; the loop
#: would still be correct with zero sleep, but a small pause cuts
#: down on hot-spinning under sustained contention.
_RETRY_BACKOFF_SECONDS = 0.01


class InboxWriteError(RuntimeError):
    """Raised on retry exhaustion or unrecoverable I/O failure
    inside :func:`write_to_member_inbox`."""


def _inbox_path(team_name: str, recipient_name: str) -> Path:
    """Resolve ``~/.claude/teams/<team>/inboxes/<recipient>.json``."""
    return TEAMS_DIR / team_name / "inboxes" / f"{recipient_name}.json"


def _stat_tuple(path: Path) -> tuple[int, int] | None:
    """Return ``(st_size, st_mtime_ns)`` for ``path``, or ``None`` if
    the file does not exist. Any other ``OSError`` propagates.

    The (size, mtime_ns) pair is the change-detection signal the
    plan specifies (v2.9 section 3.3 rule 1). The runtime's own
    writes go through lockfile-mediated ``tmp + os.replace``, which
    swaps in a fresh inode and changes both fields atomically; an
    unchanged tuple between our read and our write is strong
    evidence that the runtime did NOT interleave.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _read_inbox_array(path: Path) -> list[dict[str, Any]]:
    """Read the inbox JSON array. Returns ``[]`` if the file does
    not exist. Raises :class:`InboxWriteError` if the file exists
    but cannot be parsed as a JSON array.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise InboxWriteError(f"Failed to read inbox '{path}': {exc}") from exc
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InboxWriteError(f"Inbox '{path}' is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise InboxWriteError(
            f"Inbox '{path}' top-level value is {type(data).__name__}, expected list."
        )
    return data


def _atomic_write_json_array(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write ``entries`` as a JSON array to ``path`` via tmp +
    ``os.replace``. Creates parent directories on demand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        tmp.write_text(serialized, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise InboxWriteError(f"Failed to atomic-write inbox '{path}': {exc}") from exc


def _now_timestamp_z() -> str:
    """Return current UTC time as ISO8601 with ms precision and the
    trailing ``Z`` Anthropic's runtime emits (e.g.
    ``2026-05-21T20:40:24.677Z``).
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _lookup_operon_color(team_name: str, default: str = "magenta") -> str:
    """Read ``color`` from operon's own row in the team config, or
    return ``default`` if the config or row is missing/malformed.

    Defensive: never raises. The team config is runtime-owned and
    may not have operon's row yet at the moment of an early write;
    a fallback color keeps the primitive usable in that window.
    """
    config_path = TEAMS_DIR / team_name / "config.json"
    try:
        text = config_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return default
    if not isinstance(data, dict):
        return default
    members = data.get("members")
    if not isinstance(members, list):
        return default
    for m in members:
        if isinstance(m, dict) and m.get("name") == "operon":
            color = m.get("color")
            if isinstance(color, str) and color:
                return color
    return default


def build_operon_entry(team_name: str, text: str) -> dict[str, Any]:
    """Construct an operon-authored inbox entry in the empirical
    runtime schema. Exposed publicly so tools can build the entry
    once and pass it through :func:`write_to_member_inbox` for the
    actual stat-retry write.

    Fields:
      * ``from``  : the literal string ``"operon"`` (matches operon's
        registered team-member name; see plan section 4.7).
      * ``text``  : the caller-supplied body.
      * ``timestamp`` : ISO8601 UTC ms-precision with trailing ``Z``.
      * ``color`` : looked up from the team config; falls back to
        ``"magenta"``.
      * ``type``  : the literal string ``"message"``. Land 3
        empirical addition: today's demo showed every on-disk inbox
        entry carries this field. Either the runtime adds it
        post-write on delivery, or some other process does. To stay
        fully indistinguishable from runtime-authored writes, operon
        pre-includes it on every entry it produces.
      * ``read``  : ``False`` -- the runtime flips to ``True`` after
        delivery.
    """
    return {
        "from": "operon",
        "text": text,
        "timestamp": _now_timestamp_z(),
        "color": _lookup_operon_color(team_name),
        "type": "message",
        "read": False,
    }


def write_to_member_inbox(
    team_name: str,
    recipient_name: str,
    entry: dict[str, Any],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    """Append one entry to a team member's inbox file.

    Implements the optimistic-concurrency-with-retry sequence from
    v2.9 plan section 3.3 rule 1:

      1. Stat the file (or note absence).
      2. Read the JSON array body.
      3. Append the new entry.
      4. Re-stat -- if changed, retry.
      5. Otherwise atomic-write the new array.

    The retry bound is ``max_retries``. On exhaustion this function
    raises :class:`InboxWriteError`; operon does NOT fall back to
    an in-place write and does NOT silently drop the entry.

    Returns::

        {
          "inbox_path": "<absolute path to recipient inbox>",
          "entries_after_write": <int -- length of the array on disk>,
          "retries": <int -- 0 on first-pass success>,
        }
    """
    if not team_name:
        raise InboxWriteError("'team_name' must be a non-empty string.")
    if not recipient_name:
        raise InboxWriteError("'recipient_name' must be a non-empty string.")
    if not isinstance(entry, dict):
        raise InboxWriteError(f"'entry' must be a dict, got {type(entry).__name__}.")

    path = _inbox_path(team_name, recipient_name)
    path.parent.mkdir(parents=True, exist_ok=True)

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            initial = _stat_tuple(path)
            current = _read_inbox_array(path)
            updated = list(current)
            updated.append(entry)
            recheck = _stat_tuple(path)
            if initial == recheck:
                _atomic_write_json_array(path, updated)
                _log.info(
                    "inbox: wrote %s entry to %s (entries_after=%d, retries=%d)",
                    entry.get("from", "<unknown>"),
                    path,
                    len(updated),
                    attempt,
                )
                return {
                    "inbox_path": str(path),
                    "entries_after_write": len(updated),
                    "retries": attempt,
                }
            _log.debug(
                "inbox: stat mismatch on %s attempt %d "
                "(initial=%s, recheck=%s); retrying",
                path,
                attempt,
                initial,
                recheck,
            )
        except InboxWriteError as exc:
            # _read_inbox_array / _atomic_write_json_array already
            # wrapped the underlying OSError; preserve and break out
            # only if this is the final attempt.
            last_exc = exc
            _log.warning("inbox: attempt %d on %s failed: %s", attempt, path, exc)
        time.sleep(_RETRY_BACKOFF_SECONDS)

    raise InboxWriteError(
        f"Failed to write to inbox '{path}' after {max_retries} "
        f"attempts (stat-retry exhausted)."
    ) from last_exc


def read_team_members(team_name: str) -> list[dict[str, Any]]:
    """Read the team roster from
    ``~/.claude/teams/<team>/config.json`` and return its
    ``members`` list. Returns ``[]`` if the config is missing,
    malformed, or carries no members list.

    Read-on-demand per v2.9 plan section 4.6 (no caching): the
    runtime mutates this file on every spawn or shutdown
    handshake, so any cache opens a staleness window. Callers
    should call this fresh each time they need the roster.
    """
    config_path = TEAMS_DIR / team_name / "config.json"
    try:
        text = config_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    members = data.get("members")
    if not isinstance(members, list):
        return []
    return [m for m in members if isinstance(m, dict)]


def broadcast_to_team(
    team_name: str,
    text_for: Callable[[str], str | None],
    *,
    exclude_names: Iterable[str] = ("operon",),
) -> dict[str, Any]:
    """Write an operon-authored entry to every team member's inbox.

    For each member in the live team roster (re-read fresh from the
    team config per v2.9 plan section 4.6):

      * If the member's name is in ``exclude_names``, skip it
        (recorded under ``skipped`` in the return value).
      * Else call ``text_for(member_name)``. If it returns
        ``None``, skip the member (also recorded under
        ``skipped``). This is how the caller signals "no brief
        for this role" without ever attempting an empty write.
      * Else build an entry via :func:`build_operon_entry` and
        deliver via :func:`write_to_member_inbox`. The per-recipient
        retry budget is the function default.

    Per-recipient write failures do NOT abort the broadcast --
    each error is captured in ``errors`` so the caller can surface
    a structured manifest to the lead's LLM. The broadcast is
    best-effort by design: operon's phase advance is already
    committed to ``phase_state.json`` by the time advance_phase
    invokes this; a downstream inbox-write failure must not roll
    back the advance.

    Returns::

        {
          "team": "<team_name>",
          "recipients": [
            {"name": "<member>", "inbox_path": "<path>",
             "retries": <int>},
            ...
          ],
          "skipped": ["<member>", ...],
          "errors": [{"name": "<member>", "error": "<message>"}, ...],
        }
    """
    excluded = set(exclude_names)
    members = read_team_members(team_name)

    recipients: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []

    for m in members:
        name = m.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in excluded:
            skipped.append(name)
            continue
        try:
            body = text_for(name)
        except Exception as exc:  # noqa: BLE001 -- caller closure can raise anything
            errors.append({"name": name, "error": f"text_for raised: {exc}"})
            continue
        if body is None:
            skipped.append(name)
            continue
        entry = build_operon_entry(team_name=team_name, text=body)
        try:
            result = write_to_member_inbox(
                team_name=team_name,
                recipient_name=name,
                entry=entry,
            )
        except InboxWriteError as exc:
            errors.append({"name": name, "error": str(exc)})
            continue
        recipients.append(
            {
                "name": name,
                "inbox_path": result["inbox_path"],
                "retries": result["retries"],
            }
        )

    return {
        "team": team_name,
        "recipients": recipients,
        "skipped": skipped,
        "errors": errors,
    }
