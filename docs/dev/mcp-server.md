# MCP Server

The MCP server is the Python package `src/operon_mcp_server/`. Claude Code
launches one instance per session and talks to it over stdio; the server
exposes operon's MCP tools and drives the phase engine and guardrail
evaluation. This page covers how the server is built, how it registers and
filters tools, and how to add a new tool or advance-check.

For the *catalog* of tools (names, arguments, what each returns), see the
[MCP Tools Reference](../user-guide/mcp-tools-reference.md).

## Where it lives and how it launches

The server package is the one plugin surface at the repo root rather than
under `plugins/operon-plugin/`:

- Package: `src/operon_mcp_server/`
- Registration: `plugins/operon-plugin/.mcp.json` declares one server
  named `operon` whose `command` is
  `${CLAUDE_PLUGIN_ROOT}/bin/operon-mcp-server`.
- Launch: the [bin shim](../user-guide/install.md) resolves an
  interpreter via the `uv -> python3 -> python` ladder, prepends
  `${CLAUDE_PLUGIN_ROOT}/src` to `PYTHONPATH`, and runs `python -m
  operon_mcp_server.server`. No `pip install -e .` is required -- the
  shim makes the package importable from the bundled `src/` tree.

The server name `operon` is what makes every tool addressable as
`mcp__operon__<name>`. It is fixed in three places that must agree:
`SERVER_NAME` in `server.py`, the `mcpServers` key in `.mcp.json`, and the
`mcp__operon__` prefix the [hook](hooks.md) and [skills](skills.md) use.

## Server construction

`server.py` builds an MCP SDK `Server` and attaches two handlers:

- `@server.list_tools()` returns the role-filtered tool catalog (see
  below). It re-resolves the caller's role on every call so a subprocess
  whose identity is bound mid-session picks up the new visibility on the
  next request.
- `@server.call_tool()` looks the tool name up in `_TOOL_HANDLERS` and
  awaits the matching coroutine.

On boot, `_run()` does two extra things before opening the stdio
transport:

