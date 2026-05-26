"""Workflow manifest loader + phase engine.

Owns:
- Loading workflow YAML from the 3-tier loader (project > user >
  plugin) per SPEC §11 + §3.
- Reading / writing `<run-dir>/phase_state.json` atomically per SPEC
  §6.6 + §11.
- Reading / writing `<run-dir>/state.json` for `set_artifact_dir`.
- Running the advance-check protocol per SPEC §11.1.
- Computing the next phase given the workflow's declared ordering.

Cross-platform per SPEC §2: `pathlib.Path`, `encoding="utf-8"`,
`os.replace` for atomic rename, never `Path.rename`. No platform-gated
APIs in this module.

Imports from `checks/` (leaf) and `paths.py` (leaf). Imports from
`identity.py` only for the env-handle resolution used by `triggered_by`
field. Does NOT import upward from `tools/`.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import paths
from .checks import (
    Check,
    CheckDecl,
    CheckResult,
    build_check,
    known_check_types,
    parse_decl,
)

_log = logging.getLogger(__name__)

#: Schema version stamped into newly-created `phase_state.json` /
#: `state.json` files. Older files without this field are accepted on
#: read (treated as v1) for forward-compat with hand-edited fixtures.
SCHEMA_VERSION = 1

#: Filename Claude Code uses to expose the plugin install root via env
#: (`.mcp.json` `${CLAUDE_PLUGIN_ROOT}/bin/operon-mcp-server`).
ENV_PLUGIN_ROOT_VAR = "CLAUDE_PLUGIN_ROOT"

#: Tier-priority order per SPEC §3 + §11: project overrides user
#: overrides plugin. The loader returns the first tier that has a
#: workflow file with the given id.
TIER_ORDER = ("project", "user", "plugin")


class WorkflowError(RuntimeError):
    """Raised on manifest-load failures or phase-engine errors."""


# -- data types ---------------------------------------------------------


@dataclass(frozen=True)
class PhaseDecl:
    """One phase entry from a workflow manifest.

    Parsed from a manifest mapping like::

        - id: vision
          advance_checks:
            - type: manual-confirm
              prompt: "Vision approved by user?"
    """

    id: str
    advance_checks: tuple[CheckDecl, ...] = ()
    # `file` is the role-prompt filename (e.g. <role>/<file>.md); not
    # used by the phase engine itself but retained from the manifest
    # for downstream consumers (Phase 6's rules engine).
    file: str | None = None


@dataclass(frozen=True)
class WorkflowDecl:
    """Parsed workflow manifest."""

    workflow_id: str
    phases: tuple[PhaseDecl, ...]
    # Source tier the manifest was loaded from (for diagnostics).
    tier: str
    # Filesystem path to the manifest file (for diagnostics + base_dir
    # resolution of relative check paths).
    source_path: Path

    @property
    def first_phase_id(self) -> str | None:
        return self.phases[0].id if self.phases else None

    def phase(self, phase_id: str) -> PhaseDecl | None:
        for p in self.phases:
            if p.id == phase_id:
                return p
        return None

    def next_phase_after(self, phase_id: str) -> str | None:
        """Return the next phase id in declaration order, or None at end."""
        for i, p in enumerate(self.phases):
            if p.id == phase_id and i + 1 < len(self.phases):
                return self.phases[i + 1].id
        return None


# -- manifest loader (3-tier) -------------------------------------------


def _tier_workflow_dirs(workflow_id: str, start: Path | None = None) -> list[tuple[str, Path]]:
    """Return [(tier_name, candidate_dir), ...] in project>user>plugin order.

    A "candidate_dir" is `<tier_root>/workflows/<workflow_id>/`. The
    loader probes each candidate for a manifest YAML.

    The project tier is rooted at the active `.operon/` ancestor (the
    Coordinator's MCP subprocess cwd). Missing tiers (e.g. no
    CLAUDE_PLUGIN_ROOT env) are simply omitted from the result.
    """
    candidates: list[tuple[str, Path]] = []

    try:
        project_root = paths.project_root(start)
    except paths.OperonPathError:
        project_root = None
    if project_root is not None:
        candidates.append(
            ("project", project_root / paths.OPERON_DIRNAME / "workflows" / workflow_id)
        )

    candidates.append(
        ("user", Path.home() / paths.OPERON_DIRNAME / "workflows" / workflow_id)
    )

    plugin_root = os.environ.get(ENV_PLUGIN_ROOT_VAR, "").strip()
    if plugin_root:
        candidates.append(
            ("plugin", Path(plugin_root) / "workflows" / workflow_id)
        )

    return candidates


def _find_manifest_file(workflow_dir: Path, workflow_id: str) -> Path | None:
    """Return the manifest YAML inside `workflow_dir`, or None if absent.

    Two filenames are accepted, in priority order: `<workflow_id>.yaml`
    (the bundled `project_team/project_team.yaml` convention) then
    `phases.yaml` (the convention for minimal manifests).
    """
    for candidate in (workflow_dir / f"{workflow_id}.yaml", workflow_dir / "phases.yaml"):
        if candidate.is_file():
            return candidate
    return None


def _parse_manifest(text: str, source: Path) -> tuple[str, tuple[PhaseDecl, ...]]:
    """Parse a manifest YAML body. Returns (workflow_id, phases).

    Accepts the manifest shape::

        workflow_id: <id>
        phases:
          - id: <phase>
            file: <role-file>
            advance_checks:
              - type: file-exists-check
                path: ...

    If `workflow_id` is absent the loader uses the parent directory
    name (the behavior for hand-rolled workflows).
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"Failed to parse manifest '{source}': {exc}") from exc
    if data is None or not isinstance(data, dict):
        raise WorkflowError(f"Manifest '{source}' must be a YAML mapping at top level.")

    workflow_id_raw = data.get("workflow_id")
    workflow_id = (
        workflow_id_raw if isinstance(workflow_id_raw, str) and workflow_id_raw
        else source.parent.name
    )

    raw_phases = data.get("phases")
    if not isinstance(raw_phases, list):
        raise WorkflowError(
            f"Manifest '{source}' missing 'phases' list at top level."
        )

    phases: list[PhaseDecl] = []
    for entry in raw_phases:
        if not isinstance(entry, dict):
            raise WorkflowError(
                f"Manifest '{source}' phase entry must be a mapping; "
                f"got {type(entry).__name__}."
            )
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid:
            raise WorkflowError(
                f"Manifest '{source}' phase entry missing non-empty 'id'."
            )
        raw_checks = entry.get("advance_checks") or []
        if not isinstance(raw_checks, list):
            raise WorkflowError(
                f"Manifest '{source}' phase {pid!r} 'advance_checks' "
                "must be a list."
            )
        decls: list[CheckDecl] = []
        for ck in raw_checks:
            try:
                decls.append(parse_decl(ck))
            except ValueError as exc:
                raise WorkflowError(
                    f"Manifest '{source}' phase {pid!r}: {exc}"
                ) from exc
            if decls[-1].type not in known_check_types():
                raise WorkflowError(
                    f"Manifest '{source}' phase {pid!r}: unknown check "
                    f"type {decls[-1].type!r}. Known: "
                    f"{sorted(known_check_types())}"
                )
        phases.append(
            PhaseDecl(
                id=pid,
                advance_checks=tuple(decls),
                file=entry.get("file"),
            )
        )

    if not phases:
        raise WorkflowError(f"Manifest '{source}' declares no phases.")

    return workflow_id, tuple(phases)


