# Appendix -- operon-plugin documentation set

Non-operational context for `spec.md`: rationale, rejected alternatives,
things NOT to do, provenance, and the risk narrative. Nothing here is
needed to MAKE the changes; it explains WHY the spec is shaped as it is.

## A. Why Audience x Surface (the docs decomposition)

The docs decompose along three axes:
- Audience (user, contributor) -- the folder split `user-guide/` vs
  `dev/`.
- Surface (install/runtime, MCP tools, workflows+phases, guardrail Rules,
  hooks, skills, sessions/runs) -- the files within each folder.
- Lifecycle (install -> activate -> drive -> extend) -- section ordering
  inside a page, NOT a folder.

Lifecycle is deliberately kept in-page. Folding it into the tree would
multiply folders (audience x surface x lifecycle) without adding separable
content -- the same surface at the same audience does not split into
distinct install/extend pages worth maintaining.

The compositional law (one canonical owner per fact; everything else
links) is what prevents drift. Skeptic's C1 challenge was that a LAW with
no gate erodes the first time someone inlines a table "for convenience,"
and proposed a CI grep to enforce it. The user DECIDED against CI/test
enforcement for this work: the one-canonical-owner rule is an authoring
CONVENTION, verified by HUMAN DIFF REVIEW at the §9 checkpoint, not a gate.
(This deliberately reverses skeptic C1's enforcement recommendation while
keeping the convention itself.) The full axis analysis lives in
`specification/composability.md`.

## B. Per-line replacement reference (the 34 pure-text scrub edits)

The proposed operon replacement for each claudechic hit lives in
`research.md` (researcher's inventory tables). Highlights an implementer
should not re-derive:

- `rules.yaml` comments name `warn_sudo` and `log_git_operations`. Those
  are operon's OWN live rule ids (confirmed in the manifest). Drop only the
  "Mirrors claudechic's ..." framing; KEEP the ids.
- `project_team.yaml` header block (lines 1-12) reads as a unit -- rework
  the whole comment block, not line-by-line, so the result is coherent
  prose. Line 7 (the `${CLAUDECHIC_ARTIFACT_DIR}` rename note) is deleted
  with the token removal (§6a).
- `workflow.py` docstrings at :424/:433/:461 mention both token spellings;
  they collapse to "`${ARTIFACT_DIR}`" once the alias is gone.
- README:5 "claudechic-style" is editorial framing, not a functional
  reference -- the README-rework owner restates the value prop in operon's
  own terms.

## C. mkdocs.yml -- what was dropped from the claudechic model and why

claudechic's `mkdocs.yml` was the STRUCTURAL model only; none of its
content is carried over. Dropped:
- `site_url` -- claudechic-specific (`mrocklin.github.io/claudechic`);
  operon's Pages URL differs and an empty/foreign `site_url` can break
  absolute asset links.
- `extra.analytics` (Google property) -- claudechic's analytics.
- `extra_css: stylesheets/extra.css` -- not ported; no operon stylesheet
  exists. Adding the key without the file breaks the build.
- claudechic's product nav pages (Style, Privacy, Related Work, Asciinema,
  Compaction) -- no operon analog. Do NOT port them.

Kept verbatim: `theme.features` (content.code.copy, content.tabs.link) and
the full `markdown_extensions` set -- generic Material capabilities, not
claudechic content.

## D. Token mechanics -- why the rename is low-risk (skeptic V1 / researcher)

`${CLAUDECHIC_ARTIFACT_DIR}` is substituted by a chained `.replace()` in
`workflow.py:438-441` alongside the primary `${ARTIFACT_DIR}`. Researcher
grepped every workflow YAML, every phase `.md`, `_smoke.yaml`, and all
tests for the alias spelling: zero hits. Every live path uses
`${ARTIFACT_DIR}` (project_team.yaml x9, coordinator/setup.md x3,
_smoke.yaml x3, the `*/testing-specification.md` files, etc.). So the alias
branch is dead code; dropping it changes the function but not observable
behavior for any shipped workflow.

