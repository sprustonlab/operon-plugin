# Quickstart

The main entry point to operon-plugin is a **workflow** -- a structured,
multi-phase process carried out by a team of specialist agents, which you
invoke with a slash command like `/project_team`. This quickstart runs
that bundled `project_team` workflow: it takes a project from idea to
sign-off across ten phases, with you approving the key decisions along
the way. Assumes the plugin is installed -- if not, see
[Install](install.md).

## Your first run

### 1. Launch Claude Code in your project

```bash
cd <your-project>
claude
```

!!! note "Running from a source checkout"
    If you installed [from source](install.md#from-source-dev-loop)
    instead of the marketplace, point Claude Code at the bundled plugin:
    `claude --plugin-dir /path/to/operon-plugin/plugins/operon-plugin`

operon loads automatically when the session starts.

### 2. Start the project_team workflow

```text
/project_team my-first-run
```

This starts a run named `my-first-run` at its first phase, `vision`.
(The name is normalized to a lowercase-hyphen slug, so
`/project_team My_First_Run` lands at the same `my-first-run`.) The
[Coordinator](agents-and-messaging.md) leads from here: it delegates to a
team of specialist agents rather than doing the work itself, so you
mostly talk to the Coordinator.

### 3. Move through the phases

Work each phase with the Coordinator. When the phase's work is done you
are asked to confirm moving on, and the run advances only once the
phase's checks pass -- some ask for your approval, others require a work
product to exist. That is the whole loop: work the phase, confirm,
repeat -- from `vision` through to `signoff`.

## What a run looks like

`project_team` walks a project through ten phases, in order:

| # | Phase | What happens |
|---|-------|--------------|
| 1 | vision | Agree on what you are building and why. |
| 2 | setup | Stand up the project, its workspace, and the run's starting files. |
| 3 | leadership | Spawn the four Leadership review agents (Composability, Terminology, Skeptic, User Alignment). |
| 4 | specification | Turn the vision into a specification you approve. |
| 5 | implementation | The team writes the code. |
| 6 | testing-vision | Agree on what testing must cover. |
| 7 | testing-specification | Write the test specification. |
| 8 | testing-implementation | Write tests and fix failures until they pass. |
| 9 | documentation | Complete and review the documentation. |
| 10 | signoff | Final integration and your approval. |

You stay in control at the boundaries: a phase only advances when its
checks pass, and most phases pause for your explicit approval before
moving on. The [Coordinator](agents-and-messaging.md) orchestrates the
team, [Guardrails](guardrails.md) keep risky actions behind an approval,
and each run's work products are versioned on disk (see
[Sessions](sessions.md)).

For the phase-by-phase detail and exactly how advancing is gated, see
[Workflows and Phases](workflows-and-phases.md).

## Handy while you work

- `/rules` -- show the guardrails in force and what is needed to advance
  from where you are right now. See [Guardrails](guardrails.md).
- `/restore` -- list your operon-sessions and switch between them. See
  [Sessions](sessions.md).
- Ask the Coordinator where things stand -- "what phase are we in?" --
  any time.

## Next steps

- [Workflows and Phases](workflows-and-phases.md) -- the 10-phase flow
  and advance-checks in depth.
- [Agents and Messaging](agents-and-messaging.md) -- the team model and
  how agents talk to each other.
- [MCP Tools Reference](mcp-tools-reference.md) -- every tool and
  command.
