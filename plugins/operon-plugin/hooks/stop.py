#!/usr/bin/env python3
"""Stop hook entrypoint (Phase 8 nudge mechanism, SPEC §8).

Fires when an Agent ends its turn. Reads the agent's
`_pending_reply_to.json` to check whether any reply obligation is
past-due; if so, writes a `kind=nudge_check` control envelope into
the agent's own mailbox. The MCP server's watch loop picks up the
control envelope and runs the actual fire-or-exhaust logic INSIDE
its event loop, so the SPEC §6.6 single-writer rule on the pending
state file is preserved.

This hook does NOT mutate `_pending_reply_to.json` directly. It is a
read-only signaler.

Hook input shape (Claude Code hooks-reference): JSON on stdin with
session_id, stop_hook_active, hook_event_name="Stop", etc. We
don't use any of those fields; identity is env-anchored via
OPERON_AGENT_HANDLE.

Output: Stop hooks have minimal output semantics. We emit an empty
JSON object + exit 0 so Claude Code treats it as a no-op response.
The actual side effect is the control envelope drop. Exit code 0
regardless to keep the hook non-blocking.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from operon_mcp_server import identity, mailbox, nudge


def _debug_enabled() -> bool:
    flag = os.environ.get("OPERON_DEBUG", "").strip().lower()
    return flag not in {"", "0", "false", "no"}


if _debug_enabled():
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG,
        format="[stop] %(levelname)s: %(message)s",
    )

_log = logging.getLogger(__name__)


def _emit_noop() -> None:
    """Stop hooks treat empty stdout as 'allow continuation'. We emit
    a clean object so future Claude Code versions with stricter
    parsing still see valid JSON."""
    sys.stdout.write("{}")
    sys.stdout.flush()


def _resolve_self() -> str | None:
    """Read OPERON_AGENT_HANDLE + handle file -> agent_name."""
    handle = identity.read_env_handle()
    if handle is None:
        return None
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        _log.warning("identity read failed: %s", exc)
        return None
    if record is None:
        return None
    name = record.get("agent_name")
    return name if isinstance(name, str) and name else None


def main() -> None:
    # Drain stdin (Claude Code feeds the hook payload; we don't use it,
    # but failing to read can cause SIGPIPE on the upstream).
    try:
        _ = sys.stdin.read()
    except Exception:
        pass

    self_name = _resolve_self()
    if self_name is None:
        _log.debug("no identity; skipping Stop check")
        _emit_noop()
        return

    # Read pending state (no writes). If any entry is past-due, signal
    # the MCP. The MCP's watch loop will check generation and fire
    # nudges via fire_due_nudges; we just tap on the shoulder.
    try:
        entries = nudge.read_pending_state(self_name)
    except Exception as exc:
        _log.warning("pending read failed: %s", exc)
        _emit_noop()
        return

    now = datetime.now(timezone.utc)
    due = [e for e in entries if e.is_due(now)]
    _log.debug(
        "agent=%r pending=%d past_due=%d",
        self_name,
        len(entries),
        len(due),
    )

    if not due:
        _emit_noop()
        return

    try:
        path = nudge.signal_nudge_check(self_name, reason="stop_hook")
        _log.debug("nudge_check signal written to %s", path)
    except mailbox.MailboxError as exc:
        _log.warning("nudge_check signal write failed: %s", exc)

    _emit_noop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write(f"[stop] fatal: {exc!r}\n")
        # Always emit something + exit 0; Stop hooks shouldn't block.
        sys.stdout.write("{}")
        sys.exit(0)
