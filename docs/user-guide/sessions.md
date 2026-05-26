# Sessions

An **operon-session** is one run of a workflow -- the unit operon uses to
keep everything about that run together and resumable. It carries its own
[phase](workflows-and-phases.md) state,
[roster](agents-and-messaging.md), [escape tokens](guardrails.md), and
artifact directory, so you can step away, switch between runs, and return
to exactly where you left off.

A session's state lives on disk in **two directories** under the project,
both keyed by its `run_name`: an ephemeral runtime tree and a versioned
work-products tree. The split keeps throwaway machine state out of git
while the durable work output is committed -- see
[Run state vs. work products](#run-state-vs-work-products) below.

Two related terms name *parts* of a session, never the session itself:

- **run_name** -- the identifier of one operon-session: the directory
  leaf shared by both directories.
- **run-dir** -- the on-disk runtime path of one operon-session,
  `<project>/.operon/<run_name>/`.

## Run state vs. work products

A run produces two kinds of output, and operon keeps them in separate
trees so git tracks the durable work and ignores the churn. Both are
keyed by the run's `run_name`:

| Tree | Holds | Git treatment |
| ---- | ----- | ------------- |
| `<project>/.operon/<run_name>/` | **How the run runs** -- runtime state: mailbox, `_handles/`, `phase_state.json`, `state.json`, `agents.json`, `inbox_cursor.json`, `overrides/`, `guardrail_log.jsonl`. | Gitignored on **all** branches. |
| `<project>/operon-runs/<run_name>/` | **What the run is for** -- work products: `STATUS.md`, `userprompt.md`, the specification, and the rest of its artifacts. | Tracked on **`develop`**; ignored on **`main`**. |

These are two halves of the *same* session: when a doc page or branch
points at `operon-runs/<run_name>/`, that is the committed half of the
run you activated under `.operon/<run_name>/`. The
[artifact dir](#the-artifact-dir) is what steers a run's work products
into the tracked half.

### What lives under `.operon/`

`.operon/` holds three kinds of thing -- the active-run pointer, the
per-run run-dirs (detailed in the table above), and the project-tier
config you author -- and git treats them differently:

```
<project>/.operon/
    _active.json                 # active-run pointer (ephemeral)
    <run_name>/ ...              # a run-dir from the table above (ephemeral)
    workflows/<id>/<id>.yaml     # project-tier workflow (committed)
    rules.yaml                   # project-tier guardrails (committed)
```

`workflows/` and `rules.yaml` are config you write and want versioned
(see
[Workflows and Phases](workflows-and-phases.md#where-a-workflow-can-live)
and [Guardrails](guardrails.md)), so a project's `.gitignore` ignores the
runtime entries and keeps the authored config:

```gitignore
.operon/*
!.operon/workflows/
!.operon/rules.yaml
```

## The active pointer

operon works on one session at a time, so it needs to know which run is
current. `_active.json` is the per-project pointer that names it
(`{active_run_name, set_at}`). It is swapped when you start a new session
(`/project_team`) or switch to an existing one (`/restore`) -- under the
hood the Coordinator calls `activate_workflow` or
`restore_operon_session`.

On a fresh project with no `_active.json`, operon
[auto-bootstraps](quickstart.md) a `default` run on first MCP start so
the session is usable immediately.

## The artifact dir

The **artifact_dir** is the one place a run's work products go, so
everything the run produces lands together and can be versioned rather
than scattered across the project. Workflow advance-checks reference it
through the **`${ARTIFACT_DIR}`** token, which the engine expands to this
path at check-evaluation time -- for example, the `project_team` setup
phase requires `${ARTIFACT_DIR}/STATUS.md` to exist.

`${ARTIFACT_DIR}` is the one canonical token. The Coordinator sets the
artifact dir during the setup phase, using its
[Coordinator-only](mcp-tools-reference.md#coordinator-only)
`set_artifact_dir` tool. It points at the session's **work-products
dir**, `<project>/operon-runs/<run_name>/` -- the tracked half of the
session (see [Run state vs. work products](#run-state-vs-work-products)) -- so the run's
`STATUS.md`, specification, and other artifacts are versioned. (The
`.operon/<run_name>/` tree is ephemeral and gitignored; artifacts written
there would go untracked.)

Setting it persists the pointer to the session's `state.json` and makes
the `artifact-dir-ready-check` pass for any phase that declares it.

## Listing and switching sessions

See your runs and switch between them with `/restore`:

```text
/restore                 # picker over discovered runs
/restore my-prior-run    # restore a named run directly
```

`/restore` (backed by `mcp__operon__restore_operon_session`) discovers
the runs, shows you a picker, and -- if the current run still has alive
workers -- asks you to confirm closing them before swapping the active
session to the chosen run. The picker renders only in the Coordinator's
foreground session.

The picker lists each discoverable session with its `run_name`,
`workflow_id`, `current_phase`, created and last-active times, agent and
alive-agent counts, and whether it is the active one. Agents read the
same data through the read-only `list_operon_sessions` tool.

!!! note "Restore requires an activated workflow"
    `restore_operon_session` targets a previously activated
    operon-session: its precondition is that the target run-dir has a
    `phase_state.json`. It is not a generic Claude Code `/resume`.

## Starting a new session

Start a workflow with its slash command -- for the bundled team flow,
`/project_team` with an optional run name:

```text
/project_team my_run
```

The skill hands that to the Coordinator, which makes a single
`activate_workflow` call: a new run-dir is created and its phase state
bootstrapped. It is destructive in one respect: if the *current* active
run has alive workers, you are first asked to confirm closing them, and
it proceeds only on accept. Declining changes no on-disk state.

The run name you pass is validated -- it rejects the filesystem-unsafe
characters
`/ \ : * ? < > | "`, a leading `.`, an empty name, a name longer than
50 characters, and a collision with an existing run directory.

For the `state.json` schema and the engine internals behind sessions,
see the
[Contributor/Architecture Guide -> Workflow Engine](../dev/workflow-engine.md).
The full tool list is in the
[MCP Tools Reference](mcp-tools-reference.md).
