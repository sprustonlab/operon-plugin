"""operon-plugin MCP server entry point.

Phase 1 scaffold: completes the MCP `initialize` handshake over stdio and
advertises the server name `operon` plus the non-standard
`claude/channel` capability so that Claude Code recognizes this plugin's
MCP server as connected (see SPEC.md sections 6 and 16).

Tool registration (`tools/list`) is intentionally empty at this phase.
Tools land in Phases 2-9 under `operon_mcp_server.tools.*`. Per
SPEC.md section 16 this module will eventually also host:

- `tools/list` filtering by role (section 7.1)
- `claude/channel` push and `elicitation/create` issuance
- A `watchdog`-based filesystem-watch loop (section 6.6)
- Per-subprocess identity binding from `OPERON_AGENT_HANDLE` (section 6.5)

None of that is wired here yet; Phase 1 only proves the handshake.
"""

from __future__ import annotations

import anyio

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import ServerCapabilities

#: MCP server name advertised in the `initialize` response. Must match
#: the key under `mcpServers` in `plugins/operon-plugin/.mcp.json` so
#: that Claude Code can correlate the registered server with this
#: subprocess.
SERVER_NAME = "operon"


def _build_capabilities() -> ServerCapabilities:
    """Construct the capabilities advertised at `initialize` time.

    The `claude/channel` capability is a Claude-Code-specific extension
    that lets the server push messages into running sessions (see
    SPEC.md section 6 and section 7.2). `ServerCapabilities` is a
    pydantic model with `extra='allow'`, so we attach the capability as
    a top-level field -- mirroring how it is declared in
    `plugins/operon-plugin/.mcp.json`.
    """
    return ServerCapabilities(**{"claude/channel": {}})


def _build_server() -> Server:
    """Create the MCP `Server` instance.

    Kept tiny on purpose: Phase 1 only needs the handshake. Future
    phases attach `list_tools` / `call_tool` handlers here (via the
    decorator API on `Server`).
    """
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list:  # pragma: no cover - protocol stub
        # No tools registered yet; tools come in Phases 2-9.
        return []

    return server


async def _run() -> None:
    """Run the stdio MCP server until the peer disconnects."""
    server = _build_server()
    init_options = InitializationOptions(
        server_name=SERVER_NAME,
        server_version=server.version or "0.0.1",
        capabilities=_build_capabilities(),
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
