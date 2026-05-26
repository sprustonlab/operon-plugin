# User Alignment -- Specification phase (`docs` run)

Source of truth: `userprompt.md` + `STATUS.md` (re-read this phase).
Reviewed against: `leadership_findings.md` (the team's carried-forward
direction and open DECISIONS D1-D11). Read-only; this file is my only
write.

## Original Request Summary (verbatim anchors)

- "Write *new* documentation for operon-plugin." Audience: "Both end
  users and contributors." Scope: "The operon-plugin (this repo), not
  claudechic."
- Structure: "a **User Guide** + a **Contributor/Architecture Guide**."
- Toolchain: "**MkDocs + Material theme**, modeled on claudechic's docs
  setup ... -- but using operon-plugin's *own* config (no claudechic
  `site_url`/analytics)."
- "**README needs rework** -- restructure as the front door into the docs."
- "**Minimize claudechic presence** ... no claudechic branding/content
  carried over, so agents reading this repo are not confused."
- CI: "add `.github/workflows/docs.yml` (net-new) ... deploy to GitHub
  Pages on push to `main`, modeled on claudechic's `docs.yml`."
- Scope expansion (approved 2026-05-25): "actively audit the repo for
  content ported from claudechic that does not match operon-plugin and
  would mislead agents. ... Produce a findings list and remediate."
- Roster: "project_integrator: SKIPPED for this run."
- OUT OF SCOPE: "Rewriting plugin code or the SPEC." / "Copying
  claudechic content/branding verbatim."

## Alignment Status: ALIGNED, with 1 WARNING the Coordinator must resolve

The leadership findings + open DECISIONS faithfully cover the user's
requested deliverables (User Guide, Contributor/Architecture Guide,
README rework, net-new docs.yml -> Pages, claudechic-leftover scan +
remediation, project_integrator skipped). No scope-shrink: every
user-requested item is represented. The 10-phase manifest-as-truth call
(terminology #2) correctly protects the user from docs that teach a
stale phase model.

## WARNING -- D8 vs the "out of scope" line: needs explicit reconciliation

DECISION **D8 (RESOLVED by user 2026-05-25)** lifts the comment-only
limit: "claudechic must be removed entirely (tokens + identifiers +
comments + strings)," including rename `${CLAUDECHIC_ARTIFACT_DIR}` ->
`${ARTIFACT_DIR}` (drop the alias) WITH test coverage.

This is a genuine, user-authorized expansion that SUPERSEDES my
LEADERSHIP first-pass caution (where I flagged scrubbing provenance
comments as scope creep). I withdraw that caution -- the user owns the
call and made it explicitly.

BUT it now sits against the standing out-of-scope line "Rewriting plugin
code or the SPEC." The `claudechic` strings D8 reaches into live in
`src/operon_mcp_server/*.py` (docstring/comment prose only -- verified:
`rules.py:5,12,18,86,400`, `workflow.py:159,171,182,433`,
`checks/builtins.py:4,45,359`, `checks/protocol.py:3`,
`tools/request_override.py:28`, `tools/restore_operon_session.py:31`,
`bootstrap.py:63`). The token rename touches real code at
`workflow.py:424/433/440/461` (the `.replace("${CLAUDECHIC_ARTIFACT_DIR}",
...)` call at :440 is live logic; `${ARTIFACT_DIR}` is already accepted
alongside it, so the rename is behavior-preserving once the alias is
dropped and tests updated).

I read these as RECONCILABLE -- D8 authorizes editing prose-in-code and a
behavior-preserving token rename, while "out of scope" forbids rewriting
plugin *behavior/logic* or the SPEC. But because two user statements are
in tension, the spec must NOT silently pick one.
**Recommend:** the spec state explicitly, in the user's own framing, that
the claudechic scrub MAY touch docstrings/comments/strings in `src/*.py`
and perform the behavior-preserving `${ARTIFACT_DIR}` rename, and MUST
NOT alter control flow, rule semantics, or the SPEC. Surface this framing
to the user at the Specification checkpoint for a yes so D8 and the
out-of-scope line don't collide downstream.

## Wording / domain-term checks

- "remove claudechic entirely" (D8) must NOT be read to delete or scrub
  the internal audit docs (`docs/SPEC_GROUNDING_AUDIT.md` 35 refs,
  `AGENT_TEAMS_PIVOT_PLAN.md` 8, `APPENDIX.MD` 10) -- their PURPOSE is
  cross-referencing claudechic, and STATUS says "leave as-is." D6 must be
  confirmed EXEMPT so "entirely" is bounded to operon-plugin's own
  surfaces. Flag wording: "entirely" (D8) vs "leave as-is" (STATUS) --
  the spec should reconcile by scoping "entirely" to non-exempt files.
- "User Guide" + "Contributor/Architecture Guide" are the user's exact
  structural terms. Composability's folder shape `docs/user-guide/*` +
  `docs/dev/*` preserves the two-guide split -- ALIGNED. Watch that
  `dev/` is presented to readers AS the "Contributor/Architecture Guide"
  (the user's words), not relabeled merely "dev" in nav, so the user's
  wording survives into the rendered site.
- "modeled on claudechic" / "modeled on claudechic's docs.yml" is the
  user's own phrasing -- adapting (not copying verbatim) docs.yml and
  mkdocs.yml honors both this and the "no verbatim" out-of-scope line.
  ALIGNED.

## Open DECISIONS -- alignment view (defer resolution to Coordinator/user)

- D9 (where the authoritative SPEC lives post-rework) is the crux of the
  user's stated "agents confused" problem (README:24,290-291 +
  CHANGELOG:6 point to a claudechic-PRIVATE path). Resolving D9 is
  REQUIRED to satisfy intent -- not optional polish.
