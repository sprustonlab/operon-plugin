"""operon-plugin MCP server entry point.

Phase 2: identity binding. The server now registers two tools:

- `whoami` (visible to All roles per SPEC.md section 7.1) -- composes
  the canonical identity tuple for the calling subprocess.
- `bind_handle` (HIDDEN per SPEC.md section 7.1) -- invoked only by the
  `SessionStart` hook via the `type: mcp_tool` handler form. Not
  advertised in `tools/list`; the MCP framework routes
  `mcp__operon__bind_handle` to its handler regardless.

The handshake from Phase 1 is preserved (`operon` server name and
`claude/channel` capability). Per SPEC.md section 16 this module will
eventually also host `tools/list` filtering by bound role, the
`claude/channel` push path, `elicitation/create` issuance, and a
`watchdog` filesystem-watch loop. None of that is wired here yet.
"""

from __future__ import annotations

from typing import Any

import anyio
import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .tools import bind_handle as bind_handle_tool
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


#: Routing table: tool name -> (handler coroutine). Includes HIDDEN
#: tools so that hook-driven calls (e.g. `mcp__operon__bind_handle`)
#: still find a handler even though the tool is not listed in
#: `tools/list`.
_TOOL_HANDLERS = {
    whoami_tool.TOOL_NAME: whoami_tool.call,
    bind_handle_tool.TOOL_NAME: bind_handle_tool.call,
}

#: Tools advertised in `tools/list`. Excludes HIDDEN tools per
#: SPEC.md section 7.1. Future phases will further filter this list by
#: the bound role of the calling subprocess (section 6.5 step 7).
_VISIBLE_TOOLS = [
    whoami_tool.tool_descriptor(),
]


def _build_server() -> Server:
    """Create the MCP `Server` instance with tool handlers attached."""
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        # HIDDEN tools (e.g. bind_handle) are intentionally excluded
        # per SPEC section 7.1; the hook handler reaches them via
        # qualified name `mcp__operon__<tool>` regardless.
        return list(_VISIBLE_TOOLS)

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