def load_workflow(workflow_id: str, start: Path | None = None) -> WorkflowDecl:
    """Load workflow manifest via the 3-tier loader.

    Returns the first tier (project > user > plugin) that has a
    parseable manifest with this id. Raises `WorkflowError` if no
    tier has the workflow.
    """
    for tier_name, candidate_dir in _tier_workflow_dirs(workflow_id, start):
        manifest = _find_manifest_file(candidate_dir, workflow_id)
        if manifest is None:
            continue
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(
                f"Failed to read manifest '{manifest}': {exc}"
            ) from exc
        manifest_id, phases = _parse_manifest(text, manifest)
        return WorkflowDecl(
            workflow_id=manifest_id,
            phases=phases,
            tier=tier_name,
            source_path=manifest,
        )
    raise WorkflowError(
        f"Workflow {workflow_id!r} not found in any tier (project > user > plugin). "
        f"Searched: {[str(p) for _, p in _tier_workflow_dirs(workflow_id, start)]}"
    )


# -- phase_state.json + state.json I/O ----------------------------------


STATE_FILENAME = "state.json"


def state_file(start: Path | None = None) -> Path:
    """Return `<run-dir>/state.json`."""
    return paths.active_run_dir(start) / STATE_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> Path:
    """Write `payload` atomically via temp + os.replace (SPEC §6.6)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(
        f"{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise WorkflowError(f"Failed to write '{target}': {exc}") from exc
    return target


def read_phase_state(start: Path | None = None) -> dict[str, Any]:
    """Read `<run-dir>/phase_state.json`. Raises `WorkflowError` on absence."""
    try:
        path = paths.phase_state_file(start)
    except paths.OperonPathError as exc:
        raise WorkflowError(str(exc)) from exc
    if not path.is_file():
        raise WorkflowError(
            f"phase_state.json not found at '{path}'. Has the workflow "
            "been activated for this project? (activate_workflow)"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Failed to read '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"phase_state.json '{path}' must be a JSON object.")
    return data


def read_state(start: Path | None = None) -> dict[str, Any] | None:
    """Read `<run-dir>/state.json`. Returns None if missing.

    Differs from `read_phase_state` in that absence is non-fatal:
    `set_artifact_dir` creates this file lazily, so any read path
    that fires before the first call should see None and degrade
    gracefully.
    """
    try:
        path = state_file(start)
    except paths.OperonPathError as exc:
        raise WorkflowError(str(exc)) from exc
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Failed to read '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"state.json '{path}' must be a JSON object.")
    return data


def write_initial_phase_state(
    workflow_id: str,
    current_phase: str,
    start: Path | None = None,
) -> Path:
    """Create `<run-dir>/phase_state.json` for a freshly-activated run."""
    try:
        path = paths.phase_state_file(start)
    except paths.OperonPathError as exc:
        raise WorkflowError(str(exc)) from exc
    payload = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "current_phase": current_phase,
        "phase_started_at": _now_iso(),
        "advance_history": [],
    }
    return _atomic_write_json(path, payload)


def write_state(
    *,
    run_name: str,
    artifact_dir: str,
    start: Path | None = None,
) -> Path:
    """Create or replace `<run-dir>/state.json` for `set_artifact_dir`."""
    try:
        path = state_file(start)
    except paths.OperonPathError as exc:
        raise WorkflowError(str(exc)) from exc
    # Preserve created_at if the file already exists (so re-calls don't
    # rewrite the original creation timestamp).
    existing = read_state(start) or {}
    created_at = existing.get("created_at") or _now_iso()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_name": run_name,
        "artifact_dir": artifact_dir,
        "created_at": created_at,
    }
    return _atomic_write_json(path, payload)


# -- advance protocol (SPEC §11.1) --------------------------------------


@dataclass(frozen=True)
class AdvanceCheckOutcome:
    """One check's contribution to the advance decision."""

    check_type: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class AdvanceOutcome:
    """Aggregate result of an `advance_phase` invocation."""

    advanced: bool
    from_phase: str
    to_phase: str | None
    outcomes: tuple[AdvanceCheckOutcome, ...]
    history_entry: dict[str, Any] | None = None


