"""Polling reader for ``~/.claude/teams/<team>/inboxes/operon.json``.

Land 7 inbox channel for identity / introspection queries.

Operon's MCP runs as a singleton in the lead's claude process; a
teammate cannot reach operon-side state via direct MCP without
the lead's identity bleeding through. The teammate's only
unspoofable channel back to operon is the Anthropic-runtime
inbox: ``SendMessage(to="operon", text="[OPERON_QUERY] ...")``
stamps a server-side ``from`` field that the teammate cannot
forge. This module reads operon's inbox file, hands each new
entry to :mod:`query_protocol.dispatch_query`, and advances a
cursor on disk so restarts do not re-process entries that have
already been replied to.

Design choices:

  * **Polling, not watchdog.** A 1-second poll is simpler than
    a watchdog/inotify dependency and matches the latency budget
    Boaz approved (5-15 s end-to-end, dominated by LLM-turn cost).
    The hook can be swapped to filesystem watching later if
    latency matters.
  * **Cursor by timestamp.** Each entry carries an ISO-8601 UTC
    millisecond timestamp emitted by either operon's own writer
    or Anthropic's runtime. The cursor file
    ``<run-dir>/inbox_cursor.json`` records the highest processed
    timestamp; on each poll, entries with ``timestamp >`` cursor
    are dispatched and the cursor advances to the latest entry's
    timestamp.
  * **Cursor advances on handler errors too.** A broken or
    unsupported query produces a structured reply, not a retry
    loop. Re-processing the same entry indefinitely is worse
    than dropping a single response.
  * **Started during MCP server lifespan.** ``server._run``
    schedules :func:`run_forever` on the anyio task group; the
    task self-cancels when the task group cancels at shutdown.

Cross-platform per project rules: ``pathlib.Path``,
``encoding="utf-8"``, ``os.replace`` for the cursor file's
atomic write, ASCII-only.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import anyio

from . import inbox, paths, query_protocol

_log = logging.getLogger(__name__)

#: Polling interval between scans of operon's inbox file. Tuned
#: to match the latency budget (LLM turn cost dwarfs file I/O at
#: this cadence).
_POLL_INTERVAL_SECONDS = 1.0

#: Operon's reserved member name (mirrors
#: ``subagent_install.OPERON_MEMBER_NAME``).
_OPERON_MEMBER_NAME = "operon"

#: Anthropic's teams directory.
_TEAMS_DIR = Path.home() / ".claude" / "teams"

#: Cursor file name under the active run-dir.
_CURSOR_FILENAME = "inbox_cursor.json"


def _operon_inbox_path(team_name: str) -> Path:
    """Resolve ``~/.claude/teams/<team>/inboxes/operon.json``."""
    return _TEAMS_DIR / team_name / "inboxes" / f"{_OPERON_MEMBER_NAME}.json"


def _cursor_path() -> Path | None:
    """Return the cursor file path under the active run-dir, or
    ``None`` if there is no active run (the reader will skip in
    that case).
    """
    try:
        run_dir = paths.active_run_dir()
    except paths.OperonPathError:
        return None
    return run_dir / _CURSOR_FILENAME


def _read_cursor() -> str:
    """Return the highest processed timestamp, or empty string if
    the cursor file is absent or malformed. An empty string sorts
    before every ISO-8601 timestamp, so a fresh reader will
    consider every entry new.
    """
    path = _cursor_path()
    if path is None:
        return ""
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("inbox_reader: cursor file unreadable (%s): %s", path, exc)
        return ""
    if not isinstance(data, dict):
        return ""
    cursor = data.get("highest_processed_timestamp")
    if not isinstance(cursor, str):
        return ""
    return cursor


def _write_cursor(timestamp: str) -> None:
    """Atomically write the cursor file. Best-effort: a write
    failure logs a warning but does not crash the reader (the
    next poll will retry).
    """
    path = _cursor_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "highest_processed_timestamp": timestamp,
        # `team`/`run_name` are derivable but storing them aids
        # debugging on disk.
        "run_name": path.parent.name,
    }
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        tmp.write_text(serialized, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        _log.warning("inbox_reader: failed to write cursor file %s: %s", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_operon_inbox(team_name: str) -> list[dict[str, Any]]:
    """Read operon's inbox file. Returns ``[]`` on missing or
    malformed file. Defensive: the reader runs every second and
    should never raise.
    """
    path = _operon_inbox_path(team_name)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("inbox_reader: failed to read %s: %s", path, exc)
        return []
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning("inbox_reader: %s is not valid JSON: %s", path, exc)
        return []
    if not isinstance(data, list):
        _log.warning(
            "inbox_reader: %s top-level value is %s, expected list",
            path,
            type(data).__name__,
        )
        return []
    return [e for e in data if isinstance(e, dict)]


def _scan_once(team_name: str) -> int:
    """One polling iteration. Returns the number of entries
    dispatched. Cursor is advanced after each successful dispatch
    AND after handler errors that produced a structured reply
    (the reply itself is the "we saw your message" signal -- no
    point reprocessing).
    """
    cursor = _read_cursor()
    entries = _read_operon_inbox(team_name)

    # Sort by timestamp ascending so the cursor advances
    # monotonically even if the file is not stored in order.
    def _ts_key(e: dict[str, Any]) -> str:
        v = e.get("timestamp", "")
        return v if isinstance(v, str) else ""

    entries.sort(key=_ts_key)

    dispatched = 0
    new_cursor = cursor
    for e in entries:
        ts = e.get("timestamp")
        if not isinstance(ts, str) or not ts:
            continue
        if cursor and ts <= cursor:
            continue
        # Skip operon's own writes (the dispatch loop's replies
        # land in OTHER members' inboxes, but defensive: if any
        # entry with from="operon" appears in operon's own inbox
        # we don't reply to ourselves).
        if e.get("from") == _OPERON_MEMBER_NAME:
            if ts > new_cursor:
                new_cursor = ts
            continue
        manifest = query_protocol.dispatch_query(team_name, e)
        if manifest.get("dispatched"):
            _log.info(
                "inbox_reader: dispatched %s from=%s -> %s",
                manifest.get("command"),
                manifest.get("from"),
                manifest.get("reply_inbox_path"),
            )
            dispatched += 1
        elif manifest.get("reason") == "not_a_query":
            # Free-form text from a teammate; we don't reply.
            # Advance the cursor so we don't re-read it next poll.
            _log.debug(
                "inbox_reader: skipping non-query from=%s ts=%s",
                e.get("from"),
                ts,
            )
        else:
            _log.warning(
                "inbox_reader: dispatch failed from=%s ts=%s reason=%s",
                e.get("from"),
                ts,
                manifest.get("reason"),
            )
        if ts > new_cursor:
            new_cursor = ts

    if new_cursor != cursor:
        _write_cursor(new_cursor)
    return dispatched


async def run_forever() -> None:
    """Run the polling loop until the surrounding task group
    cancels.

    Re-resolves the active operon run / team name on EVERY
    iteration -- not just once at boot. The Land 7 hotfix
    (2026-05-22) surfaced that the original "resolve once at
    server boot" version snapshotted the bootstrap-auto-created
    ``default`` team and never noticed when ``activate_workflow``
    later flipped ``_active.json`` to a user-named team; every
    teammate query landed in the new team's inbox and was never
    read. The fix is one stat + one tiny JSON read per second
    (cheap; ``_active.json`` is a few hundred bytes), which is
    well below the latency budget Boaz approved.

    If there is no active run when an iteration starts (e.g. the
    MCP server booted before ``activate_workflow`` ran), the
    iteration is a no-op and we wait for the next poll. No crash,
    no error log spam -- just an INFO line the first time we
    successfully resolve an active team, and another INFO line
    whenever the resolved team changes, so the transition is
    observable from operon's stderr.

    Defensive against transient errors: a per-iteration exception
    is logged and the loop continues. The only path that exits
    the loop is anyio cancellation (clean shutdown).
    """
    _log.info(
        "inbox_reader: starting poll loop (interval=%.1fs, "
        "active team resolved per-iteration)",
        _POLL_INTERVAL_SECONDS,
    )
    last_team: str | None = None
    try:
        while True:
            try:
                team_name = resolve_active_team_name()
                if team_name != last_team:
                    if team_name is None:
                        _log.info(
                            "inbox_reader: no active operon run; "
                            "iteration is a no-op until activate_workflow "
                            "or restore_operon_session sets _active.json"
                        )
                    elif last_team is None:
                        _log.info(
                            "inbox_reader: active team resolved to %r; "
                            "polling its inbox",
                            team_name,
                        )
                    else:
                        _log.info(
                            "inbox_reader: active team switched %r -> %r; "
                            "polling new inbox",
                            last_team,
                            team_name,
                        )
                    last_team = team_name
                if team_name is not None:
                    _scan_once(team_name)
            except Exception as exc:  # noqa: BLE001 -- loop must survive
                _log.exception("inbox_reader: scan iteration raised: %s", exc)
            await anyio.sleep(_POLL_INTERVAL_SECONDS)
    except anyio.get_cancelled_exc_class():
        _log.info("inbox_reader: cancelled; exiting cleanly")
        raise


def resolve_active_team_name() -> str | None:
    """Return the active operon run name (== team name under the
    Land 1 v2 convention), or ``None`` if there is no active run.

    Mirrors the pattern used by :mod:`send_to_member` and
    :mod:`restore_operon_session`. Called every poll iteration by
    :func:`run_forever` so a mid-process ``activate_workflow`` is
    picked up promptly.
    """
    try:
        run_dir = paths.active_run_dir()
    except paths.OperonPathError:
        return None
    return run_dir.name


# Silence the unused-import warning if `inbox` ever stops being
# referenced here; query_protocol.dispatch_query owns the actual
# write path.
_ = inbox
