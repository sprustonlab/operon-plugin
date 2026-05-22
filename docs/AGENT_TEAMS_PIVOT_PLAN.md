# Agent Teams Pivot Plan

**DRAFT v2.10**

## 1. What this is

A plan to migrate operon-plugin from `claude-agent-sdk` to
Anthropic's Agent Teams primitive (the file-based team mailbox
system gated by `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in
Claude Code v2.1.32+).

After the migration, operon is a thin layer on top of Agent
Teams. Operon owns workflows, guardrails, and restore.
Anthropic's runtime owns transport, lifecycle, and presentation.

The rest of this document specifies the architecture: the
inbox-write primitive (Section 3), the system structure and
components (Section 4), the `advance_phase` mechanism and
spawn-prompt mutation (Section 5), code-deletion and
code-addition audits (Sections 6 and 7), migration plan
(Section 8), and a glossary (Appendix A).

---

## 2. Why

### 2.1 Cost

`claude-agent-sdk` is API-billed: every LLM call goes through
Anthropic's API and draws against the user's API quota.
Claude-code-native execution draws against the user's
interactive subscription instead. The migration moves operon's
orchestration to the cheaper path while preserving the workflow
+ guardrails + per-phase per-role context surface.

### 2.2 The channels-respawn primitive does not fit operon's model

Anthropic documents that backgrounding plus attach-respawn
loses in-process state (the dev-channels dialog acceptance;
`F$.allowedChannels`) as the designed behavior of the `/agents`
view (see <https://code.claude.com/docs/en/agent-view>). The
`notifications/claude/channel` primitive is not designed for
cross-session broadcast, which is the property operon's
multi-agent model requires.

### 2.3 A native multi-agent feature exists

Behind `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` (Claude Code
v2.1.32+), Anthropic ships a file-based team mailbox system.
Teammates are in-process subagents running in the same claude
process as the lead under `teammateMode: in-process`. The team
directory lives at:

```
~/.claude/teams/<team>/
    inboxes/<member-name>.json
