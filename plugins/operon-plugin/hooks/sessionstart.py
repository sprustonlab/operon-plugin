#!/usr/bin/env python3
"""SessionStart hook entrypoint (session-ownership marker).

The MCP server never receives the real Claude Code session_id -- it
synthesizes a bootstrap id at startup. This hook DOES see the live
session_id (Claude Code passes it on stdin), so it records it to
`<cwd>/.operon/_session.json`. `activate_workflow` and
`restore_operon_session` read that marker and stamp `owner_session_id`
into the run's state.json, binding run ownership to the session that
explicitly activated or resumed it.

The PreToolUse hook then applies a run's workflow-embedded guardrail
rules ONLY to its owner session -- so a stale paused run left behind by
a quit session cannot gate an unrelated new session opened in the same
project before it resumes.

Hook input shape (per Claude Code hooks-reference):
    {
      "session_id": "...",
      "hook_event_name": "SessionStart",
      "source": "startup" | "resume" | "clear" | "compact",
      "cwd": "...",
      "transcript_path": "...",
      "model": "..."
    }

This hook produces no decision output -- SessionStart hooks may emit
`additionalContext`, but operon only needs the side effect of writing
the marker. It always exits 0 and fails open: any error (missing
session_id, unwritable `.operon/`, bad stdin) is swallowed so a session
never fails to start because of operon.

PYTHONPATH expectation: the companion `sessionstart-wrapper` (bash/cmd)
prepends `${CLAUDE_PLUGIN_ROOT}/src` so this script can
`import operon_mcp_server.workflow` without `pip install -e .`.

Cross-platform per SPEC section 2: pathlib, encoding="utf-8", no
platform-gated APIs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from operon_mcp_server import workflow


def main() -> None:
    """Read the hook input, record the session marker, exit 0."""
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001 -- never block session start
        raise SystemExit(0)

    try:
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise SystemExit(0)

    if not isinstance(hook_input, dict):
        raise SystemExit(0)

    session_id = hook_input.get("session_id")
    if not (isinstance(session_id, str) and session_id):
        raise SystemExit(0)

    # `cwd` anchors which project's `.operon/` gets the marker. Fall back
    # to the process cwd if the field is absent.
    cwd = hook_input.get("cwd")
    start = Path(cwd) if isinstance(cwd, str) and cwd else None

    try:
        workflow.write_session_marker(session_id, start=start)
    except Exception:  # noqa: BLE001 -- best-effort, never block start
        pass

    raise SystemExit(0)


if __name__ == "__main__":
    main()
