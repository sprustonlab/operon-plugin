# Specification -- operon-plugin documentation set

Operational specification. Defines WHAT to build, HOW pieces connect, and
WHICH constraints apply. Self-contained: every term is defined in the
Glossary (§0); no cross-file reference is load-bearing. Rationale,
rejected alternatives, provenance, and risk narrative live in
`appendix.md` (not needed to make the changes).

All Specification-checkpoint decisions are RESOLVED; this spec has no open
items. The authoritative resolutions are recorded in `userprompt.md`
("Specification checkpoint resolutions") and baked in below.

## 0. Glossary (canonical terms; casing is normative)

- **operon-plugin** (lowercase, hyphenated): this repository; both a
  single-plugin marketplace and the plugin it ships.
- **plugin:** the unit Claude Code loads from `plugins/operon-plugin/`
  (manifest `.claude-plugin/plugin.json`).
- **marketplace:** the Claude Code plugin-distribution manifest
  (`.claude-plugin/marketplace.json`); this repo is a single-plugin
  marketplace.
- **MCP server:** the Python package `src/operon_mcp_server/`, registered
  under server name `operon` in `.mcp.json`, launched by the bin shim.
- **bin shim:** `plugins/operon-plugin/bin/operon-mcp-server` (bash) +
  `.cmd`; resolves a Python interpreter via the `uv -> python3 -> python`
  ladder and starts the MCP server.
- **MCP tool:** one callable the MCP server exposes, addressed as
  `mcp__operon__<name>` (full namespace on first mention; bare name
  thereafter). Source: `src/operon_mcp_server/tools/`.
- **hook:** a Claude Code event handler in
  `plugins/operon-plugin/hooks/hooks.json`. operon ships `PreToolUse`
  (Rule enforcement) and `Stop` (reply-nudge tap).
- **skill (slash command):** a user-invocable `/<name>` whose body proxies
  to MCP tools. Source: `plugins/operon-plugin/skills/<name>/SKILL.md`.
  Shown with the leading slash: `/project_team`, `/restore`, `/rules`.
- **workflow:** a named, phase-structured process defined by a manifest
  `plugins/operon-plugin/workflows/<id>/<id>.yaml`; identified by
  `workflow_id`.
- **phase:** one ordered stage in a workflow; phase ids are lowercase,
  hyphen-joined where compound (`testing-vision`).
- **advance-check:** a gate that must pass before a phase advances
  (`manual-confirm`, `file-exists-check`, `artifact-dir-ready-check`).
- **advance / advance_phase:** moving to the next phase via the
  `advance_phase` MCP tool, which first evaluates advance-checks.
- **role:** the TYPE of a team member; slug is lowercase snake_case
  (`coordinator`, `project_integrator`); display name is Title Case
  (Coordinator, Terminology Guardian). Defined by `identity.md`.
- **agent:** a running instance of a role within an operon-session.
- **Coordinator:** the orchestrating agent; the workflow `main_role`.
- **Leadership:** the four review agents -- Composability, Terminology
  Guardian, Skeptic, User Alignment. Capitalized when naming the group;
  the phase id is lowercase `leadership`.
- **guardrail Rule:** a declarative deny/warn/log policy matched on tool +
  input, scoped by role/phase. Source: `plugins/operon-plugin/rules.yaml`
  + manifest `rules:` blocks.
- **deny / warn / log:** the three enforcement tiers (lowercase). deny =
  hard block (clear with an override); warn = soft block (clear with an
  ack); log = silent audit.
- **override:** a one-shot escape token for a deny-tier action
  (`request_override`). **ack/acknowledge:** a TTL-bounded escape token
  for a warn-tier action (`acknowledge_warning`). **escape token:**
  umbrella for both.
- **operon-session:** an activated workflow instance with its own phase
  state, roster, escape tokens, and artifact dir; lives at
  `<project>/.operon/<run_name>/`. CANONICAL term for a run instance. Do
  NOT write `operon-run` or `operon session`.
- **run_name:** the identifier (directory leaf) of one operon-session.
  **run-dir:** its on-disk path. Neither is a synonym for the session.
