# Composability Specification -- operon-plugin docs

Scope: the documentation set for operon-plugin (MkDocs/Material site +
README rework + Pages CI + claudechic-leftover remediation). This
document defines the axes the docs decompose along, the seams between
them, the compositional law that keeps pages from drifting, and the
crystal-hole risks to guard against.

## Terms (defined here)

- **Page cell:** a single Markdown file in the docs tree.
- **Audience:** the reader a page is written for -- `user` (drives the
  plugin) or `contributor` (extends/maintains it).
- **Surface:** the plugin subsystem a page documents. The surfaces are:
  install/runtime, MCP tools, workflows+phases, guardrail Rules, hooks,
  skills (slash commands), sessions/runs.
- **Lifecycle:** the arc of use -- install -> activate -> drive ->
  extend. Lifecycle orders content *within* a page; it is not a folder.
- **Canonical owner:** for any single fact (a table, a schema, a value
  list), the one page cell that states it in full. Every other page
  links to that cell rather than restating the fact.
- **Reference table:** a table enumerating a surface's members (e.g. the
  MCP-tool list, the env-var list, the hook list).

## Axes

The docs set decomposes along three axes. Two are structural (they shape
the folder tree); one is sequential (it orders content within a page).

### Axis A -- Audience (structural)

- Values: `user`, `contributor`.
- Realized as the top-level folder split: `docs/user-guide/` and
  `docs/dev/`.
- Independence: the same surface is documented at two depths. The `user`
  cell describes the experience ("a deny blocks the call; run `/rules`
  to see why"); the `contributor` cell describes the mechanism
  (`rules.yaml` schema, tier merge order, the `evaluate()` hook path).
  Neither depth implies the other's content.

### Axis B -- Surface (structural)

- Values: install/runtime, MCP tools, workflows+phases, guardrail Rules,
  hooks, skills, sessions/runs.
- Realized as the files within each audience folder.
- Independence: each surface is a separable subsystem in the source
  tree, so a page can document one without pulling in the others:
  - MCP tools: `src/operon_mcp_server/tools/` (one file per tool).
  - workflows+phases: `plugins/operon-plugin/workflows/<id>/<id>.yaml`
    (manifest) + per-role identity/phase Markdown.
  - guardrail Rules: `plugins/operon-plugin/rules.yaml` (plugin tier) +
    workflow-embedded `rules:` blocks; advance-checks in
    `src/operon_mcp_server/checks/`.
  - hooks: `plugins/operon-plugin/hooks/hooks.json` + wrapper scripts.
  - skills: `plugins/operon-plugin/skills/<name>/SKILL.md`.
  - install/runtime: `plugins/operon-plugin/bin/operon-mcp-server` shim
    + `pyproject.toml` deps.
  - sessions/runs: `.operon/<run_name>/` run-dirs + restore tooling.

### Axis C -- Lifecycle (sequential)

- Values: install -> activate -> drive -> extend.
- Realized as section ordering inside a page, never as a folder.
- Independence from A and B: lifecycle is the order a reader moves
  through, orthogonal to who they are and which surface they read.
  Folding it into the folder tree would multiply folders without adding
  separable content, so it stays in-page.

## Repo-path facts (avoid the nesting trap)

The plugin's surfaces live under `plugins/operon-plugin/`, not the repo
root. Any contributor page that cites a path MUST use the nested path:

- `plugins/operon-plugin/workflows/`
- `plugins/operon-plugin/skills/`
- `plugins/operon-plugin/hooks/`
- `plugins/operon-plugin/bin/`
- `plugins/operon-plugin/rules.yaml`
- `plugins/operon-plugin/.mcp.json`

The MCP server package is the exception -- it lives at repo root:
`src/operon_mcp_server/{tools,checks}/`.

## Compositional law

A page cell documents exactly one (Audience, Surface) pair and links to
the canonical owner of every fact it needs rather than restating it.

This is the seam that keeps cells decoupled: data (a link) crosses the
boundary; the fact's full definition does not. Moving one cell (editing
the user guardrails page) does not force edits to its sibling (the
contributor rules page), because neither restates the other's facts --
they cross-link.

