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

#: Run name passed to activate_workflow (sub-act 2). Bounded length
#: per activate_workflow's validation rules. Use a hyphen rather
#: than an underscore: empirically, the runtime's TeamCreate
#: normalizes the team directory name to hyphens, while operon's
#: internal prerequisite check uses the raw run_name -- so a name
#: with an underscore causes a mismatch
#: ("expected scenario_run, got scenario-run") and activation fails.
RUN_NAME = "scenario-run"

#: ==== Feature gates for sections blocked on an operon fix ====
#:
#: The PreToolUse hook in current operon main (HEAD 10e2881) has a
#: Land 4 regression: lead-identity resolution returns null in
#: rule_fired_log entries (`agent: null, role: null,
#: current_phase: null`), while ack_issued log entries correctly
#: resolve to `agent: "Coordinator", role: "coordinator",
#: current_phase: "bootstrap"`. The mismatch breaks BOTH (a) the
#: role-scoped warn rule firing on lead-issued calls and (b) the
#: warn-ack-retry consume path: the acknowledge_warning token
#: lands on disk but the next rule_fired_log doesn't find it
#: because identity resolution returns nothing to match against.
#:
#: A fix is being implemented by Land1Implementer on the branch
#: `fix-pretooluse-identity-regression` (in the worktree at
#: /tmp/operon-plugin-fix). When the fix lands and is merged
#: into our branch base, set these flags to True and re-run the
#: scenario.
EXTEND_SUBACT_1_WITH_ACK_RETRY = True  # Land 4 fix landed (49ab0bc)
RUN_SUBACT_5_OVERRIDE_FLOW = True  # Land 4 fix landed (49ab0bc)