- **artifact_dir:** the per-session output directory, substituted as the
  `${ARTIFACT_DIR}` token. There is no `${CLAUDECHIC_ARTIFACT_DIR}` alias.
- **Claude Code:** two words, both capitalized; the host application.
- **claudechic:** lowercase; the external inspiration project. Per the
  removal directive it must not appear in shipped operon-plugin files at
  all (named here only to specify what is removed).
- **SprustonLab:** camelCase; the repo org in all docs/README prose.
- **docs site:** the MkDocs/Material site built from `docs/`.
- **User Guide:** `docs/user-guide/*` (audience = user). **Contributor/
  Architecture Guide:** `docs/dev/*` (audience = contributor). This guide
  is the AUTHORITATIVE design reference (replaces the former external
  claudechic-private spec pointer). Present `dev/` to readers AS the
  "Contributor/Architecture Guide", not relabeled merely "dev".

## 1. Docs architecture

Build the docs tree exactly:

```
docs/
  index.md
  user-guide/
    install.md
    quickstart.md
    workflows-and-phases.md
    agents-and-messaging.md
    guardrails.md
    sessions.md
    mcp-tools-reference.md
  dev/
    architecture.md
    mcp-server.md
    workflow-engine.md
    rules-and-enforcement.md
    hooks.md
    skills.md
    testing.md
    contributing.md
```

Page rules (anti-drift authoring guideline; verified by HUMAN DIFF REVIEW
at the §9 checkpoint, NOT enforced by CI):
- Each page documents one (audience, surface) pair.
- The docs site OWNS each reference table (MCP tools, hooks, env vars,
  skills). Every other page and the README LINK to the owning page; they
  do not restate the table.
- Contributor pages own a surface's schema/internals; user pages own the
  experience and link across.
- `dev/architecture.md` opens with a repo-layout callout (see §6 paths)
  and is the surface map for the rest of `dev/`.

Canonical homes (concept -> owning page; other pages link, never
redefine):
- plugin / marketplace / install / bin shim -> `user-guide/install.md`.
- MCP server / MCP tools / hooks -> `dev/architecture.md` +
  `dev/mcp-server.md`; the MCP-tool reference table ->
  `user-guide/mcp-tools-reference.md`.
- workflow / phase / advance-check / the phase flow ->
  `user-guide/workflows-and-phases.md`.
- role / agent / Coordinator / Leadership / roster ->
  `user-guide/agents-and-messaging.md`.
- operon-session / run_name / run-dir / artifact_dir ->
  `user-guide/sessions.md`.
- guardrail Rule / deny-warn-log / override / ack / escape token ->
  `user-guide/guardrails.md`.
- skills -> `user-guide/mcp-tools-reference.md` (commands section) or a
  dedicated commands section.

Phase-flow content rule: the docs teach the workflow's phases as defined
by the manifest
`plugins/operon-plugin/workflows/project_team/project_team.yaml`. The
manifest defines **10 phases**, in order: vision, setup, leadership,
specification, implementation, testing-vision, testing-specification,
testing-implementation, documentation, signoff. The docs CITE the
manifest as the source of truth rather than transcribing a static copy
that can drift. Do NOT reproduce the stale 7-phase or 4-phase variants
(see §6c). A run instance is an **operon-session**.

Internal audit files DELETED (checkpoint resolution, supersedes the
earlier move-out decision): remove all four --
`docs/AGENT_TEAMS_PIVOT_PLAN.md`, `docs/APPENDIX.MD`,
`docs/SCOPE_AUDIT_v2.1.md`, `docs/SPEC_GROUNDING_AUDIT.md`. They held 55 of
the 90 claudechic hits; deleting them removes those 55 outright, leaving no
claudechic content to carry forward. Consequence: after deletion `docs/`
contains ONLY the mkdocs source tree above, so `docs/` is the mkdocs
`docs_dir` directly -- no exclude-glob or relocation gymnastics.

