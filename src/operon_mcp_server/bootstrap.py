"""Auto-bootstrap a default Coordinator identity at MCP server startup.

Phase 14: eliminate the manual `smoke_phase4_setup.py` + env export
prep step. When the operon MCP subprocess starts in a project that
has no existing operon identity context (no `OPERON_AGENT_HANDLE`,
no `.operon/_active.json`), this module creates a minimal
bootstrap run-dir on disk and caches the new Coordinator handle in
the process-local identity cache. From the user's POV, launching
`claude` in a fresh project lets them call `/project_team my_run`
(or any other slash command) immediately.

Resolution order (per Phase 14 dispatch):

  1. `OPERON_AGENT_HANDLE` env var is set -> NO bootstrap; the
     subprocess was spawned via `spawn_agent` (or the user manually
     bound a fixture). Existing behavior preserved.
  2. `<project>/.operon/_active.json` exists AND the active run-dir
     has a single coordinator handle file -> adopt it (cache in
     memory; no filesystem mutation). Restart-idempotent.
  3. Otherwise -> create a fresh `<project>/.operon/<default>/`
     with a new Coordinator UUID, write the four canonical
     bootstrap files, cache the handle.

Failure to bootstrap is non-fatal: the subprocess can still serve
`whoami` (which returns IdentityError on call). Errors are logged
to OPERON_DEBUG stderr but do NOT block MCP startup.

Cross-platform per SPEC §2: pathlib.Path, encoding="utf-8" on every
write, os.replace for atomic rename (NOT Path.rename, which fails
on Windows when the target exists).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import identity, paths

_log = logging.getLogger(__name__)

#: Default run name written into `_active.json` when bootstrap creates a
#: fresh operon-session. Overrideable via `OPERON_DEFAULT_RUN_NAME` env
#: so users can pin a different initial run without hand-rolling files.
DEFAULT_RUN_NAME = "default"
ENV_DEFAULT_RUN_NAME = "OPERON_DEFAULT_RUN_NAME"

#: Synthetic workflow_id stamped into the bootstrap phase_state.json. NOT
#: a real workflow on disk -- the user is expected to call
#: `activate_workflow(workflow_id="<real>", run_name="<real>")` to
#: replace the bootstrap state with a usable workflow before doing
#: meaningful work. Tools that don't resolve the workflow_id (whoami,
#: get_phase, message_agent) keep working with the synthetic value.
BOOTSTRAP_WORKFLOW_ID = "_bootstrap"
BOOTSTRAP_PHASE = "bootstrap"

#: Agent name + role written into the bootstrap handle + roster. Same
#: canonical values claudechic uses for its TUI's auto-spawned
#: Coordinator agent.
BOOTSTRAP_AGENT_NAME = "Coordinator"
BOOTSTRAP_ROLE = "coordinator"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(target: Path, payload: Any) -> None:
    """Write `payload` atomically to `target` (temp + os.replace).

    Mirrors the SPEC §6.6 single-writer + atomic-rename pattern used
    elsewhere in the codebase. Best-effort cleanup of the temp file on
    failure.
    """
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


def _resolve_run_name() -> str:
    """Return the bootstrap run name, honoring `OPERON_DEFAULT_RUN_NAME`."""
    override = os.environ.get(ENV_DEFAULT_RUN_NAME, "").strip()
    return override if override else DEFAULT_RUN_NAME


def _resolve_session_id() -> str:
    """Resolve a Claude Code session_id for the bootstrap Coordinator.

    Phase 14: Claude Code does NOT inject the session_id into the MCP
    subprocess env at launch (unlike `spawn_agent`'s explicit
    propagation). We use a synthetic `bootstrap-<short-uuid>` so the
    handle record schema is satisfied and downstream tools that need a
    session_id for their own bookkeeping (e.g. nudge audit rows) have a
    non-empty value. The string is opaque to identity resolution.
    """
    return f"bootstrap-{uuid.uuid4().hex[:8]}"


def _discover_existing_coordinator(start: Path) -> str | None:
    """Scan `<active-run>/_handles/` for a single coordinator handle.

    Returns the handle UUID if exactly one coordinator-role handle file
    exists. Returns None for 0 or >1 (the ambiguous case is left for
    the bootstrap-from-scratch branch).
    """
    try:
        h_dir = paths.handles_dir(start)
    except paths.OperonPathError:
        return None
    if not h_dir.is_dir():
        return None
    coord_handles: list[str] = []
    for path in h_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("role") != BOOTSTRAP_ROLE:
            continue
        handle = data.get("handle")
        if isinstance(handle, str) and handle:
            coord_handles.append(handle)
    if len(coord_handles) == 1:
        return coord_handles[0]
    return None


def _bootstrap_fresh(project_root: Path) -> str:
    """Create `<project>/.operon/<default>/` with the canonical files.

    Writes (atomic via temp + os.replace):

      - `<project>/.operon/_active.json`           {active_run_name, set_at}
      - `<run-dir>/_handles/<uuid>.json`           SPEC §17 handle schema
      - `<run-dir>/phase_state.json`               SPEC §11 phase schema
      - `<run-dir>/agents.json`                    SPEC §17 roster row

    Creates `<run-dir>/mailbox/` as an empty dir (mailbox.py
    lazy-creates per-agent subdirs on first envelope write).

    Returns the newly minted handle UUID. Raises OSError on
    irrecoverable filesystem failure -- caller is expected to catch
    and degrade to "identity unbound".
    """
    run_name = _resolve_run_name()
    handle = str(uuid.uuid4())
    session_id = _resolve_session_id()
    now = _now_iso()

    operon_dir = project_root / paths.OPERON_DIRNAME
    run_dir = operon_dir / run_name
    handles_dir = run_dir / paths.HANDLES_DIRNAME
    mailbox_dir = run_dir / "mailbox"

    handles_dir.mkdir(parents=True, exist_ok=True)
    mailbox_dir.mkdir(parents=True, exist_ok=True)

    # 1. Active-run pointer.
    _atomic_write_json(
        operon_dir / paths.ACTIVE_POINTER_FILENAME,
        {"active_run_name": run_name, "set_at": now},
    )
    # 2. Handle file.
    _atomic_write_json(
        handles_dir / f"{handle}.json",
        {
            "handle": handle,
            "agent_name": BOOTSTRAP_AGENT_NAME,
            "role": BOOTSTRAP_ROLE,
            "workflow_id": BOOTSTRAP_WORKFLOW_ID,
            "spawned_at": now,
            "session_id": session_id,
            "spawned_by": "bootstrap",
        },
    )
    # 3. Phase state.
    _atomic_write_json(
        run_dir / "phase_state.json",
        {
            "schema_version": 1,
            "workflow_id": BOOTSTRAP_WORKFLOW_ID,
            "current_phase": BOOTSTRAP_PHASE,
            "phase_started_at": now,
            "advance_history": [],
        },
    )
    # 4. Roster row.
    _atomic_write_json(
        run_dir / "agents.json",
        [
            {
                "name": BOOTSTRAP_AGENT_NAME,
                "role": BOOTSTRAP_ROLE,
                "handle": handle,
                "session_id": session_id,
                "workflow_id": BOOTSTRAP_WORKFLOW_ID,
                "status": "idle",
                "spawned_at": now,
                "last_turn_at": now,
            }
        ],
    )
    return handle


def auto_bootstrap_if_needed(start: Path | None = None) -> str | None:
    """Resolve or create identity context per Phase 14 dispatch.

    Returns the resolved Coordinator handle UUID, or None on failure.
    Sets the process-local handle cache (`identity._set_cached_handle`)
    so subsequent calls to `identity.read_env_handle()` resolve to the
    same value without re-running the lookup.

    Behavior:
      1. If `OPERON_AGENT_HANDLE` env is set, return it. NO bootstrap.
         Existing fixture / spawn_agent flows preserved verbatim.
      2. If the current project has an active run with EXACTLY ONE
         coordinator handle file, adopt that handle (cache only; no
         filesystem mutation).
      3. Otherwise create a fresh `<project>/.operon/<default>/` and
         cache the new handle.

    Failures are logged at WARNING level and return None -- the MCP
    subprocess stays alive and serves `whoami` (which surfaces
    IdentityError to the caller).
    """
    # 1. Env-set: nothing to do.
    env_handle = os.environ.get(identity.ENV_HANDLE_VAR)
    if env_handle:
        _log.debug("bootstrap: env handle already set; skipping")
        return env_handle

    here = Path(start) if start is not None else Path.cwd()
    here = here.resolve()

    # 2. Existing project with a discoverable coordinator handle?
    try:
        existing = _discover_existing_coordinator(here)
    except Exception as exc:
        _log.warning("bootstrap: discover failed (%s); will create fresh", exc)
        existing = None
    if existing is not None:
        _log.info("bootstrap: adopted existing coordinator handle=%s", existing)
        identity._set_cached_handle(existing)
        return existing

    # 3. Fresh bootstrap.
    # Resolve project root: nearest ancestor with `.operon/`, else `here`.
    try:
        project_root = paths.project_root(here)
    except paths.OperonPathError:
        project_root = here
    try:
        new_handle = _bootstrap_fresh(project_root)
    except OSError as exc:
        _log.warning(
            "bootstrap: filesystem write failed under %s: %s; "
            "subprocess will start without identity context",
            project_root,
            exc,
        )
        return None
    _log.info(
        "bootstrap: created default operon-session at %s/.operon/%s "
        "with Coordinator handle=%s",
        project_root,
        _resolve_run_name(),
        new_handle,
    )
    identity._set_cached_handle(new_handle)
    return new_handle
