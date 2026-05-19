"""Resolve the `--channels=` tag for this plugin's own MCP server.

Phase 14 fix 8 (revert of fix 7's marketplace-detection branch):
this helper now ALWAYS returns `plugin:<plugin-name>@inline`
regardless of marketplace-install state. The marketplace-detection
branch via `~/.claude/settings.json enabledPlugins` was removed
because empirically the produced `@<marketplace-name>` tag does
NOT match the MCP server's internal identifier in the channel-gate
function -- only `@inline` works for bg workers spawned with
`--plugin-dir`. The marketplace-installed case is handled by CC
loading the plugin from the user's marketplace cache at the same
path; --plugin-dir + @inline still resolves correctly because the
gate keys on the tag we passed, not on the install source.

Returns None if `<CLAUDE_PLUGIN_ROOT>/.claude-plugin/plugin.json`
is missing or malformed -- caller skips emitting `--channels=` in
that case.

Phase 14 fix 7 (original): introduced the helper as a replacement
for the dev-channels flag, with a settings.json probe to pick
between `@<marketplace>` and `@inline`. Fix 8 reverts the probe
after empirical evidence that the marketplace branch broke worker
channel registration on Boaz's actual workflow.

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

#: Source label stamped into the channel tag. Always `inline` per
#: phase 14 fix 8 -- the marketplace-name branch was removed.
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


def channel_tag_for_self() -> str | None:
    """Return the `--channels=` tag string for this plugin's MCP server.

    Always returns `plugin:<plugin-name>@inline` when plugin.json is
    readable. Returns None when plugin.json is missing or malformed
    so the caller skips emitting `--channels=` entirely.

    Phase 14 fix 8 dropped the marketplace-detection branch. The
    enabledPlugins probe is gone; settings.json state is ignored.
    See module docstring for the empirical rationale.
    """
    plugin_name = _read_plugin_name()
    if plugin_name is None:
        return None
    return f"plugin:{plugin_name}@{INLINE_SOURCE}"
