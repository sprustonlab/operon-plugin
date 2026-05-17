"""Implementation of the hidden `bind_handle` MCP tool.

Per SPEC.md section 7 (`bind_handle` row) and section 6.5 (step 6),
this tool is invoked exclusively by Claude Code's `SessionStart` hook
via the `type: mcp_tool` handler form (it is HIDDEN from `tools/list`).

`spawn_agent` pre-generates the `session_id` (passed to Claude Code via
the `--session-id` flag) and writes it into `_handles/<handle>.json`
BEFORE launching the subprocess (SPEC 6.5 step 1). Therefore
`bind_handle` does NOT write -- it VALIDATES that the hook-supplied
`session_id` matches the spawn-time-written value:

- env-handle mismatch with the `handle` parameter -> tool error
  (signals that the hook is firing in the wrong subprocess);
- handle record missing -> tool error (signals spawn-time miscapture);
- handle record `session_id` missing or empty -> tool error;
- spawn-time `session_id` matches hook-supplied -> idempotent success
  (resume re-fires `SessionStart` against the same handle);
- spawn-time `session_id` differs -> tool error (respawn collision or
  spawn-time miscapture).
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types

from .. import identity, paths

#: MCP tool name, also used as the qualified hook target
#: `mcp__operon__bind_handle` by Claude Code.
TOOL_NAME = "bind_handle"

#: JSON schema for the tool's `inputSchema`. Both fields required.
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handle": {
            "type": "string",
            "description": (
                "Opaque per-subprocess handle from the OPERON_AGENT_HANDLE "
                "environment variable. Must match the env value of the "
                "calling subprocess."
            ),
        },
        "session_id": {
            "type": "string",
            "description": (
                "Claude Code's canonical session id supplied by the "
                "SessionStart hook's JSON payload."
            ),
        },
    },
    "required": ["handle", "session_id"],
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor.

    The server omits this descriptor from `tools/list` (HIDDEN per
    SPEC.md section 7.1) but still routes calls to `mcp__operon__bind_handle`
    through the registered handler. The descriptor exists for internal
    bookkeeping / hook dispatch.
    """
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Hook-only. Validates the per-subprocess identity binding at "
            "SessionStart. Not advertised to the LLM."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class BindHandleError(RuntimeError):
    """Raised when binding validation fails. Converted to a tool error."""


def _validate(handle: str, session_id: str) -> dict[str, Any]:
    """Core validation logic (separate from MCP plumbing for clarity)."""
    if not isinstance(handle, str) or not handle:
        raise BindHandleError("'handle' must be a non-empty string")
    if not isinstance(session_id, str) or not session_id:
        raise BindHandleError("'session_id' must be a non-empty string")

    env_handle = identity.read_env_handle()
    if env_handle is None:
        raise BindHandleError(
            f"Environment variable '{identity.ENV_HANDLE_VAR}' is not set; "
            "bind_handle was invoked outside an operon-spawned subprocess."
        )
    if env_handle != handle:
        raise BindHandleError(
            "Handle mismatch: env "
            f"'{identity.ENV_HANDLE_VAR}'='{env_handle}' but tool was "
            f"called with handle='{handle}'. SessionStart hook is firing "
            "in the wrong subprocess."
        )

    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        raise BindHandleError(str(exc)) from exc

    if record is None:
        # spawn_agent must have written the record before launching us
        # (SPEC 6.5 step 1). Its absence here is a hard error.
        raise BindHandleError(
            f"No handle record at '{paths.handle_file(handle)}'. "
            "spawn_agent must write the binding before launching the "
            "subprocess (SPEC 6.5 step 1)."
        )

    stored = record.get("session_id")
    if not isinstance(stored, str) or not stored:
        raise BindHandleError(
            f"Handle record for '{handle}' has no spawn-time session_id; "
            "spawn_agent must pre-generate and write session_id (SPEC 6.5 "
            "step 1) before this subprocess starts."
        )
    if stored != session_id:
        raise BindHandleError(
            f"session_id mismatch for handle '{handle}': "
            f"spawn-time='{stored}', hook-supplied='{session_id}'. "
            "Respawn collision or spawn-time miscapture."
        )

    # Match -> idempotent success. No write.
    return {
        "bound": True,
        "handle": handle,
        "session_id": session_id,
        "agent_name": record.get("agent_name"),
        "role": record.get("role"),
        "workflow_id": record.get("workflow_id"),
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `bind_handle`.

    Returns a single `TextContent` carrying a JSON-encoded success
    payload, or raises so the MCP framework surfaces a tool error to
    the caller.
    """
    import json

    args = arguments or {}
    result = _validate(
        handle=args.get("handle", ""),
        session_id=args.get("session_id", ""),
    )
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
