# Multi-Agent Project Team

A structured multi-agent workflow for building software projects with Claude Code. Launch it by activating the `project_team` workflow via operon (`mcp__operon__activate_workflow(workflow_id="project_team", ...)`).

The canonical phase list, advance-checks, and workflow-embedded rules are defined by the manifest [`project_team.yaml`](project_team.yaml).

**See [coordinator/identity.md](coordinator/identity.md) for the full orchestration logic.**

## Workflow

The manifest [`project_team.yaml`](project_team.yaml) defines **10 phases**, in order: vision, setup, leadership, specification, implementation, testing-vision, testing-specification, testing-implementation, documentation, signoff. The manifest is the source of truth for phase ordering and advance-checks; the descriptions below are an overview.

### 1. vision (1 agent)

You describe what you want to build. The agent clarifies your intent -- spelling out what success and failure look like -- and iterates with you until it's correct and complete. You're also asked where the project lives (new or existing directory).

**User checkpoint:** approve the vision before work proceeds.

### 2. setup

The Coordinator determines the working directory, checks for existing state, and sets the per-run artifact directory via `set_artifact_dir(...)`. It then creates, under the artifact dir (commonly `.operon/{project_name}/`):
- `userprompt.md` -- your verbatim prompt + the approved vision
- `STATUS.md` -- tracks workflow progress

### 3. leadership

The Coordinator's prime directive is **"Delegate, don't do."** It spawns the 4 Leadership agents (Composability, Terminology Guardian, Skeptic, User Alignment) plus any optional supporting agents.

### 4. specification (leadership agents)

Leadership drafts a specification together, saved as `SPECIFICATION.md` under the artifact dir's `specification/` directory.

**User checkpoint:** approve the specification before implementation begins.

**Tips:**
- The coordinator should spawn one Composability agent per identified axis. If it doesn't, say: *"Start a fresh review with new agents, this time make sure to start one composability agent per identified axis."*
- Repeat reviews until no major issues remain. You can also read the specs yourself.
- Request fresh reviews if you feel the current agents have tunnel vision.

### 5. implementation (leadership + implementers)

Implementer agents write code directly in the project, guided by leadership agents.

**User checkpoint:** all Leadership agents approve implementation before testing.

**Tips:**
- One implementer per file works well.
- If only one implementer is spawned, say: *"Spawn a sufficient amount of implementer agents."*
- If leadership isn't actively guiding, say: *"Remember to inform the leadership agents that implementation has started and that it is their role to guide the implementers."*

### 6-8. testing-vision / testing-specification / testing-implementation

Testing is split into three phases: agree on what to test (`userprompt_testing.md`), write the test specification (`TEST_SPECIFICATION.md`), then write and run the tests until all pass and comply with the testing standard.

**User checkpoint:** the testing-vision and testing-specification phases each require user approval. By default, agents write "smoke" tests with short runtimes. E2E tests run full real-world use cases but aren't always reliable -- sometimes it's faster to run them yourself.

### 9. documentation

The Coordinator ensures docs are accurate, complete, and reviewed by Leadership.

### 10. signoff

All agents confirm READY; final integration, E2E check, and user approval.

---

## Agent Roster

### Leadership

| Agent | File | Role |
|-------|------|------|
| **Coordinator** | `coordinator/identity.md` | Orchestrates the workflow. Delegates, doesn't implement. |
| **Composability** | `composability/identity.md` | Dissects problems into independent axes with defined seams. The most important leadership agent. |
| **Terminology Guardian** | `terminology/identity.md` | Ensures consistent naming across components. |
| **Skeptic** | `skeptic/identity.md` | Checks for completeness and minimality. Correctness through simplicity. |
| **User Alignment** | `user_alignment/identity.md` | Protects user intent. Has veto power -- cannot remove user-requested features. |

### Implementation

| Agent | File | Role |
|-------|------|------|
| **Implementer** | `implementer/identity.md` | Writes the code. Multiple instances spawned (one per file works well). |
| **Test Engineer** | `test_engineer/identity.md` | Writes tests and CI configuration. |
| **UI Designer** | `ui_designer/identity.md` | Interface design (spawned when applicable). |

### Advisory (spawned when applicable)

| Agent | File | Role |
|-------|------|------|
| **Researcher** | `researcher/identity.md` | Investigates technical questions, surveys approaches. |
| **Binary Portability** | `binary_portability/identity.md` | Cross-language/platform compatibility. |
| **Sync Coordinator** | `sync_coordinator/identity.md` | Concurrency correctness (only for concurrent systems). |
| **Memory Layout** | `memory_layout/identity.md` | Data structure and memory optimization. |
| **Lab Notebook** | `lab_notebook/identity.md` | Documents experiments and decisions. |

---

## Key Rules

1. **Setup runs first** -- `userprompt.md` and `STATUS.md` are created in the setup phase (enforced by its `file-exists-check` advance-checks) before coding begins
2. **User Alignment has veto** -- cannot remove user-requested features
3. **Skeptic ensures correctness** -- complete, simple, verifiable; no shortcuts
4. **Composability drives architecture** -- independent axes, defined seams, no hidden coupling
5. **Advisory agents are non-blocking** -- spawned only when relevant, don't hold up the workflow
