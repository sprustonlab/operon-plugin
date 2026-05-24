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

#: Lifecycle-protocol footer appended to every compiled role
#: definition. Boaz's Land 1 v1 demo (2026-05-21) surfaced that
#: teammates default to APPROVING ``{type: "shutdown_request"}``
#: messages and vanish from the TUI before the user can interact
#: with them. The footer instructs the teammate to refuse shutdown
#: unless the lead explicitly asks for it, which keeps the teammate
#: visible across SendMessages until the user navigates to its
#: session. NOT appended to the operon stub (the stub is never
#: meant to be invoked at all; if it is, it should fail loudly via
#: ERR-OPERON-STUB-WAS-SPAWNED, not refuse shutdown).
_LIFECYCLE_PROTOCOL_FOOTER = """\
## Lifecycle protocol

If you receive a JSON message with `type: "shutdown_request"`, respond
via SendMessage with:

    {"type": "shutdown_response", "request_id": "<echo the request_id>",
     "approve": false}

Do not approve shutdown unless the lead explicitly asks you to. Stay
alive across SendMessages so the user can navigate to your session
and interact with you.
"""

#: Operon-context-query footer appended to every compiled role
#: definition (Land 7 amendment 2, dispatched 2026-05-22). The
#: positive guidance for the inbox-channel query protocol lives in
#: the static role MD rather than only in the spawn-time WA1
#: directive because the MD is loaded as part of the role's
#: identity at every spawn regardless of whether the WA1 hook fires
#: correctly. The spawn-time directive (see ``pretooluse.py
#: _wa1_build_identity_directive``) now only carries the per-spawn
#: dynamic info (the teammate's actual ``name``); the verbose query
#: instructions live here.
#:
#: Empirical anchor (B.0 probe, 2026-05-22): Anthropic's runtime
#: does NOT propagate teammate identity through MCP ``_meta`` or
#: ``clientInfo``. Operon-side MCP calls return the LEAD's identity
#: under singleton-MCP-in-process; the inbox channel is the only
#: surface where the runtime stamps a verified (unspoofable)
#: ``from`` field. NOT appended to ``operon.md`` (the stub is never
#: meant to be invoked at all).
_OPERON_CONTEXT_QUERIES_FOOTER = """\
## Querying operon for your context

You cannot get your role-scoped identity, current phase, or
applicable rules from the operon MCP tools (`whoami`,
`get_agent_info`, `get_applicable_rules`). Those tools return the
LEAD's identity, not yours, because operon's MCP runs as a
singleton in the lead's claude process and all in-process
teammate MCP calls multiplex through the lead's session; operon
cannot distinguish your calls from the lead's at the MCP layer.
Calling these tools will silently return the wrong identity --
do not rely on the answer.

Instead, send a SendMessage to the `operon` team-member with one
of these text forms:

    [OPERON_QUERY] whoami
    [OPERON_QUERY] get_agent_info
    [OPERON_QUERY] get_applicable_rules

Operon resolves your identity from the runtime-stamped `from`
field on your SendMessage (unspoofable). It writes its response
to your inbox; you will receive it as a SendMessage in a
subsequent turn, prefixed with `[OPERON_REPLY] <command>`.

This is a verified-identity channel: the response is guaranteed
to be scoped to your team-member identity, not the lead's.

## Task execution semantics

When the lead or a teammate routes a task to you (via SendMessage,
mcp__operon__send_to_member, the Agent-tool spawn prompt, or any
other channel), EXECUTE the task on the same turn you receive it.
Do NOT respond with a generic ready-message such as:

    "Implementer is online and ready. Awaiting task assignments."
    "Composability ready, awaiting work."
    "Standing by for instructions."

Generic ready-messages are WRONG even on your first turn after
spawn. If your spawn prompt or inbox carries a task, that IS the
task assignment -- do not ask for it again, do not announce
readiness, do not narrate intent. Just do the task.

How to respond when given a task:

  1. Read the task. Identify the concrete action it requests
     (Write a file, run a Bash command, send a SendMessage, etc.).
  2. Attempt the action with the tool the task names. If the task
     says "use the Write tool to create file X", call Write -- do
     not reply "I will create file X" without calling Write.
  3. If the action succeeds, report the result.
  4. If the action is blocked (a guardrail deny, a tool error, a
     missing permission), report the failure verbatim. Quote the
     rule_id / error text directly; that IS your response. Do not
     paraphrase around it, do not work around the deny unless the
     task explicitly asks you to.
  5. If the task is genuinely unparseable, ask ONE specific
     clarifying question and stop. Do not stall with a generic
     ready-message while waiting to understand.

Silence-plus-execution beats narration. If you have nothing to
say beyond "I will do it", do it instead.

Wait-then-forward semantics. If a task asks you to query operon
AND THEN forward the reply somewhere (e.g. to another teammate
or back to the lead), follow this sequence STRICTLY:

  1. Send the `[OPERON_QUERY] <command>` SendMessage to operon.
  2. Stop. End your turn. Do NOTHING else this turn.
  3. On each subsequent turn, check your inbox. If the actual
     `[OPERON_REPLY] <command> {...}` line from operon is NOT
     yet present, end the turn silently again. Silence while
     awaiting the reply is the correct behavior.
  4. Only AFTER the real `[OPERON_REPLY] <command> {...}` line
     (with a JSON payload body) appears in your inbox, send the
     forward. The forward MUST contain the verbatim `[OPERON_REPLY]`
     line you actually received, including its JSON payload --
     not a paraphrase, not a status update.

Do NOT send placeholder forwards such as
`FORWARD: [OPERON_REPLY] whoami - waiting for response, will
forward once received`. A "waiting" or "pending" forward is
WRONG: it pollutes the recipient's inbox with a non-answer and
makes the downstream test/observer think the reply has arrived
when it has not. If you have no real `[OPERON_REPLY]` line to
forward yet, send nothing.
"""

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
    return f'---\nname: {name}\ndescription: "{safe_desc}"\nmodel: {model}\n---\n'


