"""Per-agent serialization lock for newest-MCP-wins handoff.

Phase 14 fix 6. Background:

Claude Code's `/agents` view backgrounding (and idle-timeout
supervisor stop) spawns a FRESH process that resumes from the saved
conversation. The old MCP subprocess keeps running in the
backgrounded process; a new MCP subprocess starts in the attached
process. Both watch loops race to claim envelopes from the SAME
mailbox dir. The old MCP often wins the `os.replace` claim race --
the channel push lands in the backgrounded session's jsonl, and
the user (now in the new session) sees nothing.

Fix: each MCP subprocess writes a per-agent lockfile at startup
with its `(pid, start_ts)`. The watch loop's `_process_envelope`
reads the lockfile BEFORE claiming and bails silently when another
subprocess holds the lock. The newest subprocess to write the
lockfile wins. The loser stays alive but no-ops; if the winner
exits (its PID dies), the loser transparently takes over via the
PID-alive check.

Resolution policy in `we_hold_lock(agent)`:

  1. Lockfile missing -> we own it (lazy claim by writing).
  2. Lockfile pid + start_ts both match ours -> we own it.
  3. Lockfile pid matches ours but start_ts differs -> slow-fsync
     race or corruption; rewrite our entry, we own it.
  4. Lockfile pid is some other PID that's alive -> we are loser.
  5. Lockfile pid is dead -> stale lockfile; rewrite + take over.

`transport_session_id` is stored in the lockfile payload for
diagnostic / forensic use only. It is NOT consulted in
`we_hold_lock` -- MCP transport reconnect within the same process
must not flip ownership.

Single-writer per SPEC §6.6 is preserved: the lockfile is a
serialization hint, not a canonical store. The watch loop's
`os.replace` envelope claim remains the canonical sync primitive
for each envelope. `_pending_reply_to.json` writes are gated by
`we_hold_lock` so only the winner writes them.

Cross-platform per SPEC §2:
  - `os.replace` for atomic lockfile writes (not Path.rename).
  - `os.kill(pid, 0)` PID-alive check guarded behind
    `sys.platform != "win32"`. Windows trusts the lockfile;
    orphaned entries clear at next subprocess startup.
  - `pathlib.Path` everywhere. Explicit `encoding="utf-8"`.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

_log = logging.getLogger(__name__)

#: Subdirectory of the active run-dir that holds per-agent lockfiles.
LOCKS_DIRNAME = "_locks"

#: Lockfile schema version. Bumped on incompatible payload shape changes.
SCHEMA_VERSION = 1


def lock_file_path(agent_name: str, start: Path | None = None) -> Path:
    """Return `<run-dir>/_locks/<agent_name>.json` for the active run."""
    if not agent_name:
        raise ValueError("agent_name must be non-empty")
    return paths.active_run_dir(start) / LOCKS_DIRNAME / f"{agent_name}.json"


# -- PID-alive probe ----------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Best-effort cross-platform process-liveness check.

    POSIX: `os.kill(pid, 0)` does a sanity-only signal that returns
    cleanly if the process exists, raises ProcessLookupError if it
    doesn't. A PermissionError means the PID is alive but we cannot
    signal it (different user); treat as alive.

    Windows: stdlib has no clean equivalent without ctypes or psutil.
    Treat all PIDs as alive on Windows -- a stale lockfile from a
    dead subprocess only clears at the next subprocess startup.
    Acceptable for the `/agents`-backgrounding bug fix because the
    fresh subprocess does write a new lockfile.
    """
    if sys.platform == "win32":
        return True
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


# -- Process-local state -----------------------------------------------


#: Path to OUR lockfile (set by `acquire_lock`). The atexit handler
#: reads this to know which lockfile to clean up on graceful exit.
_OUR_LOCK_PATH: Path | None = None

#: PID written into our lockfile (`os.getpid()`). Captured at
#: acquire-lock time so the atexit cleanup can verify the lockfile
#: still points at us (and not a later subprocess that overwrote).
_OUR_PID: int | None = None

#: Start timestamp written into our lockfile. Captured at
#: acquire-lock time. Same role as `_OUR_PID`: lets the atexit
#: cleanup distinguish "our entry" from "someone else's entry".
_OUR_START_TS: str | None = None

#: Agent name we acquired the lock for. Cached so `we_hold_lock`
#: can re-acquire automatically after a run-switch (the active
#: run-dir rotates underneath, the lockfile path changes too).
_OUR_AGENT_NAME: str | None = None

