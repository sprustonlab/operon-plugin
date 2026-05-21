"""Workflow-content helpers used by surviving Coordinator tools.

Land 4 of the Agent Teams Pivot
(``docs/AGENT_TEAMS_PIVOT_PLAN.md`` v2.9 section 6, table row for
``tools/spawn_agent.py``: REFACTOR -- "Strip ``_spawn_subprocess``
and the ``OPERON_AGENT_HANDLE`` env propagation. Spawn goes
through Anthropic's ``Agent`` tool invoked by the lead's LLM.").

Pre-Land-4 this module hosted the ``spawn_agent`` MCP tool
(operon-spawned ``claude --bg`` background workers, per-agent
handle files, mailbox-substrate plumbing). Post-Land-4 the
``Agent`` tool is the runtime's contract; operon ships no
subprocess-spawn surface. What remains here are the pure
workflow-content helpers other Coordinator tools still consume:

  * :func:`_require_coordinator` and :class:`SpawnAgentError`
    -- the env-anchored identity gate used by
    ``activate_workflow``, ``advance_phase``,
    ``set_artifact_dir``, ``restore_operon_session``.
  * :func:`assemble_caller_brief` and :func:`absent_caller_brief`
    -- the 3-tier role + phase markdown renderer used by the
    Coordinator tools to give the caller per-phase context.
  * The 3-tier lookup helpers (``_workflow_role_tiers``,
    ``_first_existing_dir``, ``_first_existing_file``) and the
    identity.md frontmatter parser (``_parse_identity_md``) that
    the brief renderer depends on.

The module name (``spawn_agent``) is preserved so existing
``from . import spawn_agent as spawn_agent_tool`` imports in
sibling tools keep working without an audit-trail-breaking
rename. Future cleanup may move these helpers under a more
neutral name (e.g. ``identity_brief.py``); not in scope for
Land 4.

Cross-platform: ``pathlib.Path``, UTF-8 on all I/O, ASCII-only.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from .. import identity, paths

_log = logging.getLogger(__name__)

#: Env var exposing the plugin install root (set by Claude Code in
#: ``.mcp.json``'s ``${CLAUDE_PLUGIN_ROOT}`` expansion). Used to
#: locate the plugin tier of the 3-tier workflow loader.
ENV_PLUGIN_ROOT = "CLAUDE_PLUGIN_ROOT"

#: Coordinator role identifier (lowercase snake_case).
COORDINATOR_ROLE = "coordinator"


class SpawnAgentError(RuntimeError):
    """Raised on identity-gate or brief-resolution failures.

    Kept under the legacy name so sibling tools' existing
    ``except spawn_agent_tool.SpawnAgentError`` clauses do not
    need to change in this commit. The class no longer relates
    to subprocess spawn; it is the generic error type for the
    helpers in this module.
    """


# -- 3-tier loader helpers ----------------------------------------------


def _workflow_role_tiers(workflow_id: str, role: str) -> list[Path]:
    """Return tier directories in priority order: project > user > plugin.

    Project tier resolves against the current ``.operon/`` ancestor
    (the Coordinator's MCP subprocess cwd). User tier is
    ``~/.operon/``. Plugin tier reads ``CLAUDE_PLUGIN_ROOT``; if the
    env var is unset the plugin tier is omitted (the project + user
    tiers can still satisfy the lookup).
    """
    try:
        project_root = paths.project_root()
    except paths.OperonPathError:
        # No .operon ancestor at all -- only user + plugin tiers apply.
        project_root = None

    candidates: list[Path] = []
    if project_root is not None:
        candidates.append(
            project_root / paths.OPERON_DIRNAME / "workflows" / workflow_id / role
        )
    candidates.append(
        Path.home() / paths.OPERON_DIRNAME / "workflows" / workflow_id / role
    )
    plugin_root = os.environ.get(ENV_PLUGIN_ROOT)
    if plugin_root:
        candidates.append(Path(plugin_root) / "workflows" / workflow_id / role)
    return candidates


def _first_existing_dir(tiers: list[Path]) -> Path | None:
    """Return the first directory in ``tiers`` that exists, or ``None``."""
    for tier in tiers:
        if tier.is_dir():
            return tier
    return None


def _first_existing_file(tiers: list[Path], filename: str) -> Path | None:
    """Return the first ``<tier>/<filename>`` that exists, or ``None``."""
    for tier in tiers:
        candidate = tier / filename
        if candidate.is_file():
            return candidate
    return None


# -- Frontmatter parsing -------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)$",
    re.DOTALL,
)


def _parse_identity_md(text: str) -> tuple[dict[str, str], str]:
    """Parse identity.md frontmatter + body.

    Returns ``({key: value, ...}, body_text)``. If the file has no
    leading ``---`` frontmatter block, returns ``({}, full_text)``.

    Only consumes ``description`` and ``model`` per the Phase 3
    simplification; other keys are tolerated and ignored. The
    parser is intentionally hand-rolled (no PyYAML dep) because
    the schema is minimal: ``key: value`` per line, no nesting,
    no lists.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    fm_block = match.group("fm")
    body = match.group("body")
    fields: dict[str, str] = {}
    for raw_line in fm_block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            _log.debug("identity.md frontmatter: skipped unparseable line %r", raw_line)
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, body


# -- Public caller-brief helper -----------------------------------------


def assemble_caller_brief(
    workflow_id: str,
    role: str,
    current_phase: str | None,
) -> dict[str, Any] | None:
    """Render ``<role>/identity.md`` + ``<role>/<phase>.md`` for the caller.

    ``activate_workflow``, ``advance_phase`` and
    ``restore_operon_session`` surface the caller's role brief so
    the Coordinator's LLM gets the same per-phase context that
    spawned teammates receive via the subagent definition.

    Returns the brief dict on success::

        {
          "role": "<role>",
          "phase": "<phase>" or None,
          "identity_md_path": "<str path>",
          "phase_md_path": "<str path>" or None,
          "content": "<identity body>\\n\\n---\\n\\n<phase body>",
          "reason": None,
        }

    Returns ``None`` when the caller's role has no ``identity.md``
    in any tier for this workflow. Callers should surface a
    brief-less response (typically via
    :func:`absent_caller_brief`) rather than treating the absence
    as an error.
    """
    tiers = _workflow_role_tiers(workflow_id, role)
    identity_file = _first_existing_file(tiers, "identity.md")
    if identity_file is None:
        return None

    try:
        identity_text = identity_file.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "caller_brief: identity.md read failed for %s: %s",
            identity_file,
            exc,
        )
        return None
    _, identity_body = _parse_identity_md(identity_text)

    phase_file = None
    phase_body = ""
    if current_phase:
        phase_file = _first_existing_file(tiers, f"{current_phase}.md")
        if phase_file is not None:
            try:
                phase_text = phase_file.read_text(encoding="utf-8")
                _, phase_body = _parse_identity_md(phase_text)
            except OSError as exc:
                _log.warning(
                    "caller_brief: %s.md read failed for %s: %s",
                    current_phase,
                    phase_file,
                    exc,
                )
                phase_file = None
                phase_body = ""

    parts: list[str] = [identity_body.rstrip()]
    if phase_body:
        parts.append(phase_body.rstrip())
    content = "\n\n---\n\n".join(p for p in parts if p)

    return {
        "role": role,
        "phase": current_phase,
        "identity_md_path": str(identity_file),
        "phase_md_path": str(phase_file) if phase_file is not None else None,
        "content": content,
    }


