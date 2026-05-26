# Terminology -- Canonical Glossary for operon-plugin Docs

> **Status:** Specification phase artifact (terminology guardian).
> **Purpose:** ONE name, ONE meaning, ONE canonical home for every
> domain term the operon-plugin docs must teach. The User Guide and the
> Contributor/Architecture Guide reference this glossary; they do not
> redefine these terms.
>
> **Newcomer rule:** every term below is written so a contributor who
> has never seen operon-plugin (or claudechic) can follow it in
> isolation. Where a term is grounded in a real file, the path is given
> so the doc page can LINK to the source rather than duplicate it.

This file is the canonical home for naming. When two files disagree,
this file (aligned with the source-of-truth artifacts it cites) wins;
fix the other file.

---

## 1. Canonical glossary (term -> one-line definition)

Definitions are the contract. Casing conventions are in section 4;
resolved naming decisions are in section 2; known conflicts the docs
must NOT reproduce are in section 3.

### Distribution & runtime surface

| Term | Canonical definition | Canonical source |
|------|----------------------|------------------|
| **plugin** | The operon-plugin: a Claude Code native plugin bundling an MCP server, workflows, skills, hooks, and guardrail Rules. | `plugins/operon-plugin/` |
| **marketplace** | The Claude Code plugin-distribution manifest that lists installable plugins. This repo is a *single-plugin marketplace*: it is both the marketplace and the one plugin it ships. | `.claude-plugin/marketplace.json` |
| **MCP server** | The Python package that exposes operon's MCP tools; launched once per Claude Code session by the bin shim. Registered under the server name `operon` in `.mcp.json`. | `src/operon_mcp_server/`, `.mcp.json` |
| **bin shim** | The launcher script (`operon-mcp-server` bash + `.cmd`) that resolves a Python interpreter via the `uv -> python3 -> python` ladder and starts the MCP server. | `plugins/operon-plugin/bin/` |
| **MCP tool** | A single callable the MCP server exposes, addressed as `mcp__operon__<name>` (e.g. `mcp__operon__activate_workflow`). Visibility tiers: all-visible, Coordinator-only, hidden/hook-only. | `src/operon_mcp_server/tools/` |
| **hook** | A Claude Code event handler the plugin registers. operon ships two: `PreToolUse` (Rule enforcement) and `Stop` (reply-nudge tap). | `plugins/operon-plugin/hooks/hooks.json` |
| **skill (slash command)** | A user-invocable, script-injection command (`/<name>`) whose body proxies to one or more MCP tools so the model cannot be prompt-injected into the action. | `plugins/operon-plugin/skills/<name>/SKILL.md` |

### Workflow engine

| Term | Canonical definition | Canonical source |
|------|----------------------|------------------|
| **workflow** | A named, phase-structured process defined by a manifest. Identified by its `workflow_id` (e.g. `project_team`, `_smoke`). Activated via the `activate_workflow` MCP tool or the matching skill. | `plugins/operon-plugin/workflows/<id>/<id>.yaml` |
| **phase** | One ordered stage within a workflow, each carrying its own instructions and zero or more advance-checks. The active phase is tracked in `phase_state.json`. | workflow manifest `phases:` |
| **advance-check** | A gate that must pass before a phase can advance (e.g. `manual-confirm`, `file-exists-check`, `artifact-dir-ready-check`). | manifest `advance_checks:` |
| **advance / advance_phase** | The act of moving to the next phase, run via the `advance_phase` MCP tool, which first evaluates the current phase's advance-checks. | `tools/advance_phase.py` |

### Identity & team

