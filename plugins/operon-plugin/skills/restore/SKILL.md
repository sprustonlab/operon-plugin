---
name: restore
description: Switch the active operon-session to a different existing run. Use when the user asks to restore an operon-session, switch to a previous run, pick from existing runs, or see what runs are available. Closes any alive workers in the current run (with confirmation) before swapping the active pointer.
user-invocable: true
allowed-tools:
  - mcp__operon__list_operon_sessions
  - mcp__operon__restore_operon_session
---

# /restore — Switch the active operon-session

This skill picks an existing operon-session and switches `_active.json`
to point at it. It is the user-facing entry point per SPEC §13.

## Behavior

Call `mcp__operon__restore_operon_session` with NO arguments. The tool
itself runs the discovery + picker + confirmation flow end-to-end:

1. Enumerates every `<project>/.operon/<run-name>/` directory via the
   same logic as `mcp__operon__list_operon_sessions`.
2. Issues an `elicitation/create` single-select picker listing the
   discovered runs with their workflow_id, current_phase, and alive
   worker count.
3. If the user picks a run AND there are alive workers in the CURRENT
   run, issues a second confirmation elicitation listing the workers
   that will be closed.
4. On accept: closes the alive workers via `claude stop <daemonShort>`,
   ensures the Coordinator's handle file + roster row exist in the
   target run-dir, then atomically swaps `_active.json`.
5. Returns a structured result with `restored: true/false` and the
   killed worker list.

## Argument handling

`$ARGUMENTS` (if provided) names a specific run to restore directly,
bypassing the picker. Pass it through as
`mcp__operon__restore_operon_session(run_name="$ARGUMENTS")`.

If `$ARGUMENTS` is empty, call the tool with no arguments and let it
surface the picker.

## What NOT to do

- Do NOT ask the user "which run?" yourself. The MCP tool handles
  that via `elicitation/create` so the LLM is out of the dialog
  loop -- channel messages cannot prompt-inject a restore.
- Do NOT call `mcp__operon__list_operon_sessions` first and then
  pass a chosen `run_name`. The tool already calls list internally
  when `run_name` is omitted; surfacing it through the LLM defeats
  the script-injection pattern.
- Do NOT proceed if the tool returns `restored: false`. Just relay
  the `reason` field back to the user verbatim.

## Sample invocations

User typed `/restore` (no args): call
`mcp__operon__restore_operon_session()` and report the result.

User typed `/restore my-prior-run`: call
`mcp__operon__restore_operon_session(run_name="my-prior-run")`. If
the run doesn't exist, the tool returns a structured error -- relay
that verbatim.

## Limitations (Phase 6.5)

- The `elicitation/create` dialog renders only in the Coordinator's
  foreground session. Workers (`claude --bg`) have no TTY and cannot
  render the picker -- they will get back a `no_selection` outcome
  from `_maybe_pick_run_name` and the tool will return without
  mutating state. Phase 7 may explore a worker-can-request flow
  via filesystem-routed elicitation per SPEC §9.
- The Coordinator's bg session (if any) is never auto-killed by
  `/restore`. Only worker rows from `agents.json` (role != coordinator)
  are eligible for destructive close.