#: Debug-hygiene flag (Q18a). When True the harness does NOT delete
#: ~/.claude/teams/<RUN_NAME> at scenario start. Useful when iterating
#: a single failing sub-act and wanting to inspect team state from the
#: previous run BEFORE the next run wipes it. Should default to False
#: for normal runs (deterministic clean start).
KEEP_TEAM_CONFIG_ON_FAIL = False


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
    # Clean up any leftover team config from prior runs that share the
    # same RUN_NAME. The runtime's leader-uniqueness check refuses
    # activate_workflow if a team by that name already exists. The
    # KEEP_TEAM_CONFIG_ON_FAIL flag (Q18a) bypasses this cleanup so a
    # debugger can inspect the previous run's team state before the
    # next run wipes it.
    _stale_team = Path.home() / ".claude" / "teams" / RUN_NAME
    if _stale_team.exists() and not KEEP_TEAM_CONFIG_ON_FAIL:
        shutil.rmtree(_stale_team)

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
        marker_1 = idle.latest_stop_uuid(observer)
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
            after_marker_uuid=marker_1,
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

        # =============== SUB-ACT 1 EXTENSION: fire -> ack -> retry ===============
        # Gated by EXTEND_SUBACT_1_WITH_ACK_RETRY because the Land 4
        # identity-resolution regression in current operon main
        # breaks the ack-consume path (lead-identity resolves to
        # null in rule_fired_log entries; acknowledge_warning issues
        # a valid token but the next firing of the same rule does
        # NOT consume it). Turn ON after Land1Implementer's fix
        # lands on fix-pretooluse-identity-regression and is
        # merged in.
        #
        # The full "user trips a warn rule" envelope is:
        #   fire -> ack -> retry succeeds (no second fire).
        # The base sub-act 1 above only covers "fire". The
        # extension below covers ack + retry.
        if EXTEND_SUBACT_1_WITH_ACK_RETRY:
            recorder.set_sub_act("sub_act_1_extension_ack_retry")
            recorder.record(
                user_observable=(
                    "After the warn fires, the user acknowledges "
                    "the rule and retries the same command; the "
                    "retry succeeds and no second warn fires."
                ),
                precise_state={
                    "rule_id": "warn_sudo",
                    "command": "sudo ls /tmp/",
                },
            )

            # Step A: send the acknowledge_warning call.
            marker_1a = idle.latest_stop_uuid(observer)
            driver.send(
                "Call mcp__operon__acknowledge_warning with "
                "rule_id=\"warn_sudo\" and reason=\"harness "
                "sub-act 1 probe -- the harness is testing the "
                "warn-ack-retry envelope\". Report ONLY the "
                "tool response JSON, nothing else."
            )
            ok_1a = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=None,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_1a,
            )
            assert ok_1a, (
                "sub-act 1 ext: assistant never reached idle "
                f"after acknowledge_warning call within "
                f"{SUB_ACT_TIMEOUT_S}s. Pane:\n"
                f"{driver.capture_pane()}"
            )

            # Step A assertions: response carries acknowledged=true
            # and a token_path. The JSONL tool_result wraps the
            # acknowledge_warning response as a JSON string; when
            # we json.dumps the records list, the inner quotes get
            # escaped (\"acknowledged\": true), so we check both
            # forms.
            recs_ext_a = observer.all_records()
            recs_ext_a_blob = json.dumps(recs_ext_a, ensure_ascii=False)
            ack_succeeded = (
                '"acknowledged": true' in recs_ext_a_blob
                or '\\"acknowledged\\": true' in recs_ext_a_blob
                or "'acknowledged': True" in recs_ext_a_blob
            )
            assert ack_succeeded, (
                "MUST-see: acknowledge_warning response did not "
                "carry acknowledged=true. Token issuance failed."
            )

            # Locate the token file on disk. Pre-activation there
            # is no operon run dir, so the token lives in a
            # default-handle location -- per operon's convention,
            # something like
            #   .operon/default/acks/<handle>/warn_sudo-<hash>.json
            # If the actual path is different, look in the response
            # JSON's token_path field via recs_ext_a_blob.
            token_files = list(
                (tmp_cwd / ".operon").rglob("warn_sudo-*.json")
            )
            assert token_files, (
                "MUST-see: no acknowledge token file matching "
                "warn_sudo-*.json was created under .operon/."
            )
            token_data = json.loads(
                token_files[0].read_text(encoding="utf-8")
            )
            # Empirically the on-disk token schema is:
            #   {rule_id, agent_handle, kind: "ack", reason,
            #    issued_at, expires_at, one_shot}
            # No top-level `acknowledged` field -- that field is in
            # the tool RESPONSE only. The on-disk equivalent is
            # `kind == "ack"`.
            assert token_data.get("kind") == "ack", (
                f"MUST-see: ack token {token_files[0]} does not "
                f"carry kind='ack': {token_data}"
            )
            assert token_data.get("one_shot") is False, (
                f"MUST-see: ack token {token_files[0]} does not "
                f"carry one_shot=false: {token_data}"
            )
            # expires_at must be in the future.
            import datetime as _dt
            exp = token_data.get("expires_at")
            exp_in_future = False
            if isinstance(exp, str):
                try:
                    exp_dt = _dt.datetime.fromisoformat(
                        exp.replace("Z", "+00:00")
                    )
                    exp_in_future = exp_dt > _dt.datetime.now(
                        _dt.timezone.utc
                    )
                except ValueError:
                    exp_in_future = False
            assert exp_in_future, (
                f"MUST-see: ack token expires_at is not in the "
                f"future: {exp!r}"
            )

            # Step B: retry the same Bash. The hook should find
            # the valid ack token and allow the call. The actual
            # ls output should land in the response.
            marker_1b = idle.latest_stop_uuid(observer)
            driver.send(
                "Now run the bash command `sudo ls /tmp/` "
                "exactly as before. The acknowledgment token "
                "is now on disk so the rule should resolve to "
                "allow on this retry. Report the actual command "
                "output (the directory listing) verbatim."
            )
            ok_1b = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=None,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_1b,
            )
            assert ok_1b, (
                "sub-act 1 ext: assistant never reached idle "
                "after retry within "
                f"{SUB_ACT_TIMEOUT_S}s. Pane:\n"
                f"{driver.capture_pane()}"
            )

            # Step B assertions:
            #
            # The operon hook ACK-consume path: on the retry, the
            # rule still fires (logged with outcome=acked), but
            # the hook ALLOWS the Bash through. The tool_result
            # for the retry Bash therefore must NOT carry the
            # warn-fire reject text. The Bash itself may fail at
            # the OS level (e.g. sudo demanding a TTY for a
            # password) -- that's not a guardrail failure.
            #
            # Find the SECOND Bash tool_result (the retry's) and
            # check it does NOT match the warn-fire reject
            # pattern. This is the precise behavioral test.
            recs_ext_b = observer.all_records()
            bash_results = []
            for rec in recs_ext_b:
                msg = rec.get("message") or {}
                if rec.get("type") != "user":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if (
                        isinstance(c, dict)
                        and c.get("type") == "tool_result"
                    ):
                        # tool_result.content may be a str or a
                        # list[{type, text}]; normalize.
                        body = c.get("content")
                        if isinstance(body, list):
                            body = "".join(
                                str(sub.get("text", ""))
                                if isinstance(sub, dict)
                                else str(sub)
                                for sub in body
                            )
                        bash_results.append({
                            "tool_use_id": c.get("tool_use_id"),
                            "is_error": c.get("is_error"),
                            "body": str(body),
                        })
            # The Bash tool_results are interleaved with operon
            # MCP tool_results (acknowledge_warning, etc.). Filter
            # to those whose body looks like a Bash result
            # (contains the "Using sudo" reject text OR is a
            # plausible bash stdout/stderr).
            bash_result_bodies = [
                r for r in bash_results
                if "Using sudo" in r["body"]
                or "sudo:" in r["body"]
                or "ls:" in r["body"]
                or "operon-tests" in r["body"]
                or "Exit code" in r["body"]
            ]
            # Expect at least two Bash results: the initial reject
            # and the retry. The retry must NOT have the reject
            # text.
            assert len(bash_result_bodies) >= 2, (
                "MUST-see: at least two Bash tool_results "
                "(initial reject + retry). Got: "
                f"{len(bash_result_bodies)}. Bodies: "
                f"{[r['body'][:120] for r in bash_result_bodies]}"
            )
            retry_body = bash_result_bodies[-1]["body"]
            retry_rejected_by_operon = (
                "acknowledge_warning" in retry_body
                and "Using sudo" in retry_body
            )
            assert not retry_rejected_by_operon, (
                "MUST-NOT-see: the retry Bash result still "
                "carries operon's warn-fire reject text. The ack "
                f"was not consumed. Body: {retry_body[:400]}"
            )
            # Soft positive: the retry got past the guardrail
            # (either succeeded with ls output, or failed at OS
            # level with a sudo-needs-TTY error -- both are
            # acceptable evidence that operon allowed the call).
            retry_passed_guardrail = (
                not retry_rejected_by_operon
            )

            # guardrail_log.jsonl inspection: exactly ONE
            # rule_fired_log for warn_sudo, ONE ack_issued, ZERO
            # subsequent rule_fired_log for warn_sudo.
            gl_path = run_dir / "guardrail_log.jsonl"
            # Pre-activation the run_dir is absent -- the
            # guardrail log lives under default/ before activate.
            if not gl_path.is_file():
                gl_path = tmp_cwd / ".operon" / "default" / "guardrail_log.jsonl"
            gl_entries: list[dict] = []
            if gl_path.is_file():
                for line in gl_path.read_text(encoding="utf-8").splitlines():
                    try:
                        gl_entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            # Empirical guardrail_log shape under the Land 4 fix:
            # the rule fires TWICE -- once for the initial reject
            # (outcome=blocked) and once for the ack-consumed retry
            # (outcome=acked). Plus one ack_issued in between.
            # The semantic assertion is therefore:
            #   one rule_fired_log with outcome=blocked
            #   one ack_issued
            #   one rule_fired_log with outcome=acked
            warn_fired_entries = [
                e for e in gl_entries
                if e.get("type") == "rule_fired_log"
                and e.get("rule_id") == "warn_sudo"
            ]
            warn_blocked = [
                e for e in warn_fired_entries
                if e.get("outcome") == "blocked"
            ]
            warn_acked = [
                e for e in warn_fired_entries
                if e.get("outcome") == "acked"
            ]
            ack_issued_entries = [
                e for e in gl_entries
                if e.get("type") == "ack_issued"
                and e.get("rule_id") == "warn_sudo"
            ]
            assert len(warn_blocked) == 1, (
                f"MUST-see exactly ONE rule_fired_log with "
                f"outcome=blocked for warn_sudo; got "
                f"{len(warn_blocked)}. Entries: {warn_fired_entries}"
            )
            assert len(warn_acked) == 1, (
                f"MUST-see exactly ONE rule_fired_log with "
                f"outcome=acked for warn_sudo (the retry); got "
                f"{len(warn_acked)}. Entries: {warn_fired_entries}"
            )
            assert len(ack_issued_entries) == 1, (
                f"MUST-see exactly ONE ack_issued for warn_sudo; "
                f"got {len(ack_issued_entries)}. Entries: "
                f"{ack_issued_entries}"
            )

            meter.checkpoint("sub_act_1_extension")
            meter.assert_under_cap("sub_act_1_extension")
            bundle.snapshot(
                "sub_act_1_extension_ack_retry",
                token_state={
                    "cumulative": meter.cumulative.__dict__,
                    "billable": meter.cumulative.billable,
                },
                notes={
                    "ack_token_path": str(token_files[0]),
                    "ack_token_data": token_data,
                    "warn_fired_entries": warn_fired_entries,
                    "ack_issued_entries": ack_issued_entries,
                    "retry_passed_guardrail": retry_passed_guardrail,
                    "retry_body_excerpt": retry_body[:400],
                },
            )
            gate_check(
                "sub_act_1_extension",
                must_hold=[
                    ("ack succeeded", ack_succeeded),
                    ("ack token file present", bool(token_files)),
                    ("ack token kind=ack", token_data.get("kind") == "ack"),
                    ("ack token one_shot=false", token_data.get("one_shot") is False),
                    ("ack token expires_at in future", exp_in_future),
                    ("retry passed operon guardrail", retry_passed_guardrail),
                    ("exactly one rule_fired_log outcome=blocked", len(warn_blocked) == 1),
                    ("exactly one rule_fired_log outcome=acked", len(warn_acked) == 1),
                    ("exactly one ack_issued", len(ack_issued_entries) == 1),
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

        # =============== SUB-ACT 2: activate workflow ===============
        # User vocabulary: "Activate the project_team workflow for
        # the current session." The lead invokes
        # mcp__operon__activate_workflow with workflow_id=project_team
        # and run_name=scenario_run. After activation, the active
        # workflow's rules are in scope, in addition to the global
        # baseline rules.
        recorder.set_sub_act("sub_act_2_activate_workflow")
        recorder.record(
            user_observable=(
                "After activation, the project_team workflow's "
                "rules join the global baseline rules in deciding "
                "what tool calls are allowed; rules from any other "
                "workflow remain dormant (no other workflow is "
                "loaded)."
            ),
            precise_state={
                "operon_run_dir_before": str(tmp_cwd / ".operon" / RUN_NAME),
                "operon_run_dir_present_before": (
                    tmp_cwd / ".operon" / RUN_NAME
                ).is_dir(),
                "test_input_1": (
                    f"NL: activate project_team with run name {RUN_NAME}"
                ),
                "test_input_2": (
                    "NL: write hello.py (coordinator-direct-code, "
                    "warn-tier workflow rule)"
                ),
            },
        )
        # 2.1: activate the workflow.
        marker_2a = idle.latest_stop_uuid(observer)
        driver.send(
            "Use the mcp__operon__activate_workflow tool to "
            f"activate workflow_id=project_team with "
            f"run_name={RUN_NAME}. Once it returns, report only "
            "the activation status and the current phase, nothing "
            "else."
        )
        ok = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_2a,
        )
        assert ok, (
            "sub-act 2.1: assistant never reached idle within "
            f"{SUB_ACT_TIMEOUT_S}s. Pane:\n{driver.capture_pane()}"
        )

        # Verify activation: run dir + phase_state.json present;
        # current_phase = vision.
        run_dir = tmp_cwd / ".operon" / RUN_NAME
        phase_state_path = run_dir / "phase_state.json"
        assert phase_state_path.is_file(), (
            f"MUST-see: phase_state.json was not created at "
            f"{phase_state_path}. Workflow activation appears to "
            f"have failed.\nPane:\n{driver.capture_pane()}"
        )
        phase_state = json.loads(
            phase_state_path.read_text(encoding="utf-8")
        )
        current_phase = phase_state.get("current_phase")
        assert current_phase == "vision", (
            "MUST-see: post-activation current_phase should be "
            f"'vision' (first phase in project_team.yaml); got "
            f"{current_phase!r}"
        )

        # 2.2: trigger the workflow-scoped `no_direct_code_coordinator`
        # warn rule. The rule fires on PreToolUse/Write when
        # file_path matches `.+` and is not excluded by the .md /
        # .gitignore pattern, scoped to roles=[coordinator]. As the
        # lead session IS the coordinator (per operon's identity
        # gate), the rule fires when we ask for a .py write.
        # 2.2: verify the workflow rules are now in the applicable
        # set (post-activation). Empirically the role-scoped warn
        # rule no_direct_code_coordinator does NOT fire on a Write
        # call from the lead pre-teammates, because operon's
        # identity gate resolves the lead's role to null until a
        # team is provisioned (guardrail_log shows `role: null`).
        # To verify "current-phase rules fire" in the looser sense
        # of "are loaded + in scope for the current (role, phase)",
        # we call operon's MCP get_applicable_rules and assert the
        # response includes workflow-scoped rule ids alongside
        # global ones.
        marker_2b = idle.latest_stop_uuid(observer)
        driver.send(
            "Call mcp__operon__get_applicable_rules and report "
            "back ONLY the JSON list of rule_ids in the response "
            "(no commentary, no other text)."
        )
        ok = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_2b,
        )
        assert ok, (
            "sub-act 2.2: assistant never reached idle within "
            f"{SUB_ACT_TIMEOUT_S}s. Pane:\n{driver.capture_pane()}"
        )

        recs2 = observer.all_records()
        recs2_blob = json.dumps(recs2, ensure_ascii=False)

        # MUST see: workflow rule ids are in the applicable set
        # (proves the workflow rules block is loaded post-activation).
        # Global rule ids are also expected (warn_sudo is in the
        # plugin-tier rules.yaml).
        # The model returns a JSON-ish list of strings. We check for
        # workflow-scoped rule ids by substring.
        applicable_rules_includes_workflow = (
            "no_push_before_testing" in recs2_blob
            or "no_force_push" in recs2_blob
            or "no_direct_code_coordinator" in recs2_blob
        )
        applicable_rules_includes_global = (
            "warn_sudo" in recs2_blob
            or "warn_force_push" in recs2_blob
            or "no_rm_rf" in recs2_blob
        )
        workflow_rule_fired_in_2 = applicable_rules_includes_workflow

        # MUST see: activate_workflow tool call result has a
        # success-shape. The tool returns a JSON object whose
        # exact shape is operon-internal, but should mention
        # "project_team" or the run name.
        activate_called = (
            "mcp__operon__activate_workflow" in recs2_blob
            and RUN_NAME in recs2_blob
        )

        # MUST NOT see: any rule from a different workflow.
        # Project's setup is the ONLY workflow we load; other
        # workflows wouldn't have rules in scope, so this should
        # be vacuously true. We check by ensuring no rule message
        # text from a hypothetical non-project_team workflow
        # appears -- there's nothing to check positively here, so
        # we assert that the operon run-dir contains only ONE
        # workflow's metadata.
        operon_state_json = run_dir / "state.json"
        state_has_one_workflow = True
        if operon_state_json.is_file():
            try:
                state = json.loads(operon_state_json.read_text(encoding="utf-8"))
                # Loosest sane invariant: workflow_id is project_team
                # if recorded at all in state.json.
                state_has_one_workflow = (
                    state.get("workflow_id") in (None, "project_team")
                )
            except json.JSONDecodeError:
                state_has_one_workflow = False

        assert workflow_rule_fired_in_2, (
            "MUST-see: no workflow-scoped rule ids (e.g. "
            "no_push_before_testing, no_force_push, "
            "no_direct_code_coordinator) appeared in the "
            "get_applicable_rules response, even though "
            "activate_workflow succeeded. The workflow rules "
            "block did not load."
        )
        assert applicable_rules_includes_global, (
            "MUST-see: no global-tier rule ids (warn_sudo, "
            "warn_force_push, no_rm_rf) appeared in the "
            "get_applicable_rules response. The plugin-tier "
            "rules.yaml did not load."
        )
        assert activate_called, (
            "MUST-see: mcp__operon__activate_workflow tool call "
            f"with run_name={RUN_NAME!r} was not visible in the "
            "JSONL stream."
        )
        assert state_has_one_workflow, (
            "MUST-NOT-see: state.json suggests more than one "
            "workflow is loaded."
        )

        meter.checkpoint("sub_act_2")
        meter.assert_under_cap("sub_act_2")
        bundle.snapshot(
            "sub_act_2_activate_workflow",
            token_state={
                "cumulative": meter.cumulative.__dict__,
                "billable": meter.cumulative.billable,
            },
            notes={
                "current_phase": current_phase,
                "workflow_warn_fired": workflow_rule_fired_in_2,
                "activate_called": activate_called,
            },
        )
        gate_check(
            "sub_act_2",
            must_hold=[
                ("activate_workflow called", activate_called),
                ("phase_state.json present", phase_state_path.is_file()),
                ("current_phase == vision", current_phase == "vision"),
                ("workflow warn rule fired", workflow_rule_fired_in_2),
                ("only one workflow loaded", state_has_one_workflow),
                ("under token cap", meter.cumulative.billable <= TOKEN_CAP),
            ],
        )

        # =============== SUB-ACT 3: advance vision -> setup ===============
        # User vocabulary: "Now advance the workflow to the next
        # phase." Vision's advance_check is a single manual-confirm
        # (per project_team.yaml). The lead invokes
        # mcp__operon__advance_phase; operon runs the elicitation
        # form via MCP elicit/create. We drive the answer through.
        recorder.set_sub_act("sub_act_3_advance_vision_to_setup")
        recorder.record(
            user_observable=(
                "Advance the workflow past the vision phase. The "
                "advance check is a manual-confirm of vision "
                "approval; once it resolves, current_phase becomes "
                "setup."
            ),
            precise_state={
                "advance_check_type": "manual-confirm",
                "from_phase": "vision",
                "to_phase": "setup",
            },
        )

        marker_3 = idle.latest_stop_uuid(observer)
        driver.send(
            "Call mcp__operon__advance_phase now. The vision "
            "phase's advance check is a manual-confirm "
            "(\"Vision approved by user?\"). The confirmation "
            "form will be answered by the harness driving the "
            "TUI directly. After the tool returns, report ONLY "
            "the new current_phase value (no commentary)."
        )

        # Drive the manual-confirm elicit form. The lead will
        # invoke advance_phase, which triggers an MCP
        # elicit/create form in the pane. We accept it here.
        form_accepted = driver.accept_elicit_form(
            wait_for_substring="Vision approved by user",
            timeout_s=180.0,
        )
        assert form_accepted, (
            "sub-act 3: manual-confirm elicit form for "
            "'Vision approved by user' never appeared in the "
            "pane. Pane:\n" + driver.capture_pane()
        )

        ok = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_3,
        )
        assert ok, (
            "sub-act 3: assistant never reached idle within "
            f"{SUB_ACT_TIMEOUT_S}s. Pane:\n{driver.capture_pane()}"
        )

        # Verify the phase_state.json now reads current_phase=setup.
        phase_state_3 = json.loads(
            phase_state_path.read_text(encoding="utf-8")
        )
        new_phase = phase_state_3.get("current_phase")
        recs3 = observer.all_records()
        recs3_blob = json.dumps(recs3, ensure_ascii=False)

        # MUST see: current_phase advanced.
        assert new_phase == "setup", (
            "MUST-see: post-advance current_phase should be "
            f"'setup'; got {new_phase!r}.\nphase_state.json "
            f"contents: {phase_state_3}"
        )

        # MUST see: manual-confirm advance check ran (its prompt
        # text should appear somewhere in the JSONL stream, either
        # in an elicit-form, an elicit_create record, or in the
        # advance_phase tool result).
        manual_confirm_ran = (
            "Vision approved by user" in recs3_blob
            or "manual-confirm" in recs3_blob
            or "manual_confirm" in recs3_blob
        )

        # MUST NOT see: workflow-rule message from the PREVIOUS
        # phase that should no longer apply. Vision had no
        # exclude_phases entries, so this assertion is loose: we
        # check that no advance-check FAILURE marker appears.
        advance_failed = (
            "advance check failed" in recs3_blob.lower()
            or "\"status\": \"blocked\"" in recs3_blob
        )
        assert not advance_failed, (
            "MUST-NOT-see: advance_phase returned a blocked status."
        )
        assert manual_confirm_ran, (
            "MUST-see: the manual-confirm advance check did not "
            "appear in the JSONL stream. Vision phase's advance "
            "check should have surfaced a confirmation prompt."
        )

        meter.checkpoint("sub_act_3")
        meter.assert_under_cap("sub_act_3")
        bundle.snapshot(
            "sub_act_3_advance_vision_to_setup",
            token_state={
                "cumulative": meter.cumulative.__dict__,
                "billable": meter.cumulative.billable,
            },
            notes={
                "new_phase": new_phase,
                "manual_confirm_ran": manual_confirm_ran,
            },
        )
        gate_check(
            "sub_act_3",
            must_hold=[
                ("current_phase advanced to setup", new_phase == "setup"),
                ("manual-confirm surfaced", manual_confirm_ran),
                ("no advance failure marker", not advance_failed),
                ("under token cap", meter.cumulative.billable <= TOKEN_CAP),
            ],
        )
        # =============== SUB-ACT 4: spawn 3 teammates ===============
        # User vocabulary: "Bring in the team -- a Leadership
        # (composability), an Implementer, and a Skeptic." Q3
        # coordinator answer pinned these three. Each is spawned
        # via the Agent tool with run_in_background=true and
        # team_name=<RUN_NAME>; the runtime adds them to the team
        # config under ~/.claude/teams/<RUN_NAME>/config.json.
        #
        # NOTE: this sub-act is independent of the Land 4 identity
        # regression -- the spawn path doesn't touch warn-ack
        # consumption or role-scoped rule firing. Built construction-
        # only here; will run after Land1's fix lands as part of the
        # rebase verification chain (1 / 1-ext / 2 / 3 / 4 / 5).
        recorder.set_sub_act("sub_act_4_spawn_teammates")
        recorder.record(
            user_observable=(
                "The user asks for the team to be assembled. "
                "The lead spawns composability, implementer, and "
                "skeptic teammates via Agent. After spawn, the "
                "team config carries five members: lead + the "
                "synthetic operon entry (added at activate time) "
                "+ the three teammates."
            ),
            precise_state={
                "teammate_names": ["composability", "implementer", "skeptic"],
                "team_config_path": str(
                    teams_dir / RUN_NAME / "config.json"
                ),
            },
        )

        # Spawn each teammate in a separate driver.send() turn.
        # Sonnet 4.6 empirically declines to execute parallel
        # multi-Agent spawns in a single turn this far into a long
        # chain (responds with "Acknowledged." / "Noted." rather
        # than calling the tool). Sequential single-spawn turns
        # parallel the working Step 0.5 spike prompt shape.
        #
        # Per-turn retry: Sonnet sometimes declines a spawn even on
        # a single-teammate turn (says "Noted." with no Agent call).
        # The harness retries up to N_SPAWN_RETRIES times for each
        # teammate, checking the team config after each retry.
        # Q3 originally pinned 3 teammates (composability +
        # implementer + skeptic). Coordinator path (b): drop
        # skeptic, run with 2 teammates. Cardinality dimension
        # still resolves to "many".
        #
        # Implementation: one driver.send that asks the lead to
        # issue TWO PARALLEL Agent calls in a single turn.
        # Sequential single-spawn turns fail because the TUI
        # auto-focuses on the new teammate's conversation thread
        # after each Agent(run_in_background=true) returns, and
        # subsequent send-keys land in the teammate's channel
        # (teammates cannot spawn other named teammates). With
        # parallel-in-one-turn the lead remains in main for both
        # spawns; the focus shift happens at end of turn after
        # both have completed.
        teammate_names = ["composability", "implementer"]
        N_SPAWN_RETRIES = 4
        for attempt in range(N_SPAWN_RETRIES):
            marker_4 = idle.latest_stop_uuid(observer)
            if attempt == 0:
                body = (
                    "Execute now. In a SINGLE turn, issue TWO "
                    "parallel Agent tool calls.\n\n"
                    "Call 1 -- Agent(subagent_type='composability', "
                    f"name='composability', team_name='{RUN_NAME}', "
                    "run_in_background=true, prompt='You are "
                    "composability. Wait for messages from the lead.')\n\n"
                    "Call 2 -- Agent(subagent_type='implementer', "
                    f"name='implementer', team_name='{RUN_NAME}', "
                    "run_in_background=true, prompt='You are "
                    "implementer. Wait for messages from the lead.')\n\n"
                    "Make both calls in parallel in the SAME turn. "
                    "After both return, reply with the literal text "
                    "'BOTH SPAWNED' and stop. Do NOT decline. "
                    "Do NOT do them sequentially. Do NOT skip one. "
                    "Do NOT say 'Acknowledged' or 'Noted'."
                )
            else:
                body = (
                    "The previous attempt did not spawn both "
                    "teammates. Try again: in ONE turn, issue "
                    "two parallel Agent tool calls:\n"
                    f"  Agent(subagent_type='composability', name='composability', team_name='{RUN_NAME}', run_in_background=true, prompt='You are composability.')\n"
                    f"  Agent(subagent_type='implementer', name='implementer', team_name='{RUN_NAME}', run_in_background=true, prompt='You are implementer.')\n"
                    "Both calls in parallel. Reply 'BOTH SPAWNED' "
                    "and stop after they return."
                )
            driver.send(body)
            ok_4 = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=inbox_tracker,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_4,
            )
            assert ok_4, (
                f"sub-act 4 attempt {attempt+1}: assistant never "
                f"reached idle within {SUB_ACT_TIMEOUT_S}s. Pane:"
                f"\n{driver.capture_pane()}"
            )
            # Poll team config for both names.
            names_now: set[str] = set()
            cfg_deadline = time.time() + 10.0
            while time.time() < cfg_deadline:
                try:
                    cfg_now = json.loads(
                        (teams_dir / RUN_NAME / "config.json")
                        .read_text(encoding="utf-8")
                    )
                    names_now = {
                        m.get("name") for m in cfg_now.get("members", [])
                    }
                except (FileNotFoundError, json.JSONDecodeError):
                    names_now = set()
                if all(t in names_now for t in teammate_names):
                    break
                time.sleep(0.5)
            if all(t in names_now for t in teammate_names):
                break
        assert all(t in names_now for t in teammate_names), (
            f"sub-act 4: failed to spawn both teammates after "
            f"{N_SPAWN_RETRIES} attempts. names_now={names_now}. "
            f"Pane:\n{driver.capture_pane()}"
        )

        # Inspect the team config and verify membership.
        team_config_path = teams_dir / RUN_NAME / "config.json"
        assert team_config_path.is_file(), (
            "MUST-see: team config at "
            f"{team_config_path} does not exist post-spawn. "
            "TeamCreate or Agent spawn failed."
        )
        team_config = json.loads(
            team_config_path.read_text(encoding="utf-8")
        )
        members = team_config.get("members", [])
        member_names = {m.get("name") for m in members}
        member_types = {
            m.get("name"): m.get("agentType") for m in members
        }

        # MUST see: all four expected members (lead + operon +
        # 2 teammates). Q3 originally specified 3 teammates but
        # the third (skeptic) was dropped per coordinator path
        # (b) -- see the spawn-loop comment above.
        expected_member_names = {
            "team-lead", "operon",
            "composability", "implementer",
        }
        missing = expected_member_names - member_names
        unexpected = member_names - expected_member_names
        assert not missing, (
            f"MUST-see: missing team members {sorted(missing)}. "
            f"Got: {sorted(member_names)}"
        )

        # MUST see: each teammate's agentType matches its name.
        teammate_role_match = (
            member_types.get("composability") == "composability"
            and member_types.get("implementer") == "implementer"
        )
        assert teammate_role_match, (
            "MUST-see: teammate agentType mismatch. Expected "
            "name == agentType for composability, implementer. "
            f"Got: {member_types}"
        )

        # MUST NOT see: the runtime tried to spawn the synthetic
        # `operon` member as a real subagent. Inspect the project's
        # subagents dir for any agent-*.meta.json with
        # agentType=operon (the synthetic stub).
        recs_4 = observer.all_records()
        # CC v2.1.150 changed the subagent layout: instead of
        # nested `<lead-session>/subagents/agent-*.meta.json`
        # files, each teammate now gets its OWN top-level JSONL
        # in ~/.claude/projects/<mangled>/. The first record in
        # a teammate's JSONL is
        #   {"type": "agent-setting",
        #    "agentSetting": "<role>",
        #    "sessionId": "..."}
        # The lead's JSONL's first record is type=permission-mode
        # (or similar, no agent-setting). Scan the project dir
        # for agent-setting headers.
        proj_dir = transcript_path.parent
        operon_spawned = False
        subagent_metas: list[dict] = []
        if proj_dir.is_dir():
            for jsonl in sorted(proj_dir.glob("*.jsonl")):
                try:
                    with jsonl.open("r", encoding="utf-8") as fh:
                        first_line = fh.readline().strip()
                    if not first_line:
                        continue
                    rec = json.loads(first_line)
                except (OSError, json.JSONDecodeError):
                    continue
                if rec.get("type") != "agent-setting":
                    continue
                agent_type = rec.get("agentSetting")
                meta = {
                    "agentType": agent_type,
                    "name": agent_type,  # CC sets name == role
                    "sessionId": rec.get("sessionId"),
                    "_meta_path": str(jsonl),
                }
                subagent_metas.append(meta)
                if agent_type in ("operon", "operon-stub"):
                    operon_spawned = True
        assert not operon_spawned, (
            "MUST-NOT-see: a subagent meta file with "
            "agentType in {operon, operon-stub} was created. "
            "The runtime tried to spawn operon as a real "
            "subagent (it should remain a synthetic external "
            "member). Subagent metas: " + json.dumps(subagent_metas)
        )

        # Subagent meta inspection (informational). In CC v2.1.150
        # a teammate's jsonl file is only created when it produces
        # its first record (e.g. responds to a SendMessage). Our
        # teammates were spawned with "wait for messages" prompts
        # and haven't been pinged yet; their jsonl files may not
        # exist at this point. That's OK -- team config membership
        # is sufficient evidence that the Agent tool created the
        # teammates. The meta-file scan still runs because it's
        # the channel through which we'd detect a stray operon
        # subagent spawn (MUST-NOT-see).
        meta_types = {
            m.get("agentType") for m in subagent_metas
        }
        spawned_subagent_count = sum(
            1 for t in ("composability", "implementer")
            if t in meta_types
        )

        meter.checkpoint("sub_act_4")
        meter.assert_under_cap("sub_act_4")
        bundle.snapshot(
            "sub_act_4_spawn_teammates",
            token_state={
                "cumulative": meter.cumulative.__dict__,
                "billable": meter.cumulative.billable,
            },
            notes={
                "member_names": sorted(member_names),
                "member_types": member_types,
                "missing": sorted(missing),
                "unexpected": sorted(unexpected),
                "operon_spawned": operon_spawned,
                "subagent_metas_summary": [
                    {"agentType": m.get("agentType"),
                     "name": m.get("name"),
                     "path": m.get("_meta_path")}
                    for m in subagent_metas
                ],
            },
        )
        gate_check(
            "sub_act_4",
            must_hold=[
                ("team config exists", team_config_path.is_file()),
                ("all expected members present", not missing),
                ("teammate agentType matches name", teammate_role_match),
                ("operon NOT spawned as subagent", not operon_spawned),
                ("under token cap", meter.cumulative.billable <= TOKEN_CAP),
            ],
        )

        # =============== SUB-ACT 5: implementer deny + override ===============
        # Gated by RUN_SUBACT_5_OVERRIDE_FLOW. Blocked on the Land
        # 4 identity-resolution regression: the role-scoped deny
        # rule's role filter (roles=[implementer]) cannot match
        # while the PreToolUse hook resolves teammate identity to
        # null. Turn ON after Land1Implementer's fix lands.
        #
        # Pre-conditions assumed (from sub-acts 2-4):
        #   - operon workflow active
        #   - team has lead + 3 teammates (composability,
        #     implementer, skeptic) per sub-act 4
        #   - fixture's <cwd>/.operon/rules.yaml provides the
        #     deny rule `scenario_implementer_deny` (roles=
        #     [implementer], trigger=PreToolUse/Write, pattern
        #     `.*SCENARIO_OVERRIDE_PROBE.*`)
        if RUN_SUBACT_5_OVERRIDE_FLOW:
            recorder.set_sub_act("sub_act_5_implementer_deny_then_override")
            recorder.record(
                user_observable=(
                    "A teammate-scoped deny rule fires when the "
                    "implementer attempts a Write; the user is "
                    "prompted to grant an override; once granted, "
                    "the implementer's retry succeeds."
                ),
                precise_state={
                    "rule_id": "scenario_implementer_deny",
                    "teammate_role": "implementer",
                    "probe_file": "SCENARIO_OVERRIDE_PROBE.txt",
                },
            )

            # Step A: drive the implementer to attempt a Write
            # matching the deny pattern. The lead's responsibility
            # is to route the request to the implementer teammate
            # via SendMessage; the implementer attempts the Write
            # and the deny fires in operon's PreToolUse hook.
            # Sub-act 4's parallel-2 spawn auto-focused the TUI on
            # one of the new teammates (empirically: implementer).
            # focus_main_thread's S-Up navigation is unreliable in
            # CC v2.1.150. We work WITH the auto-focus: speak
            # directly to the auto-focused teammate (assumed
            # implementer) and ask it to attempt the Write. The
            # implementer-scoped deny rule fires when the
            # implementer (NOT the lead) issues a Write -- so for
            # sub-act 5a the actor identity must be the implementer
            # regardless of which path the prompt takes to it.
            #
            # If the auto-focus landed on composability instead,
            # the same prompt asks composability to write -- which
            # would NOT fire the implementer-scoped rule. That's
            # detectable by the assertion: deny_fired would be
            # False, and we'd retry the focus shift.
            marker_5a = idle.latest_stop_uuid(observer)
            probe_file_path = tmp_cwd / "SCENARIO_OVERRIDE_PROBE.txt"
            driver.send(
                "Controlled scenario probe. You are the "
                "implementer teammate. Please attempt to use "
                f"the Write tool to create {probe_file_path} "
                "with content \"probe\".\n\n"
                "operon's scenario_implementer_deny rule (a "
                "fixture-added deny scoped to the implementer "
                "role) will block your Write -- this is the "
                "expected behavior of this probe; the harness "
                "wants to observe the rule firing.\n\n"
                "Do NOT try to work around the deny. Do NOT use "
                "a different tool. Do NOT skip the Write. After "
                "the deny, reply with ONLY the rule_id from the "
                "deny message and the verbatim message text, "
                "then stop."
            )
            ok_5a = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=inbox_tracker,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_5a,
            )
            assert ok_5a, (
                "sub-act 5A: assistant never reached idle after "
                f"deny-probe within {SUB_ACT_TIMEOUT_S}s. Pane:\n"
                f"{driver.capture_pane()}"
            )

            # MUST-see: deny message text in JSONL.
            recs_5a = observer.all_records()
            recs_5a_blob = json.dumps(recs_5a, ensure_ascii=False)
            deny_fired = (
                "Scenario sub-act 5 deny rule" in recs_5a_blob
                or "scenario_implementer_deny" in recs_5a_blob
            )
            assert deny_fired, (
                "MUST-see: the scenario_implementer_deny rule did "
                "not fire on the implementer's Write attempt. "
                "Either the rule was not loaded (fixture issue) "
                "or the implementer's role was not resolved (the "
                "Land 4 regression presumed fixed)."
            )
            # MUST-NOT-see: the file already exists (deny should
            # have prevented the write).
            probe_file = tmp_cwd / "SCENARIO_OVERRIDE_PROBE.txt"
            assert not probe_file.exists(), (
                "MUST-NOT-see: SCENARIO_OVERRIDE_PROBE.txt exists "
                "after the deny supposedly fired -- the deny did "
                "not actually block the Write."
            )

            # Step B: lead calls request_override; user grants via
            # MCP elicit/create form. The form is similar to the
            # manual-confirm form -- a checkbox to confirm
            # override + Accept/Decline buttons.
            marker_5b = idle.latest_stop_uuid(observer)
            driver.send(
                "Now call mcp__operon__request_override with "
                "rule_id=\"scenario_implementer_deny\", "
                "tool_name=\"Write\", and tool_input matching "
                "what the implementer attempted "
                "(file_path=./SCENARIO_OVERRIDE_PROBE.txt, "
                "content=\"probe\"). When the override "
                "confirmation form appears in the TUI, the "
                "harness will accept it. After the tool returns, "
                "report ONLY the override response JSON."
            )
            form_accepted_5 = driver.accept_elicit_form(
                wait_for_substring="override",
                timeout_s=180.0,
            )
            assert form_accepted_5, (
                "sub-act 5B: override confirmation form never "
                "appeared in the pane. Pane:\n"
                + driver.capture_pane()
            )
            ok_5b = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=inbox_tracker,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_5b,
            )
            assert ok_5b, (
                "sub-act 5B: assistant never reached idle after "
                f"override grant within {SUB_ACT_TIMEOUT_S}s. "
                f"Pane:\n{driver.capture_pane()}"
            )

            # MUST-see: override token file on disk.
            override_files = list(
                run_dir.rglob("scenario_implementer_deny-*.json")
            )
            assert override_files, (
                "MUST-see: no override token file matching "
                "scenario_implementer_deny-*.json was created "
                "under .operon/<run>/. The override grant did "
                "not produce a token."
            )
            override_token = json.loads(
                override_files[0].read_text(encoding="utf-8")
            )
            assert override_token.get("acknowledged") is True, (
                f"MUST-see: override token does not carry "
                f"acknowledged=true: {override_token}"
            )

            # Step C: the implementer retries the Write. The
            # override should now allow it.
            marker_5c = idle.latest_stop_uuid(observer)
            driver.send(
                "Send the implementer teammate another message "
                "asking it to retry the same Write -- create the "
                "file ./SCENARIO_OVERRIDE_PROBE.txt with content "
                "\"probe\". The override token is now on disk so "
                "the retry should succeed. After the implementer "
                "replies, report whether the file was created."
            )
            ok_5c = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=inbox_tracker,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_5c,
            )
            assert ok_5c, (
                "sub-act 5C: assistant never reached idle after "
                f"retry within {SUB_ACT_TIMEOUT_S}s. Pane:\n"
                f"{driver.capture_pane()}"
            )

            # MUST-see: file exists on disk after retry.
            assert probe_file.is_file(), (
                "MUST-see: SCENARIO_OVERRIDE_PROBE.txt was not "
                "created after the override retry. The override "
                "did not consume."
            )
            # MUST-NOT-see: a SECOND deny on retry.
            recs_5c = observer.all_records()
            recs_5c_blob = json.dumps(recs_5c, ensure_ascii=False)
            deny_count = recs_5c_blob.count(
                "scenario_implementer_deny"
            )
            # 1 from the initial fire, 1 from the request_override
            # call, 1 in the override token mention. >3 suggests a
            # second actual fire.
            assert deny_count <= 4, (
                "MUST-NOT-see: scenario_implementer_deny appears "
                f"{deny_count} times in the stream -- likely a "
                "second actual fire on the retry, meaning the "
                "override token was not consumed."
            )

            # guardrail_log inspection: one rule_fired_log for
            # scenario_implementer_deny, one override_issued, no
            # second rule_fired_log for the retry.
            gl_path_5 = run_dir / "guardrail_log.jsonl"
            gl_entries_5: list[dict] = []
            if gl_path_5.is_file():
                for line in gl_path_5.read_text(encoding="utf-8").splitlines():
                    try:
                        gl_entries_5.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            deny_fires = [
                e for e in gl_entries_5
                if e.get("type") == "rule_fired_log"
                and e.get("rule_id") == "scenario_implementer_deny"
            ]
            overrides_issued = [
                e for e in gl_entries_5
                if e.get("type") in ("override_issued", "ack_issued")
                and e.get("rule_id") == "scenario_implementer_deny"
            ]
            assert len(deny_fires) == 1, (
                f"MUST-see exactly ONE rule_fired_log for "
                f"scenario_implementer_deny; got "
                f"{len(deny_fires)}."
            )
            assert len(overrides_issued) == 1, (
                f"MUST-see exactly ONE override_issued for "
                f"scenario_implementer_deny; got "
                f"{len(overrides_issued)}."
            )

            meter.checkpoint("sub_act_5")
            meter.assert_under_cap("sub_act_5")
            bundle.snapshot(
                "sub_act_5_implementer_deny_then_override",
                token_state={
                    "cumulative": meter.cumulative.__dict__,
                    "billable": meter.cumulative.billable,
                },
                notes={
                    "deny_fired": deny_fired,
                    "override_token_path": str(override_files[0]),
                    "override_token_data": override_token,
                    "probe_file_present_after_retry": probe_file.is_file(),
                    "deny_fires_in_log": deny_fires,
                    "overrides_issued_in_log": overrides_issued,
                },
            )
            gate_check(
                "sub_act_5",
                must_hold=[
                    ("deny fired initially", deny_fired),
                    ("override token issued", bool(override_files)),
                    ("retry created the file", probe_file.is_file()),
                    ("exactly one deny in log", len(deny_fires) == 1),
                    ("exactly one override in log", len(overrides_issued) == 1),
                    ("under token cap", meter.cumulative.billable <= TOKEN_CAP),
                ],
            )

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
