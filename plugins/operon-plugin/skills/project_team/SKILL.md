---
name: project_team
description: Activate the project_team workflow as a new operon-session. Use only when the user typed /project_team; do not invoke on the model's own initiative.
disable-model-invocation: true
user-invocable: true
allowed-tools:
  - "Bash(python *)"
  - mcp__operon__activate_workflow
---

# /project_team -- Activate the project_team workflow

This skill is the user-facing entry point for the `project_team`
workflow per SPEC §13b (Phase 12). The user types `/project_team`
(optionally followed by a run_name) and a fresh operon-session is
created under `<project>/.operon/<run_name>/` with `current_phase`
set to the workflow's first phase (`vision`).

The model never frames the activation question -- the shared
`activate.py` script validates the user's input client-side and emits
a single `OPERON_DISPATCH` directive that this skill body translates
into one `mcp__operon__activate_workflow` call.

## Script invocation

!`python ${CLAUDE_PLUGIN_ROOT}/skills/activate/scripts/activate.py project_team $ARGUMENTS`

## What to do with the script output

Read the script's stdout (above). Exactly one of two cases:

**Case A -- the line begins with `OPERON_DISPATCH`.** Format:

    OPERON_DISPATCH tool=mcp__operon__activate_workflow workflow_id=<id> run_name=<name>

Parse the `workflow_id=<id>` and `run_name=<name>` fields (single-space
separated, neither value can contain spaces or `=` post-validation) and
call:

    mcp__operon__activate_workflow(workflow_id=<id>, run_name=<name>)

Relay the tool's full structured result to the user. On success the
tool returns `{activated: true, run_name, workflow_id, current_phase,
run_dir, tier, manifest_path, killed_workers, previous_run}`. Surface
`run_name`, `workflow_id`, and `current_phase` prominently so the user
sees the operon-session is live. If `killed_workers` is non-empty,
mention the closed workers by name (the user just lost their bg
sessions).

On `{activated: false, reason: "user_declined", ...}`: relay the
reason and the `would_have_killed` list verbatim; do NOT retry.

**Case B -- the line begins with `ERROR:`.** Relay the diagnostic to
the user verbatim and stop. Do NOT call `mcp__operon__activate_workflow`.
The script's validation rejected the input or no run_name was supplied
on a non-interactive stdin.

## Sample invocations

- `/project_team` -- script tries interactive stdin prompt; on a TTY
  this works, otherwise it emits `ERROR: /project_team requires a
  run_name...`.
- `/project_team my_refactor` -- script validates and emits
  `OPERON_DISPATCH ... run_name=my_refactor`; LLM calls
  `mcp__operon__activate_workflow(workflow_id="project_team",
  run_name="my_refactor")`.
- `/project_team invalid/name` -- script emits
  `ERROR: run_name contains disallowed character(s): '/' ...`; LLM
  relays the diagnostic; no activation.

## What NOT to do

- Do NOT prompt the user for a run_name yourself. If the script
  emitted ERROR for missing run_name, relay the diagnostic and stop;
  the user re-invokes `/project_team <name>` on their next turn.
- Do NOT call `mcp__operon__activate_workflow` with arguments other
  than the exact `workflow_id` + `run_name` parsed from the
  `OPERON_DISPATCH` line. The script is the validation gate; bypassing
  it defeats the client-side check.
- Do NOT retry on `activated: false` reasons. The MCP tool already
  staged the destructive-confirm elicitation; a retry would re-prompt
  the user redundantly.

## Phase 12 limitation

The SPEC §13b "issues an elicitation/create to the host" step is
implemented as a stdin prompt fallback in `activate.py` (works on a
TTY, fails on `--bg`). A follow-up phase will add a server-side
elicit_form MCP tool the script can call via JSON-RPC; until then,
non-interactive contexts must supply the run_name as an explicit
argument.
