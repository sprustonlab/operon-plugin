"""Resolve the `--channels=` tag for this plugin's own MCP server.

Phase 14 fix 7: replaces Phase 14 fix 2's
`--dangerously-load-development-channels=` flag with the production
`--channels=` flag for `claude --bg` worker spawns.

The production flag survives Claude Code's `/agents`-view attach-
respawn (the parser re-applies it from argv on every supervisor
restart), whereas the dev flag requires an interactive confirmation
dialog that does NOT survive respawn. NudgeResearcher's
investigation: `F$.allowedChannels` is per-process in-memory only,
not persisted to session jsonl, so the production flag in argv is
the only way to repopulate it on respawn.

Channel tag format (per Claude Code 2.1.143 empirical pre-flight):
  plugin:<plugin-name>@<source>

Where `<source>` is:
  - the marketplace name from `~/.claude/settings.json`
    `enabledPlugins` when the user installed via
    `claude plugin install ...` (marketplace-installed).
  - the literal string `inline` when the user dev-loaded via
    `--plugin-dir <path>` (the dev workflow Boaz uses).

The dispatched-and-then-revised initial format
`plugin:<plugin>:<server>` was REJECTED by Claude Code's `--channels=`
parser at session init -- empirical pre-flight confirmed that the
`plugin:<plugin>:<server>` colon-separated form is not a valid
channel tag. The only valid forms are
`plugin:<name>@<marketplace>` (this helper) and `server:<name>`
(for manually-configured non-plugin MCP servers).

Cross-platform per SPEC §2: `pathlib.Path`, explicit
`encoding="utf-8"`. No platform-gated APIs.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)

#: Env var Claude Code sets to the plugin install root.
ENV_PLUGIN_ROOT = "CLAUDE_PLUGIN_ROOT"

#: User-level Claude Code settings file. Read to detect marketplace
#: installs (the `enabledPlugins` map). Missing / corrupt is non-fatal:
#: dev-loaded plugins typically have no entry here.
USER_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

#: Fallback source label when no marketplace install is detected.
#: Matches Claude Code's internal sentinel for `--plugin-dir`-loaded
#: plugins ("from inline" in CC's mismatch error messages).
INLINE_SOURCE = "inline"


def _read_plugin_name() -> str | None:
    """Read our plugin's name from `<CLAUDE_PLUGIN_ROOT>/.claude-plugin/plugin.json`.

    Returns None on missing env var, missing file, parse error, or
    missing/empty `name` field. Caller treats None as "cannot
    construct a tag" and skips emitting `--channels=`.
    """
    plugin_root_str = os.environ.get(ENV_PLUGIN_ROOT, "").strip()
    if not plugin_root_str:
        return None
    plugin_json = Path(plugin_root_str) / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(plugin_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name:
        return None
    return name


def _detect_marketplace_source(plugin_name: str) -> str:
    """Probe `~/.claude/settings.json` `enabledPlugins` for our marketplace.

    Each key has shape `<plugin>@<marketplace>` -> truthy value. If
    our plugin appears under any marketplace, return that marketplace
    name. Otherwise return `INLINE_SOURCE`.

    Defensive on failure: missing file, parse error, wrong types, or
    no matching entry all return `INLINE_SOURCE` with a DEBUG log line.
    Dev-load is the common case, so any read failure defaults to
    `@inline` without raising.
    """
    try:
        data = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _log.debug(
            "channel_tag: %s missing; defaulting to @inline source",
            USER_SETTINGS_PATH,
        )
        return INLINE_SOURCE
    except (OSError, json.JSONDecodeError) as exc:
        _log.debug(
            "channel_tag: settings.json unreadable (%s); defaulting to "
            "@inline source",
            exc,
        )
        return INLINE_SOURCE
    if not isinstance(data, dict):
        _log.debug("channel_tag: settings.json not a JSON object; @inline")
        return INLINE_SOURCE
    enabled = data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return INLINE_SOURCE

    prefix = f"{plugin_name}@"
    for key, value in enabled.items():
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        if not value:
            continue
        marketplace = key[len(prefix):]
        if not marketplace:
            continue
        return marketplace
    return INLINE_SOURCE


def channel_tag_for_self() -> str | None:
    """Return the `--channels=` tag string for this plugin's MCP server.

    Format: `plugin:<plugin-name>@<source>` where `<source>` is the
    detected marketplace name (for marketplace-installed plugins) or
    the literal `inline` (for dev-loaded plugins). Returns None when
    the plugin's own `plugin.json` is missing / unreadable -- caller
    skips emitting `--channels=` in that case.

    Empirically (Boaz's machine, CC 2.1.143 with binary patches in
    place), this tag produces `"Channel notifications registered"` in
    the worker's MCP log instead of `"Channel notifications
    skipped"`. The `@inline` form works for dev-loaded plugins; the
    marketplace form would work for marketplace-installed plugins
    (untested directly but the format is what CC's own error
    messages cite as valid).
    """
    plugin_name = _read_plugin_name()
    if plugin_name is None:
        return None
    source = _detect_marketplace_source(plugin_name)
    return f"plugin:{plugin_name}@{source}"
