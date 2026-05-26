"""E2E scenario: a user walks the `project_team` workflow end-to-end.

Originally one E2E test function (`test_project_team_workflow`)
spanning all 10 sub-acts. Split per Land 13 into two tests after
the Land 12 + Group B diagnostic (commit a9ed0ae) empirically
confirmed (3/3 PASS isolated, 1/3 PASS chain) that LLM context
saturation across sub-acts 5/6/7 caused sub-act 8's advance_phase
prompt to be interpreted as a summary directive at a 67% rate.

The two tests are:

- ``test_project_team_coordination_chain`` (sub-acts 1-7):
  pre-activation guardrail through teammate cross-talk + whoami.
  Stops cleanly at sub-act 7's gate. ~50K cumulative tokens.

- ``test_project_team_advance_lifecycle`` (sub-acts 8, 9, 10) from
  a fresh state with minimal scaffolding (activate workflow +
  vision->setup advance + spawn). The lead sees NO send_to_member,
  NO Monitor flushes, NO teammate coordination before sub-act 8's
  advance_phase call -- a "clean-room" advance lifecycle. ~80-120K
  cumulative tokens.

Together the two tests preserve coverage of all originally-tested
behaviors. The dimensions matrix below is now split across them:

- Lifecycle: chain test = fresh; lifecycle test = fresh -> restored.
- Cardinality: zero (1-3) -> 2 teammates (composability + implementer
  per Q3 path b). Both tests reach "many".
- Phase trajectory: stays + 1 advance (chain test); 2 advances + halt
  + restore (lifecycle test).
- Inbound traffic: none (1-3) -> mixed cross-talk + queries (4-7) in
  chain; none in lifecycle (no coordination).
- Guardrail surface: silent (1-2, 4) -> firing (3, 5) in chain;
  silent in lifecycle.
- Workflow longevity: vision -> setup (chain test); vision -> setup
  -> leadership + restore (lifecycle test).

Test bed: real subscription-path Claude Code v2.1.150 driven via
pty driver (`tests/_harness/pty_driver.py`), observed via JSONL
transcript. Filesystem-only fixtures.

Per the TEST_SPECIFICATION compliance checklist:
- TOKEN_CAP / TOKEN_CAP_ADVANCE_LIFECYCLE declared at module level.
- Each sub-act has at least one "MUST NOT see" assertion.
- Each sub-act emits a snapshot waypoint.
- Each sub-act ends with a `gate_check`.
- Idle waits use the harness `wait_idle_pre_kill`; no time.sleep
  for wall-clock-only synchronization.
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
    inbox_watcher,
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

#: Run name passed to activate_workflow (sub-act 2). Already a
#: canonical slug, so operon's `_normalize_run_name` leaves it
#: unchanged and it matches the directory TeamCreate creates. (As of
#: 0.0.4 operon normalizes any run_name to this slug form -- lowercase,
#: hyphens -- so an underscore name like `scenario_run` would also
#: work; we keep the canonical form here so the expected on-disk paths
#: below are literal.)
RUN_NAME = "scenario-run"

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


def _last_advance_failed_check(records: list[dict]) -> dict | None:
    """Find the most recent advance_phase tool_result and parse the failed
    check it reports.

    Looks for the latest tool_use_result for
    ``mcp__plugin_operon-plugin_operon__advance_phase``, then parses the
    response body for a failed check.

    Returns a dict ``{check_type, missing_path?}`` or None if no
    advance_phase result found or none failed.

    Operon's advance_phase response shape (per the activate / advance
    tool docstrings): ``{"status": "blocked", "outcomes": [
    {"check_type": "<type>", "passed": false, "evidence": "...",
    "missing_path": "..."}, ...], ...}``. We pluck the first failed
    outcome.
    """
    # Walk records in reverse to find the latest tool_use_result for
    # advance_phase.
    for rec in reversed(records):
        # The tool_result lives in a user-type record whose message
        # content includes a tool_use_id; we don't need to correlate
        # to the original tool_use, only to find an advance_phase
        # response.
        if rec.get("type") != "user":
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "tool_result":
                continue
            body = item.get("content")
            if isinstance(body, list):
                body_text = "".join(
                    str(sub.get("text", ""))
                    if isinstance(sub, dict) else str(sub)
                    for sub in body
                )
            else:
                body_text = str(body or "")
            if "advance_phase" not in body_text and "check_type" not in body_text:
                # heuristic: most advance_phase responses mention
                # one of these.
                continue
            # Try to parse a JSON object out of the body. The model
            # may have already JSON-stringified the response.
            try:
                parsed = json.loads(body_text)
            except json.JSONDecodeError:
                continue
            outcomes = parsed.get("outcomes") if isinstance(parsed, dict) else None
            if not isinstance(outcomes, list):
                continue
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                if outcome.get("passed") is True:
                    continue
                ct = outcome.get("check_type")
                if not ct:
                    continue
                report: dict = {"check_type": ct, "raw_outcome": outcome}
                # For file-exists-check, the missing path is often
                # in the outcome as `missing_path`, `path`, or
                # mentioned in `evidence`.
                missing = (
                    outcome.get("missing_path")
                    or outcome.get("path")
                )
                if not missing:
                    ev = outcome.get("evidence") or outcome.get("message") or ""
                    # Look for a quoted path or trailing absolute path.
                    import re as _re
                    m = _re.search(r"['\"]([^'\"]+\.(?:md|txt|json))['\"]", str(ev))
                    if not m:
                        m = _re.search(r"(/[\w./_-]+\.(?:md|txt|json))", str(ev))
                    if m:
                        missing = m.group(1)
                if missing:
                    report["missing_path"] = missing
                return report
    return None


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

def test_project_team_coordination_chain(tmp_cwd, operon_plugin_dir, claude_driver_cls):
    """Walk the project_team coordination chain (sub-acts 1-7).

    Originally one E2E test spanning all 10 sub-acts; split per the
    Land 12 + Group B diagnostic (commit a9ed0ae) which empirically
    confirmed (3/3 PASS isolated, 1/3 PASS chain) that LLM context
    saturation across sub-acts 5/6/7 caused sub-act 8's
    advance_phase prompt to be interpreted as a summary directive
    at a 67% rate. Sub-acts 8, 9, 10 now live in
    ``test_project_team_advance_lifecycle``. The two tests together
    preserve coverage of all originally-tested behaviors.

    This test covers:
      Sub-act 1     -- pre-activation guardrail
      Sub-act 1-ext -- fire -> ack -> retry envelope
      Sub-act 2     -- TeamCreate(coordinator) + activate_workflow
      Sub-act 3     -- advance vision -> setup (manual-confirm)
      Sub-act 4     -- parallel spawn of composability + implementer
      Sub-act 5     -- implementer deny + request_override + retry
      Sub-act 6     -- teammate cross-talk (composability -> impl + lead)
      Sub-act 7     -- composability runs [OPERON_QUERY] whoami
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

    driver = claude_driver_cls(
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
        # The full "user trips a warn rule" envelope is:
        #   fire -> ack -> retry succeeds (no second blocked fire).
        # The base sub-act 1 above only covers the initial fire; the
        # extension below covers ack + retry.
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
        # Two-call sequence (per Boaz's empirical walkthrough):
        # first TeamCreate with agent_type="coordinator" so the
        # lead's roster row has agentType="coordinator" -- this
        # is what operon's per-(role, phase).md brief lookup uses;
        # without it the lookup falls back. Then activate_workflow.
        driver.send(
            "Two-step setup. Step 1: call TeamCreate with "
            f"team_name='{RUN_NAME}' AND agent_type='coordinator'. "
            "Step 2: call mcp__operon__activate_workflow with "
            f"workflow_id='project_team' and run_name='{RUN_NAME}'. "
            "After both return, report ONLY the activation status "
            "and the current phase, nothing else."
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
        # Revised design per Boaz's manual walkthrough + Q23c +
        # Land 8 role-resolution: lead routes the deny probe to
        # the implementer via send_to_member, with no lead bounce. CC's elicit-
        # form routing follows the originating MCP-call session,
        # so request_override called from the implementer's
        # channel produces a form there; the harness drives the
        # form in-place (no channel switch). operon's
        # request_override mints the token under the resolved
        # Coordinator handle regardless of which session called
        # it (B.0 MCP-identity limit), so the implementer-self
        # path produces an override token usable by the
        # implementer on retry.
        #
        # Pre-conditions assumed (from sub-acts 2-4):
        #   - operon workflow active, phase = setup
        #   - team has lead + 2 teammates (composability,
        #     implementer) per sub-act 4 [Q3 path b]
        #   - fixture's <cwd>/.operon/rules.yaml provides the
        #     deny rule `scenario_implementer_deny` (roles=
        #     [implementer], trigger=PreToolUse/Write, pattern
        #     `.*SCENARIO_OVERRIDE_PROBE.*`)
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

        # Q23c lead-driven design: the entire sub-act 5 runs
        # from the LEAD's pane via mcp__operon__send_to_member.
        # The lead routes prompts to teammates; the teammates
        # execute and reply via SendMessage back to the lead;
        # the lead's inbox file at
        # ~/.claude/teams/<run>/inboxes/team-lead.json is the
        # completion signal for each step.
        probe_file_path = tmp_cwd / "SCENARIO_OVERRIDE_PROBE.txt"
        probe_file = tmp_cwd / "SCENARIO_OVERRIDE_PROBE.txt"
        lead_inbox_path_5 = teams_dir / RUN_NAME / "inboxes" / "team-lead.json"

        # Step A: lead routes the deny-probe to implementer
        # via send_to_member. Implementer attempts Write, deny
        # fires, implementer SendMessage's the rule_id back to
        # team-lead.
        baseline_5a = inbox_watcher.inbox_baseline(lead_inbox_path_5)
        marker_5a = idle.latest_stop_uuid(observer)
        driver.send(
            "Call mcp__operon__send_to_member now. Parameters:\n"
            "  name='implementer'\n"
            "  text: 'Controlled scenario probe -- please use "
            f"the Write tool to create the file {probe_file_path} "
            "with content \"probe\". operon\\'s "
            "scenario_implementer_deny rule (a fixture-added "
            "deny scoped to your role) will block your Write -- "
            "that is the expected behavior of this probe. After "
            "the deny, send a SendMessage back to team-lead "
            "with ONLY the rule_id from the deny message. Do "
            "NOT try to work around the deny or use a different "
            "tool.'\n\n"
            "DO NOT use the Monitor or Task tools to wait. The "
            "harness watches the inbox directly via the "
            "filesystem. Return immediately after send_to_member "
            "with a brief status line and end the turn -- the "
            "harness will detect the reply when it lands."
        )
        # Wait for lead idle (send_to_member returned), then
        # for the implementer's reply to land in lead's inbox.
        ok_5a = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,  # don't rely on global tracker
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_5a,
        )
        assert ok_5a, (
            "sub-act 5A: lead never reached idle after "
            f"send_to_member within {SUB_ACT_TIMEOUT_S}s."
        )
        # Wait for implementer's reply.
        impl_reply_5a = inbox_watcher.wait_for_inbox_entry(
            lead_inbox_path_5,
            predicate=lambda e: (
                e.get("from") == "implementer"
                and not inbox_watcher.is_idle_notification(e)
            ),
            timeout_s=SUB_ACT_TIMEOUT_S,
            baseline_count=baseline_5a,
        )
        assert impl_reply_5a is not None, (
            "MUST-see: no reply from implementer landed in "
            f"team-lead's inbox at {lead_inbox_path_5} within "
            f"{SUB_ACT_TIMEOUT_S}s."
        )
        # MUST-see: the deny rule fired.
        reply_text_5a = impl_reply_5a.get("text") or ""
        deny_fired = "scenario_implementer_deny" in reply_text_5a
        assert deny_fired, (
            "MUST-see: implementer's reply did not mention "
            "scenario_implementer_deny. Reply text: "
            f"{reply_text_5a[:500]}"
        )
        # MUST-NOT-see: the file exists yet (deny blocked the Write).
        assert not probe_file.exists(), (
            "MUST-NOT-see: SCENARIO_OVERRIDE_PROBE.txt exists "
            "after the deny supposedly fired -- the deny did "
            "not actually block the Write."
        )

        # Monitor-leak guard (Land 12): the lead may have used
        # Monitor to satisfy the prior "wait for inbox" prompt
        # despite the prompt-prevention language. Flush any
        # active background tasks before the next phase of
        # sub-act 5.
        idle.flush_lead_background_tasks(driver, observer)

        # Step B: lead calls request_override itself; the
        # elicit form renders in the LEAD's pane (which the
        # pty driver displays cleanly -- no overlay). Harness
        # drives Space+Down+Enter via accept_elicit_form.
        marker_5b = idle.latest_stop_uuid(observer)
        driver.send(
            "Now call mcp__operon__request_override yourself. "
            "Parameters: rule_id='scenario_implementer_deny', "
            "tool_name='Write', tool_input="
            f"{{\"file_path\": \"{probe_file_path}\", "
            "\"content\": \"probe\"}}. An override confirmation "
            "form will appear in your pane -- the harness will "
            "accept it. After the tool returns, report ONLY the "
            "override response and stop."
        )
        form_accepted_5 = driver.accept_elicit_form(
            wait_for_substring="override",
            timeout_s=180.0,
        )
        assert form_accepted_5, (
            "sub-act 5B: override confirmation form never "
            "appeared in the lead's pane within 180s. Pane:\n"
            + driver.capture_pane()
        )
        ok_5b = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_5b,
        )
        assert ok_5b, (
            "sub-act 5B: lead never reached idle after "
            f"request_override within {SUB_ACT_TIMEOUT_S}s."
        )

        # MUST-see: override flow registered in the audit log.
        # Empirically (Land 8, runs through 2026-05-24):
        # operon's request_override does NOT write a separate
        # on-disk token file (unlike acknowledge_warning's ack
        # token). The override surfaces ONLY as guardrail_log
        # entries:
        #   type=override_requested outcome=pending
        #   type=override_granted   outcome=overridden
        # The next rule_fired_log for the same rule_id then
        # carries outcome=overridden (rather than blocked).
        # Inspect the log immediately for the override_granted
        # entry.
        gl_path_5_b = run_dir / "guardrail_log.jsonl"
        gl_entries_5_b: list[dict] = []
        if gl_path_5_b.is_file():
            for line in gl_path_5_b.read_text(encoding="utf-8").splitlines():
                try:
                    gl_entries_5_b.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        override_granted = [
            e for e in gl_entries_5_b
            if e.get("type") == "override_granted"
            and e.get("rule_id") == "scenario_implementer_deny"
        ]
        assert override_granted, (
            "MUST-see: no override_granted entry for "
            "scenario_implementer_deny in guardrail_log.jsonl. "
            "The user-grant step did not register."
        )

        # Step C: lead routes the retry-probe to implementer
        # via send_to_member. The override token is on disk;
        # implementer's PreToolUse hook should consume it and
        # allow the Write.
        baseline_5c = inbox_watcher.inbox_baseline(lead_inbox_path_5)
        marker_5c = idle.latest_stop_uuid(observer)
        driver.send(
            "Call mcp__operon__send_to_member now. Parameters:\n"
            "  name='implementer'\n"
            "  text: 'The override token is now on disk. Retry "
            f"the same Write -- create {probe_file_path} with "
            "content \"probe\". The retry should succeed. After "
            "the Write returns, send a SendMessage back to "
            "team-lead with \"WROTE\" if the file was created.'\n\n"
            "DO NOT use the Monitor or Task tools to wait. The "
            "harness watches the inbox directly. Return "
            "immediately after send_to_member with a brief status "
            "line and end the turn."
        )
        ok_5c = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_5c,
        )
        assert ok_5c, (
            "sub-act 5C: lead never reached idle after "
            f"send_to_member within {SUB_ACT_TIMEOUT_S}s."
        )
        # Wait for implementer's WROTE reply.
        impl_reply_5c = inbox_watcher.wait_for_inbox_entry(
            lead_inbox_path_5,
            predicate=lambda e: (
                e.get("from") == "implementer"
                and not inbox_watcher.is_idle_notification(e)
            ),
            timeout_s=SUB_ACT_TIMEOUT_S,
            baseline_count=baseline_5c,
        )
        assert impl_reply_5c is not None, (
            "MUST-see: no reply from implementer landed in "
            f"team-lead's inbox after retry within "
            f"{SUB_ACT_TIMEOUT_S}s."
        )

        # MUST-see: file exists on disk after retry.
        assert probe_file.is_file(), (
            "MUST-see: SCENARIO_OVERRIDE_PROBE.txt was not "
            "created after the override retry. The override "
            "did not consume."
        )
        # guardrail_log inspection. Empirical envelope:
        #   rule_fired_log outcome=blocked      (initial fire)
        #   override_requested outcome=pending  (request)
        #   override_granted   outcome=overridden (user grant)
        #   rule_fired_log outcome=overridden   (retry, consumed)
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
        deny_blocked = [
            e for e in deny_fires
            if e.get("outcome") == "blocked"
        ]
        deny_overridden = [
            e for e in deny_fires
            if e.get("outcome") == "overridden"
        ]
        overrides_granted_final = [
            e for e in gl_entries_5
            if e.get("type") == "override_granted"
            and e.get("rule_id") == "scenario_implementer_deny"
        ]
        assert len(deny_blocked) == 1, (
            f"MUST-see exactly ONE rule_fired_log outcome=blocked "
            f"for scenario_implementer_deny; got {len(deny_blocked)}."
        )
        assert len(deny_overridden) == 1, (
            f"MUST-see exactly ONE rule_fired_log "
            f"outcome=overridden (the retry consuming the "
            f"override); got {len(deny_overridden)}."
        )
        assert len(overrides_granted_final) == 1, (
            f"MUST-see exactly ONE override_granted entry; "
            f"got {len(overrides_granted_final)}."
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
                "probe_file_present_after_retry": probe_file.is_file(),
                "deny_fires_in_log": deny_fires,
                "overrides_granted_in_log": overrides_granted_final,
            },
        )
        gate_check(
            "sub_act_5",
            must_hold=[
                ("deny fired initially", deny_fired),
                ("override granted via elicit form", bool(overrides_granted_final)),
                ("retry created the file", probe_file.is_file()),
                ("exactly one deny outcome=blocked", len(deny_blocked) == 1),
                ("exactly one deny outcome=overridden", len(deny_overridden) == 1),
                ("exactly one override_granted", len(overrides_granted_final) == 1),
                ("under token cap", meter.cumulative.billable <= TOKEN_CAP),
            ],
        )

        # Post-5: no focus switch needed -- the entire sub-act
        # 5 ran from the lead's pane. Sub-acts 6/7 will also
        # be lead-driven (Q23c design).
        # Monitor-leak guard (Land 12) at the 5 -> 6 boundary.
        idle.flush_lead_background_tasks(driver, observer)

        # =============== SUB-ACT 6: teammate cross-talk ===============
        # Empirical envelope per Boaz's walk: focus into a
        # teammate, ask it to SendMessage to another teammate /
        # the lead. The runtime stamps the `from` field; the
        # recipient's inbox file at
        # ~/.claude/teams/<RUN_NAME>/inboxes/<recipient>.json
        # carries the entry.
        recorder.set_sub_act("sub_act_6_teammate_cross_talk")
        recorder.record(
            user_observable=(
                "Teammates can exchange messages with each "
                "other and with the lead via the inbox surface. "
                "composability sends 'hi' to implementer and "
                "'reporting in' to the lead."
            ),
            precise_state={
                "from": "composability",
                "to_teammate": "implementer",
                "to_lead": "team-lead",
            },
        )

        # Q23c lead-driven: lead routes via send_to_member to
        # composability; composability does the two outgoing
        # SendMessages (to implementer + to team-lead). One lead
        # send issuing TWO outgoing actions in composability's
        # turn.
        impl_inbox_path = teams_dir / RUN_NAME / "inboxes" / "implementer.json"
        lead_inbox_path = teams_dir / RUN_NAME / "inboxes" / "team-lead.json"
        baseline_impl_6 = inbox_watcher.inbox_baseline(impl_inbox_path)
        baseline_lead_6 = inbox_watcher.inbox_baseline(lead_inbox_path)

        marker_6 = idle.latest_stop_uuid(observer)
        driver.send(
            "Call mcp__operon__send_to_member now. Parameters:\n"
            "  name='composability'\n"
            "  text: 'Two tasks in this single turn:\\n"
            "  1. Send a SendMessage to teammate \"implementer\" "
            "with text exactly \"hi from composability\".\\n"
            "  2. Send a SendMessage to teammate \"team-lead\" "
            "with text exactly \"reporting in\".\\nAfter both "
            "SendMessages return, reply with the literal text "
            "\"SENT-6\" and stop.'\n\n"
            "DO NOT use the Monitor or Task tools to wait. The "
            "harness watches the inboxes directly. Return "
            "immediately after send_to_member with a brief status "
            "line and end the turn."
        )
        ok_6 = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_6,
        )
        assert ok_6, (
            f"sub-act 6: lead never reached idle. "
            f"Pane:\n{driver.capture_pane()}"
        )
        # Wait for composability's two outgoing messages.
        impl_msg_6 = inbox_watcher.wait_for_inbox_entry(
            impl_inbox_path,
            predicate=lambda e: (
                e.get("from") == "composability"
                and "hi from composability" in (e.get("text") or "")
            ),
            timeout_s=SUB_ACT_TIMEOUT_S,
            baseline_count=baseline_impl_6,
        )
        lead_msg_6 = inbox_watcher.wait_for_inbox_entry(
            lead_inbox_path,
            predicate=lambda e: (
                e.get("from") == "composability"
                and "reporting in" in (e.get("text") or "")
            ),
            timeout_s=SUB_ACT_TIMEOUT_S,
            baseline_count=baseline_lead_6,
        )

        # Inspect inbox state for the gate_check.
        impl_inbox = inbox_watcher.read_inbox(impl_inbox_path)
        lead_inbox = inbox_watcher.read_inbox(lead_inbox_path)
        assert impl_msg_6 is not None, (
            "MUST-see: implementer's inbox did not receive a "
            "from=composability entry with text 'hi from "
            "composability' within timeout."
        )
        assert lead_msg_6 is not None, (
            "MUST-see: team-lead's inbox did not receive a "
            "from=composability entry with text 'reporting in' "
            "within timeout."
        )
        # MUST NOT see: operon receiving the cross-talk.
        operon_inbox_path_6 = teams_dir / RUN_NAME / "inboxes" / "operon.json"
        operon_inbox_6 = []
        if operon_inbox_path_6.is_file():
            try:
                operon_inbox_6 = json.loads(operon_inbox_path_6.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                operon_inbox_6 = []
        operon_got_crosstalk = any(
            isinstance(m, dict)
            and ("hi from composability" in (m.get("text") or "")
                 or "reporting in" in (m.get("text") or ""))
            for m in operon_inbox_6
        )
        assert not operon_got_crosstalk, (
            "MUST-NOT-see: operon's inbox received cross-talk "
            "messages meant for implementer/team-lead."
        )

        meter.checkpoint("sub_act_6")
        meter.assert_under_cap("sub_act_6")
        bundle.snapshot(
            "sub_act_6_teammate_cross_talk",
            token_state={
                "cumulative": meter.cumulative.__dict__,
                "billable": meter.cumulative.billable,
            },
            notes={
                "impl_inbox_entries": len(impl_inbox),
                "lead_inbox_entries": len(lead_inbox),
                "impl_msg_6_excerpt": (impl_msg_6 or {}).get("text", "")[:200],
                "lead_msg_6_excerpt": (lead_msg_6 or {}).get("text", "")[:200],
            },
        )
        gate_check(
            "sub_act_6",
            must_hold=[
                ("implementer got hi from composability", impl_msg_6 is not None),
                ("team-lead got reporting in from composability", lead_msg_6 is not None),
                ("operon did NOT receive cross-talk", not operon_got_crosstalk),
                ("under token cap", meter.cumulative.billable <= TOKEN_CAP),
            ],
        )
        # Monitor-leak guard (Land 12) at the 6 -> 7 boundary.
        idle.flush_lead_background_tasks(driver, observer)

        # =============== SUB-ACT 7: OPERON_QUERY whoami ===============
        # Still in composability's channel. Ask composability to
        # SendMessage to operon with text "[OPERON_QUERY] whoami".
        # operon's inbox_reader polls scenario-run/inboxes/operon.json
        # and dispatches; operon replies via SendMessage back to
        # composability's inbox with "[OPERON_REPLY] whoami {...}".
        recorder.set_sub_act("sub_act_7_operon_query_whoami")
        recorder.record(
            user_observable=(
                "A teammate (composability) can introspect its "
                "identity by asking operon over the inbox channel. "
                "Operon replies with a verified-identity payload."
            ),
            precise_state={
                "querier": "composability",
                "query": "[OPERON_QUERY] whoami",
            },
        )

        # Q23c lead-driven: lead routes via send_to_member to
        # composability; composability sends the OPERON_QUERY to
        # operon and waits for the [OPERON_REPLY] in its own
        # inbox; composability then forwards the reply text to
        # team-lead via SendMessage so we can read it from the
        # lead's inbox (avoiding the need to peek into the
        # teammate's pane).
        baseline_lead_7 = inbox_watcher.inbox_baseline(lead_inbox_path)
        marker_7 = idle.latest_stop_uuid(observer)
        driver.send(
            "Call mcp__operon__send_to_member now. Parameters:\n"
            "  name='composability'\n"
            "  text: 'Three-step task:\\n"
            "  1. Send a SendMessage to teammate \"operon\" with "
            "text EXACTLY \"[OPERON_QUERY] whoami\".\\n"
            "  2. Wait for the [OPERON_REPLY] whoami response to "
            "land in YOUR inbox (operon polls ~1s; allow up to "
            "30s).\\n"
            "  3. Once you have the [OPERON_REPLY] line in your "
            "inbox, send a SendMessage to teammate \"team-lead\" "
            "with the VERBATIM text of the [OPERON_REPLY] line, "
            "prefixed by \"FORWARD: \".\\nThen reply WHOAMI-DONE "
            "and stop.'\n\n"
            "DO NOT use the Monitor or Task tools to wait. The "
            "harness watches the inbox directly. Return "
            "immediately after send_to_member with a brief status "
            "line and end the turn."
        )
        ok_7 = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_7,
        )
        assert ok_7, (
            f"sub-act 7: lead never reached idle. "
            f"Pane:\n{driver.capture_pane()}"
        )
        # Wait for composability's FORWARD: message in lead's inbox.
        forward_msg = inbox_watcher.wait_for_inbox_entry(
            lead_inbox_path,
            predicate=lambda e: (
                e.get("from") == "composability"
                and "FORWARD:" in (e.get("text") or "")
                and "[OPERON_REPLY]" in (e.get("text") or "")
            ),
            timeout_s=SUB_ACT_TIMEOUT_S,
            baseline_count=baseline_lead_7,
        )
        assert forward_msg is not None, (
            "MUST-see: composability did not forward an "
            "[OPERON_REPLY] whoami payload to team-lead's inbox "
            f"within {SUB_ACT_TIMEOUT_S}s."
        )
        forward_txt = forward_msg.get("text") or ""
        # Also inspect composability's own inbox for the operon
        # reply (cross-check).
        comp_inbox_path = teams_dir / RUN_NAME / "inboxes" / "composability.json"
        comp_inbox = inbox_watcher.read_inbox(comp_inbox_path)
        operon_replies = [
            m for m in comp_inbox
            if isinstance(m, dict)
            and m.get("from") == "operon"
            and "[OPERON_REPLY] whoami" in (m.get("text") or "")
        ]
        whoami_payload_ok = (
            "composability" in forward_txt
            and ("source" in forward_txt or "team_roster" in forward_txt)
        )
        assert whoami_payload_ok, (
            "MUST-see: the forwarded [OPERON_REPLY] whoami payload "
            "did not contain expected identity fields "
            f"(composability + source/team_roster). Body: "
            f"{forward_txt[:400]}"
        )

        meter.checkpoint("sub_act_7")
        meter.assert_under_cap("sub_act_7")
        bundle.snapshot(
            "sub_act_7_operon_query_whoami",
            token_state={
                "cumulative": meter.cumulative.__dict__,
                "billable": meter.cumulative.billable,
            },
            notes={
                "operon_reply_count": len(operon_replies),
                "last_reply_excerpt": (
                    (operon_replies[-1].get("text") or "")[:500]
                    if operon_replies else ""
                ),
            },
        )
        gate_check(
            "sub_act_7",
            must_hold=[
                ("[OPERON_REPLY] whoami received", bool(operon_replies)),
                ("payload has expected identity fields", whoami_payload_ok),
                ("under token cap", meter.cumulative.billable <= TOKEN_CAP),
            ],
        )

        # Chain test ends at sub-act 7. Sub-acts 8, 9, 10
        # moved to test_project_team_advance_lifecycle per the
        # chain-saturation split (Land 13 / Group B diagnostic
        # commit a9ed0ae).

    finally:
        driver.kill()