def _expand_artifact_dir(value: Any, artifact_dir: str | None) -> Any:
    """Substitute `${ARTIFACT_DIR}` in string params with the value from
    state.json (`set_artifact_dir`).

    Recurses into lists. Strings with no placeholder pass through
    unchanged. If `artifact_dir` is None, placeholders are left as-is
    -- the artifact-dir-ready-check should have failed earlier in the
    advance order, so we never reach a substituted file-exists-check
    with no artifact_dir bound.
    """
    if artifact_dir is None:
        return value
    if isinstance(value, str):
        return value.replace("${ARTIFACT_DIR}", artifact_dir)
    if isinstance(value, list):
        return [_expand_artifact_dir(v, artifact_dir) for v in value]
    return value


def _inject_seam_params(
    decls: tuple[CheckDecl, ...],
    *,
    workflow_root: Path,
    state_path: Path,
    elicit,
) -> list[Check]:
    """Hydrate Decls with engine-tier params + build executable Checks.

    `workflow_root` becomes the `base_dir` for file-* checks and the
    `cwd` for command-output-check. `state_path` is the `state.json`
    target for `artifact-dir-ready-check`. `elicit` is the closure
    that issues `elicitation/create` for `manual-confirm`.

    Also substitutes `${ARTIFACT_DIR}` in `path`/`paths` params from the
    current run's state.json so workflows can pin advance checks to
    artifact-dir-relative paths.
    """
    # Best-effort read of state.json for ${ARTIFACT_DIR} expansion.
    # If state.json is missing or artifact_dir unset, placeholders are
    # left literal -- artifact-dir-ready-check earlier in the chain
    # should have failed first.
    state_data = None
    try:
        state_data = read_state(state_path.parent)
    except Exception:
        state_data = None
    artifact_dir = None
    if isinstance(state_data, dict):
        ad = state_data.get("artifact_dir")
        if isinstance(ad, str) and ad:
            artifact_dir = ad

    checks: list[Check] = []
    for decl in decls:
        params = dict(decl.params)
        if decl.type in {"file-exists-check", "file-content-check"}:
            params.setdefault("base_dir", str(workflow_root))
            for k in ("path", "paths"):
                if k in params:
                    params[k] = _expand_artifact_dir(params[k], artifact_dir)
        if decl.type == "command-output-check":
            params.setdefault("cwd", str(workflow_root))
        if decl.type == "manual-confirm":
            params["_elicit"] = elicit
        if decl.type == "artifact-dir-ready-check":
            params["state_file"] = state_path
        checks.append(build_check(CheckDecl(decl.type, params, decl.on_failure)))
    return checks