#: True after atexit has registered our cleanup handler. Guards
#: against double-registration when `acquire_lock` is called more
#: than once (e.g. after a Phase 14 fix 3 watch-loop rebind).
_ATEXIT_REGISTERED: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    """Write `payload` to `target` atomically via temp + os.replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_lockfile(path: Path) -> dict[str, Any] | None:
    """Read the lockfile at `path`. Returns None on missing /
    unparseable. Best-effort: callers treat None as "no lock"."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


# -- Public API ---------------------------------------------------------


def acquire_lock(
    agent_name: str,
    transport_session_id: str | None = None,
) -> None:
    """Claim the per-agent lock at MCP subprocess startup.

    Reads the existing lockfile first; if a newer subprocess (by
    start_ts) is alive, we DEFER -- our internal `_OUR_*` state stays
    None and subsequent `we_hold_lock` calls return False from us.

    Otherwise we write our entry via atomic temp + os.replace,
    register our atexit cleanup, and set process-local state so
    subsequent `we_hold_lock` calls can verify our ownership.

    Idempotent under repeat calls in the same subprocess (e.g. after
    a Phase 14 fix 3 watch-loop rebind to a new active run-dir):
    each call re-runs the read-before-write check against the
    current run-dir's lockfile path.

    `transport_session_id` is stored for diagnostics only -- it is
    NOT consulted by `we_hold_lock` so a transport reconnect cannot
    flip ownership.
    """
    global _OUR_LOCK_PATH, _OUR_PID, _OUR_START_TS, _OUR_AGENT_NAME
    global _ATEXIT_REGISTERED

    if not agent_name:
        raise ValueError("acquire_lock: agent_name must be non-empty")

    try:
        path = lock_file_path(agent_name)
    except paths.OperonPathError as exc:
        _log.warning("acquire_lock: cannot resolve lock path: %s", exc)
        return

    pid = os.getpid()
    start_ts = _now_iso()

    # Read existing lockfile first. If a newer subprocess is alive,
    # defer (don't clobber its entry).
    existing = _read_lockfile(path)
    if existing is not None:
        ex_pid = existing.get("pid")
        ex_ts = existing.get("start_ts")
        if (
            isinstance(ex_pid, int)
            and isinstance(ex_ts, str)
            and ex_ts > start_ts  # ISO-8601 sorts lexically
            and _pid_alive(ex_pid)
            and ex_pid != pid
        ):
            _log.info(
                "acquire_lock: deferring to newer subprocess pid=%d "
                "start_ts=%s (ours would have been pid=%d start_ts=%s)",
                ex_pid,
                ex_ts,
                pid,
                start_ts,
            )
            # Clear our state so `we_hold_lock` returns False from us.
            _OUR_LOCK_PATH = None
            _OUR_PID = None
            _OUR_START_TS = None
            _OUR_AGENT_NAME = agent_name
            return

    # Either lockfile missing, or existing entry is older/dead/ours.
    # Write our entry.
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "agent": agent_name,
        "pid": pid,
        "start_ts": start_ts,
        "transport_session_id": transport_session_id,
    }
    try:
        _atomic_write_json(path, payload)
    except OSError as exc:
        _log.warning("acquire_lock: lockfile write failed: %s", exc)
        return

    _OUR_LOCK_PATH = path
    _OUR_PID = pid
    _OUR_START_TS = start_ts
    _OUR_AGENT_NAME = agent_name

    if not _ATEXIT_REGISTERED:
        atexit.register(_release_lock)
        _ATEXIT_REGISTERED = True

    _log.info(
        "acquire_lock: lockfile written agent=%s pid=%d start_ts=%s path=%s",
        agent_name,
        pid,
        start_ts,
        path,
    )


def _release_lock() -> None:
    """atexit handler: remove our lockfile iff it still points at us.

    If another subprocess overwrote our entry between acquire and exit,
    we leave that entry alone -- our cleanup must not delete a newer
    subprocess's winning lock.
    """
    if _OUR_LOCK_PATH is None or _OUR_PID is None or _OUR_START_TS is None:
        return
    data = _read_lockfile(_OUR_LOCK_PATH)
    if data is None:
        return
    if data.get("pid") == _OUR_PID and data.get("start_ts") == _OUR_START_TS:
        try:
            _OUR_LOCK_PATH.unlink()
            _log.info(
                "_release_lock: removed our lockfile at %s (pid=%d)",
                _OUR_LOCK_PATH,
                _OUR_PID,
            )
        except OSError as exc:
            _log.debug("_release_lock: unlink failed: %s", exc)


