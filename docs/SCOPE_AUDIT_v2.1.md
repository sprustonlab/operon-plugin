# Scope Audit -- AGENT_TEAMS_PIVOT_PLAN.md v2.1

Audited against §1 framing: the doc must describe "what we ARE
doing." Speculation about future runtime behavior, fallback paths,
self-congratulation, defensive prose about rejected alternatives,
and historical retelling beyond a tight motivation anchor are
out-of-scope.

## Verdict

**SIGNIFICANT BLOAT.** 22 distinct removal candidates identified;
the changelog stack alone consumes ~135 lines of editorial
process commentary, and §9.1 + §9.8 explicitly violate the §1
framing by speculating about runtime contingencies.

## Recent-removal verification (PASSES)

- `pre-pivot-stable` references: NOT present in the body. Only
  mentioned at line 47 in the v2.1 changelog describing the
  removal. PASS.
- Exit-ramp framing: NOT present in the body. Mentions at lines
  14, 44, 121 are all in changelogs describing the removal.
  PASS.
- Fallback prose: line 1077 ("not a fallback in this one") is an
  explicit anti-fallback assertion in the migration plan, not a
  fallback path. Other mentions are changelog entries.
  PASS. The committed-path posture is intact in the body.

## Removal proposals

1. **§1 v1.3 changelog (lines 130-162).** Out-of-scope: historical
   editorial about a prior revision's edit list, including
   self-congratulation ("Composability fixes integrated,"
   "Skeptic conditions integrated"). DELETE -- move to
   CHANGELOG.md if retention matters.

2. **§1 v1.3-final changelog (lines 112-128).** Same class as
   above: editorializing about edits between drafts. DELETE or
   relocate to CHANGELOG.md.

3. **§1 v2.0 rewrite scope (lines 50-110).** "What was rewritten /
   preserved / added / removed" is process artifact, not
   architecture. The architecture itself is in §§3-8.
   DELETE; relocate to CHANGELOG.md.

4. **§1 v2.1 changelog (lines 27-49).** Decisions recorded here
   (delivery confirmation NEITHER, no spoof fallback, exit ramp
   REMOVED) belong in-line at the sections that record them.
   TIGHTEN to a 3-line summary or relocate to CHANGELOG.md.

5. **Lines 16-20 ("Versioning is iterative... no 'final' version").**
   Editorial commentary about doc lifecycle. DELETE.

6. **§2.1 "channels-respawn bug class" (lines 188-206).**
   Long retelling of Phase 14 fix 1-8 history. The motivation
   only needs to assert "the channel substrate is documented as
   not designed for cross-session broadcast." TIGHTEN TO ONE
   PARAGRAPH (3-4 lines).

7. **§2.3 "Community state-of-the-art" (lines 232-243).**
   Defensive prose comparing to retrodigio/claude-channel-slack
   ("meaningfully weaker than..."). The "no prior art exists...
   credible contribution" line is self-congratulation.
   TIGHTEN TO ONE LINE or DELETE entirely; this isn't load-bearing
   for the architecture being built.

8. **§3.5 "keystone empirical finding" framing (lines 326-330).**
   "This is the keystone... Everything in Section 4 follows from
   it." is editorial self-anchoring. The test result itself (the
   blockquote above) is the spec content. DELETE the editorial
   paragraph; keep the empirical record.

9. **§3.6 "Bonus observation" + §3.7 "Note for users"
   (lines 332-350).** §3.6 points to a future opportunity
   (Appendix A.1). §3.7 is implementation-tone guidance about
   what users will see. Neither describes the architecture being
   built today. RELOCATE §3.6 to Appendix A.1 as a single line
   ("UserPromptSubmit fires lead-side on synthetic teammate
   messages -- enables future audit hook"); DELETE §3.7 (covered
   by §4.7).

10. **§4.4 closing sentence (line 528-529): "The deletion list is
    large precisely because each item was defending a seam that
    operon no longer owns."** Editorial commentary. DELETE.

11. **§4.5 closing "Phase 14 fixes 1-8 fighting" reference
    (within lines 553-557 / 559-562).** Defensive prose against
    a rejected alternative (tmux). The single decision is
    recorded; the explanatory loop is residue. TIGHTEN to one
    sentence pointing at §10.5.

12. **§4.7 "Why this design (compositional rationale)" paragraph
    (lines 667-679).** Discussion of the rejected spoof
    alternative. Per rubric, allowed ONCE per decision. This is
    the canonical instance, so KEEP -- but TIGHTEN to 3-4 lines
    (currently 13). Remove the "elsewhere in the system means..."
    elaboration.

13. **§6.1 "interrupt_agent / close_agent" (a)/(b) two-options
    menu (lines 989-1018).** This is exactly the "two-options
    menu framings" residue called out in the rubric. The
    decision is DROP both; rationale can be stated in one
    paragraph without the (a)/(b) anatomy. TIGHTEN: keep the
    "DELETE both, decline to ship either" verdict + one-sentence
    rationale; drop the (a)/(b) breakdown.

14. **§9.1 "Experimental flag instability" (lines 1136-1142).**
    DIRECT VIOLATION of §1 framing -- speculation that Anthropic
    "may change semantics or remove the feature." TIGHTEN TO ONE
    LINE risk-acknowledgment or DELETE.