| Term | Canonical definition | Canonical source |
|------|----------------------|------------------|
| **role** | The *type* of a team member (e.g. `coordinator`, `implementer`, `terminology`). Defined by an `identity.md`. The machine identifier is a lowercase snake_case directory name; the display name is Title Case (e.g. role `terminology` -> "Terminology Guardian"). | `workflows/<wf>/<role>/identity.md` |
| **agent** | A *running instance* of a role within an operon-session. The Coordinator runs in the foreground; workers run via `claude --bg`. | roster (`agents.json`) |
| **Coordinator** | The orchestrating agent; the workflow's `main_role`. Prime directive: "delegate, don't do." Holds the Coordinator-only MCP tools. | `workflows/project_team/coordinator/identity.md` |
| **handle** | The env-anchored identity record that binds a running agent to its role and run. Carried in the `OPERON_AGENT_HANDLE` env var; persisted under `_handles/`. | `src/operon_mcp_server/identity.py` |
| **team / roster** | The set of agents in one operon-session. Snapshot via the `list_agents` MCP tool. | `agents.json` |
| **Leadership** | The fixed set of FOUR review agents: Composability, Terminology Guardian, Skeptic, User Alignment. | `workflows/project_team/README.md` |
| **Leadership phase** | The `leadership` workflow phase, where the Coordinator spawns the Leadership agents. | manifest phase `leadership` |
| **advisory agent** | A non-blocking supporting role spawned only when relevant (e.g. Researcher, UI Designer, Project Integrator). | `workflows/project_team/README.md` |

### Run / session & artifacts

An operon-session occupies **two directories** keyed by the same
`run_name` (see SPECIFICATION §5): `<project>/.operon/<run_name>/` holds
the ephemeral runtime state, and `<project>/operon-runs/<run_name>/`
holds the versioned work products (the artifact directory). The
definitions below reflect that split.

| Term | Canonical definition | Canonical source |
|------|----------------------|------------------|
| **operon-session** | An activated workflow instance, with its own phase state, roster, escape tokens, and artifact directory. It occupies TWO directories under the same `run_name`: `<project>/.operon/<run_name>/` (runtime state) and `<project>/operon-runs/<run_name>/` (work products). This is the canonical term for the concept (see D1, section 2). | `.operon/<run_name>/` + `operon-runs/<run_name>/` |
| **`.operon/<run_name>/`** | The EPHEMERAL runtime directory of one operon-session: mailbox, `_handles/`, `phase_state.json`, `state.json`, roster (`agents.json`), escape tokens (`overrides/`), and the audit log. Gitignored on every branch; never committed. | `<project>/.operon/<run_name>/` |
| **`operon-runs/<run_name>/`** | The WORK-PRODUCTS directory of one operon-session: `STATUS.md`, `userprompt.md`, `specification/`, and other authored artifacts. This is the artifact directory (`artifact_dir`). Tracked on `develop`, ignored on `main` (SPECIFICATION §5). | `<project>/operon-runs/<run_name>/` |
| **run_name** | The identifier of one operon-session (the shared directory leaf under BOTH `.operon/` and `operon-runs/`). Reserved for the identifier only -- not a synonym for the session concept. | `state.json` |
| **run-dir** | An on-disk directory of one operon-session. Disambiguate when it matters: the *runtime* run-dir is `.operon/<run_name>/`; the *work-products* run-dir is `operon-runs/<run_name>/`. A path, not the concept. | -- |
| **artifact_dir** | The per-session WORK-PRODUCTS directory pointer -- `<project>/operon-runs/<run_name>/` -- substituted as the `${ARTIFACT_DIR}` token in advance-check paths. Set via the `set_artifact_dir` MCP tool; the value is stored in `.operon/<run_name>/state.json`. | `tools/set_artifact_dir.py` |
| **`${ARTIFACT_DIR}`** | The ONE canonical template token for the artifact directory (`operon-runs/<run_name>/`) in workflow manifests and checks. There is no `${CLAUDECHIC_ARTIFACT_DIR}` alias (removed; see section 3.D). | `project_team.yaml` |
| **`_active.json`** | The per-project pointer naming the currently active operon-session. Swapped by `activate_workflow` and `restore_operon_session`. | `<project>/.operon/_active.json` |

### Guardrails & escape tokens

