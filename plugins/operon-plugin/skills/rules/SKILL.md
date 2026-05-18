---
name: rules
description: Introspect the guardrail Rules and advance-checks that apply to you right now. Use when the user asks what rules are active, what constraints apply, what acks or overrides are in flight, or to show the active escape tokens. Returns a structured listing plus a human-readable markdown rendering for the current (role, phase).
user-invocable: true
allowed-tools:
  - mcp__operon__get_applicable_rules
---

# /rules -- Introspect active constraints

This skill returns the active Rules + advance-checks + escape tokens
for the caller's `(role, current_phase)`. It is the user-facing entry
point per SPEC_APPENDIX §F (Phase 9 introspection skill).

## Behavior

Call `mcp__operon__get_applicable_rules` with NO arguments. The tool
does the projection in-process:

1. Resolves the caller's identity from `OPERON_AGENT_HANDLE` (role +
   current_phase from `phase_state.json`).
2. Loads the merged guardrail Rule set (plugin > user > project >
   workflow-embedded) and filters by `(role, phase)`.
3. Enumerates the caller's active escape tokens:
   - **acks** -- TTL-bounded (default 60s); a `seconds_remaining`
     countdown is included.
   - **overrides** -- one-shot tokens; consumed by the PreToolUse
     hook on first match.
   Expired ack tokens are lazily garbage-collected during the scan.
4. Renders a `## Constraints` markdown block with three sections:
   advance checks, active Rules, and active escape tokens.

## Argument handling

`$ARGUMENTS` is currently unused; if the user supplies an agent
name, the tool returns `cross_agent_not_implemented`. Cross-Agent
inspection (Coordinator / chain-of-trust per SPEC §7) is deferred
to a future phase.

## What to return

Relay the tool's `markdown` field verbatim as the primary response.
If `active_tokens.acks` or `active_tokens.overrides` is non-empty,
call out the count + nearest TTL in a one-line summary above the
markdown so the user can see at a glance.

If the result has an `error` key (e.g. `identity_unbound`,
`workflow_error`), relay the `reason` field verbatim and stop.

## Sample invocations

User typed `/rules` (no args): call
`mcp__operon__get_applicable_rules()` and report the markdown.

User typed `/rules my-worker`: call
`mcp__operon__get_applicable_rules(agent_name="my-worker")`. Phase 9
returns `cross_agent_not_implemented` -- relay that reason verbatim.

## Limitations (Phase 9)

- Cross-Agent inspection is stubbed. Workers + Coordinator can
  introspect their own scope only.
- Token state is a point-in-time snapshot. Acks may expire between
  the read and any subsequent gated tool call (60s TTL); overrides
  may be consumed by an unrelated hook fire in the same turn.
