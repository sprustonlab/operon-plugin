"""Inbox-watcher helpers for lead-driven sub-acts.

Per Q23c: sub-acts 5/6/7 are driven from the lead's pane via
``mcp__operon__send_to_member``. The lead routes a prompt to a
teammate; the teammate executes; the teammate replies via
``SendMessage`` back to the lead. The reply lands in the lead's
inbox file at ``~/.claude/teams/<run>/inboxes/team-lead.json``.

The scenario's idle predicate (JSONL transcript stop_reason) is
necessary but not sufficient for these sub-acts: the lead's
turn completes when ``send_to_member`` returns, but the actual
sub-act completion signal is the teammate's reply landing in
the lead's inbox file. This module provides the supplementary
inbox-watch predicates.

Cross-platform: ``pathlib.Path``, UTF-8, ASCII-only.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable


def read_inbox(inbox_path: Path) -> list[dict]:
    """Return the parsed inbox-file entries, or [] if absent/invalid."""
    if not inbox_path.is_file():
        return []
    try:
        data = json.loads(inbox_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def is_idle_notification(entry: dict) -> bool:
    """True if ``entry`` is a runtime-auto-emitted idle notification.

    The Anthropic runtime drops these into inbox files alongside real
    SendMessage entries; they have ``text`` starting with the literal
    JSON ``{"type":"idle_notification"``. Predicates that match
    "any reply from X" should typically exclude these.
    """
    text = entry.get("text") or ""
    if not isinstance(text, str):
        return False
    return text.lstrip().startswith('{"type":"idle_notification"')


def wait_for_inbox_entry(
    inbox_path: Path,
    predicate: Callable[[dict], bool],
    *,
    timeout_s: float,
    poll_s: float = 0.5,
    baseline_count: int | None = None,
) -> dict | None:
    """Poll ``inbox_path`` until ``predicate(entry)`` matches a NEW entry.

    ``baseline_count`` is the number of entries already in the inbox
    at the start of waiting; only entries at index >= baseline_count
    are considered. If omitted, the count is read from the inbox at
    call time.

    Returns the matching entry on success; None on timeout.
    """
    if baseline_count is None:
        baseline_count = len(read_inbox(inbox_path))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        entries = read_inbox(inbox_path)
        for entry in entries[baseline_count:]:
            if predicate(entry):
                return entry
        time.sleep(poll_s)
    return None


def inbox_baseline(inbox_path: Path) -> int:
    """Return the current entry count for use as a baseline."""
    return len(read_inbox(inbox_path))
