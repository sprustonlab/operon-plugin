# operon-plugin

**Operon: A Guided, Self-Revisable plugin for AI Research Software Operations in Claude Code.**

operon-plugin brings claudechic-style multi-Agent orchestration (Agent
spawning, inter-Agent messaging, workflow + phase engine, and guardrail
Rules with override / acknowledge) into Claude Code as a native plugin
so that runtime draws on the Max 20x interactive subscription instead
of the SDK credit bucket.

This repository is both a **single-plugin marketplace** and the plugin
itself:

```
.claude-plugin/marketplace.json   # marketplace manifest (lists ./plugins/operon-plugin)
plugins/operon-plugin/            # the plugin
src/operon_mcp_server/            # Python MCP server package
```

## Status

Pre-release. The MCP server, workflows, hooks, rules, and skills are
landed phase by phase per the implementation plan in
`claudechic/.project_team/claude_code_port/SPEC_APPENDIX.md` §F.
The current phase is whatever the most recent commit on `main` says it
is; the README intentionally does not pin a phase number to avoid
drifting from reality.

## Install for development

Requires Python >= 3.10. The MCP server is launched by the bundled
`plugins/operon-plugin/bin/operon-mcp-server` shim (bash on
Linux/macOS, `.cmd` on Windows). The shim resolves the Python
interpreter in this order:

1. **`uv run`** if `uv` is on PATH. uv reads our `pyproject.toml`
   and resolves deps (`mcp`, `watchdog`, `PyYAML`) into an
   ephemeral environment. Zero setup beyond installing uv.
   RECOMMENDED. See https://docs.astral.sh/uv/.
2. **First `python3` then `python`** on PATH whose site-packages
   contain all three of `mcp`, `watchdog`, `yaml`. Pre-flight
   import check catches missing-deps BEFORE Claude Code's MCP
   handshake fails silently.
3. **Loud error to stderr** with the exact `pip install` command
   the user needs. The error message ends up in Claude Code's MCP
   log at
   `~/.cache/claude-cli-nodejs/<cwd>/mcp-logs-plugin-operon-plugin-operon/<ts>.jsonl`.

The shim always prepends `${CLAUDE_PLUGIN_ROOT}/src` to PYTHONPATH so
the `operon_mcp_server` package is importable without `pip install -e .`
-- only the three runtime deps need to be available to the chosen
python.

### Option A: uv (recommended, zero-pip-install)

```bash
# install uv once: https://docs.astral.sh/uv/getting-started/installation/
claude --plugin-dir /groups/spruston/home/moharb/operon-plugin/plugins/operon-plugin/
```

uv resolves the three deps on first launch and caches them.

### Option B: pip install into the daemon-PATH python

```bash
# from this repo root
pip install -e .   # installs operon_mcp_server + the three runtime deps
claude --plugin-dir /groups/spruston/home/moharb/operon-plugin/plugins/operon-plugin/
```

CRITICAL: the python you pip into MUST be the one Claude Code's
daemon finds on PATH. The daemon is launched once and caches its
PATH; running `which python3` from the same shell you launch
`claude` from is the reliable check. If you use a conda env, make
sure that env is activated in the shell where you `claude` -- or
ship the activation into your shell init.

If the spawned worker MCP subprocess fails with
`ModuleNotFoundError`, the daemon's python is missing one of the
deps. Either switch to uv (Option A), or pip install into the right
python:

```bash
pip install 'mcp>=1.0' 'watchdog>=4.0' 'PyYAML>=6.0'
```

In a separate Claude Code session, run `/plugin list`; `operon-plugin`
should appear as registered. Use `/reload-plugins` after edits.

## Install from marketplace

Once published to `SprustonLab/operon-plugin` on GitHub:

```bash
claude plugin marketplace add SprustonLab/operon-plugin
claude plugin install operon-plugin@operon-plugin-marketplace
```

NOTE: `claude plugin install` copies the plugin files into
`~/.claude/plugins/cache/...` but does NOT install the Python
runtime deps. The bin shim still needs `uv` (Option A) or
pip-installed deps (Option B) per the section above.

