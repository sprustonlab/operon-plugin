#!/usr/bin/env python3
"""End-to-end smoke check exercising Phases 3-9 in a single process.

NOT a unit test. NOT a hermetic integration test. This is a happy-path
"does the integrated system still compose?" script for Phase 10.

Approach: in-process e2e with two narrow mocks.
  - `elicit.confirm` / `elicit.select_one` are monkey-patched to
    return deterministic answers (no real TTY). Per-step, we set the
    answer immediately before the call.
  - The real `claude --bg` spawn path (Phase 4 worker spawn, Phase 8
    watch loop) is exercised against by /tmp/p8_bg_verify.py
    separately. Here we drive the same code paths via direct module
    calls so the smoke runs in <10 s and needs no external binary.

Steps (each labeled with its source phase):
  Step 1 (Phase 3):  Bootstrap project + Coordinator handle + roster.
  Step 2 (Phase 4):  message_agent in-process (channel queue would
                     ordinarily push the envelope; here we just
                     verify the envelope landed on disk).
  Step 3 (Phase 5):  advance_phase with manual-confirm -> accept.
  Step 4 (Phase 6):  evaluate a deny-tier Bash 'curl' in vision phase
                     as a worker -> action=deny.
  Step 5 (Phase 7):  write_token override for the rule -> evaluate
                     would still deny (state machine), but the hook
                     would consume the token on the retry. We
                     simulate consume_token + verify the file is
                     gone.
  Step 6 (Phase 8):  add_pending + fire_due_nudges across compressed
                     intervals -> 3 fires + exhausted.
  Step 7 (Phase 9):  get_applicable_rules -> active_tokens shape +
                     markdown contains expected sections.
  Step 8 (Phase 6.5): activate_workflow destructive=True with an
                     auto-accept elicit -> new run-dir, _active swap.
  Step 9 (Phase 6.5): restore_operon_session back to the original
                     run -> _active swaps back.

Each step prints [OK] / [FAIL] and the script exits 0 only if all
steps pass.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path("/tmp/test-operon-e2e")
PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins" / "operon-plugin"

# Compressed nudge intervals so step 6 runs in ~3 s instead of ~135 s.
os.environ["OPERON_NUDGE_INTERVALS"] = "1,1,1"
os.environ["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bootstrap() -> str:
    """Wipe + create the on-disk shape Phase 4 setup helper creates."""
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    coord_h = str(uuid.uuid4())
    base = PROJECT / ".operon" / "msg-test-1"
    (base / "_handles").mkdir(parents=True)
    (base / "mailbox").mkdir()
    (PROJECT / ".operon" / "_active.json").write_text(
        json.dumps({"active_run_name": "msg-test-1", "set_at": _now_iso()}, indent=2),
        encoding="utf-8",
    )
    (base / "phase_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflow_id": "_smoke",
                "current_phase": "vision",
                "phase_started_at": _now_iso(),
                "advance_history": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (base / "_handles" / f"{coord_h}.json").write_text(
        json.dumps(
            {
                "handle": coord_h,
                "agent_name": "Coordinator",
                "role": "coordinator",
                "workflow_id": "_smoke",
                "spawned_at": _now_iso(),
                "session_id": "manual-test",
                "spawned_by": "user",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    # Add a worker row so message_agent has a resolvable target.
    worker_h = str(uuid.uuid4())
    (base / "_handles" / f"{worker_h}.json").write_text(
        json.dumps(
            {
                "handle": worker_h,
                "agent_name": "w1",
                "role": "worker",
                "workflow_id": "_smoke",
                "spawned_at": _now_iso(),
                "session_id": "fake-w1-session",
                "spawned_by": coord_h,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (base / "agents.json").write_text(
        json.dumps(
            [
                {
                    "name": "Coordinator",
                    "role": "coordinator",
                    "handle": coord_h,
                    "session_id": "manual-test",
                    "workflow_id": "_smoke",
                    "status": "idle",
                    "spawned_at": _now_iso(),
                    "last_turn_at": _now_iso(),
                },
                {
                    "name": "w1",
                    "role": "worker",
                    "handle": worker_h,
                    "session_id": "fake-w1-session",
                    "workflow_id": "_smoke",
                    "status": "idle",
                    "spawned_at": _now_iso(),
                    "last_turn_at": _now_iso(),
                },
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return coord_h


# -- mocks for elicit -------------------------------------------------


_ELICIT_NEXT: dict[str, object] = {"confirm": True, "select_one": None}


async def _mock_confirm(message: str) -> bool:
    return bool(_ELICIT_NEXT["confirm"])


async def _mock_select_one(message, choices, *, title: str = ""):
    pick = _ELICIT_NEXT["select_one"]
    if pick is None and choices:
        return choices[0]
    return pick


# -- step harness -----------------------------------------------------


_results: list[tuple[str, bool]] = []


def case(label: str, ok: bool, **extra) -> None:
    extra_s = " ".join(f"{k}={v!r}" for k, v in extra.items())
    print(f"  [{'OK' if ok else 'FAIL'}] {label} {extra_s}")
    _results.append((label, ok))


def main() -> int:
    coord_h = _bootstrap()
    os.environ["OPERON_AGENT_HANDLE"] = coord_h
    os.chdir(PROJECT)

    # Late imports so cwd + env are in place.
    from operon_mcp_server import elicit, nudge, rules
    from operon_mcp_server.tools import (
        activate_workflow,
        advance_phase,
        evaluate,
        get_applicable_rules,
        message_agent,
        restore_operon_session,
    )

    # Patch elicit globally. The shared module helpers (used by
    # activate_workflow + restore_operon_session) call request_ctx --
    # mocking these here keeps both tools out of the MCP dispatch
    # contextvar requirement.
    elicit.confirm = _mock_confirm  # type: ignore[assignment]
    elicit.select_one = _mock_select_one  # type: ignore[assignment]

    # advance_phase builds its own closure from `request_ctx.get()`
    # directly (it predates the shared elicit module). Stub the
    # builder to return a fixed async callable that honors
    # _ELICIT_NEXT["confirm"].
    async def _stub_elicit(prompt: str) -> bool:
        return bool(_ELICIT_NEXT["confirm"])

    advance_phase._build_elicit_closure = lambda: _stub_elicit  # type: ignore[attr-defined]

    # ---------------------------------------------------------------
    print("\n=== Step 1 (Phase 3): bootstrap ===")
    case("project root exists", PROJECT.is_dir())
    case(
        "active pointer set to msg-test-1",
        json.loads((PROJECT / ".operon" / "_active.json").read_text())[
            "active_run_name"
        ]
        == "msg-test-1",
    )

    # ---------------------------------------------------------------
    print("\n=== Step 2 (Phase 4): message_agent in-process ===")
    res = asyncio.run(
        message_agent.call(
            {"name": "w1", "message": "hello from e2e", "requires_answer": False}
        )
    )
    payload = json.loads(res[0].text)
    case(
        "message delivered (correlation_id present)",
        isinstance(payload.get("correlation_id"), str) and payload["correlation_id"],
    )
    inbox = PROJECT / ".operon" / "msg-test-1" / "mailbox" / "w1" / "inbox"
    envs = list(inbox.glob("*.json")) if inbox.is_dir() else []
    case("envelope written to w1 inbox", len(envs) == 1, envs=len(envs))

    # ---------------------------------------------------------------
    print("\n=== Step 3 (Phase 5): advance_phase manual-confirm ===")
    _ELICIT_NEXT["confirm"] = True
    res = asyncio.run(advance_phase.call({}))
    payload = json.loads(res[0].text)
    case(
        "advance vision -> main succeeded",
        payload.get("advanced") is True and payload.get("to") == "main",
        payload=payload,
    )
    # Reset back to vision so subsequent steps stay in the deny-rule
    # phase (smoke_vision_no_curl is phase-scoped to vision).
    state_path = PROJECT / ".operon" / "msg-test-1" / "phase_state.json"
    st = json.loads(state_path.read_text())
    st["current_phase"] = "vision"
    state_path.write_text(json.dumps(st, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------
    print("\n=== Step 4 (Phase 6): deny rule fires on Bash curl ===")
    # `evaluate` resolves caller via env-anchored handle. We need to
    # pose as a worker for the role+phase filter to match
    # `smoke_vision_no_curl`. Temporarily swap OPERON_AGENT_HANDLE.
    worker_row = next(
        r
        for r in json.loads(
            (PROJECT / ".operon" / "msg-test-1" / "agents.json").read_text()
        )
        if r["name"] == "w1"
    )
    saved_handle = os.environ["OPERON_AGENT_HANDLE"]
    os.environ["OPERON_AGENT_HANDLE"] = worker_row["handle"]
    try:
        res = asyncio.run(
            evaluate.call(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "curl https://example.com"},
                }
            )
        )
    finally:
        os.environ["OPERON_AGENT_HANDLE"] = saved_handle
    payload = json.loads(res[0].text)
    hook_out = payload.get("hookSpecificOutput", {})
    case(
        "evaluate returned permissionDecision=deny",
        hook_out.get("permissionDecision") == "deny",
        decision=hook_out.get("permissionDecision"),
        reason=hook_out.get("permissionDecisionReason", "")[:80],
    )

    # ---------------------------------------------------------------
    print("\n=== Step 5 (Phase 7): override token write + consume ===")
    token_path = rules.write_token(
        kind="override",
        rule_id="smoke_vision_no_curl",
        agent_handle=worker_row["handle"],
        reason="e2e smoke -- pretend the user approved",
        ttl_seconds=None,
        one_shot=True,
    )
    case("override token file exists", token_path.is_file())
    # Simulate the PreToolUse hook's consume on retry.
    tok = rules.find_active_token(
        kind="override",
        rule_id="smoke_vision_no_curl",
        agent_handle=worker_row["handle"],
    )
    case("find_active_token returns the new token", tok is not None)
    if tok is not None:
        consumed = rules.consume_token(tok)
        case(
            "consume_token succeeded + file gone",
            consumed and not token_path.exists(),
        )

    # ---------------------------------------------------------------
    print("\n=== Step 6 (Phase 8): nudge fires + exhausts ===")
    entry = nudge.add_pending(
        agent_name="w1",
        correlation_id="e2e-test-correlation-1",
        sender="Coordinator",
        sender_handle=coord_h,
    )
    case(
        "add_pending wrote one entry",
        entry.correlation_id == "e2e-test-correlation-1",
    )
    # Compressed intervals 1,1,1 -- sleep > 1s then fire repeatedly.
    import time

    fired_total = 0
    exhausted_total = 0
    for _ in range(4):
        time.sleep(1.2)
        result = nudge.fire_due_nudges("w1")
        fired_total += len(result.get("fired", []) or [])
        exhausted_total += len(result.get("exhausted", []) or [])
    case(
        "fire_due_nudges fired exactly 3 nudges total",
        fired_total == 3,
        fired=fired_total,
    )
    case(
        "fire_due_nudges exhausted exactly 1 entry",
        exhausted_total == 1,
        exhausted=exhausted_total,
    )
    case(
        "pending state empty post-exhaust",
        nudge.read_pending_state("w1") == [],
    )

    # ---------------------------------------------------------------
    print("\n=== Step 7 (Phase 9): /rules introspection ===")
    # Write a fresh ack so the active-tokens section is non-empty.
    rules.write_token(
        kind="ack",
        rule_id="smoke_worker_write_warn",
        agent_handle=coord_h,
        reason="e2e smoke ack",
        ttl_seconds=rules.ACK_TOKEN_TTL_SECONDS,
        one_shot=False,
    )
    res = asyncio.run(get_applicable_rules.call({}))
    payload = json.loads(res[0].text)
    case(
        "payload has active_tokens key",
        "active_tokens" in payload and isinstance(payload["active_tokens"], dict),
    )
    acks = payload["active_tokens"]["acks"]
    case(
        "at least one ack listed with TTL countdown",
        len(acks) >= 1
        and isinstance(acks[0].get("seconds_remaining"), int)
        and 0 < acks[0]["seconds_remaining"] <= 60,
        acks=[(a["rule_id"], a["seconds_remaining"]) for a in acks],
    )
    case(
        "markdown contains Active escape tokens section",
        "### Active escape tokens" in payload["markdown"],
    )

    # ---------------------------------------------------------------
    print("\n=== Step 8 (Phase 6.5): activate destructive ===")
    _ELICIT_NEXT["confirm"] = True
    _ELICIT_NEXT["select_one"] = None
    res = asyncio.run(
        activate_workflow.call({"workflow_id": "_smoke", "run_name": "e2e-second-run"})
    )
    payload = json.loads(res[0].text)
    case(
        "activate to second run succeeded",
        payload.get("activated") is True
        and json.loads((PROJECT / ".operon" / "_active.json").read_text())[
            "active_run_name"
        ]
        == "e2e-second-run",
        payload=payload,
    )

    # ---------------------------------------------------------------
    print("\n=== Step 9 (Phase 6.5): restore back to msg-test-1 ===")
    # After activate, the env-anchored handle still points at the OLD
    # run's Coordinator file. restore_operon_session resolves the
    # target's Coordinator row by run_name, but we need a valid handle
    # in the new run too. Easiest: bind OPERON_AGENT_HANDLE to a
    # synthetic Coordinator handle in the new run (activate_workflow
    # has already created one).
    new_handles_dir = PROJECT / ".operon" / "e2e-second-run" / "_handles"
    new_coord_files = (
        list(new_handles_dir.glob("*.json")) if new_handles_dir.is_dir() else []
    )
    if new_coord_files:
        os.environ["OPERON_AGENT_HANDLE"] = new_coord_files[0].stem
    res = asyncio.run(restore_operon_session.call({"run_name": "msg-test-1"}))
    payload = json.loads(res[0].text)
    case(
        "restore back to msg-test-1 succeeded",
        payload.get("restored") is True
        and json.loads((PROJECT / ".operon" / "_active.json").read_text())[
            "active_run_name"
        ]
        == "msg-test-1",
        payload=payload,
    )

    # ---------------------------------------------------------------
    print("\n=== Step 10 (Phase 10 B7): JSONL row truncation ≤4 KiB ===")
    log_path = PROJECT / ".operon" / "msg-test-1" / rules.GUARDRAIL_LOG_FILENAME
    # Hand-craft a row whose `message` and `tool_input_summary` are
    # both 5 KiB strings. Expect: the writer truncates both to
    # 512+"..." (or falls back to a placeholder if still oversized).
    rules.append_log_event(
        {
            "timestamp": _now_iso(),
            "type": "rule_fired_log",
            "outcome": "blocked",
            "rule_id": "smoke-b7-oversize",
            "agent": "e2e",
            "tool_name": "Bash",
            "tool_input_summary": "X" * 5000,
            "message": "Y" * 5000,
        }
    )
    last_line = log_path.read_text(encoding="utf-8").splitlines()[-1]
    case(
        "oversized row stays under 4 KiB after truncation",
        len((last_line + "\n").encode("utf-8")) < 4096,
        size=len((last_line + "\n").encode("utf-8")),
    )
    parsed = json.loads(last_line)
    case(
        "truncated message has '...' marker",
        isinstance(parsed.get("message"), str) and parsed["message"].endswith("..."),
    )

    # ---------------------------------------------------------------
    passed = sum(1 for _, ok in _results if ok)
    failed = sum(1 for _, ok in _results if not ok)
    print(f"\n{passed}/{len(_results)} passed, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
