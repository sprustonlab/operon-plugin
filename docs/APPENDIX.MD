# Agent Teams Pivot Plan -- Appendix

A quiet record of editorial history, rejected-alternative
reasoning, and deferred future opportunities preserved during
the v2.1 -> v2.2 trim pass. None of the content here is
load-bearing for the architecture.

Section headings below use "relocated from spec Section X.Y"
labels as historical anchors only -- they record where each
piece of material lived in an earlier revision. They are not
navigation aids; readers seeking the current architecture
should consult the spec directly.

---

## Changelogs

### v2.10 changelog (since v2.9)

Three empirically-grounded edits from Boaz's Land 1 v2 demo
(commit `e6228e5` on `agent-teams-pivot`, run_name
`land1-v2-test`, 2026-05-21).

- **Section 3.1 / 3.2 inbox-write primitive.** Replaced the
  v2.9 assumed schema with the empirical schema captured from
  `/groups/spruston/home/moharb/.claude/teams/land1-v2-test/inboxes/team-lead.json`.
  Changes:
  - Filename keyed by member `name` (not `agentId`); cited
    explicitly.
  - Field contract spelled out: required (`from`, `text`,
    `timestamp`, `color`, `read`); optional (`summary`).
  - Empirical sample (two composability-authored entries with
    one plain-text body and one JSON-encoded
    `idle_notification` control message) quoted verbatim with
    the on-disk source path.
  - Lazy-existence semantic added: inbox files do NOT exist
    immediately after TeamCreate; they appear when the first
    SendMessage to that recipient is delivered. Operon's
    writer MUST `mkdir -p` the `inboxes/` directory and
    create-or-append the recipient file.

- **Section 5.2 Lifecycle protocol REINSTATED.** Was dropped
  in v2.9 as un-grounded. The Land 1 v1 demo (yesterday)
  surfaced the failure mode (teammates auto-approve
  `{type: "shutdown_request"}` and vanish from the TUI before
  the user can interact); the v2 fix in commit `e6228e5`
  inserts a `## Lifecycle protocol` footer into every
  compiled `~/.claude/agents/<role>.md` instructing the
  teammate to reply `approve: false`. v2 demo today verified
  the teammate stays visible across the demo.

  Section 5.2 now records:
  - The footer's source location:
    `src/operon_mcp_server/subagent_install.py:_LIFECYCLE_PROTOCOL_FOOTER`
    (lines 85-107) appended in `_compile_role_definition`
    (line 211).
  - The exception: the operon stub at
    `~/.claude/agents/operon.md` does NOT get the footer.
    The stub is never meant to be invoked; if it is, it
    fails loudly via `ERR-OPERON-STUB-WAS-SPAWNED`
    (`subagent_install.py:79-83`).
  - The verbatim footer text.

- **Section 4.6 + 4.7 `members[]` writer claim corrected.**
  The v2 demo confirmed the Anthropic runtime DOES update
  `members[]` when the `Agent` tool is invoked with a `name=`
  parameter (the third row in `land1-v2-test/config.json`
  appeared after composability was spawned, alongside the
  pre-existing team-lead and operon entries). Operon's
  append for itself remains additive and idempotent. v2.10
  updates:
  - Section 4.6's team-roster row in the disk-state table:
    "(runtime-owned)" -> "(runtime + operon both write;
    runtime is the primary writer for teammate entries)";
    Cell text explains operon's append-its-own-row scope.
  - Section 4.6's roster paragraph rewritten to name both
    writers explicitly and state operon's invariant: "append
    idempotently; never delete; never overwrite entries
    operon did not write."