## Refreshing after edits to a marketplace-installed plugin

`claude plugin install` snapshots the marketplace source at install
time, so post-install edits to `workflows/`, `rules.yaml`, `.mcp.json`,
or `hooks/hooks.json` are not visible until the snapshot is
refreshed. Use `claude plugin update`:

```bash
claude plugin update operon-plugin@operon-plugin-marketplace
```

The note from `claude plugin update --help` says "restart required to
apply" -- restart Claude Code after updating so the daemon picks up
the new plugin state. If `update` doesn't pick up your changes (e.g.,
because the marketplace is a local directory and `update` looks for
a git rev change), the heavy-handed fallback is uninstall +
reinstall:

```bash
claude plugin uninstall operon-plugin@operon-plugin-marketplace
claude plugin install operon-plugin@operon-plugin-marketplace
```

The dev-loop path (`claude --plugin-dir <repo>`) reads from disk
on every launch and does not need refresh; only marketplace
installs are snapshot-cached.

## Quick start

The plugin ships a throwaway `_smoke` workflow for verifying the
install. After Claude Code picks up the plugin, in a session inside
any project:

```text
/_smoke
```

This activates the smoke workflow (creates `.operon/<run-name>/`,
writes the Coordinator handle, advances to phase `vision`). From
there you can:

- `/agent w1 .` -- spawn a worker named `w1` in the current cwd.
- `message_agent("w1", "hi", requires_answer=true)` -- send the
  worker a message and arm a reply-nudge timer.
- `/rules` -- introspect the active guardrail Rules + escape tokens.
- `advance_phase()` -- elicit manual-confirm and move to the next
  phase.
- `/restore` -- pick a different operon-run to switch to.

If any of these fail with `ModuleNotFoundError` or hang at MCP
handshake, the daemon python is missing deps (see Option B above).

## MCP tools

All-visible (any agent can call):

| Tool | Purpose |
| ---- | ------- |
| `message_agent` | Send a message to another Agent. `requires_answer=true` (default) arms a reply-nudge timer. |
| `broadcast_message` | Same message to N targets. |
| `interrupt_agent` | Halt a busy peer. |
| `whoami` | Caller's identity from env-anchored handle. |
| `get_phase` | Active workflow + current phase + artifact dir. |
| `get_applicable_rules` | Rules + advance-checks + active escape tokens for the caller's (role, phase). |
| `get_agent_info` | Aggregator of the above. |
| `list_agents` | Current roster snapshot. |
| `list_operon_sessions` | Discoverable runs under this project. |
| `acknowledge_warning` | Issue a warn-tier ack token (TTL 60s). |
| `request_override` | Request a deny-tier override token (one-shot; elicits user approval). |
| `evaluate` | Hook-internal: PreToolUse rule projection. |

Coordinator-only:

| Tool | Purpose |
| ---- | ------- |
| `spawn_agent` | Spawn a worker via `claude --bg`. |
| `close_agent` | `claude stop` a worker + roster removal. |
| `spawn_worktree` | Spawn into a git worktree. |
| `advance_phase` | Run the current phase's advance-checks + advance. |
| `set_artifact_dir` | Set the per-run artifact directory pointer. |
| `get_artifact_dir` | Read the artifact-dir pointer. |
| `activate_workflow` | DESTRUCTIVE: swap `_active.json` to a new run-dir (closes alive workers with confirmation). |
| `restore_operon_session` | Switch the active pointer to an existing run-dir. |

Hidden (hook-only / internal):

| Tool | Purpose |
| ---- | ------- |
| `arm_nudge_timer` | Fire the pending-reply check in-event-loop. |

## Slash commands (skills)

User-invocable. Each is a script-injection skill that proxies to one
or more MCP tools so the LLM can't be prompt-injected.

