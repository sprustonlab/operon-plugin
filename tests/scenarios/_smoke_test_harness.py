"""Smoke test for the harness modules. NOT one of the spec'd scenarios.

This file exercises tmux_driver + transcript_observer + cc_version_gate
end-to-end without burning a meaningful number of tokens: it launches a
claude session, sends `/exit` to quit cleanly, and verifies the
transcript file appeared at the predicted path.

Run with:
  pytest tests/scenarios/_smoke_test_harness.py -v --timeout=120

This test exists to validate the harness before the full
project_team_workflow scenario runs. It is named with a leading
underscore so pytest's default discovery still picks it up but it
sorts apart from the spec'd scenario file.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

# pylint: disable=import-error
from _harness import cc_version_gate, transcript_observer, tmux_driver  # noqa: E402


def test_harness_smoke_launch_claude(tmp_cwd, operon_plugin_dir):
    """Launch claude via tmux, verify a transcript is produced, exit cleanly."""
    # Version drift gate (Q1 -- pin 2.1.148 per coordinator).
    cc_version_gate.assert_cc_version()

    session_id = str(uuid.uuid4())
    session_name = f"smoke-{session_id[:8]}"
    driver = tmux_driver.TmuxClaudeDriver(
        session_name=session_name,
        cwd=tmp_cwd,
        plugin_dir=operon_plugin_dir,
        session_uuid=session_id,
        extra_env={"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
    )

    try:
        driver.start()
        transcript_path = transcript_observer.find_transcript(tmp_cwd, session_id)

        # CC v2.1.148 does not create the project transcript dir until
        # the first turn lands. Wait for the TUI to settle on its
        # input prompt, then send one minimal message to force a turn.
        time.sleep(3.0)
        driver.send("Reply with just the word OK and stop.")

        deadline = time.time() + 120
        while time.time() < deadline:
            if transcript_path.exists() and transcript_path.stat().st_size > 0:
                break
            time.sleep(0.5)
        assert transcript_path.exists() and transcript_path.stat().st_size > 0, (
            f"transcript never populated at {transcript_path} within 120s. "
            f"Pane buffer:\n{driver.capture_pane()}"
        )

        obs = transcript_observer.TranscriptObserver(transcript_path)
        recs = obs.all_records()
        assert recs, "transcript file exists but contains no JSONL records"
        types_seen = {r.get("type") for r in recs}
        assert {"user", "assistant"} & types_seen, (
            f"expected at least one user or assistant record; saw types={types_seen}"
        )
    finally:
        driver.kill()
