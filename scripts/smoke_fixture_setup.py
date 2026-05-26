#!/usr/bin/env python3
"""Hand-rolled smoke-fixture bootstrap helper.

Phase 14 note: this script is OPTIONAL for normal use. The MCP server
auto-bootstraps a default Coordinator identity on first launch (see
`plugins/operon-plugin/src/operon_mcp_server/bootstrap.py`), so users who just want
`/project_team my_run` to work no longer need to run this. Keep this
script around for testing-with-specific-fixtures workflows where you
want a pre-built operon-session under a known path with a stable
handle UUID. The script was previously named `smoke_phase4_setup.py`.

Bootstraps a clean operon-run under `/tmp/test-operon/` so the user
(Boaz) can exercise Phase 4 inter-agent messaging without hand-rolling
shell heredocs or JSON quoting. Writes exactly the four Coordinator
bootstrap files documented in SPEC.md sections 11 and 17:

  /tmp/test-operon/
      .operon/
          _active.json
          msg-test-1/
              phase_state.json
              _handles/<coord_handle>.json
              agents.json

Speculative directories (mailbox/<agent>/inbox/, control/, acks/,
overrides/, etc.) are NOT created here -- `mailbox.write_envelope()`
lazy-creates them on first use per SPEC §6.6. Same for `processed/`
subdirs.

Cross-platform per SPEC §2: `pathlib.Path` only, `encoding="utf-8"`
on every write, `os.replace` for atomic rename (NOT `Path.rename`,
which raises on Windows when the target exists).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Project root path the smoke check uses. Hardcoded so the script is
#: zero-arg; rerunning wipes and recreates from a known clean state.
PROJECT_ROOT = Path("/tmp/test-operon")

#: Active run name written into `_active.json`. Stable so the user
#: can reason about the path without rereading the script.
RUN_NAME = "msg-test-1"

#: Workflow id the Coordinator's handle and phase state reference.
#: Matches the `_smoke` workflow shipped with operon-plugin in Phase 3.
WORKFLOW_ID = "_smoke"

#: Phase the run starts in. `vision` is the first phase of every
#: bundled workflow; `_smoke` accepts it as a no-op anchor.
INITIAL_PHASE = "vision"

#: Coordinator session id. Fixed string so the smoke check is
#: reproducible -- the Coordinator session here is NOT a real Claude
#: Code session (no `--session-id` flag was passed), so the value is
#: only used as the spawn-time anchor for any spawned-by chains.
COORDINATOR_SESSION_ID = "manual-test-session"


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 with seconds precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(target: Path, payload: dict[str, Any] | list[Any]) -> None:
    """Write `payload` atomically to `target` (temp + os.replace).

    Mirrors the SPEC §6.6 single-writer + atomic-rename pattern used by
    `roster.py` and `mailbox.py` so the bootstrap shape on disk matches
    what the production code expects to see at startup.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        # Best-effort temp cleanup; bubble the original error.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _wipe_project_root() -> None:
    """Remove the project root if present so the bootstrap is clean."""
    if PROJECT_ROOT.exists():
        shutil.rmtree(PROJECT_ROOT)


def _write_active_pointer() -> Path:
    """Write `<project>/.operon/_active.json` -> active run name."""
    path = PROJECT_ROOT / ".operon" / "_active.json"
    _atomic_write_json(
        path,
        {
            "active_run_name": RUN_NAME,
            "set_at": _now_iso(),
        },
    )
    return path


def _write_phase_state() -> Path:
    """Write `<run-dir>/phase_state.json` per SPEC §11."""
    path = PROJECT_ROOT / ".operon" / RUN_NAME / "phase_state.json"
    _atomic_write_json(
        path,
        {
            "schema_version": 1,
            "workflow_id": WORKFLOW_ID,
            "current_phase": INITIAL_PHASE,
            "phase_started_at": _now_iso(),
            "advance_history": [],
        },
    )
    return path


def _write_coord_handle(handle: str) -> Path:
    """Write `<run-dir>/_handles/<handle>.json` per SPEC §17."""
    path = PROJECT_ROOT / ".operon" / RUN_NAME / "_handles" / f"{handle}.json"
    _atomic_write_json(
        path,
        {
            "handle": handle,
            "agent_name": "Coordinator",
            "role": "coordinator",
            "workflow_id": WORKFLOW_ID,
            "spawned_at": _now_iso(),
            "session_id": COORDINATOR_SESSION_ID,
            "spawned_by": "user",
        },
    )
    return path


def _write_initial_roster(coord_handle: str) -> Path:
    """Write `<run-dir>/agents.json` with a Coordinator row.

    Phase 5 prep: workers need to be able to `message_agent("Coordinator",
    ...)` to send replies upstream. `message_agent` resolves targets via
    `agents.json`, so the Coordinator MUST appear in the roster from
    the moment the run is created. Same row schema as spawned worker
    rows (SPEC §17 `agents.json`): `name, role, handle, session_id,
    workflow_id, status, spawned_at, last_turn_at`.
    """
    now = _now_iso()
    path = PROJECT_ROOT / ".operon" / RUN_NAME / "agents.json"
    _atomic_write_json(
        path,
        [
            {
                "name": "Coordinator",
                "role": "coordinator",
                "handle": coord_handle,
                "session_id": COORDINATOR_SESSION_ID,
                "workflow_id": WORKFLOW_ID,
                "status": "idle",
                "spawned_at": now,
                "last_turn_at": now,
            }
        ],
    )
    return path


def main() -> int:
    """Wipe + bootstrap the scratch project; print the export commands."""
    _wipe_project_root()

    handle = str(uuid.uuid4())

    active_path = _write_active_pointer()
    phase_path = _write_phase_state()
    handle_path = _write_coord_handle(handle)
    roster_path = _write_initial_roster(handle)

    # Use stderr for human-readable confirmation so stdout stays clean
    # for the copy/paste-able export lines.
    print(f"wrote {active_path}", file=sys.stderr)
    print(f"wrote {phase_path}", file=sys.stderr)
    print(f"wrote {handle_path}", file=sys.stderr)
    print(f"wrote {roster_path}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Run these in your shell:", file=sys.stderr)
    print("", file=sys.stderr)

    # The export and cd commands are the script's machine-consumable
    # output. Keep them on stdout so `eval "$(smoke_phase4_setup.py)"`
    # works in a pinch.
    print(f"export OPERON_AGENT_HANDLE={handle}")
    print(f"cd {PROJECT_ROOT}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