- D7 (fix CHANGELOG:6 dangling private pointer): the user asked to rework
  the README, not the CHANGELOG. Fixing the one broken pointer serves the
  "not confused" intent; a full CHANGELOG rework would be scope creep.
  Recommend the narrow fix only, and confirm with user.
- D11 (project_integrator stale identity.md: rewrite vs delete): role is
  SKIPPED this run and NOT referenced in `project_team.yaml`, so deleting
  its dir+row breaks no manifest reference. Either rewrite or delete
  satisfies "remediate the conda leftover"; this is a HOW call -- defer to
  Coordinator/Skeptic, no user-intent constraint either way.
- D5 (`.claude/` gitignore), D2 (org casing), D3 (existing internal
  `docs/*.md` vs mkdocs docs_dir): not user-stated; surface at the
  Specification checkpoint rather than the team deciding silently.

## Update -- Coordinator decisions landed (STATUS rev + peer specs)

Re-checked against the Coordinator's resolved decisions and the peer spec
contributions (composability.md, terminology.md, skeptic_review.md). Most
prior flags are now RESOLVED and resolved ON-INTENT:

- **D9 RESOLVED = "docs-site-is-spec."** The authoritative SPEC lives in
  the docs site post-rework; the README front-doors to it instead of the
  claudechic-private path. Directly fixes the "agents confused" problem.
  ALIGNED. (Skeptic C3 still rightly flags this is a hard blocker for the
  README rework -- so resolving it before implementer touches the README
  is correct sequencing.)
- **D6/"entirely" wording conflict RESOLVED.** Two independent specs agree
  on the bounded reading: composability risk #5 + D3 relocate the 4
  internal audit docs OUT of the mkdocs source (so they don't publish);
  skeptic gate #5 exempts them from the claudechic scrub. So "remove
  entirely" = operon-plugin's own surfaces; audit docs keep their
  claudechic cross-references. This is exactly the bounded reading I
  recommended. ALIGNED.
- **Two-guide structure preserved with user's wording.** terminology.md
  consistently calls the dev/ folder the "Contributor/Architecture Guide"
  in prose + source-mapping table (lines 5-6, 158, 213). The user's exact
  term survives into how the guide is referenced, not just the folder
  slug. ALIGNED -- my nav-label flag is satisfied.
- **D8 FULL-REMOVAL is on-intent and lower-risk than feared.** Skeptic V1
  grounded it: `${CLAUDECHIC_ARTIFACT_DIR}` is emitted by ZERO live
  templates; only a dead substitution branch + comments reference it.
  Dropping it is near-zero behavior risk; the load-bearing test is that
  `${ARTIFACT_DIR}` STILL substitutes. So D8 does NOT require rewriting
  plugin behavior -- it confirms the reconciliation I asked for (prose +
  behavior-preserving rename, no logic/SPEC change). I consider the
  D8-vs-out-of-scope WARNING RESOLVED, contingent on the user-checkpoint
  yes the Coordinator already scheduled ("User checkpoint at end").

## Residual alignment nuances (2) -- for Coordinator

1. **D11 delete vs "skipped this run" -- mild over-reach to confirm.**
   Skeptic C4 recommends DELETING `project_integrator/` (dir; verified it
   has NO manifest/yaml/py/json references -- exists only as a dir with
   identity.md, so there is no roster row to remove and no advance-check
   depends on it -> deletion breaks nothing). I agree deletion is the
   cleanest remediation of the conda leftover. BUT the userprompt says the
   role is "SKIPPED *for this run*," not retired. Deleting the dir is a
   more permanent reading than "skip once." This is a user-intent call,
   not purely a simplicity one. **Recommend:** confirm with the user at
   the checkpoint that permanent deletion (vs leaving the dir dormant /
   rewriting later) is acceptable. If unconfirmed, the conservative
   on-intent move is delete (it ships no misleading content and is
   trivially restorable from git), but say so explicitly.

2. **Remediation extending into the TEST SUITE (skeptic V2) is IN scope,
   not creep.** The `.project_team/` drift at
   `tests/scenarios/project_team_workflow.py:2191` (and the 7/4-phase
   stale mirrors in coordinator identity.md + workflow README per C2) are
   "ported claudechic content that would mislead agents" -- squarely the
   user's approved scope-expansion. Fixing them is ALIGNED. Guard: keep it
   to correcting misleading strings/paths/phase-counts, NOT altering test
   assertions' behavior or plugin logic (stays clear of the out-of-scope
   "rewriting plugin code" line).

## Recommendation

Proceed -- spec is on-intent and the major flags are resolved on-intent.
Before finalizing, the Coordinator should carry to the user checkpoint:
(1) the D8 prose+rename framing (confirm the scrub touches docstrings/
strings + does the behavior-preserving rename, no logic/SPEC change),
(2) D11 permanent-delete vs skip-this-run, and confirm D6 audit-doc
exemption in the user's own words so "entirely" stays bounded.
