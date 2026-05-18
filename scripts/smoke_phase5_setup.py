#!/usr/bin/env python3
"""Phase 5 smoke-check setup helper.

Bootstraps `/tmp/test-operon-phase5/` with the Coordinator scaffolding
needed to exercise the workflow + phase engine (SPEC §11). Uses the
bundled `_smoke` workflow's 2-phase ordering (vision -> main).

After running this script the user (or this script's verifier mode)
can:
1. Activate the `_smoke` workflow via `mcp__operon__activate_workflow`
   (or use the phase_state.json this script pre-writes for
   handle-style smoke tests that don't go through claude --bg).
2. Call `mcp__operon__set_artifact_dir(path=...)` to satisfy
   `artifact-dir-ready-check`.
3. Call `mcp__operon__advance_phase` to step from vision -> main (will
   require an `elicitation/create` accept for the manual-confirm).

Cross-platform per SPEC §2: `pathlib`, `encoding="utf-8"`, `os.replace`.
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

PROJECT_ROOT = Path("/tmp/test-operon-phase5")
RUN_NAME = "phase5-run"
WORKFLOW_ID = "_smoke"
INITIAL_PHASE = "vision"
COORDINATOR_SESSION_ID = "manual-test-session-phase5"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(target: Path, payload: dict[str, Any] | list[Any]) -> None:
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


def _wipe() -> None:
    if PROJECT_ROOT.exists():
        shutil.rmtree(PROJECT_ROOT)


def main() -> int:
    _wipe()

    handle = str(uuid.uuid4())

    _atomic_write_json(
        PROJECT_ROOT / ".operon" / "_active.json",
        {"active_run_name": RUN_NAME, "set_at": _now_iso()},
    )
    _atomic_write_json(
        PROJECT_ROOT / ".operon" / RUN_NAME / "phase_state.json",
        {
            "schema_version": 1,
            "workflow_id": WORKFLOW_ID,
            "current_phase": INITIAL_PHASE,
            "phase_started_at": _now_iso(),
            "advance_history": [],
        },
    )
    _atomic_write_json(
        PROJECT_ROOT / ".operon" / RUN_NAME / "_handles" / f"{handle}.json",
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
    now = _now_iso()
    _atomic_write_json(
        PROJECT_ROOT / ".operon" / RUN_NAME / "agents.json",
        [
            {
                "name": "Coordinator",
                "role": "coordinator",
                "handle": handle,
                "session_id": COORDINATOR_SESSION_ID,
                "workflow_id": WORKFLOW_ID,
                "status": "idle",
                "spawned_at": now,
                "last_turn_at": now,
            }
        ],
    )

    print(f"wrote scaffolding under {PROJECT_ROOT}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Run these in your shell:", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"export OPERON_AGENT_HANDLE={handle}")
    print(f"cd {PROJECT_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