The framing trap (skeptic V1): checking only "the claudechic token is
gone" checks the wrong thing. The load-bearing requirement is the INVERSE
-- 7+ live files depend on `${ARTIFACT_DIR}` still substituting. Per the
user's decision, this is confirmed by the EXISTING test suite passing (the
live templates already exercise `${ARTIFACT_DIR}` substitution), not by a
new bespoke test. If no existing test exercises substitution, the
implementer notes that gap for the user at the checkpoint (spec §6a/§9).

## E. Risk narrative

- **R1 -- leftover scan breadth.** The footprint is wider than
  project_integrator's identity.md: 90 total claudechic hits across 15
  files. 55 were in the four internal audit docs -- now DELETED, removing
  those 55 outright; 35 actionable hits remain in source/manifest/README/
  CHANGELOG. The footprint also reaches the TEST SUITE -- `.project_team/`
  drift at `tests/scenarios/project_team_workflow.py:2191` / :2354-2355
  (skeptic V2). The backstop against an incomplete upfront list is the
  implementation's one-off `grep -rni claudechic` self-check plus the user
  diff review at the §9 checkpoint -- not a CI gate.

- **R2 -- audit trail not yet tracked on develop.** The work products are
  the audit trail and live in `operon-runs/` (the ephemeral `.operon/`
  runtime is gitignored on all branches and never tracked). The
  branch-model task (spec §5) makes `operon-runs/` tracked on `develop`
  and ignored on `main`; the tracked-on-develop / rejected-on-main test
  commit proves it. Until that lands, `operon-runs/` is untracked, so the
  audit trail is not actually versioned yet.

- **R3 -- Pages prereqs + CI/dep coupling.** operon has NO CI today. Two
  coupled hazards: (a) `docs.yml` and the mkdocs dep-add must land together
  or the first `main` push fails at `uv run mkdocs build`; (b) GitHub Pages
  must be enabled by the user (manual step, spec §10) before the deploy job
  can publish. Run `uv sync --dev && uv run mkdocs build` locally once
  before pushing.

- **R4 -- Pages base-path / empty site_url.** With `site_url` dropped and
  the project published at a `/operon-plugin/` base path, absolute asset
  links can break. Verify rendered links after the first deploy. A
  post-deploy verification note, not a build-time gate.

- **R5 -- token rename blast radius.** Covered in §D: low because the alias
  is dead code. The function changes, so the EXISTING test suite must stay
  green over `${ARTIFACT_DIR}` substitution; if no existing test covers it,
  that gap is surfaced to the user at the checkpoint (no new test is added
  to verify the cleanup).

## F. What NOT to do

- Do NOT change rule, manifest, or workflow LOGIC while scrubbing
  claudechic prose. The scrub is comments/strings/docstrings only; the one
  exception is the token rename (spec §6a), which is behavior-preserving
  and confirmed by the existing test suite (no new test).
- Do NOT alter `project_team.yaml:98` -- the
  `(?i)(?:\.project_team|\.operon)/...spec...` regex matches both layouts
  by design and contains no claudechic string. It is live rule logic.
- Do NOT remove the rule ids `warn_sudo` / `log_git_operations` from
  rules.yaml comments; they are operon's real ids. Drop only the "Mirrors
  claudechic" framing.
- Do NOT "correct" the git remote URL host casing `sprustonlab` ->
  `SprustonLab`. The org spelling fix (spec §6f) applies to docs/README
  PROSE; the URL is a platform fact.
- Do NOT port claudechic's product nav pages, `extra_css`, or analytics.
- The 4 internal audit files are DELETED, not relocated (checkpoint
  resolution). Do NOT leave them in the repo and merely exclude them from
  the build -- an exclude glob is easy to regress, `APPENDIX.MD`'s
  uppercase extension can dodge globs, AND leaving them would keep 55
  claudechic hits alive (the user diff review would flag them). After
  deletion, remove any dangling in-repo references to their filenames.
- Do NOT reproduce the stale 7-phase (coordinator/identity.md) or 4-phase
  (project_team/README.md) flow descriptions, and do NOT transcribe the
  10-phase list as a static 4th copy -- cite the manifest as source.