Dangling-reference repair (required): before/after deleting the four
files, grep the repo for in-repo links or references to them (by filename,
e.g. `grep -rn "AGENT_TEAMS_PIVOT_PLAN\|APPENDIX.MD\|SCOPE_AUDIT\|SPEC_GROUNDING_AUDIT"`)
and remove or repoint every hit so no dangling link remains (README,
CHANGELOG, STATUS, other docs, code comments).

## 2. mkdocs.yml (exact config)

```yaml
site_name: operon-plugin
site_description: A Guided, Self-Revisable plugin for AI Research Software Operations in Claude Code

nav:
  - Home: index.md
  - User Guide:
      - Install: user-guide/install.md
      - Quickstart: user-guide/quickstart.md
      - Workflows and Phases: user-guide/workflows-and-phases.md
      - Agents and Messaging: user-guide/agents-and-messaging.md
      - Guardrails: user-guide/guardrails.md
      - Sessions: user-guide/sessions.md
      - MCP Tools Reference: user-guide/mcp-tools-reference.md
  - Contributor/Architecture Guide:
      - Architecture: dev/architecture.md
      - MCP Server: dev/mcp-server.md
      - Workflow Engine: dev/workflow-engine.md
      - Rules and Enforcement: dev/rules-and-enforcement.md
      - Hooks: dev/hooks.md
      - Skills: dev/skills.md
      - Testing: dev/testing.md
      - Contributing: dev/contributing.md

theme:
  name: material
  features:
    - content.code.copy
    - content.tabs.link

markdown_extensions:
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.superfences
  - pymdownx.inlinehilite
  - pymdownx.tabbed:
      alternate_style: true
  - admonition
  - pymdownx.details
  - attr_list
  - md_in_html
```

Constraints: NO `site_url`; NO `extra.analytics`; NO `extra_css` unless a
stylesheet is actually added (claudechic's `stylesheets/extra.css` is not
ported). The top-level nav label for `dev/` is the user's exact wording
"Contributor/Architecture Guide".

## 3. CI and guard artifacts

### 3a. `.github/workflows/docs.yml` (net-new; build + deploy)

```yaml
name: Deploy Docs

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --dev
      - run: uv run mkdocs build
      - uses: actions/upload-pages-artifact@v3
        with:
          path: site
      - uses: actions/deploy-pages@v4
        id: deployment
```

### 3b. `.github/workflows/no-operon-runs-additions-on-main.yml` (lives on main)

PR guard into `main`. Reject diffs that ADD or MODIFY `^operon-runs/`
paths; allow deletions (keep `--diff-filter=AM`). Adapt claudechic's
`no-project-team-additions-on-main.yml`: swap `.project_team/` ->
`operon-runs/` in the name, messages, and the grep pattern (`grep
'^operon-runs/'`). Keep `fetch-depth: 0` and the `base...HEAD` diff. (The
target is `operon-runs/` -- the tracked work products; `.operon/` is never
committed so it needs no guard.)

### 3c. `block-operon-runs-on-main` pre-commit hook (lives on both branches)

Add a local hook to `.pre-commit-config.yaml`:

```yaml
- id: block-operon-runs-on-main
  name: Block operon-runs/ on main
  entry: bash -c 'branch=$(git rev-parse --abbrev-ref HEAD); if [ "$branch" = "main" ]; then echo "operon-runs/ files must not be committed to main (develop only)"; exit 1; fi'
  language: system
  files: ^operon-runs/
  pass_filenames: false
```

If the repo has no `.pre-commit-config.yaml`, create one containing only
this local hook (do not port claudechic's ruff/pyright hooks unless
asked).

The three git-safety artifacts (per-branch `.gitignore` §5, this
pre-commit hook, the CI guard §3b) are intentionally distinct -- each
guards a different surface (local commit / branch state / PR). Do not
collapse them.

## 4. pyproject.toml dependency add

In `[dependency-groups]`, add to the existing `dev` list (keep all current
entries -- pytest, pytest-timeout, pexpect, pyte):

```
mkdocs>=1.6.1
mkdocs-material>=9.7.1
```

