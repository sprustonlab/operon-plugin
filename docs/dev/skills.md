# Skills

A skill is a user-invocable slash command (`/<name>`) whose body proxies
to one or more MCP tools. operon ships three: `/project_team`, `/restore`,
and `/rules`. Each is a `SKILL.md` under
`plugins/operon-plugin/skills/<name>/`. This page owns the skill anatomy,
the script-injection pattern, and how role identity files become Claude
Code subagent definitions.

For the user-facing list of commands and what they do, see the
[MCP Tools Reference](../user-guide/mcp-tools-reference.md).

## Anatomy of a skill

A `SKILL.md` is Markdown with YAML frontmatter. The fields operon uses:

```yaml
---
name: project_team
description: Activate the project_team workflow as a new operon-session. ...
disable-model-invocation: true     # the model cannot self-invoke; only the user typing /project_team
user-invocable: true
allowed-tools:
  - "Bash(python *)"
  - mcp__operon__activate_workflow
---
```

- `name` -- the command; `/project_team` invokes this file.
- `disable-model-invocation` -- when true, only a user typing the slash
  command triggers the skill; the model cannot call it on its own
  initiative.
- `allowed-tools` -- the bounded tool set the skill body may use. This is
  the security boundary: the body can dispatch only these tools.

The body (below the frontmatter) is the instructions the model follows
when the command runs. It is prose, not code -- the model reads the
script output and calls the named MCP tool.

## The script-injection pattern

The skills deliberately keep the model out of any decision the user
should own. Two patterns realize this.

**Client-side validation, then dispatch (`/project_team`).** The body
runs a stdlib-only script as a dynamic-context injector:

```
!`python ${CLAUDE_PLUGIN_ROOT}/skills/activate/scripts/activate.py project_team $ARGUMENTS`
```

`activate.py` validates the `run_name` client-side (filesystem-safe, no
leading dot, <=50 chars) and emits exactly one line: either

```
OPERON_DISPATCH tool=mcp__operon__activate_workflow workflow_id=project_team run_name=my_refactor
```

or `ERROR: <reason>`. The body parses the `OPERON_DISPATCH` line and calls
`mcp__operon__activate_workflow(workflow_id=..., run_name=...)` with
exactly those args, or relays the `ERROR` verbatim and stops. The model's
discretion is bounded to "call the tool with the args the script printed"
-- there is no free-form role-framing, so a channel message cannot
prompt-inject an activation.

`activate.py` dispatches *through the model* rather than calling the MCP
tool itself because the plugin install layout ships only the bin shim, not
the `operon_mcp_server` source, under the plugin cache -- a client-side
script cannot import the tool modules in-process, and there is no separate
JSON-RPC endpoint. Routing the validated dispatch through the model's
`allowed-tools` reaches the same end state deterministically.

**Tool-owns-the-dialog (`/restore`, `/rules`).** These call a single MCP
tool with no arguments and let the tool run the whole flow in-process:

- `/restore` calls `mcp__operon__restore_operon_session()`. The tool
  enumerates runs, issues the `elicitation/create` picker, confirms any
  worker closes, and swaps `_active.json` -- all server-side. The body
  must *not* call `list_operon_sessions` first and pass a chosen name;
  that would surface the choice through the model and defeat the
  injection pattern.
- `/rules` calls `mcp__operon__get_applicable_rules()`. The tool projects
  the merged Rules + advance-checks + escape tokens onto the caller's
  `(role, phase)` and returns a rendered `## Constraints` markdown block
  the body relays verbatim. See
  [Rules and Enforcement](rules-and-enforcement.md).

In both cases the elicitation renders only in the Coordinator's foreground
session; workers (`claude --bg`) have no TTY and get a `no_selection`
outcome.

## Adding a skill

1. Create `plugins/operon-plugin/skills/<name>/SKILL.md` with the
   frontmatter above. Set `allowed-tools` to the minimal set the body
   needs.
2. Write the body as instructions: what to run, how to parse the output,
   which tool to call, and what *not* to do (the "What NOT to do" section
   in each shipped skill is load-bearing -- it keeps the model from
   re-introducing a dialog the tool already owns).
3. If the skill needs client-side validation, reuse
   `skills/activate/scripts/activate.py` (it takes the workflow id as
   `argv[1]`) or add a stdlib-only script beside it. Keep validation
   logic mirrored with the server-side tool's so the user sees the same
   error vocabulary regardless of which layer caught it.

## Roles become Claude Code subagents

Workflow roles and skills are distinct surfaces, but they meet at one
seam: `plugins/operon-plugin/src/operon_mcp_server/subagent_install.py`. operon's roles are
plain `identity.md` files; Claude Code's spawn mechanism expects subagent
definitions at `~/.claude/agents/<role>.md` with required frontmatter.
`install_workflow_subagents` (called from `activate_workflow`) compiles
each `<workflow_root>/<role>/identity.md` into that shape:

- It strips any existing frontmatter, synthesizes a `name` +
  `description`, sets `model: inherit` (the role uses the lead's model),
  and omits `tools` (roles inherit the full tool set).
- It appends two footers to every compiled role: a **lifecycle-protocol**
  footer (refuse `shutdown_request` unless the lead asks, so teammates
  stay visible in the TUI) and an **operon-context-queries** footer
  documenting the `[OPERON_QUERY]` inbox channel for identity / phase /
  rules lookups.

It also writes a no-op `operon.md` stub and registers operon as an
external member in `~/.claude/teams/<team>/config.json`, so the runtime
roster has a real target for `from: "operon"` reply routing without ever
spawning the stub. The transformation is intentionally not a file copy --
the two schemas differ, and this module is the seam between them.

To **add a role**, you only add a `<role>/identity.md` under the workflow
directory; the next `activate_workflow` compiles and installs it. See
[Contributing](contributing.md) for the full add-a-role checklist and
[Workflow Engine](workflow-engine.md) for how per-phase role briefs are
delivered.
