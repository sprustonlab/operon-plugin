"""operon-plugin MCP server entry point.

Phase 4: inter-agent messaging. Registered tools (visibility per
SPEC.md section 7.1):

- `whoami` -- visible to All roles.
- `spawn_agent` -- visible to Coordinator only.
- `bind_handle` -- HIDDEN (hook-only; routed by qualified name).
- `message_agent` -- visible to All roles.
- `broadcast_message` -- visible to All roles.
- `interrupt_agent` -- visible to Coordinator only.
- `close_agent` -- visible to Coordinator only.

Role-scoped `tools/list` filtering is applied per SPEC.md section 7.1:
each MCP subprocess returns only the tools visible to its env-anchored
role. The bound role is resolved by reading `OPERON_AGENT_HANDLE`,
looking up `_handles/<handle>.json`, and reading the `role` field. If no
identity context is available (env unset, handle file missing) the
server defaults to the least-privilege view (All-class tools only).

Per SPEC.md section 6.6 each MCP subprocess also runs a background
filesystem-watch loop over its OWN `mailbox/<self>/inbox/` and
`mailbox/<self>/control/` directories. Implemented in `watch.py`;
started lazily on the first `tools/list` fire that resolves identity.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import anyio
import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import bootstrap, identity
from .tools import acknowledge_warning as acknowledge_warning_tool
from .tools import activate_workflow as activate_workflow_tool
from .tools import advance_phase as advance_phase_tool
from .tools import bind_handle as bind_handle_tool
from .tools import evaluate as evaluate_tool
from .tools import get_agent_info as get_agent_info_tool
from .tools import get_applicable_rules as get_applicable_rules_tool
from .tools import get_phase as get_phase_tool
from .tools import list_operon_sessions as list_operon_sessions_tool
from .tools import request_override as request_override_tool
from .tools import restore_operon_session as restore_operon_session_tool
from .tools import send_to_member as send_to_member_tool
from .tools import set_artifact_dir as set_artifact_dir_tool
from .tools import whoami as whoami_tool

#: MCP server name advertised in the `initialize` response. Must match
#: the key under `mcpServers` in `plugins/operon-plugin/.mcp.json` so
#: that Claude Code can correlate the registered server with this
#: subprocess.
SERVER_NAME = "operon"

#: Plugin version advertised in `initialize` response. Hardcoded here
#: (rather than read from `server.version`) because the MCP SDK
#: auto-populates `server.version` with the SDK's own version string,
#: which would otherwise leak through as the plugin's version. Keep in
#: sync with `pyproject.toml`'s `[project] version`.
SERVER_VERSION = "0.0.1"

#: Non-standard Claude-Code capabilities advertised under the
#: `experimental` field of `capabilities` per the MCP spec. The
#: `claude/channel` extension lets the server push messages into
#: running sessions (SPEC.md sections 6 and 7.2). Per the MCP spec all
#: non-standard capabilities MUST be nested under `experimental` --
#: Phase 1 placed it at top level (via pydantic extra-field), which
#: caused Claude Code to silently ignore the capability AND, because
#: the `tools` capability was not declared at all, Claude Code never
#: even asked for `tools/list`. We now delegate capability
#: construction to the SDK helper which auto-derives `tools` from the
#: registered `@server.list_tools()` handler.
EXPERIMENTAL_CAPABILITIES: dict[str, dict[str, Any]] = {"claude/channel": {}}

#: Env var that, when set to a truthy value, enables verbose DEBUG-level
#: logging to stderr. Defaults to off: in production the subprocess is
#: spawned with `stderr=DEVNULL` by `spawn_agent` (see SPEC §6.5) and
#: we don't want chatter even if a terminal is wired in. Toggle on for
#: manual diagnosis of identity binding / watch-loop startup issues.
ENV_DEBUG_FLAG = "OPERON_DEBUG"


def _maybe_configure_stderr_logging() -> None:
    """Wire DEBUG-level stderr logging if `OPERON_DEBUG=1` is set.

    Safe to call multiple times -- `logging.basicConfig` is a no-op
    after the first call. Format includes timestamp + logger name so
    the per-subprocess origin of each line is unambiguous when
    multiple Agents log into the same shell session via the
    `claude attach` viewer. Claude Code routes the MCP subprocess's
    stderr to `~/.claude/debug/<session-id>.txt` (per channels-reference
    docs), so OPERON_DEBUG output lands there for spawned workers.
    """
    flag = os.environ.get(ENV_DEBUG_FLAG, "").strip().lower()
    if flag in {"", "0", "false", "no"}:
        return
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG,
        format=("[%(asctime)s] %(name)s %(levelname)s (pid=%(process)d): %(message)s"),
    )


#: Coordinator role identifier (mirrors `tools.spawn_agent.COORDINATOR_ROLE`).
COORDINATOR_ROLE = "coordinator"

#: Visibility labels per SPEC.md section 7.1. `"all"` = advertised to
#: every role; `"coordinator_only"` = advertised only when the calling
#: subprocess's bound role is the Coordinator; `"hidden"` = never
#: advertised in `tools/list` (hook-callable by qualified name only).
_VISIBILITY_ALL = "all"
_VISIBILITY_COORDINATOR_ONLY = "coordinator_only"
_VISIBILITY_HIDDEN = "hidden"

#: Per-tool visibility metadata. Adding a new tool requires adding both
#: a handler entry in `_TOOL_HANDLERS` and a visibility entry here.
#:
#: Land 4 (v2.9 plan section 6) removed the pre-pivot tool surfaces:
#: spawn_agent, message_agent, broadcast_message, interrupt_agent,
#: close_agent, arm_nudge_timer. Spawn is now the runtime's Agent
#: tool; message delivery is via the inbox-write primitive
#: (send_to_member + advance_phase team_broadcast).
_TOOL_VISIBILITY: dict[str, str] = {
    whoami_tool.TOOL_NAME: _VISIBILITY_ALL,
    bind_handle_tool.TOOL_NAME: _VISIBILITY_HIDDEN,
    # Phase 5: workflow + phase engine.
    activate_workflow_tool.TOOL_NAME: _VISIBILITY_COORDINATOR_ONLY,
    set_artifact_dir_tool.TOOL_NAME: _VISIBILITY_COORDINATOR_ONLY,
    advance_phase_tool.TOOL_NAME: _VISIBILITY_COORDINATOR_ONLY,
    get_phase_tool.TOOL_NAME: _VISIBILITY_ALL,
    get_applicable_rules_tool.TOOL_NAME: _VISIBILITY_ALL,
    get_agent_info_tool.TOOL_NAME: _VISIBILITY_ALL,
    # Phase 6: guardrail Rules.
    evaluate_tool.TOOL_NAME: _VISIBILITY_HIDDEN,
    request_override_tool.TOOL_NAME: _VISIBILITY_ALL,
    acknowledge_warning_tool.TOOL_NAME: _VISIBILITY_ALL,
    # Phase 6.5: operon-session management.
    list_operon_sessions_tool.TOOL_NAME: _VISIBILITY_ALL,
    restore_operon_session_tool.TOOL_NAME: _VISIBILITY_COORDINATOR_ONLY,
    # Agent Teams pivot Land 2: inbox-write primitive surface.
    send_to_member_tool.TOOL_NAME: _VISIBILITY_ALL,
}

#: Routing table: tool name -> handler coroutine. Includes HIDDEN tools
#: so that hook-driven calls (e.g. `mcp__operon__bind_handle`) still find
#: a handler even though the tool is not listed in `tools/list`.
_TOOL_HANDLERS = {
    whoami_tool.TOOL_NAME: whoami_tool.call,
    bind_handle_tool.TOOL_NAME: bind_handle_tool.call,
    # Phase 5: workflow + phase engine.
    activate_workflow_tool.TOOL_NAME: activate_workflow_tool.call,
    set_artifact_dir_tool.TOOL_NAME: set_artifact_dir_tool.call,
    advance_phase_tool.TOOL_NAME: advance_phase_tool.call,
    get_phase_tool.TOOL_NAME: get_phase_tool.call,
    get_applicable_rules_tool.TOOL_NAME: get_applicable_rules_tool.call,
    get_agent_info_tool.TOOL_NAME: get_agent_info_tool.call,
    # Phase 6: guardrail Rules.
    evaluate_tool.TOOL_NAME: evaluate_tool.call,
    request_override_tool.TOOL_NAME: request_override_tool.call,
    acknowledge_warning_tool.TOOL_NAME: acknowledge_warning_tool.call,
    # Phase 6.5: operon-session management.
    list_operon_sessions_tool.TOOL_NAME: list_operon_sessions_tool.call,
    restore_operon_session_tool.TOOL_NAME: restore_operon_session_tool.call,
    # Agent Teams pivot Land 2: inbox-write primitive.
    send_to_member_tool.TOOL_NAME: send_to_member_tool.call,
}

#: Tool descriptors keyed by name (used by the role-scoped filter to
#: build the `tools/list` response). HIDDEN tools are omitted from this
#: table -- they are never advertised, only dispatched via qualified
#: name from hooks.
_TOOL_DESCRIPTORS: dict[str, mcp_types.Tool] = {
    whoami_tool.TOOL_NAME: whoami_tool.tool_descriptor(),
    # Phase 5 tools.
    activate_workflow_tool.TOOL_NAME: activate_workflow_tool.tool_descriptor(),
    set_artifact_dir_tool.TOOL_NAME: set_artifact_dir_tool.tool_descriptor(),
    advance_phase_tool.TOOL_NAME: advance_phase_tool.tool_descriptor(),
    get_phase_tool.TOOL_NAME: get_phase_tool.tool_descriptor(),
    get_applicable_rules_tool.TOOL_NAME: get_applicable_rules_tool.tool_descriptor(),
    get_agent_info_tool.TOOL_NAME: get_agent_info_tool.tool_descriptor(),
    # Phase 6 tools. `evaluate` is HIDDEN (PreToolUse hook-only) and
    # therefore intentionally omitted from this descriptor table; the
    # `_TOOL_HANDLERS` routing still dispatches it for the hook call.
    request_override_tool.TOOL_NAME: request_override_tool.tool_descriptor(),
    acknowledge_warning_tool.TOOL_NAME: acknowledge_warning_tool.tool_descriptor(),
    # Phase 6.5 tools.
    list_operon_sessions_tool.TOOL_NAME: list_operon_sessions_tool.tool_descriptor(),
    restore_operon_session_tool.TOOL_NAME: restore_operon_session_tool.tool_descriptor(),
    # Agent Teams pivot Land 2: inbox-write primitive surface.
    send_to_member_tool.TOOL_NAME: send_to_member_tool.tool_descriptor(),
}

_log = logging.getLogger(__name__)


def _resolve_caller_role() -> str | None:
    """Resolve the calling MCP subprocess's bound role via env handle.

    Returns the role string (lowercase snake_case per SPEC.md section 5),
    or None if no identity context is available -- env var unset, handle
    file missing, or handle record malformed. The list-tools filter
    treats None as least-privilege (All-class tools only).

    Defensive: never raises. Identity-resolution exceptions are caught
    and demoted to None so a corrupt handle file cannot brick the
    server's `tools/list` response.
    """
    handle = identity.read_env_handle()
    if handle is None:
        return None
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        _log.warning("tools/list filter: failed to read handle %s: %s", handle, exc)
        return None
    if record is None:
        return None
    role = record.get("role")
    if not isinstance(role, str) or not role:
        return None
    return role


def _filter_tools_for_role(role: str | None) -> list[mcp_types.Tool]:
    """Apply the SPEC section 7.1 role-scoped filter to the catalog.

    - Coordinator: All + Coordinator-only tools.
    - Any other role (or unbound subprocess): All-class tools only
      (least privilege; never advertise Coordinator tools to a
      non-Coordinator subprocess, and never to an unbound caller).
    - HIDDEN tools are always omitted -- the hook handler reaches them
      by qualified name.
    """
    visible: list[mcp_types.Tool] = []
    is_coordinator = role == COORDINATOR_ROLE
    for name, descriptor in _TOOL_DESCRIPTORS.items():
        visibility = _TOOL_VISIBILITY.get(name, _VISIBILITY_HIDDEN)
        if visibility == _VISIBILITY_HIDDEN:
            continue
        if visibility == _VISIBILITY_ALL:
            visible.append(descriptor)
            continue
        if visibility == _VISIBILITY_COORDINATOR_ONLY and is_coordinator:
            visible.append(descriptor)
            continue
        # Coordinator-only tool seen by non-Coordinator: skip.
    return visible


def _build_server() -> Server:
    """Create the MCP `Server` instance with tool handlers attached.

    Land 4 removed the legacy mailbox watch loop; the lead's MCP
    server is now stdio-only -- no concurrent background task. The
    Anthropic runtime drives inbox delivery; operon's surface is
    write-side only (inbox.write_to_member_inbox + send_to_member
    + advance_phase team_broadcast).
    """
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        # Role-scoped filter per SPEC section 7.1. Resolved fresh on
        # every list_tools call so a subprocess that gets its identity
        # bound mid-session (e.g. SessionStart hook fires after first
        # ping) picks up the new visibility on the next request.
        role = _resolve_caller_role()
        _log.debug("tools/list: resolved caller role=%r", role)
        return _filter_tools_for_role(role)

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[mcp_types.TextContent]:
        _log.debug("tools/call: name=%r", name)
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name!r}")
        return await handler(arguments)

    return server


async def _run() -> None:
    """Run the stdio MCP server until the peer disconnects.

    Land 4: the legacy filesystem-watch loop was removed. The lead's
    operon MCP subprocess now runs only the stdio MCP protocol loop;
    no concurrent background task. Anthropic's runtime watches the
    inbox files; operon only writes to them.

    Bootstrap still resolves the Coordinator identity for the
    lead's subprocess (and any fixture handle the user has bound)
    so the Coordinator-only tools find a valid role on first call.
    """
    _maybe_configure_stderr_logging()
    _log.info("operon MCP server boot: pid=%d cwd=%s", os.getpid(), os.getcwd())
    _log.debug(
        "boot env: %s=%r",
        identity.ENV_HANDLE_VAR,
        os.environ.get(identity.ENV_HANDLE_VAR),
    )

    # Phase 14: auto-bootstrap a default Coordinator identity if no env
    # handle is set and the project has no existing operon-session.
    # No-op when OPERON_AGENT_HANDLE is already exported (manually-bound
    # fixtures preserve existing behavior). Failures are logged and
    # non-fatal; whoami will surface the missing identity to the LLM
    # on first call.
    try:
        bootstrap_handle = bootstrap.auto_bootstrap_if_needed()
        _log.debug(
            "bootstrap resolved: handle=%r (cached=%r)",
            bootstrap_handle,
            identity.get_cached_handle(),
        )
    except Exception as exc:
        _log.warning("bootstrap raised unexpectedly: %s", exc)

    server = _build_server()
    init_options = server.create_initialization_options(
        experimental_capabilities=EXPERIMENTAL_CAPABILITIES,
    )
    async with stdio_server() as (read_stream, write_stream):
        _log.info("stdio transport open")
        await server.run(read_stream, write_stream, init_options)


def main() -> None:
    """Synchronous entry point referenced by `pyproject.toml`.

    Also the target of `python -m operon_mcp_server.server`.
    """
    anyio.run(_run)


if __name__ == "__main__":
    main()