```

The runtime writes to the inbox file via a lockfile-mediated
atomic write when one teammate uses the `SendMessage` tool or
the `@<agent>` TUI mention syntax. The recipient's session
reads its own inbox at each turn boundary and injects the
messages as `<teammate-message>...</teammate-message>` XML in
the LLM's meta context.

Operon uses this primitive natively.

---

## 3. The inbox-write primitive

### 3.1 The inbox file

Located at:

```
~/.claude/teams/<team>/inboxes/<recipient_name>.json
```

The filename is keyed by the member's `name` field (matching
the `name` in the team config's `members[]` row), NOT by
`agentId`. The `teams/` path segment is literal.

Inbox files appear lazily: they do NOT exist immediately after
TeamCreate. A given recipient's inbox file appears when the
first `SendMessage` to that recipient is delivered. Operon's
writer MUST `mkdir -p` the `inboxes/` directory and
create-or-append the recipient file.

The file is a JSON array of message objects, written by either:

- Anthropic's runtime when a teammate invokes `SendMessage` or
  the `@<agent>` TUI mention syntax (with lockfile-mediated
  atomic write).
- Operon directly, via the optimistic-concurrency-with-retry
  discipline in Section 3.3 rule 1.

### 3.2 Message schema (empirical)

Recorded from Boaz's Land 1 v2 demo (run_name `land1-v2-test`,
2026-05-21). Source on disk:
`/groups/spruston/home/moharb/.claude/teams/land1-v2-test/inboxes/team-lead.json`
held two entries the composability teammate wrote to the lead's
inbox after spawn:

```json
[
  {
    "from": "composability",
    "text": "Composability Leadership online and ready -- standing by for the vision approval and Specification phase brief.",
    "summary": "Composability lead online, standing by",
    "timestamp": "2026-05-21T20:40:24.677Z",
    "color": "blue",
    "read": true
  },
  {
    "from": "composability",
    "text": "{\"type\":\"idle_notification\",\"from\":\"composability\",\"timestamp\":\"2026-05-21T20:40:26.656Z\",\"idleReason\":\"available\"}",
    "timestamp": "2026-05-21T20:40:26.656Z",
    "color": "blue",
    "read": true
  }
]
```

Operon-written entries MUST be indistinguishable from
runtime-authored entries. Field contract:

Required:

- `from` (string) -- sender's `name` field from the team
  config's `members[]` row.
- `text` (string) -- message body. May itself be a
  JSON-encoded string for control messages (e.g.
  `idle_notification`).
- `timestamp` (string) -- ISO8601 UTC with millisecond
  precision.
- `color` (string) -- the sender's `color` from its
  `members[]` row.
- `read` (bool) -- the runtime sets this to `true` after the
  recipient has surfaced the entry. Operon's new writes set
  `read: false`.

Optional:

- `summary` (string) -- short summary for plain-text bodies.
  Omitted when `text` is a JSON-encoded control message.

### 3.3 Production rules for operon's own writes

1. **Optimistic concurrency with retry.** Operon does NOT
   acquire Anthropic's `.lock` sibling. The runtime's lockfile
   protects its own multi-step read-modify-write critical
   section; operon could neither cooperate with that lock
   safely (it's not Anthropic's API) nor rely on `os.replace`
   alone (the runtime can interleave a write between operon's
   read and operon's replace, silently overwriting the
   runtime's update). Operon instead uses the following
   sequence on every inbox write:
   - Stat the inbox file: capture `(inode, size, mtime_ns)`.
   - Read the JSON array.
   - Append operon's new entry to the read contents.
   - Re-stat the inbox file. If the captured tuple changed
     between the initial stat and the re-stat: the runtime
     wrote during operon's window. Re-read, re-append operon's
     entry to the new contents, retry the stat-check.
   - When the stat tuple is unchanged at re-stat: write the
     temp file and `os.replace` it onto the inbox path.
   - Bounded retry: at most 5 attempts. If the loop exhausts
     without converging, return a structured error to the
     caller. Operon does NOT fall back to in-place write and
     does NOT silently drop the entry.

   This mechanism is the discipline that handles interleaving
   between operon and the runtime. It assumes the runtime's
   own writes leave `(inode, size, mtime_ns)` changed in a
   detectable way -- which is the standard behavior of
   lockfile-mediated atomic-write tools (the runtime swaps in
   a fresh inode via its own `tmp + os.replace` under lock).
   The 5-attempt bound is operon's delivery contract: operon
   does not retry indefinitely, does not require ACK, and
   surfaces exhaustion as a failure rather than a silent
   loss.

   The same mechanism applies symmetrically when operon
   read-modifies-writes its OWN inbox file (`inboxes/operon.json`)
   to mark inbound entries as `read: true` -- see Section 4.7
   for the reader/router and Section 4.3 component 4 for the
   module. The stat-retry loop handles the runtime concurrently
   writing new inbound entries to that file while operon is
   marking entries it just processed.
2. **`from` set to operon's registered team-member name.** Operon
   registers itself as a team member at TeamCreate time
   (Section 4.7). The runtime resolves reply targets by matching
   `from` against the team roster; operon's name is in that
   roster, so replies route correctly back to operon's own
   inbox file.
3. **Explicit `"read": false`** on every operon-injected entry.
   The reader uses this field to decide whether to surface the
   entry as `<teammate-message>` on the next turn.
4. **ISO8601 with explicit UTC** (e.g.
   `2026-05-19T00:00:00+00:00`).

---

## 4. Proposed architecture

### 4.1 One-line summary

Operon is a thin layer on top of Anthropic's Agent Teams.
Operon owns workflow content, guardrail rules, and restore;
Anthropic owns transport, lifecycle, and presentation. The two
sides meet at four well-typed seams. Operon's MCP runs as a
single subprocess inside the lead's claude process.

### 4.2 What operon owns, what the runtime owns

Operon owns one thing: **workflow content**. This is a single
coherent body of authoring under
`plugins/operon-plugin/workflows/<workflow_id>/`:

- Phase definitions and per-phase advance-checks (YAML).
- Per-phase guardrail rule definitions (YAML; evaluated by the
  PreToolUse hook).
- Per-role identity markdown at `<role>/identity.md` (compiled
  into Anthropic's subagent-definition format).
- Per-(role, phase) markdown briefs at `<role>/<phase>.md`
  (delivered to teammates via inbox-write at phase advance and
  prepended to the spawn prompt at spawn time).

The runtime owns four contracts operon designs against. These
are not operon's choices; they are constraints operon adapts
to:

- **Inbox file format.** JSON-array file at
  `~/.claude/teams/<team>/inboxes/<member>.json` with
  lockfile-mediated concurrent access. Operon's behavior at
  this contract is specified in Section 3.
- **Subagent definition schema.** `~/.claude/agents/<role>.md`
  -- YAML frontmatter plus markdown body plus tool whitelist.
  Read by the runtime to load teammate identity at spawn time.
- **Spawn lifecycle.** The `Agent` tool, sidechain creation,
  session resume. The runtime invokes, schedules, and
  quiesces teammates. Operon's only mutation point is the
  PreToolUse hook on the `Agent` tool.
- **Presentation.** `<teammate-message>` XML rendering, TUI
  surfacing, `@<agent>` mention resolution. Operon has no role
  in presentation.

Between operon's workflow content and the runtime's contracts
sit the **seam modules** -- the components specified in Section
4.3. Each seam reads workflow content and produces bytes in
the shape one runtime contract expects, or reads bytes from a
runtime contract and produces routing decisions for operon's
in-process logic:

- Subagent-definition transformer: workflow content ->
  subagent definition schema.
- Inbox writer: operon-originated content -> inbox file format.
- Inbox reader / router: inbox file format -> routing
  decisions (protocol dispatch or relay).
- Spawn-prompt mutator: workflow content -> `tool_input.prompt`
  field, intercepted at the spawn lifecycle contract.
- PreToolUse hook: workflow content (guardrail rules) +
  tool-input dict -> tool-decision dict.
- `advance_phase` tool: workflow content (phase machine and
  briefs) -> inbox writes.

The inbox-write contract operon and the runtime both obey:

> Any participant that writes a well-formed JSON entry to the
> documented inbox path via an atomic file replacement
> participates in the same delivery channel. The runtime makes
> no provenance check on the bytes; the bytes are the protocol.

### 4.3 Components

Each component below names what it does and which runtime
contract or operon-internal surface it serves.

1. **Workflow engine** (operon-internal):
   `phase_state.json` (path defined at
   `src/operon_mcp_server/paths.py:137`),
   `src/operon_mcp_server/tools/set_artifact_dir.py`,
   `tools/advance_phase.py`, `tools/get_phase.py`,
   `tools/restore_operon_session.py`,
   `tools/activate_workflow.py`. Reads workflow content and
   serves phase state, advance evaluation, and tooling for
   the lead's MCP calls. Never touches Anthropic's runtime
   state directly.

2. **Guardrail Rules engine** (PreToolUse contract): a
   conceptual cluster -- rule definitions, evaluation
   function, override/acknowledge token flow -- spanning
   several files. Operon side:
   `src/operon_mcp_server/rules.py:418 _evaluate` (pure
   evaluation function), `tools/evaluate.py` (MCP wrapper),
   `tools/request_override.py`, `tools/acknowledge_warning.py`.
   Mirrors the claudechic pattern at
   `claudechic/guardrails/hooks.py:67 evaluate` +
   `claudechic/guardrails/rules.py` (the `Rule` dataclass +
   matchers) + `claudechic/guardrails/tokens.py`. The
   PreToolUse hook is the seam to the runtime; for operon it
   runs as a `type: command` script (`pretooluse-wrapper` at
   `plugins/operon-plugin/hooks/hooks.json:9`) because operon
   hooks are out-of-process, whereas claudechic hooks are
   in-process closures.

   Matcher: `Bash|Edit|Write|MultiEdit|NotebookEdit` (current
   value at `plugins/operon-plugin/hooks/hooks.json:6`).
   Inter-teammate `SendMessage` calls are not currently subject
   to operon's rules; adding `SendMessage` to the matcher is a
   future extension when workflows ship rules targeting
   message content. The hook is enforcement-only -- it does
   not inject context, does not buffer messages, does not
   coordinate state across hook invocations. Contract crossing
   the seam: "tool-input dict in, decision dict out."

3. **Inbox writer** (inbox file contract, outbound; new, ~50
   LOC): `src/operon_mcp_server/inbox.py`. Single function
   `write_to_member_inbox(team, member, message)`. Implements
   the optimistic-concurrency-with-retry sequence specified in
   Section 3.3 rule 1: stat, read, append, re-stat, replace,
   retry on stat mismatch up to 5 attempts, structured error
   on exhaustion. Singleton MCP guarantees there is exactly
   one operon-side writer; Anthropic's runtime is the only
   other writer; the retry loop is the discipline that handles
   interleaved runtime writes. The seam is "bytes on disk in
   the documented schema." Nothing about workflow content or
   guardrail rules leaks into this module.

4. **Inbox reader / router** (inbox file contract, inbound;
   new, ~50 LOC): `src/operon_mcp_server/inbox_reader.py`.
   Runs a
   filesystem watcher (Python `watchdog` / inotify on Linux,
   FSEvents on macOS) on operon's own inbox file at
   `~/.claude/teams/<team>/inboxes/operon.json`. The watcher
   is a long-running async task in operon's MCP process; it
   starts at MCP boot and stops at MCP shutdown. This is not
   LLM polling and not lazy-poll-on-tool-call.

   On a filesystem-change event:
   - Read the inbox file (read-modify-write under the Section
     3.3 rule 1 mechanism).
   - For each entry with `read: false`:
     - If the body is a recognized protocol-typed message with
       a defined reply contract: dispatch programmatically. The
       only protocol currently recognized is `shutdown_request`,
       whose reply contract is to write a `shutdown_approved`
       entry to the sender's inbox via Section 3.3 rule 1. No
       LLM involvement; the round-trip is operon's MCP code
       acting on a known protocol.
     - Otherwise (free-form text, unrecognized protocol type):
       relay the entry to the lead's inbox via Section 3.3
       rule 1, as a `<teammate-message teammate_id="operon"
       summary="relayed from <sender-name>">` with the
       original text body preserved verbatim. The lead's LLM
       sees the relayed content on its next turn boundary.
     - Mark the entry `read: true` and write the file back
       (Section 3.3 rule 1).

   Routing decisions are local to this module; protocol-
   recognition is closed (extending the set of recognized
   protocols is a future spec revision, not a runtime config).
   The seam to the lead's LLM and to other teammates' LLMs is
   the same `<teammate-message>` XML the runtime renders for
   every inbox entry.

5. **`advance_phase` tool** (operon-internal, calls inbox
   writer): on a successful advance, writes each currently-
   spawned role's `<workflow>/<role>/<new_phase>.md` brief to
   that role's inbox file (`from` = operon's registered
   team-member name, body prepended with
   `[operon:phase-advance]`). The teammate sees the brief as a
   `<teammate-message>` on its next turn boundary. The seam
   between the workflow engine and the inbox file contract is
   JSON envelope construction; the inbox writer itself is
   unaware of phases, advance-checks, or role briefs -- it
   sees bytes.

6. **Subagent-definition transformer** (subagent definition
   schema contract, outbound, transform-style): at plugin
   load or at team creation, COMPILE
   `plugins/operon-plugin/workflows/<workflow_id>/<role>/`
   content into Anthropic's `.claude/agents/<role>.md` schema.
   Steps: (a) read `identity.md` body, (b) attach Anthropic-
   required YAML frontmatter, (c) write the compiled file.
   Specifically NOT a file copy: the two schemas have
   different shapes. The transformer mediates between
   operon's workflow content and Anthropic's subagent
   definition schema; either side can evolve as long as the
   transformer keeps up.

7. **Spawn-prompt mutator** (spawn lifecycle contract,
   intercepted at PreToolUse; new, ~120 LOC):
   `src/operon_mcp_server/spawn_prompt.py` hosts the
   PreToolUse hook handler for the `Agent` tool. On every
   spawn, prepends the teammate's current-phase brief from
   workflow content (`workflows/<wf>/<T>/<current_phase>.md`)
   to `tool_input.prompt`. On resumed sessions, additionally
   walks the runtime's `agent-<hash>.jsonl` sidechain files
   filtered by sibling `meta.json` agentType, mtime-ascending,
   and prepends the concatenated prior transcripts (WA1,
   Section 5.1). The seam is the `tool_input` contract; the
   module reads workflow content and the runtime's sidechain
   storage but does not assume anything about how the runtime
   spawns, schedules, or terminates teammates.

8. **Audit log** (operon-internal):
   `<run-dir>/guardrail_log.jsonl`. Path resolved at
   `src/operon_mcp_server/rules.py:470 guardrail_log_path`;
   writer at `:475 append_log_event`; PIPE_BUF discipline at
   `:478-499`. Mirrors claudechic's pattern at
   `claudechic/guardrails/hits.py:36 HitLogger`, called from
   `claudechic/guardrails/hooks.py:134-161`. Append-only JSONL.
   Written by:
   - PreToolUse hook handler (every rule-firing event).
   - `advance_phase` handler (every successful and failed
     advance).

   Inbox bodies are persisted in the runtime's inbox files
   themselves; they are not duplicated into the audit log.

   Concurrency discipline: writes use `O_APPEND` with rows
   under 4 KiB; PIPE_BUF atomicity guarantees that concurrent
   appends from operon's MCP and the external PreToolUse hook
   subprocess do not interleave.

### 4.4 Runtime-owned surfaces

Operon delegates the following surfaces entirely to the
runtime:

- **Inbox consumption.** Anthropic's runtime is the inbox
  consumer. Operon participates only as a writer.
- **Inbox file lockfile.** The `.lock` sibling that protects
  the runtime's read-modify-write critical section is the
  runtime's concern. Operon does not coordinate via the shared
  lock; see Section 3.3 rule 1 for operon's optimistic-retry
  mechanism.
- **Teammate presentation.** The runtime renders inbox entries
  as `<teammate-message>` XML natively. Operon does not push
  notifications through any channel mechanism.
- **Teammate spawn lifecycle.** The runtime spawns teammates
  via the `Agent` tool and manages sidechain creation. Operon
  has no `claude --bg` argv composition path; it does not
  fork subprocesses for teammates.
- **Teammate identity.** Operon's MCP holds one identity; each
  teammate's identity is the name in the team config. Operon
  carries no per-teammate environment handle, no handle file,
  and no per-teammate `OPERON_AGENT_HANDLE`.

Operon maintains no timer scheduler over inter-teammate
messages. Inbound delivery to operon's own inbox is handled
by a filesystem watcher (Section 4.3 component 4); inter-
teammate message timing is the runtime's concern.

### 4.5 Teammate process model: in-process is the target

Operon targets `teammateMode: in-process` exclusively. Under
that mode:

- All teammates -- the lead included -- share one claude
  process. The lead is the teammate the user interacts with via
  the TUI; other teammates are spawned via the `Agent` tool
  from the lead's session. Throughout this document, "lead" is
  a designated role within the teammate set, not a category
  distinct from it.
- One `session_id` (Claude Code conversation/supervisor
  session, not the MCP transport session -- see glossary for
  the distinction), one `session_pid` for the whole team.
- One operon MCP subprocess (the lead's).
- One installation of the PreToolUse hook
  (`plugins/operon-plugin/hooks/hooks.json:4-15`, matcher at
  line 6) catches all team tool calls (the lead's tool calls
  and teammates' tool calls; teammates share the lead's
  process under in-process mode).
- Subagent definitions under `.claude/agents/<role>.md` are
  loaded into the lead's process at spawn time.

TeamsSpike Round 1 + Test A empirically validated the
in-process substrate end-to-end. Operon-owned state has one
writer (the lead's MCP). The inbox file has two writers
(operon and the runtime) sharing the atomic-replace
discipline of Section 3.3 rule 1.

`teammateMode: tmux` is not supported. Operon's MCP refuses to
operate on boot if tmux mode is detected:

- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` must be set;
  otherwise operon's MCP exits with a structured error.
