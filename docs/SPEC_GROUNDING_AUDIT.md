# Spec Grounding Audit -- AGENT_TEAMS_PIVOT_PLAN.md v2.5

**Date:** 2026-05-21
**Auditor:** SpecGroundingAudit (research role, project-team workflow)
**Method:** Read-only walk of every spec claim that names a file,
hook, tool, or runtime artifact PORTED FROM claudechic; cross-checked
against claudechic source at `/groups/spruston/home/moharb/claudechic`
and operon-plugin source at `/groups/spruston/home/moharb/operon-plugin`.

---

## Verdict

**MOSTLY GROUNDED.** 4 INACCURATE / PARTIAL findings, 3 NOT
APPLICABLE (net-new operon design with no claudechic counterpart),
and several ACCURATE-but-worth-citing claims. The two issues Boaz
caught belong to a single pattern: when the spec describes the
ported audit log it under-specifies the writer set on one side
and over-specifies it on the other. No catastrophic misreadings of
claudechic infrastructure surfaced.

The compositional anchor sections (§3 inbox-write primitive, §5 WA1
restore, Agent Teams runtime contracts, Section 11 empirical table)
are mostly NET-NEW Anthropic-runtime work and are out of scope for a
claudechic-port audit -- they should be reviewed against TeamsSpike
artifacts, not claudechic source.

---

## Per-finding report

### 1. PARTIAL -- §4.3 component 8 audit-log writer list

**Spec claim (lines 352-361):**

> Audit log (operon-internal): `.operon/<run>/events.log` is written by:
> - PreToolUse hook (every rule-firing event).
> - `advance_phase` handler (every successful and failed advance).
> - `inbox.write_to_member_inbox` (every operon-injected outbound entry).
> - `inbox_reader` (every inbound entry processed: protocol dispatch,
>   relay to lead, or skip).

**claudechic implementation:**
`claudechic/guardrails/hits.py:36 HitLogger` is the audit-log writer.
It is invoked only from `claudechic/guardrails/hooks.py:134-161` (the
`evaluate()` PreToolUse closure inside `create_guardrail_hooks`). No
other call sites exist in claudechic (verified by grep on
`hit_logger.record` / `HitLogger`). The other in-process hooks --
`SessionStart` matcher=`compact` in
`claudechic/workflows/agent_folders.py:1077` and the plan-mode
PreToolUse closure in `claudechic/app.py:840` -- do not write the
audit log.

