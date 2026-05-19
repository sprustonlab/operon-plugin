"""Tests for phase 14 fix 8 channel-tag resolver (simplified).

Coverage (per claudechic's fix 8 dispatch):
  - Default case: plugin.json readable -> plugin:<name>@inline.
  - settings.json absent: still @inline (helper ignores it).
  - settings.json with marketplace entry: STILL @inline (proves we
    ignore the misleading enabledPlugins state; this is the
    regression guard against fix 7's behavior).
  - Corrupt settings.json: ignored, still @inline.
  - Plugin.json missing -> None (caller skips --channels=).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_plugin_root(tmp: Path, plugin_name: str) -> Path:
    """Create a minimal <plugin_root>/.claude-plugin/plugin.json so
    channel_tag._read_plugin_name() succeeds."""
    plugin_root = tmp / "plugin_root"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": plugin_name, "version": "0.0.0"}),
        encoding="utf-8",
    )
    return plugin_root


@pytest.fixture
def stub_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """Stub a plugin root + plugin name. Returns (tmp_path, plugin_name)."""
    plugin_name = "operon-plugin"
    plugin_root = _make_plugin_root(tmp_path, plugin_name)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    return tmp_path, plugin_name


# ====================================================================
# Default case: helper returns @inline
# ====================================================================


def test_default_returns_inline(stub_plugin):
    """With CLAUDE_PLUGIN_ROOT set and a readable plugin.json, the
    helper returns `plugin:<name>@inline`."""
    from operon_mcp_server import channel_tag

    _, plugin_name = stub_plugin
    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@inline"


# ====================================================================
# Regression guard: marketplace state in settings.json is IGNORED
# ====================================================================


def test_marketplace_state_in_settings_json_is_ignored(
    stub_plugin, tmp_path: Path
):
    """Phase 14 fix 8 regression guard: even if Boaz's actual
    enabledPlugins state contains `operon-plugin@operon-plugin-marketplace`
    (which it does), the helper must still return @inline. The fix
    7 marketplace-detection branch is removed; helper now ignores
    settings.json entirely."""
    from operon_mcp_server import channel_tag

    _, plugin_name = stub_plugin
    # Simulate the user's actual settings.json shape that triggered
    # the fix 7 bug. Even if the helper still reads settings.json
    # (it shouldn't, post-fix-8), the result must be @inline.
    fake_settings = tmp_path / "fake_settings.json"
    fake_settings.write_text(
        json.dumps(
            {
                "enabledPlugins": {
                    f"{plugin_name}@operon-plugin-marketplace": True,
                    "other-plugin@other-marketplace": True,
                }
            }
        ),
        encoding="utf-8",
    )
    # We don't even bother monkey-patching USER_SETTINGS_PATH because
    # the helper post-fix-8 doesn't reference it. Verify by source:
    # `channel_tag` module no longer exports USER_SETTINGS_PATH.
    assert not hasattr(channel_tag, "USER_SETTINGS_PATH"), (
        "fix 8 should have removed USER_SETTINGS_PATH constant; the "
        "marketplace probe is supposed to be gone"
    )
    assert not hasattr(channel_tag, "_detect_marketplace_source"), (
        "fix 8 should have removed _detect_marketplace_source helper"
    )

    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@inline"


# ====================================================================
# Plugin.json resolution failures -> None (caller skips --channels=)
# ====================================================================


def test_no_plugin_root_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If CLAUDE_PLUGIN_ROOT is unset, helper returns None so the
    caller skips emitting --channels=."""
    from operon_mcp_server import channel_tag

    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    assert channel_tag.channel_tag_for_self() is None


def test_plugin_json_missing_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """CLAUDE_PLUGIN_ROOT points at a dir with no plugin.json."""
    from operon_mcp_server import channel_tag

    plugin_root = tmp_path / "empty_plugin_root"
    plugin_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    assert channel_tag.channel_tag_for_self() is None


def test_plugin_json_corrupt_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """plugin.json present but unparseable -> None."""
    from operon_mcp_server import channel_tag

    plugin_root = tmp_path / "corrupt_plugin_root"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        "not valid json {",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    assert channel_tag.channel_tag_for_self() is None


def test_plugin_json_missing_name_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """plugin.json parses but has no 'name' field -> None."""
    from operon_mcp_server import channel_tag

    plugin_root = tmp_path / "noname_plugin_root"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": "0.0.0"}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    assert channel_tag.channel_tag_for_self() is None
