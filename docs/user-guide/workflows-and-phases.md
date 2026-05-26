# Workflows and Phases

A **workflow** is a named, phase-structured process identified by a
`workflow_id` and defined by a manifest: a `<workflow_id>.yaml` file
inside a `workflows/<workflow_id>/` directory. The bundled workflows ship
under the plugin, and you can add your own (see
[Where a workflow can live](#where-a-workflow-can-live)). Activating a
workflow creates an [operon-session](sessions.md) -- a running instance
with its own phase state.

A **phase** is one ordered stage within a workflow. Each phase delivers
its own instructions and carries zero or more **advance-checks** -- the
gates that must pass before the workflow can move to the next phase.

The ten-phase `project_team` flow at a glance is in the
[Quickstart](quickstart.md#what-a-run-looks-like). This page covers how
phases work underneath that view: the instructions each agent receives,
the gates that move a run forward, and how to inspect your live run.

## What each agent receives in a phase

Phase instructions are role-scoped. Each phase entry in the manifest
names a `file:` -- a role-prompt basename -- and each role directory
under `plugins/operon-plugin/workflows/project_team/<role>/` holds a
markdown file per phase alongside a standing `identity.md`. When a phase
begins (or when the Coordinator spawns an agent into it), that named
agent receives its `identity.md` together with the phase's file for its
role, `<role>/<phase>.md`. So an agent always works from its identity
plus the current phase's instructions.

A role carries files only for the phases it takes part in -- the Skeptic,
for example, has `implementation.md` and the testing-phase files but no
`vision.md`.

## Advance-checks

An advance-check is a gate evaluated before a phase transition. Checks
run in declaration order with AND semantics: the first failing check
stops the transition, so the phase holds until you resolve it. The check
types `project_team` uses are:

- **`manual-confirm`** -- prompts you with a yes/no question; the phase
  advances on an explicit "accept". This is the user checkpoint, and it
  gates vision, setup, specification, implementation, the testing phases,
  and documentation.
- **`artifact-dir-ready-check`** -- the artifact dir is the run's single
  agreed-on home for work products. Setting it once gives every agent a
  consistent place to write files, addressed through the
  [`${ARTIFACT_DIR}`](sessions.md#the-artifact-dir) token rather than
  hard-coded paths, so a run's outputs land together no matter which
  agent produced them. This check passes once that dir has been set via
  `mcp__operon__set_artifact_dir`. The setup phase declares it; until it
  passes, the message tells the agent to call `set_artifact_dir(...)`
  first.
- **`file-exists-check`** -- requires a named file to be present, with
  the `${ARTIFACT_DIR}` token expanded to that directory. For example,
  setup requires `${ARTIFACT_DIR}/STATUS.md` and
  `${ARTIFACT_DIR}/userprompt.md`; specification requires
  `${ARTIFACT_DIR}/specification/SPECIFICATION.md`.

A phase does not have to declare any checks. When it declares none it
advances freely -- the `leadership` and `signoff` phases are examples,
carrying no checks at all.

## Advancing

The Coordinator advances a phase with `mcp__operon__advance_phase()` (a
[Coordinator-only](mcp-tools-reference.md#coordinator-only) tool). It
evaluates the current phase's checks and, on all-pass, moves the run to
the next phase and records the transition. A `manual-confirm` check
renders its prompt directly in your foreground session.

## Seeing the live flow for your run

The manifest is the source of truth for the phase list, ordering, and
checks. To see where your run actually is, ask the Coordinator ("what
phase are we in?") or run `/rules`:

```text
/rules        # the advance-checks gating THIS phase, for your role
```

`/rules` (backed by `mcp__operon__get_applicable_rules`) renders the
advance-checks for the calling agent's current `(role, phase)` straight
from the manifest, so it stays in step with the workflow. (Agents can
also read the current phase directly with the `get_phase` tool.)

## Where a workflow can live

operon looks for a workflow in three places, in priority order:

| Tier | Location |
|------|----------|
| Project | `<project>/.operon/workflows/<workflow_id>/` |
| User | `~/.operon/workflows/<workflow_id>/` |
| Plugin | `plugins/operon-plugin/workflows/<workflow_id>/` (bundled) |

A project-tier workflow is authored config you commit, even though it
sits under `.operon/` -- set the project's `.gitignore` to keep it (see
[What lives under `.operon/`](sessions.md#what-lives-under-operon)).

Unlike guardrail [Rules](guardrails.md), which merge across tiers, a
workflow resolves **first-match-wins**: operon uses the first tier that
defines the `workflow_id` and stops there. A project- or user-tier
workflow with the same id shadows the bundled one entirely rather than
merging with it -- which is how you customize a bundled flow without
editing the plugin.

The plugin bundles `project_team` and `_smoke` (a minimal flow for
verifying the engine end-to-end). To add your own, create
`workflows/<your_id>/<your_id>.yaml` under the project or user root,
with a `<role>/` directory for each role (each holding an `identity.md`
and the phase files that role uses). You start the bundled `project_team`
with its slash command, `/project_team <run_name>`, which the skill hands
to the Coordinator to activate. Any workflow is ultimately activated by
the Coordinator's `activate_workflow` tool, addressed by `workflow_id`.

For how manifests are loaded across tiers and how advance-checks are
evaluated, see the
[Contributor/Architecture Guide -> Workflow Engine](../dev/workflow-engine.md).
