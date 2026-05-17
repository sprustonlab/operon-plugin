"""operon-plugin MCP server entry point.

Phase 3: single-agent spawn. Registered tools (visibility per SPEC.md
section 7.1):

- `whoami` -- visible to All roles.
- `spawn_agent` -- visible to Coordinator only.
- `bind_handle` -- HIDDEN (hook-only; routed by qualified name).

Role-scoped `tools/list` filtering is applied per SPEC.md section 7.1:
each MCP subprocess returns only the tools visible to its env-anchored
role. The bound role is resolved by reading `OPERON_AGENT_HANDLE`,
looking up `_handles/<handle>.json`, and reading the `role` field. If no
identity context is available (env unset, handle file missing) the
server defaults to the least-privilege view (All-class tools only).

Per SPEC.md section 16 the server will eventually also host the
`claude/channel` push path, `elicitation/create` issuance, and a
`watchdog` filesystem-watch loop. None of that is wired here yet.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio
import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import identity
from .tools import bind_handle as bind_handle_tool
from .tools import spawn_agent as spawn_agent_tool
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
_TOOL_VISIBILITY: dict[str, str] = {
    whoami_tool.TOOL_NAME: _VISIBILITY_ALL,
    spawn_agent_tool.TOOL_NAME: _VISIBILITY_COORDINATOR_ONLY,
    bind_handle_tool.TOOL_NAME: _VISIBILITY_HIDDEN,
}

#: Routing table: tool name -> handler coroutine. Includes HIDDEN tools
#: so that hook-driven calls (e.g. `mcp__operon__bind_handle`) still find
#: a handler even though the tool is not listed in `tools/list`.
_TOOL_HANDLERS = {
    whoami_tool.TOOL_NAME: whoami_tool.call,
    spawn_agent_tool.TOOL_NAME: spawn_agent_tool.call,
    bind_handle_tool.TOOL_NAME: bind_handle_tool.call,
}

#: Tool descriptors keyed by name (used by the role-scoped filter to
#: build the `tools/list` response). HIDDEN tools are omitted from this
#: table -- they are never advertised, only dispatched via qualified
#: name from hooks.
_TOOL_DESCRIPTORS: dict[str, mcp_types.Tool] = {
    whoami_tool.TOOL_NAME: whoami_tool.tool_descriptor(),
    spawn_agent_tool.TOOL_NAME: spawn_agent_tool.tool_descriptor(),
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
    """Create the MCP `Server` instance with tool handlers attached."""
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        # Role-scoped filter per SPEC section 7.1. Resolved fresh on
        # every list_tools call so a subprocess that gets its identity
        # bound mid-session (e.g. SessionStart hook fires after first
        # ping) picks up the new visibility on the next request.
        role = _resolve_caller_role()
        return _filter_tools_for_role(role)

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[mcp_types.TextContent]:
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name!r}")
        return await handler(arguments)

    return server


async def _run() -> None:
    """Run the stdio MCP server until the peer disconnects."""
    server = _build_server()
    # Use the SDK helper: it auto-derives `capabilities.tools` from the
    # registered `@server.list_tools()` handler and nests our
    # `claude/channel` extension under `experimental` per MCP spec. The
    # helper also sets `server_name` / `server_version` from the
    # `Server(name, version=...)` arguments.
    init_options = server.create_initialization_options(
        experimental_capabilities=EXPERIMENTAL_CAPABILITIES,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def main() -> None:
    """Synchronous entry point referenced by `pyproject.toml`.

    Also the target of `python -m operon_mcp_server.server`.
    """
    anyio.run(_run)


if __name__ == "__main__":
    main()
