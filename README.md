# operon-plugin

**Operon: A Guided, Self-Revisable plugin for AI Research Software Operations in Claude Code.**

operon-plugin brings guided, self-revisable multi-Agent orchestration
(Agent spawning, inter-Agent messaging, a workflow + phase engine, and
guardrail Rules with override / acknowledge) into Claude Code as a
native plugin so that runtime draws on the Max 20x interactive
subscription instead of the SDK credit bucket.

## Documentation

Full documentation lives in the docs site (built from `docs/` with
MkDocs Material). Start there:

- **User Guide** (`docs/user-guide/`) -- for people running operon:
  [Install](docs/user-guide/install.md),
  [Quickstart](docs/user-guide/quickstart.md),
  [Workflows and Phases](docs/user-guide/workflows-and-phases.md),
  [Agents and Messaging](docs/user-guide/agents-and-messaging.md),
  [Guardrails](docs/user-guide/guardrails.md),
  [Sessions](docs/user-guide/sessions.md),
  [MCP Tools Reference](docs/user-guide/mcp-tools-reference.md).
- **Contributor/Architecture Guide** (`docs/dev/`) -- the authoritative
  design reference: [Architecture](docs/dev/architecture.md),
  [MCP Server](docs/dev/mcp-server.md),
  [Workflow Engine](docs/dev/workflow-engine.md),
  [Rules and Enforcement](docs/dev/rules-and-enforcement.md),
  [Hooks](docs/dev/hooks.md), [Skills](docs/dev/skills.md),
  [Testing](docs/dev/testing.md),
  [Contributing](docs/dev/contributing.md).

The reference tables (MCP tools, hooks, env vars, slash commands) are
owned by the docs pages above; this README links to them rather than
duplicating them.

This repository is both a **single-plugin marketplace** and the plugin
itself:

```
.claude-plugin/marketplace.json   # marketplace manifest (lists ./plugins/operon-plugin)
plugins/operon-plugin/            # the plugin
plugins/operon-plugin/src/operon_mcp_server/            # Python MCP server package
```

## Status

Pre-release. The MCP server, workflows, hooks, rules, and skills are
landed phase by phase; the authoritative design reference is the
[Contributor/Architecture Guide](docs/dev/architecture.md). The current
phase is whatever the most recent commit on `main` says it is; the
README intentionally does not pin a phase number to avoid drifting from
reality.

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
pip install -e plugins/operon-plugin   # installs operon_mcp_server + the three runtime deps
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
time, and the marketplace catalog is cached separately, so picking up a
new release (a version bump, or edits to `workflows/`, `rules.yaml`,
`.mcp.json`, or `hooks/hooks.json`) takes two steps -- refresh the
catalog first, then update the plugin:

```bash
claude plugin marketplace update operon-plugin-marketplace
claude plugin update operon-plugin@operon-plugin-marketplace
```

The first command re-pulls `marketplace.json`; the second then sees the
newer version and pulls the code. `claude plugin update` compares the
`version` string in `plugins/operon-plugin/.claude-plugin/plugin.json`,
so a release must bump that version or `update` reports "already at the
latest version" and keeps the cached copy. Restart Claude Code after
updating so the daemon picks up the new plugin state.

If `update` still reports up-to-date after both commands, clear the
plugin cache and reinstall (Windows PowerShell: `Remove-Item -Recurse
-Force $HOME\.claude\plugins\cache`):

```bash
rm -rf ~/.claude/plugins/cache
claude plugin install operon-plugin@operon-plugin-marketplace
```

The dev-loop path (`claude --plugin-dir <repo>`) reads from disk
on every launch and does not need refresh; only marketplace
installs are snapshot-cached.

## Quick start

Auto-bootstrap (Phase 14): launching `claude` in any project
auto-creates a default Coordinator identity on first MCP start --
no `OPERON_AGENT_HANDLE` env export, no setup script, nothing to
remember. From a fresh project:

```bash
cd <your-project>
claude --plugin-dir /path/to/operon-plugin/plugins/operon-plugin
```

The MCP server detects the missing `.operon/_active.json`, writes a
new `<project>/.operon/default/` with a fresh Coordinator handle,
and threads that identity through the rest of the session. The
default run name is overrideable via `OPERON_DEFAULT_RUN_NAME`.

Once in the session, jump straight to a real workflow:

```text
mcp__operon__activate_workflow(workflow_id="project_team", run_name="my_first_run")
```

(Or invoke the corresponding skill: `/project_team my_first_run`.)
From there you can:

- `/agent w1 .` -- spawn a worker named `w1` in the current cwd.
- `message_agent("w1", "hi", requires_answer=true)` -- send the
  worker a message and arm a reply-nudge timer.
- `/rules` -- introspect the active guardrail Rules + escape tokens.
- `advance_phase()` -- elicit manual-confirm and move to the next
  phase.
- `/restore` -- pick a different operon-session to switch to.

The bundled `_smoke` workflow is still available for verifying the
install end-to-end without committing to a real project structure;
activate it via `activate_workflow(workflow_id="_smoke", ...)`.

If any of these fail with `ModuleNotFoundError` or hang at MCP
handshake, the daemon python is missing deps (see Option B above).

### Manual fixture (advanced)

For testing-with-specific-fixtures workflows where you want a
pre-built operon-session at a known path with a stable handle UUID,
`scripts/smoke_fixture_setup.py` writes a minimal Coordinator
bootstrap under `/tmp/test-operon/`. This is the path used by the
in-process e2e + bg verification scripts; ordinary users never need
to run it.

## MCP tools, skills, hooks, and env vars

The reference tables for these surfaces live in the docs site and are
the single source of truth -- this README links to them rather than
carrying duplicate tables that drift:

- **MCP tools** (all-visible, Coordinator-only, hidden) ->
  [MCP Tools Reference](docs/user-guide/mcp-tools-reference.md).
- **Slash commands (skills)** -- `/rules`, `/restore`, `/project_team`
  -> [MCP Tools Reference](docs/user-guide/mcp-tools-reference.md)
  (commands section).
- **Hooks** (`PreToolUse` rule enforcement, `Stop` reply-nudge tap) ->
  [Hooks](docs/dev/hooks.md).
- **Configuration env vars** (`OPERON_AGENT_HANDLE`,
  `OPERON_NUDGE_INTERVALS`, ...) ->
  [Install](docs/user-guide/install.md).

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

The authoritative design reference is the
[Contributor/Architecture Guide](docs/dev/architecture.md) in the docs
site. See also `CHANGELOG.md` for a phase-by-phase implementation log.

## License

Apache-2.0. See `LICENSE`.
