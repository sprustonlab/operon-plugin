# Hooks

operon registers Claude Code event handlers in
`plugins/operon-plugin/hooks/hooks.json`. The active hook is `PreToolUse`,
which does two unrelated jobs depending on the tool being called:
**guardrail enforcement** for side-effectful tools, and **spawn-prompt
injection** for the `Agent` (spawn) tool. This page owns the hook wiring,
the interpreter-resolution wrapper, the request/response contract, and the
fail-closed safety net.

## Wiring

`hooks.json` registers one matcher:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Agent|Bash|Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          { "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/pretooluse-wrapper",
            "timeout": 10 }
        ]
      }
    ]
  }
}
```

The matcher covers the spawn tool (`Agent`) and the side-effectful tools
(`Bash`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit`). Read-only tools are
not matched -- guardrails target actions with side effects.

## The wrapper and the interpreter ladder

`pretooluse-wrapper` (bash, with a `.cmd` sibling for Windows) is a thin
launcher that mirrors the [bin shim](../user-guide/install.md)'s
`uv -> python3 -> python` resolution ladder:

1. `uv run --project <plugin-root>` if `uv` is on `PATH`.
2. Otherwise the first of `python3`/`python` that passes a pre-flight
   `import mcp, watchdog, yaml`.
3. Otherwise a loud stderr error naming the exact `pip install` command,
   then **fail-open** -- emit `allow` so a broken hook environment does
   not false-block every legitimate tool call.

The wrapper always prepends `${CLAUDE_PLUGIN_ROOT}/src` to `PYTHONPATH` so
`pretooluse.py` can `import operon_mcp_server.rules` without
`pip install -e .`. Only `yaml` is strictly needed on this path, but the
dep set matches the MCP server's so one ladder covers both. The `.cmd`
sibling exists because Claude Code invokes the bare command name; keeping
the `.cmd` pair is required for the hook to launch on Windows.

## Request and response contract

Claude Code passes the hook a JSON object on stdin:

```json
{ "session_id": "...", "hook_event_name": "PreToolUse",
  "tool_name": "Bash", "tool_input": {"command": "..."} }
```

`pretooluse.py` writes a decision JSON to stdout and exits 0:

```json
{ "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow" | "deny",
    "permissionDecisionReason": "<message>" } }
```

operon emits only `allow` or `deny`, never the native `ask`: the native
prompt is bypassable by permission mode and out of operon's control, so a
`warn` becomes a `deny` carrying a hint to call `acknowledge_warning`,
keeping the loop closed to the model rather than the user. The `Agent`
branch instead emits an `updatedInput` payload (see below) and no
`permissionDecision`.

## Two branches

### Agent branch -- spawn-prompt injection

When `tool_name == "Agent"`, the hook *mutates* the spawn prompt instead
of gating it. It prepends, when each is present:

1. an `[OPERON IDENTITY] Your operon team-member name is "<name>"`
   directive (so a teammate can distinguish itself from the lead on
   subsequent MCP calls -- see [the identity
   channel](mcp-server.md#teammate-identity-the-operon_query-channel)), and
2. the teammate's prior sidechain transcripts, discovered under
   `~/.claude/projects/<cwd-mangled>/*/subagents/` and concatenated in
   mtime-ascending order, so a re-spawned teammate has first-person
   recall.

This branch **bypasses the guardrail pipeline** and never writes to the
audit log. Every failure path is fail-open: a discovery / read / decode
error returns the input unchanged so the spawn proceeds normally.

### Side-effect branch -- guardrail enforcement

For `Bash|Edit|Write|MultiEdit|NotebookEdit`, the hook runs two evaluation
passes:

1. **Fail-closed hardcoded deny set** (`_FAILCLOSED_DENY`). A small
   curated set of catastrophic-class patterns (today: `no_rm_rf`),
   evaluated with stdlib `re` only. This pass fires *even when* `rules.yaml`
   is missing, PyYAML is unavailable, identity is unresolvable, or
   `phase_state.json` is corrupt. Each entry's `rule_id` mirrors the
   corresponding entry in `plugins/operon-plugin/rules.yaml` so a single
   grep links them -- and a deliberately disabled `rules.yaml` entry cannot
   turn off the hardcoded copy. Keep this set under five entries, reserved
   for unrecoverable-class actions.
2. **Full rules engine.** Loads the merged 3-tier + workflow-embedded
   Rules, projects through `(role, current_phase)`, and runs
   `rules._evaluate`. This pass fails *open* on a rules-load error -- the
   fail-closed pass above has already cleared the catastrophic patterns.
   See [Rules and Enforcement](rules-and-enforcement.md) for the
   evaluation algorithm.

On a `deny`/`warn` decision the hook first checks for an active escape
token (override for deny, ack for warn) under the run-dir; if present it
consumes/honors it and converts the block to allow, tagging the audit row
`overridden` / `acked`. Otherwise it emits `deny` with the appropriate
request_override / acknowledge_warning hint.

## Identity resolution in the hook

The hook is a **separate subprocess** Claude Code spawns from the lead's
process; it does not inherit `OPERON_AGENT_HANDLE` from the lead's MCP
subprocess. So at startup it calls `bootstrap.discover_coordinator_handle`
(a read-only disk lookup -- it never creates a fresh run) and pins the
discovered handle in its process-local cache. This restores
`agent`/`role` enrichment on audit rows for lead-issued calls.

For teammate-issued calls, Claude Code (2.1.150+) propagates the
originating teammate's `subagent_type` as an `agent_type` field on the
hook input, present on teammate calls and absent on lead calls.
`_resolve_identity` uses that field as the role override so guardrail
Rules project onto the originating teammate rather than the lead;
`team-lead` is ignored so the bootstrap-resolved `coordinator` role wins
for the lead. `agent_name` and `current_phase` still come from the handle
file and `phase_state.json`.

!!! note "What the hook cannot do"
    A rolled-back experiment tried to reject teammate-originated calls to
    operon's identity MCP tools by comparing the hook's `session_id` to the
    lead's. It never fired: in-process Agent Teams multiplex every
    teammate MCP call through the lead's single transport, so the hook
    always sees the lead's `session_id`. The `agent_type` field above is
    the only per-call teammate signal available at the hook layer; the
    `[OPERON_QUERY]` inbox channel remains the trustworthy path for
    teammate identity.

## Fail-open as the default failure mode

Every layer fails open on its own breakage: an unparseable stdin, a
missing `tool_name`, a rules-load error, and a top-level exception all emit
`allow`. The one exception is the fail-closed deny set, which fails
*closed* by design. Claude Code treats hook errors as non-blocking anyway,
so fail-open keeps a buggy hook from bricking the user's tool calls while
the curated safety net still guards the catastrophic cases.
