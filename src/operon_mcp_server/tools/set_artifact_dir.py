"""`set_artifact_dir` MCP tool (Coordinator-only).

Per SPEC §7 `set_artifact_dir` row + §11 + §17. Persists the
operon-session's `artifact_dir` to `<run-dir>/state.json` (schema per
§17). Also makes `artifact-dir-ready-check` pass for any phase that
declares it.

Identity gate: Coordinator-only per SPEC §7.1. The implementation
defers to `spawn_agent._require_coordinator()` to keep the gate
definition in one place.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

from .. import paths, workflow
from . import spawn_agent as spawn_agent_tool

#: MCP tool name. Coordinator-only per SPEC §7.1.
TOOL_NAME = "set_artifact_dir"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Filesystem path that becomes the operon-session's "
                "artifact_dir. The recommended default per SPEC §7 is "
                "the operon-session directory itself (e.g. "
                "`<project>/.operon/<run_name>/`). May be absolute or "
                "relative to the active run directory."
            ),
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


def tool_descriptor() -> mcp_types.Tool:
    """Return the MCP `Tool` descriptor for `tools/list` (Coordinator-only)."""
    return mcp_types.Tool(
        name=TOOL_NAME,
        description=(
            "Persist the artifact_dir for the active operon-session "
            "to <run-dir>/state.json (SPEC §17). Required before any "
            "phase with artifact-dir-ready-check can advance. "
            "Coordinator-only."
        ),
        inputSchema=INPUT_SCHEMA,
    )


class SetArtifactDirError(RuntimeError):
    """Raised on validation or write failures."""


def _require_coordinator() -> str:
    """Return caller agent_name; reject non-Coordinator with tool error."""
    try:
        record = spawn_agent_tool._require_coordinator()
    except spawn_agent_tool.SpawnAgentError as exc:
        raise SetArtifactDirError(str(exc)) from exc
    name = record.get("agent_name")
    if not isinstance(name, str) or not name:
        raise SetArtifactDirError(
            "Coordinator handle record is missing 'agent_name' field."
        )
    return name


def _do_set(args: dict[str, Any]) -> dict[str, Any]:
    raw_path = args.get("path")
    if not (isinstance(raw_path, str) and raw_path):
        raise SetArtifactDirError("'path' must be a non-empty string")

    _require_coordinator()

    # Resolve relative paths against the active run directory so
    # callers can pass either `<absolute>` or `./relative`.
    try:
        run_dir = paths.active_run_dir()
    except paths.OperonPathError as exc:
        raise SetArtifactDirError(str(exc)) from exc

    artifact_path = Path(raw_path).expanduser()
    if not artifact_path.is_absolute():
        artifact_path = (run_dir / artifact_path).resolve()
    else:
        artifact_path = artifact_path.resolve()

    # Create the dir if it doesn't exist -- artifact dirs are
    # session-local working space and SPEC recommends the run-dir
    # itself, which already exists.
    artifact_path.mkdir(parents=True, exist_ok=True)

    try:
        written = workflow.write_state(
            run_name=run_dir.name, artifact_dir=str(artifact_path)
        )
    except workflow.WorkflowError as exc:
        raise SetArtifactDirError(str(exc)) from exc

    return {
        "artifact_dir": str(artifact_path),
        "state_path": str(written),
        "run_name": run_dir.name,
    }


async def call(arguments: dict[str, Any] | None) -> list[mcp_types.TextContent]:
    """MCP `call_tool` handler for `set_artifact_dir`."""
    args = arguments or {}
    result = _do_set(args)
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]
