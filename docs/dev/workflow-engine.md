# Workflow Engine

The workflow engine turns a declarative **manifest** into a running,
phase-structured process. It lives in `plugins/operon-plugin/src/operon_mcp_server/workflow.py`
(the loader + phase engine) and `plugins/operon-plugin/src/operon_mcp_server/checks/` (the
advance-check implementations). This page covers the manifest schema, the
3-tier loader, the advance protocol, and how to add a phase.

For the user-facing tour of what the phases *mean* and how to drive them,
see [Workflows and Phases](../user-guide/workflows-and-phases.md).

## The manifest is the source of truth

A workflow is a directory `plugins/operon-plugin/workflows/<id>/` whose
manifest is `<id>.yaml`. The bundled `project_team` workflow's manifest is
`plugins/operon-plugin/workflows/project_team/project_team.yaml`, and it is
the **authoritative definition of the phase flow**. Read it rather than
trusting any prose copy -- a transcribed phase list drifts; the manifest
does not.

`project_team.yaml` declares ten phases; the at-a-glance list with what
each one does is the
[Quickstart table](../user-guide/quickstart.md#what-a-run-looks-like).
Phase ids are lowercase, hyphen-joined where compound (`testing-vision`,
not `testing_vision`). Read the manifest for the authoritative order -- a
transcribed list drifts; the manifest does not.

## Manifest schema

Each entry under `phases:` is parsed into a `PhaseDecl`
(`workflow.py`). The fields:

```yaml
phases:
  - id: setup                       # required; the phase id
    file: setup                     # optional; the role-prompt filename (<role>/<file>.md)
    advance_checks:                 # optional; gates evaluated before advancing
      - type: artifact-dir-ready-check
        on_failure:
          message: "Artifact directory not set -- call set_artifact_dir(...) first."
      - type: file-exists-check
        path: "${ARTIFACT_DIR}/STATUS.md"
```

Top-level keys:

- `workflow_id` -- the workflow identifier. If absent, the loader falls
  back to the parent directory name.
- `phases` -- the ordered list. At least one phase is required.
- `rules` -- optional workflow-embedded guardrail Rules, additive to the
  3-tier `rules.yaml`. See [Rules and Enforcement](rules-and-enforcement.md).

The `file` field names the per-role brief Markdown. On a phase advance,
the engine looks up `<workflow_root>/<role>/<new_phase>.md` (falling back
to `<workflow_root>/<new_phase>.md`) to deliver each team member a
role-specific brief for the destination phase. The role directories
(`coordinator/`, `composability/`, ...) hold these briefs plus each
role's `identity.md`.

## The `${ARTIFACT_DIR}` token

Advance-check paths use one template token, `${ARTIFACT_DIR}` -- the sole
token for the artifact directory. The engine substitutes it from the
active run's `state.json` (written by `set_artifact_dir`) at evaluation
time. If `artifact_dir` is unset, the placeholder is left literal; the
`artifact-dir-ready-check` earlier in the chain is what fails first, so a
substituted `file-exists-check` never runs against an unbound
artifact dir.

## The 3-tier loader

`load_workflow(workflow_id)` searches three tiers in priority order and
returns the first that has a parseable manifest:

1. **project** -- `<project>/.operon/workflows/<id>/`
2. **user** -- `~/.operon/workflows/<id>/`
3. **plugin** -- `${CLAUDE_PLUGIN_ROOT}/workflows/<id>/`

Within a tier, two manifest filenames are accepted: `<id>.yaml` first,
then `phases.yaml` (a minimal form). A missing tier (e.g. no
`CLAUDE_PLUGIN_ROOT`) is simply skipped. The returned `WorkflowDecl`
records which tier and file it came from, so diagnostics and relative-path
resolution can use the manifest's directory as the workflow root.

## Phase state

A running [operon-session](../user-guide/sessions.md) tracks its phase in
`<run-dir>/phase_state.json`:

```json
{
  "schema_version": 1,
  "workflow_id": "project_team",
  "current_phase": "setup",
  "phase_started_at": "2026-05-25T12:00:00+00:00",
  "advance_history": [
    {"from": "vision", "to": "setup", "at": "...", "triggered_by": "Coordinator"}
  ]
}
```

All writes go through `_atomic_write_json` (temp file + `os.replace`),
single-writer (the Coordinator). There is no compare-and-swap; the
single-writer discipline is the concurrency guarantee. A separate
`state.json` holds the `run_name` + `artifact_dir` set by
`set_artifact_dir`; its absence is non-fatal (read returns `None`) because
it is created lazily.

## Advance-checks

The five built-in check types live in `checks/builtins.py`:

| Check type | Passes when |
|------------|-------------|
| `manual-confirm` | the user accepts an `elicitation/create` form (`confirm: true`) |
| `file-exists-check` | at least one configured `path`/`paths` exists |
| `file-content-check` | a configured file has a line matching `pattern` |
| `command-output-check` | a shell command's stdout matches `pattern` (30s timeout) |
| `artifact-dir-ready-check` | `state.json` exists with a non-empty `artifact_dir` |

`checks/` is a **leaf module**: it imports only stdlib, `re`, and
`asyncio` -- never the MCP SDK and never `workflow.py`. The check types
that need transport or run-tier context receive it through injected seam
parameters rather than importing it:

- `manual-confirm` receives an `_elicit` callable. The engine builds this
  closure (over the MCP SDK's `session.elicit_form`) in
  `tools/advance_phase.py` and injects it; the leaf module never touches
  the SDK.
- `file-exists-check` / `file-content-check` receive a `base_dir` (the
  workflow root) for resolving relative paths, plus `${ARTIFACT_DIR}`
  substitution.
- `command-output-check` receives a `cwd`.
- `artifact-dir-ready-check` receives the `state.json` path.

## The advance protocol

`advance_phase` (Coordinator-only) runs `_do_advance` in
`tools/advance_phase.py`:

1. **Require Coordinator.** Reject non-Coordinator callers (identity is
   env-anchored).
2. **Resolve the next phase** from the manifest's declaration order. If
   the current phase is the last, return `advanced: false` with a reason.
3. **Roster check (fail-loud).** Every team-config member's `agentType`
   must be a defined role in the active workflow. A mismatch (e.g. a lead
   created as `team-lead` rather than `coordinator`) returns
   `members_not_in_workflow_roster` *before* any advance-check runs, so
   the user is not prompted with manual-confirm dialogs only to have the
   commit refused. The synthetic `operon` member is exempt.
4. **Run advance-checks in order, AND semantics, short-circuit.**
   `workflow.run_advance_checks` evaluates the current phase's checks in
   declaration order and stops at the first failure. The result lists one
   outcome per check that actually ran.
5. **Commit on all-pass.** `workflow.commit_advance` atomically rewrites
   `phase_state.json` to the next phase and appends an `advance_history`
   entry stamped with the triggering agent.
6. **Broadcast per-role briefs.** After the commit, the engine delivers
   each team member their `<role>/<new_phase>.md` brief via the
   inbox-write primitive. This is best-effort: a per-recipient failure is
   captured in the response manifest and never rolls back the
   (already-committed) advance.

## Adding a phase

To add a phase to `project_team` (or any workflow):

1. **Edit the manifest.** Add a `phases:` entry at the right position --
   ordering is declaration order. Give it an `id` (lowercase,
   hyphen-joined if compound) and, if it gates, `advance_checks`.
2. **Add the role briefs.** For each role that needs phase-specific
   instructions, create `<workflow_root>/<role>/<new_phase>.md`. Roles
   without a specific brief fall back to a phase-level
   `<workflow_root>/<new_phase>.md`, then to a generated one-liner.
3. **Use a known check type.** The loader rejects an unknown
   `advance_checks` type at parse time (`known_check_types()`), so a typo
   in the manifest fails loudly on the next `advance_phase` rather than
   silently skipping the gate.
4. **Update the docs.** The phase list lives in the
   [Quickstart table](../user-guide/quickstart.md#what-a-run-looks-like) --
   keep it in step with the manifest (or, better, point readers at the
   manifest).

See [Contributing](contributing.md) for the end-to-end checklist of adding
a phase, a guardrail rule, or a role together.