Coupling constraint: this add and `.github/workflows/docs.yml` MUST land
in the same change. The CI runs `uv sync --dev` then `uv run mkdocs
build`; if the deps are absent when `docs.yml` first runs on `main`, the
deploy fails.

## 5. Git branch model

Two distinct directories, two distinct treatments:

- **`.operon/<run>/`** -- operon MCP runtime + phase state ONLY (mailbox,
  `_handles/`, `phase_state.json`, `state.json`, `inbox_cursor.json`,
  `guardrail_log.jsonl`, `agents.json`, `overrides/`). EPHEMERAL.
  Gitignored on ALL branches; never committed; no develop tracking and no
  guard needed (it is never staged). `main`'s root `.gitignore` already
  ignores `.operon/` (with the `!tests/fixtures/**/.operon/**` fixture
  exceptions); keep that exactly, on both branches.
- **`operon-runs/<run>/`** -- the project_team WORK PRODUCTS (`STATUS.md`,
  `userprompt.md`, `spec.md`, `appendix.md`, `research.md`,
  `leadership_findings.md`, `specification/`). This is the audit trail
  worth versioning. The develop/main split applies HERE:
  - `develop` TRACKS `operon-runs/`.
  - `main` IGNORES `operon-runs/` (it is develop-only audit-trail
    material).

Implementation:
- On `main`, add `operon-runs/` to the root `.gitignore` with the in-file
  comment:
  `# operon-runs/ = project_team work products: develop-only audit trail.`
  `# main: ignored here. develop: intentionally NOT ignored. The`
  `# block-operon-runs-on-main pre-commit hook + PR guard enforce the split.`
- On `develop`, `operon-runs/` is NOT ignored (tracked).
- Leave `.operon/` ignored on BOTH branches; do not add any "track
  .operon on develop" rule.
- Guard files (§3b, §3c) live on `main`; they block `operon-runs/`, not
  `.operon/`.

ACCEPTANCE: a test commit touching `operon-runs/docs/<file>` is TRACKED on
`develop` and REJECTED on `main` (pre-commit hook locally + PR guard in
CI). `.operon/` stays ignored on both branches.

## 6. CLAUDECHIC REMOVAL change list

Directive: replace ANY claudechic occurrence in repo source with the
operon equivalent -- comments, strings, identifiers, AND live tokens.
Bounded scope: the scrub MAY edit docstrings/comments/strings in
`src/*.py` and perform the behavior-preserving `${ARTIFACT_DIR}` token
rename; it MUST NOT alter control flow, rule semantics, or design beyond
that. There is NO exemption -- the four internal audit docs that held the
remaining hits are DELETED (§1), so every remaining claudechic occurrence
must be removed.

Inventory baseline (researcher full scan): 90 total hits across 15 files.
55 were in the four internal audit docs -- those files are now DELETED
(§1), removing those 55 hits outright. The remaining **35 actionable hits
across 11 files** are scrubbed per §6b-§6f. Of the 35, exactly ONE is
behavior-affecting (the token alias removal, §6a); the other 34 are pure
text.

Two action types:
- **SCRUB:** rewrite comment/string/docstring prose to state the operon
  fact without naming claudechic. No logic/behavior change.
- **RENAME:** change a live token; behavior-preserving. Verified by the
  EXISTING test suite passing, not by a new bespoke test (§6a).

### 6a. Live token rename (RENAME -- the ONE behavior-affecting item)

`src/operon_mcp_server/workflow.py`, function `_expand_artifact_dir`,
lines 438-441, today chains two substitutions:

```python
return value.replace("${ARTIFACT_DIR}", artifact_dir).replace(
    "${CLAUDECHIC_ARTIFACT_DIR}", artifact_dir
)
```

ACTION: drop the second `.replace(...)` so only `${ARTIFACT_DIR}` is
substituted; delete the alias mentions in the docstrings at :424, :433,
:461; delete the `project_team.yaml:7` comment documenting the rename.

The alias is dead code: no live template, manifest, phase `.md`, or test
emits `${CLAUDECHIC_ARTIFACT_DIR}` (researcher grepped all of them -- zero
hits). Every live path already uses `${ARTIFACT_DIR}`.

