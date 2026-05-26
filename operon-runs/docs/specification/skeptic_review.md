# Skeptic Review -- Specification phase (`docs` run)

Reviewed against actual code, both git branches, the claudechic
reference, and `leadership_findings.md`. No formal `SPECIFICATION.md`
exists yet -- this reviews the proto-spec captured in
`leadership_findings.md` plus the open DECISIONS D1-D11. Findings are
ordered by correctness impact (completeness first, then simplicity).

## Verified since first pass (changes the risk picture)

### V1 -- D8 alias-drop is SAFE for production, but the framing is backwards
The user (D8) wants `${CLAUDECHIC_ARTIFACT_DIR}` renamed to
`${ARTIFACT_DIR}` and the back-compat alias DROPPED, with test coverage.
I grounded the actual surface:

- The token `${CLAUDECHIC_ARTIFACT_DIR}` is EMITTED by **zero** workflow
  template files. Every live template already uses `${ARTIFACT_DIR}`
  (project_team.yaml advance-check paths :35,:37,:44,:59,:66; plus
  composability/skeptic/terminology/test_engineer/user_alignment
  `testing-specification.md`).
- The token survives only as: the dead-code substitution branch at
  `src/operon_mcp_server/workflow.py:440` (`.replace("${CLAUDECHIC_ARTIFACT_DIR}", ...)`),
  its doc-comments at workflow.py:424/433/461, and the rename note at
  `project_team.yaml:7`.

Implication: dropping the `${CLAUDECHIC_ARTIFACT_DIR}` branch removes a
substitution for a token nothing produces -- near-zero behavior risk.
The REAL completeness requirement is the inverse: the substituter MUST
keep handling `${ARTIFACT_DIR}`, because 7+ live files depend on it. A
test that only asserts "the claudechic token is gone" is the wrong test;
the load-bearing assertion is "`${ARTIFACT_DIR}` still substitutes to
the run's artifact dir." Spec the test around the surviving token, not
the deleted one.

### V2 -- `.project_team/` drift is also IN THE TEST SUITE (new, not in findings)
`tests/scenarios/project_team_workflow.py:2191` builds
`artifact_dir = tmp_cwd / ".project_team" / RUN_NAME` and then (lines
2354-2355) instructs the agent to call
`set_artifact_dir(path='<that .project_team path>')`. So a LIVE test
drives the engine to write under `.project_team/` while the engine's own
default, STATUS, and src/ all use `.operon/`. This is the same R1 drift
class as the identity files, but in code that doubles as a usage
reference. The leftover catalog (findings 56-70) lists docs/identity/
yaml hits only -- it MUST be extended to the test suite, or the scan is
incomplete. Detect: `grep -rn "\.project_team" tests/`.

## Challenges to the proposed spec

### C1 -- "LINK don't inline" is the right anti-drift law; make it ENFORCEABLE
The composability LAW (findings:13) -- each page links to canonical
source rather than inlining -- is the single best drift defense. But as
stated it is a convention, not a check. A convention with no gate erodes
the moment one author inlines a tools table "for convenience." If we
want this to hold, the spec should name ONE concrete enforcement: either
(a) the reference tables (MCP tools, env vars, rules) have exactly one
home (D4: docs own, README links) and a CI grep rejects a second copy,
or (b) we accept it is advisory and say so. Do not write it as a LAW and
then leave it unenforced -- that is simplicity masking incompleteness.

### C2 -- Phase-count drift (findings:46-54) needs a SINGLE source the docs cite
Three phase-count variants exist (yaml=10, coordinator identity=7,
README=4). The spec says "docs MUST teach the 10-phase manifest." Good,
but the docs should not transcribe the phase list (a 4th copy that can
drift) -- they should cite `project_team.yaml` as the source of truth and
ideally render from it. At minimum the spec must state the docs derive
the count from the yaml, and the two stale mirrors (coordinator
identity, README) are FIXED in this run (they mislead live agents now),
not left as future work. Leaving stale 7/4 variants in the shipped
plugin while publishing a 10-phase doc is internally contradictory.

### C3 -- D9 (where the SPEC lives) is a hard blocker for the README rework
README:24,290-291 + CHANGELOG:6 point at claudechic-private
`../claudechic/.project_team/claude_code_port/SPEC*.md`. The README is
being reworked as the docs front door (vision item 3). You cannot freeze
the README without resolving D9, and D9 has real options with different
cost: (a) vendor the SPEC into operon-plugin (large, may carry more
claudechic content -- contradicts "minimize claudechic"), (b) link a
public URL (does one exist? unverified), (c) drop the pointer and let
the docs/CHANGELOG be the authority. This is essential complexity --
the project genuinely has an external design doc -- so the answer is the
simplest HONEST pointer, not "leave it dangling." Recommend the user
pick before README work starts, else implementer blocks or guesses.

### C4 -- D11 (project_integrator stale dir) -- delete is simpler AND more correct
project_integrator is SKIPPED this run (userprompt roster decision) and
its identity.md is 100% wrong-for-operon conda content. Rewriting it
means authoring a correct uv/pyproject integration identity for a role
nobody spawns -- effort spent on a dead path, and a new file that can
itself drift. Deleting the dir + its roster row removes the misleading
content with the least surface. Rewrite only if the user intends to
spawn project_integrator in a FUTURE run; if so, that is its own task,
not this docs run. Recommend D11 = delete, and if the yaml references
the role, remove that row too (verify no advance-check depends on it).

## Simplicity check (per phase instructions 4-6)

- The proto-spec is NOT over-engineered: docs/ + docs/dev/ split mirrors
  claudechic, no new phase engine, no abstraction layers. The "Audience x
  Surface" decomposition is a folder shape, not machinery. No more than
  2 moving parts in the toolchain (mkdocs.yml + docs.yml). PASS.
- One watch item: the git-safety model has THREE artifacts (per-branch
  .gitignore, pre-commit hook, CI guard). That is essential, not
  accidental -- each guards a distinct surface (local commit vs PR vs
  branch state), and all three are direct ports of claudechic's proven
  set. Do not collapse them to "fewer parts"; that would reintroduce the
  hole each one closes. Flagging so the count is not "simplified" away.

## Carry-forward gates (cheap, grep/git-verifiable)

1. `grep -rn "\.project_team" plugins/operon-plugin/ tests/` -> only
   project_team.yaml:98 (the dual-match regex, intentional) may remain.
2. `git show develop:.gitignore` must NOT contain a bare `.operon/`
   ignore after the un-ignore lands (R2 -- still unapplied; STATUS now
   corrected to OPEN).
3. `uv sync --dev && uv run mkdocs build` must succeed locally BEFORE
   docs.yml is trusted (R3 -- mkdocs deps + CI file land together).
4. Substitution test asserts `${ARTIFACT_DIR}` resolves (V1), not merely
   that the claudechic token is absent.
5. `grep -rni claudechic` repo-wide returns only the intentionally-exempt
   internal audit docs (D6) after the scrub (D8 scope: tokens +
   identifiers + comments + strings).
