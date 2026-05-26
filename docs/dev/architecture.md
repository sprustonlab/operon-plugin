# Architecture

This is the **Contributor/Architecture Guide** for operon-plugin: how the
MCP server, workflow engine, guardrail Rules, hooks, and skills fit
together, and how to extend each one.

If you are here to *use* operon rather than change it, start with the
[User Guide](../user-guide/install.md) instead. The pages in this guide
assume you will be reading and editing source.

## The big picture

operon turns a single Claude Code session into a guided, multi-phase team
workflow. It plugs into Claude Code at three points:

- an **MCP server** (`src/operon_mcp_server/`) that exposes operon's tools,
  runs the phase engine, and holds each run's state;
- a **PreToolUse hook** that evaluates guardrails before any side-effectful
  tool call and injects identity and prior-transcript recall into spawned
  teammates;
- **skills** -- the `/project_team`, `/restore`, and `/rules` slash commands
  the user invokes.

On top of those, a **workflow manifest** defines the ordered phases and the
gates between them; **guardrail Rules** constrain what each agent may do;
and a **team** of role-scoped agents -- a Coordinator plus the specialists
it spawns -- carries the work, coordinating through an inbox channel. The
hook and the MCP server run as independent subprocesses that share one
run's state on disk under `.operon/`.

The rest of this page pins each surface to its place on disk and the page
that documents it.

## Repo layout

!!! note "Paths live under `plugins/operon-plugin/`, not the repo root"
    operon-plugin is a [single-plugin
    marketplace](../user-guide/install.md): the repository is both the
    Claude Code marketplace manifest and the one plugin it ships. Almost
    every plugin surface lives **nested** under
    `plugins/operon-plugin/`. The one exception is the MCP server Python
    package, which lives at the repo root in `src/`. When you cite a path
    in a doc or a comment, use the real nested path -- citing a repo-root
    path for a nested surface is the single most common way these docs
    drift.

```
operon-plugin/                          # repo root == marketplace root
  .claude-plugin/
    marketplace.json                    # marketplace manifest (lists the plugin)
  pyproject.toml                        # MCP server package + dev deps (uv-managed)
  mkdocs.yml                            # this docs site
  src/
    operon_mcp_server/                  # the MCP server package (repo ROOT, not nested)
      server.py                         # entry point: tool registry + role-scoped tools/list
      workflow.py                       # manifest loader + phase engine + advance protocol
      rules.py                          # guardrail Rule parsing, matching, evaluation, tokens
      bootstrap.py                      # auto-bootstrap a Coordinator identity at startup
      identity.py                       # handle records (OPERON_AGENT_HANDLE -> role)
      paths.py                          # .operon/ run-dir path resolution (leaf)
      inbox_reader.py                   # [OPERON_QUERY] inbox-channel reader loop
      subagent_install.py               # compile role identity.md -> ~/.claude/agents/<role>.md
      tools/                            # one file per MCP tool (TOOL_NAME + call + descriptor)
      checks/                           # advance-check implementations (leaf)
  plugins/
    operon-plugin/                      # the plugin Claude Code loads
      .claude-plugin/plugin.json        # plugin manifest (name, version, author)
      .mcp.json                         # registers server "operon" -> bin/operon-mcp-server
      bin/
        operon-mcp-server               # launcher shim (bash); resolves uv -> python3 -> python
        operon-mcp-server.cmd           # Windows launcher
      hooks/
        hooks.json                      # PreToolUse matcher -> pretooluse-wrapper
        pretooluse.py                   # guardrail enforcement + spawn-prompt injection
        pretooluse-wrapper(.cmd)        # interpreter-resolution wrapper for the hook
      rules.yaml                        # plugin-tier guardrail Rules
      skills/
        project_team/SKILL.md           # /project_team
        restore/SKILL.md                # /restore
        rules/SKILL.md                  # /rules
        activate/scripts/activate.py    # shared client-side validator for activation
      workflows/
        project_team/
          project_team.yaml             # THE manifest: 10 phases + workflow-embedded rules
          coordinator/                  # per-role identity.md + per-phase brief .md files
          composability/  implementer/  skeptic/  terminology/  ...
        _smoke/_smoke.yaml              # throwaway 2-phase workflow for tests
  tests/
    scenarios/project_team_workflow.py  # the end-to-end scenarios
    _harness/                           # pty driver, transcript observer, idle predicates
    fixtures/                           # filesystem seeds for scenarios
  operon-runs/                          # project_team work products (tracked on develop only)
```