| Term | Canonical definition | Canonical source |
|------|----------------------|------------------|
| **guardrail Rule** | A declarative safety rule matched on tool + input, scoped by role and/or phase. Plugin-tier rules are global; workflow-embedded rules are additive; higher tiers can override by id. | `plugins/operon-plugin/rules.yaml`, manifest `rules:` |
| **enforcement tier** | A Rule's severity. Exactly three: **deny**, **warn**, **log** (defined below). | `rules.yaml` schema header |
| **deny** | Hard block. The action does not run unless the agent first obtains an override. | rule `enforcement: deny` |
| **warn** | Soft block. The action proceeds only after the agent acknowledges it. | rule `enforcement: warn` |
| **log** | Silent audit. The action proceeds; the Rule records an entry and never prompts. | rule `enforcement: log` |
| **override** | A one-shot escape token authorizing a single deny-tier action. Requested via `request_override` (elicits user approval); consumed by the PreToolUse hook on first match. | `tools/request_override.py` |
| **acknowledge / ack** | A TTL-bounded escape token (default 60s) clearing a warn-tier Rule. Issued via `acknowledge_warning`. | `tools/acknowledge_warning.py` |
| **escape token** | Umbrella term covering both *overrides* and *acks* -- the two ways an agent clears a gating Rule. | `/rules` skill output |

### Introspection

| Term | Canonical definition | Canonical source |
|------|----------------------|------------------|
| **whoami / get_phase / get_applicable_rules / get_agent_info** | The self-awareness MCP tools: caller identity; active phase + artifact dir; Rules + advance-checks + escape tokens for the caller's (role, phase); and an aggregator of all three. | `src/operon_mcp_server/tools/` |

---

## 2. Resolved naming decisions (locked)

These were decided by the user during the Specification phase
(`operon-runs/docs/userprompt.md`). They are now canonical; docs MUST
follow them.

- **D1 -- run concept term = `operon-session`.** Use `operon-session`
  for the concept everywhere. Do NOT write `operon-run` or
  `operon session` (space form). Use `run_name` only for the identifier
  and `run-dir` only for an on-disk path (disambiguate the runtime
  `.operon/<run_name>/` vs the work-products `operon-runs/<run_name>/`
  when it matters; see section 1, Run / session & artifacts).
- **D2 -- repository org casing = `SprustonLab`** (camelCase) in all
  docs and the README, including URLs and the marketplace install
  command. Do not write `sprustonlab` in prose. (The git remote URL host
  path may be lowercase where the platform requires it; prose and
  display use `SprustonLab`.)
- **Claudechic removal -- `${ARTIFACT_DIR}` is the sole token.** The
  `${CLAUDECHIC_ARTIFACT_DIR}` token has been renamed to
  `${ARTIFACT_DIR}` with no back-compat alias. Docs reference only
  `${ARTIFACT_DIR}`.
- **Phase flow -- teach the 10-phase manifest.** See section 3.B.

---

## 3. Conflicts the docs must NOT reproduce

Each item names the canonical source and the divergent copies. Doc
pages must follow the canonical source and link to it; the divergent
copies are being remediated under the claudechic-removal scope.

### A. The "run" concept had three names

`operon-session` (dominant), `operon-run`, and `operon session` (space
form) all named the same thing across the repo. **Canonical:
`operon-session`** (D1). The other two are being renamed. Sub-parts:
`run_name` = identifier, `run-dir` = path. Never use these sub-part
names as a synonym for the session.

### B. The phase list had three contradictory versions

- **Canonical source of truth:** `project_team.yaml` -- **10 phases**:
  `vision, setup, leadership, specification, implementation,
  testing-vision, testing-specification, testing-implementation,
  documentation, signoff`.
- **Stale 7-phase mirror:** `coordinator/identity.md` "Roadmap"
  collapses the three `testing-*` phases into one "Testing" and omits
  `documentation`. (The file self-labels the manifest as source of
  truth.)
- **Third variant (4 phases):** `workflows/project_team/README.md`
  "Workflow" section lists only Vision / Specification / Implementation
  / Testing.

Docs MUST present the 10-phase flow and must not reproduce the 7- or
4-phase counts.

