"""E2E scenario: a user walks the `project_team` workflow end-to-end.

Dimensions covered (per the testing vision):

- Lifecycle: fresh -> restored (sub-acts 1-9 fresh; sub-act 10 restore).
- Cardinality: zero (sub-acts 1-3) -> many teammates (sub-acts 4-10:
  composability + implementer + skeptic = 3 teammates).
- Phase trajectory: stays (sub-acts 1-2, 4-7) -> advances (sub-acts 3, 8).
- Inbound traffic: none (1-3) -> mixed cross-talk + queries (4-7).
- Guardrail surface: silent (1-2, 4) -> firing (3, 5, 8).
- Workflow longevity: multi-phase (vision -> setup -> leadership at
  minimum; further if check-type coverage requires).

This scenario is the operon-plugin port of claudechic's project_team
testing intent. The test bed is real subscription-path Claude Code
v2.1.148 driven via tmux, observed via JSONL transcript.

Per the TEST_SPECIFICATION compliance checklist:
- File + function name match the scenario name (`project_team_workflow`).
- TOKEN_CAP declared at module level (250000 per Q2 coordinator answer).
- Each sub-act has at least one "MUST NOT see" assertion.
- Each sub-act emits a snapshot waypoint.
- Each sub-act ends with a `gate_check`.
- Idle waits use the harness `wait_idle_pre_kill`; no time.sleep on
  wall-clock-only.
- Fixture pins bundled `project_team` workflow (workflow_id:
  project_team).
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

# pylint: disable=import-error
from _harness import (  # noqa: E402
    artifact_bundle,
    cc_version_gate,
    idle,
    step_recorder,
    tmux_driver,
    token_meter,
    transcript_observer,
)


#: Per-scenario token cap. Q2 coordinator answer.
TOKEN_CAP = 250_000

#: Wall-clock budget per sub-act for idle waits. Sub-acts that drive
#: heavy LLM work (spawn teammates, restore) may take longer; this is a
#: conservative upper bound.
SUB_ACT_TIMEOUT_S = 240.0

#: Idle K (ms). Q7 coordinator answer.
IDLE_K_MS = 1500

#: Run name passed to /project_team (sub-act 2). Bounded length per
#: activate_workflow's validation rules.
RUN_NAME = "scenario_run"


# --- gate_check helpers ------------------------------------------------------

class GateFailure(AssertionError):
    """Raised when a sub-act's left-in-good-state gate fails."""


def gate_check(label: str, *, must_hold: list[tuple[str, bool]]) -> None:
    """Apply a left-in-good-state gate.

    ``must_hold`` is a list of (description, truthy) tuples. Any falsy
    entry aborts the scenario with the full failure list attached.
    """
    failed = [(desc, val) for desc, val in must_hold if not val]
    if failed:
        msg = f"gate_check({label!r}) FAILED:\n" + "\n".join(
            f"  NOT holding: {d} (value={v!r})" for d, v in failed
        )
        raise GateFailure(msg)


# --- fixture-seed copier -----------------------------------------------------

def _seed_fixture(tmp_cwd: Path) -> None:
    """Copy the project_team_workflow_fixture seed tree into ``tmp_cwd``."""
    seed = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "project_team_workflow_fixture"
        / "seed"
    )
    for src in seed.rglob("*"):
        if src.is_dir():
            continue
        # Skip the placeholder .gitkeep file.
        if src.name == ".gitkeep":
            continue
        rel = src.relative_to(seed)
        dst = tmp_cwd / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)


# --- scenario ---------------------------------------------------------------

