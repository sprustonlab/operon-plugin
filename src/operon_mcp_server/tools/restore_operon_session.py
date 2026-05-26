"""`restore_operon_session` MCP tool (Coordinator-only).

Land 5 of the Agent Teams Pivot. Boaz's empirical 2026-05-21
finding: ``/resume`` of a
post-Land-4 operon-session does NOT auto-respawn the teammates
that were alive at suspend time -- Shift+Down shows no
composability teammate. Land 5 wires operon-driven RESTORE on
top of WA1 (Section 5.1) so a previously-activated operon team
project can be brought back to a workable state.

User-side framing (Boaz, 2026-05-21): "I want a RESTORE not a
resume, it should be for an activated team project only."
Translation (with the Land 5 v2 amendment, 2026-05-21):

  * Operon owns the operation; this MCP tool is the entry point.
  * REQUIRED precondition: ``<cwd>/.operon/<run>/phase_state.json``
    exists -- the proof an operon workflow was activated.
  * OPTIONAL precondition: ``~/.claude/teams/<run>/config.json``.
    When present, the response carries a ``members_to_respawn``
    manifest derived from the team's ``members[]``. When absent,
    restore still succeeds as a lead-only operon-session and
    surfaces a ``suggested_members`` list (derived from prior
    sidechain transcripts on disk) plus a ``call TeamCreate
    first`` lead-instructions hint.
  * Generic ``/resume`` of a non-operon Claude Code session is
    NOT in this tool's scope (the Anthropic runtime handles
    lead-side ``/resume``; operon's job is the teammate respawn
    manifest).

Two entry modes (the WA1 PreToolUse hook in
``plugins/operon-plugin/hooks/pretooluse.py`` is the WA1
substitute for the SDK ``resume=session_id`` parameter):

  * ``run_name`` supplied: skip the picker, validate the
    REQUIRED precondition, swap the active pointer, branch on
    team-config presence to build the response.
  * ``run_name`` omitted: discover candidate runs via
    ``list_operon_sessions._do_list()`` filtered to runs whose
    phase_state.json is present (team config not required),
    surface a single-select picker elicitation (each entry
    tagged ``team=present|absent``), then proceed.

This tool does NOT spawn teammates itself (operon ships no
lifecycle control over teammates per v2.9 section 6.1). It
returns a structured manifest the lead's LLM uses to call
``Agent(subagent_type=..., team_name=..., name=...)`` for each
teammate to respawn. WA1 transcript replay happens inside the
PreToolUse hook on the ``Agent`` tool.

Cross-platform per project rules: ``pathlib.Path``,
``encoding="utf-8"``, ``os.replace`` for atomic rename,
ASCII-only.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

from .. import elicit, identity, paths, workflow
from . import list_operon_sessions as list_tool
from . import spawn_agent as spawn_agent_tool

_log = logging.getLogger(__name__)

TOOL_NAME = "restore_operon_session"

#: Operon's reserved team-member name (mirrors
#: ``subagent_install.OPERON_MEMBER_NAME``; kept local so this module
#: has no upward import into the install path).
_OPERON_MEMBER_NAME = "operon"

#: Lead's name in Anthropic's TeamCreate-shaped config. Empirically
#: ``team-lead``; never participates in the respawn manifest because
#: the lead is the user's foreground claude process and runtime
#: ``/resume`` brings it back without operon's help.
_LEAD_MEMBER_NAME = "team-lead"

#: Anthropic team-config root. Local copy of the constant from
#: ``subagent_install.TEAMS_DIR`` -- duplicated to avoid the import
#: dependency on the install module from a Land-5 read path.
_TEAMS_DIR = Path.home() / ".claude" / "teams"


INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_name": {
            "type": "string",
            "description": (
                "Name of the operon workflow to restore. If "
                "omitted, the tool lists existing runs (filtered "
                "to those with a phase_state.json) and issues a "
                "picker that marks each candidate "
                "team=present|absent."
            ),
        },
    },
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Restore a previously-activated operon workflow. "
            "Required precondition: <cwd>/.operon/<run_name>/"
            "phase_state.json exists (proof an operon workflow was "
            "activated). The Anthropic team config at "
            "~/.claude/teams/<run_name>/config.json is OPTIONAL: when "
            "present, the response includes the teammate-respawn "
            "manifest; when absent, restore still succeeds as a "
            "lead-only operon-session and surfaces a "
            "'call TeamCreate first' lead_instructions hint. Sets "
            "<cwd>/.operon/_active.json to point at the chosen run. "
            "WA1 transcript replay (v2.9 section 5.1) is delivered "
            "by the PreToolUse hook on Agent. With no run_name "
            "argument, surfaces a picker. Coordinator-only."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class RestoreOperonSessionError(RuntimeError):
    """Raised on validation or write failures; becomes a tool error."""


def _require_coordinator() -> dict[str, Any]:
    try:
        record = spawn_agent_tool._require_coordinator()
    except spawn_agent_tool.SpawnAgentError as exc:
        raise RestoreOperonSessionError(str(exc)) from exc
    return record


def _team_config_path(run_name: str) -> Path:
    """Return ``~/.claude/teams/<run_name>/config.json``."""
    return _TEAMS_DIR / run_name / "config.json"


def _has_operon_state(run_name: str, operon_dir: Path) -> tuple[bool, str | None]:
    """Return ``(ok, reason)`` for the operon-side precondition.

    The required precondition (Land 5 v2 amendment, coordinator
    dispatch 2026-05-21): ``<cwd>/.operon/<run_name>/phase_state.json``
    exists. This is the proof an operon workflow was activated
    against this run.

    The Anthropic team config at
    ``~/.claude/teams/<run_name>/config.json`` is checked separately
    by :func:`_has_team_config` and is OPTIONAL -- a workflow that
    was activated but never had teammates spawned has no team
    config, and restore still works for it.

    Returns ``(False, "<human reason>")`` if phase_state is absent;
    ``(True, None)`` otherwise.
    """
    phase_state = operon_dir / run_name / "phase_state.json"
    if not phase_state.is_file():
        return False, (
            f"Operon phase_state not found at '{phase_state}'. This "
            f"run was never activated by operon's activate_workflow."
        )
    return True, None


def _has_team_config(run_name: str) -> bool:
    """Return True iff ``~/.claude/teams/<run_name>/config.json``
    exists. Optional precondition -- restore still succeeds when
    this is False (lead-only operon-session).
    """
    return _team_config_path(run_name).is_file()


def _read_team_members_for_restore(run_name: str) -> list[dict[str, Any]]:
    """Read the team config and return the ``members`` list.

    Defensive: returns ``[]`` on any read / parse failure. Restore
    treats an unreadable team config as the manifest-empty case
    rather than aborting -- the active-pointer swap is still useful.
    """
    cfg_path = _team_config_path(run_name)
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("restore: failed to read team config %s: %s", cfg_path, exc)
        return []
    if not isinstance(data, dict):
        return []
    members = data.get("members")
    if not isinstance(members, list):
        return []
    return [m for m in members if isinstance(m, dict)]


def _cwd_mangled() -> str:
    """Return the Claude Code project-dir name for the current cwd.

    Empirical convention (verified 2026-05-21 against
    ``~/.claude/projects/-tmp-operon-land4-test/``): each ``/`` in
    the absolute cwd is replaced with ``-``. A path like
    ``/tmp/operon-land4-test`` becomes ``-tmp-operon-land4-test``
    (leading dash from the leading slash). The PreToolUse hook in
    ``hooks/pretooluse.py`` uses the same convention to locate
    sidechain transcripts at hook time.
    """
    return str(Path.cwd().resolve()).replace("/", "-")


def _discover_sidechain_transcripts(agent_type: str) -> list[Path]:
    """Find all ``agent-<hash>.jsonl`` transcripts whose sibling
    ``agent-<hash>.meta.json`` declares ``agentType == agent_type``.

    Walks ``~/.claude/projects/<cwd-mangled>/*/subagents/``. The
    middle path segment is the parent session id; we glob across all
    of them so a teammate that participated in multiple lead-side
    sessions has its transcripts unioned. Sorted by file mtime
    ascending for deterministic temporal order (matches v2.9 section
    5.1 step 3 wording).

    Defensive: returns ``[]`` on filesystem errors. Restore must not
    crash on a missing projects dir or a malformed meta.json.
    """
    project_dir = Path.home() / ".claude" / "projects" / _cwd_mangled()
    if not project_dir.is_dir():
        return []
    matches: list[tuple[float, Path]] = []
    for meta_path in project_dir.glob("*/subagents/agent-*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        if meta.get("agentType") != agent_type:
            continue
        jsonl_path = meta_path.with_suffix("")
        # ``with_suffix("")`` strips only ``.json``; we need to strip
        # ``.meta.json`` -> ``.jsonl``. Build it explicitly.
        jsonl_path = meta_path.parent / (
            meta_path.name[: -len(".meta.json")] + ".jsonl"
        )
        if not jsonl_path.is_file():
            continue
        try:
            mtime = jsonl_path.stat().st_mtime
        except OSError:
            continue
        matches.append((mtime, jsonl_path))
    matches.sort(key=lambda t: t[0])
    return [p for _mtime, p in matches]