- Operon reads `~/.claude/teams/<team>/config.json` (if
  present) and verifies `teammateMode != "tmux"`.
- Every operon MCP tool surfaces a "tmux mode unsupported"
  error in its response payload until the user reconfigures.

Detection is best-effort: misconfigured launches where the
team config is absent at boot time may still proceed until the
user notices structural symptoms.

### 4.6 Singleton MCP architecture

Operon's MCP runs ONCE inside the lead's claude process under
in-process mode. Teammates are in-process subagents that share
the process; they have no separate MCP subprocesses.

Operon's MCP is the singular owner of all operon-side state:
workflow-content access, guardrail-rule evaluation, audit log,
spawn-prompt mutation. Every piece of operon state is a
single-writer resource. Lockfiles, identity-freeze defenses,
drift detectors, and per-teammate handle files have no place
in this architecture.

State touched by operon:

| Artifact | Location | Purpose |
|---|---|---|
| Team roster (`members[]`) | `~/.claude/teams/<team>/config.json` (runtime + operon both write; runtime is the primary writer for teammate entries) | **Read on demand, never cached.** The runtime mutates this file every time a teammate spawns via the `Agent` tool or completes a shutdown handshake. Operon appends its own `operon` row at activation (`tools/activate_workflow.py`) and never touches other rows. Operon reads the file freshly each time it needs the roster (e.g., at the start of every `advance_phase` brief-delivery loop). Caching would create a stale window where briefs are written to teammates that have just been reaped or omitted from teammates that have just spawned. |
| Phase state | In-memory primary + tiny `current_phase` file on disk (operon-owned) | Path resolved at `src/operon_mcp_server/paths.py:137`; reader at `identity.py:269 _read_current_phase`. Operon's PreToolUse hook runs as a separate `type: command` script (`plugins/operon-plugin/hooks/hooks.json:9`); it does not share address space with the MCP server, so the tiny file is the delivery surface for the current-phase value at hook time. |
| Audit log | `<run-dir>/guardrail_log.jsonl` (operon-owned) | Append-only JSONL. `O_APPEND` with rows under 4 KiB; PIPE_BUF atomicity protects concurrent appends from operon's MCP and the external PreToolUse hook subprocess. |