1. **Auto-bootstrap.** `bootstrap.auto_bootstrap_if_needed()` resolves or
   creates a Coordinator identity so the Coordinator-only tools find a
   valid role on the first call. See [the bootstrap
   section](#identity-and-bootstrap) below.
2. **Inbox-channel reader.** `inbox_reader.run_forever` is scheduled on
   the same anyio task group as the protocol loop. It polls the team
   inbox for `[OPERON_QUERY] <command>` messages from teammates and
   writes verified replies back. See [the identity-query
   section](#teammate-identity-the-operon_query-channel).

## Tool registration

Each tool is one file under `src/operon_mcp_server/tools/`. The
convention every tool file follows:

- `TOOL_NAME` -- the bare name (e.g. `"advance_phase"`), addressed as
  `mcp__operon__advance_phase` by callers.
- `tool_descriptor()` -- returns the `mcp.types.Tool` (name, description,
  `inputSchema`) advertised in `tools/list`.
- `async def call(arguments)` -- the handler. Returns a list of
  `TextContent`; tool results are JSON-serialized into the text field.

`server.py` wires each tool into three tables:

- `_TOOL_HANDLERS` -- name -> `call` coroutine. **Includes hidden tools**
  so hook-driven calls (e.g. `evaluate`, `bind_handle`) still dispatch.
- `_TOOL_DESCRIPTORS` -- name -> descriptor. **Omits hidden tools** --
  they are never advertised.
- `_TOOL_VISIBILITY` -- name -> visibility tier.

To **add a tool**: create `tools/<name>.py` with the three members above,
then add an entry to all three tables in `server.py` (descriptor only if
the tool is visible). Forgetting the visibility entry defaults the tool to
hidden.

## Role-scoped `tools/list`

Tools have three visibility tiers, enforced by `_filter_tools_for_role`:

| Tier | Constant | Advertised to |
|------|----------|---------------|
| all | `_VISIBILITY_ALL` | every role, and unbound callers |
| coordinator-only | `_VISIBILITY_COORDINATOR_ONLY` | only when the bound role is `coordinator` |
| hidden | `_VISIBILITY_HIDDEN` | never advertised; reachable only by qualified name from a hook |

The filter resolves the caller's role from `OPERON_AGENT_HANDLE` ->
`_handles/<handle>.json` -> `role`. An unbound caller (no env handle, or a
missing/malformed handle file) gets the least-privilege view: all-class
tools only, never the Coordinator-only set. Role resolution never raises;
any exception demotes to `None` so a corrupt handle file cannot brick
`tools/list`.

The tools registered today and their tiers (see `_TOOL_VISIBILITY` in
`server.py` for the authoritative list):

- **all:** `whoami`, `get_phase`, `get_applicable_rules`,
  `get_agent_info`, `request_override`, `acknowledge_warning`,
  `list_operon_sessions`, `send_to_member`.
- **coordinator-only:** `activate_workflow`, `set_artifact_dir`,
  `advance_phase`, `restore_operon_session`.
- **hidden:** `bind_handle`, `evaluate`.

`evaluate` is the guardrail-evaluation tool the design originally routed
through MCP; it is now hidden because the [PreToolUse hook](hooks.md)
calls `rules._evaluate` in-process instead. It stays in `_TOOL_HANDLERS`
for compatibility but is never advertised.

## Identity and bootstrap

Every tool that needs to know who is calling reads the **handle**: the
env var `OPERON_AGENT_HANDLE` names a record under
`<run-dir>/_handles/<handle>.json` that binds a running agent to its role,
run, and session. The model cannot supply identity via tool arguments;
identity is env-anchored.

`bootstrap.py` resolves identity at server startup in priority order:

1. `OPERON_AGENT_HANDLE` is set **and** the handle file exists in this
   project's active run -> use it (the spawn / fixture path).
2. Otherwise, if the active run has exactly one coordinator handle file
   -> adopt it (cache only; no disk write).
3. Otherwise -> create a fresh `<project>/.operon/<default>/` with a new
   Coordinator handle and the four canonical files
   (`_active.json`, `_handles/<uuid>.json`, `phase_state.json`,
   `agents.json`).

A stale env handle from a different project is detected (handle file not
found) and ignored, falling through to discovery / fresh bootstrap. This
is what lets a user launch `claude` in a fresh project and run
`/project_team my_run` immediately, with no manual setup step.

## Teammate identity: the `[OPERON_QUERY]` channel

Under Claude Code's in-process Agent Teams, operon's MCP server is a
**singleton in the lead's process**: every teammate's MCP call multiplexes
through the lead's stdio transport, and Anthropic's runtime does not
propagate teammate identity through MCP `_meta`/`clientInfo`. So
`whoami` / `get_agent_info` / `get_applicable_rules` called directly by a
teammate return the **lead's** identity, not the teammate's.

The supported path for a teammate to learn its own identity is the
inbox channel, not the MCP tool:

- The teammate sends a `SendMessage` to the `operon` team-member with
  text `[OPERON_QUERY] whoami` (or `get_agent_info` /
  `get_applicable_rules`).
- `inbox_reader.run_forever` (running in the lead's MCP server) reads the
  message, resolves the sender from the runtime-stamped `from` field
  (which a teammate cannot spoof), and writes the verified reply back to
  the sender's inbox as `[OPERON_REPLY] <command> {...}`.

This is why role definitions installed by [subagent_install](skills.md)
carry a footer instructing teammates to use `[OPERON_QUERY]` rather than
the MCP identity tools. The footer is the steering surface; the inbox
`from` field is the trust anchor.

## Advance-checks (`checks/`)

The `checks/` subpackage is a **leaf module**: it imports only stdlib,
`re`, and `asyncio`, never `workflow.py` or the MCP SDK. It declares the
Check-Engine seam and the five built-in check types. The engine
(`workflow.py`) builds executable `Check` objects from declarations and
injects the transport-tier parameters (the elicitation closure, the
`state.json` path, the `base_dir`) that the leaf module must not import
for itself. See [Workflow Engine](workflow-engine.md#advance-checks) for
the check types and how the engine drives them.

## Module dependency direction

The package keeps a strict no-upward-import discipline so the leaf modules
stay testable in isolation:

- `paths.py`, `checks/protocol.py`, `checks/builtins.py` -- leaves
  (stdlib + `re`). Imported by, but never importing, the engine.
- `workflow.py`, `rules.py` -- engine tier. Import the leaves; do not
  import `tools/`.
- `tools/*.py` -- top tier. Import the engine + leaves.
- `server.py` -- wires the tools together; imported by nothing inside the
  package.

When you add code, keep the arrow pointing down: a leaf that imports the
engine breaks the in-isolation tests and the hook's lightweight import
path.