VERIFICATION (no new bespoke test): the load-bearing property is that
`${ARTIFACT_DIR}` STILL expands to the run's artifact dir. Rely on the
EXISTING test suite, which should already exercise `${ARTIFACT_DIR}`
substitution (the live templates use it in 7+ files). The implementer
confirms that existing coverage runs green after the alias removal; if no
existing test exercises substitution, the implementer NOTES that gap for
the user at the checkpoint rather than adding a new maintained test.

### 6b. Source comment/string scrub (SCRUB -- 34 pure-text edits)

Verified file:line anchors (researcher inventory). The edit pattern:
remove the claudechic framing, keep the operon fact. Per-line proposed
replacements are in `research.md` (researcher's inventory tables).

- `src/operon_mcp_server/workflow.py`: :159, :171, :182 (prose; the
  :424/:433/:461 docstring hits are handled with 6a).
- `src/operon_mcp_server/rules.py`: :5, :12, :18, :86, :353, :400.
- `plugins/operon-plugin/rules.yaml`: :8, :24, :44, :54. CAVEAT: the
  comments name rule ids `warn_sudo` and `log_git_operations` -- those are
  operon's OWN live rule ids; keep the ids, drop only the "Mirrors
  claudechic" framing.
- `plugins/operon-plugin/workflows/project_team/project_team.yaml`: :1,
  :3, :7, :9, :11, :12, :47 (rework the header comment block 1-12 as a
  unit; :7 is deleted with 6a). DO NOT touch :98 -- the
  `(?i)(?:\.project_team|\.operon)/...spec...` regex is live rule logic
  and contains no claudechic string.
- `plugins/operon-plugin/workflows/project_team/README.md`: :5 -- DELETE
  the "Ported from claudechic..." blockquote or replace with an
  operon-native one-liner (high visibility, user-facing).
- `src/operon_mcp_server/checks/builtins.py`: :4, :45, :359.
- `src/operon_mcp_server/checks/protocol.py`: :3.
- `src/operon_mcp_server/bootstrap.py`: :63.
- `src/operon_mcp_server/tools/request_override.py`: :28.
- `src/operon_mcp_server/tools/restore_operon_session.py`: :31.

### 6c. Stale-model fixes (SCRUB / correction -- mislead live agents now)

These are ported-claudechic content that misleads agents; in scope per the
leftover-scan expansion. Correct strings/paths/phase-counts only; do not
change test assertions or plugin logic.

- `coordinator/identity.md` "Roadmap": stale 7-phase mirror (collapses the
  three testing-* phases, omits documentation). Correct to the 10-phase
  manifest flow, citing the manifest.
- `plugins/operon-plugin/workflows/project_team/README.md`: lists a stale
  4-phase flow; references a non-existent `git_setup` agent and
  non-existent `docs/getting-started.md` / "AI_PROJECT_TEMPLATE";
  `.project_team/` path fragments. Correct to operon reality (10 phases;
  `.operon/`). Remove the dead `git_setup` agent references and the broken
  `docs/getting-started.md` / AI_PROJECT_TEMPLATE links. Do NOT add a
  reference to `project_integrator` here -- that role's directory is being
  deleted (§6d); if the file mentions `project_integrator`, drop the
  mention rather than describing a deleted role.
- `coordinator/documentation.md`, `coordinator/setup.md`: `.project_team/`
  path fragments -> `.operon/`.
- `tests/scenarios/project_team_workflow.py`: :2191 builds
  `artifact_dir = tmp_cwd / ".project_team" / RUN_NAME`; :2354-2355 drive
  `set_artifact_dir` to that `.project_team` path. Correct the path string
  to `.operon` (string/path fix only -- do not alter the test's
  behavior/assertions). Detect remaining drift with
  `grep -rn "\.project_team" plugins/operon-plugin/ tests/`; only
  `project_team.yaml:98` (the dual-match regex) may remain.

### 6d. project_integrator directory (DELETE -- resolved at checkpoint)

`plugins/operon-plugin/workflows/project_team/project_integrator/` holds
only an `identity.md` that is 100% wrong-for-operon conda / `source
activate` / `commands/` / `envs/` content. The directory is NOT referenced
by the manifest, any `.py`, `.json`, or roster row (verified; no
advance-check depends on it). ACTION: DELETE the entire
`project_integrator/` directory. Permanent deletion is the resolved
checkpoint decision; deletion breaks no manifest reference and is
trivially restorable from git if the role is ever reintroduced.

### 6e. README and CHANGELOG pointers (resolved by D9)

- `README.md:5` "claudechic-style multi-Agent orchestration" -> drop
  "claudechic-style"; state operon's value prop in its own terms.
- `README.md:24`, `README.md:290-291`: external pointer to
  `../claudechic/.project_team/claude_code_port/SPEC*.md` -> DROP and
  REPOINT to the docs site (the Contributor/Architecture Guide is now the
  authoritative design reference). Do not merely delete -- that orphans the
  reader; repoint.
- `CHANGELOG.md:6`: same dangling private-path pointer. CHANGELOG is a
  historical record; apply the NARROW fix (repoint the one pointer to the
  docs site), no full CHANGELOG rework.

### 6f. Org spelling

Use `SprustonLab` (camelCase) in all docs/README prose, including the
marketplace install command. Do NOT rewrite the git remote URL host path
(a platform fact, not prose).

### 6g. Completeness mechanism (self-check + user diff review)

The §6a-§6f list is the working inventory, not a perfect upfront list.
Completeness is confirmed by the USER reviewing the diff at the §9
checkpoint -- NOT by a CI gate or an automated test. As a self-check before
that review, the implementation runs `grep -rni claudechic` repo-wide once
(exclusions: `.git/`, `.venv/`, `__pycache__/`, `.ruff_cache/`,
`.pytest_cache/`, `.test_results/`, `site/`, `uv.lock`, and the external
`/groups/spruston/home/moharb/claudechic` repo) and iterates until it
returns zero hits, then presents the diff. The self-check MUST also cover
files the first pass may miss: `pyproject.toml`,
`.claude-plugin/marketplace.json`, `plugins/operon-plugin/.mcp.json`,
`plugins/operon-plugin/hooks/`, `LICENSE`, `licenses/`, and `tests/`.

## 7. README rework

- Restructure the README as the FRONT DOOR into the docs site: orientation
  (what operon is, who each guide serves) + deep links into `docs/`.
- The docs site OWNS the reference tables; the README links to those pages
  instead of carrying duplicate tables (anti-drift guideline, §1; checked
  at the user diff review).
- Apply §6e (pointer drops/repoints, soften :5) and §6f (SprustonLab).
- Keep the install ladder (uv -> python3 -> python) accurate; link to
  `user-guide/install.md` as the canonical owner.
- SEQUENCING: D9 (docs-site-is-authoritative) is resolved, so the README
  pointer repoint target exists; the README rework may proceed once the
  `dev/` pages it links to are stubbed.

## 8. Implementation task breakdown

- **project_integrator: SKIPPED this run.** Its stale dir is DELETED
  (§6d). CI + git work goes to the implementer.
- **implementer:** docs tree + page content (§1), `mkdocs.yml` (§2),
  `docs.yml` + guard artifacts (§3), pyproject dep add (§4), branch-model
  `.gitignore` change (ignore `operon-runs/` on main, track on develop;
  leave `.operon/` ignored on both) (§5), the SCRUB edits (§6b), stale-model
  fixes (§6c), project_integrator delete (§6d), README/CHANGELOG pointers
  (§6e), README rework (§7), delete the 4 internal audit files + repair
  any dangling references to them (§1).
- **test_engineer:** confirms the EXISTING test suite still passes after
  the token-alias removal (no new bespoke test; §6a). If no existing test
  exercises `${ARTIFACT_DIR}` substitution, notes that gap for the user at
  the checkpoint. Verification of the cleanup itself is the user diff
  review (§9), not a test the test_engineer authors.
- **terminology / user_alignment reviewers:** verify the 10-phase flow and
  the operon-session term are taught correctly and no claudechic framing
  remains in published pages; verify `dev/` is labeled "Contributor/
  Architecture Guide".

## 9. Acceptance criteria

Verification is lightweight: local build + a USER CHECKPOINT diff review +
the existing test suite. There is NO new CI and NO new test authored to
verify the cleanup. The cleanup's completeness is confirmed by the user
reviewing the diff.

1. `uv sync --dev && uv run mkdocs build` succeeds locally (no errors, no
   broken-nav warnings). Run before relying on `docs.yml`, since
   `docs.yml` + the mkdocs deps (§3/§4) land together.
2. USER CHECKPOINT (the verification of the cleanup): the user reviews the
   diff and confirms -- (a) claudechic is gone (the implementation's
   one-off `grep -rni claudechic` self-check, §6g, is presented as
   evidence, but the user's review is what verifies it); (b) no duplicate
   reference tables (the anti-drift guideline, §1); (c) docs content is
   correct, including the 10-phase flow and the operon-session term; (d)
   the 4 internal audit files are deleted with no dangling references.
3. The full EXISTING test suite still passes (this is what covers the
   `${ARTIFACT_DIR}` substitution after the token-alias removal, §6a).
4. Branch model verified by a ONE-OFF manual test commit: `git show
   main:.gitignore` ignores `operon-runs/`; a commit touching
   `operon-runs/docs/<file>` is TRACKED on `develop` and REJECTED on
   `main` (pre-commit hook locally + PR guard in CI). `.operon/` stays
   ignored on both branches. This is a manual check, not a maintained test.

Enforcement artifacts: the ONLY enforcement artifacts in this work are the
`docs.yml` Pages deploy and the two branch guards
(`no-operon-runs-additions-on-main.yml` + the `block-operon-runs-on-main`
pre-commit hook). These exist by PRIOR USER REQUEST; the claudechic
cleanup and the anti-drift guideline are verified by the user diff review
above, not by CI or tests.

## 10. Manual (user) step

Enable GitHub Pages for the repo (Settings -> Pages -> source: GitHub
Actions) before `docs.yml` can publish. Outside CI's control.

## 11. Resolved decisions (no open items)

All Specification-checkpoint decisions are settled and baked into the
sections above:

- **Audit docs = DELETE** (supersedes the earlier move-out): remove all 4
  audit files; `docs/` is then clean and serves as the mkdocs `docs_dir`
  directly; repair any dangling references (§1).
- **project_integrator = DELETE the directory** (§6d).
- **Verification = user diff review, not CI/tests:** the claudechic cleanup
  is confirmed by the user reviewing the diff at the §9 checkpoint; the
  implementation's `grep -rni claudechic` is a one-off self-check (§6g),
  not an automated gate.
- **D8 boundary:** the scrub edits docstrings/comments/strings + the
  behavior-preserving `${ARTIFACT_DIR}` rename, and MUST NOT change control
  flow, rule semantics, or the design (§6 scope; §6a). The EXISTING test
  suite passing is what confirms `${ARTIFACT_DIR}` still resolves (§9.3);
  no new bespoke test is authored.
- **CHANGELOG:** scrub claudechic refs + narrow fix of the dangling
  private-path pointer at line 6; no broader rewrite (§6e).
- **Scrub extends to `tests/`:** fix the `.project_team/` drift at
  `tests/scenarios/project_team_workflow.py:2191` (paths only, not test
  behavior) (§6c).
- **Stale phase mirrors fixed THIS run:** coordinator `identity.md`
  (7-phase) and `project_team/README.md` (4-phase) corrected to cite the
  10-phase manifest; dead `git_setup` refs and broken
  `docs/getting-started.md` / AI_PROJECT_TEMPLATE links removed (§6c).
- **Standing terminology/structure decisions:** D1 operon-session; D2
  SprustonLab; D9 docs site is the authoritative design reference; docs
  own the reference tables; `.claude/` gitignored.