def _build_respawn_manifest(run_name: str) -> list[dict[str, Any]]:
    """For every member that is NOT operon and NOT the lead, record
    the prior sidechain transcripts that WA1 will replay on Agent
    spawn.

    Returns one entry per respawn-target member::

        {
          "name": "<member name>",
          "subagent_type": "<agentType>",  # what Agent(...) needs
          "sidechain_count": <int>,
          "sidechain_paths": ["<absolute path>", ...],
        }

    ``operon`` is excluded because it's a non-teammate member.
    ``team-lead`` is excluded because the runtime's ``/resume``
    handles the lead's own session.
    """
    members = _read_team_members_for_restore(run_name)
    manifest: list[dict[str, Any]] = []
    for m in members:
        name = m.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in (_OPERON_MEMBER_NAME, _LEAD_MEMBER_NAME):
            continue
        agent_type = m.get("agentType")
        if not isinstance(agent_type, str) or not agent_type:
            # Fall back to the member name itself; Land 1's
            # subagent_install installs definitions under
            # ``<name>.md`` so name and agentType are typically
            # equal for operon-roster members.
            agent_type = name
        transcripts = _discover_sidechain_transcripts(agent_type)
        manifest.append(
            {
                "name": name,
                "subagent_type": agent_type,
                "sidechain_count": len(transcripts),
                "sidechain_paths": [str(p) for p in transcripts],
            }
        )
    return manifest


