# STATUS -- `docs` run

- **Workflow:** project_team
- **Run name:** docs   **Project name:** docs
- **Working dir:** /groups/spruston/home/moharb/operon-plugin
- **Artifact dir (work products):** /groups/spruston/home/moharb/operon-plugin/operon-runs/docs
  (ephemeral runtime/phase state lives separately in `.operon/docs/`; see Git / branch model)
- **Active git branch:** `develop` -- ALL implementation work is here, UNCOMMITTED
  (in the working tree as modified/untracked files; nothing committed or pushed yet)
- **Current phase:** implementation (authoritative source: `.operon/docs/phase_state.json`)
- **Last updated:** 2026-05-25

## Where we are (resume here)

Implementation is COMPLETE on `develop` and verified green. We are paused
at the **implementation user-checkpoint**: before advancing to Testing,
the user must (a) review the diff and (b) settle 2 small open decisions
(see "OPEN -- user decisions" below). After that: `advance_phase` to the
testing phase.

To continue in a new session: the `docs` operon-session is the active run
(`/restore` if needed). Read, in order: this file, then
`operon-runs/docs/specification/SPECIFICATION.md` (the authoritative spec),
then `operon-runs/docs/userprompt.md` (intent + all decisions). Work is on
branch `develop`, uncommitted.

## Goal

Stand up a MkDocs (Material) documentation site for operon-plugin -- a
User Guide + a Contributor/Architecture Guide -- modeled on claudechic's
docs toolchain; rework the README as the front door; add a GitHub Pages
CI workflow; and remove claudechic from the repo entirely.

## Decisions (locked)

- **D1:** canonical run term = `operon-session` (not operon-run / operon session).
- **D2:** org spelling = `SprustonLab` (camelCase) in prose.
- **D9:** the docs site (Contributor/Architecture Guide) is the AUTHORITATIVE
  design reference; the old external claudechic-private SPEC pointer is dropped.
- **claudechic removal = ABSOLUTE:** `grep -rni claudechic` over SHIPPED
  surfaces returns ZERO. Includes the live token rename
  `${CLAUDECHIC_ARTIFACT_DIR}` -> `${ARTIFACT_DIR}` (behavior-preserving).
- **DELETE** the 4 internal audit docs (supersedes the earlier "move out"):
  AGENT_TEAMS_PIVOT_PLAN.md, APPENDIX.MD, SCOPE_AUDIT_v2.1.md, SPEC_GROUNDING_AUDIT.md.
- **DELETE** `plugins/operon-plugin/workflows/project_team/project_integrator/`.
- **Verification = USER DIFF REVIEW at a checkpoint, NOT new CI/tests.**
  The token rename is verified via the EXISTING test suite (no bespoke test).
  Only enforcement artifacts allowed: the (user-requested) Pages deploy +
  the two branch guards.
- **Scrub also fixes** stale phase-count mirrors (cite the 10-phase manifest)
  and `.project_team/` drift in `tests/`.

## Phase log

| Phase | Status | Notes |
|-------|--------|-------|
| vision | DONE | Vision approved; CI/Pages + claudechic-removal scope. |
| setup | DONE | artifact_dir bound; `main` pushed (override); `develop` created. |
| leadership | DONE | composability, terminology, skeptic, user_alignment, researcher. project_integrator SKIPPED. |
| specification | DONE | SPECIFICATION.md + appendix.md authored; user approved. |
| implementation | AT USER CHECKPOINT | All work done + verified; 4/4 Leadership PASS; awaiting user diff review + 2 decisions. |
| testing | NEXT | Manifest: testing-vision/-specification/-implementation. Run existing suite; confirm `${ARTIFACT_DIR}` substitution scenarios RUN (not skipped) -- that verifies the token rename. |
| documentation / signoff | pending | User checkpoints. |

## Implementation result (on `develop`, uncommitted)

NEW:
- Docs site: `docs/index.md` + `docs/user-guide/` (install, quickstart,
  workflows-and-phases, agents-and-messaging, guardrails, sessions,
  mcp-tools-reference) + `docs/dev/` (architecture, mcp-server,
  workflow-engine, rules-and-enforcement, hooks, skills, testing,
  contributing). 16 pages total.
- `mkdocs.yml` (site_name operon-plugin, no site_url/analytics, Material +
  features, full markdown_extensions, nav with "Contributor/Architecture Guide").
- `.github/workflows/docs.yml` (Pages deploy on push to main).
- `.github/workflows/no-operon-runs-additions-on-main.yml` (PR guard, `^operon-runs/`, --diff-filter=AM).
- `.pre-commit-config.yaml` (single `block-operon-runs-on-main` local hook).

