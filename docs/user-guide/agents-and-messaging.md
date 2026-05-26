# Agents and Messaging

A workflow run is carried out by a **team** of specialist agents, led by
a **Coordinator**. The Coordinator is your main point of contact, but
you can switch to and talk to any agent on the team directly. This page
defines that vocabulary and shows how the agents work together and talk
to each other.

A **role** is the *type* of a team member -- `coordinator`,
`implementer`, `terminology`, and so on. A role is defined by an
`identity.md` under
`plugins/operon-plugin/workflows/<workflow>/<role>/`. The role's
machine identifier is a lowercase snake_case slug (`terminology`); its
readable name is the H1 title the author writes at the top of that
`identity.md` -- the `terminology` role titles itself **Terminology
Guardian** there, which is why it reads as more than a slug.

An **agent** is a *running instance* of a role within an
[operon-session](sessions.md). The set of agents in one session is the
**team** (or **roster**).

## The team runs in-process

operon's agents run as in-process teammates inside your single Claude
Code session, not as separate processes you manage by hand. The
**Coordinator** runs in the foreground as your main point of contact,
and it spawns the rest of the team using the runtime's own Agent tool.
Spawning stays the Coordinator's job, and you can switch to any spawned
agent to see its work or message it directly.

Because the team is in-process, every agent's MCP traffic is served by
one operon MCP server living in the Coordinator (lead) session. That
single fact shapes how identity and messaging work (below).

## The Coordinator

The **Coordinator** is the orchestrating agent and the workflow's
`main_role`. Its prime directive is *delegate, don't do*: it routes
work to the appropriate team member rather than writing code, designing
interfaces, or writing tests itself. The
[Coordinator-only MCP tools](mcp-tools-reference.md#coordinator-only)
(`activate_workflow`, `set_artifact_dir`, `advance_phase`,
`restore_operon_session`) are held by the Coordinator alone; a worker
that calls one gets a structured rejection.

## Leadership

**Leadership** is the fixed set of four review agents the Coordinator
spawns in the [`leadership` phase](workflows-and-phases.md):

- **Composability**
- **Terminology Guardian**
- **Skeptic**
- **User Alignment**

Beyond Leadership, the `project_team` workflow ships additional roles
(Implementer, Test Engineer, UI Designer, Researcher, and others under
`plugins/operon-plugin/workflows/project_team/`) that the Coordinator
spawns when the work calls for them.

## Messaging between agents

Agents communicate two ways:

- **Runtime SendMessage** -- the primary teammate-to-teammate channel.
  An agent's plain text output is not visible to its teammates; to
  reach another agent it uses the runtime's `SendMessage` tool with the
  recipient's member name. Messages are delivered to the recipient's
  inbox automatically.
- **`mcp__operon__send_to_member`** -- how operon adds context to an
  agent *programmatically*, driven by the workflow itself rather than by
  another agent's reasoning. It lets the workflow place authored content
  into a specific member's working context -- the basis for feeding each
  agent the material its current phase calls for. (Mechanically it writes
  that content to the member's inbox, addressed by the `name` in the team
  config.)

See the [MCP Tools Reference](mcp-tools-reference.md) for the full tool
table.

## How an agent asks operon about itself

A team member gets its own role-scoped identity by querying operon
through the inbox channel:

```text
SendMessage(to="operon", text="[OPERON_QUERY] whoami")
```

operon identifies the asker from the runtime-stamped `from` field on the
message -- which an agent cannot spoof -- and writes the answer back to
that agent's inbox, prefixed `[OPERON_REPLY] <command>`. The same form
returns `get_agent_info` and `get_applicable_rules`, each scoped to the
asking agent's own role and phase. This is the trust anchor agent
identity relies on.

The Coordinator, as the lead, can also call `whoami`, `get_agent_info`,
and `get_applicable_rules` as MCP tools directly. For why the inbox
query is the route for spawned teammates -- and how the runtime stamps
identity onto a message -- see
[MCP Server -> Teammate identity](../dev/mcp-server.md#teammate-identity-the-operon_query-channel).

## Introspecting the roster

Agents read the live roster -- each session's agent and alive-agent
counts -- through the read-only `mcp__operon__list_operon_sessions`
tool. The `/restore` picker surfaces the same per-session data to you
(see [Sessions](sessions.md#listing-and-switching-sessions)).

For the internals of identity handles and how the runtime injects the
`[OPERON IDENTITY]` directive into spawned agents, see the
[Contributor/Architecture Guide -> Architecture](../dev/architecture.md)
and [Hooks](../dev/hooks.md).