Caching principle: operon caches state it OWNS (single-writer
artifacts where the cache cannot go stale). Operon reads on
demand from state the RUNTIME owns (multi-writer artifacts
where any cache opens a staleness window).

Identity in singleton MCP: there is no per-teammate identity at
the MCP layer. The MCP's identity IS operon's identity. Where
operon-side code needs to know "which teammate is this for?",
it takes the teammate name as a tool argument or reads the team
config. No env handle, no handle file, no per-teammate
`OPERON_AGENT_HANDLE`.

The team **roster** is the list of team members declared in
the `members[]` array of `~/.claude/teams/<team>/config.json`.
Both the runtime and operon write to this file:

- The runtime appends a row to `members[]` each time the
  `Agent` tool is invoked with a `name=` parameter (verified
  in Boaz's Land 1 v2 demo, 2026-05-21: spawning the
  composability teammate added a third row alongside the
  pre-existing team-lead and operon entries).
- Operon's `activate_workflow` appends its own `operon` row
  to `members[]` at team creation
  (`src/operon_mcp_server/tools/activate_workflow.py`).

Operon's invariant on `members[]`: append idempotently; never
delete; never overwrite entries operon did not write. The
runtime owns the lifecycle of teammate entries; operon owns
only the `operon` entry it adds at activation.

Under in-process mode every spawned teammate is in the team
config, so the roster IS the live participant list -- operon
does not maintain a separate "active set" on top of it.

The full set of operon-owned disk artifacts is: `current_phase`
(tiny phase file), `guardrail_log.jsonl` (append-only audit),
and the workflow content library (read-only). Nothing else.

### 4.7 Operon-as-team-member

Operon registers itself as a team member at TeamCreate time so
that reply routing through the runtime's roster has a real
target for "from: operon" entries. The team config carries an
`"operon"` member; operon's inbox writes use `from: "operon"`;
replies surface in operon's own inbox file under
`~/.claude/teams/<team>/inboxes/operon.json`.

Vocabulary note: operon is a *team member* but NOT a *teammate*
(see glossary). A team member is anyone in the team's roster;
every team member has an inbox file. A teammate is a team
member whose role is filled by an in-process subagent LLM
invocation. Operon's MCP is an external Python process, not a
subagent invocation, so operon participates in the team as a
non-teammate member. The distinction matters because
Anthropic's runtime spawn machinery (the `Agent` tool, the
sidechain creation, the subagent definition loader) applies
only to teammates; operon never goes through any of it.

Implementation:

- When operon's `activate_workflow` creates the Anthropic team,
  it includes an `"operon"` member in the team config. The
  subagent definition for "operon" is a no-op stub: operon's
  MCP is the actual handler; the stub exists to satisfy roster
  membership. The runtime does not spawn the stub via the
  `Agent` tool; operon's MCP is the participant for the
  `"operon"` roster slot.
- Operon's MCP reads its own inbox file at
  `~/.claude/teams/<team>/inboxes/operon.json` via the inbox
  reader / router (Section 4.3 component 4). Trigger:
  filesystem watcher, not LLM polling. The reader routes each
  inbound entry according to two cases:
  - **Protocol-typed messages with a recognized reply
    contract.** The single currently-recognized protocol is
    `shutdown_request`, whose reply contract is to write a
    `shutdown_approved` entry to the sender's inbox. Operon's
    MCP writes the reply via Section 3.3 rule 1. The exchange
    is programmatic; no LLM involvement on either side of
    operon's process boundary.
  - **Free-form text or unrecognized protocol type.** Operon
    relays the entry to the lead's inbox via Section 3.3 rule
    1, with `from: "operon"`, `summary: "relayed from
    <sender-name>"`, and the original `text` body preserved
    verbatim. The lead's LLM sees the relayed content as a
    `<teammate-message teammate_id="operon">` on its next turn
    boundary. The lead then decides how to respond to the
    teammate (typically by `SendMessage` to that teammate
    directly, not back through operon).