Consequences:
- Each reference table has exactly one canonical owner. Per the resolved
  decision, the docs site owns the reference tables and the README links
  in. The contributor surface page owns its surface's schema/internals;
  the user surface page owns the experience and links across.
- The README is the front door: orientation plus deep links, not a
  second copy of the tables.

## Docs tree (axes made visible)

```
docs/
  index.md                  # front door; what operon is, who each guide serves
  user-guide/               # Audience = user
    install.md              # install/runtime surface
    quickstart.md           # activate lifecycle, end to end
    workflows-and-phases.md # workflows+phases surface
    agents-and-messaging.md # MCP tools surface (spawn, messaging, nudge)
    guardrails.md           # guardrail Rules surface (experience)
    sessions.md             # sessions/runs surface
    mcp-tools-reference.md  # MCP tools surface (canonical reference tables)
  dev/                      # Audience = contributor
    architecture.md         # surface map + repo layout (the keystone)
    mcp-server.md           # MCP tools surface (tools/ + checks/, evaluate())
    workflow-engine.md      # workflows+phases surface (manifest, advance-checks, loader)
    rules-and-enforcement.md# guardrail Rules surface (schema, tier merge, hook path)
    hooks.md                # hooks surface (wrappers, .cmd pairs, resolution ladder)
    skills.md               # skills surface (script-injection, activate.py dispatch)
    testing.md              # test surface (smoke_e2e, p*_bg_verify, fixtures)
    contributing.md         # branch model, .operon tracking, guard artifacts
```

`user-guide/` vs `dev/` is Axis A. The files within each are Axis B.
Lifecycle (Axis C) orders sections inside each file.

## Content-fact constraints

These facts the docs must teach (they are the canonical values for their
surface):

- Phase count: the workflow manifest defines 10 phases -- vision, setup,
  leadership, specification, implementation, testing-vision,
  testing-specification, testing-implementation, documentation, signoff.
  Docs teach this 10-phase flow. The 7-phase and 4-phase variants found
  elsewhere in the source are stale and must not be reproduced.
- Canonical run term: `operon-session` (with `run_name` / `run-dir` used
  only for the identifier and the path).

## Crystal-hole risks

1. **Surface duplication across audiences.** If a user page and its
   contributor sibling both fully state the same fact (e.g. the rule
   schema), they drift. Closed by the compositional law: one canonical
   owner per fact; the other cell links.

2. **README-vs-docs double source.** The README holds reference tables
   today. Resolved: docs own the tables; README links in. The risk is
   reintroduction -- any future table added to both forks the source.

3. **Stale-model leakage from claudechic leftovers.** Source identity
   and manifest files carry claudechic assumptions (conda / `source
   activate` / `commands/` / `envs/`; the 7- and 4-phase variants; the
   `${CLAUDECHIC_ARTIFACT_DIR}` token, now to be renamed to
   `${ARTIFACT_DIR}`). Documenting a surface from a stale source re-encodes
   the wrong model. The leftover-remediation output is the ground-truth
   list; contributor pages describe only the post-remediation reality
   (uv + pyproject + bin shim; 10 phases; `${ARTIFACT_DIR}`).

4. **Path-nesting trap.** Citing repo-root paths for surfaces that live
   under `plugins/operon-plugin/` misleads contributors. Closed by the
   repo-path facts above and a layout callout in `dev/architecture.md`.

5. **Published-internal-docs leak.** The 4 internal audit files
   (AGENT_TEAMS_PIVOT_PLAN, APPENDIX.MD, SCOPE_AUDIT_v2.1,
   SPEC_GROUNDING_AUDIT) must sit outside the mkdocs source so the build
   does not publish them.

## Orthogonality check

For each surface, both an `install`-time and an `extend`-time reader can
reach a coherent page at both audience depths; choosing a value on one
axis does not remove a value on another. No (Audience, Surface) cell is
unreachable. The two-axis grid is therefore a full crystal: every cell
in the tree above is a real, writable page.