| Slash | Backing tool(s) | Purpose |
| ----- | --------------- | ------- |
| `/rules` | `get_applicable_rules` | Show active Rules + escape tokens for (role, phase). |
| `/restore` | `list_operon_sessions` + `restore_operon_session` | Switch active operon-session. |

## Hooks

| Event | Type | Script | Purpose |
| ----- | ---- | ------ | ------- |
| `PreToolUse` | `command` | `hooks/pretooluse-wrapper` | Rule enforcement (deny / warn / log) + override + ack consumption. |
| `Stop` | `command` | `hooks/stop-wrapper` | Phase 8 reply-nudge tap: writes `kind=nudge_check` control envelope when a pending reply is past-due. |

Both wrappers ship in two flavors (`<wrapper>` bash + `<wrapper>.cmd`
Windows batch) with the same `uv -> python3 -> python` resolution
ladder as `bin/operon-mcp-server`.

## Configuration env vars

| Var | Default | Purpose |
| --- | ------- | ------- |
| `OPERON_AGENT_HANDLE` | (required at runtime) | Env-anchored caller identity. Set by `spawn_agent` for workers; the Coordinator binds this from `_handles/<coord>.json` on session start. |
| `CLAUDE_PLUGIN_ROOT` | (set by Claude Code) | Plugin install root. Used to resolve the 3-tier workflow loader's plugin tier and the `mcp_tools/` directory. |
| `OPERON_BG_CHANNELS` | (autodetect) | Whether spawned workers should attach a `--channels` flag (Phase 4 message-push transport). |
| `OPERON_DEBUG` | unset | Set to any truthy value (`1`, `true`, `yes`) to enable verbose hook + MCP server logging on stderr. |
| `OPERON_NUDGE_INTERVALS` | `15,30,60` | Comma-separated seconds between successive nudges. Test harnesses use `1,1,1` or `2,2,2` to compress wall-clock. |
| `OPERON_NUDGE_MAX` | derived | Implicit cap = `len(OPERON_NUDGE_INTERVALS)`. After this many fires, the entry is `nudge_exhausted`. |

## Cross-platform notes

- `pathlib.Path` everywhere; `encoding="utf-8"` on every file open and
  on `subprocess.run` calls that use text mode.
- `os.replace` for atomic renames (NOT `Path.rename`, which raises on
  Windows when the target exists).
- Hook + bin shims ship as bash + `.cmd` pairs. Claude Code on Windows
  invokes the `.cmd` variant via `cmd.exe`.

Manual Windows smoke procedure (no Windows test box in CI yet):

1. Install Python 3.10+ (Microsoft Store or python.org) and either
   `uv` (recommended) or `pip install 'mcp>=1.0,<2' 'watchdog>=4.0,<5' 'PyYAML>=6.0,<7'`.
2. `git clone` this repo to a path with no spaces.
3. `claude --plugin-dir <repo>\plugins\operon-plugin`.
4. In the session, run `/agent w1 .`; verify the worker spawns and a
   `_handles\<uuid>.json` file is created with forward slashes inside
   the JSON (the directory separator on disk is `\`).
5. Send a `requires_answer=true` message; in the worker, do NOT
   reply. Verify a `kind=nudge` envelope appears in the worker's
   inbox after `OPERON_NUDGE_INTERVALS` elapses.

## End-to-end smoke

`scripts/smoke_e2e.py` is an in-process happy-path verification that
exercises Phases 3-9 + the Phase 10 row-truncation guard in a single
zero-arg run. Mocks `elicit.confirm` / `elicit.select_one`; does NOT
spawn real `claude --bg` workers (that path is covered by the
phase-by-phase `p*_bg_verify.py` scripts the implementer used during
development).

```bash
uv run --project . python scripts/smoke_e2e.py
```

Expected: 20/20 steps pass in <10s.

## Specification

The authoritative design lives in the claudechic repository at
`../claudechic/.project_team/claude_code_port/SPEC.md` and
`SPEC_APPENDIX.md`. See also `CHANGELOG.md` for a phase-by-phase
implementation log.

## License

Apache-2.0. See `LICENSE`.
