# Testing

operon's test suite is end-to-end: it drives **real subscription-path
Claude Code** through a controlling pty and observes the JSONL transcript,
rather than mocking the runtime. The tests live in `tests/`; the
reusable machinery lives in `tests/_harness/`. This page covers how the
suite is structured, how to run it, and what the harness gives you.

## Layout

```
tests/
  conftest.py                         # fixtures: driver selection, tmp cwd, plugin dir
  scenarios/
    project_team_workflow.py          # the project_team end-to-end scenarios
    _smoke_test_harness.py            # harness self-check (launch claude, /exit, verify transcript)
  _harness/
    pty_driver.py                     # default driver: gives CC a controlling pty, renders ANSI with pyte
    tmux_driver.py                    # legacy driver (sub-acts 1-4 only)
    transcript_observer.py            # reads the JSONL transcript as the observation surface
    idle.py                           # idle predicates (no time.sleep for synchronization)
    inbox_watcher.py                  # drives lead-pane sub-acts via the inbox channel
    token_meter.py                    # enforces the per-test token cap
    cc_version_gate.py                # asserts the pinned Claude Code version
    step_recorder.py                  # per-sub-act snapshot waypoints
    artifact_bundle.py                # per-sub-act forensic bundle
  fixtures/
    project_team_workflow_fixture/    # filesystem seed for the scenario
```

There are no in-process unit tests today: every test boots a real Claude
Code session. The `_smoke_test_harness.py` file is a fast self-check for
the harness itself (launch, `/exit`, confirm the transcript appears); it
is not one of the spec'd scenarios but sorts first so you can validate the
harness before spending tokens on a full run.

## The two project_team scenarios

`project_team_workflow.py` holds two tests, split (per the Land 13
diagnostic) because LLM context saturation across the middle sub-acts
caused the original single 10-sub-act test to misread the later
`advance_phase` prompt:

- `test_project_team_coordination_chain` -- sub-acts 1-7: pre-activation
  guardrail firing through teammate cross-talk and identity queries.
  Stops cleanly at sub-act 7's gate. ~50K cumulative tokens.
- `test_project_team_advance_lifecycle` -- sub-acts 8-10 from a fresh
  state with minimal scaffolding: a clean-room advance lifecycle
  (activate -> vision->setup advance -> spawn, then halt and restore).
  ~80-120K cumulative tokens.

Together they preserve coverage of all originally-tested behaviors:
lifecycle (fresh -> restored), teammate cardinality (zero -> many),
phase trajectory, inbound traffic (none -> mixed cross-talk + queries),
and guardrail surface (silent -> firing).

## Scenario discipline

Each sub-act in a scenario follows the same shape (enforced by the
harness, checked against the test specification):

- A per-test **token cap** declared at module level; `token_meter`
  enforces it so a runaway LLM does not burn the budget.
- At least one **"MUST NOT see"** assertion per sub-act (a negative
  assertion, not just a positive one).
- A **snapshot waypoint** emitted per sub-act (`step_recorder`).
- A `gate_check` closing each sub-act.
- Idle waits via the harness idle predicates -- **no `time.sleep`** for
  wall-clock-only synchronization.
- The fixture pins the bundled `project_team` workflow.

When you add a sub-act, match this discipline; the harness modules exist
specifically to make each item cheap to satisfy.

## Drivers

Two drivers implement the same interface; select with the
`OPERON_TEST_DRIVER` env var or the `--driver` pytest flag (default
`pty`):

- **pty** (`pty_driver.PtyClaudeDriver`) -- the default. Gives Claude Code
  a controlling pty and renders the alt-screen ANSI stream with `pyte`, so
  the harness can read the team-panel widget. Required for sub-acts 5+,
  where teammate subprocesses are visible. Needs `pexpect` + `pyte` (in
  the `dev` dependency group).
- **tmux** (`tmux_driver.TmuxClaudeDriver`) -- legacy. Works for sub-acts
  1-4 but cannot present the lead's TUI once teammate subprocesses overlay
  the shared pane.

If `pty` is selected without `pexpect`/`pyte` installed, `conftest.py`
fails loudly with the `uv sync --group dev` fix.

## Version gate

The harness pins a Claude Code version (`cc_version_gate.PINNED_CC_VERSION`,
currently `2.1.150`) and asserts `claude --version` matches before a
scenario runs. The pin has moved as CC auto-updated; when a scenario
breaks after a CC update, check this gate first -- a version drift is a
likely cause, and the gate makes it explicit rather than a mysterious
mid-run failure.

## Running the suite

The suite is uv-managed via `pyproject.toml`.

```bash
uv sync --dev                       # install pytest + pexpect + pyte
uv run pytest tests/scenarios/_smoke_test_harness.py -v --timeout=120
uv run pytest tests/scenarios/project_team_workflow.py -v
```

pytest discovery is widened in `pyproject.toml` (`python_files` includes
`*_workflow.py` and `_smoke_*.py`) because the scenario files are named for
their scenario, not `test_*.py`.

!!! warning "These tests cost tokens"
    Each scenario boots a real Claude Code session and drives a real LLM,
    so a full run consumes subscription tokens and wall-clock minutes.
    During development, run the single test or sub-act relevant to your
    change, not the whole suite. The `tmp_cwd` fixture leaves its
    throwaway directory under `/tmp/operon-tests/` on failure for
    forensics and cleans it up on success.

## What the artifact-dir token relies on

`${ARTIFACT_DIR}` is the sole template token for the artifact directory
(see [Workflow Engine](workflow-engine.md#the-artifact_dir-token)). The
load-bearing property -- that `${ARTIFACT_DIR}` expands to the run's
artifact dir at advance-check time -- is covered by the existing scenario
suite, which exercises that substitution through the live `project_team`
manifest, rather than by a dedicated unit test. A green suite is the
confirmation.
