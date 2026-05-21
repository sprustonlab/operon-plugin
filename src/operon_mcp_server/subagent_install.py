"""Subagent-definition transformer + operon-as-team-member registration.

Land 1 of the Agent Teams Pivot (see
``docs/AGENT_TEAMS_PIVOT_PLAN.md`` v2.9 sections 4.3 component 6,
4.7, 7, and 8 Land 1).

This module bridges two schemas:

  * operon workflow content -- per-role identity markdown at
    ``plugins/operon-plugin/workflows/<workflow_id>/<role>/identity.md``,
    typically a bare markdown file with no YAML frontmatter.
  * Anthropic's subagent definition schema -- a markdown file at
    ``~/.claude/agents/<role>.md`` with required YAML frontmatter
    (``name``, ``description``) plus optional ``model`` and
    ``tools`` keys, followed by the body the runtime loads as the
    teammate's system prompt at spawn time.

The transformation is intentionally NOT a file copy: the two
sides have different shapes, and the transformer is the seam
between them (v2.9 plan section 4.3 component 6).

This module also handles operon-as-team-member registration
(v2.9 plan section 4.7): when ``activate_workflow`` creates the
team config at ``~/.claude/teams/<team>/config.json``, operon
includes itself as a non-teammate member backed by a no-op
subagent definition. The stub exists so reply routing through
the runtime's roster has a real target for ``from: "operon"``
inbox writes; the runtime never actually spawns the stub.

Cross-platform per project rules:

  * ``pathlib.Path`` throughout; never string-join with ``/``.
  * ``encoding="utf-8"`` on every read/write.
  * ``os.replace`` for atomic rename (works on Windows when
    target exists; ``Path.rename`` does not).
  * ASCII only; no emoji, em-dash, or box-drawing characters.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import paths, workflow

_log = logging.getLogger(__name__)

#: Anthropic's subagent definition directory (project-scope).
SUBAGENTS_DIR = Path.home() / ".claude" / "agents"

#: Anthropic's teams directory.
TEAMS_DIR = Path.home() / ".claude" / "teams"

#: Reserved member name operon registers itself under (v2.9 plan
#: section 4.7). The ``from`` field on every operon-originated
#: inbox write must equal this value for reply routing to resolve.
OPERON_MEMBER_NAME = "operon"

#: ``agentType`` for the operon stub subagent definition. Mirrors
#: the TeamsSpike step05 fixture
#: (``~/.claude/teams/step05-test/config.json``).
OPERON_STUB_AGENT_TYPE = "operon-stub"

#: Default model marker for transformed role definitions. ``inherit``
#: tells the runtime to use the lead's model, which is what we want:
#: operon does not pick models for roles; the lead's choice cascades.
DEFAULT_MODEL = "inherit"

#: Stub prompt body for the operon subagent definition. The runtime
#: does NOT spawn this; the entry exists only so the team roster has
#: a real target for ``from: "operon"`` reply routing. If something
#: does invoke it, the stub fails loudly so we notice.
_OPERON_STUB_BODY = (
    "You should never be invoked. Operon's MCP server is the actual "
    "handler for the 'operon' team-member slot. If you are spawned, "
    "this is a bug -- reply 'ERR-OPERON-STUB-WAS-SPAWNED' and stop.\n"
)

#: Frontmatter delimiter regex (mirrors spawn_agent.py's parser but
#: kept local so this module has no upward import).
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)$",
    re.DOTALL,
)


class SubagentInstallError(RuntimeError):
    """Raised on transform / write failures."""


# -- identity.md helpers -------------------------------------------------


def _strip_frontmatter(text: str) -> str:
    """Return the body of an identity.md file with any leading YAML
    frontmatter block removed. If no frontmatter is present, returns
    the input unchanged.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text
    return match.group("body")


def _extract_description(body: str, role: str) -> str:
    """Synthesize a one-line description for the subagent frontmatter.

    Strategy: walk the body for the first non-heading, non-empty,
    non-list-marker line. Truncate to 200 characters to keep the
    frontmatter readable. Falls back to a generic ``role`` template
    if nothing useful is found.
    """
    fallback = f"Operon workflow role: {role}"
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("---"):
            continue
        # Strip markdown emphasis markers so the description reads
        # cleanly as a plain sentence.
        cleaned = line.strip("*_").strip()
        if not cleaned:
            continue
        if len(cleaned) > 200:
            cleaned = cleaned[:197].rstrip() + "..."
        return cleaned
    return fallback


def _format_frontmatter(name: str, description: str, model: str) -> str:
    """Render the YAML frontmatter block for a subagent definition.

    Schema (Anthropic documented; cross-checked against bundled
    marketplace agents under
    ``~/.claude/plugins/marketplaces/.../agents/*.md``):

      * ``name``: required, used as the dispatch name.
      * ``description``: required, surfaced when listing agents.
      * ``model``: optional; ``inherit`` reuses the lead's model.
      * ``tools``: omitted -- when absent, the subagent inherits
        the full tool set, which is what operon roles need (the
        roles do real work, not narrow lookups).

    Description is escaped defensively: any embedded double quotes
    are replaced with single quotes so the value can be wrapped in
    double quotes without YAML-parse trouble.
    """
    safe_desc = description.replace('"', "'").replace("\n", " ").strip()
    return (
        "---\n"
        f"name: {name}\n"
        f'description: "{safe_desc}"\n'
        f"model: {model}\n"
        "---\n"
    )