def absent_caller_brief(
    workflow_id: str,
    role: str,
    current_phase: str | None,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Diagnostic stub for the common no-brief response shape.

    Returned in place of ``caller_brief: null`` when the caller
    wants to explain WHY there's no brief (e.g.
    ``_smoke/coordinator/identity.md not found``).
    """
    return {
        "role": role,
        "phase": current_phase,
        "identity_md_path": None,
        "phase_md_path": None,
        "content": None,
        "reason": reason
        or (
            f"No identity.md found for role={role!r} in workflow "
            f"{workflow_id!r} (checked project, user, plugin tiers)."
        ),
    }


# -- Coordinator identity check ------------------------------------------


def _require_coordinator() -> dict[str, Any]:
    """Resolve the calling Agent's identity; require role=coordinator.

    Returns the handle record on success. Raises :class:`SpawnAgentError`
    if no identity is bound or if the caller is not the Coordinator.
    Land 4 keeps the env-anchored handle path intact: the
    Coordinator's MCP subprocess still binds an
    ``OPERON_AGENT_HANDLE`` to its identity file, and the gate
    rejects non-Coordinator callers.
    """
    handle = identity.read_env_handle()
    if handle is None:
        raise SpawnAgentError(
            f"Environment variable '{identity.ENV_HANDLE_VAR}' is not "
            "set; this Coordinator-only tool requires an env-anchored "
            "Coordinator identity."
        )
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        raise SpawnAgentError(str(exc)) from exc
    if record is None:
        raise SpawnAgentError(
            f"No handle record at _handles/{handle}.json; cannot verify caller role."
        )
    role = record.get("role")
    if role != COORDINATOR_ROLE:
        raise SpawnAgentError(f"Coordinator-only tool; caller role is {role!r}.")
    return record