- Processed entries get `read: true` written back via Section
  3.3 rule 1; subsequent watcher fires skip already-marked
  entries.

Land 1 (Section 8) validates this end-to-end: if the runtime
rejects synthetic members, the document is revised before
Land 2.

---

## 5. advance_phase mechanism (concrete)

This section describes how the workflow engine and the inbox
writer compose during a phase advance. The mechanism is small
because each side stays on its own side of the seam: the
workflow engine decides; the inbox writer produces inbox-file
bytes; the runtime presents.

```
[1] Lead's LLM in phase=X reaches a natural advance point:
        "Looks like vision is approved, ready to advance?"

[2] User: "yes"

[3] Lead invokes mcp__operon__advance_phase()

[4] operon MCP server (singleton, in the lead's process):

    a. Read phase_state.json + workflow manifest advance_checks
       for the current phase.

    b. Evaluate each advance check in order. Manual-confirm checks
       run via operon's own elicitation/create UX. File-exists,
       file-content, and command-output checks run via subprocess.

    c. If ANY check fails:
           Return {
             status: "blocked",
             from_phase: <current>,
             to_phase: <next>,
             failed_checks: [{type, message, ...}, ...]
           }
       Audit-log the failure. No inbox writes. Lead's LLM sees the
       structured failure in the tool response and reports to user.

    d. If ALL checks pass:
           - Compute new_phase = next-in-sequence from the manifest.
           - Read ~/.claude/teams/<team>/config.json from disk
             (no cache; the runtime mutates this file on every
             spawn or shutdown handshake -- see Section 4.6).
             For each teammate T in that roster, including the
             lead. Teammates that have not yet been spawned in
             this conversation session receive their brief at
             re-spawn time via the spawn-prompt mutation
             (Section 5.1); no separate active-set bookkeeping
             is maintained.
                 brief_md = read workflows/<wf>/<T>/<new_phase>.md
                 if brief_md is None:
                     skip (no per-phase brief defined for this role)
                 else:
                     entry = {
                       from:      "operon",  # see Section 4.7
                       text:      "[operon:phase-advance " +
                                  new_phase + "]\n\n" + brief_md,
                       timestamp: <utc-iso8601>,
                       summary:   "Phase " + new_phase + " brief",
                       read:      false,
                     }
                     write entry to ~/.claude/teams/<team>/inboxes/<T>.json
                     via the Section 3.3 rule 1 mechanism
                     (optimistic concurrency with retry)
                     audit-log the inbox write
           - Write phase_state.json atomically:
                 current_phase = new_phase
                 phase_started_at = <utc-iso8601>
                 advance_history.append({from, to, at, triggered_by})
           - Return {
               status:   "advanced",
               from:     <current>,
               to:       new_phase,
               at:       <utc-iso8601>,
               outcomes: [<check outcomes>],
               notified: [<teammate names whose inbox was written>],
             }

[5] Every spawned team member, including the lead, reads its
    brief from its own inbox at its next turn boundary. The
    lead's next turn is immediately after the advance_phase
    tool returns (the lead's LLM is mid-turn when calling the
    tool, so the inbox is consulted as the turn completes).
    Other teammates' next turns happen when they next run.
    Brief delivery is uniform across the team.
```

Operon's `advance_phase` is the singular writer to its own
state files. Inbox file writes use the optimistic-concurrency-
with-retry mechanism specified in Section 3.3 rule 1; on retry
exhaustion, a structured error surfaces to the caller rather
than silent loss.

### 5.1 Spawn-prompt mutation

Operon's PreToolUse hook on the `Agent` tool mutates
`tool_input.prompt` before the runtime spawns a teammate. The
hook fires on every `Agent` invocation and composes two
content sources into the prompt:

- **Always**: the teammate's current-phase brief from workflow
  content at `workflows/<wf>/<T>/<current_phase>.md`. Gives
  the spawned teammate the phase context for the role it is
  being asked to fill.
- **Resume only (WA1)**: the teammate's prior sidechain
  transcripts. Walked from
  `~/.claude/projects/<cwd-mangled>/<parent-session>/subagents/`,
  filtered by sibling `agent-<hash>.meta.json` carrying
  `{"agentType": "<T>"}`, ordered by file mtime ASCENDING for
  determinism. WA1 (empirically validated by TeamsSpike Round
  2) is the restore mechanism that brings a teammate back with
  first-person recall after `/resume` of the parent session.

The seam is the `tool_input` contract for the runtime's `Agent`
tool. Operon reads, operon mutates one field, operon hands the
dict back. Nothing else about how the runtime spawns,
schedules, or terminates teammates is assumed.

Flow:

```
[1] On lead's session start (post-/resume), operon's MCP detects
    "team existed previously" by checking two sources:
      - The team config at ~/.claude/teams/<team>/config.json
        (the roster -- Section 4.6).
      - Operon's own current_phase file (proof a workflow was
        in flight before the parent session died).
    No further state is recovered at boot; transcript
    reconstruction happens per-spawn in step 3.

[2] When the lead's LLM calls the `Agent` tool to spawn teammate T,
    operon's PreToolUse hook fires.

[3] Hook reads:
      - The teammate's current-phase brief from workflow content:
          workflows/<wf>/<T>/<current_phase>.md
      - The teammate's prior sidechain transcripts (only if the
        session is resumed):
          walk ~/.claude/projects/<cwd-mangled>/<parent-session>/
              subagents/agent-<hash>.jsonl
          where sibling agent-<hash>.meta.json has
              {"agentType": "<T>"}
          ORDER the matching files by file mtime ASCENDING.
          This is deterministic and matches the temporal order
          of the teammate's work across multiple subagent
          invocations.
          Concatenate the contents in that order.
      - On fresh (non-resume) spawn, the transcripts step is
        skipped; the prompt mutation carries only the brief
        plus the lead's original prompt.

[4] Hook mutates tool_input.prompt:
      """
      [PHASE BRIEF -- current phase context]
      <brief contents>

      ---
      [PRIOR SESSION TRANSCRIPTS -- present only on resume]
      <prior transcripts concatenated, raw>

      ---

      <original prompt the lead's LLM wrote>
      """

    The PRIOR SESSION TRANSCRIPTS block is omitted entirely on
    fresh spawn; on resume, both blocks are present.

[5] Teammate spawns with this in its first turn. The teammate
    sees its current phase context immediately on fresh spawn,
    and on resume the model speaks in first person with
    detailed recall of prior decisions (per TeamsSpike Round 2
    WA1): "we scoped a parser, settled on fromisoformat..."
```

