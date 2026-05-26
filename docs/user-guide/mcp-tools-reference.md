# MCP Tools and Commands Reference

This page catalogs every operon MCP tool and every slash command --
what each does, who can call it, and where to read more.

An **MCP tool** is one callable the operon [MCP server](../dev/mcp-server.md)
exposes. Its full address is `mcp__operon__<name>` -- shown in full on
first mention, the bare name thereafter. A **skill** is a user-invocable
`/<name>` command whose body proxies to one or more MCP tools.

## Visibility tiers

operon scopes each tool to the calling agent's role, so a worker never
sees a Coordinator-only tool in its tool list. There are three tiers:

- **All-visible** -- any agent can call it.
- **Coordinator-only** -- advertised only when the calling subprocess's
  bound role is the [Coordinator](agents-and-messaging.md). A worker
  that somehow calls one gets a structured rejection, not silent
  success.
- **Hidden** -- never advertised in the tool list; reached only by the
  [PreToolUse hook](../dev/hooks.md) under its fully qualified name.

The authoritative visibility map lives in
`plugins/operon-plugin/src/operon_mcp_server/server.py` (`_TOOL_VISIBILITY`); the source of
each tool is `plugins/operon-plugin/src/operon_mcp_server/tools/<name>.py`.

## MCP tools

### All-visible

| Tool | Purpose |
| ---- | ------- |
| `mcp__operon__whoami` | Return the bootstrap (lead) identity from the env-anchored handle. Teammates query their own identity through the inbox channel -- see [Agents and Messaging](agents-and-messaging.md#how-an-agent-asks-operon-about-itself). |
| `mcp__operon__get_phase` | Active workflow id, current phase, and artifact dir. |
| `mcp__operon__get_applicable_rules` | The advance-checks, guardrail Rules, and active escape tokens projected for the caller's `(role, phase)`, rendered as a `## Constraints` markdown block. Backs `/rules`. |
| `mcp__operon__get_agent_info` | Aggregator of `whoami` + `get_phase` + `get_applicable_rules`. |
| `mcp__operon__list_operon_sessions` | Enumerate every [operon-session](sessions.md) under `<project>/.operon/`, sorted by `last_active_at`. Read-only. |
| `mcp__operon__request_override` | Request a one-shot [override](guardrails.md#overrides-deny-tier) token for a deny-tier Rule. Elicits user approval. Listed All-visible but runtime-gated to the Coordinator. |
| `mcp__operon__acknowledge_warning` | Issue a TTL-bounded [ack](guardrails.md#acks-warn-tier) token (default 60s) for a warn-tier Rule. No user involvement; any agent may call it. |
| `mcp__operon__send_to_member` | Write an operon-authored entry to a team member's inbox by member `name`. See [Agents and Messaging](agents-and-messaging.md). |

### Coordinator-only

| Tool | Purpose |
| ---- | ------- |
| `mcp__operon__activate_workflow` | Create a new operon-session -- bootstrapping its runtime state under `<project>/.operon/<run_name>/` (a session [spans two trees](sessions.md#run-state-vs-work-products)). Destructive: closes alive workers in the current run (with confirmation). Backs `/project_team`. |
| `mcp__operon__set_artifact_dir` | Persist the operon-session's [artifact_dir](sessions.md#the-artifact-dir) pointer to `state.json`. |
| `mcp__operon__advance_phase` | Evaluate the current [phase](workflows-and-phases.md)'s advance-checks (AND semantics, short-circuit on first failure) and, on all-pass, advance to the next phase. |
| `mcp__operon__restore_operon_session` | Switch the active pointer to an existing run-dir. Backs `/restore`. |

### Hidden (hook-only / internal)

| Tool | Purpose |
| ---- | ------- |
| `mcp__operon__bind_handle` | Bind a running subprocess to its role/run handle. Reached only by the runtime. |
| `mcp__operon__evaluate` | The [PreToolUse hook](../dev/hooks.md)'s in-process Rule projection. Never user-callable. |

For the internal schema and dispatch of each tool, see the
[Contributor/Architecture Guide -> MCP Server](../dev/mcp-server.md).

## Slash commands (skills)

Each skill is a script-injection command: its body proxies to MCP
tools so the model cannot be prompt-injected into the action. Source:
`plugins/operon-plugin/skills/<name>/SKILL.md`.

| Command | Backing tool(s) | Purpose |
| ------- | --------------- | ------- |
| `/project_team [run_name]` | `mcp__operon__activate_workflow` | Activate the `project_team` workflow as a new operon-session. The shared `activate.py` normalizes the run_name to a canonical slug (lowercase, hyphens) and validates it client-side before the tool call. See [Quickstart](quickstart.md) and [Workflows and Phases](workflows-and-phases.md). |
| `/restore [run_name]` | `mcp__operon__list_operon_sessions` + `mcp__operon__restore_operon_session` | Switch the active operon-session, with a picker when no run_name is given. See [Sessions](sessions.md). |
| `/rules [agent_name]` | `mcp__operon__get_applicable_rules` | Show the active Rules, advance-checks, and escape tokens for the calling agent's current `(role, phase)`. See [Guardrails](guardrails.md). |

!!! note "`/rules` cross-agent inspection"
    `/rules` currently introspects only the calling agent's own
    `(role, phase)`. Passing an agent name returns
    `cross_agent_not_implemented`; the skill relays that verbatim.

There is also an internal `activate` skill directory
(`skills/activate/scripts/activate.py`, launched via `activate-wrapper`);
it is the shared run_name normalizer/validator that `/project_team`
invokes, not a separate user command.