def _compile_role_definition(role: str, identity_text: str) -> str:
    """Transform a role's identity.md body into Anthropic's subagent
    definition shape. The result has YAML frontmatter followed by
    the body of the source file (frontmatter stripped if present).
    """
    body = _strip_frontmatter(identity_text).lstrip("\n")
    description = _extract_description(body, role)
    frontmatter = _format_frontmatter(
        name=role, description=description, model=DEFAULT_MODEL
    )
    if not body.endswith("\n"):
        body = body + "\n"
    return f"{frontmatter}\n{body}"


# -- atomic write --------------------------------------------------------


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Uses tmp + ``os.replace`` per project rules (Windows-safe; the
    rename succeeds even if the target exists). Creates parent
    directories on demand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise SubagentInstallError(
            f"Failed to write subagent definition to '{path}': {exc}"
        ) from exc


# -- role discovery ------------------------------------------------------


def _discover_role_identity_files(workflow_id: str) -> list[tuple[str, Path]]:
    """Find every ``<role>/identity.md`` under the workflow's root
    directory (the directory containing the workflow's manifest YAML).

    Returns ``[(role, identity_md_path), ...]`` sorted by role for
    deterministic install order. Role discovery is one-tier: the
    transformer compiles roles from the tier that satisfied the
    workflow manifest lookup (project > user > plugin via
    ``workflow.load_workflow``). Cross-tier role merging is out of
    scope for Land 1 -- if a user wants to override a single role,
    the right tool is a full-workflow override, not a per-role
    splice.
    """
    try:
        decl = workflow.load_workflow(workflow_id)
    except workflow.WorkflowError as exc:
        raise SubagentInstallError(
            f"Cannot discover roles: workflow {workflow_id!r} did "
            f"not load ({exc})."
        ) from exc

    workflow_root = decl.source_path.parent
    if not workflow_root.is_dir():
        raise SubagentInstallError(
            f"Workflow root '{workflow_root}' is not a directory; "
            f"cannot discover roles."
        )

    roles: list[tuple[str, Path]] = []
    for child in sorted(workflow_root.iterdir()):
        if not child.is_dir():
            continue
        # Skip private / non-role directories (e.g. ``__pycache__``).
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        identity_md = child / "identity.md"
        if identity_md.is_file():
            roles.append((child.name, identity_md))
    return roles


# -- public API: subagent definitions -----------------------------------


def install_role_definition(role: str, identity_md: Path) -> Path:
    """Compile one role's identity.md into a subagent definition and
    write it to ``~/.claude/agents/<role>.md``. Returns the install
    path.

    Overwrites any existing file at the install path. This is by
    design: ``activate_workflow`` is the single seam that triggers
    install, and a fresh activate should ship a fresh definition.
    """
    try:
        identity_text = identity_md.read_text(encoding="utf-8")
    except OSError as exc:
        raise SubagentInstallError(
            f"Failed to read role identity '{identity_md}': {exc}"
        ) from exc
    compiled = _compile_role_definition(role, identity_text)
    dest = SUBAGENTS_DIR / f"{role}.md"
    _atomic_write_text(dest, compiled)
    _log.info(
        "subagent_install: wrote %s (%d bytes) from %s",
        dest,
        len(compiled),
        identity_md,
    )
    return dest


def install_operon_stub() -> Path:
    """Write the operon stub subagent definition.

    The stub satisfies Anthropic's roster requirement (every team
    member needs a subagent definition lookup target by
    ``agentType``) without ever being spawned: the runtime treats
    operon's roster entry as an external backend, so the stub's
    body only fires if something invokes it accidentally.
    """
    body = _OPERON_STUB_BODY
    frontmatter = _format_frontmatter(
        name=OPERON_MEMBER_NAME,
        description=(
            "No-op stub for the operon team-member slot. Operon's "
            "MCP is the real handler; this file exists only so the "
            "team roster has a subagent-definition target for "
            "reply routing."
        ),
        model=DEFAULT_MODEL,
    )
    content = f"{frontmatter}\n{body}"
    dest = SUBAGENTS_DIR / f"{OPERON_MEMBER_NAME}.md"
    _atomic_write_text(dest, content)
    _log.info("subagent_install: wrote operon stub at %s", dest)
    return dest