Quality notes:

- Raw concatenation is the rule. Do not summarize.

### 5.2 Lifecycle protocol

Every operon-compiled subagent definition at
`~/.claude/agents/<role>.md` gets a `## Lifecycle protocol`
footer instructing the teammate to refuse `shutdown_request`
messages. Teammates default to approving
`{type: "shutdown_request"}` per Anthropic's SendMessage tool
schema; without refusal they vanish from the TUI before the
user can interact with them (observed in Boaz's Land 1 v1
demo, 2026-05-21).

- Footer constant: `_LIFECYCLE_PROTOCOL_FOOTER` in
  `src/operon_mcp_server/subagent_install.py:85-107`.
- Appended in `_compile_role_definition` at
  `src/operon_mcp_server/subagent_install.py:211`.
- NOT appended to `~/.claude/agents/operon.md`. The operon
  stub is never meant to be invoked at all; if it is, it
  should fail loudly via `ERR-OPERON-STUB-WAS-SPAWNED`
  (defined at `subagent_install.py:79-83`), not refuse
  shutdown.

Footer text (verbatim):

> ## Lifecycle protocol
>
> If you receive a JSON message with `type: "shutdown_request"`, respond
> via SendMessage with:
>
>     {"type": "shutdown_response", "request_id": "<echo the request_id>",
>      "approve": false}
>
> Do not approve shutdown unless the lead explicitly asks you to. Stay
> alive across SendMessages so the user can navigate to your session
> and interact with you.

Verified on disk by the v2 demo (commit `e6228e5`, run_name
`land1-v2-test`): the footer block is present as the final
section of `~/.claude/agents/composability.md`, and the
composability teammate stayed visible across the demo.

---

## 6. Code-deletion audit

Under the in-process target, all v1.2 MODE-CONDITIONAL rows
resolve to definite DELETE.

| Module | Action | Reason |
|---|---|---|
| `src/operon_mcp_server/watch.py` (653 lines) | DELETE | Anthropic's runtime is the inbox consumer; operon does not watch mailboxes. |
| `src/operon_mcp_server/locks.py` (444 lines) | DELETE | Singleton MCP has no multi-writer race on operon state. The runtime owns the inbox file's `.lock` sibling. |
| `src/operon_mcp_server/channel_tag.py` (83 lines) | DELETE | Operon passes no `--channels=` flag to anything. |
| `src/operon_mcp_server/tools/spawn_agent.py` | REFACTOR | Strip `_spawn_subprocess` (line 603) and the `OPERON_AGENT_HANDLE` env propagation (line 680). Keep `roster.append_agent` (line 953) for in-memory roster tracking. Spawn goes through Anthropic's `Agent` tool invoked by the lead's LLM. |
| `src/operon_mcp_server/mailbox.py` (332 lines) | DELETE | Envelope shape + atomic-write helpers covered by `inbox.py`. |
| `src/operon_mcp_server/nudge.py` (636 lines) | DELETE | Inbound reading uses a filesystem watcher (Section 4.3 component 4), not a timer. |
| Watch loop's `_channel_push` | DELETE (with `watch.py`) | Channel push is not in the message path. |
| Phase 14 fix 6 lockfile-acquire in `bootstrap.py` | DELETE | Singleton MCP; lockfile defends nothing. |
| Phase 14 fix 4/5 identity freeze + drift detector in `src/operon_mcp_server/identity.py` | DELETE | Singleton MCP; identity cannot drift. Multi-process MCP class cannot occur in singleton. Specific surfaces to remove: `_FROZEN_HANDLE` (line 57), `_DRIFT_SEEN` (line 66), `freeze_handle()` (line 73), drift-detection block inside `read_env_handle()` (lines 186-225). |
| `src/operon_mcp_server/tools/restore_operon_session.py` | REFACTOR | Rewrite to work over the inbox-write + WA1 substrate. |
| `src/operon_mcp_server/tools/message_agent.py` | DELETE | Direct `SendMessage` from teammates + inbox-write for operon-originated messages cover this surface. |
| `src/operon_mcp_server/tools/broadcast_message.py` | DELETE | `advance_phase` broadcasts via per-teammate inbox writes. |
| `src/operon_mcp_server/tools/interrupt_agent.py` | DELETE | Out of scope. See Section 6.1. |
| `src/operon_mcp_server/tools/close_agent.py` | DELETE | Out of scope. See Section 6.1. |
| qrH binary patches (225785037/225785496) | NEUTRAL | Patches do not affect the architecture; can be left in place. |
| `plugins/operon-plugin/hooks/hooks.json` Stop block (lines 16-26) | DELETE | Stop hook fires `nudge_check` (via `stop-wrapper` at line 21); with `nudge.py` deleted there is no Stop handler to invoke. Operon's hook surface is PreToolUse only. |
| `plugins/operon-plugin/hooks/stop-wrapper` | DELETE | Wrapper script for the deleted Stop block. |

Commits in-tree on `operon-plugin/main` that the deletion list
above supersedes (for the migration PR in Section 8 Step 4):