def we_hold_lock(agent_name: str) -> bool:
    """Return True iff we currently hold the per-agent lock.

    Cases (in evaluation order):
      1. We never called `acquire_lock` (test default) -> True.
      2. We called `acquire_lock` but it DEFERRED (newer subprocess
         was alive) -> our `_OUR_*` state is cleared -> False.
      3. Lockfile path can't be resolved (no active run) -> True
         (best-effort: missing infrastructure should not block).
      4. Lockfile missing -> we lazy-claim by rewriting -> True.
      5. Lockfile pid + start_ts both match ours -> True.
      6. Lockfile pid matches ours but start_ts differs (slow-fsync
         race / corruption) -> rewrite our entry to restore correct
         state -> True.
      7. Lockfile pid is dead -> stale; rewrite to take over -> True.
      8. Lockfile pid is some OTHER live PID -> False (we are loser).
    """
    # Case 1: never acquired (test scaffold, pre-bootstrap).
    if _OUR_AGENT_NAME is None:
        return True

    # Case 2: acquire ran but deferred.
    if _OUR_LOCK_PATH is None or _OUR_PID is None or _OUR_START_TS is None:
        return False

    # If agent_name differs from what we acquired for, something's
    # wrong (caller bug). Default to True to avoid blocking work.
    if agent_name != _OUR_AGENT_NAME:
        _log.warning(
            "we_hold_lock(%r) called but we acquired for %r; defaulting True",
            agent_name,
            _OUR_AGENT_NAME,
        )
        return True

    try:
        path = lock_file_path(agent_name)
    except paths.OperonPathError:
        # Case 3: no active run resolvable. Permit (defensive).
        return True

    data = _read_lockfile(path)
    if data is None:
        # Case 4: lockfile missing -> claim by rewriting our entry.
        try:
            _atomic_write_json(
                path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "agent": agent_name,
                    "pid": _OUR_PID,
                    "start_ts": _OUR_START_TS,
                    "transport_session_id": None,
                    "reclaimed_reason": "lockfile_missing",
                },
            )
        except OSError as exc:
            _log.debug("we_hold_lock: lazy-claim write failed: %s", exc)
        return True

    lock_pid = data.get("pid")
    lock_ts = data.get("start_ts")

    # Case 5: full match -> winner.
    if lock_pid == _OUR_PID and lock_ts == _OUR_START_TS:
        return True

    # Case 6: pid match but start_ts mismatch -> slow-fsync race /
    # corruption. Defensive rewrite to restore correct state.
    if lock_pid == _OUR_PID and lock_ts != _OUR_START_TS:
        _log.warning(
            "we_hold_lock: pid match but start_ts mismatch (lock=%r, "
            "ours=%r). Slow-fsync race or corruption; rewriting our "
            "entry.",
            lock_ts,
            _OUR_START_TS,
        )
        try:
            _atomic_write_json(
                path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "agent": agent_name,
                    "pid": _OUR_PID,
                    "start_ts": _OUR_START_TS,
                    "transport_session_id": None,
                    "reclaimed_reason": "start_ts_mismatch",
                },
            )
        except OSError as exc:
            _log.debug("we_hold_lock: corruption rewrite failed: %s", exc)
        return True

    # Case 7: lockfile points at a different (dead) PID -> take over.
    if isinstance(lock_pid, int) and not _pid_alive(lock_pid):
        _log.info(
            "we_hold_lock: stale lockfile pid=%d (dead); taking over "
            "as pid=%d",
            lock_pid,
            _OUR_PID,
        )
        try:
            _atomic_write_json(
                path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "agent": agent_name,
                    "pid": _OUR_PID,
                    "start_ts": _OUR_START_TS,
                    "transport_session_id": None,
                    "reclaimed_from_pid": lock_pid,
                    "reclaimed_reason": "stale_pid",
                },
            )
        except OSError as exc:
            _log.debug("we_hold_lock: takeover write failed: %s", exc)
        return True

    # Case 8: another live subprocess holds the lock. We are loser.
    return False


# -- Test helpers (production code should not call these) ---------------


def _reset_for_tests() -> None:
    """Reset module-level state. Test-only."""
    global _OUR_LOCK_PATH, _OUR_PID, _OUR_START_TS, _OUR_AGENT_NAME
    global _ATEXIT_REGISTERED
    _OUR_LOCK_PATH = None
    _OUR_PID = None
    _OUR_START_TS = None
    _OUR_AGENT_NAME = None
    _ATEXIT_REGISTERED = False


def _peek_state() -> dict[str, Any]:
    """Return a snapshot of process-local state. Test-only."""
    return {
        "lock_path": str(_OUR_LOCK_PATH) if _OUR_LOCK_PATH else None,
        "pid": _OUR_PID,
        "start_ts": _OUR_START_TS,
        "agent_name": _OUR_AGENT_NAME,
        "atexit_registered": _ATEXIT_REGISTERED,
    }