async def run_advance_checks(
    decls: tuple[CheckDecl, ...],
    *,
    workflow_root: Path,
    state_path: Path,
    elicit,
) -> list[AdvanceCheckOutcome]:
    """Run advance checks in order, short-circuit on first failure.

    Returns one `AdvanceCheckOutcome` per check that ACTUALLY RAN.
    Short-circuit per SPEC §11.1 step 1: if check N fails, checks
    N+1..end are not run.
    """
    checks = _inject_seam_params(
        decls, workflow_root=workflow_root, state_path=state_path, elicit=elicit
    )
    outcomes: list[AdvanceCheckOutcome] = []
    for decl, chk in zip(decls, checks):
        try:
            result = await chk.check()
        except Exception as exc:  # pragma: no cover (defensive)
            result = CheckResult(passed=False, evidence=f"Check raised: {exc}")
        outcomes.append(
            AdvanceCheckOutcome(
                check_type=decl.type, passed=result.passed, evidence=result.evidence
            )
        )
        if not result.passed:
            break
    return outcomes


def commit_advance(
    *,
    workflow_id: str,
    current_phase: str,
    next_phase: str,
    triggered_by: str | None,
    start: Path | None = None,
) -> dict[str, Any]:
    """Atomically rewrite `phase_state.json` for a successful advance.

    Returns the new `advance_history` entry just appended. Single
    writer (Coordinator) per SPEC §6.6, atomic via temp+os.replace,
    no CAS.
    """
    state = read_phase_state(start)
    history = state.get("advance_history") or []
    if not isinstance(history, list):
        raise WorkflowError(
            "phase_state.json `advance_history` corrupt (not a list); refusing to advance."
        )

    now = _now_iso()
    entry: dict[str, Any] = {
        "from": current_phase,
        "to": next_phase,
        "at": now,
    }
    if triggered_by:
        entry["triggered_by"] = triggered_by

    new_state = {
        "schema_version": state.get("schema_version", SCHEMA_VERSION),
        "workflow_id": workflow_id,
        "current_phase": next_phase,
        "phase_started_at": now,
        "advance_history": [*history, entry],
    }
    try:
        path = paths.phase_state_file(start)
    except paths.OperonPathError as exc:
        raise WorkflowError(str(exc)) from exc
    _atomic_write_json(path, new_state)
    return entry


# -- caller identity for `triggered_by` ---------------------------------


def resolve_triggered_by() -> str | None:
    """Best-effort lookup of the caller's agent_name via env handle.

    Used to stamp `advance_history` entries with the Agent who
    triggered the advance. Returns None on any failure so `advance_phase`
    never refuses to run because identity resolution glitched.
    """
    from . import identity  # local import to keep this module lean

    handle = identity.read_env_handle()
    if handle is None:
        return None
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError:
        return None
    if record is None:
        return None
    name = record.get("agent_name")
    return name if isinstance(name, str) and name else None


# -- destructive-flow helpers (Phase 6.5) -------------------------------