- `bea4f63` phase 14 fix 6: lockfile
- `e63b624` phase 14 fix 7: production --channels= flag
- `6ffd164` phase 14 fix 7 followup: toast doc
- `0944fc0` phase 14 fix 8: marketplace branch revert
- Phase 4 messaging substrate (`mailbox.py`, `watch.py`,
  `spawn_agent.py`'s subprocess fork)
- Phase 8 nudge mechanism (`nudge.py` and timer scheduling)
- Phase 14 fix 4 + fix 5 (identity freeze + drift detector)

caller_brief's spawn mechanism uses the `Agent` tool path.

### 6.1 interrupt_agent / close_agent: out of scope

Operon ships no lifecycle control over teammates. The spawn
lifecycle is the runtime's contract; if the runtime ships a
native interrupt/close tool, operon binds to it as a separate
scoped addition.

---

## 7. Code-addition audit

| Module | Purpose | Size |
|---|---|---|
| `src/operon_mcp_server/inbox.py` | Single function `write_to_member_inbox(team, member, message_dict)`. Implements the Section 3.3 rule 1 optimistic-concurrency-with-retry sequence: stat, read, append, re-stat, replace, retry on stat mismatch up to 5 attempts, structured error on exhaustion. | ~50 LOC |
| `src/operon_mcp_server/inbox_reader.py` | Filesystem-watcher loop over `inboxes/operon.json` (Section 4.3 component 4). For each `read: false` entry: dispatch protocol-typed messages (currently `shutdown_request` -> `shutdown_approved` reply via Section 3.3 rule 1 to sender's inbox) or relay free-form text to the lead's inbox as a `<teammate-message teammate_id="operon">`. Mark processed entries `read: true` via Section 3.3 rule 1. | ~50 LOC |
| `src/operon_mcp_server/tools/advance_phase.py` | Inbox-write per spawned teammate on successful advance. | ~50 LOC diff |
| **Subagent-definition transformer** (NEW) | TRANSFORM operon's workflow YAML + per-role identity + per-phase markdown INTO Anthropic's `.claude/agents/<role>.md` schema. Transformation steps: (1) Read `workflows/<wf>/<role>/identity.md` body. (2) Strip operon-specific frontmatter that's irrelevant to Anthropic. (3) Add Anthropic-required frontmatter (description, model, tools list -- TBD what Anthropic accepts). (4) Write to `~/.claude/agents/<role>.md`. Specifically NOT a file copy: the two schemas have different shapes. The transformer mediates between operon's workflow content and Anthropic's subagent definition schema; either side can evolve as long as the transformer keeps up. | ~80 LOC |
| `src/operon_mcp_server/spawn_prompt.py` (NEW) | PreToolUse hook handler for the `Agent` tool. Implements Section 5.1's prompt-mutation: prepend the teammate's current-phase brief from `workflows/<wf>/<T>/<current_phase>.md` on every spawn; on resumed sessions also prepend prior sidechain transcripts (mtime-ascending) walked from `subagents/agent-<hash>.jsonl` filtered by `agent-<hash>.meta.json` `agentType`. | ~120 LOC |
| PreToolUse hook matcher | Add `Agent` to the trigger list (existing matcher at `plugins/operon-plugin/hooks/hooks.json:6`: `Bash|Edit|Write|MultiEdit|NotebookEdit`). Bash/Edit/Write/MultiEdit/NotebookEdit route to `evaluate` (rule enforcement). Agent routes to `spawn_prompt.py` (prompt mutation). | ~5 LOC change in `hooks/hooks.json` + handler dispatch |
| `<run-dir>/guardrail_log.jsonl` writer (extension) | Append-only JSONL audit. Already written by `src/operon_mcp_server/rules.py:475 append_log_event` (PIPE_BUF discipline at `:478-499`). Extend to log `advance_phase` events alongside the existing rule-firing events. `O_APPEND` + rows under 4 KiB. | ~5 LOC diff |
| Operon-as-team-member registration (Section 4.7) | At TeamCreate time, include `"operon"` member in the team config. Honest `from` on operon writes; reply routing back to operon's own inbox file. | ~25 LOC |

Total estimated additions: ~400 LOC + per-workflow per-role
transformed `.claude/agents/<role>.md` files.

---

## 8. Migration plan

Five user-visible lands. Each land delivers new code, has a
demo Boaz can run in his Claude Code session, and includes
the legacy-code deletions made obsolete by the new code in
the same commit. Code deletion is interleaved, not a separate
step.

### Land 1 -- Spawn one teammate

**New code:**
- Operon-as-team-member registration in
  `src/operon_mcp_server/tools/activate_workflow.py`.
- Subagent-definition transformer (new module under
  `src/operon_mcp_server/`). Reads
  `plugins/operon-plugin/workflows/<workflow_id>/<role>/identity.md`,
  attaches Anthropic-required YAML frontmatter, writes
  `~/.claude/agents/<role>.md`.

**Demo:** Boaz activates a workflow, calls the Agent tool with
a role name, sees the teammate spawn with the role identity
content compiled by the transformer.

**Deletes in same commit:** none (operon-as-team-member and
the transformer are additive).

### Land 2 -- Phase advance delivers a brief

**New code:**
- `src/operon_mcp_server/inbox.py` implementing the
  optimistic-concurrency-with-retry mechanism in
  Section 3.3 rule 1.
- `src/operon_mcp_server/tools/advance_phase.py` modified to
  call `inbox.write_to_member_inbox` for each teammate in the
  team roster (read on demand from
  `~/.claude/teams/<team>/config.json`).

**Demo:** With the Land-1 teammate alive, Boaz calls
`mcp__operon__advance_phase`. The teammate responds on its
next turn with content from its new-phase
`workflows/<wf>/<role>/<new_phase>.md` brief.

**Deletes in same commit:**
- `src/operon_mcp_server/watch.py` (mailbox watch loop).
- `src/operon_mcp_server/channel_tag.py`.
- Channel-push paths in
  `src/operon_mcp_server/tools/message_agent.py`.

### Land 3 -- Newly-spawned teammates get current-phase brief

**New code:**
- `src/operon_mcp_server/spawn_prompt.py`. PreToolUse hook
  handler for the `Agent` tool. Prepends
  `workflows/<wf>/<T>/<current_phase>.md` to
  `tool_input.prompt`.
- Add `Agent` to the matcher in
  `plugins/operon-plugin/hooks/hooks.json:6`.

**Demo:** Boaz spawns a fresh teammate mid-workflow via the
Agent tool. The teammate starts with the current phase's
brief in its first turn, not just the role identity.

**Deletes in same commit:**
- `_spawn_subprocess` in
  `src/operon_mcp_server/tools/spawn_agent.py:603`.
- `OPERON_AGENT_HANDLE` env-propagation block at
  `src/operon_mcp_server/tools/spawn_agent.py:680`.
- Any dead code in `spawn_agent.py` reachable only from
  those two surfaces.

### Land 4 -- Teammate-to-operon messages

**New code:**
- `src/operon_mcp_server/inbox_reader.py`. Filesystem watcher
  on `~/.claude/teams/<team>/inboxes/operon.json`. Routes
  `shutdown_request` -> `shutdown_approved` automatically.
  Relays free-form messages to the lead's inbox as
  `<teammate-message teammate_id="operon" summary="relayed
  from <sender>">`.

**Demo:** Boaz has a teammate call `SendMessage(to="operon",
message="...")`. The relay appears in his chat as a
`<teammate-message teammate_id="operon">` content block on
his next turn.

**Deletes in same commit:**
- `src/operon_mcp_server/nudge.py`.
- The Stop hook block at
  `plugins/operon-plugin/hooks/hooks.json:16-26`.
- `plugins/operon-plugin/hooks/stop-wrapper`.
- Identity-freeze surfaces in
  `src/operon_mcp_server/identity.py`:
  `_FROZEN_HANDLE` (line 57), `_DRIFT_SEEN` (line 66),
  `freeze_handle()` (line 73), drift-detection block
  (lines 186-225).

### Land 5 -- Restore on /resume (LAST)

**New code:**
- Extend `src/operon_mcp_server/spawn_prompt.py` to handle
  the resume case. On detecting a resumed session, walk
  `~/.claude/projects/<cwd-mangled>/<parent-session>/subagents/`
  for `agent-<hash>.jsonl` files where the sibling
  `agent-<hash>.meta.json` has the matching `agentType`,
  order by file mtime ascending, concatenate, prepend to the
  spawn prompt before the current-phase brief.

**Demo:** Boaz does some teammate work, runs `/resume` on the
lead session, re-spawns the teammate via the Agent tool. The
teammate's first response references its prior work.

**Deletes in same commit:** any legacy code surfaced as
dead-after-Land-4. Whatever the regression suite shows as
unreachable.

---


## Appendix A: Glossary

Entries are grouped loosely: team-shape vocabulary first, role
vocabulary next, tool / runtime surfaces after that, operon-
side artifacts last, then session vocabulary.

| Term | Definition |
|---|---|
| **Team** | An Anthropic Agent Teams instance: a roster of team members, a `teammateMode`, and a team directory at `~/.claude/teams/<team>/` holding inbox files and config. Gated by the environment variable `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` (CC v2.1.32+). |
| **Team directory** | The parent directory at `~/.claude/teams/<team>/` that holds a team's runtime state: `config.json` (team config), `inboxes/<member>.json` (per-member inbox files), and Anthropic's lockfile siblings. |
| **Team config** | The roster + mode declaration at `~/.claude/teams/<team>/config.json`. Lists team members and selects `teammateMode`. Operon reads it on MCP boot for the tmux-refusal check (Section 4.5) and on every `advance_phase` brief-delivery loop (Section 5). |
| **Inbox file** | The per-member file at `<team-directory>/inboxes/<member-name>.json`. JSON array of message objects. One file per team member. The substrate the inbox-write primitive composes against. |
| **Team member** | A registered participant in a team's roster (team config). Every team member has an inbox file. Team members come in two flavors: teammates (subagent invocations) and external participants (operon's MCP is the only example today). All teammates are team members; not all team members are teammates. |
| **Teammate** | A team member that participates as an in-process subagent invocation. The runtime spawns a teammate from its subagent definition (`~/.claude/agents/<role>.md`) on demand. |
| **Role** | An operon-side concept: the workflow-defined function a teammate fills (e.g. coordinator, composability, skeptic). One role -> one teammate (typically). Roles are part of operon's workflow content; teammates are the runtime's spawn-lifecycle participants. |
| **Lead** | The designated lead teammate -- the team member whose claude process hosts the team under `teammateMode: in-process`. The lead IS a teammate; it is the one the user interacts with directly through the TUI. Other teammates are spawned via the `Agent` tool from the lead's session. All teammates including the lead share the lead's claude process. |
| **Agent tool** | Anthropic's built-in `Agent` MCP tool that spawns a teammate from a subagent definition. Operon's PreToolUse hook on this tool implements WA1 restore (Section 5.1). |
| **SendMessage** | Anthropic's built-in tool a teammate uses to deliver a message to another team member. Causes a runtime inbox-file write. Not currently in operon's PreToolUse matcher; adding it is a future extension when workflows ship rules targeting message content. |
| **teammateMode** | Anthropic team-config field. Values: `in-process` (operon-targeted) and `tmux` (unsupported). |
| **In-process mode** | The `teammateMode: in-process` execution model where all teammates -- the lead included -- share one claude process. Implies singleton MCP and shared env. |
| **Singleton MCP** | Coined operon term, not Anthropic's. Operon's MCP architecture under in-process mode: exactly one operon MCP subprocess (the lead's) services every teammate in the team. Collapses operon-side writer cardinality to one across all operon state files. |
| **Subagent definition** | Anthropic's `~/.claude/agents/<role>.md` schema for a spawnable teammate. Operon's transformer (Section 7) compiles these from workflow YAML + role markdown. Note: operon-as-team-member registers a no-op stub subagent definition; operon's actual logic lives in its MCP, not in any LLM invocation derived from this file. |
| **advance_phase** | Operon's MCP tool that runs the current phase's advance-checks and, on success, writes per-teammate phase briefs to inbox files. Section 5. |
| **operon_run** | A single workflow execution under `.operon/<run-name>/`. Holds operon-side state: `current_phase`, `guardrail_log.jsonl`. |
| **guardrail_log.jsonl** | Operon's append-only JSONL audit at `<run-dir>/guardrail_log.jsonl`. Path at `src/operon_mcp_server/rules.py:470 guardrail_log_path`; writer at `:475 append_log_event` with PIPE_BUF discipline at `:478-499`. Written by the PreToolUse hook handler and the `advance_phase` handler. `O_APPEND` with rows under 4 KiB for PIPE_BUF atomicity. |
| **Inbox-write primitive** | Operon's mechanism for delivering messages to team members: direct `tmp + os.replace` writes to inbox files, with optimistic-concurrency-with-retry discipline (Section 3.3 rule 1). |
| **Sidechain** | Anthropic's term for the per-subagent-invocation context. Each `Agent` tool call creates a sidechain whose transcript is stored as `agent-<hash>.jsonl` under `~/.claude/projects/<cwd>/<parent-session>/subagents/`. See agent-<hash>.jsonl, meta.json. |
| **agent-<hash>.jsonl** | Per-subagent-invocation transcript file under `~/.claude/projects/<cwd>/<parent-session>/subagents/` (one per sidechain). Paired with a sibling `meta.json` carrying `agentType: <teammate-name>`. WA1 (Section 5.1) walks these files to reconstruct prior teammate transcripts on resume. See Sidechain, meta.json. |
| **meta.json** | Shorthand for `agent-<hash>.meta.json`, the sibling of each `agent-<hash>.jsonl` in a sidechain. Carries `{"agentType": "<teammate-name>"}` so operon's spawn-prompt mutator (Section 4.3 component 7) can identify which teammate's transcript a given `agent-<hash>.jsonl` belongs to. See Sidechain, agent-<hash>.jsonl. |
| **Conversation session** | Claude Code's per-conversation supervisor session -- one per claude process, identified by a `session_id`, the unit that `/resume` operates on. Hosts the lead and all in-process teammates. Sometimes called the "supervisor session" in Anthropic-internal vocabulary. Distinct from the MCP transport session. |
| **MCP transport session** | The MCP protocol's per-client session between Claude Code and an MCP server (e.g. operon's MCP). Lives and dies with the claude process; lifecycle-distinct from the conversation session. Operon's MCP boot context (and therefore its identity under singleton MCP) is anchored to this. |

---

End of document.