15. **§9.5 "Loss of operon-mailbox-as-source-of-truth"
    (lines 1180-1191).** Historical retelling of pre-pivot
    mailbox audit semantics. The architecture's audit choice
    (events.log, append-only JSONL) is stated in §4.3 item 7.
    TIGHTEN to one line ("audit moves from mailbox dir tree to
    events.log; pre-pivot mailbox data not migrated") or DELETE.

16. **§9.8 "Inbox provenance validation" (lines 1228-1240).**
    DIRECT VIOLATION of §1 framing -- "A future Claude Code
    release could add provenance validation..." with Impact /
    Likelihood scoring. This is exactly the speculation §1
    excludes. DELETE entirely or TIGHTEN TO ONE LINE
    ("provenance-validation regression would invalidate the
    inbox-write primitive; monitor release notes").

17. **§9.9 "Delivery-semantics regression" rationale tail
    (lines 1252-1259).** The decision (no tracking, no ACK, no
    retry) is recorded; the "would require either... both of
    which expand the architecture's surface area..." is
    defensive prose against rejected alternatives. KEEP the
    decision (lines 1244-1252); TIGHTEN the rationale to one
    sentence.

18. **§10.4 "Composability self-check" (lines 1340-1392).**
    Skeptic-style hand-wringing ("the kind of thing that erodes
    over time," "weakest kind of architectural defense,"
    "papering over"). Process artifact: a reviewer-prompt
    section. The substantive content (dual-purpose hook is
    highest-risk seam; needs independent Composability review
    when staffing permits) is one sentence. TIGHTEN to that one
    sentence in §4 (or §10) or RELOCATE to a separate review-
    notes file.

19. **§10.1-10.3 reviewer prompts (lines 1267-1338).** "Open
    questions for [Reviewer]" is review-process artifact, not
    architecture. Once reviews land, these are dead weight.
    RELOCATE to a separate review-prompts file, or DELETE the
    boilerplate framing and inline the substantive assumptions
    (e.g., §10.2's assumption list) directly into §9.

20. **§11 implementation-detail handwringing rows (last 3 rows,
    lines 1458-1461).** Rows for "PreToolUse hook execution-time
    limits," "Missing/corrupt meta.json handling," "schema_version
    drift detection" each carry a "Spec: fail loudly... Implement
    in Step 1" sub-clause. That's implementation-detail prose
    that belongs in code comments. TIGHTEN each row to "NOT
    TESTED. Pre-implementation gate." -- one-line per row,
    matching the rest of the table.

21. **§12 "What this doc is NOT" (lines 1473-1488).** Editorial
    framing about the doc's nature. The §1 framing already says
    this is a design spec. The "not a menu of options" line
    duplicates the §1 commitment. DELETE; the §1 framing
    suffices.

22. **Appendix A future opportunities (lines 1491-1519).** Per
    §1 framing, the doc describes what we ARE doing. Appendix A
    explicitly describes what we are NOT doing on the critical
    path. Borderline -- keeping them as a deferred-opportunities
    pointer is useful, but the per-section prose can TIGHTEN to
    one-line entries (subject + estimated effort). Currently
    each is 4-7 lines of speculation; cut to ~2.

## Borderline items (route to Boaz)

- **§4.6 "Singleton MCP architecture" rationale paragraph
  (lines 569-577).** "This is not an optimization. It is the
  compositional consequence..." reads as defensive justification,
  but it IS the load-bearing compositional claim of the pivot.
  Keep or tighten -- Boaz call.

- **Appendix B Glossary (lines 1522-1552).** Rubric says glossaries
  are in-scope. Size is 23 entries; some (e.g. "operon_run",
  "events.log") could move to inline definitions on first use.
  Probably keep as-is; flagging only for completeness.

- **§10.5 in-process commitment (lines 1394-1422).** Four-bullet
  rationale for an already-recorded decision. Could TIGHTEN to
  two bullets (compositional anchor + UX trade-off accepted), or
  KEEP given it's the single most consequential decision in the
  doc. Borderline.

- **§10.6 reply tracking (line 1424-1427).** Already one line.
  KEEP.

- **§5.1 "Quality notes" (lines 883-889).** "Raw concatenation
  works. Don't summarize for v1." is a direction. The
  Haiku-summarization mention points at Appendix A.3. KEEP; it's
  tight.

## Summary

The architecture itself (§§3-8) is largely clean and
well-scoped. The bloat is concentrated in three regions:

1. **§1 changelog stack** -- four stacked changelogs totaling
   ~135 lines of process narrative. Relocate to CHANGELOG.md.
2. **§9 risks** -- two rows (§9.1, §9.8) violate §1 framing
   directly; one row (§9.5) is historical retelling.
3. **§10 reviewer prompts** -- §10.1-10.4 are review-process
   artifacts that don't describe architecture. §10.4 in
   particular is self-skepticism that belongs elsewhere.

Trimming items 1-22 would reduce the doc from 1556 lines to an
estimated ~900-1000 lines without touching architectural
substance.

Recent-removal verification confirms exit-ramp, fallback, and
`pre-pivot-stable` content is OUT of the body; only present in
changelog entries describing the removals. Those changelog
entries themselves are proposed for relocation under item 4
above.
