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

Bootstrap (Phase 0 of the implementation plan in `claudechic/.project_team/claude_code_port/SPEC_APPENDIX.md` §F).
Directory skeleton only; the MCP server, workflows, hooks, rules, and
skills are populated in subsequent phases.

## Install for development

Requires Python >= 3.10.

```bash
# from this repo root
pip install -e .   # installs operon_mcp_server in editable mode + its deps (mcp, watchdog)
claude --plugin-dir /groups/spruston/home/moharb/operon-plugin/plugins/operon-plugin/
```

In a separate Claude Code session, run `/plugin list`; `operon-plugin`
should appear as registered. Use `/reload-plugins` after edits.

## Install from marketplace (post-publish, placeholder)

Once published to `SprustonLab/operon-plugin` on GitHub:

```bash
claude plugin marketplace add SprustonLab/operon-plugin
claude plugin install operon-plugin@operon-plugin-marketplace
```

## Specification

The authoritative design lives in the claudechic repository at
`../claudechic/.project_team/claude_code_port/SPEC.md` and
`SPEC_APPENDIX.md`. This README will be expanded as the plugin
implementation progresses through Phases 1--10.

## License

Apache-2.0. See `LICENSE`.
