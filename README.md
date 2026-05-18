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

Requires Python >= 3.10 and these runtime deps in whichever python is
first on the daemon's PATH:

- `mcp>=1.0` -- MCP SDK
- `watchdog>=4.0` -- filesystem mailbox watch loop
- `PyYAML>=6.0` -- workflow manifest parser

```bash
# from this repo root
pip install -e .   # installs operon_mcp_server + the three runtime deps above
claude --plugin-dir /groups/spruston/home/moharb/operon-plugin/plugins/operon-plugin/
```

In a separate Claude Code session, run `/plugin list`; `operon-plugin`
should appear as registered. Use `/reload-plugins` after edits.

If the spawned worker MCP subprocess fails with
`ModuleNotFoundError: No module named 'operon_mcp_server'` or
`No module named 'yaml'`, the `python` on Claude Code's daemon PATH
does not have the operon package and/or its deps installed in its
site-packages. Either `pip install -e .` into that python, or set
`PYTHONPATH=<repo>/src` and `pip install mcp watchdog PyYAML` into
the daemon-PATH python (Carryover #4 documents this in detail).

## Install from marketplace

Once published to `SprustonLab/operon-plugin` on GitHub:

```bash
claude plugin marketplace add SprustonLab/operon-plugin
claude plugin install operon-plugin@operon-plugin-marketplace
```

NOTE: `claude plugin install` copies the plugin files into
`~/.claude/plugins/cache/...` but does NOT pip-install the
`operon_mcp_server` Python package. After installing the plugin,
you still need to ensure the daemon-PATH python has the runtime deps:

```bash
pip install 'mcp>=1.0' 'watchdog>=4.0' 'PyYAML>=6.0'
# AND either pip install operon-plugin's package into the same python,
# or set PYTHONPATH so `python -m operon_mcp_server.server` resolves.
```

## Specification

The authoritative design lives in the claudechic repository at
`../claudechic/.project_team/claude_code_port/SPEC.md` and
`SPEC_APPENDIX.md`. This README will be expanded as the plugin
implementation progresses through Phases 1--10.

## License

Apache-2.0. See `LICENSE`.