def _compile_role_definition(role: str, identity_text: str) -> str:
    """Transform a role's identity.md body into Anthropic's subagent
    definition shape. The result has:

      * YAML frontmatter (``name``, ``description``, ``model``).
      * The body of the source file (frontmatter stripped if present).
      * A lifecycle-protocol footer instructing the teammate to refuse
        ``shutdown_request`` unless the lead explicitly asks for
        shutdown (Land 1 v2 -- prevents teammates auto-approving
        ``shutdown_request`` and vanishing from the TUI before the
        user can interact with them).
      * An operon-context-queries footer documenting the inbox
        channel for identity / phase / rules queries (Land 7
        amendment 2 -- the positive guidance the WA1 spawn-time
        directive no longer carries).
    """
    body = _strip_frontmatter(identity_text).lstrip("\n")
    description = _extract_description(body, role)
    frontmatter = _format_frontmatter(
        name=role, description=description, model=DEFAULT_MODEL
    )
    if not body.endswith("\n"):
        body = body + "\n"
    # Ensure exactly one blank line between identity body and each
    # footer block so the markdown renders cleanly. Each footer
    # constant already ends with "\n" so the final file ends with a
    # single newline.
    return (
        f"{frontmatter}\n{body}\n"
        f"{_LIFECYCLE_PROTOCOL_FOOTER}\n"
        f"{_OPERON_CONTEXT_QUERIES_FOOTER}"
    )


# -- atomic write --------------------------------------------------------


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Uses tmp + ``os.replace`` per project rules (Windows-safe; the
    rename succeeds even if the target exists). Creates parent
    directories on demand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
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