Two runtime directories are *not* in the tree above because they are
created at run time, never committed:

- `<project>/.operon/<run_name>/` -- one
  [operon-session](../user-guide/sessions.md)'s phase state, roster,
  handles, escape tokens, and audit log. Gitignored on every branch.
- `~/.claude/agents/<role>.md` and `~/.claude/teams/<team>/config.json`
  -- the Claude Code subagent definitions and team config that
  `activate_workflow` writes so the runtime can spawn workers. See
  [Skills](skills.md) and [Workflow Engine](workflow-engine.md).

The develop/main branch split for `operon-runs/` and `.operon/` is
documented in [Contributing](contributing.md).

## How a tool call flows through operon

A single side-effectful tool call (say, a worker runs `git push`) touches
three of operon's surfaces in order:

1. **Claude Code fires the `PreToolUse` hook** before the tool runs. The
   hook (`plugins/operon-plugin/hooks/pretooluse.py`) resolves the
   caller's `(role, current_phase)`, loads the merged guardrail Rules,
   and evaluates them. A `deny` Rule blocks the call with a hint to
   request an override; a `warn` Rule blocks with a hint to acknowledge;
   a `log` Rule records an audit row and allows. See [Hooks](hooks.md)
   and [Rules and Enforcement](rules-and-enforcement.md).
2. **The MCP server handles operon's own tool calls.** When the model
   calls `mcp__operon__advance_phase` (or any operon tool), the request
   goes to the MCP server subprocess, which routes it to the matching
   handler in `src/operon_mcp_server/tools/`. See [MCP
   Server](mcp-server.md).
3. **The workflow engine runs the phase machinery.** `advance_phase`
   asks `workflow.py` to evaluate the current phase's advance-checks and,
   on success, atomically rewrite `phase_state.json`. See [Workflow
   Engine](workflow-engine.md).

Hooks and the MCP server are independent subprocesses. The hook never
calls operon's MCP tools; it imports the same Python package directly via
a `PYTHONPATH` the wrapper sets. Both reach the same `.operon/<run_name>/`
state on disk.

## Surface map

Each page below documents one surface's schema and internals.

| Surface | Source of truth | Contributor page |
|---------|-----------------|-------------------|
| MCP server + tools + checks | `src/operon_mcp_server/{server,tools,checks}.py` | [MCP Server](mcp-server.md) |
| Workflows + phases + advance protocol | `plugins/operon-plugin/workflows/<id>/<id>.yaml`, `src/operon_mcp_server/workflow.py` | [Workflow Engine](workflow-engine.md) |
| Guardrail Rules + enforcement tiers | `plugins/operon-plugin/rules.yaml`, `src/operon_mcp_server/rules.py` | [Rules and Enforcement](rules-and-enforcement.md) |
| Hooks (PreToolUse) | `plugins/operon-plugin/hooks/` | [Hooks](hooks.md) |
| Skills (slash commands) | `plugins/operon-plugin/skills/<name>/SKILL.md` | [Skills](skills.md) |
| Tests + harness | `tests/` | [Testing](testing.md) |
| Branch model + adding a phase/rule/role | git + manifests | [Contributing](contributing.md) |

The enumerated **MCP-tool reference table** is in the
[MCP Tools Reference](../user-guide/mcp-tools-reference.md); this guide
covers how the tools are built and registered.