The caching principle in Section 4.6 ("operon caches state it
owns; reads on demand from state the runtime owns") stands
unchanged. The roster file is multi-writer, so the read-on-
demand discipline still applies.

Status: bumped to DRAFT v2.10.

### v2.9 changelog (since v2.8)

Three substantive changes: section drops, "+ tests" strip, and
the Migration plan restructured into 5 user-visible lands.

**Sections dropped from the spec body:**

- **Section 5.2 (longevity directive).** Not empirically
  supported as a discrete need; role identity.md content
  carries the longevity expectation when authors choose to
  include it. The v2.8 prompt-level instruction ("Respond to
  each incoming SendMessage. Stay available across multiple
  messages within this session. Do not stop or self-terminate
  unless explicitly instructed.") becomes an authoring
  convention for `<role>/identity.md`, not a spec
  requirement. The subagent-definition transformer no longer
  has a separate "append longevity directive" step (Section
  4.3 component 6 and Section 7 transformer row updated to
  drop the step).

- **Section 5.3 (schema corrections from spike).** Empirical-
  fact territory; the four facts (sidechain transcripts live
  at `subagents/agent-<hash>.jsonl`; each invocation gets its
  own file; sibling `meta.json` carries `agentType`; inbox
  entries are stored in append-order) are consumed in-line
  where they matter -- Section 5.1 cites them through the WA1
  walk pseudocode. The free-standing list of facts moved here:
  - Sidechain transcripts live at
    `~/.claude/projects/<cwd-mangled>/<parent-session>/subagents/agent-<hash>.jsonl`.
  - Each subagent invocation gets its OWN
    `agent-<hash>.jsonl`. The runtime does NOT re-read prior
    files when building a new invocation's context.
  - Sibling `agent-<hash>.meta.json` carries
    `{"agentType": "<teammate-name>"}`.
  - Inbox entries are stored in append-order, NOT timestamp
    order.

- **Section 9 (Risks).** Removed entirely. The schema_version
  field discipline from v2.8's Section 9.2 is not propagated
  into the spec; implementers can add it when shipping
  inbox writes if they choose. The delivery-non-guarantees
  list (no at-least-once, no at-most-once, no ordering, no
  idempotency) is implicit in the inbox-write contract;
  operon's behavior (write inbox file and return) covers it.
  Tmux-refusal moved into Section 4.5 as inline operational
  behavior.

- **Section 10 (Standing decisions).** Removed entirely.
  Section 10.1 in-process commitment (four-bullet rationale)
  was already on the operational side (in-process is what we
  do; Section 4.5 documents the mode); Section 10.2 inbound
  message routing duplicated Section 4.7.

- **Section 11 (Empirical validation status).** Removed
  entirely. The table mixed VALIDATED runtime-anchor records
  (TeamsSpike Round 1, Test A, WA1) with NOT TESTED future-
  empirical-edges. With Step 0.5 already executed (TeamsSpike
  Round 3 happened) and the migration now scoped as 5 lands
  rather than empirically-gated steps, the table is no longer
  driving anything in the spec. The compositional anchors
  (inbox-write primitive, runtime's lack of provenance check,
  WA1 prompt-mutation mechanism, sidechain transcript schema)
  are still architectural anchors; they live as the contract
  in Section 4.2 and the mechanisms in Section 3 / Section
  5.1. The NOT TESTED operational edges (filesystem watcher
  reliability, protocol dispatch round-trip, free-form relay
  rendering, etc.) become implementation concerns at the
  appropriate land in Section 8.

**Cross-references cleaned up:**

- Section 1 roadmap drops "(Section 9), standing decisions
  (Section 10), empirical validation status (Section 11)";
  new roadmap lists only what remains.
- Section 3.3 rule 1: "the Section 9.8 contract" -> "operon's
  delivery contract" (the contract is stated in the same
  paragraph).
- Section 4.2: trailing "Empirical status in Section 11."
  dropped.
- Section 4.5: tmux-refusal mechanism inlined (was just a
  cross-reference to Section 9.5 and Section 10.1).
- Section 4.7: dropped "Section 11 lists registration +
  reply-routing as a pre-implementation gate" sentence;
  replaced with a forward reference to Land 1 (Section 8)
  for end-to-end validation.
- Glossary `Team config` entry: cross-ref to Section 9.5 ->
  Section 4.5; added cross-ref to Section 5 for the
  advance_phase read-on-demand discipline.
- Glossary `Teammate` entry: dropped "the longevity directive
  (Section 5.2)" tail.
- Glossary `Inbox-write primitive` entry: dropped "Empirical
  status in Section 11"; replaced with the Section 3.3 rule 1
  reference for the optimistic-retry discipline.

**"+ tests" / "+ unit tests" / "+ transformation tests" /
"+ behavior test" stripped from Section 7.** Five table rows
affected: `inbox_reader.py`, the subagent-definition
transformer, `spawn_prompt.py`, the operon-as-team-member
registration row, and (implicit) any related entries. The
modules and behaviors themselves stay; test artifacts are out
of scope.

**Section 8 restructured into 5 lands.** The Steps 0-7
sequence (Leadership review, TeamsSpike Round 3 gate, build-
branch deliverables, test suite, full-loop test, freeze + PR,
merge, SPEC.md revision, docs) is replaced with five
user-visible lands. Each land:

- Names the new code it delivers.
- Names the demo Boaz can run in his Claude Code session.
- Names the legacy code deletions made obsolete by the new
  code, all in the same commit. Code deletion is interleaved,
  not a separate step.

The new lands:

1. Spawn one teammate (operon-as-team-member +
   subagent-definition transformer).
2. Phase advance delivers a brief (inbox.py +
   advance_phase.py modification; deletes watch.py,
   channel_tag.py, message_agent.py channel paths).
3. Newly-spawned teammates get current-phase brief
   (spawn_prompt.py + matcher update; deletes
   `_spawn_subprocess` and OPERON_AGENT_HANDLE paths in
   spawn_agent.py).
4. Teammate-to-operon messages (inbox_reader.py with
   filesystem watcher; deletes nudge.py, Stop hook block,
   stop-wrapper, identity-freeze surfaces in identity.py).
5. Restore on /resume (extend spawn_prompt.py for the
   resume case; deletes any remaining dead-after-Land-4
   code).

Status: bumped to DRAFT v2.9.

### v2.8 changelog (since v2.7)

Promote-vs-describe scrub. The spec body should describe what
the system does, not justify why the design is good. Removed
rationale blocks that promote the architecture.

Blocks removed from the spec:

- **Section 5 "Composability properties of the above:"** (5
  bullets after the advance_phase pseudocode). Each bullet
  asserted a separation property ("the workflow engine has no
  knowledge of the inbox file format," "the inbox writer has
  no knowledge of phases," etc.). The pseudocode above
  already specifies the function signatures and call
  boundaries; the bullets restated them as design virtues.
  Removed.
- **Section 5.1 "Why the seam is clean:"** (4 bullets after
  the spawn-prompt mutation flow). Same pattern -- asserted
  what the hook does NOT do, what the workflow engine is
  "oblivious to," what stays "insulated." Removed.
- **Section 5.2 "Why this matters compositionally:"
  paragraph** trimmed to a one-paragraph operational
  statement of the contract selection, dropping the
  promotional framing.

Additional promote-pattern edits (supplemental sweep):

- **Section 5.3 schema corrections**: dropped "This is why
  WA1's prompt-mutation hook is needed" from the
  "Each subagent invocation gets its OWN `agent-<hash>.jsonl`"
  bullet. The operational fact (runtime does NOT re-read prior
  jsonl files) stands without the design-rationale
  attribution.
- **Section 9.4 stateful restore semantics**: "This is
  acceptable for forward-progress workflows but does NOT
  approximate true session-state-restoration" -> "Forward-
  progress workflows tolerate this; true session-state-
  restoration is not achieved." Drops the "acceptable"
  judgment; keeps the factual limitation.
- **Section 10.1 in-process commitment, second bullet**: "In-
  process keeps the seams clean. The PreToolUse hook fires..."
  -> "Under in-process, the PreToolUse hook fires..." Drops
  the "keeps the seams clean" framing; the operational facts
  stand.

Editorial framings tightened:

- **Section 4.2 "The compositional law"** -> "The inbox-write
  contract operon and the runtime both obey." The boxed
  contract quote stays (operational; specifies what writers
  are allowed). "This is the law. ... Everything follows from
  it." closing dropped.
- **Section 4.5 closing** "The inbox-write primitive composes
  cleanly here because..." -> flat statement of the
  writer-cardinality facts.
- **Section 4.6 "The compositional consequence:" intro** ->
  direct statement of what operon's MCP owns.
- **Section 10.1 "The compositional rationale:" intro** ->
  dropped; the four bullets below speak for themselves and
  were already retained as standing-decisions content per
  prior leadership scope.

What stayed:

- The inbox-write contract quote in Section 4.2 (it specifies
  who can write to the inbox; load-bearing for implementers).
- The classification of Section 11 entries as "compositional
  anchors" vs "operational edges" (organizes the table).
- The Section 4.5 in-process-mode bullet list (operational
  facts about the mode, not rationale).
- The Section 10.1 four-bullet body (Boaz's explicit
  preserve-as-standing-decision call in earlier reviews).
- The Section 5.2 longevity directive itself and its
  placement explanation (the directive IS a spec item; where
  it sits is operational).

Spec body shrinks. The relocated rationale fits in this
changelog -- the bullets are preserved here for readers who
want the "why" exposition, but the spec body is now
description-only.

Status: bumped to DRAFT v2.8.

### v2.7 changelog (since v2.6)

Two changes bundled into v2.7: the citation pass (primary) and
a cache-discipline supplement covering runtime-vs-operon-owned
state.

**Cache discipline supplement (Section 4.6 + Section 5 step
4d).** v2.6's "Operon-owned state" table listed the agent
roster as "in-memory dict... refreshed when the team config
changes." That framing was misleading: the team config IS the
roster, the runtime mutates it on every Agent-tool spawn and
shutdown handshake, and operon doesn't have a reliable signal
for "the team config changed" -- so any cache opens a stale
window where briefs are written to teammates that have just
been reaped or omitted from teammates that have just spawned.

v2.7 fixes this by:

- Retitling the Section 4.6 table to "State touched by operon" and
  adding a runtime-owned row for the team roster
  (`~/.claude/teams/<team>/config.json`) marked **read on
  demand, never cached**. The row explains the staleness
  hazard explicitly.
- Tagging the other two rows (phase state, audit log) as
  operon-owned so the cache distinction is visible at a
  glance.
- Adding a closing paragraph to Section 4.6 stating the principle:
  "Operon caches state it OWNS (single-writer artifacts where
  the cache cannot go stale). Operon reads on demand from
  state the RUNTIME owns (multi-writer artifacts where any
  cache opens a staleness window)."
- Updating Section 5 step 4d to make the read-on-demand explicit:
  the brief-delivery loop now starts with "Read
  `~/.claude/teams/<team>/config.json` from disk (no cache;
  the runtime mutates this file on every spawn or shutdown
  handshake -- see Section 4.6)" before iterating the roster.

The implementation implication: `inbox_writer.py` and
`advance_phase.py` must `Path.read_text()` the team config on
each brief-delivery loop entry, not call a cached helper. No
new module is required; the cache discipline is a property of
each caller's read pattern.

**Citation pass (primary).** The spec now carries file:line
citations where
it touches existing code in operon-plugin or claudechic, so
implementers can navigate directly from spec to source. Per
the audit (`SPEC_GROUNDING_AUDIT.md`), the v2.6 spec was
mostly grounded but lacked the implementer-pointer
citations.

Citations added (verified against current source):

- **Section 4.3 component 1 (workflow engine).** Existing tool
  modules cited: `src/operon_mcp_server/tools/{advance_phase,
  get_phase, set_artifact_dir, activate_workflow,
  restore_operon_session}.py`. `phase_state.json` path cited
  at `src/operon_mcp_server/paths.py:137`.

- **Section 4.3 component 2 (guardrail rules engine).** Now
  cites both the claudechic pattern
  (`claudechic/guardrails/hooks.py:67 evaluate`, the `Rule`
  dataclass in `claudechic/guardrails/rules.py`, tokens in
  `claudechic/guardrails/tokens.py`) and operon's analogue
  (`src/operon_mcp_server/rules.py:418 _evaluate`,
  `tools/evaluate.py`, `tools/request_override.py`,
  `tools/acknowledge_warning.py`). The current matcher line
  is cited at `plugins/operon-plugin/hooks/hooks.json:6`.
  Also notes the rationale gap: operon hooks are out-of-
  process (`type: command`), whereas claudechic hooks are
  in-process closures -- this is why operon needs a tiny
  on-disk current_phase file (Section 4.6) and claudechic
  does not.

- **Section 4.3 component 8 (audit log).** Path resolved at
  `src/operon_mcp_server/rules.py:470 guardrail_log_path`;
  writer at `:475 append_log_event`; PIPE_BUF discipline at
  `:478-499`. Mirrors `claudechic/guardrails/hits.py:36
  HitLogger`, called from
  `claudechic/guardrails/hooks.py:134-161`.

- **Section 4.5 hook description.** Cites the PreToolUse hook
  block at `plugins/operon-plugin/hooks/hooks.json:4-15` with
  the matcher line at line 6.

- **Section 4.6 disk-state table.** `current_phase` /
  `phase_state.json` path cited at
  `src/operon_mcp_server/paths.py:137`; reader at
  `src/operon_mcp_server/identity.py:269 _read_current_phase`.
  The hook-delivery rationale (operon hooks run
  out-of-process per `hooks.json:9`) is now in the row.

- **Section 6 DELETE rows.** Each deletion row gets the
  relevant source coordinate or line count. Specifically:
  - `watch.py` (653 lines), `locks.py` (444 lines),
    `channel_tag.py` (83 lines), `mailbox.py` (332 lines),
    `nudge.py` (636 lines) -- file sizes captured so the
    deletion PR's diff size is predictable.
  - `tools/spawn_agent.py` REFACTOR row cites the specific
    surfaces: `_spawn_subprocess` (line 603),
    `OPERON_AGENT_HANDLE` env propagation (line 675),
    `roster.append_agent` (line 953).
  - `identity.py` DELETE row cites the four Phase 14 fix
    4/5 surfaces: `_FROZEN_HANDLE` (line 57), `_DRIFT_SEEN`
    (line 66), `freeze_handle()` (line 73), drift-detection
    block (lines 186-225).
  - Stop block DELETE cites `hooks.json:16-26` and the
    `stop-wrapper` script path at line 21.

- **Section 7 extension rows.** PreToolUse hook matcher row
  cites the current matcher location at `hooks.json:6`. The
  `guardrail_log.jsonl` extension row cites the existing
  writer at `rules.py:475` with PIPE_BUF discipline at
  `:478-499`.

- **Section 8 migration plan.** Step 0.5 cites the TeamsSpike
  scenario directory at `/tmp/operon-teams-spike/step05/`.
  Step 1 deliverables each cite the source file path that
  will exist (or be modified). Step 4 lists the specific
  files affected by the code-deletion PR for diff
  predictability.

- **Glossary `guardrail_log.jsonl` entry.** Cites the path
  function, writer function, and PIPE_BUF discipline line
  range.

Verification: every line number was read from the current
source before being inserted. Files verified:
`src/operon_mcp_server/identity.py`, `paths.py`, `rules.py`;
`src/operon_mcp_server/tools/spawn_agent.py`;
`plugins/operon-plugin/hooks/hooks.json`;
`claudechic/guardrails/{hooks.py,hits.py,rules.py}`.

No discrepancies surfaced between the audit's claimed lines
and the current source. The audit's recommendations on
findings 3 (identity.py) and 4 (spawn_agent.py) propagated
into the spec verbatim; finding 5 (rules.py module mapping)
propagated as the multi-file cluster citation; findings 1, 2,
6 were already addressed by the v2.6 edits.

Status: bumped to DRAFT v2.7.

### v2.6 changelog (since v2.5)

Four edits resolving audit findings on the spec body.

- **Audit log filename + scope + concurrency (Section 4.3
  component 8, Section 4.6 disk-state row, Section 7,
  Section 8 Step 1 deliverable, Section 9.3, Section 9.5,
  Section 10.1, glossary).** The audit artifact reverts to
  the existing filename `<run-dir>/guardrail_log.jsonl`; the
  `.operon/<run>/events.log` rename is dropped (the file
  already exists under that name in `rules.py:478-499`). The
  writer list is trimmed to two callers: the PreToolUse hook
  handler and the `advance_phase` handler. Inbox writer /
  reader entries are removed from the audit list -- inbox
  bodies live in the runtime's inbox files; the audit log does
  not duplicate them and does not log metadata or retry
  counts. A concrete concurrency-discipline statement is
  added: `O_APPEND` with rows under 4 KiB, PIPE_BUF atomicity
  for concurrent appends from operon's MCP and the external
  PreToolUse hook subprocess.

- **Active-set concept removed (Section 4.6, Section 5
  step 4d, Section 5.1 step 1).** The runtime's team config
  IS the live roster under in-process mode; every spawned
  teammate is in the team config. Operon does not maintain
  a separate "active set" tracked via PreToolUse hook
  observation. Section 4.6's two-concept exposition collapses
  to one: the team roster from `~/.claude/teams/<team>/
  config.json`. Section 5 step 4d's loop iterates the team
  roster (briefs go to roster members; teammates not yet
  re-spawned post-/resume receive their brief at re-spawn
  time via the spawn-prompt mutation). Section 5.1 step 1's
  active-set rebuild description is removed; the boot-time
  detection is now just team config + current_phase. Per-
  spawn transcript reconstruction (step 3) is unchanged.

- **Stop and SessionStart hooks removed (Section 4.5, Section
  6 code-deletion table, glossary "In-process mode" entry).**
  The Stop hook's only handler was `nudge_check`, and
  `nudge.py` is on the deletion list; with nudge gone, Stop
  has no purpose. SessionStart was referenced in earlier
  drafts but is not registered in `hooks/hooks.json`. Section
  4.5's "PreToolUse / SessionStart / Stop hooks catches all
  team traffic" is rewritten to "PreToolUse hook catches all
  team tool calls." Section 6 gains two DELETE rows:
  `hooks/hooks.json` Stop block and the
  `hooks/stop-wrapper` script. Operon's hook surface is
  PreToolUse only.

- **SendMessage matcher claim dropped (Section 4.3 component
  2, Section 7 PreToolUse-matcher row, Section 8 Step 1
  deliverable, Section 8 Step 3 verification line, glossary
  SendMessage entry).** v2.5 claimed the PreToolUse matcher
  was widened to include `SendMessage`, which would have
  required workflows to ship rules targeting message content
  (not in v2 scope). The spec now records the actual matcher
  `Bash|Edit|Write|MultiEdit|NotebookEdit` and notes that
  adding `SendMessage` is a future extension. The Section 7
  matcher row updates to add only `Agent` to the existing
  list. The glossary SendMessage entry no longer asserts
  PreToolUse coverage.

Architectural side-effects surfaced by these simplifications:

- Active-set removal eliminates one source of state that
  operon was tracking redundantly. The spawn-prompt mutator
  still fires on every `Agent` invocation (for brief
  injection plus WA1 transcript replay on resume), but its
  role is no longer "observe to update operon's roster" --
  the roster IS the team config. Hook responsibilities are
  cleaner: matcher-driven enforcement (for the existing tool
  list) and Agent-tool spawn-prompt mutation. Nothing else.

- Audit log scope simplification means the inbox reader /
  router's writer-side path (relay to lead, protocol
  dispatch) is not audit-logged. The persistent record of
  what was relayed is the lead's inbox file itself; the
  persistent record of protocol dispatches is the
  shutdown_approved entry in the sender's inbox. Operon does
  not double-record this in the audit log. If a workflow
  needs auditing of inbox traffic, that is a future
  extension orthogonal to the guardrail log.

