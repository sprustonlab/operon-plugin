# operon-plugin

**Operon: A Guided, Self-Revisable plugin for AI Research Software
Operations in Claude Code.**

operon-plugin brings multi-agent orchestration -- a team of agents, a
phase-structured workflow engine, and guardrail Rules with override /
acknowledge -- into Claude Code as a native plugin. The team runs
in-process inside your Claude Code session, so it draws on your
interactive subscription rather than a separate SDK credit bucket.

This repository is both a **single-plugin marketplace** and the plugin
it ships:

```
.claude-plugin/marketplace.json   # marketplace manifest (lists ./plugins/operon-plugin)
plugins/operon-plugin/            # the plugin
src/operon_mcp_server/            # Python MCP server package
```

## What operon gives you

- A **workflow engine** that walks a project through ordered
  [phases](user-guide/workflows-and-phases.md), gating each transition
  behind advance-checks.
- A **team** of [agents](user-guide/agents-and-messaging.md) led by a
  Coordinator, with inter-agent messaging.
- **[Guardrail Rules](user-guide/guardrails.md)** that deny, warn, or
  silently log tool calls, with user-gated overrides and self-service
  acknowledgments.
- **[operon-sessions](user-guide/sessions.md)**: each run gets its own
  phase state, roster, escape tokens, and artifact directory on disk.
- A set of **[MCP tools and slash commands](user-guide/mcp-tools-reference.md)**
  for driving and introspecting all of the above.

## Where to start

| You want to... | Go to |
| -------------- | ----- |
| Install the plugin | [Install](user-guide/install.md) |
| Run your first workflow | [Quickstart](user-guide/quickstart.md) |
| Understand the phase flow | [Workflows and Phases](user-guide/workflows-and-phases.md) |
| Understand the team and messaging | [Agents and Messaging](user-guide/agents-and-messaging.md) |
| See what a deny/warn feels like | [Guardrails](user-guide/guardrails.md) |
| Manage runs on disk | [Sessions](user-guide/sessions.md) |
| Look up a tool or command | [MCP Tools Reference](user-guide/mcp-tools-reference.md) |

## Two guides

This site has two halves:

- **User Guide** (you are here) -- how to install, run, and live with
  operon: what each phase does, what the guardrails feel like, how
  sessions work.
- **[Contributor/Architecture Guide](dev/architecture.md)** -- the
  internals: MCP server, workflow engine, rule enforcement, hooks, and
  skills. This is the authoritative design reference for the codebase.

## Status

Pre-release. The MCP server, workflows, hooks, rules, and skills land
phase by phase; see `CHANGELOG.md` in the repository for the
implementation log.