**Verdict:** PARTIAL. The PreToolUse row is consistent with
claudechic. The three new writer rows (advance_phase, inbox writer,
inbox_reader) describe net-new operon work; they have no claudechic
analogue and one of them (inbox_reader / inbox.write_to_member_inbox)
re-logs data that the inbox files already persist (Boaz's concern).

**Proposed spec edit:** Split the bullet list into two named blocks:

  *Ported writers (1 row):* `evaluate` (the PreToolUse hook handler;
  matches claudechic's HitLogger usage).

  *Net-new writers (3 rows):* `advance_phase`, `inbox.write_to_member_inbox`,
  `inbox_reader`.

For the inbox-writer and inbox_reader rows, replace "every entry" with
"writer/reader-side metadata only -- protocol type, sender, retry
count, exhaustion outcome. The inbox JSON itself is the persistent
record of message bodies; do not duplicate." This preserves the
audit surface for failure diagnostics without doubling the write
volume.

---

### 2. PARTIAL -- §4.6 "tiny `current_phase` file on disk" rationale

**Spec claim (lines 442-445):**

> | Phase state | In-memory primary + tiny `current_phase` file on
> disk | The disk file lets hooks read the current phase without
> bouncing through the MCP. |

**claudechic implementation:**
`claudechic/workflows/engine.py:170-176` keeps `_current_phase`
purely in-process; `to_session_state` (line 453) emits a serialized
dict consumed by chicsession (`<repo>/.chicsessions/<name>.json`).
The PreToolUse hook reads the phase via the in-process `get_phase`
callback (`guardrails/hooks.py:84`) -- no disk file.

Current operon (pre-pivot) writes
`<run-dir>/phase_state.json` (`paths.phase_state_file`, read by
`identity.py:269 _read_current_phase`). The PreToolUse hook is an
external `type: command` script (`hooks/pretooluse.py` via
`plugins/operon-plugin/hooks/hooks.json:9`), so it cannot reach
in-process state.

**Verdict:** PARTIAL / NOT APPLICABLE. The rationale "lets hooks
read without bouncing through the MCP" is operon-specific (it
follows from `type: command` hooks); claudechic does not need this
file because its hooks are in-process closures. The spec is
internally consistent but the rationale should cite the hook
delivery model rather than imply this is general workflow-engine
infrastructure.

**Proposed spec edit:** Append to the rationale: "Operon's
PreToolUse hook runs as a separate `type: command` script
(`plugins/operon-plugin/hooks/hooks.json`); it does not share
address space with the MCP server, so the tiny file is the
delivery surface for the current-phase value at hook time."

---

### 3. ACCURATE -- §6 DELETE row for identity.py Phase 14 fix 4/5

**Spec claim (line 835):**

> Phase 14 fix 4/5 identity freeze + drift detector in `identity.py` |
> DELETE | Singleton MCP; identity cannot drift.

**operon-plugin implementation:**
`src/operon_mcp_server/identity.py:57 _FROZEN_HANDLE`, line 66
`_DRIFT_SEEN`, line 73 `freeze_handle()`, lines 186-225 drift
detection block inside `read_env_handle()`. All four are exactly
the "Phase 14 fix 4/5" surface the spec names.

**Verdict:** ACCURATE.

---

### 4. ACCURATE -- §6 REFACTOR row for tools/spawn_agent.py

**Spec claim (line 830):**

> Strip `_spawn_subprocess` and the `OPERON_AGENT_HANDLE` env
> propagation. Keep roster row append for in-memory roster tracking.

**operon-plugin implementation:**
`tools/spawn_agent.py:603 _spawn_subprocess`, line 675 mentions
`OPERON_AGENT_HANDLE` env propagation; `roster.append_agent(...)`
at line 953 is the roster row append. Names, structure, and
purpose match.

**Verdict:** ACCURATE.

---

### 5. PARTIAL -- §4.3 component 2 module mapping for guardrail engine

**Spec claim (lines 250-253):**

> Guardrail Rules engine (PreToolUse contract): `rules.py`, `evaluate`,
> `request_override`, `acknowledge_warning`.

**claudechic implementation:**
`evaluate` lives in `claudechic/guardrails/hooks.py:67`, not
`rules.py`. `rules.py` holds the `Rule` dataclass + matchers.
Override tokens live in `guardrails/tokens.py`. `request_override`
and `acknowledge_warning` are MCP tools defined elsewhere
(`claudechic/mcp.py`), not part of the guardrails leaf module.

**operon-plugin implementation:**
`rules.py:418 _evaluate` is the pure function; `tools/evaluate.py`
is the MCP wrapper; `tools/request_override.py` and
`tools/acknowledge_warning.py` are MCP tools.

**Verdict:** PARTIAL. The conceptual cluster ("rules + evaluate +
override flow") is right; the file-level mapping
("rules.py contains all four") is loose. Reasonable for a spec, but
will read as wrong to anyone diffing against either source.

**Proposed spec edit:** Replace the file-level enumeration with a
purpose-level cluster: "Rule definitions + match/eval helpers +
warn/deny token flow." Drop the implication that all four live in a
single file.

---

### 6. INACCURATE -- §4.3 component 8 audit-log path

**Spec claim (line 352):**

> Audit log (operon-internal): `.operon/<run>/events.log` ...

**Current operon path:**
`rules.py:470 guardrail_log_path` resolves to
`<run-dir>/guardrail_log.jsonl` (verified by inspecting the function
and its callers in `tools/evaluate.py`).

**claudechic path:**
`<repo>/.claudechic/hits.jsonl` (per `app.py:1967` and the
`.gitignore` entries at `.claudechic/hits.jsonl`).

**Verdict:** INACCURATE in the sense that no codebase today writes
to `.operon/<run>/events.log`. The spec is proposing a rename
without flagging it as one.

**Proposed spec edit:** Add a parenthetical on first mention: "(renamed
from current `guardrail_log.jsonl`; rename lands as part of the
audit-log writer consolidation in §7)." Mirror the rename in the
glossary entry (line 1188).

---

### 7. NOT APPLICABLE -- runtime path claims in §3, §5, §5.3, Glossary

The following spec claims describe Anthropic-runtime artifacts and
have no claudechic counterpart; they cannot be ground-truthed
against claudechic source and must be verified against TeamsSpike
artifacts or Claude Code binary disassembly:

- `~/.claude/teams/<team>/inboxes/<member>.json` (§3.1, glossary)
- `~/.claude/teams/<team>/config.json` (§4.6 "Roster" row)
- `~/.claude/agents/<role>.md` (§4.2 subagent definition schema)
- `~/.claude/projects/<cwd-mangled>/<parent-session>/subagents/agent-<hash>.jsonl`
  (§5.1, §5.3, glossary)
- `<teammate-message>` XML envelope (§4.4, §5.3)

Section 11 already classes these as compositional anchors with
their own validation status; I confirm they are out of scope for a
claudechic-port audit.

---

### 8. ACCURATE -- §4.4 "no per-teammate OPERON_AGENT_HANDLE"

**Spec claim (lines 385-387):**

> Operon's MCP holds one identity ... Operon carries no per-teammate
> environment handle, no handle file, and no per-teammate
> `OPERON_AGENT_HANDLE`.

**claudechic equivalent:**
`Agent.agent_type` is a process-local attribute on each `Agent`
instance (multi-agent-architecture.md, `agent.py`). No env var, no
handle file. Switching agents updates `agent_type` live; the
PreToolUse hook reads it via a callable that resolves at every fire
(`app.py:1162-1171`).

**Verdict:** ACCURATE. The target state described matches claudechic's
in-process identity model.

---

## Patterns

1. **Audit-log surface drift.** Findings 1, 6, and (partially) 5 all
   touch the audit-log writer / path. The spec interleaves
   port-from-claudechic claims with proposed-rename and add-writer
   claims without flagging which is which. A single audit-log
   subsection consolidating "today's writer (1) / tomorrow's writers
   (4) / today's path / tomorrow's path" would close all three.

2. **File-level vs concept-level module mapping.** Finding 5 surfaces
   a habit of naming a single source file for a multi-file cluster
   in claudechic (e.g. `rules.py` standing in for
   `rules.py + hooks.py + tokens.py`). Acceptable for a spec, but
   flag it once globally rather than at each component.

3. **Hook-delivery rationale unstated.** Finding 2 (current_phase
   file) is one example; the rationale chain "operon hooks are
   `type: command` -> they need disk-side state -> claudechic hooks
   don't because they are in-process closures" should be stated
   once in §4.5 or §4.6 so downstream readers stop expecting a
   claudechic counterpart for every disk file.

No glossary entry that names a specific file gets the path wrong;
the glossary is tight on Anthropic-runtime paths (verified against
§3.1, §5.3, §11 schema-anchor rows).

---

## Net-new sections out of scope

Confirmed out of scope for this audit (no claudechic counterpart;
must be reviewed against TeamsSpike / Anthropic-runtime artifacts):

- §2 (Why), §3 (inbox-write primitive), §4.5 (in-process target),
  §4.7 (operon-as-team-member), §5.1 (WA1 restore), §5.2 (longevity
  directive), §5.3 (schema corrections), §6.1 (interrupt/close OOS),
  §9 (risks), §10 (standing decisions), §11 (empirical validation
  table).

The above sections rest on Anthropic-runtime behavior and the
TeamsSpike empirical record, not on claudechic infrastructure.

---

End of audit.