- Stop hook removal means there is no longer a per-turn-end
  operon entry point. Anything that could have used a
  per-turn-end signal (post-turn nudges, post-turn
  bookkeeping) is unsupported in v2. The PreToolUse hook
  fires per-tool-call, not per-turn-boundary.

- The SendMessage matcher decision means inter-teammate
  message bodies bypass operon's guardrail evaluation
  entirely. Workflows that rely on rule enforcement over
  inter-teammate messages (e.g. "no swearing in
  SendMessage") would need to wait for a future spec
  revision that re-introduces `SendMessage` to the matcher.
  v2 workflows that ship rules: those rules cover
  Bash/Edit/Write/MultiEdit/NotebookEdit only.

Status: bumped to DRAFT v2.6.

### v2.5 changelog (since v2.4)

Architectural addition: operon's MCP reads its own inbox and
routes inbound entries. The "reply tracking is out of scope"
deferral from v2.4 Section 10.2 is replaced by a concrete
reader / router design.

- **New Section 4.3 component 4 (Inbox reader / router).**
  `src/operon_mcp_server/inbox_reader.py`. Runs a filesystem
  watcher (Python `watchdog` / inotify / FSEvents) over
  `~/.claude/teams/<team>/inboxes/operon.json`. The watcher is
  a long-running async task in operon's MCP process; it starts
  at MCP boot and stops at MCP shutdown. Not LLM polling and
  not lazy-poll-on-tool-call. On each filesystem-change event,
  the reader walks `read: false` entries and dispatches them
  per the routing rules in Section 4.7.

- **Section 4.3 components renumbered.** advance_phase tool
  4 -> 5; subagent-definition transformer 5 -> 6; restore
  module 6 -> 7; audit log 7 -> 8. The audit log row gained
  an `inbox_reader` writer bullet (each inbound entry is
  audit-logged).

- **Section 4.7 extended with routing semantics.** Operon is a
  team member that reads and routes, not a write-only
  participant. Two routing cases:
  - Protocol-typed messages with recognized reply contracts.
    The currently-recognized protocol is `shutdown_request`
    with reply `shutdown_approved`. Operon's MCP writes the
    reply to the sender's inbox via Section 3.3 rule 1. No
    LLM involvement on either side of operon's process
    boundary.
  - Free-form text and unrecognized protocol types. Operon
    relays the entry verbatim to the lead's inbox with
    `from: "operon"`, `summary: "relayed from <sender-name>"`.
    The lead's LLM sees the relayed content as a
    `<teammate-message teammate_id="operon">` on its next turn
    boundary and decides how to respond.
  Processed entries are marked `read: true` via the Section
  3.3 rule 1 stat-retry mechanism (same loop, applied to
  operon's own inbox file).

- **Section 3.3 extended.** A trailing paragraph notes that
  the optimistic-concurrency-with-retry mechanism applies
  symmetrically to operon read-modify-writing its own inbox to
  mark inbound entries as `read: true`. The runtime can write
  new inbound entries to that file concurrently with operon's
  marking pass; the stat-retry loop handles the interleaving.

- **Section 4.4 closing paragraph rewritten.** The v2.4
  "Reply tracking is out of scope" line is replaced by an
  affirmative statement: operon maintains no timer scheduler;
  inbound delivery is filesystem-watcher-driven (Section 4.3
  component 4); inter-teammate message timing is the
  runtime's concern.

- **Section 7 code-addition audit.** New row for
  `src/operon_mcp_server/inbox_reader.py` (~50 LOC + tests).
  Total LOC estimate updated from ~350 to ~400.

- **Section 10.2 retitled.** "Reply tracking" -> "Inbound
  message routing." Body replaced with a current-state
  summary of the routing design and a note that extending
  the closed set of recognized protocols is a future spec
  revision.

- **Section 6 nudge.py row.** Reason rewritten: "Inbound
  reading uses a filesystem watcher (Section 4.3 component 4),
  not a timer."

- **Section 11.** Three new NOT TESTED rows: filesystem
  watcher reliability on operon's own inbox; protocol-dispatch
  round-trip for `shutdown_request` -> `shutdown_approved`;
  free-form relay rendering in the lead's session. All marked
  pre-implementation gates.

Architectural side-effects surfaced during the rework:

- **Lead receives multiple inbox entries from operon.** When
  operon relays a free-form message from a teammate to the
  lead's inbox, the lead's inbox has an `<teammate-message
  teammate_id="operon" summary="relayed from <sender>">`. The
  lead's inbox can also receive an `<teammate-message
  teammate_id="operon" summary="Phase X brief">` from
  advance_phase. The summary field is the disambiguator; both
  paths use `from: "operon"` so the runtime renders them
  uniformly. No collision; the lead reads them in order.

- **Lead sending to operon is supported but ambiguous.** If
  the lead's LLM uses SendMessage targeting "operon" with
  free-form text, operon will relay that text BACK to the
  lead's inbox per the free-form routing rule. The lead
  effectively talks to itself through operon. This is harmless
  (the lead can recognize its own content) but worth flagging
  as a behavioral curiosity. A future spec revision could add
  a self-loop suppression rule if it becomes a real concern.

- **Protocol dispatch is closed.** The reader recognizes
  exactly one protocol (`shutdown_request`). Other JSON-shaped
  bodies or protocol-prefixed text fall through to free-form
  relay. Extending the closed set is a spec revision, not a
  runtime configuration. This keeps the dispatch logic simple
  and inspectable.

- **The "shutdown" semantics are coordinative, not
  authoritative.** Operon does not control teammate lifecycle
  (Section 6.1: lifecycle is the runtime's axis). The
  `shutdown_approved` reply is an acknowledgement contract:
  the teammate that asked is told it has permission, but the
  teammate is the one that decides what shutting down means
  (typically: stop emitting `SendMessage` calls and let the
  runtime quiesce it). Operon does not invoke an interrupt or
  close tool on the teammate.

- **Filesystem watcher lifecycle is bound to the MCP.** The
  watcher starts when operon's MCP starts and dies when it
  dies. There is no separate watcher process and no watcher
  state on disk. If operon's MCP crashes and restarts, the
  watcher restarts cold; the inbox file's `read: false`
  entries are processed on the next watcher fire (the file
  itself is the persisted queue).

Status: bumped to DRAFT v2.5.

### v2.4 changelog (since v2.3)

Editorial strip-pass plus two Skeptic2 follow-up notes.

Strip-pass (primary). Per the role-spec rule that the body of
this document must describe what the system IS doing in
positive terms, every backward-pointing phrase was reviewed:

- Section 1 status block: removed iterative-versioning note,
  trimmed editorial framing.
- Section 2.1: tense fix ("required" -> "requires").
- Section 2.2: dropped "instead of building a parallel system."
- Section 4.2 lede: "post-pivot system" -> "system."
- Section 4.3 component 1: removed "(kept, substantially
  unchanged from pre-pivot)" parenthetical.
- Section 4.4: rewritten end-to-end. "What goes away (axes
  that collapse)" -> "Runtime-owned surfaces." Each bullet
  recast as a current-state statement about what the runtime
  owns; the reader no longer needs to know what operon used
  to do.
- Section 4.5: dropped "in v2" version reference on tmux
  unsupported.
- Section 4.6: rewrote the singleton-MCP rationale to drop the
  "removes the need for..." phrasing (defensive against
  pre-pivot state). Replaced the two-column "Pre-pivot state /
  Post-pivot state" table with a single-column "Operon-owned
  state" table; pre-pivot column names are no longer in the
  spec.
- Section 5 step 4d: dropped "; there is no lead-exclusion
  clause" tail.
- Section 5 step [5] prose: dropped "-- there is no separate
  own_brief field in the tool response" tail. Replaced with
  "Brief delivery is uniform across the team."
- Section 5 composability bullet for lead-uniformity: tightened
  to a single sentence.
- Section 5.1 quality notes: dropped "An earlier spike round
  tried Haiku-summarization..." Replaced with "Raw
  concatenation is the rule. Do not summarize."
- Section 6 row reasons: every backward-pointing reason
  rewritten as current-state. The deletion-list table still
  describes migration actions (DELETE / REFACTOR / NEUTRAL) but
  the Reason column reads as the architectural property each
  row reflects, not a "this used to do X" justification.
- Section 6 commits-that-become-legacy lede: "Commits that
  become legacy under the pivot" -> "Commits in-tree on
  operon-plugin/main that the deletion list above
  supersedes."
- Section 6 deleted the "Phase 5 workflow engine... ALL KEEP"
  sentence; preserved features are described in Section 4.3.
- Section 6.1: "v2 ships no operon-side lifecycle control" ->
  "Operon ships no lifecycle control over teammates." Dropped
  the "Workflows that depended on interrupt_agent..." sentence.
- Section 7 advance_phase.py row: dropped "(refactored)" tag.
- Section 7 events.log writer row: dropped "replaces
  guardrail_log.jsonl" suffix.
- Section 8 Step 2: dropped "(some tests will be removed
  entirely because they exercise deleted modules)" parenthetical.
- Section 8 Step 6: dropped the "current SPEC.md describes the
  mailbox + watch + channel-push substrate" comparison.
- Section 9.2: "We inferred" -> "The schema is inferred."
- Section 9.3 last paragraph: "Identity tracking moves to
  operon's own roster; audit moves to events.log..." -> "lives
  in operon's own roster; lives in events.log..."
- Section 9.5 (audit relocation): DELETED entirely. The audit
  artifact is described in current-state form in Section 4.3
  component 7 already.
- Section 9.5 (formerly 9.6, tmux-mode launch): "multi-MCP-
  subprocess class returns" -> "multi-MCP-subprocess problem
  appears."
- Section 9 renumbering: 9.6 -> 9.5, 9.7 -> 9.6, 9.8 -> 9.7,
  9.9 -> 9.8. Cross-references updated at Section 3.3
  ("Section 9.9 contract" -> "Section 9.8 contract"), Section
  4.5 (tmux refusal), Section 10.1 (tmux refusal), and the
  Team config glossary entry.
- Section 10.1 UX trade-off bullet: "was user-accepted on the
  basis of..." -> "The UX contract: cycle Shift+Down..."
- Section 10.2: "Reply tracking deferred from v1." -> "Reply
  tracking is out of scope."
- Glossary cleanup: Teammate entry dropped "Operon's prior
  'worker' terminology maps to this"; teammateMode entry
  dropped "in v2" version reference; Singleton MCP entry
  dropped the "makes the deletions in Section 4.4 coherent..."
  clause; operon_run entry updated to list `current_phase` +
  `events.log` (no more roster snapshot); events.log entry
  dropped "Replaces pre-pivot guardrail_log.jsonl" suffix.

N2 -- Section 11 row added. New NOT TESTED row covering: the
lead's own inbox is consulted by the runtime during the tool-
result phase of the turn that issued `advance_phase`, vs. only
at the next user-prompt-triggered turn boundary. The lead's
brief-delivery latency depends on this. Marked as a pre-
implementation gate; Step 0.5 validates implicitly.

N3 -- Section 4.7 ambiguity fix. The earlier "Operon's MCP
reads inboxes/operon.json on its own turn boundaries to receive
replies" sentence implied a polling mechanism without naming a
trigger. Replaced with explicit non-reading: "Operon does not
read inboxes/operon.json. Inbound replies from teammates
accumulate in that file unprocessed; reply tracking is out of
scope (Section 10.2)." Consistent with Section 10.2's defer-
reply-tracking posture.

(Skeptic2 notes N1 and N4 from the same review round were
withdrawn before application -- N1's residual-race window is
not a practical concern, and N4's mtime_ns tiebreaker is not
needed at nanosecond resolution.)

Status: bumped to DRAFT v2.4.

### v2.3 changelog (since v2.2)

Skeptic2 + user-decision outcomes applied in v2.3:

- **CR1 -- Concurrent-writer fix.** Section 3.3 rule 1 was
  rewritten. The earlier framing ("os.replace is atomic
  regardless; the lockfile is Anthropic's concern; operon does
  not need to acquire it") implicitly assumed no read-modify-
  write race between operon and the runtime. The new
  formulation is optimistic concurrency with retry: stat the
  inbox file before read; re-stat before replace; if the
  `(inode, size, mtime_ns)` tuple changed, re-read and retry;
  bounded to 5 attempts; structured error on exhaustion (no
  silent loss, no in-place fallback). Section 4.3 component 3
  (inbox writer) updated, Section 5 key safety property
  updated, Section 5 step 4d body updated to reference the
  Section 3.3 mechanism by name, Section 9.7 rewritten to
  match. Estimated inbox.py size grew from ~30 LOC to ~50 LOC;
  total Section 7 additions grew from ~330 LOC to ~350 LOC.

- **SF-A -- Transcript concatenation order.** Section 5.1
  step 3 now specifies that `agent-<hash>.jsonl` files for a
  given role are ordered by file mtime ASCENDING before
  concatenation. Deterministic; matches temporal order of the
  teammate's work across multiple subagent invocations.

- **SF-B -- Lead is a teammate, uniformly.** Section 5 step
  4d's loop no longer excludes the lead. The advance_phase
  tool response no longer carries an `own_brief` field. Every
  spawned team member -- the lead included -- sees its phase
  brief in its own inbox at its next turn boundary. Section 5
  composability properties section gained a bullet stating
  the uniform-teammate property explicitly. The asymmetry is
  eliminated.

- **SF-C -- No roster_snapshot.jsonl.** The disk-state-
  minimality directive (operon's persisted files limited to
  `current_phase`, `events.log`, workflow content library)
  was applied to the spec. Section 4.6.1 was removed. Section
  4.6 gained a paragraph defining roster (from team config)
  and active set (in-memory, populated by PreToolUse hook
  observation in-session plus a one-time walk of the runtime's
  `subagents/` directory on MCP boot to recover historically-
  spawned teammate types). The third term "roster snapshot"
  is dropped. Section 5.1 step 1 was updated to remove the
  roster-snapshot source; the boot-time walk is the recovery
  anchor.

Editorial:

- Status header bumped to DRAFT v2.3.

### v2.1 changelog (relocated from spec Section 1, was "v2.1 changelog (since v2.0 caught-up)")

User-decision outcomes applied:

- **Delivery confirmation (Skeptic2 MF2):** NEITHER option. No
  read-back seam, no teammate-acknowledge-receipt instruction.
  The `advance_phase` contract is: operon writes the inbox file
  and returns "advanced." Section 9.9 trimmed to remove the
  "operon needs to handle delivery edge cases via correlation
  IDs" prescription; the delivery-semantics risk class is
  acknowledged as a runtime-contract fact, no operon-side
  response is specified.
- **Operon-as-team-member (Skeptic2 SD1):** No fallback. v2.0
  framing (operon registers as a team member, no spoof fallback
  documented) stands unchanged. Section 8 Step 0.5 catches a
  registration failure cheaply before code ships.
- **Exit ramp (Skeptic2 SD2):** REMOVED entirely. Section 10.2
  "Exit-ramp framing" paragraph deleted. Section 9.1 and 9.8
  speculation about how operon would respond to runtime changes
  deleted; risk acknowledgment kept to one line. Section 8
  Step 5 reference to the `pre-pivot-stable` git tag removed
  (an implementation-time call, not a spec concern).

### v2.0 rewrite scope (relocated from spec Section 1)

What was rewritten (Composability voice):

- **Section 4** ("Proposed architecture") -- recast around the
  axis decomposition. The pivot's value is exposed as: which
  axes operon keeps, which axes operon delegates to Anthropic,
  and what protocol (the law) lets the two compose. Each
  subsection now names the axis it concerns and the seam it
  meets.
- **Section 5** (advance_phase mechanism, WA1 restore,
  longevity guidance, schema corrections) -- restated as
  protocols-and-seams rather than as procedural recipes. The
  mechanism is still concrete; the framing now makes clear what
  crosses each boundary and what does not.
- **Section 10.5** (in-process commitment) -- recast as a
  compositional argument: in-process collapses the writer
  cardinality on operon-owned state, which makes the rest of
  the deletion list coherent.
- **Section 11** (empirical validation status) -- table preserved
  in shape; framing made explicit about which entries anchor the
  compositional law vs. which probe operational edges.

What was preserved verbatim:

- Section 2 (motivation), Section 9 (risks), and Section 10.1-
  10.4, 10.6 (open questions for the other Leadership voices)
  -- substance unchanged; light framing edits only.
- Section 6 (code-deletion audit), Section 7 (code-addition
  audit), Section 8 (migration plan) -- code-impact tables are
  Implementer territory and left as authored.
- Section 12 ("what this doc is NOT"), Appendix A (future
  opportunities) -- preserved.

What was added in response to TerminologyGuardian2's preliminary
review of v1.3:

- Lead-vs-teammate disambiguation. v1.3 was ambiguous about
  whether the lead is itself a teammate. v2.0 commits: the lead
  IS a teammate (the one whose claude process hosts the team
  under in-process mode).
- Team-member-vs-teammate distinction. v1.3 conflated the two.
  v2.0 separates them: "team member" is the roster-level term
  for any registered participant; "teammate" is reserved for
  team members that participate as in-process subagent
  invocations. Operon's MCP is a team member but NOT a
  teammate.
- Appendix B glossary gained six entries (Team, Team config,
  Team member, Singleton MCP, Conversation session, MCP
  transport session) and cross-linked the sidechain triple.

What was removed:

- Fallback framing. Where prior revisions hedged with "if
  Anthropic does X, fall back to Y," this revision states the
  single committed path and identifies the empirical gate that
  would surface a need to revise.

### v1.3-final changelog (since v1.3 draft) (relocated from spec Section 1)

Per Coordinator: minimize fallbacks and hedges. The plan documents
COMMITTED design, not menu-of-options. Five targeted edits:

- Section 4.6: operon-as-team-member is the sole approach.
  Spoof fallback removed entirely; if empirical validation
  fails, the doc gets revised then.
- Section 10.6 reply tracking: cut to one-line defer.
- Section 10.3 SDK exit-ramp: dropped the 4-6 week figure;
  exit ramp acknowledged without estimate.
- Section 5.1 WA1 idempotency: discussion removed from 5.1;
  added as a single NOT TESTED row in Section 11.
- Section 9.6 loud-refusal detection: pid-file fallback
  dropped; env-var + team-config detection only, with a
  one-line "best-effort" limitation note.

### v1.3 changelog (relocated from spec Section 1)

The user has COMMITTED to the X1 pivot, `teammateMode: in-process`,
and singleton-MCP architecture. v1.3 stops being a "which option?"
document and becomes a "how do we build this?" document. Major
edits since v1.2:

- Section 10.5 rewritten. No more Option (a) vs (b). The
  in-process commitment is stated up front with rationale; tmux
  mode is deferred to a hypothetical Phase 17+.
- NEW Section 2.0 "Economic forcing function". The strongest
  motivation -- moving off claude-agent-sdk's API-token cost path
  to claude-code-native execution -- was missing from v1.2.
- NEW Section 4.5 "Singleton MCP architecture". Under in-process
  mode, operon's MCP runs ONCE in the lead's claude process.
  Most operon disk state collapses to in-memory or to small
  per-process artifacts.
- NEW Section 5.1 "WA1: Restore via Agent-tool prompt mutation".
  Empirically validated by TeamsSpike. Operon's PreToolUse hook
  on the `Agent` tool mutates the spawn-prompt to inject prior
  sidechain transcripts so resumed teammates carry forward their
  state.
- NEW Section 5.2 "Subagent definition longevity guidance".
- NEW Section 5.3 "Schema corrections from spike".
- NEW Section 9.8 / 9.9 -- two missing risks from Skeptic's
  v1.2 review.
- NEW Appendix B "Glossary" -- 17 entries.
- Terminology renames throughout. "mailbox" -> "team directory"
  + "inbox file"; "worker" -> "teammate"; "PUSH primitive" ->
  "inbox-write primitive".
- Composability fixes integrated from v1.2 review.
- Skeptic conditions integrated from v1.2 review.
- UserAlignment additions integrated from v1.2 review.

### Iterative-versioning note (relocated from spec Section 1)

Versioning is iterative; subsequent revisions will be v2.3,
v2.4, etc. as the design evolves. There is no "final" version;
the spec is the architecture being built today and will
continue to be revised as implementation surfaces new
questions. Editorial language about doc lifecycle is recorded
here rather than in the spec body.

---

## Motivation backstory

### Channels-respawn bug class -- Phase 14 history (relocated from spec Section 2.1)

Across Phase 14 fixes 1-8 we built progressively more elaborate
machinery (identity freeze + drift detector + per-agent
lockfile + production `--channels=` flag + tag-detection helper
+ cosmetic-toast documentation) to push channel notifications
across `claude --bg` worker session boundaries reliably. Each
fix worked for a slightly larger fraction of the failure modes
Boaz hit.

The underlying claim from Anthropic, however, is that
backgrounding + attach-respawn losing in-process state (the
dev-channels dialog acceptance; `F$.allowedChannels`) is
documented behavior of `/agents` view (see
<https://code.claude.com/docs/en/agent-view>). Our patches kept
papering over this surface, but the underlying primitive
(`notifications/claude/channel`) is not designed for cross-
session broadcast in the way operon's multi-agent model
required.

The spec body now retains only the load-bearing claim: the
channel substrate is documented as not designed for cross-
session broadcast. The Phase 14 fix-arc history is preserved
here for readers who want context on why operon spent multiple
revisions trying to make channels work before pivoting away.

### Community state-of-the-art comparison (relocated from spec Section 2.3)

InternetResearcher's survey returned one comparable open-source
attempt: `retrodigio/claude-channel-slack`. Its summary: "give
up trying to make channels work in bg; run a tmux/screen
session for each agent in the foreground." That is meaningfully
weaker than Agent Teams + inbox-writer (which actually delivers
messages reliably across sessions).

No prior art exists for using Agent Teams' inbox files as the
delivery substrate for a third-party orchestration layer.
Operon pioneering this pattern is a credible contribution.

This comparison is not load-bearing for the architecture being
built; it's preserved as context for readers asking "did anyone
else solve this?"

---

## Inbox primitive rationale

### Binary trace (relocated from spec Section 3.4)

The runtime's writer is `writeToMailbox` at offset
~220422200, lockfile-mediated via the `.lock` sibling-file
pattern. The consumer is at offset 225040650; it runs at each
turn boundary, reads the JSON array, formats entries with
`read: false` as `<teammate-message teammate_id="<from>"
summary="<summary>"><text></teammate-message>`, marks them
`read: true`, and writes the file back.

Symbol-name note: the binary uses `writeToMailbox` (singular,
internal Anthropic vocabulary). Operon vocabulary calls the
parent directory the "team directory" and the per-member file
the "inbox file." Per NudgeResearcher's disassembly.

Forensic evidence; not load-bearing for the architecture. The
schema is documented in the spec Section 3.2; the read-state
semantics in Section 3.3 rule 3; the validation status in
Section 11.

### Test A PASS transcript (relocated from spec Section 3.5)

TeamsSpike Round 1 + Test A:

> TeamsSpike wrote `{"from": "team-lead", "text":
> "ECHO:PUSH-A-<uuid>", ...}` to
> `~/.claude/teams/push-test-a/inboxes/recv-a.json` via atomic
> tmp + os.replace. recv-a processed the injection exactly as
> if it had arrived via real SendMessage. Reply
> `ECHOED:ECHO:PUSH-A-<uuid>` appeared in team-lead's inbox.
> No corruption, rejection, or crash. Anthropic's reader does
> not validate provenance.

The architectural law established by this test ("any
participant that writes well-formed JSON via atomic replace
participates in the same delivery channel; the runtime makes
no provenance check; the bytes are the protocol") is stated in
the spec Section 4.2. The empirical validation status is
recorded in Section 11. The transcript itself is preserved
here.

### Keystone framing on the inbox-write law (relocated from spec Section 3.5)

The Test A result was the keystone empirical finding. It
established the law that the rest of the architecture composes
against: the inbox file is a bytes-on-disk JSON-array protocol,
and any writer that respects the schema and atomic-replace
semantics participates in the same delivery channel as
Anthropic's own runtime.

Editorial framing moved here so the spec body shows only the
operational law. The empirical anchor is in the spec Section
11.

### UserPromptSubmit lead-side observation (relocated from spec Section 3.6)

Per NudgeResearcher's binary trace: the `UserPromptSubmit` hook
fires in the lead's process on every input it processes,
INCLUDING synthetic `<teammate-message>` wraps of teammate
outbound `SendMessage` calls. Does NOT fire on the teammate's
inbound side.

Practical use case: operon could expose a lead-side audit /
observation surface that captures all team traffic by hooking
UserPromptSubmit and parsing the `<teammate-message>` XML.
This is described under "Future opportunities -- lead-side audit
via UserPromptSubmit" below.

### Note on the "from" field display (relocated from spec Section 3.7)

When operon writes briefs to a teammate's inbox (e.g. on phase
advance), the recipient's session displays a `<teammate-message
teammate_id="operon">` header. "operon" is a registered team
member, so reply routing back to it works the same way reply
routing between actual teammates works.

This is operational UX context, not architecture; the spec
covers operon-as-team-member registration directly.

---

## Architecture rationale

### Why the deletion list is large (relocated from spec Section 4.4 closing)

Closing editorial sentence: "The deletion list is large
precisely because each item was defending a seam that operon no
longer owns."

Self-evident to a reader who walks the deletion list; relocated
here rather than restated as commentary in the spec body.

### tmux residue (relocated from spec Section 4.5)

Earlier draft language repeated the explanation that
`teammateMode: tmux` reintroduces "the cross-process
coordination problem that operon spent Phase 14 fixes 1-8
fighting." That history is recorded in the channels-respawn
section above; the spec body's tmux-rejection now cross-
references the in-process commitment section directly without
restating the historical motivation.

### Operon-as-team-member full rationale (relocated from spec Section 4.7)

Why this design (compositional rationale, full text): the
runtime's reply-routing logic looks up the `from` field against
the team roster. Operon has two ways to participate as a
sender. Either operon masquerades as an existing teammate
(spoofs `from`), or operon appears in the roster as itself. The
spoof path leaks operon's delivery semantics into the teammate
identity axis: reply routing for the spoofed teammate becomes
ambiguous, and the teammate's transcript displays
`<teammate-message teammate_id="operon">` from a name that
elsewhere in the system means "the LLM at that role."
Registering operon as a team member keeps the seam clean: the
teammate identity axis treats "operon" as a first-class
participant, and reply routing resolves without ambiguity.

The spec body retains a tight three-line version of this
rationale and the operational empirical-gate note. The full
rejected-alternative discussion lives here.

---

## interrupt_agent / close_agent options breakdown (relocated from spec Section 6.1)

Both tools existed in the pre-pivot design as operon-side
controls over teammate lifecycle. Under the pivot, lifecycle
moves entirely onto the runtime's axis. Two candidate designs
presented themselves:

- **(a) Bind to a runtime-native interrupt/close tool.** Clean:
  lifecycle stays on the runtime side; operon defers entirely.
  The defect is that no such tool exists in CC v2.1.32+; there
  is no contract to bind to.
- **(b) Treat the inbox file as a control channel.** Operon
  writes typed control envelopes (e.g. `{"type": "interrupt"}`)
  to a teammate's inbox; the teammate's LLM, or a runtime hook,
  acts on them. The defect is that this expands the inbox-
  transport axis to carry two semantic types: messages and
  control commands. That bifurcates the axis at exactly the
  seam the pivot is trying to keep clean. It also raises the
  surface area Anthropic might choose to gate behind provenance
  validation -- control envelopes are more invasive than
  message envelopes, and asking the runtime to honor them
  stretches the "bytes are the protocol" law.

The spec body records the decision (delete both, decline to
ship either) plus a one-sentence rationale. The (a)/(b)
breakdown is preserved here for readers who want the
rejected-alternative anatomy.

---

## Risk-section detail

### Section 9.1 full text on experimental flag instability (relocated)

Earlier drafts described the experimental flag as gated, noted
that Anthropic may change semantics or remove the feature, and
specified monitoring of CC release notes plus parse-time
breakage detection. The spec body now carries the
one-line risk acknowledgment; the longer monitoring posture
is the same operational practice and is not architectural.

### Section 9.5 historical mailbox audit retelling (relocated)

Pre-pivot, `.operon/<run>/mailbox/<agent>/inbox/processed/` was
the canonical audit trail of inter-agent messages. Under the
pivot, audit moves to:

- Anthropic's team inbox files (transport; not durable record).
- `.operon/<run>/events.log` (operon's own append-only JSONL).

The events.log captures everything PreToolUse evaluates + every
operon-injected inbox write + every advance. Historical mailbox
data from the pre-pivot era is test/dev artifacts; not
migrated.

The spec body now states the migration outcome in one line; the
historical retelling is preserved here.

### Section 9.8 full text on inbox provenance validation (relocated)

A future Claude Code release could add provenance validation on
inbox writes -- e.g. require that writes be cryptographically
signed by a team-registered member, or that they go through the
runtime's own write path (rejecting direct `os.replace` writes
from operon).

- Impact: HIGH. The inbox-write primitive would evaporate;
  operon would lose its delivery mechanism.
- Likelihood: LOW. No current indication Anthropic is adding
  provenance gates; the experimental flag's stated purpose is
  to enable rapid iteration, not to lock down semantics.
- Mitigation: monitor CC release notes.

The spec body now carries the one-line risk acknowledgment plus
the monitoring mitigation. The Impact/Likelihood scoring and
the structured "what would change" description are preserved
here for readers who want the full anatomy.

### Section 9.9 delivery-semantics rationale tail (relocated)

Earlier draft expanded the "no tracking" decision with: "adding
a tracking layer would require either a read-back seam (operon
polling the inbox after write to see if the entry survived) or
a teammate-side ACK instruction (each teammate replies to
confirm receipt), both of which expand the architecture's
surface area without addressing a demonstrated failure mode."

The spec body retains the decision (operon does not track
delivery, solicit ACK, or retry) in a tighter form. The
two-options breakdown is preserved here as rejected-alternative
documentation.

---

## Reviewer prompts (relocated from spec Section 10)

Section 10.1-10.4 were open-question prompts to the v1.3 / v2.0
Leadership reviewers. Once reviews landed, the prompts became
process artifact. They are preserved here for traceability;
sign-off outcomes are recorded in the changelogs above.

### Section 10.1 TerminologyGuardian2 prompt

> Confirm the v1.2 rename suite landed correctly in v2.0 and
> identify remaining inconsistencies.

The renames applied throughout v2.0:

- "mailbox" -> "team directory" (parent dir) + "inbox file"
  (per-member file).
- "worker" -> "teammate" in user-facing prose; "agent" reserved
  for the generic LLM concept and for Anthropic's `Agent` tool.
- "PUSH primitive" -> "inbox-write primitive".
- Section 3 heading renamed accordingly.

Verification asks (carried out in v2.0 caught-up):

- No remaining "worker" or "mailbox" in user-facing prose
  (acknowledged that `mailbox.py` module name is grandfathered
  pre-pivot and deleted in the pivot).
- "session_id" footnote confirmed: "Claude Code conversation /
  supervisor session, not the MCP transport session" is the
  right disambiguation.
- Glossary entries audited; "law" and "seam" use compositional
  prose definitions in spec body rather than glossary entries.

### Section 10.2 Skeptic2 prompt

> What breaks? What assumptions are we making about Agent Teams
> stability?

The specific assumptions baked into v2.0 (and preserved in
v2.2):

1. The experimental flag stays gated and operational.
2. The inbox file schema (`from`, `text`, `timestamp`,
   `summary`, `read`) stays stable.
3. Anthropic's runtime's write semantics on the inbox file
   remain compatible with operon's `tmp + os.replace` (no
   anti-spoof, no signature requirement).
4. `<teammate-message>` XML formatting stays consistent enough
   for any future events.log structured-extraction work.
5. The `agent-<hash>.jsonl` + `agent-<hash>.meta.json` schema
   under `~/.claude/projects/<cwd>/<parent-session>/subagents/`
   stays stable.

Pre-flight test recommendation (carried forward as
implementation guidance, not spec content):
`tests/test_inbox_compat.py` exercises the inbox write/read
flow against an actual Agent Teams session on each CC version
bump (manual run on user's machine, not CI).

### Section 10.3 UserAlignment2 prompt

> Does this match what Boaz actually wants?

Boaz's stated preferences as of v2.0 review:

- Strong preference for claude-code-native execution to avoid
  the SDK's API-token cost path.
- Strong preference for clean code over backward-compat
  scaffolding (across the Phase 14 fix arc: patches were
  tolerated as scaffolding; the pivot's "delete the
  scaffolding" posture aligns).
- Acceptance of UX trade-off: in-process mode means cycling
  Shift+Down navigation between teammates rather than
  simultaneous split-pane viewing. User-confirmed on the basis
  of "easy navigation is the requirement, not simultaneous
  viewing."

UserAlignment2 verdict: ALIGNED.

### Section 10.4 Composability self-check (relocated)

> Does the rewritten v2.0 hold its own compositional claims?

The spec's axis decomposition asserts six axes; operon owns
three (workflow content, guardrail rules, restore) and the
runtime owns three (subagent identity schema, inbox transport,
lifecycle/spawn; plus presentation). Composability2 self-
reviewed and found:

- The workflow content axis and the inbox transport axis meet
  at the envelope construction inside `advance_phase`. Clean:
  the inbox writer is unaware of phases.
- The guardrail rules axis and the lifecycle axis meet at the
  PreToolUse hook contract. Clean for SendMessage. For the
  `Agent` tool, the same hook surface also hosts WA1's restore
  module; both handlers dispatch from the same matcher entry
  but operate on disjoint tool inputs.
- The workflow content axis and the subagent identity schema
  axis meet at the transformer. Transformer-as-seam is the
  textbook clean boundary: each axis changes independently as
  long as the transformer mediates.
- The restore module reads the runtime's sidechain storage and
  writes only one field of `tool_input`. The read side is the
  only place where operon takes a dependency on a runtime-
  internal storage layout.

Known limitation of self-attestation (per Skeptic2 SF3):
Composability self-attestation is structurally weaker than an
outside Composability review. The dual-purpose PreToolUse hook
(guardrail enforcement on SendMessage + restore-context
injection on Agent) is the highest-risk item -- conflating
enforcement and context-injection on a single hook surface is
the kind of thing that erodes over time, and "document the
dispatch clearly" is a comment-in-a-config-file kind of
defense. An independent Composability voice would be the
appropriate review surface; the project's current Leadership-
staffing constraint (one Composability slot) does not permit
one. If a future leadership rotation introduces a second
Composability voice, the dual-purpose hook is the first item to
hand to them for an independent pass.

---

## Section 11 implementation-detail handwringing (relocated)

The original last three rows of the empirical validation table
carried implementation-detail subclauses ("Spec: skip the
offending sidechain with a warning to events.log; do not abort
the hook. Implement in Step 1; test in Step 3."). Those
implementation prescriptions belong in code comments, not in a
spec table that primarily records empirical status. The spec
body now lists each row as a one-line "NOT TESTED" entry.

Full original prose for the three rows:

- **PreToolUse hook execution-time limits.** WA1's transcript
  walk reads potentially many `agent-<hash>.jsonl` files per
  `Agent` invocation. If CC enforces a hook-runtime budget,
  large transcripts could blow it. Implementation note: add a
  size budget + truncate-with-warning path; probe the limit
  empirically during the migration full-loop test.
- **Missing/corrupt `agent-<hash>.meta.json` handling.** The
  restore module joins `agent-<hash>.jsonl` to its sibling
  meta.json by filename; if a meta.json is missing, malformed,
  or has no `agentType` field, the join fails. Implementation
  note: skip the offending sidechain with a warning to
  events.log; do not abort the hook.
- **`schema_version` drift detection at parse time.** Operon's
  inbox writes include a `schema_version` field. Reader-side
  handling on version mismatch (operon reading the runtime's
  writes, or operon reading its own older writes) is
  unspecified. Implementation note: fail loudly with a
  structured error to events.log on mismatch; do not silently
  coerce.

---

## "What this doc is NOT" (relocated from spec Section 12)

Earlier drafts closed with an editorial framing section listing
what the doc was not (not a SPEC revision, not a code change,
not a deadline-driven proposal, not a menu of options). The
substantive content is covered by the spec's status section at
the top. The closing section is preserved here for completeness:

- **Not a SPEC revision.** SPEC.md describes the pre-pivot
  substrate today. The post-pivot SPEC.md is a separate
  document with its own review cycle.
- **Not a code change.** Nothing in `operon-plugin/main` is
  touched by this draft. No file under `src/` is modified. No
  `git add`, no `git commit`, no `git push`.
- **Not a deadline-driven proposal.** No date is attached to
  the migration steps. Each step is gated on the prior step's
  success criteria, not a calendar.
- **Not a menu of options.** The doc documents the single
  committed architecture. Where empirical gates surface that
  the architecture needs to change, the remediation is to
  revise this document, not to maintain parallel paths.

---

## Future opportunities (relocated from spec Appendix A)

These are deferred items that are NOT on the critical path of
the pivot. They are recorded here so that future revisions of
the spec can pick them up if user demand or experience
surfaces a need.

### Lead-side audit / observation via UserPromptSubmit

The `UserPromptSubmit` hook fires in the lead's session on
every input, including synthetic `<teammate-message>` wraps
(see the UserPromptSubmit observation above). Operon could
expose a "team traffic recorder" feature that hooks
UserPromptSubmit, parses the XML, and writes structured audit
records to `.operon/<run>/events.log` automatically. Estimated
effort: ~50 LOC + tests. Defer until post-pivot stability.

### Cross-team operations

Anthropic's team primitive is single-team. If a user wants
multiple concurrent teams (e.g., one for spec work, one for
implementation), each would be a separate
`~/.claude/teams/<team>/`. Operon could orchestrate across
teams by writing to multiple inbox files. Out of scope for
v2.

### Haiku summarization for huge WA1 transcripts

The spec's restore module uses raw concatenation of prior
sidechain transcripts. An earlier spike round tried
Haiku-summarization and found it hallucinated specifics.
Future direction: opt-in summarization with explicit caveat
in the prompt mutation that says "the following is a
Haiku-generated summary; treat as paraphrase, not transcript."
Defer until operon hits a real transcript-size pain point.

---

End of appendix.
