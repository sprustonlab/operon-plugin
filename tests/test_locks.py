"""Tests for phase 14 fix 6 per-agent serialization lock.

Coverage:
  - acquire_lock writes lockfile + sets process-local state.
  - we_hold_lock cases 1-8 per the locks.py docstring.
  - PID-alive check cross-platform.
  - Slow-fsync race defensive: pid match + start_ts mismatch ->
    self-heal.
  - atexit cleanup only removes our own entry.
  - Spruston-shared-filesystem path coverage (one explicit case
    using a directory under /groups/spruston/home/moharb/).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest


# Bootstrap shared fixtures + import targets.
@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset locks module state between every test."""
    from operon_mcp_server import locks

    locks._reset_for_tests()
    yield
    locks._reset_for_tests()


def _bootstrap_project(project_dir: Path, run_name: str = "default") -> str:
    """Create a minimal .operon/<run>/ skeleton + return coord handle uuid."""
    coord_handle = str(uuid.uuid4())
    run_dir = project_dir / ".operon" / run_name
    (run_dir / "_handles").mkdir(parents=True)
    (project_dir / ".operon" / "_active.json").write_text(
        json.dumps({"active_run_name": run_name, "set_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    (run_dir / "phase_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflow_id": "_smoke",
                "current_phase": "vision",
                "phase_started_at": "2026-01-01T00:00:00+00:00",
                "advance_history": [],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "_handles" / f"{coord_handle}.json").write_text(
        json.dumps(
            {
                "handle": coord_handle,
                "agent_name": "Coordinator",
                "role": "coordinator",
                "workflow_id": "_smoke",
                "spawned_at": "2026-01-01T00:00:00+00:00",
                "session_id": "test-session",
                "spawned_by": "test",
            }
        ),
        encoding="utf-8",
    )
    return coord_handle


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bootstrapped project in tmp_path with cwd switched to it."""
    _bootstrap_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_pid_alive_self_returns_true():
    from operon_mcp_server import locks

    assert locks._pid_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only kill probe")
def test_pid_alive_nonexistent_returns_false():
    from operon_mcp_server import locks

    # 2**31 - 1 = INT_MAX; almost certainly not a real PID on any OS.
    assert locks._pid_alive(2**31 - 1) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows conservative behavior")
def test_pid_alive_windows_returns_true_always():
    from operon_mcp_server import locks

    assert locks._pid_alive(2**31 - 1) is True


def test_pid_alive_invalid_inputs():
    from operon_mcp_server import locks

    assert locks._pid_alive(0) is False
    assert locks._pid_alive(-1) is False


def test_acquire_lock_writes_lockfile(project: Path):
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator", transport_session_id="sid-A")
    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    assert lock_path.is_file()
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["agent"] == "Coordinator"
    assert data["pid"] == os.getpid()
    assert data["transport_session_id"] == "sid-A"
    assert isinstance(data["start_ts"], str)
    assert data["schema_version"] == 1


def test_acquire_lock_sets_process_state(project: Path):
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator")
    state = locks._peek_state()
    assert state["pid"] == os.getpid()
    assert state["agent_name"] == "Coordinator"
    assert state["start_ts"] is not None
    assert state["lock_path"] is not None
    assert state["atexit_registered"] is True


def test_we_hold_lock_after_acquire(project: Path):
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator")
    assert locks.we_hold_lock("Coordinator") is True


def test_we_hold_lock_case1_pre_acquire(project: Path):
    """Case 1: never called acquire_lock -> True (test default)."""
    from operon_mcp_server import locks

    assert locks.we_hold_lock("Coordinator") is True


def test_we_hold_lock_case2_deferred_acquire(project: Path):
    """Case 2: acquire_lock saw a newer alive subprocess and deferred."""
    from operon_mcp_server import locks

    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Pre-write a lockfile from a newer, alive subprocess (us, but
    # with a deliberately-future timestamp).
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agent": "Coordinator",
                "pid": os.getpid(),
                "start_ts": "2099-01-01T00:00:00+00:00",
                "transport_session_id": None,
            }
        ),
        encoding="utf-8",
    )
    locks.acquire_lock("Coordinator")
    # Hmm: with pid=self, _pid_alive(self) returns True, so the defer
    # should fire. But the defer also checks ex_pid != pid; same pid
    # means it's "us" (idempotent), so it actually overwrites. Adjust
    # this test: use a DIFFERENT alive PID. PID 1 is init on Linux.
    if sys.platform != "win32":
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "agent": "Coordinator",
                    "pid": 1,  # init/launchd on POSIX, always alive
                    "start_ts": "2099-01-01T00:00:00+00:00",
                    "transport_session_id": None,
                }
            ),
            encoding="utf-8",
        )
        locks._reset_for_tests()
        locks.acquire_lock("Coordinator")
        # acquire deferred -> _OUR_* state cleared except agent_name
        assert locks.we_hold_lock("Coordinator") is False


def test_we_hold_lock_case5_full_match(project: Path):
    """Case 5: lockfile pid + start_ts match ours -> True."""
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator")
    # Read back the lockfile that acquire just wrote.
    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    # we_hold_lock should agree.
    assert locks.we_hold_lock("Coordinator") is True


def test_we_hold_lock_case6_start_ts_mismatch_self_heal(project: Path):
    """Case 6: pid match but start_ts mismatch -> rewrite our entry."""
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator")
    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    # Simulate a corrupted lockfile: our pid but wrong start_ts.
    corrupted = json.loads(lock_path.read_text(encoding="utf-8"))
    corrupted["start_ts"] = "2000-01-01T00:00:00+00:00"
    lock_path.write_text(json.dumps(corrupted), encoding="utf-8")

    # we_hold_lock should detect the mismatch, rewrite, return True.
    assert locks.we_hold_lock("Coordinator") is True
    # Confirm the rewrite restored our start_ts.
    restored = json.loads(lock_path.read_text(encoding="utf-8"))
    assert restored["start_ts"] == locks._peek_state()["start_ts"]
    assert restored.get("reclaimed_reason") == "start_ts_mismatch"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only PID-alive check")
def test_we_hold_lock_case7_stale_pid_takeover(project: Path):
    """Case 7: lockfile points at a dead PID -> take over."""
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator")
    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    # Overwrite with a guaranteed-dead PID.
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agent": "Coordinator",
                "pid": 2**31 - 1,
                "start_ts": "2099-01-01T00:00:00+00:00",
                "transport_session_id": None,
            }
        ),
        encoding="utf-8",
    )
    assert locks.we_hold_lock("Coordinator") is True
    # Confirm we rewrote the lockfile with our entry.
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data.get("reclaimed_reason") == "stale_pid"
    assert data.get("reclaimed_from_pid") == 2**31 - 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only PID-alive check")
def test_we_hold_lock_case8_loser(project: Path):
    """Case 8: lockfile points at a different live PID -> False."""
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator")
    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    # Overwrite with PID 1 (init, always alive) and a newer timestamp.
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agent": "Coordinator",
                "pid": 1,
                "start_ts": "2099-01-01T00:00:00+00:00",
                "transport_session_id": None,
            }
        ),
        encoding="utf-8",
    )
    assert locks.we_hold_lock("Coordinator") is False


def test_we_hold_lock_case4_lockfile_missing(project: Path):
    """Case 4: lockfile deleted between acquire and read -> lazy-claim."""
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator")
    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    lock_path.unlink()
    assert locks.we_hold_lock("Coordinator") is True
    # Lazy-claim re-wrote the lockfile.
    assert lock_path.is_file()
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data.get("reclaimed_reason") == "lockfile_missing"


def test_acquire_lock_defers_to_newer_alive(project: Path):
    """Slow-fsync sibling scenario: newer subprocess already wrote;
    our acquire defers and our state stays cleared."""
    from operon_mcp_server import locks

    if sys.platform == "win32":
        pytest.skip("POSIX-only PID-alive check")
    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agent": "Coordinator",
                "pid": 1,
                "start_ts": "2099-01-01T00:00:00+00:00",
                "transport_session_id": None,
            }
        ),
        encoding="utf-8",
    )
    locks.acquire_lock("Coordinator")
    state = locks._peek_state()
    assert state["pid"] is None  # deferred
    assert state["start_ts"] is None
    # we_hold_lock returns False from a deferred acquire.
    assert locks.we_hold_lock("Coordinator") is False
    # Lockfile is unchanged (newer subprocess's entry preserved).
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["pid"] == 1


def test_acquire_lock_idempotent_self_overwrite(project: Path):
    """Re-running acquire_lock in the same process overwrites cleanly."""
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator")
    first_state = dict(locks._peek_state())
    # Tiny sleep would help (start_ts differs in seconds); but the
    # idempotent path is "ex_pid != pid is False", which short-circuits
    # the defer. So even if start_ts is the same it overwrites our entry.
    locks.acquire_lock("Coordinator")
    second_state = locks._peek_state()
    assert second_state["pid"] == first_state["pid"] == os.getpid()


def test_release_lock_removes_our_entry(project: Path):
    """atexit handler removes the lockfile when our entry still owns it."""
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator")
    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    assert lock_path.is_file()
    locks._release_lock()
    assert not lock_path.is_file()


def test_release_lock_skips_when_overwritten(project: Path):
    """atexit handler does NOT remove the lockfile if a newer subprocess
    has overwritten our entry."""
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator")
    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    # Overwrite with someone else's entry.
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agent": "Coordinator",
                "pid": 99999,
                "start_ts": "2099-01-01T00:00:00+00:00",
                "transport_session_id": None,
            }
        ),
        encoding="utf-8",
    )
    locks._release_lock()
    # Lockfile still exists with the other entry.
    assert lock_path.is_file()
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["pid"] == 99999


def test_transport_session_id_is_diagnostic_only(project: Path):
    """Changing transport_session_id between acquire and read MUST NOT
    flip we_hold_lock. The field is for forensics, not gating logic."""
    from operon_mcp_server import locks

    locks.acquire_lock("Coordinator", transport_session_id="sid-A")
    lock_path = project / ".operon" / "default" / "_locks" / "Coordinator.json"
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    # Simulate transport reconnect: same pid, same start_ts, different sid.
    data["transport_session_id"] = "sid-B"
    lock_path.write_text(json.dumps(data), encoding="utf-8")
    # Still ours -- gate not flipped.
    assert locks.we_hold_lock("Coordinator") is True


# -- Shared-filesystem coverage ----------------------------------------


SPRUSTON_BASE = Path("/groups/spruston/home/moharb")


@pytest.mark.skipif(
    not SPRUSTON_BASE.is_dir(),
    reason="Spruston shared filesystem not mounted",
)
def test_lockfile_on_spruston_shared_filesystem():
    """The /groups/spruston path uses EFS/NFS-class semantics; verify
    the lockfile flow works there (not just tmpfs / local ext4)."""
    from operon_mcp_server import locks

    work_dir = SPRUSTON_BASE / ".tmp_operon_locks_test" / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        _bootstrap_project(work_dir)
        prev_cwd = os.getcwd()
        os.chdir(work_dir)
        try:
            locks.acquire_lock("Coordinator", transport_session_id="spruston-sid")
            lock_path = work_dir / ".operon" / "default" / "_locks" / "Coordinator.json"
            assert lock_path.is_file()
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            assert data["pid"] == os.getpid()
            assert data["agent"] == "Coordinator"
            assert locks.we_hold_lock("Coordinator") is True

            # Self-heal pass: corrupt start_ts, verify rewrite works on
            # the shared FS too (where rename semantics may differ).
            corrupted = dict(data)
            corrupted["start_ts"] = "2000-01-01T00:00:00+00:00"
            lock_path.write_text(json.dumps(corrupted), encoding="utf-8")
            assert locks.we_hold_lock("Coordinator") is True
            restored = json.loads(lock_path.read_text(encoding="utf-8"))
            assert restored.get("reclaimed_reason") == "start_ts_mismatch"
        finally:
            os.chdir(prev_cwd)
    finally:
        shutil.rmtree(work_dir.parent, ignore_errors=True)
