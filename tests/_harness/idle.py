"""Idle-predicate helpers.

Per TEST_SPECIFICATION.md "Idle predicates" + Q7 (K=1500ms):

  Pre-kill (sub-acts 1-8): last JSONL record is assistant with
                            stop_reason AND no inbox file bytes
                            mutated in K ms.
  During-kill (sub-act 9): tmux session PID absent.
  Post-restore (sub-act 10): re-discovered JSONL path's last record
                             is assistant with stop_reason AND no
                             inbox file bytes mutated in K ms.

This module composes :class:`TranscriptObserver` and
:class:`InboxQuiescenceTracker` into one ``wait_idle()`` call per the
regime.

Cross-platform: ``pathlib.Path``, UTF-8, ASCII-only.
"""
from __future__ import annotations

import time
from pathlib import Path

from .transcript_observer import (
    DEFAULT_IDLE_K_MS,
    InboxQuiescenceTracker,
    TranscriptObserver,
)


def wait_idle_pre_kill(
    observer: TranscriptObserver,
    inboxes_tracker: InboxQuiescenceTracker | None,
    timeout_s: float,
    k_ms: int = DEFAULT_IDLE_K_MS,
    poll_s: float = 0.25,
    after_marker_uuid: str | None = None,
) -> bool:
    """Wait until the assistant has stopped AND inboxes are quiescent.

    Returns True iff both predicates hold before ``timeout_s``.

    ``after_marker_uuid``: if provided, the function requires that the
    most recent assistant-with-stop_reason record has a uuid DIFFERENT
    from this marker. Use this to wait for a NEW turn boundary after a
    fresh ``driver.send()`` -- otherwise the pre-existing
    assistant-stop from the previous sub-act would make this function
    return True immediately.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        observer.read_new()
        last = observer.last_assistant_stop()
        if last is not None and last.get("uuid") != after_marker_uuid:
            if inboxes_tracker is None or inboxes_tracker.quiescent_for(k_ms):
                return True
        time.sleep(poll_s)
    return False


def latest_stop_uuid(observer: TranscriptObserver) -> str | None:
    """Return the uuid of the latest assistant-with-stop_reason record,
    or None if the transcript has no such record yet. Useful as a
    `before-send` marker to feed back into wait_idle_pre_kill.
    """
    observer.read_new()
    last = observer.last_assistant_stop()
    return last.get("uuid") if last else None


def wait_idle_during_kill(
    pid_check,
    timeout_s: float,
    poll_s: float = 0.25,
) -> bool:
    """Wait until ``pid_check()`` returns None / falsy (process gone)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not pid_check():
            return True
        time.sleep(poll_s)
    return False


def flush_lead_background_tasks(
    driver,
    observer: TranscriptObserver,
    timeout_s: float = 60.0,
) -> bool:
    """Ask the lead to terminate any active background Monitor / Task tools.

    Per the Land 11 R1+R2 investigation and the Monitor-leak verdict:
    the lead chooses on its own initiative to use the ``Monitor`` tool
    to satisfy "wait for X to land in your inbox" prompts in sub-acts
    5/6/7. Each Monitor has a 120 s timeout; lingering Monitor tasks
    block subsequent tool calls (notably ``advance_phase`` in sub-act
    8), causing the lead to never reach the next sub-act's MCP call.

    This helper sends the lead a housekeeping prompt asking it to
    ``TaskStop`` any active background tasks, then waits for idle.
    Idempotent: if no tasks are active, the lead replies
    ``NO-MONITORS`` and stops. Always returns True if the wait_idle
    succeeded; False if it timed out.

    Usage pattern: call AFTER a sub-act's inbox-wait succeeds, BEFORE
    the next sub-act's ``recorder.set_sub_act(...)``. That keeps the
    cleanup logically attached to the originating sub-act's tail.
    """
    marker = latest_stop_uuid(observer)
    driver.send(
        "Housekeeping: if you have any active background "
        "Monitor or Task tools running from prior sub-acts, "
        "terminate them all NOW via TaskStop. If you have "
        "none active, reply 'NO-MONITORS' and stop. Either "
        "way, end the turn after the cleanup."
    )
    return wait_idle_pre_kill(
        observer=observer,
        inboxes_tracker=None,
        timeout_s=timeout_s,
        after_marker_uuid=marker,
    )
