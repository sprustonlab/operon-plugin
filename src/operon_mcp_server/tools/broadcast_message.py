"""Implementation of the `broadcast_message` MCP tool.

Per SPEC.md section 7 (`broadcast_message` row) and section 7.1 (visible
to All roles). Sends the same message to N recipients via the
`message_agent` envelope path. Per-target failures are recorded in
`<run-dir>/broadcast_results.jsonl` (SPEC §17, append-only log per
§6.6).

The caller's own name is silently skipped if present in `names` (mirrors
the claudechic behavior so a Coordinator can address all-by-name without
filtering itself out first).

Identity gating mirrors `message_agent`: the sender is anchored to
`OPERON_AGENT_HANDLE`; LLM-supplied identity claims are ignored.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

from .. import mailbox, paths
from . import message_agent as message_agent_tool

#: MCP tool name. Visible to All per SPEC §7.1.
TOOL_NAME = "broadcast_message"

#: Filename for the broadcast results log under `<run-dir>/`. Append-only.
BROADCAST_RESULTS_FILENAME = "broadcast_results.jsonl"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "List of target Agent names. Each must be a current "
                "roster entry; per-target failures are recorded in "
                "broadcast_results.jsonl. Caller's own name is silently "
                "skipped if present."
            ),
        },
        "message": {
            "type": "string",
            "description": "Free-form message body delivered to every named target.",
        },
        "requires_answer": {
            "type": "boolean",
            "description": (
                "If true, every named target owes a reply. Defaults to false."
            ),
            "default": False,
        },
    },
    "required": ["names", "message"],
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (All-visible)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Send the same message to multiple Agents. Equivalent to N "
            "parallel message_agent calls; per-target failures are "
            "recorded in broadcast_results.jsonl. Caller's own name is "
            "silently skipped."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class BroadcastError(RuntimeError):
    """Raised on unrecoverable broadcast failures (e.g. no identity)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _broadcast_results_path() -> Path:
    return paths.active_run_dir() / BROADCAST_RESULTS_FILENAME


def _append_result_row(row: dict[str, Any]) -> None:
    """Append one JSON line to `broadcast_results.jsonl`.

    Per SPEC §6.6 append-only logs open with `O_APPEND` so the kernel
    serializes writes. Lines are wrapped under 4 KiB; here the per-row
    payload is small (under ~300 bytes), well below `PIPE_BUF`.
    """
    path = _broadcast_results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    # Open with O_APPEND so concurrent writers (in principle there is
    # only one Coordinator broadcasting, but other Agents may also call
    # broadcast in their own roles) are kernel-serialized.
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_APPEND | os.O_CREAT,
        0o644,
    )
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _do_broadcast(args: dict[str, Any]) -> dict[str, Any]:
    """Core broadcast logic."""
    names = args.get("names")
    message = args.get("message")
    requires_answer = bool(args.get("requires_answer", False))

    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise BroadcastError("'names' must be a list of strings")
    if not (isinstance(message, str) and message):
        raise BroadcastError("'message' must be a non-empty string")

    sender = message_agent_tool._resolve_sender_name()

    delivered: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    # Per SPEC §7 `broadcast_message` row: caller's own name silently skipped.
    targets = [n for n in names if n and n != sender]

    for target in targets:
        result_row: dict[str, Any] = {
            "timestamp": _now_iso(),
            "sender": sender,
            "target": target,
        }
        try:
            # Validate target exists; raises MessageAgentError on miss.
            message_agent_tool._validate_target_exists(target)
            envelope = mailbox.build_envelope(
                sender=sender,
                target=target,
                kind=mailbox.KIND_DELIVER_MESSAGE,
                payload={
                    "message_text": message,
                    "requires_answer": requires_answer,
                },
            )
            mailbox.write_envelope(
                envelope,
                target_agent=target,
                kind=mailbox.KIND_DELIVER_MESSAGE,
            )
            result_row["outcome"] = "delivered"
            result_row["correlation_id"] = envelope["correlation_id"]
            delivered.append(
                {
                    "target": target,
                    "correlation_id": envelope["correlation_id"],
                }
            )
        except (message_agent_tool.MessageAgentError, mailbox.MailboxError) as exc:
            result_row["outcome"] = "failed"
            result_row["reason"] = str(exc)
            failed.append({"target": target, "reason": str(exc)})
        # Append even on failure -- the audit row is the lasting record.
        try:
            _append_result_row(result_row)
        except OSError as exc:
            # Log write failure is non-fatal to the broadcast itself,
            # but we surface it in the failed list so the caller knows
            # the audit trail is incomplete.
            failed.append(
                {
                    "target": target,
                    "reason": f"audit log write failed: {exc}",
                }
            )

    skipped_self = sender in names

    return {
        "sender": sender,
        "delivered": delivered,
        "failed": failed,
        "skipped_self": skipped_self,
        "broadcast_results_path": str(_broadcast_results_path()),
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `broadcast_message`."""
    args = arguments or {}
    result = _do_broadcast(args)
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