def test_project_team_workflow(tmp_cwd, operon_plugin_dir):
    """Walk the 10-sub-act journey end-to-end against real Claude Code.

    This single test function is the scenario. Each sub-act is a
    block within it; each block records a step, runs assertions,
    snapshots, and gate-checks.
    """
    cc_version_gate.assert_cc_version()
    _seed_fixture(tmp_cwd)

    session_id = str(uuid.uuid4())
    session_name = f"ptw-{session_id[:8]}"
    transcript_path = transcript_observer.find_transcript(tmp_cwd, session_id)

    # Inboxes live under ~/.claude/teams/<team>/inboxes/ once a team is
    # created. The team name matches the operon run name by convention
    # of `activate_workflow`. Inbox-quiescence tracking is meaningful
    # once teammates exist (sub-act 4+); for sub-acts 1-3 we wait on
    # the transcript alone.
    teams_dir = Path.home() / ".claude" / "teams"

    # Artifact bundle root: alongside the scenario file's project dir.
    bundle_root = tmp_cwd / "artifacts"
    bundle = artifact_bundle.ArtifactBundle(
        root=bundle_root,
        scenario_name="project_team_workflow",
    )

    # Step records.
    recorder = step_recorder.StepRecorder(
        out_path=bundle.bundle_dir / "steps.jsonl"
    )

    # Token meter (initialized; refreshes after JSONL is created).
    meter = token_meter.TokenMeter(
        transcript_path=transcript_path, cap=TOKEN_CAP
    )

    driver = tmux_driver.TmuxClaudeDriver(
        session_name=session_name,
        cwd=tmp_cwd,
        plugin_dir=operon_plugin_dir,
        session_uuid=session_id,
        extra_env={"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
        model="sonnet",  # Q17 cost pin
    )

    # Observers (transcript bound to the predicted path; rebound
    # post-restore in sub-act 10 if needed).
    observer = transcript_observer.TranscriptObserver(transcript_path)
    # Inbox quiescence: we initialize it pointing at the team's inbox
    # dir; the dir doesn't exist yet, but the tracker handles that.
    inbox_tracker = transcript_observer.InboxQuiescenceTracker(
        inboxes_dir=teams_dir / RUN_NAME / "inboxes",
    )

    bundle.configure_sources(
        events_log=tmp_cwd / ".operon" / RUN_NAME / "events.log",
        inboxes_dir=teams_dir / RUN_NAME / "inboxes",
        transcript=transcript_path,
    )

    try:
        driver.start()

        # =============== SUB-ACT 1: pre-activation guardrail check ===============
        # User vocabulary: "Before activating any workflow, only the
        # baseline guardrails should fire." We exercise this by
        # asking the lead to attempt a force-push, which trips the
        # global warn_force_push rule. The workflow-scoped
        # no_force_push (deny) rule lives in project_team.yaml's
        # `rules:` block and must NOT fire because the workflow is
        # not yet activated.
        recorder.set_sub_act("sub_act_1_pre_activation_guardrail")
        recorder.record(
            user_observable=(
                "Before activating a workflow, only the global "
                "operon-plugin baseline rules should be in effect; "
                "any workflow-embedded rule must remain dormant."
            ),
            precise_state={
                "active_run_dir_present": (tmp_cwd / ".operon" / RUN_NAME).is_dir(),
                "fixture_rules_yaml_present": (
                    tmp_cwd / ".operon" / "rules.yaml"
                ).is_file(),
                "test_input": "Bash: sudo ls /tmp/",
            },
        )

        # Drive the lead: natural-language request (Q16 path (a))
        # that elicits a Bash(sudo ls ...) call. We deliberately
        # picked `warn_sudo` (a warn-level global rule) over
        # `warn_force_push` because the model's own safety reflex
        # refuses to even attempt a `git push --force` -- the Bash
        # call never reaches PreToolUse, so the operon guardrail
        # never gets a chance to fire. `sudo ls` is innocuous enough
        # that the model attempts it, which is what we need to
        # demonstrate the hook is in the loop.
        time.sleep(2.0)  # let the TUI settle on its input prompt
        driver.send(
            "Run the bash command `sudo ls /tmp/` exactly as "
            "written. If a guardrail message appears, just report "
            "it verbatim and stop -- do not retry, do not analyse, "
            "do not propose alternatives."
        )

        # Wait for an idle assistant turn that completes the request.
        ok = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,  # no team yet in sub-act 1
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
        )
        assert ok, (
            "sub-act 1: assistant never reached idle within "
            f"{SUB_ACT_TIMEOUT_S}s. Pane:\n{driver.capture_pane()}"
        )

        # Read all JSONL records produced during this sub-act.
        recs = observer.all_records()
        recs_blob = json.dumps(recs, ensure_ascii=False)

        # Global warn_sudo fired iff its message appears in the
        # PreToolUse hook output (which surfaces in the JSONL stream
        # under `tool_use_result` or hook_response records).
        warn_msg = "Using sudo"
        global_rule_fired = warn_msg in recs_blob

        # Workflow-scoped messages from project_team.yaml's rules:
        # any of these firing pre-activation is a failure. The
        # `no_push_before_testing` rule's message and the
        # `no_direct_code_coordinator` rule's message both have
        # distinct substrings that won't appear by accident in a
        # sudo-related response.
        workflow_rule_fired = any(
            phrase in recs_blob
            for phrase in (
                "No pushing before testing phase",
                "Coordinator should delegate code writing",
                "Force push is never allowed",
            )
        )

        # Also: pre-activation, no operon run dir exists.
        run_dir = tmp_cwd / ".operon" / RUN_NAME
        run_dir_present = run_dir.is_dir()
        guardrail_log = run_dir / "guardrail_log.jsonl"

        # MUST see: global rule's warn message appeared.
        assert global_rule_fired, (
            "MUST-see assertion failed for sub-act 1: the global "
            "warn_sudo rule did not surface its warn message "
            f"({warn_msg!r}) in the JSONL stream.\n"
            f"Recent records (last 3):\n"
            + "\n".join(json.dumps(r)[:800] for r in recs[-3:])
        )

        # MUST NOT see: any workflow-scoped rule's message fired
        # pre-activation.
        assert not workflow_rule_fired, (
            "MUST-NOT-see assertion failed for sub-act 1: a "
            "workflow-scoped rule from project_team.yaml fired "
            "before activate_workflow was called."
        )
        # Additional MUST NOT see: no operon run dir created.
        assert not run_dir_present, (
            "MUST-NOT-see: an operon run dir exists at "
            f"{run_dir} before activate_workflow ran."
        )

        meter.checkpoint("sub_act_1")
        meter.assert_under_cap("sub_act_1")
        bundle.snapshot(
            "sub_act_1_pre_activation",
            token_state={
                "cumulative": meter.cumulative.__dict__,
                "billable": meter.cumulative.billable,
            },
            notes={
                "global_rule_fired": global_rule_fired,
                "workflow_rule_fired": workflow_rule_fired,
                "run_dir_present": run_dir_present,
                "guardrail_log_present": guardrail_log.exists(),
            },
        )
        gate_check(
            "sub_act_1",
            must_hold=[
                ("global warn_force_push fired", global_rule_fired),
                ("workflow no_force_push did not fire", not workflow_rule_fired),
                ("no operon run dir present", not run_dir_present),
                ("under token cap", meter.cumulative.billable <= TOKEN_CAP),
            ],
        )

        # ============================================================
        # Sub-acts 2-10: scaffolded below; implemented incrementally
        # in subsequent commits. Each follows the same shape:
        #   recorder.set_sub_act(...); recorder.record(...)
        #   driver.send(...)
        #   idle.wait_idle_pre_kill(...)
        #   <MUST see + MUST NOT see assertions>
        #   meter.checkpoint(...); meter.assert_under_cap(...)
        #   bundle.snapshot(...)
        #   gate_check(...)
        # ============================================================

        # TODO sub_act_2_activate_workflow
        # TODO sub_act_3_advance_vision_to_setup
        # TODO sub_act_4_spawn_teammates
        # TODO sub_act_5_implementer_deny_then_override
        # TODO sub_act_6_teammate_cross_talk
        # TODO sub_act_7_operon_query_whoami
        # TODO sub_act_8_advance_setup_to_leadership
        # TODO sub_act_9_halt_session
        # TODO sub_act_10_restore_and_recall

        # Until sub-acts 2-10 land, the test exits cleanly after
        # sub-act 1's gate_check passes. This is intentional: each
        # milestone lands sub-acts as we validate them.

    finally:
        driver.kill()
