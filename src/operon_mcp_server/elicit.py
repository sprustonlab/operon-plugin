"""Shared elicitation-from-tool-handler helpers.

`advance_phase` (Phase 5), `activate_workflow` (Phase 6.5), and
`restore_operon_session` (Phase 6.5) all need to issue
`elicitation/create` from inside a tool handler. The pattern is the
same in every case: pull the active `ServerSession` out of the
`request_ctx` contextvar (set by the MCP SDK on every tool dispatch),
call `session.elicit_form(message, requestedSchema)`, and translate
the typed `ElicitResult` to a simple bool / dict.

This module owns that pattern so the per-tool implementations stay
focused on their domain logic. Per SPEC §16 the elicitation transport
is an engine concern; leaf modules (`checks/`, `rules.py`) never
import the MCP SDK.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.lowlevel.server import request_ctx

_log = logging.getLogger(__name__)

#: Schema for a yes/no confirmation form (SPEC §11 manual-confirm
#: shape; reused here for destructive-action gates).
CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirm": {
            "type": "boolean",
            "title": "Continue?",
        }
    },
    "required": ["confirm"],
}


async def confirm(message: str) -> bool:
    """Issue an elicit_form with the yes/no `CONFIRM_SCHEMA`.

    Returns True iff the user accepted the form AND set
    `confirm: true`. Returns False on decline, cancel, or any error
    inside the SDK boundary -- caller treats False as "do not
    proceed".
    """
    try:
        ctx = request_ctx.get()
    except LookupError:
        # Called outside a tool dispatch -- no session to elicit
        # against. Treat as decline.
        _log.warning("elicit.confirm: no request context; returning False")
        return False
    try:
        result = await ctx.session.elicit_form(
            message=message, requestedSchema=CONFIRM_SCHEMA
        )
    except Exception as exc:
        _log.warning("elicit.confirm: elicit_form raised: %s", exc)
        return False
    if getattr(result, "action", None) != "accept":
        return False
    content = getattr(result, "content", None) or {}
    return bool(content.get("confirm"))


async def select_one(
    message: str,
    choices: list[str],
    *,
    title: str = "Choose one",
) -> str | None:
    """Issue an elicit_form with a single-select enum field.

    Returns the chosen string on accept, or None on decline / cancel /
    error. Used by `/restore` to pick an operon-session from the
    discovered list.
    """
    if not choices:
        return None
    schema = {
        "type": "object",
        "properties": {
            "selection": {
                "type": "string",
                "title": title,
                "enum": choices,
            }
        },
        "required": ["selection"],
    }
    try:
        ctx = request_ctx.get()
    except LookupError:
        _log.warning("elicit.select_one: no request context; returning None")
        return None
    try:
        result = await ctx.session.elicit_form(
            message=message, requestedSchema=schema
        )
    except Exception as exc:
        _log.warning("elicit.select_one: elicit_form raised: %s", exc)
        return None
    if getattr(result, "action", None) != "accept":
        return None
    content = getattr(result, "content", None) or {}
    val = content.get("selection")
    if isinstance(val, str) and val in choices:
        return val
    return None
