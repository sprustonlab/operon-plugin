# Contributing

This page covers the parts of contributing that are not derivable from the
source: the two-branch git model, what is tracked versus ignored, and the
end-to-end recipes for adding a workflow phase, a guardrail Rule, or a
role. For the surfaces themselves, see the rest of this guide --
[Architecture](architecture.md) is the map.

## Development environment

operon-plugin is managed with **uv** and `pyproject.toml`. The MCP server
and the hook both resolve their interpreter through the same
`uv -> python3 -> python` ladder (the [bin shim](../user-guide/install.md)
and the [hook wrapper](hooks.md)), so installing `uv` is the only setup
step required to run the plugin.

```bash
uv sync --dev                       # install runtime + dev deps from pyproject.toml
uv run pytest tests/scenarios/_smoke_test_harness.py -v   # validate the harness
uv run mkdocs build                 # build this docs site
```

The runtime deps (`mcp`, `watchdog`, `PyYAML`) are pinned in
`[project] dependencies`; the test + docs deps (`pytest`, `pexpect`,
`pyte`, `mkdocs`, `mkdocs-material`) live in the `dev` dependency group.

## The two-branch model

The develop/main split governs one tree: `operon-runs/`, the per-run work
products. (For how a run's bytes divide between `.operon/` runtime state
and `operon-runs/` work products, see
[Run state vs. work products](../user-guide/sessions.md#run-state-vs-work-products).)

- **`develop`** tracks `operon-runs/` -- the versioned audit trail of a
  project_team run.
- **`main`** ignores `operon-runs/`; it carries the shipped plugin, the
  docs source, and the CI.
- **`.operon/`** runtime state (run-dirs, `_active.json`, `_handles/`) is
  ignored on both branches and regenerated every run. The narrowed root
  `.gitignore` re-includes the authored project-tier config
  (`.operon/workflows/`, `.operon/rules.yaml`) and the
  `!tests/fixtures/**/.operon/**` seeds, so those stay tracked. See
  [What lives under `.operon/`](../user-guide/sessions.md#what-lives-under-operon).

Three distinct artifacts enforce the `operon-runs/` split, each guarding a
different surface -- do not collapse them into one:

1. **`.gitignore`** -- on `main`, `operon-runs/` is listed (with the
   in-file comment explaining the develop-only split); on `develop` it is
   not. Guards the *branch state*.
2. **`block-operon-runs-on-main` pre-commit hook** -- a local hook in
   `.pre-commit-config.yaml` that rejects any commit touching
   `^operon-runs/` while on `main`. Guards the *local commit*.
3. **`no-operon-runs-additions-on-main.yml` CI workflow** -- a PR guard
   that rejects diffs adding or modifying `operon-runs/` into `main`
   (deletions are allowed). Guards the *PR*.

The acceptance check for the model: a commit touching
`operon-runs/docs/<file>` is tracked on `develop` and rejected on `main`
(by the pre-commit hook locally and the PR guard in CI), while `.operon/`
runtime state stays ignored on both.

## Docs deploy

The docs site is built and published by `.github/workflows/docs.yml` on
push to `main`: it runs `uv sync --dev` then `uv run mkdocs build` and
deploys the `site/` output to GitHub Pages. Because the mkdocs deps and
that workflow must both be present for the first deploy to succeed, they
land in the same change. Enabling GitHub Pages (Settings -> Pages ->
source: GitHub Actions) is a one-time manual step outside CI's control.

## Recipe: add a workflow phase

1. Edit the manifest
   `plugins/operon-plugin/workflows/project_team/project_team.yaml`: add a
   `phases:` entry at the right ordinal position (declaration order is
   phase order). Give it a lowercase, hyphen-joined `id` and, if it gates,
   `advance_checks` using a known check type.
2. Add per-role briefs: `<workflow_root>/<role>/<new_phase>.md` for each
   role that needs phase-specific instructions; roles without one fall
   back to a phase-level `<workflow_root>/<new_phase>.md`.
3. Run the relevant scenario sub-act to confirm the advance protocol picks
   up the new phase. See [Workflow Engine](workflow-engine.md#adding-a-phase)
   for the engine details.
4. Update the phase list in
   [Workflows and Phases](../user-guide/workflows-and-phases.md) and the
   list in [Workflow Engine](workflow-engine.md), both of which cite the
   manifest.

## Recipe: add a guardrail Rule

1. Choose the tier: a global catastrophic block goes in
   `plugins/operon-plugin/rules.yaml`; a workflow-specific policy goes in
   the manifest's `rules:` block; a site policy goes in a user/project
   `rules.yaml`.
2. Write the entry (unique `id`, `trigger`, `enforcement`,
   `detect.pattern`, plus `roles`/`phases` scoping).
3. For an unrecoverable-class deny, mirror it into the hook's
   `_FAILCLOSED_DENY` set with an identical regex + `rule_id`.
4. See [Rules and Enforcement](rules-and-enforcement.md#adding-a-rule) for
   the schema and the fail-closed discipline.

## Recipe: add a role

1. Create `plugins/operon-plugin/workflows/project_team/<role>/identity.md`
   (the lowercase snake_case directory name is the role slug; the role's
   readable name is the H1 title you write at the top of that `identity.md`).
2. Add per-phase briefs `<role>/<phase>.md` for the phases where the role
   needs phase-specific instructions.
3. The next `activate_workflow` compiles the `identity.md` into
   `~/.claude/agents/<role>.md` automatically -- no separate registration.
   See [Skills](skills.md#roles-become-claude-code-subagents).
4. If the role participates in the team, ensure its `agentType` matches
   the role slug when the team is created (`TeamCreate`), or
   `advance_phase`'s roster check will refuse to advance with
   `members_not_in_workflow_roster`. See
   [Workflow Engine](workflow-engine.md#the-advance-protocol).

## Conventions

- **Cross-platform** (Linux/macOS/Windows): pass `encoding="utf-8"` to
  every file/subprocess call; use `pathlib.Path`, never string-join paths;
  use `os.replace` for atomic renames (not `Path.rename`); guard
  POSIX-only APIs with a platform check; ASCII only in source -- no emoji,
  em-dash, or box-drawing characters.
- **Module import direction** stays downward: leaf modules
  (`paths.py`, `checks/`) never import the engine
  (`workflow.py`, `rules.py`), which never imports `tools/`. See
  [MCP Server](mcp-server.md#module-dependency-direction).
- **Terminology**: use `operon-session` for a run instance (never
  `operon-run` or `operon session`); `run_name` for the identifier and
  `run-dir` for the path; `SprustonLab` (camelCase) for the org in prose.
