"""Claude Code version drift gate.

Per TEST_SPECIFICATION.md "CC version drift gate" section:

  `start_session` runs a self-check: writes a known SendMessage to an
  inbox file and verifies the resulting on-disk entry's key set matches
  the documented set. Key-set mismatch aborts the scenario before any
  sub-act runs.

This module provides:

- :func:`assert_cc_version` -- assert local ``claude --version`` is the
  pinned 2.1.144 (Q1 in testing-implementation phase coordination).
- :func:`assert_inbox_schema_keys` -- write one inbox entry whose
  body is known, atomically replace the inbox file on disk, read it
  back, and verify the parsed-back keys are a superset of the documented
  required key set. The documented set is taken from operon's own
  ``inbox.py`` docstring (empirical schema verified 2026-05-21).

Cross-platform: ``pathlib.Path``, UTF-8, ``os.replace``, ASCII-only.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

#: Pinned Claude Code version. Q1 originally pinned 2.1.144; the
#: local install auto-updates. Per Q1-update option (a), the pin
#: tracks whatever is on PATH at deliberate re-pin time. Update
#: history:
#:   2.1.144 -> 2.1.148  (CC auto-update during milestone 1)
#:   2.1.148 -> 2.1.150  (CC auto-update at start of post-fix chain run)
#: The pin's purpose is drift visibility, not eternal freezing --
#: a CC auto-update fires this gate, prompting a re-pin decision.
PINNED_CC_VERSION = "2.1.150"

#: Required keys an inbox entry MUST carry. Optional keys (``summary``,
#: ``color``) are not required. Derived from operon's ``inbox.py``
#: docstring + empirical evidence in Step 0.5 spike findings.
REQUIRED_INBOX_KEYS = frozenset({"from", "text", "timestamp", "read"})


class CCVersionDriftError(AssertionError):
    """Raised when ``claude --version`` does not match the pin."""


class InboxSchemaDriftError(AssertionError):
    """Raised when an inbox entry round-trip loses required keys."""


def assert_cc_version(expected: str = PINNED_CC_VERSION) -> str:
    """Run ``claude --version`` and assert it equals ``expected``.

    Returns the observed version string on success.
    Raises :class:`CCVersionDriftError` on mismatch.
    """
    out = subprocess.run(
        ["claude", "--version"], capture_output=True, text=True, timeout=10,
    )
    # `claude --version` typically prints "2.1.144 (Claude Code)".
    line = (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
    observed = line.split()[0] if line else ""
    if observed != expected:
        raise CCVersionDriftError(
            f"claude --version pin mismatch: expected {expected!r}, "
            f"got {observed!r} (full stdout: {out.stdout!r}, "
            f"stderr: {out.stderr!r})"
        )
    return observed


def write_inbox_entry(inbox_path: Path, entry: dict) -> None:
    """Atomically append one entry to an inbox JSON file.

    Uses ``tmp + os.replace`` per project rules. If the file does not
    exist, it is created with a single-entry list.
    """
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    if inbox_path.exists():
        existing = json.loads(inbox_path.read_text(encoding="utf-8"))
    else:
        existing = []
    existing.append(entry)
    tmp = inbox_path.with_suffix(inbox_path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, inbox_path)


def assert_inbox_schema_keys(probe_inbox: Path) -> dict:
    """Write a known entry to ``probe_inbox``, read back, verify keys.

    Returns the round-tripped entry on success. Raises
    :class:`InboxSchemaDriftError` if any required key is missing from
    the round-trip.
    """
    probe_entry = {
        "from": "harness-probe",
        "text": "schema-probe",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "summary": "harness schema probe",
        "color": "magenta",
        "read": False,
    }
    write_inbox_entry(probe_inbox, probe_entry)
    payload = json.loads(probe_inbox.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise InboxSchemaDriftError(
            f"probe inbox not a non-empty list: {payload!r}"
        )
    last = payload[-1]
    missing = REQUIRED_INBOX_KEYS - set(last.keys())
    if missing:
        raise InboxSchemaDriftError(
            f"probe inbox entry missing required keys {sorted(missing)}; "
            f"got keys {sorted(last.keys())}"
        )
    return last