def list_run_dirs(start: Path | None = None) -> list[Path]:
    """Return all `<project>/.operon/<run-name>/` directories.

    Excludes the `_active.json` pointer and any non-directory entries.
    Used by `list_operon_sessions` to enumerate every run that has on-
    disk state, whether active or dormant.
    """
    try:
        op_dir = paths.operon_dir(start)
    except paths.OperonPathError:
        return []
    if not op_dir.is_dir():
        return []
    return sorted(
        p for p in op_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def is_bg_session_alive(daemon_short: str) -> bool:
    """Return True iff `~/.claude/jobs/<short>/state.json` reports the
    session is in a non-terminal state.

    Terminal states observed empirically on Claude Code 2.1.143:
    `"stopped"`, `"complete"`, `"failed"`, `"cancelled"`. Note that
    `"done"` is NOT terminal -- a "done" session has finished its
    initial prompt but is still tracked by the daemon and can be
    resumed; for destructive-close purposes we treat it as alive
    (the bg pty-host is still up and `claude stop` still applies).

    Known lag: when `claude stop` is invoked, the bg processes
    terminate immediately (`ps` confirms) but `state.json.state` can
    take 5-10 seconds to update to "stopped". Callers that need
    real-time post-kill confirmation should poll `ps` instead. For
    the destructive-close pre-flight check (which fires BEFORE the
    kill), state.json lag is irrelevant.

    Missing or unreadable state.json => not alive (the daemon prunes
    state.json for sessions it has fully released).
    """
    if not daemon_short:
        return False
    job_state = Path.home() / ".claude" / "jobs" / daemon_short / "state.json"
    if not job_state.is_file():
        return False
    try:
        data = json.loads(job_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    state = data.get("state")
    if not isinstance(state, str):
        return False
    return state not in {"stopped", "complete", "failed", "cancelled"}


def alive_agents_in_run(run_dir: Path) -> list[dict[str, Any]]:
    """Return agent rows from `<run-dir>/agents.json` whose bg session
    is alive per `is_bg_session_alive`.

    Coordinator-role rows are EXCLUDED because they represent the
    foreground session that the user is talking to right now -- killing
    that would brick the user's session. Only worker-style rows are
    considered for destructive close.
    """
    roster_path = run_dir / "agents.json"
    if not roster_path.is_file():
        return []
    try:
        rows = json.loads(roster_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    alive: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("role") == "coordinator":
            continue
        sid = r.get("session_id", "")
        if not isinstance(sid, str) or not sid:
            continue
        short = sid.split("-", 1)[0]
        if is_bg_session_alive(short):
            row = dict(r)
            row["_daemon_short"] = short
            alive.append(row)
    return alive


def kill_bg_session(daemon_short: str, timeout_s: float = 15.0) -> dict[str, Any]:
    """Invoke `claude stop <daemon_short>`. Returns a result dict for
    inclusion in tool responses; never raises.

    Phase 5 carryover #4 established that `claude stop` takes the
    8-char daemonShort, not the full session_id UUID.
    """
    import subprocess
    res: dict[str, Any] = {"daemon_short": daemon_short}
    try:
        proc = subprocess.run(
            ["claude", "stop", daemon_short],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout_s, check=False,
        )
        res["returncode"] = proc.returncode
        if proc.stdout:
            res["stdout"] = proc.stdout.strip()[:200]
        if proc.stderr:
            res["stderr"] = proc.stderr.strip()[:200]
    except FileNotFoundError:
        res["error"] = "claude binary not on PATH"
    except subprocess.TimeoutExpired:
        res["error"] = f"claude stop timed out after {timeout_s}s"
    except OSError as exc:
        res["error"] = f"claude stop failed: {exc}"
    return res


def write_active_pointer(
    operon_dir: Path, run_name: str
) -> Path:
    """Atomically rewrite `<.operon>/_active.json` to point at `run_name`.

    Shared by both `activate_workflow` (new-run creation) and
    `restore_operon_session` (operon-session switch) per SPEC §17 row
    on `_active.json`. Single-writer attribution: whichever tool is
    running here.
    """
    active_path = operon_dir / paths.ACTIVE_POINTER_FILENAME
    payload = {"active_run_name": run_name, "set_at": _now_iso()}
    return _atomic_write_json(active_path, payload)