### C. "Agent" vs "agent" casing was mixed

The top-level README used both "Agent" (capitalized) and "agent"
(lowercase) for the same common noun. **Canonical convention:** see
section 4 -- lowercase `agent`/`role` as common nouns; Title Case only
for proper role display names (Coordinator, Terminology Guardian).

### D. `${CLAUDECHIC_ARTIFACT_DIR}` token

This live token was renamed to `${ARTIFACT_DIR}` (claudechic-removal
directive); the back-compat alias is dropped. Docs reference only
`${ARTIFACT_DIR}`. No doc should mention the old token except, if
needed, a one-line migration note in the Contributor guide.

### E. Non-existent agent / dead links in `project_team/README.md`

This file (claudechic carry-over) references a **`git_setup` agent**
(roster table + "Git Setup runs first" rule) that does not exist in the
roster, and links to `docs/getting-started.md` and an
"AI_PROJECT_TEMPLATE" that do not exist in this repo. Docs must not
reference any of these. Integration-type work in this repo is owned by
the **`project_integrator`** role (which is itself skipped for this
run).

---

## 4. Casing & spelling conventions

- **operon-plugin** -- lowercase, hyphenated. The product/repo name.
- **operon** -- lowercase, when referring to the MCP server name, the
  `.operon/` or `operon-runs/` directories, or the project generally.
  Not capitalized mid-sentence.
- **claudechic** -- lowercase. (And: should not appear in shipped
  operon-plugin files at all, per the removal directive -- present in
  this spec only to name what is being removed.)
- **Claude Code** -- two words, both capitalized. The host application.
- **MCP** -- all caps. **MCP server**, **MCP tool** -- "MCP" caps, the
  noun lowercase.
- **role / agent** -- lowercase as common nouns. **Role slugs** are
  lowercase snake_case (`terminology`, `project_integrator`). **Role
  display names** are Title Case (Coordinator, Terminology Guardian,
  User Alignment, Test Engineer, UI Designer).
- **Leadership** -- capitalized when naming the four-agent group or the
  `leadership` phase as a proper concept; the phase *id* is lowercase
  `leadership`.
- **phase ids** -- lowercase, hyphen-joined where compound
  (`testing-vision`, not `testing_vision`).
- **enforcement tiers** -- lowercase `deny` / `warn` / `log`.
- **MCP tool names** -- always shown with the full namespace on first
  mention: `mcp__operon__activate_workflow`; the bare name
  (`activate_workflow`) is acceptable on subsequent mentions in the same
  page.
- **skills** -- shown with the leading slash: `/project_team`,
  `/restore`, `/rules`.
- **SprustonLab** -- camelCase (D2).

---

## 5. Canonical-home map (where each term is defined vs referenced)

To prevent definition drift, each concept has ONE defining doc page.
Other pages link to it. Proposed homes (final folder shape owned by
Composability):

| Concept cluster | Canonical doc home (proposed) | Backed by source |
|-----------------|-------------------------------|------------------|
| plugin, marketplace, install, bin shim | User Guide: install page | README, `.mcp.json`, `bin/` |
| MCP server, MCP tools, hooks | Contributor guide: architecture page | `src/operon_mcp_server/`, `hooks/` |
| workflow, phase, advance-check, the 10-phase flow | User Guide: workflows page | `project_team.yaml` |
| role, agent, Coordinator, Leadership, roster | User Guide: team/agents page | `workflows/project_team/README.md` |
| operon-session (the two-dir model), run_name, run-dir, artifact_dir, `_active.json` | User Guide: sessions page | `.operon/` + `operon-runs/` layout, `set_artifact_dir.py` |
| guardrail Rule, deny/warn/log, override, ack, escape token | User Guide: guardrails page | `rules.yaml`, `/rules` skill |
| skills (`/project_team`, `/restore`, `/rules`) | User Guide: commands page | `skills/*/SKILL.md` |

The glossary in this file is the ONE home for the one-line definitions;
the pages above expand each concept with examples and link back here.