def install_workflow_subagents(workflow_id: str) -> dict[str, Any]:
    """Install subagent definitions for every role declared by a
    workflow, plus the operon stub.

    Idempotent: each invocation overwrites the previous install.
    Safe to call on every ``activate_workflow`` (which is what
    Land 1 does -- see ``tools/activate_workflow.py``).

    Returns a manifest dict suitable for inclusion in the tool's
    JSON response::

        {
          "workflow_id": "<id>",
          "subagents_dir": "<path>",
          "roles_installed": [
            {"role": "<role>", "source": "<identity_md_path>",
             "dest": "<agents/<role>.md path>"},
            ...
          ],
          "operon_stub": "<agents/operon.md path>",
        }
    """
    roles = _discover_role_identity_files(workflow_id)
    installed: list[dict[str, str]] = []
    for role, identity_md in roles:
        dest = install_role_definition(role, identity_md)
        installed.append(
            {
                "role": role,
                "source": str(identity_md),
                "dest": str(dest),
            }
        )
    operon_dest = install_operon_stub()
    return {
        "workflow_id": workflow_id,
        "subagents_dir": str(SUBAGENTS_DIR),
        "roles_installed": installed,
        "operon_stub": str(operon_dest),
    }


# -- public API: team config + operon-as-member registration ------------


def _team_config_path(team_name: str) -> Path:
    """Return the path ``~/.claude/teams/<team>/config.json``.

    The literal segment ``teams/`` between ``.claude`` and the team
    name is required (v2.9 plan section 3.1).
    """
    return TEAMS_DIR / team_name / "config.json"


def _now_ms() -> int:
    """Return the current UTC time as ms since epoch. Matches the
    integer-ms timestamp shape Anthropic uses in its team configs
    (see ``~/.claude/teams/spike-test/config.json`` for the empirical
    schema)."""
    return int(time.time() * 1000)


def _operon_member_entry(team_name: str) -> dict[str, Any]:
    """Build the operon member record for a team config.

    Mirrors the empirical schema observed at
    ``~/.claude/teams/step05-test/config.json`` from TeamsSpike:
    ``backendType: external``, ``tmuxPaneId: external-process``,
    ``agentType: operon-stub``. The runtime treats this slot as
    handled by an out-of-process participant (operon's MCP) and
    will not spawn the stub on its own.
    """
    return {
        "agentId": f"{OPERON_MEMBER_NAME}@{team_name}",
        "name": OPERON_MEMBER_NAME,
        "color": "magenta",
        "joinedAt": _now_ms(),
        "tmuxPaneId": "external-process",
        "subscriptions": [],
        "agentType": OPERON_STUB_AGENT_TYPE,
        "model": DEFAULT_MODEL,
        "prompt": _OPERON_STUB_BODY.strip(),
        "planModeRequired": False,
        "cwd": str(Path.cwd()),
        "backendType": "external",
    }


def register_operon_in_team_config(team_name: str) -> dict[str, Any]:
    """Ensure ``~/.claude/teams/<team>/config.json`` exists with
    operon registered as a member.

    Behaviour:

      * If the team config file is absent, create a minimal config
        whose only member is operon. The runtime will append a
        ``leadAgentId``/lead member entry the first time the lead's
        session attaches to this team (TeamsSpike Round 1 validated
        this idempotent append).
      * If the file exists but does not include operon, read its
        ``members`` list, append the operon entry, and write the
        whole config back atomically (tmp + ``os.replace``).
      * If the file exists and already includes operon, leave it
        alone.

    Returns a small manifest::

        {
          "team": "<team>",
          "config_path": "<path>",
          "operon_registered": True,
          "created_config": <bool>,
        }
    """
    config_path = _team_config_path(team_name)
    created = False
    if config_path.is_file():
        try:
            text = config_path.read_text(encoding="utf-8")
            config = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise SubagentInstallError(
                f"Failed to read existing team config '{config_path}': "
                f"{exc}"
            ) from exc
        if not isinstance(config, dict):
            raise SubagentInstallError(
                f"Team config '{config_path}' is not a JSON object."
            )
        members = config.get("members")
        if not isinstance(members, list):
            members = []
            config["members"] = members
        already_present = any(
            isinstance(m, dict) and m.get("name") == OPERON_MEMBER_NAME
            for m in members
        )
        if not already_present:
            members.append(_operon_member_entry(team_name))
    else:
        created = True
        config = {
            "name": team_name,
            "createdAt": _now_ms(),
            "members": [_operon_member_entry(team_name)],
        }

    serialized = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(config_path, serialized)
    _log.info(
        "subagent_install: team config %s (operon=%s, created=%s)",
        config_path,
        OPERON_MEMBER_NAME,
        created,
    )
    return {
        "team": team_name,
        "config_path": str(config_path),
        "operon_registered": True,
        "created_config": created,
    }


# -- top-level entry point ----------------------------------------------


def install_for_activation(
    workflow_id: str, team_name: str
) -> dict[str, Any]:
    """Compose the two Land-1 surfaces into one call.

    Order matters: subagent definitions are written BEFORE the team
    config is registered, so the runtime can resolve every member's
    ``agentType`` to a real subagent definition on first spawn.

    Returns a single manifest combining both sub-results, keyed by
    section, for inclusion in ``activate_workflow``'s tool response.
    """
    # Silence the unused-import warning if paths ever stops being
    # touched here -- the module relies on paths via workflow.load_workflow.
    _ = paths
    subagents = install_workflow_subagents(workflow_id)
    team = register_operon_in_team_config(team_name)
    return {"subagents": subagents, "team": team}
