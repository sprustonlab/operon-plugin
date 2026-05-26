# User Prompt -- `docs` run

## Original request

Activate `/project_team docs` to build a documentation set for the
operon-plugin.

## Clarified intent (Vision phase)

- **Goal:** Write *new* documentation for operon-plugin.
- **Audience:** Both end users and contributors.
- **Scope:** The operon-plugin (this repo), not claudechic.

## Direction added during Vision approval

1. Structure: a **User Guide** + a **Contributor/Architecture Guide**.
2. Toolchain: **MkDocs + Material theme**, modeled on claudechic's docs
   setup (`mkdocs.yml`, `docs/` tree with a `dev/` subfolder,
   `mkdocs>=1.6.1` + `mkdocs-material>=9.7.1`) -- but using
   operon-plugin's *own* config (no claudechic `site_url`/analytics).
3. **README needs rework** -- restructure as the front door into the docs.
4. **Minimize claudechic presence** -- the docs must describe
   operon-plugin; no claudechic branding/content carried over, so agents
   reading this repo are not confused.
5. **CI / GitHub Pages** -- add `.github/workflows/docs.yml` (net-new;
   repo has no CI yet) to build the MkDocs site and deploy to GitHub
   Pages on push to `main`, modeled on claudechic's `docs.yml`.
6. **Scan + remediate claudechic leftovers (scope expansion, approved
   2026-05-25).** Beyond docs content: actively audit the repo for
   content ported from claudechic that does not match operon-plugin and
   would mislead agents. Confirmed example:
   `workflows/project_team/project_integrator/identity.md` describes a
   conda / `source activate` / `commands/` / `envs/` launcher system
   that does NOT exist here (operon-plugin uses uv + pyproject.toml +
   `bin/operon-mcp-server` shim). Other agent identity files likely
   carry similar assumptions. Produce a findings list and remediate.
   - **USER DECISION (2026-05-25):** Amend/remove claudechic attribution
     comments such as "mirrors claudechic rule X" and "ported from
     claudechic" wherever they appear -- INCLUDING in code/config/
     manifest files (`rules.yaml`, `project_team.yaml`, workflow
     READMEs). This overrides the earlier "leave accurate attribution
     alone" calibration. Constraint: edit only the COMMENT text; do not
     change rule or manifest logic/behavior.

## Roster decisions

- **project_integrator: SKIPPED for this run** (approved 2026-05-25).
  Its identity.md is a claudechic port with wrong (conda) environment
  assumptions; the CI/git work is straightforward uv + GitHub Actions
  and goes to implementer instead.

## Specification decisions (2026-05-25)

- **D1 run term:** canonical = `operon-session` (rename `operon-run` /
  `operon session` variants). `run_name`/`run-dir` only for id/path.
- **D2 org casing:** `SprustonLab` (camelCase) everywhere in docs/README.
- **D3 internal docs:** MOVE the 4 audit files (AGENT_TEAMS_PIVOT_PLAN,
  APPENDIX.MD, SCOPE_AUDIT_v2.1, SPEC_GROUNDING_AUDIT) OUT of the mkdocs
  source so they are not published. (Audit-doc claudechic-scrub status =
  OPEN, see directive below.)
- **D9 SPEC pointer:** the new Contributor/Architecture docs site BECOMES
  the authoritative design reference; DROP the external claudechic-private
  pointer in README (24, 290-291) and CHANGELOG (6); link to the docs.
- Defaults accepted: docs own the reference tables (README links in);
  `.claude/` gitignored; README:5 "claudechic-style" softened; docs teach
  the manifest's real 10-phase flow.

## CLAUDECHIC REMOVAL DIRECTIVE (2026-05-25, strongest)

Get claudechic OUT of the repo entirely -- replace ANY claudechic
occurrence with the operon equivalent, INCLUDING live tokens/identifiers,
not just comments. Notably rename `${CLAUDECHIC_ARTIFACT_DIR}` ->
`${ARTIFACT_DIR}` and drop the back-compat alias (behavior-preserving,
WITH test coverage). This LIFTS the earlier "comment text only" limit.
Goal: `grep -ri claudechic` returns nothing (modulo any user-approved
exception). The "inspiration" source repo at
`/groups/spruston/home/moharb/claudechic` stays external; nothing
claudechic should remain inside operon-plugin.

## Specification checkpoint resolutions (2026-05-25)

- **Audit docs = DELETE** (supersedes D3 "move out"): remove all 4 --
  `docs/AGENT_TEAMS_PIVOT_PLAN.md`, `docs/APPENDIX.MD`,
  `docs/SCOPE_AUDIT_v2.1.md`, `docs/SPEC_GROUNDING_AUDIT.md`. They held 55
  of 90 claudechic hits. `docs/` is then clean for the mkdocs site.
  Implementation must repair any in-repo links to these deleted files.
- **project_integrator = DELETE the directory**
  `plugins/operon-plugin/workflows/project_team/project_integrator/`
  (only an identity.md; no manifest/code refs; breaks nothing).
- **Acceptance gate (now absolute):** `grep -rni claudechic` over the
  repo (excluding .git/.venv/caches and the external claudechic repo)
  returns ZERO -- no exemptions.
- **D8 boundary:** scrub edits docstrings/comments/strings + the
  behavior-preserving `${CLAUDECHIC_ARTIFACT_DIR}` -> `${ARTIFACT_DIR}`
  rename (alias is dead -- zero live templates emit it). MUST NOT change
  control flow, rule semantics, or the SPEC. Regression test asserts
  `${ARTIFACT_DIR}` STILL substitutes (load-bearing), not just that the
  old token is gone.
- **CHANGELOG:** scrub its claudechic refs + fix the dangling private
  pointer (line 6); no broader CHANGELOG rewrite.
- **Scrub extends to `tests/`:** `.project_team/` drift at
  `tests/scenarios/project_team_workflow.py:2191` is in scope (fix
  misleading paths only, not test behavior/assertions).
- **Fix stale phase mirrors this run:** coordinator identity (7-phase)
  and `project_team/README.md` (4-phase) corrected; docs cite the
  10-phase manifest as source of truth. Also remove the dead `git_setup`
  agent refs + broken links in `project_team/README.md`.

## Out of scope

- Rewriting plugin code or the SPEC (changing logic/behavior). NOTE the
  narrow exception in scope item 6: amending claudechic *comment* text
  in code/config/manifests is allowed; their logic must not change.
- Copying claudechic content/branding verbatim.
