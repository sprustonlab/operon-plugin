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

## Specification

The authoritative design lives in the claudechic repository at
`../claudechic/.project_team/claude_code_port/SPEC.md` and
`SPEC_APPENDIX.md`. This README will be expanded as the plugin
implementation progresses through Phases 1--10.

## License

Apache-2.0. See `LICENSE`.