- Do NOT collapse the three git-safety artifacts to "fewer parts" (skeptic
  simplicity note). Each guards a distinct surface (local commit / branch
  state / PR); collapsing reintroduces a hole.
- Do NOT blindly delete the README/CHANGELOG spec pointers -- repoint them
  to the docs site, or the reader is orphaned (researcher F2).

## G. Provenance (where the shape came from)

- The docs structure mirrors claudechic's `docs/` + `docs/dev/` split as a
  STRUCTURAL model only -- no claudechic docs CONTENT is carried over.
- The CI (`docs.yml`) and the two guard artifacts are adapted from
  claudechic's `docs.yml`, `no-project-team-additions-on-main.yml`, and the
  `block-project-team-on-main` pre-commit hook by a mechanical
  `.project_team/` -> `operon-runs/` swap (the guards target the tracked
  work products, not the ephemeral `.operon/` runtime), preserving the
  `--diff-filter=AM` (additions/modifications only; deletions allowed)
  logic.
- The authoritative design reference WAS an external pointer into the
  claudechic-private repo
  (`../claudechic/.project_team/claude_code_port/SPEC*.md`), unreachable to
  anyone who clones operon-plugin. That pointer is the concrete instance of
  the "agents confused by claudechic" problem; the docs site replaces it
  (decision D9).

## H. Decision log (settled)

- D1 -- run instance term: `operon-session` (run_name = identifier,
  run-dir = path).
- D2 -- org spelling in prose: `SprustonLab`.
- D3 -- the 4 internal audit files: DELETE (checkpoint resolution,
  supersedes the earlier "move out of the mkdocs source"). See §I for why
  delete beat move-out.
- D4 -- reference-table ownership: docs own; README links in.
- D5 -- `.claude/` gitignored (local team/agent state).
- D8 -- claudechic removal lifts the "comment text only" limit: live
  tokens/identifiers in scope (the `${ARTIFACT_DIR}` rename). Bounded:
  prose + behavior-preserving rename, no logic/SPEC change. Reconciled with
  the "no rewriting plugin code" out-of-scope line and RESOLVED at the
  checkpoint (user_alignment WARNING withdrawn).
- D9 -- the Contributor/Architecture docs site is the authoritative design
  reference; drop the external claudechic-private SPEC pointer; repoint.
- D10 -- README:5 "claudechic-style" framing: soften/drop.
- D11 -- project_integrator stale identity: DELETE the directory (skeptic
  C4 + user_alignment: no manifest/roster references, so nothing to fix up;
  simplest and most correct remediation). Permanent deletion RESOLVED at
  the checkpoint.

## I. Why DELETE beat the alternatives for the audit docs (resolved)

The audit docs (AGENT_TEAMS_PIVOT_PLAN, APPENDIX.MD, SCOPE_AUDIT_v2.1,
SPEC_GROUNDING_AUDIT) held 55 of the 90 claudechic hits. Three options were
on the table; the user chose DELETE at the checkpoint.

- **Rejected -- keep + scrub:** scrubbing claudechic from docs whose PURPOSE
  is to cross-reference claudechic destroys their meaning, for large effort.
- **Rejected -- move out + leave their claudechic refs:** keeps 55 hits
  alive in the repo, so the `grep -rni claudechic` self-check never reaches
  zero; relies on an exclude mechanism that can regress; leaves stale
  port-era content around.
- **Chosen -- DELETE:** removes the 55 hits outright, so the
  `grep -rni claudechic` self-check reaches zero with no carve-out, and
  leaves `docs/` clean so it is the mkdocs `docs_dir` directly (no
  exclude-glob or relocation gymnastics). Cost: the port-audit history is
  dropped from the working tree, but it remains recoverable from git if
  ever needed.

The two former companion confirmations are also resolved: the D8 boundary
(prose + behavior-preserving rename, no logic/SPEC change) and the §6d
permanent deletion of `project_integrator/`. No open items remain.