def _discover_suggested_members() -> list[dict[str, Any]]:
    """Walk every sidechain meta in the current cwd's project dir and
    return one manifest entry per distinct ``agentType`` seen,
    excluding operon + team-lead.

    Used by the team-config-absent branch of restore: the team
    roster does not exist yet (the user previously cleaned it up or
    never ran TeamCreate after activate_workflow), but the sidechain
    transcripts from the prior session ARE on disk. Each distinct
    agentType the user previously spawned becomes a suggested
    member; the LLM can call ``TeamCreate`` + ``Agent`` to bring
    them back, and operon's WA1 PreToolUse hook will replay the
    matching transcripts at spawn time.

    Returns the same per-entry shape :func:`_build_respawn_manifest`
    produces::

        {"name": "<agentType>",
         "subagent_type": "<agentType>",
         "sidechain_count": <int>,
         "sidechain_paths": ["<path>", ...]}

    The ``name`` field defaults to the agentType because we have no
    team config to consult for a member ``name`` distinct from the
    agentType; the lead can supply a different ``name`` to ``Agent``
    when it spawns the teammate. Empirically the convention is
    name == agentType for operon-installed roles (Land 1's
    ``subagent_install`` installs definitions under ``<role>.md``),
    so this default is the right one.

    Defensive: returns ``[]`` if the projects dir is absent.
    """
    project_dir = Path.home() / ".claude" / "projects" / _cwd_mangled()
    if not project_dir.is_dir():
        return []
    agent_types: set[str] = set()
    for meta_path in project_dir.glob("*/subagents/agent-*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        at = meta.get("agentType")
        if not isinstance(at, str) or not at:
            continue
        if at in (_OPERON_MEMBER_NAME, _LEAD_MEMBER_NAME):
            continue
        agent_types.add(at)
    manifest: list[dict[str, Any]] = []
    for agent_type in sorted(agent_types):
        transcripts = _discover_sidechain_transcripts(agent_type)
        manifest.append(
            {
                "name": agent_type,
                "subagent_type": agent_type,
                "sidechain_count": len(transcripts),
                "sidechain_paths": [str(p) for p in transcripts],
            }
        )
    return manifest


async def _pick_run_name() -> str | None:
    """Surface a picker over runs that have operon phase state on
    disk.

    Land 5 v2 amendment: filter is operon-state-only; team config is
    NOT required for picker eligibility. Each candidate carries a
    short ``team=present|absent`` marker so the user can see at a
    glance which sessions had teammates and which were lead-only.

    Returns the chosen ``run_name``, or ``None`` if the user declined
    or no candidates exist.
    """
    listing = list_tool._do_list()
    raw_sessions = listing.get("sessions", []) or []
    try:
        op_dir = paths.operon_dir()
    except paths.OperonPathError:
        return None

    candidates: list[tuple[str, str, str, str]] = []
    for s in raw_sessions:
        rn = s.get("run_name")
        if not isinstance(rn, str) or not rn:
            continue
        ok, _reason = _has_operon_state(rn, op_dir)
        if not ok:
            continue
        wf = s.get("workflow_id") or "?"
        ph = s.get("current_phase") or "?"
        tc_marker = "team=present" if _has_team_config(rn) else "team=absent"
        candidates.append((rn, wf, ph, tc_marker))
    if not candidates:
        return None

    lines = ["Pick the operon workflow to restore:\n"]
    choices: list[str] = []
    for rn, wf, ph, tc in candidates:
        lines.append(f"  - {rn}  (workflow={wf}, phase={ph}, {tc})")
        choices.append(rn)
    return await elicit.select_one("\n".join(lines), choices, title="Operon workflow")


async def _do_restore(args: dict[str, Any]) -> dict[str, Any]:
    # Identity gate (Coordinator-only). The record itself is not
    # consumed downstream -- restore does not write any handle files
    # in the post-Land-4 architecture -- but the call is retained for
    # its side effect (raises if the caller is not the Coordinator).
    _require_coordinator()
    coord_handle = identity.read_env_handle()
    if not coord_handle:
        raise RestoreOperonSessionError(
            "Coordinator identity is missing OPERON_AGENT_HANDLE."
        )

    raw_run_name = args.get("run_name")
    if raw_run_name is None:
        run_name = await _pick_run_name()
        if run_name is None:
            return {
                "success": False,
                "error": "no_candidates_or_user_declined",
                "details": (
                    "No operon workflows (runs with a phase_state.json) "
                    "were found, or the user declined the picker."
                ),
            }
    else:
        if not isinstance(raw_run_name, str) or not raw_run_name:
            raise RestoreOperonSessionError("'run_name' must be a non-empty string")
        run_name = raw_run_name

    try:
        op_dir = paths.operon_dir()
    except paths.OperonPathError as exc:
        raise RestoreOperonSessionError(str(exc)) from exc

    # Step 1: REQUIRED precondition -- operon phase state for the
    # chosen run must exist (Land 5 v2 amendment).
    ok, reason = _has_operon_state(run_name, op_dir)
    if not ok:
        return {
            "success": False,
            "error": "no_operon_state_for_run",
            "details": reason,
            "run_name": run_name,
        }

    # Atomically swap the active pointer to the chosen run.
    try:
        workflow.write_active_pointer(op_dir, run_name)
    except workflow.WorkflowError as exc:
        raise RestoreOperonSessionError(str(exc)) from exc

    # Read the restored phase state for the response.
    target_phase_state = op_dir / run_name / "phase_state.json"
    workflow_id: str | None = None
    current_phase: str | None = None
    try:
        ps = json.loads(target_phase_state.read_text(encoding="utf-8"))
        if isinstance(ps, dict):
            wid = ps.get("workflow_id")
            if isinstance(wid, str) and wid:
                workflow_id = wid
            cp = ps.get("current_phase")
            if isinstance(cp, str) and cp:
                current_phase = cp
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning(
            "restore: failed to read restored phase_state %s: %s",
            target_phase_state,
            exc,
        )

    # Step 2: OPTIONAL precondition -- branch on team config presence.
    if _has_team_config(run_name):
        members_to_respawn = _build_respawn_manifest(run_name)
        lead_instructions = (
            "For each member in members_to_respawn, call "
            "Agent(subagent_type=<subagent_type>, team_name="
            f"{run_name!r}, name=<name>, prompt=<your continuation "
            "instruction>). Operon's PreToolUse hook on Agent "
            "injects the matching sidechain transcripts in front of "
            "your prompt (WA1 -- v2.9 section 5.1); the teammate "
            "spawns with first-person recall of its prior work."
        )
        return {
            "success": True,
            "run_name": run_name,
            "workflow_id": workflow_id,
            "current_phase": current_phase,
            "team_config": "present",
            "members_to_respawn": members_to_respawn,
            "lead_instructions": lead_instructions,
        }

    # team_config == "absent": no Anthropic team scaffold, but the
    # operon side is intact. Walk the project's sidechain transcripts
    # so the LLM can see which teammates were previously alive and
    # decide whether to recreate the team.
    suggested = _discover_suggested_members()
    lead_instructions = (
        f"Call TeamCreate(team_name={run_name!r}) first to recreate "
        "the Anthropic team scaffold (~/.claude/teams/<run>/"
        "config.json). Then, for each entry in suggested_members, "
        "call Agent(subagent_type=<subagent_type>, team_name="
        f"{run_name!r}, name=<name>, prompt='continue from prior "
        "session'). Operon's PreToolUse hook on Agent injects the "
        "matching sidechain transcripts (WA1 -- v2.9 section 5.1) "
        "so the re-spawned teammate has first-person recall. If you "
        "want a lead-only restore (no teammates), skip the Agent "
        "calls."
    )
    return {
        "success": True,
        "run_name": run_name,
        "workflow_id": workflow_id,
        "current_phase": current_phase,
        "team_config": "absent",
        "suggested_members": suggested,
        "lead_instructions": lead_instructions,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    args = arguments or {}
    try:
        result = await _do_restore(args)
    except RestoreOperonSessionError as exc:
        result = {
            "success": False,
            "error": "validation_failed",
            "details": str(exc),
        }
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]


__all__ = [
    "TOOL_NAME",
    "INPUT_SCHEMA",
    "tool_descriptor",
    "call",
    "RestoreOperonSessionError",
]
