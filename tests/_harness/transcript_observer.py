"""JSONL transcript observer.

Per TEST_SPECIFICATION.md "Test bed" + "Idle predicates" sections,
the scenario observes Claude Code via the JSONL transcript file that
the runtime appends to at
``~/.claude/projects/<cwd-mangled>/<session-uuid>.jsonl``.

The runtime's cwd-mangling: each path component separator ``/`` becomes
``-``. Leading ``/`` produces a leading ``-``; trailing ``/`` is
typically not present in the path so trailing ``-`` is uncommon.
Verified against Step 0.5 spike artifacts where the cwd
``/tmp/operon-teams-spike/step05`` mapped to
``-tmp-operon-teams-spike-step05``.

This module exposes:

- :func:`mangle_cwd_path` -- compute the cwd-mangled directory name.
- :func:`find_transcript` -- locate the JSONL for a given cwd + session.
- :class:`TranscriptObserver` -- tailing observer with idle predicates
  and pattern-wait helpers.

Cross-platform: ``pathlib.Path``, UTF-8, ASCII-only.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# 1500 ms inbox-bytes quiescence + last-assistant-stop_reason
# (Q7 coordinator answer).
DEFAULT_IDLE_K_MS = 1500


def mangle_cwd_path(cwd: Path) -> str:
    """Compute the cwd-mangled directory name used by Claude Code.

    Replaces ``/`` with ``-`` in the absolute path. Anything that
    starts with ``/`` therefore gets a leading ``-`` in the result,
    matching the runtime's empirical behavior.
    """
    abs_cwd = str(cwd.resolve())
    return abs_cwd.replace(os.sep, "-")


def find_transcript(cwd: Path, session_uuid: str) -> Path:
    """Return the expected JSONL transcript path.

    The file may not exist yet at the moment of session start; callers
    can poll for existence.
    """
    mangled = mangle_cwd_path(cwd)
    return Path.home() / ".claude" / "projects" / mangled / f"{session_uuid}.jsonl"


def find_latest_transcript(cwd: Path, after_ts: float = 0.0) -> Path | None:
    """Return the most recently modified JSONL transcript in cwd's project dir.

    Useful for post-restore re-discovery when ``/resume`` may create a
    new session-id rather than reusing the old one.
    """
    mangled = mangle_cwd_path(cwd)
    proj_dir = Path.home() / ".claude" / "projects" / mangled
    if not proj_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in proj_dir.glob("*.jsonl") if p.stat().st_mtime > after_ts),
        key=lambda p: p.stat().st_mtime,
    )
    return candidates[-1] if candidates else None


@dataclass
class TranscriptObserver:
    """Tailing reader for one JSONL transcript file.

    Maintains a byte offset; ``read_new()`` returns newly-appended
    records since last call.
    """

    transcript_path: Path
    _offset: int = 0
    _records: list[dict] = field(default_factory=list)

    def reset(self) -> None:
        self._offset = 0
        self._records.clear()

    def rebind(self, new_path: Path) -> None:
        """Re-point the observer at a different JSONL (post-restore)."""
        self.transcript_path = new_path
        self.reset()

    def read_new(self) -> list[dict]:
        """Return any records appended since last call. Empty if none."""
        if not self.transcript_path.exists():
            return []
        new_records: list[dict] = []
        with self.transcript_path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()
            self._offset = f.tell()
        if not chunk:
            return []
        # JSONL: line-by-line decode.
        leftover = b""
        for raw in chunk.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                # incomplete final line: rewind offset
                leftover = raw
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            new_records.append(rec)
            self._records.append(rec)
        if leftover:
            self._offset -= len(leftover)
        return new_records

    def all_records(self) -> list[dict]:
        """Return all parsed records seen so far (cumulative)."""
        self.read_new()
        return list(self._records)

    def wait_for(
        self,
        predicate,
        timeout_s: float,
        poll_s: float = 0.25,
    ) -> dict | None:
        """Poll until ``predicate(records)`` returns truthy.

        Returns the truthy value (the new record that triggered) or
        None on timeout.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self.read_new()
            hit = predicate(self._records)
            if hit:
                return hit
            time.sleep(poll_s)
        return None

    def last_assistant_stop(
        self,
        terminal_only: bool = True,
        lead_only: bool = True,
    ) -> dict | None:
        """Return the most recent LEAD assistant record with a stop_reason.

        ``terminal_only=True`` (default): restrict to ``stop_reason``
        in {end_turn, max_tokens}. Intermediate ``stop_reason:
        tool_use`` records, which mean the assistant paused mid-turn
        to invoke a tool, are skipped.

        ``lead_only=True`` (default): restrict to records that are NOT
        sidechain (i.e. the lead's own messages, not an in-process
        teammate sub-agent). Empirically in CC v2.1.150 in-process
        teammates share the lead's session JSONL but carry
        ``isSidechain: true``; treating a teammate's end_turn as the
        lead's idle signal makes the harness return early before the
        lead has actually processed the new user prompt (observed
        symptom: third teammate spawn turn drops because the second
        teammate's reply triggers wait_idle to return).

        Set either flag to False to widen the filter for
        legacy/debug introspection.
        """
        terminal = {"end_turn", "max_tokens"}
        for rec in reversed(self._records):
            if rec.get("type") != "assistant":
                continue
            if lead_only and rec.get("isSidechain") is True:
                continue
            msg = rec.get("message") or {}
            sr = msg.get("stop_reason")
            if not sr:
                continue
            if terminal_only and sr not in terminal:
                continue
            return rec
        return None


def inbox_files_quiescent(inbox_paths: Iterable[Path], k_ms: int) -> bool:
    """True if no inbox file has mutated in the last K ms.

    Tracks (size, mtime_ns) tuples on the second call; first call seeds.
    """
    raise NotImplementedError(
        "Use InboxQuiescenceTracker; this stub kept for documentation."
    )


@dataclass
class InboxQuiescenceTracker:
    """Tracks inbox file (size, mtime_ns) tuples to detect quiescence."""

    inboxes_dir: Path
    last_seen: dict[str, tuple[int, int]] = field(default_factory=dict)
    last_change_ts: float = field(default_factory=time.time)

    def poll(self) -> bool:
        """Re-scan inbox files. Returns True iff anything changed since last poll."""
        changed = False
        if not self.inboxes_dir.is_dir():
            return False
        for inbox in sorted(self.inboxes_dir.glob("*.json")):
            try:
                st = inbox.stat()
            except FileNotFoundError:
                continue
            key = inbox.name
            tup = (st.st_size, st.st_mtime_ns)
            if self.last_seen.get(key) != tup:
                changed = True
                self.last_seen[key] = tup
        if changed:
            self.last_change_ts = time.time()
        return changed

    def quiescent_for(self, k_ms: int) -> bool:
        self.poll()
        return (time.time() - self.last_change_ts) * 1000.0 >= k_ms