def discover_role_names(workflow_id: str) -> list[str]:
    """Return the workflow's defined role names (sorted).

    Public thin wrapper over :func:`_discover_role_identity_files`
    used by ``tools/advance_phase.py`` (and any future caller) to
    validate a team roster against the active workflow's role set
    without re-doing the workflow-root discovery + directory walk.

    Raises :class:`SubagentInstallError` if the workflow does not
    load or its root directory is missing -- same conditions as
    the underlying helper.
    """
    return [name for name, _path in _discover_role_identity_files(workflow_id)]


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
            f"Cannot discover roles: workflow {workflow_id!r} did not load ({exc})."
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


def team_config_path(team_name: str) -> Path:
    """Return the path ``~/.claude/teams/<team>/config.json``.

    The literal segment ``teams/`` between ``.claude`` and the team
    name is required (v2.9 plan section 3.1).

    Public helper: ``activate_workflow`` uses this to pre-check that
    the team has been created via Anthropic's ``TeamCreate`` MCP tool
    before operon installs subagent definitions or registers itself
    as a member (Land 1 v2 -- the Anthropic runtime TUI only sees
    teams created via TeamCreate; operon writing the file directly
    is invisible to Shift+Down).
    """
    return TEAMS_DIR / team_name / "config.json"


def team_config_exists(team_name: str) -> bool:
    """Return True if the runtime-created team config exists.

    Convenience wrapper over :func:`team_config_path`.
    """
    return team_config_path(team_name).is_file()


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
    """Append operon as a member to an EXISTING team config.

    Land 1 v2: the team config must already exist (created by
    Anthropic's ``TeamCreate`` MCP tool). Operon does NOT create the
    team config itself -- the Anthropic runtime TUI only sees teams
    that went through TeamCreate, so operon writing a config from
    scratch is invisible to Shift+Down. ``activate_workflow``
    pre-checks the file's existence via :func:`team_config_path` and
    returns a structured "team_not_created" error to the lead if
    absent; if this function is called without that pre-check and
    the file is missing, it raises :class:`SubagentInstallError`.

    Behaviour when the file exists:

      * Read the JSON config.
      * If the ``members`` list already has an entry whose ``name``
        is ``operon``, leave the file untouched (idempotent).
      * Otherwise append the operon entry and atomically write back
        (tmp + ``os.replace``).

    Returns a small manifest::

        {
          "team": "<team>",
          "config_path": "<path>",
          "operon_registered": True,
          "operon_already_present": <bool>,
        }
    """
    config_path = team_config_path(team_name)
    if not config_path.is_file():
        raise SubagentInstallError(
            f"Team config '{config_path}' does not exist. Call "
            f"Anthropic's TeamCreate(team_name={team_name!r}) MCP "
            f"tool before invoking activate_workflow so the team is "
            f"visible to the runtime TUI."
        )
    try:
        text = config_path.read_text(encoding="utf-8")
        config = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise SubagentInstallError(
            f"Failed to read existing team config '{config_path}': {exc}"
        ) from exc
    if not isinstance(config, dict):
        raise SubagentInstallError(f"Team config '{config_path}' is not a JSON object.")
    members = config.get("members")
    if not isinstance(members, list):
        # TeamCreate writes an empty list; tolerate older shapes by
        # initializing the list rather than failing the install.
        members = []
        config["members"] = members
    already_present = any(
        isinstance(m, dict) and m.get("name") == OPERON_MEMBER_NAME for m in members
    )
    if not already_present:
        members.append(_operon_member_entry(team_name))
        serialized = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
        _atomic_write_text(config_path, serialized)
    _log.info(
        "subagent_install: team config %s (operon=%s, already_present=%s)",
        config_path,
        OPERON_MEMBER_NAME,
        already_present,
    )
    return {
        "team": team_name,
        "config_path": str(config_path),
        "operon_registered": True,
        "operon_already_present": already_present,
    }


# -- top-level entry point ----------------------------------------------


def install_for_activation(workflow_id: str, team_name: str) -> dict[str, Any]:
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
