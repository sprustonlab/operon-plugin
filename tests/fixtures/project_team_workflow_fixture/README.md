# project_team_workflow_fixture

Filesystem-state fixture for the `project_team_workflow` scenario.

Per Q14 (coordinator path b): this fixture provides a **clean,
operon-empty** working state. The scenario itself drives:

- `activate_workflow(project_team)` in sub-act 2 (lands at phase `vision`),
- `vision -> setup` advance in sub-act 3,
- `setup -> leadership` advance in sub-act 8,
- restore re-attachment in sub-act 10.

Sidechains (subagent transcripts under
`~/.claude/projects/<cwd-mangled>/<session-id>/subagents/`) come from
the journey's own teammate spawns in sub-acts 4-8, not from
pre-seeded fixture state.

## Contents

The fixture directory contains:

- `seed/` -- a tree copied into the scenario's tmp_cwd at scenario start.
  - `.gitkeep` -- present so the empty-state directory exists in git.

The fixture intentionally does NOT pre-create:

- `.operon/` -- the scenario creates this via `activate_workflow`.
- `~/.claude/teams/<team>/config.json` -- the runtime creates this when
  the lead invokes `TeamCreate` (or when operon's `activate_workflow`
  registers the team).
- Sidechain transcripts -- produced by sub-acts 4-8.

## Override-rule extension (Q4 path 4a)

For sub-act 5's override flow, the fixture installs a
per-scenario workflow override at
`<tmp_cwd>/.claude/plugins/operon-plugin/workflows/project_team/extras.yaml`
that adds ONE deny-level rule scoped to the `implementer` teammate
role. The base `project_team.yaml` is NOT modified -- the override is
a per-scenario filesystem-only addition consumed by operon's 3-tier
loader at activate_workflow time.

The override rule:

```yaml
rules:
  - id: scenario_implementer_deny
    trigger: PreToolUse/Write
    enforcement: deny
    detect:
      field: file_path
      pattern: ".*SCENARIO_OVERRIDE_PROBE.*"
    message: "Scenario sub-act 5 deny rule -- exercise override flow"
    roles: [implementer]
```

The scenario drives the implementer teammate to write a file matching
the pattern in sub-act 5, which fires the deny; the lead invokes
`request_override` and the user (the harness) confirms via tmux input.
