"""`restore_operon_session` MCP tool (Coordinator-only).

Land 5 of the Agent Teams Pivot (see
``docs/AGENT_TEAMS_PIVOT_PLAN.md`` v2.9 section 5.1 + section 8
Land 5). Boaz's empirical 2026-05-21 finding: ``/resume`` of a
post-Land-4 operon session does NOT auto-respawn the teammates
that were alive at suspend time -- Shift+Down shows no
composability teammate. Land 5 wires operon-driven RESTORE on
top of WA1 (Section 5.1) so a previously-activated operon team
project can be brought back to a workable state.

User-side framing (Boaz, 2026-05-21): "I want a RESTORE not a
resume, it should be for an activated team project only."
Translation:

  * Operon owns the operation; this MCP tool is the entry point.
  * Precondition: previously-activated operon team project ->
    both the team config at ``~/.claude/teams/<run>/config.json``
    AND ``<cwd>/.operon/<run>/phase_state.json`` exist on disk.
  * Generic ``/resume`` of a non-operon Claude Code session is
    NOT in this tool's scope (the Anthropic runtime handles
    lead-side ``/resume``; operon's job is the teammate respawn
    manifest).

Two entry modes (matches the claudechic ``chicsessions.py`` /
``chicsession_cmd.py`` restore pattern; the WA1 PreToolUse hook
in ``plugins/operon-plugin/hooks/pretooluse.py`` is the WA1
substitute for the SDK ``resume=session_id`` parameter):

  * ``run_name`` supplied: skip the picker, validate the target,
    swap the active pointer, build the respawn manifest.
  * ``run_name`` omitted: discover candidate runs via
    ``list_operon_sessions._do_list()`` filtered to runs whose
    BOTH team config + phase_state are present, surface a
    single-select picker elicitation, then proceed.

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
                "Name of the operon team project to restore. If "
                "omitted, the tool lists existing projects (filtered "
                "to those with BOTH a team config and a phase_state) "
                "and issues a picker."
            ),
        },
    },
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Restore a previously-activated operon team project. "
            "Precondition: ~/.claude/teams/<run_name>/config.json AND "
            "<cwd>/.operon/<run_name>/phase_state.json both exist. "
            "Sets <cwd>/.operon/_active.json to point at the chosen "
            "run, then returns a manifest of the team members that "
            "need to be re-spawned via Anthropic's Agent tool. WA1 "
            "transcript replay (v2.9 section 5.1) is delivered by the "
            "PreToolUse hook on Agent. With no run_name argument, "
            "surfaces a picker. Coordinator-only."
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


def _is_operon_team_project(run_name: str, operon_dir: Path) -> tuple[bool, str | None]:
    """Return ``(ok, reason)`` for the operon-team-project precondition.

    A run satisfies the precondition iff both:

      1. ``~/.claude/teams/<run_name>/config.json`` exists (the
         Anthropic runtime saw a TeamCreate for this name).
      2. ``<cwd>/.operon/<run_name>/phase_state.json`` exists
         (operon's activate_workflow ran against it).

    Returns ``(False, "<human reason>")`` on failure; ``(True, None)``
    on success.
    """
    team_cfg = _team_config_path(run_name)
    if not team_cfg.is_file():
        return False, (
            f"Team config not found at '{team_cfg}'. This run was "
            f"never activated as an Anthropic team (no TeamCreate)."
        )
    phase_state = operon_dir / run_name / "phase_state.json"
    if not phase_state.is_file():
        return False, (
            f"Operon phase_state not found at '{phase_state}'. This "
            f"run was never activated by operon's activate_workflow."
        )
    return True, None


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


async def _pick_run_name() -> str | None:
    """Surface a picker over runs that satisfy the operon-team-project
    precondition.

    Returns the chosen ``run_name``, or ``None`` if the user declined
    or no candidates exist.
    """
    listing = list_tool._do_list()
    raw_sessions = listing.get("sessions", []) or []
    try:
        op_dir = paths.operon_dir()
    except paths.OperonPathError:
        return None

    candidates: list[tuple[str, str, str]] = []
    for s in raw_sessions:
        rn = s.get("run_name")
        if not isinstance(rn, str) or not rn:
            continue
        ok, _reason = _is_operon_team_project(rn, op_dir)
        if not ok:
            continue
        wf = s.get("workflow_id") or "?"
        ph = s.get("current_phase") or "?"
        candidates.append((rn, wf, ph))
    if not candidates:
        return None

    lines = ["Pick the operon team project to restore:\n"]
    choices: list[str] = []
    for rn, wf, ph in candidates:
        lines.append(f"  - {rn}  (workflow={wf}, phase={ph})")
        choices.append(rn)
    return await elicit.select_one(
        "\n".join(lines), choices, title="Operon team project"
    )


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
                    "No operon team projects (runs with both team "
                    "config and phase_state) were found, or the user "
                    "declined the picker."
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

    ok, reason = _is_operon_team_project(run_name, op_dir)
    if not ok:
        return {
            "success": False,
            "error": "not_an_operon_team_project",
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

    members_to_respawn = _build_respawn_manifest(run_name)

    lead_instructions = (
        "For each member in members_to_respawn, call "
        "Agent(subagent_type=<subagent_type>, team_name="
        f"{run_name!r}, name=<name>, prompt=<your continuation "
        "instruction>). Operon's PreToolUse hook on Agent injects "
        "the matching sidechain transcripts in front of your prompt "
        "(WA1 -- v2.9 section 5.1); the teammate spawns with "
        "first-person recall of its prior work."
    )

    return {
        "success": True,
        "run_name": run_name,
        "workflow_id": workflow_id,
        "current_phase": current_phase,
        "members_to_respawn": members_to_respawn,
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
