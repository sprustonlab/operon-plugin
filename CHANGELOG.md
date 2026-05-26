# Changelog

Phase-by-phase implementation log for operon-plugin. Format: each
entry lists the major capability landed in that phase plus the
commits on `main` that make it up. The authoritative design reference
is the Contributor/Architecture Guide in the docs site
(`docs/dev/architecture.md`).

## 0.0.3 -- `/project_team` Python resolution on Windows

- `/project_team` no longer shells out to bare `python`, which on Windows
  resolves to the Microsoft Store "App Execution Alias" stub ("Python was
  not found...") and never runs the activation script. The skill now
  invokes a new `skills/activate/scripts/activate-wrapper` (paired bash +
  `.cmd`) that resolves a working interpreter the same way the MCP server
  and hooks do: try `python3` then `python` with a pre-flight `-c ""`
  check that skips the stub, then fall back to `uv run --no-project
  python`. `activate.py` is stdlib-only, so unlike the dep-needing
  wrappers this one prefers a bare interpreter and only uses uv as a last
  resort.
- `allowed-tools` for `/project_team` changed from `Bash(python *)` to
  `Bash(*activate-wrapper*)` to match the new invocation.

## 0.0.2 -- self-contained plugin packaging + Windows cwd-mangle fix

- The `operon_mcp_server` package and its runtime `pyproject.toml` now
  live INSIDE `plugins/operon-plugin/` so a marketplace install is
  self-contained. Previously they sat at the repo root, outside
  `${CLAUDE_PLUGIN_ROOT}`, so the MCP server could not import its own
  package after `claude plugin install` and failed to start. The repo
  root is now a virtual uv-workspace holding only the dev/test toolchain.
- `hooks/pretooluse.py` and `tools/restore_operon_session.py` cwd-mangle
  helpers now replace `\`, `/`, and `:` (was `/` only), so Claude Code
  sidechain-transcript discovery resolves on Windows. Byte-identical on
  POSIX.
- Docs updated to the nested layout; the dev pip path is now
  `pip install -e plugins/operon-plugin`.

## Phase 10 -- polish + e2e smoke + B-tier cleanup

- B1: `subprocess.run(["claude", "stop", ...])` in `workflow.py` and
  `watch.py` now passes `encoding="utf-8", errors="replace"` for
  Windows-safe non-ASCII handling.
- B7: `rules.append_log_event` and `broadcast_message._append_result_row`
  fall back to a minimal placeholder row if a payload still exceeds
  4 KiB after the known-fat-field truncation pass (SPEC §6.6
  PIPE_BUF defense-in-depth).
- B9: `pyproject.toml` dependencies pin upper-major bounds:
  `mcp>=1.0,<2`, `watchdog>=4.0,<7`, `PyYAML>=6.0,<7`.
- `scripts/smoke_e2e.py`: in-process e2e covering Phases 3-9 +
  the B7 truncation guard. 20/20 PASS in <10 s.
- README expanded with quick-start, MCP tool table, slash-commands,
  hooks, env vars, cross-platform notes, manual Windows procedure.
- CHANGELOG (this file).

## Phase 9 -- /rules introspection skill (09511bc)

- New `skills/rules/SKILL.md` (script-injection pattern, mirrors
  `/restore`): introspect active Rules + advance-checks + escape
  tokens for the caller's (role, phase).
- `get_applicable_rules` extended with `active_tokens` payload
  (acks with `seconds_remaining` countdown; overrides with
  `one_shot` annotation) and an `### Active escape tokens` section
  in the markdown rendering.
- `rules.list_active_tokens(agent_handle)` helper: scans
  `acks/<handle>/` and `overrides/<handle>/`, lazy-GCs expired
  acks during the scan.

## Phase 8 -- reply nudge mechanism (a80d96c)

- New `nudge.py` module: `add_pending`, `clear_pending_for_sender`,
  `fire_due_nudges`, `schedule_initial_timer` (generation-guarded
  asyncio.call_later), `signal_nudge_check`.
- `mailbox.py`: `kind=nudge` (inbox-routed) +
  `kind=nudge_check` (control-routed) envelope kinds.
- `watch.py`: on inbox `deliver_message` with
  `requires_answer=true`, arm the timer; on control `nudge_check`,
  fire in-event-loop.
- `message_agent` clears pending entries directly when it sees a
  reply (SPEC §6.6 single-writer preserved -- the sender's MCP
  owns its own pending file).
- New Stop hook (`hooks/stop.py` + bash + .cmd wrappers): read
  pending state, write `kind=nudge_check` signal envelope when
  any entry is past-due.
- New hidden tool `arm_nudge_timer` (in-process fire path).
- Intervals via `OPERON_NUDGE_INTERVALS` env (default `15,30,60`).

## Phase 7 -- override + acknowledge flow (0a16e12, 00fbace)

- `request_override` MCP tool: elicits user yes/no via
  `elicitation/create`; on approve writes a one-shot override
  token to `overrides/<handle>/<rule_id>-<uuid>.json`. PreToolUse
  hook consumes the token on the retry.
- `acknowledge_warning` MCP tool: writes a non-one-shot ack token
  with TTL (default 60 s) to `acks/<handle>/`.
- Hook semantics: warn-tier rule -> `deny + "call
  acknowledge_warning"`; deny-tier rule -> `deny + "call
  request_override"`. Hook never emits `permissionDecision: ask`.
- 00fbace: audit-trail outcome correction for the failclosed
  override-consumed path (`outcome=overridden` not `blocked`).

## Phase 6.5 -- destructive activate + restore (57908c0)

- `activate_workflow(workflow_id, run_name)`: creates new run-dir,
  optional destructive close of alive workers (with elicit
  confirmation), atomically swaps `_active.json`.
- `restore_operon_session(run_name)`: switch active pointer to an
  existing run-dir. With no args, presents a single-select picker
  via `elicitation/create`.

## Phase 6 -- guardrail Rules (75ddcca, 7d62b3a, e50e304)

- `rules.py`: parse + merge plugin/user/project/workflow-tier
  manifests, role/phase filtering, severity ordering
  (deny>warn>log).
- `evaluate` MCP tool: hook-internal projection of the merged
  rule set against (tool_name, tool_input, role, phase).
- PreToolUse hook switched from `type:mcp_tool` to
  `type:command` (recursion + connection-race fix).
- Fail-closed deny when the hook can't reach the MCP server.

## Phase 5 -- workflow + phase engine (56f60a2, 7ae8d2a, e347448)

- `workflow.py`: 3-tier manifest loader (project > user >
  plugin), phase parsing, advance-check protocol.
- `advance_phase` MCP tool: runs the current phase's
  advance-checks; on success, advances `phase_state.json` and
  notifies all roster Agents via `kind=deliver_message`.
- `get_phase`, `set_artifact_dir`, `get_artifact_dir`.
- `get_applicable_rules` (introspection -- extended in Phase 9).
- Phase 5 carryovers: handle propagation across spawn,
  portable shim ladder for non-uv environments.

## Phase 4 -- inter-agent messaging (7984820 + 5 carryovers)

- `mailbox.py`: envelope shape, atomic write via temp + os.replace,
  inbox/control directory layout.
- `watch.py`: watchdog-based watch loop, async dispatch into a
  per-agent push channel, processed-dir cleanup.
- `message_agent` / `broadcast_message` / `interrupt_agent` MCP
  tools.
- `--channels` capability flag plumbed through `spawn_agent` so
  bg-worker MCP servers can push envelope payloads as chat
  messages instead of polling.

## Phase 3 -- single-agent spawn (a38b6db, 4d10d0e)

- `spawn_agent` MCP tool: `claude --bg <prompt>` subprocess,
  parses "backgrounded · <short>" stdout for session_id,
  writes `_handles/<uuid>.json`, appends roster row.
- `close_agent` MCP tool: `claude stop <daemon_short>` +
  roster row removal.
- `_smoke` throwaway workflow (2 phases: vision -> main).

## Phase 2 -- identity binding (0f5b492, 05e6bda, dfb44f9)

- `identity.py`: env-anchored handle resolution
  (`OPERON_AGENT_HANDLE` -> `_handles/<uuid>.json`).
- `whoami` MCP tool.
- `bind_handle` MCP tool for the Coordinator's session-start
  handshake.
- `hooks.json` schema declarations.

## Phase 1 -- MCP server scaffold (c6df237)

- `operon_mcp_server` Python package, lowlevel `Server` from the
  `mcp` SDK, `bin/operon-mcp-server` shim with uv -> python3 ->
  python resolution ladder.

## Phase 0 -- repo bootstrap (bf9f389, 646e9a9)

- `pyproject.toml`, `.claude-plugin/marketplace.json`,
  `plugins/operon-plugin/.claude-plugin/plugin.json`, license,
  initial README.