# ============================================================================
# Advance lifecycle test (sub-acts 8, 9, 10): isolated from the
# coordination chain to avoid Land 12's chain-saturation failure mode.
# Originally landed as the "Group B" diagnostic for the Land 11 R1+R2
# Monitor-leak investigation (commit a9ed0ae); promoted to canonical
# at Land 13. See module docstring for the split rationale.
# ============================================================================

#: Cap for the advance-lifecycle test. Covers: TeamCreate + activate
#: + vision->setup advance + spawn + setup->leadership cascade + halt
#: + restore + recall. Empirically (Group B + extension): cumulative
#: cost ~80-120K. Bumped to 230K per Land 14 to absorb up to 2
#: prose retries per advance_phase attempt (~5-10K each, 2 retries
#: worst case = ~20K).
TOKEN_CAP_ADVANCE_LIFECYCLE = 230_000


def test_project_team_advance_lifecycle(tmp_cwd, operon_plugin_dir, claude_driver_cls):
    """Walk the project_team advance lifecycle (sub-acts 8, 9, 10).

    Isolated from the coordination chain (sub-acts 1-7) per the
    Land 13 split (rationale + diagnostic in
    test_project_team_coordination_chain's docstring). The lead
    sees NO send_to_member, NO Monitor flushes, NO teammate
    coordination before sub-act 8's advance_phase call -- a
    "clean-room" advance.

    Sequence:
      Step A: TeamCreate(coordinator) + activate_workflow.
      Step B: Advance vision -> setup via manual-confirm.
      Step C: Spawn composability + implementer in parallel.
      Step D: Sub-act 8 -- advance setup -> leadership (cascading
              file-exists-check satisfaction, or pre-empted by
              the lead's proactive prereq-creation from operon's
              setup brief).
      Step E: Sub-act 9 -- halt session via /exit; verify operon
              state + sidechain transcripts survive.
      Step F: Sub-act 10 -- new session, restore_operon_session,
              respawn composability, WA1 transcript injection,
              verify first-person recall.

    Recall assertions in sub-act 10 are adapted for the isolated
    context (composability's prior session had only the spawn
    handshake, not the rich sub-act-6/7 history). The MUST-see is
    composability acknowledging its prior identity; the MUST-NOT-see
    is the "no prior memory" disclaimer that indicates WA1 didn't
    fire.
    """
    cc_version_gate.assert_cc_version()
    _seed_fixture(tmp_cwd)
    _stale_team = Path.home() / ".claude" / "teams" / RUN_NAME
    if _stale_team.exists() and not KEEP_TEAM_CONFIG_ON_FAIL:
        shutil.rmtree(_stale_team)

    session_id = str(uuid.uuid4())
    session_name = f"ptw-iso-{session_id[:8]}"
    transcript_path = transcript_observer.find_transcript(tmp_cwd, session_id)
    teams_dir = Path.home() / ".claude" / "teams"
    run_dir = tmp_cwd / ".operon" / RUN_NAME

    bundle = artifact_bundle.ArtifactBundle(
        root=tmp_cwd / "artifacts",
        scenario_name="subact_8_isolated",
    )
    recorder = step_recorder.StepRecorder(
        out_path=bundle.bundle_dir / "steps.jsonl"
    )
    meter = token_meter.TokenMeter(
        transcript_path=transcript_path, cap=TOKEN_CAP_ADVANCE_LIFECYCLE
    )
    driver = claude_driver_cls(
        session_name=session_name,
        cwd=tmp_cwd,
        plugin_dir=operon_plugin_dir,
        session_uuid=session_id,
        extra_env={"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
        model="sonnet",
    )
    observer = transcript_observer.TranscriptObserver(transcript_path)
    inbox_tracker = transcript_observer.InboxQuiescenceTracker(
        inboxes_dir=teams_dir / RUN_NAME / "inboxes",
    )
    bundle.configure_sources(
        events_log=tmp_cwd / ".operon" / RUN_NAME / "events.log",
        inboxes_dir=teams_dir / RUN_NAME / "inboxes",
        transcript=transcript_path,
    )

    phase_state_path = run_dir / "phase_state.json"
    lead_response_at_advance: str = ""  # for the diagnostic capture

    try:
        driver.start()

        # --- Step A: TeamCreate(coordinator) + activate_workflow ---
        recorder.set_sub_act("iso_setup_team_and_activate")
        recorder.record(
            user_observable="Fresh team + workflow activate, no coordination.",
            precise_state={"run_name": RUN_NAME},
        )
        time.sleep(2.0)
        marker_setup = idle.latest_stop_uuid(observer)
        driver.send(
            "Two-step setup. Step 1: call TeamCreate with "
            f"team_name='{RUN_NAME}' AND agent_type='coordinator'. "
            "Step 2: call mcp__operon__activate_workflow with "
            f"workflow_id='project_team' and run_name='{RUN_NAME}'. "
            "After both return, report ONLY the activation status "
            "and the current phase, nothing else."
        )
        ok_setup = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_setup,
        )
        assert ok_setup, "iso setup: lead timed out on TeamCreate+activate"
        assert phase_state_path.is_file(), (
            "iso setup: phase_state.json not created"
        )
        phase_state = json.loads(phase_state_path.read_text(encoding="utf-8"))
        assert phase_state.get("current_phase") == "vision", (
            f"iso setup: phase != vision; got {phase_state.get('current_phase')!r}"
        )

        # --- Step B: advance vision -> setup via manual-confirm ---
        recorder.set_sub_act("iso_advance_vision_to_setup")
        marker_adv1 = idle.latest_stop_uuid(observer)
        driver.send(
            "Call mcp__operon__advance_phase now. The vision "
            "phase's advance check is a manual-confirm "
            "('Vision approved by user?'). The confirmation form "
            "will be answered by the harness driving the TUI. "
            "After the tool returns, report ONLY the new "
            "current_phase value (no commentary)."
        )
        form_ok = driver.accept_elicit_form(
            wait_for_substring="Vision approved by user",
            timeout_s=180.0,
        )
        assert form_ok, (
            "iso advance: manual-confirm form never appeared.\n"
            + driver.capture_pane()
        )
        ok_adv1 = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=None,
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_adv1,
        )
        assert ok_adv1, "iso advance: idle wait timeout"
        ps2 = json.loads(phase_state_path.read_text(encoding="utf-8"))
        assert ps2.get("current_phase") == "setup", (
            f"iso advance: phase != setup; got {ps2.get('current_phase')!r}"
        )

        # --- Step C: spawn composability + implementer ---
        recorder.set_sub_act("iso_spawn_teammates")
        teammate_names = ["composability", "implementer"]
        marker_spawn = idle.latest_stop_uuid(observer)
        driver.send(
            "Execute now. In a SINGLE turn, issue TWO parallel "
            "Agent tool calls.\n\n"
            "Call 1 -- Agent(subagent_type='composability', "
            f"name='composability', team_name='{RUN_NAME}', "
            "run_in_background=true, prompt='You are "
            "composability. Wait for messages from the lead.')\n\n"
            "Call 2 -- Agent(subagent_type='implementer', "
            f"name='implementer', team_name='{RUN_NAME}', "
            "run_in_background=true, prompt='You are "
            "implementer. Wait for messages from the lead.')\n\n"
            "Make both calls in parallel in the SAME turn. After "
            "both return, reply with the literal text 'BOTH "
            "SPAWNED' and stop."
        )
        ok_spawn = idle.wait_idle_pre_kill(
            observer=observer,
            inboxes_tracker=inbox_tracker,
            timeout_s=SUB_ACT_TIMEOUT_S,
            k_ms=IDLE_K_MS,
            after_marker_uuid=marker_spawn,
        )
        assert ok_spawn, "iso spawn: idle wait timeout"
        # Poll team config for both members.
        names_now: set[str] = set()
        cfg_deadline = time.time() + 10.0
        while time.time() < cfg_deadline:
            try:
                cfg_now = json.loads(
                    (teams_dir / RUN_NAME / "config.json").read_text(encoding="utf-8")
                )
                names_now = {m.get("name") for m in cfg_now.get("members", [])}
            except (FileNotFoundError, json.JSONDecodeError):
                names_now = set()
            if all(t in names_now for t in teammate_names):
                break
            time.sleep(0.5)
        assert all(t in names_now for t in teammate_names), (
            f"iso spawn: missing teammates. names_now={names_now}"
        )

        # --- Step D: sub-act-8 cascade (no prior coordination!) ---
        recorder.set_sub_act("iso_subact_8_advance_setup_to_leadership")
        recorder.record(
            user_observable=(
                "Clean-room sub-act 8: cascading advance from "
                "setup -> leadership with NO prior teammate "
                "coordination, send_to_member, or flush rounds."
            ),
            precise_state={"from_phase": "setup", "to_phase": "leadership"},
        )

        # Harness-quiescence flush before cascade. Empirically
        # (Group B run #1): after spawn, the lead emits late
        # end_turns ("Implementer is idle and ready" etc.) that
        # arrive AFTER ok_spawn returned True. If marker_8 below
        # captures the spawn-time stop while those late end_turns
        # are still arriving, wait_idle returns True on a stale
        # signal before the lead processes the advance_phase send.
        # The settle+refresh forces marker_8 to capture the truly-
        # latest stop.
        time.sleep(5.0)
        observer.read_new()

        artifact_dir = tmp_cwd / ".operon" / RUN_NAME
        MAX_ADVANCE_RETRIES = 6
        MAX_PROSE_RETRIES = 2  # Land 14: retry budget for prose-only responses
        advance_attempts = 0
        observed_check_types: list[str] = []
        first_advance_response_recorded = False

        def _advance_phase_tool_uses_after(records, after_idx: int) -> int:
            """Count advance_phase tool_use entries from index after_idx onward."""
            count = 0
            for i in range(after_idx, len(records)):
                rec = records[i]
                if rec.get("isSidechain") is True:
                    continue
                if rec.get("type") != "assistant":
                    continue
                for c in (rec.get("message", {}).get("content") or []):
                    if (
                        isinstance(c, dict)
                        and c.get("type") == "tool_use"
                        and "advance_phase" in str(c.get("name", ""))
                    ):
                        count += 1
            return count

        for attempt in range(MAX_ADVANCE_RETRIES):
            marker_8 = idle.latest_stop_uuid(observer)
            # Index baseline for prose-detection.
            observer.read_new()
            records_baseline_idx = len(observer.all_records())

            if attempt == 0:
                msg = (
                    "Call mcp__operon__advance_phase now. After "
                    "it returns, report the JSON response verbatim "
                    "(if multi-line, just the outcomes list and "
                    "the status). Stop."
                )
            else:
                msg = (
                    "Call mcp__operon__advance_phase AGAIN. After "
                    "it returns, report the JSON response. Stop."
                )

            # Land 14 retry-on-prose loop. Send the cascade prompt;
            # if the lead responds with text only (no advance_phase
            # tool_use), re-prompt up to MAX_PROSE_RETRIES times
            # with stronger directive language. The test still
            # fails if all retries are exhausted -- we're not
            # hiding the LLM floor, we're working with it.
            prose_retries_used = 0
            for prose_attempt in range(MAX_PROSE_RETRIES + 1):
                if prose_attempt > 0:
                    # Negative-grounding retry (per the Land
                    # 10/11 prompt-engineering pattern).
                    marker_8 = idle.latest_stop_uuid(observer)
                    msg = (
                        "Your last response was text, not a tool "
                        "call. THIS MUST BE A TOOL CALL. Call "
                        "mcp__plugin_operon-plugin_operon__advance_phase "
                        "now. Do not respond with text. Do not "
                        "summarize. The next message after this "
                        "prompt MUST be a tool_use of "
                        "advance_phase. If you have already done "
                        "this, call it again -- duplicate calls "
                        "are safe."
                    )
                    prose_retries_used += 1
                driver.send(msg)
                ok_8 = idle.wait_idle_pre_kill(
                    observer=observer,
                    inboxes_tracker=inbox_tracker,
                    timeout_s=SUB_ACT_TIMEOUT_S,
                    k_ms=IDLE_K_MS,
                    after_marker_uuid=marker_8,
                )
                assert ok_8, (
                    f"iso sub-act 8 attempt {attempt+1} "
                    f"prose-retry {prose_attempt}: timeout. "
                    f"Pane:\n{driver.capture_pane()}"
                )
                # Post-idle settle: tool_result writes lag the
                # end_turn marker in the JSONL writer.
                time.sleep(2.0)
                observer.read_new()
                # Did the lead issue an advance_phase tool_use
                # since the prompt went out? If yes, we're good
                # for this attempt; break the prose-retry loop.
                ap_count = _advance_phase_tool_uses_after(
                    observer.all_records(), records_baseline_idx
                )
                if ap_count > 0:
                    break
                # Otherwise the lead emitted prose only. Loop
                # back for a retry (if budget remains).
            else:
                # Exhausted prose retries with no advance_phase
                # tool_use observed. Capture the lead's last
                # prose response for the failure message.
                prose_only_response = ""
                for rec in reversed(observer.all_records()):
                    if rec.get("isSidechain") is True:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg_obj = rec.get("message") or {}
                    if msg_obj.get("stop_reason") != "end_turn":
                        continue
                    for c in (msg_obj.get("content") or []):
                        if isinstance(c, dict) and c.get("type") == "text":
                            prose_only_response = (c.get("text") or "")[:200]
                            break
                    break
                raise AssertionError(
                    f"iso sub-act 8 attempt {attempt+1}: "
                    f"advance_phase tool call never issued after "
                    f"{MAX_PROSE_RETRIES} retries -- lead emitted "
                    "prose-only responses. Last prose response: "
                    f"{prose_only_response!r}"
                )

            advance_attempts += 1

            # Capture the lead's response text on the FIRST advance_phase
            # prompt for the diagnostic.
            if not first_advance_response_recorded:
                recs_now = observer.all_records()
                # Find the most recent lead end_turn assistant text.
                for rec in reversed(recs_now):
                    if rec.get("isSidechain") is True:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg_obj = rec.get("message") or {}
                    if msg_obj.get("stop_reason") != "end_turn":
                        continue
                    for c in (msg_obj.get("content") or []):
                        if isinstance(c, dict) and c.get("type") == "text":
                            lead_response_at_advance = (c.get("text") or "")[:400]
                            break
                    break
                first_advance_response_recorded = True

            ps = json.loads(phase_state_path.read_text(encoding="utf-8"))
            if ps.get("current_phase") != "setup":
                break

            recs_8 = observer.all_records()
            failed_check = _last_advance_failed_check(recs_8)
            if not failed_check:
                raise AssertionError(
                    f"iso sub-act 8 attempt {attempt+1}: advance_phase "
                    "did not advance and we couldn't determine the "
                    "failing check from the JSONL. Lead's response "
                    f"at advance prompt:\n{lead_response_at_advance!r}\n"
                    + "Pane:\n" + driver.capture_pane()
                )
            observed_check_types.append(failed_check["check_type"])

            ct = failed_check["check_type"]
            if ct == "artifact-dir-ready-check":
                marker_8s = idle.latest_stop_uuid(observer)
                driver.send(
                    f"Call mcp__operon__set_artifact_dir with "
                    f"path='{artifact_dir}'. After it returns, "
                    "reply 'ARTIFACT-DIR-SET' and stop."
                )
                ok_8s = idle.wait_idle_pre_kill(
                    observer=observer,
                    inboxes_tracker=inbox_tracker,
                    timeout_s=SUB_ACT_TIMEOUT_S,
                    k_ms=IDLE_K_MS,
                    after_marker_uuid=marker_8s,
                )
                assert ok_8s, "iso set_artifact_dir timeout"
            elif ct == "file-exists-check":
                missing = failed_check.get("missing_path") or ""
                if not missing:
                    raise AssertionError(
                        f"iso: file-exists-check failed but no path "
                        f"parsed. {failed_check}"
                    )
                missing_path = Path(missing)
                missing_path.parent.mkdir(parents=True, exist_ok=True)
                missing_path.write_text(
                    f"# Placeholder for {missing_path.name}\n",
                    encoding="utf-8",
                )

        final_phase = json.loads(phase_state_path.read_text(encoding="utf-8"))
        new_phase_iso = final_phase.get("current_phase")

        meter.checkpoint("iso_subact_8")
        bundle.snapshot(
            "iso_subact_8",
            token_state={
                "cumulative": meter.cumulative.__dict__,
                "billable": meter.cumulative.billable,
            },
            notes={
                "new_phase": new_phase_iso,
                "advance_attempts": advance_attempts,
                "observed_check_types": observed_check_types,
                "lead_response_at_advance": lead_response_at_advance,
            },
        )
        gate_check(
            "iso_subact_8",
            must_hold=[
                ("phase advanced past setup", new_phase_iso != "setup"),
                # In isolation the lead may PROACTIVELY satisfy
                # setup-phase prereqs from operon's brief, so the
                # cascade may be 0 iterations. The diagnostic
                # signal we care about is "did the lead call
                # advance_phase and did it succeed" -- advance
                # attempts >= 1 suffices.
                ("at least one advance_phase attempt", advance_attempts >= 1),
                ("under token cap", meter.cumulative.billable <= TOKEN_CAP_ADVANCE_LIFECYCLE),
            ],
        )

        # =============== STEP E (Sub-act 9): halt session ===============
        # User ends the session via /exit. Operon's run-dir state
        # (.operon/<run>/) and the sidechain transcripts under
        # ~/.claude/projects/<cwd-mangled>/ must SURVIVE the exit.
        # CC v2.1.150 empirically cleans ~/.claude/teams/<run>/ on
        # exit -- that's EXPECTED and fine; operon-side state is
        # what matters for the restore in sub-act 10.
        recorder.set_sub_act("iso_sub_act_9_halt_session")
        recorder.record(
            user_observable=(
                "User exits the session. Operon state + sidechain "
                "transcripts persist so restore can re-attach."
            ),
            precise_state={
                "operon_run_dir": str(run_dir),
                "project_dir": str(transcript_path.parent),
            },
        )
        sidechain_jsonls_pre = list(transcript_path.parent.glob("*.jsonl"))
        driver.send("/exit")
        ok_9 = idle.wait_idle_during_kill(
            pid_check=lambda: driver.session_pid(),
            timeout_s=30.0,
            poll_s=0.5,
        )
        if not ok_9:
            driver.kill()
        operon_run_dir_post = run_dir.is_dir()
        sidechain_jsonls_post = list(transcript_path.parent.glob("*.jsonl"))
        assert operon_run_dir_post, (
            f"sub-act 9: operon run-dir at {run_dir} did NOT "
            "survive the halt."
        )
        assert sidechain_jsonls_post, (
            f"sub-act 9: sidechain JSONL transcripts at "
            f"{transcript_path.parent} did NOT survive the halt."
        )
        meter.checkpoint("iso_sub_act_9")
        bundle.snapshot(
            "iso_sub_act_9_halt_session",
            token_state={
                "cumulative": meter.cumulative.__dict__,
                "billable": meter.cumulative.billable,
            },
            notes={
                "operon_run_dir_post": operon_run_dir_post,
                "sidechain_jsonls_pre": len(sidechain_jsonls_pre),
                "sidechain_jsonls_post": len(sidechain_jsonls_post),
                "team_dir_post": (teams_dir / RUN_NAME).is_dir(),
            },
        )
        gate_check(
            "iso_sub_act_9",
            must_hold=[
                ("operon run-dir survived halt", operon_run_dir_post),
                ("sidechain JSONLs survived halt", bool(sidechain_jsonls_post)),
                ("under token cap", meter.cumulative.billable <= TOKEN_CAP_ADVANCE_LIFECYCLE),
            ],
        )

        # =============== STEP F (Sub-act 10): restore + recall ===============
        # Launch a NEW CC session in the same cwd. Lead calls
        # restore_operon_session, then TeamCreate + Agent spawn for
        # composability. Operon's WA1 hook walks the sidechain
        # transcripts and prepends them into composability's spawn
        # prompt; composability replies via SendMessage with its
        # recall content.
        #
        # Recall assertion is ADAPTED for the isolated context.
        # The prior session's composability had only the spawn
        # handshake (no sub-act-6/7 history), so the concrete-fact
        # set is leaner than the chain test would use. MUST-see:
        # composability acknowledges its prior role/identity.
        # MUST-NOT-see: "no prior memory" disclaimer (WA1 failure).
        recorder.set_sub_act("iso_sub_act_10_restore_and_recall")
        recorder.record(
            user_observable=(
                "After exiting, the user resumes work later. "
                "Operon restores the team; WA1 feeds prior "
                "transcripts into the respawned composability; "
                "composability recalls its prior identity."
            ),
            precise_state={
                "restore_run_name": RUN_NAME,
                "halted_phase": new_phase_iso,
            },
        )
        session_id_10 = str(uuid.uuid4())
        session_name_10 = f"iso-ptw10-{session_id_10[:8]}"
        transcript_path_10 = transcript_observer.find_transcript(
            tmp_cwd, session_id_10
        )
        driver_10 = claude_driver_cls(
            session_name=session_name_10,
            cwd=tmp_cwd,
            plugin_dir=operon_plugin_dir,
            session_uuid=session_id_10,
            extra_env={"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
            model="sonnet",
        )
        try:
            driver_10.start()
            observer.rebind(transcript_path_10)
            meter.transcript_path = transcript_path_10
            meter.seen_uuids.clear()
            time.sleep(3.0)

            # ===== STEP F-pre (Sub-act 9b): stale-session guardrail =====
            # A NEW session is now open in the same cwd, but it has NOT
            # yet resumed the run (no restore_operon_session call yet).
            # The prior session's run-dir state still sits on disk and
            # still names the active run + halted phase. A workflow-
            # embedded deny rule (no_push_before_testing) must NOT gate
            # this unrelated session before it claims the run --
            # ownership binds at restore, not at "a session happens to
            # be open in the project directory". The global 3-tier
            # rules (warn_sudo) MUST still fire: ownership gating drops
            # ONLY the workflow-embedded rules for a non-owner session,
            # never the plugin/user/project tiers.
            recorder.set_sub_act("iso_sub_act_9b_stale_session_guardrail")
            recorder.record(
                user_observable=(
                    "Before resuming, the freshly-opened session must "
                    "not inherit the paused run's workflow guardrails; "
                    "the global guardrails still apply."
                ),
                precise_state={
                    "halted_phase": new_phase_iso,
                    "prior_session_id": session_id,
                    "new_session_id": session_id_10,
                },
            )
            # Precondition: the halted phase must be one where
            # no_push_before_testing is ACTIVE (not in its
            # exclude_phases), or the workflow-rule probe is vacuous.
            # The lifecycle halts in 'leadership', well before the
            # excluded testing/documentation/signoff phases.
            assert new_phase_iso not in (
                "testing-implementation", "documentation", "signoff"
            ), (
                "sub-act 9b precondition: halted phase "
                f"{new_phase_iso!r} is in no_push_before_testing's "
                "exclude_phases, so the workflow-rule probe cannot "
                "distinguish gated from skipped."
            )

            # Probe 1 (MUST-NOT fire): a workflow-embedded deny rule.
            # Pattern 'git push' matches; --dry-run keeps it harmless
            # (the fixture cwd has no configured remote). Pre-fix the
            # hook applies the active run's workflow rules to ANY
            # session in the cwd, so this fires -- the bug.
            base_push = len(observer.all_records())
            marker_9b1 = idle.latest_stop_uuid(observer)
            driver_10.send(
                "Run the bash command `git push --dry-run` exactly "
                "as written. If a guardrail message appears, report "
                "it verbatim and stop -- do not retry, do not "
                "analyse, do not propose alternatives."
            )
            ok_9b1 = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=None,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_9b1,
            )
            assert ok_9b1, (
                "sub-act 9b probe 1 (git push): idle wait timeout.\n"
                + driver_10.capture_pane()
            )
            time.sleep(2.0)
            push_blob = json.dumps(
                observer.all_records()[base_push:], ensure_ascii=False
            )
            workflow_push_fired = (
                "before it reaches the testing phase" in push_blob
            )

            # Probe 2 (MUST fire): a global 3-tier rule, proving the
            # gate drops ONLY the workflow tier for the non-owner
            # session and leaves the global tiers intact.
            base_sudo = len(observer.all_records())
            marker_9b2 = idle.latest_stop_uuid(observer)
            driver_10.send(
                "Run the bash command `sudo ls /tmp/` exactly as "
                "written. If a guardrail message appears, report it "
                "verbatim and stop -- do not retry, do not analyse."
            )
            ok_9b2 = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=None,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_9b2,
            )
            assert ok_9b2, (
                "sub-act 9b probe 2 (sudo): idle wait timeout.\n"
                + driver_10.capture_pane()
            )
            time.sleep(2.0)
            sudo_blob = json.dumps(
                observer.all_records()[base_sudo:], ensure_ascii=False
            )
            global_sudo_fired = "Using sudo" in sudo_blob

            meter.checkpoint("iso_sub_act_9b")
            bundle.snapshot(
                "iso_sub_act_9b_stale_session_guardrail",
                token_state={
                    "cumulative": meter.cumulative.__dict__,
                    "billable": meter.cumulative.billable,
                },
                notes={
                    "halted_phase": new_phase_iso,
                    "workflow_push_fired": workflow_push_fired,
                    "global_sudo_fired": global_sudo_fired,
                },
            )
            # MUST-NOT-see: the workflow rule gated a session that
            # never resumed the run. This is the bug under fix; the
            # assertion is RED until session-ownership gating lands.
            assert not workflow_push_fired, (
                "MUST-NOT-see: no_push_before_testing fired for a "
                "session that has not resumed the run. A stale paused "
                "run gated an unrelated new session -- the "
                "session-ownership gate is missing.\n"
                f"Records excerpt: {push_blob[:600]!r}"
            )
            # MUST-see: the global tier still active for the new
            # session (we dropped only the workflow tier).
            assert global_sudo_fired, (
                "MUST-see: warn_sudo (a global 3-tier rule) did not "
                "fire for the new session. Ownership gating must drop "
                "ONLY workflow-embedded rules, never the global "
                "tiers.\n"
                f"Records excerpt: {sudo_blob[:600]!r}"
            )
            gate_check(
                "iso_sub_act_9b",
                must_hold=[
                    ("workflow rule skipped for non-owner session",
                     not workflow_push_fired),
                    ("global rule still fires for non-owner session",
                     global_sudo_fired),
                    ("under token cap",
                     meter.cumulative.billable <= TOKEN_CAP_ADVANCE_LIFECYCLE),
                ],
            )

            # 10A: restore_operon_session.
            marker_10a = idle.latest_stop_uuid(observer)
            driver_10.send(
                "Call mcp__operon__restore_operon_session with "
                f"run_name='{RUN_NAME}'. After it returns, report "
                "ONLY the JSON response and stop."
            )
            ok_10a = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=None,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_10a,
            )
            assert ok_10a, (
                "sub-act 10A: restore_operon_session timed out.\n"
                + driver_10.capture_pane()
            )

            # 10B: TeamCreate + Agent spawn composability.
            marker_10b = idle.latest_stop_uuid(observer)
            driver_10.send(
                "Follow the lead_instructions from "
                "restore_operon_session: (1) call TeamCreate with "
                f"team_name='{RUN_NAME}' and agent_type='coordinator'; "
                "(2) use the Agent tool to spawn composability with "
                "run_in_background=true, name='composability', "
                f"subagent_type='composability', team_name='{RUN_NAME}', "
                "and prompt='You are composability; the operon WA1 "
                "hook will feed prior transcripts.'. After both "
                "return, reply 'RESTORED' and stop."
            )
            ok_10b = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=None,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_10b,
            )
            assert ok_10b, (
                "sub-act 10B: TeamCreate + Agent spawn timed out.\n"
                + driver_10.capture_pane()
            )
            time.sleep(2.0)
            observer.read_new()

            # 10C: lead routes recall probe via send_to_member.
            lead_inbox_path_10 = teams_dir / RUN_NAME / "inboxes" / "team-lead.json"
            baseline_lead_10 = inbox_watcher.inbox_baseline(lead_inbox_path_10)
            marker_10c = idle.latest_stop_uuid(observer)
            driver_10.send(
                "Call mcp__operon__send_to_member now. Parameters:\n"
                "  name='composability'\n"
                "  text: 'What did we discuss in the prior session? "
                "Be specific. Describe in plain prose:\\n"
                "  (1) who you are and what role you played;\\n"
                "  (2) any messages you sent or received;\\n"
                "  (3) anything else you remember from before "
                "this session.\\nSend your full recall as a "
                "SendMessage to team-lead, prefixed by "
                "\"RECALL: \". Then stop.'\n\n"
                "DO NOT use the Monitor or Task tools to wait. "
                "Return immediately after send_to_member with a "
                "brief status line and end the turn."
            )
            ok_10c = idle.wait_idle_pre_kill(
                observer=observer,
                inboxes_tracker=None,
                timeout_s=SUB_ACT_TIMEOUT_S,
                k_ms=IDLE_K_MS,
                after_marker_uuid=marker_10c,
            )
            assert ok_10c, (
                "sub-act 10C: lead idle wait timeout.\n"
                + driver_10.capture_pane()
            )

            recall_msg = inbox_watcher.wait_for_inbox_entry(
                lead_inbox_path_10,
                predicate=lambda e: (
                    e.get("from") == "composability"
                    and "RECALL:" in (e.get("text") or "")
                ),
                timeout_s=SUB_ACT_TIMEOUT_S,
                baseline_count=baseline_lead_10,
            )
            assert recall_msg is not None, (
                "MUST-see: composability did not send a RECALL: "
                f"message to team-lead's inbox within "
                f"{SUB_ACT_TIMEOUT_S}s."
            )
            recall_text = recall_msg.get("text") or ""
            recall_lower = recall_text.lower()

            # Adapted concrete-fact set for the isolated context.
            # The prior session's composability had only the spawn
            # handshake -- no rich coordination history. So we
            # check for prior-identity acknowledgment + spawn
            # acknowledgment + role acknowledgment.
            recall_concrete_facts = [
                ("acknowledges composability identity",
                 "composability" in recall_lower),
                ("mentions prior session/spawn/wait",
                 any(t in recall_lower for t in (
                     "spawn", "prior", "previous", "earlier",
                     "ready", "waiting", "wait for"))),
                ("references role context",
                 any(t in recall_lower for t in (
                     "role", "agent", "lead", "team"))),
            ]
            recall_hits = [name for name, hit in recall_concrete_facts if hit]
            recall_no_memory = any(
                phrase in recall_lower
                for phrase in (
                    "i don't have any prior",
                    "no prior session",
                    "fresh session",
                    "i have no memory of",
                    "no memory of",
                )
            )
            assert len(recall_hits) >= 2, (
                "MUST-see: composability's recall after restore hit "
                f"fewer than 2 of the 3 adapted concrete facts. "
                f"Hits: {recall_hits}. Recall body: "
                f"{recall_text[:400]!r}"
            )
            assert not recall_no_memory, (
                "MUST-NOT-see: composability claimed it has no "
                "prior memory -- WA1 transcript injection did not "
                f"fire. Recall body: {recall_text[:400]!r}"
            )

            meter.checkpoint("iso_sub_act_10")
            bundle.snapshot(
                "iso_sub_act_10_restore_and_recall",
                token_state={
                    "cumulative": meter.cumulative.__dict__,
                    "billable": meter.cumulative.billable,
                },
                notes={
                    "restore_session_id": session_id_10,
                    "recall_hits": recall_hits,
                    "recall_excerpt": recall_text[:500],
                },
            )
            gate_check(
                "iso_sub_act_10",
                must_hold=[
                    ("at least 2 adapted concrete facts recalled",
                     len(recall_hits) >= 2),
                    ("no 'no prior memory' disclaimer",
                     not recall_no_memory),
                    ("under token cap",
                     meter.cumulative.billable <= TOKEN_CAP_ADVANCE_LIFECYCLE),
                ],
            )
        finally:
            driver_10.kill()
    finally:
        driver.kill()
