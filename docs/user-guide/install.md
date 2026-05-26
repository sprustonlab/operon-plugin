# Install

operon-plugin is a Claude Code native plugin. The recommended path is to
install it from the marketplace. If you are working on the plugin itself,
use the from-source dev loop further down.

## Install from the marketplace (recommended)

The repository is a *single-plugin marketplace*: it is both the
marketplace (`.claude-plugin/marketplace.json`) and the one plugin it
ships (`plugins/operon-plugin/`).

```bash
# install uv once so the server's deps resolve automatically:
#   https://docs.astral.sh/uv/getting-started/installation/
claude plugin marketplace add SprustonLab/operon-plugin
claude plugin install operon-plugin@operon-plugin-marketplace
```

!!! note "Runtime deps are separate from the plugin files"
    `claude plugin install` copies the plugin into
    `~/.claude/plugins/cache/...` but does NOT install the MCP server's
    three Python runtime deps. With `uv` on PATH the bundled bin shim
    resolves them automatically on first launch (zero extra steps).
    Without uv, pip-install them into the Python Claude Code's daemon
    uses: `pip install 'mcp>=1.0' 'watchdog>=4.0' 'PyYAML>=6.0'`.

### Refreshing a marketplace install

`claude plugin install` snapshots the marketplace source at install
time, and the marketplace catalog itself is cached separately. So
picking up a new release (a version bump, or edits to `workflows/`,
`rules.yaml`, `.mcp.json`, or `hooks/hooks.json`) takes two steps --
refresh the catalog first, then update the plugin:

```bash
claude plugin marketplace update operon-plugin-marketplace
claude plugin update operon-plugin@operon-plugin-marketplace
```

The first command re-pulls `marketplace.json` from the marketplace
repo; the second then sees the newer version and pulls the code. Skip
the first and `update` keeps comparing against the stale cached catalog.
Restart Claude Code after updating so the daemon picks up the new plugin
state.

!!! note "Updates are detected by version, not by new commits"
    `claude plugin update` compares the plugin's `version` string. A
    release that pushes new code without bumping `version` in
    `plugins/operon-plugin/.claude-plugin/plugin.json` reports "already
    at the latest version" and keeps the cached copy -- so bump the
    version to ship a change to existing installs.

If `update` still reports up-to-date after both commands, clear the
plugin cache and reinstall:

```bash
rm -rf ~/.claude/plugins/cache
# Windows (PowerShell): Remove-Item -Recurse -Force $HOME\.claude\plugins\cache
claude plugin install operon-plugin@operon-plugin-marketplace
```

## From source (dev loop)

For working on the plugin itself, point Claude Code at a local clone
with `--plugin-dir`. This reads from disk on every launch and never
needs a marketplace refresh.

### Option A: uv (no pip-install needed)

```bash
# 1. install uv once: https://docs.astral.sh/uv/getting-started/installation/

# 2. clone the repo
git clone https://github.com/SprustonLab/operon-plugin.git

# 3. launch Claude Code pointed at the bundled plugin
claude --plugin-dir operon-plugin/plugins/operon-plugin/
```

uv resolves the three runtime deps on first launch and caches them.

### Option B: pip into the daemon-PATH Python

```bash
# clone the repo, then from the repo root:
git clone https://github.com/SprustonLab/operon-plugin.git
cd operon-plugin
pip install -e plugins/operon-plugin   # installs operon_mcp_server + the three runtime deps
claude --plugin-dir plugins/operon-plugin/
```

!!! warning "Use the daemon's Python"
    The Python you pip into MUST be the one Claude Code's daemon finds
    on PATH. The daemon launches once and caches its PATH; run `which
    python3` from the same shell you launch `claude` from to check. If
    you use a conda env, make sure that env is activated in the shell
    where you launch `claude`.

If a spawned worker's MCP subprocess fails with `ModuleNotFoundError`,
the daemon's Python is missing a dep. Either switch to uv (Option A) or
pip-install into the right Python:

```bash
pip install 'mcp>=1.0' 'watchdog>=4.0' 'PyYAML>=6.0'
```

After editing plugin files, run `/reload-plugins` (or restart) so the
daemon picks up the change. `/plugin list` should show `operon-plugin`
as registered.

## Verifying the install

Once a session is up, the [Quickstart](quickstart.md) walks you through
activating a workflow. If anything fails with `ModuleNotFoundError` or
hangs at the MCP handshake, the daemon's Python is missing deps -- see
the runtime-deps note above.

## Requirements and the interpreter ladder

- **Python >= 3.10**, reachable by Claude Code's daemon.
- The three runtime dependencies of the MCP server: `mcp`, `watchdog`,
  `PyYAML`.

The MCP server is launched by the bundled **bin shim**,
`plugins/operon-plugin/bin/operon-mcp-server` (bash on Linux/macOS,
`operon-mcp-server.cmd` on Windows). The shim resolves a Python
interpreter via this ladder:

1. **`uv run`** if `uv` is on PATH. uv reads the plugin's
   `pyproject.toml`, resolves the three deps into an ephemeral
   environment, and caches them. Zero setup beyond installing uv.
   RECOMMENDED. See <https://docs.astral.sh/uv/>.
2. **`python3` then `python`** -- the first whose site-packages already
   contain `mcp`, `watchdog`, and `yaml`. A pre-flight import check
   catches missing deps before Claude Code's MCP handshake fails
   silently.
3. **Loud error to stderr** with the exact `pip install` command to
   run. That message lands in Claude Code's MCP log at
   `~/.cache/claude-cli-nodejs/<cwd>/mcp-logs-plugin-operon-plugin-operon/<ts>.jsonl`.

The shim always prepends `${CLAUDE_PLUGIN_ROOT}/src` to `PYTHONPATH`,
so `operon_mcp_server` is importable without `pip install -e .` -- only
the three runtime deps need to be available to the chosen Python.

For how the MCP server is registered and launched internally, see the
[Contributor/Architecture Guide -> MCP Server](../dev/mcp-server.md).