MODIFIED:
- `pyproject.toml`: added `mkdocs>=1.6.1` + `mkdocs-material>=9.7.1` to dev group.
- `.gitignore` (develop): added `.claude/` + `site/`; `operon-runs/` NOT ignored (tracked).
- claudechic scrub (prose/strings only, no logic): `src/operon_mcp_server/`
  (workflow.py incl. token rename, rules.py, checks/builtins.py, checks/protocol.py,
  bootstrap.py, tools/request_override.py, tools/restore_operon_session.py +
  dangling-ref repairs in inbox.py, subagent_install.py, tools/send_to_member.py,
  tools/spawn_agent.py), `rules.yaml`, `project_team.yaml`.
- `tests/scenarios/project_team_workflow.py`: `.project_team` -> `.operon` (path string only).
- `README.md`: reworked front door, repointed SPEC pointer to docs site,
  SprustonLab, softened "claudechic-style", :189 operon-run -> operon-session.
- `CHANGELOG.md:6`: repointed dangling pointer (narrow fix).
- Stale phase mirrors fixed: `coordinator/identity.md` (7->10), workflow
  `project_team/README.md` (4->10, removed dead git_setup/getting-started/
  AI_PROJECT_TEMPLATE refs), `coordinator/documentation.md` + `setup.md` (.project_team->.operon).

DELETED: the 4 audit docs + `project_integrator/identity.md` (dir).

## Verification (all green)

- `grep -rni claudechic` over shipped surfaces (src/, plugins/, docs/,
  README, CHANGELOG, pyproject, .github, configs) = ZERO. (Mentions remain
  ONLY in `operon-runs/` -- the develop-only audit trail recording the
  removal; never shipped to main. See OPEN decision #1.)
- `uv run mkdocs build --strict` = clean, 16 pages, no broken-nav warnings.
- `.project_team` residual = only `project_team.yaml:95` (intentional dual-match regex).
- `py_compile` + `yaml.safe_load` OK on edited code/manifests.
- Leadership sign-off: composability PASS, skeptic PASS (7-pt), user_alignment
  PASS, terminology PASS (its 3 naming fixes applied).

## OPEN -- user decisions before advancing to Testing

1. **`operon-runs/` claudechic mentions** -- leave them (coordinator
   recommends: they are the develop-only record of what was removed, never
   ship to main) vs. scrub them too. UNDECIDED.
2. **`src/operon_mcp_server/tools/set_artifact_dir.py:34-36`** -- the tool's
   input-schema description (model-surfaced) still recommends
   `<project>/.operon/<run_name>/` as the artifact-dir default (cites SPEC §7).
   This is the root cause of the earlier co-mingling. Coordinator recommends
   FIXING it to `operon-runs/<run_name>/` (1-line prose, routes to cleanup_code).
   UNDECIDED.
3. **User diff review** of the implementation (the verification checkpoint). PENDING.

## Pending tasks (not yet done)

- [ ] MAIN-branch `.gitignore`: when on `main`, add the `operon-runs/` block
      + `.claude/` + `site/` (main still has the old short `.gitignore`; develop's
      edits are NOT on main). Per-branch commit step.
- [ ] Commit the develop work + eventually merge to main. NOTE: a guardrail
      `no_push_before_testing` blocks pushing until the testing phase (needs
      `request_override` if pushed earlier; was used once in setup for the inaugural main push).
- [ ] Testing phase: run `uv run pytest`; confirm artifact-dir-substitution
      scenarios run (skeptic's note) to verify the token rename via existing suite.
- [ ] (If user approves) the 2 OPEN decisions above.

## Manual (user) step

- Enable GitHub Pages: repo Settings -> Pages -> Source = "GitHub Actions"
  (the `docs.yml` deploy can't publish until this is set).

## Team roster (team `docs`)

- Lead/coordinator: `team-lead` (this agent). Stub: `operon`.
- Leadership/support (idle, reviewed): composability, terminology, skeptic,
  user_alignment, researcher.
- Implementers (idle, work done): docs_user, docs_dev, infra, cleanup_code, cleanup_meta.

## Key references

- Spec: `operon-runs/docs/specification/SPECIFICATION.md` (+ `appendix.md`).
- Inventory: `operon-runs/docs/research.md` (90 claudechic hits catalogued).
- Per-axis reviews: `operon-runs/docs/specification/{composability,terminology,skeptic_review,user_alignment}.md`.
- claudechic (EXTERNAL inspiration only; not in this repo):
  `/groups/spruston/home/moharb/claudechic` (mkdocs.yml, docs/, .github/workflows/docs.yml).
