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
) -> bool:
    """Wait until the assistant has stopped AND inboxes are quiescent.

    Returns True iff both predicates hold before ``timeout_s``.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        observer.read_new()
        last = observer.last_assistant_stop()
        if last is not None:
            if inboxes_tracker is None or inboxes_tracker.quiescent_for(k_ms):
                return True
        time.sleep(poll_s)
    return False


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
