# Leadership First-Pass Findings -- `docs` run

Synthesis of the 5 leadership/supporting reports (composability,
terminology, skeptic, user_alignment, researcher). Read-only first pass;
no project files edited. Feeds the Specification phase.

## Agreed docs architecture (composability)

- Decompose **Audience x Surface**; Lifecycle (install->activate->drive
  ->extend) is in-page ordering, not a folder.
- Folder shape: `docs/index.md`, `docs/user-guide/*`, `docs/dev/*`
  (mirrors claudechic's `docs/` + `docs/dev/` split).
- LAW: each page documents one (audience, surface) cell and LINKS to the
  canonical source rather than inlining -> prevents drift.
- Real surfaces live under `plugins/operon-plugin/` (NOT repo root):
  workflows/, skills/, hooks/, bin/, rules.yaml, .mcp.json, plus
  `src/operon_mcp_server/{tools,checks}/`.

## Toolchain recipe (researcher)

- mkdocs.yml: site_name->operon-plugin, DROP site_url + analytics, KEEP
  theme material + features (content.code.copy, content.tabs.link), KEEP
  all markdown_extensions, REPLACE nav with user-guide/ + dev/ split.
- `.github/workflows/docs.yml`: adapt claudechic's near-verbatim
  (uv -> `uv run mkdocs build` -> upload-pages-artifact@v3 ->
  deploy-pages@v4; on push main + workflow_dispatch).
- pyproject `[dependency-groups] dev`: ADD `mkdocs>=1.6.1`,
  `mkdocs-material>=9.7.1`.
- Guard artifacts: swap `.project_team/` -> `.operon/` in claudechic's
  `no-project-team-additions-on-main.yml` + `block-project-team-on-main`
  hook; keep `--diff-filter=AM` (allow deletions) logic.
- VALIDATION (skeptic R3 + researcher): operon has NO CI yet; implementer
  MUST run `uv sync --dev && uv run mkdocs build` once; docs.yml + the
  mkdocs dep-add must land together or first push to main breaks. Empty
  site_url + project Pages base-path `/operon-plugin/` can break absolute
  asset links -> verify after first deploy.

## Glossary (terminology) -- canonical terms

plugin, marketplace, MCP server, MCP tool (`mcp__operon__*`), workflow,
phase, role/agent, Coordinator, Leadership, guardrail Rule
(deny/warn/log), override, acknowledge/ack, skill (slash command),
operon-session/run, team/roster, artifact_dir. (Full one-liners in the
terminology report.)

## Phase-count drift (terminology #2) -- IMPORTANT

- `project_team.yaml` (SOURCE OF TRUTH) = 10 phases: vision, setup,
  leadership, specification, implementation, testing-vision,
  testing-specification, testing-implementation, documentation, signoff.
- `coordinator/identity.md` roadmap = stale 7-phase mirror (collapses
  the 3 testing phases, omits documentation).
- `project_team/README.md` = a THIRD variant (4 phases).
- Docs MUST teach the 10-phase manifest. Do not reproduce 7/4 variants.

## Claudechic-leftover catalog (researcher + skeptic + terminology)

### (a) Misleading / wrong-for-operon -- FIX
- CRITICAL `workflows/project_team/project_integrator/identity.md` --
  entire file = conda/`source activate`/`commands/`/`envs/` (none exist;
  real env = uv + pyproject + bin shim). project_integrator SKIPPED this
  run but stale file still ships. -> DECISION D11 (rewrite vs delete).
- HIGH `workflows/project_team/README.md`: :81 + :87 reference a
  non-existent `git_setup` agent; :7 "AI_PROJECT_TEMPLATE / docs/
  getting-started.md" (neither exists); :17,:25 `.project_team/` paths.
- MEDIUM `coordinator/documentation.md:8` and `coordinator/setup.md:4`
  use `.project_team/` (engine creates `.operon/`). (I hit setup.md:4
  myself during Setup.) setup.md:9-10 already names BOTH conventions.
- NOT stale: `project_team.yaml:98` regex matches BOTH `.project_team`
  and `.operon` by design -- leave.

### (b) Claudechic attribution comments -- SCRUB per user (comment text only)
~22 hits (prose/comments only, NEVER logic):
- `rules.yaml`: lines 8, 24, 44, 54 ("Mirrors claudechic...", "per
  claudechic convention").
- `project_team.yaml` header: lines 1, 3, 9, 11-12, 47.
- `workflows/project_team/README.md:5` blockquote "Ported from
  claudechic..." (high-visibility, user-facing).
- `src/operon_mcp_server/`: rules.py:5,12,18,86,353,400;
  workflow.py:159,171,182,433(prose only); checks/builtins.py:4,45,359;
  checks/protocol.py:3; tools/request_override.py:28;
  tools/restore_operon_session.py:31; bootstrap.py:63.

### (c) Human-call flags
- `${CLAUDECHIC_ARTIFACT_DIR}` is a LIVE token (workflow.py:440 does
  `.replace(...)`; described at 424/433/461 + project_team.yaml:7).
  Renaming = behavior change, OUTSIDE comment-only scope. -> DECISION D8
  (default: keep token, scrub surrounding prose only).
- README.md:24,290-291 + CHANGELOG.md:6 point to claudechic-PRIVATE
  `../claudechic/.project_team/claude_code_port/SPEC*.md` as the
  authoritative spec -- functional pointer, unreachable to repo cloners
  (THE "agents confused" problem). -> DECISION D9 (where does the SPEC
  live post-rework).
- README.md:5 "claudechic-style multi-Agent orchestration" -- origin
  framing. -> DECISION D10 (soften vs keep; default: soften).
- Internal audit docs `docs/SPEC_GROUNDING_AUDIT.md`,
  `AGENT_TEAMS_PIVOT_PLAN.md`, `APPENDIX.MD` have 40+ claudechic refs
  whose PURPOSE is cross-referencing claudechic. STATUS says leave
  as-is. -> DECISION D6 (confirm EXEMPT from scrub). Note `APPENDIX.MD`
  uppercase ext may dodge mkdocs include globs.

## Risks (skeptic)
R1 leftover scan broader than just project_integrator (covered above).
R2 develop un-ignore NEVER applied -> `.operon/docs/*` untracked on both
   branches (STATUS corrected; OPEN implementation task; verify w/ test
   commit). R3 Pages prereqs + CI/dep coupling. R4 org casing
   `sprustonlab` (remote) vs `SprustonLab` (pyproject/marketplace) ->
   DECISION D2. R5 don't port claudechic-only nav pages (style/privacy/
   related/asciinema). R6 existing `docs/*.md` would publish unless
   excluded / separate docs_dir -> DECISION D3.

## Open DECISIONS for the user (Specification checkpoint)
- D1 canonical run term (rec: `operon-session`).
- D2 repo org casing (`sprustonlab` vs `SprustonLab`).
- D3 existing internal `docs/*.md` vs mkdocs docs_dir (exclude / move /
  separate dir).
- D4 reference-table ownership (rec: docs own, README links in).
- D5 `.claude/` gitignore (rec: ignore -- local team/agent state).
- D6 confirm internal audit docs EXEMPT from claudechic scrub.
- D7 CHANGELOG.md:6 dangling private-path pointer -- fix it too? (rec:
  fix the pointer; no full CHANGELOG rework).
- D8 RESOLVED (user, 2026-05-25): RENAME `${CLAUDECHIC_ARTIFACT_DIR}` ->
  `${ARTIFACT_DIR}` and DROP the back-compat alias. Behavior-preserving,
  WITH test coverage. The "comment-only" limit is LIFTED -- claudechic
  must be removed entirely (tokens + identifiers + comments + strings).
- D9 where the authoritative SPEC lives post-rework.
- D10 README:5 "claudechic-style" framing (rec: soften).
- D11 project_integrator stale identity.md: rewrite vs delete dir+row.
