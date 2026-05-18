"""Tests for phase 14 fix 7 channel-tag resolver.

Coverage (per claudechic's revised Fix B spec):
  1. enabledPlugins absent -> @inline.
  2. enabledPlugins present, plugin appears under marketplace `X`
     with truthy value -> plugin:operon-plugin@X.
  3. enabledPlugins present but plugin missing -> @inline.
  4. Corrupt settings.json -> @inline + DEBUG log line.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

# All tests import the channel_tag module under operon_mcp_server.
# They use monkeypatch to redirect USER_SETTINGS_PATH and
# CLAUDE_PLUGIN_ROOT so each case runs in isolation.


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


def _redirect_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: object | None
) -> Path:
    """Redirect USER_SETTINGS_PATH and write `payload` there.

    `payload=None` leaves the file absent. Any other value is
    JSON-encoded if it's serializable; otherwise written as raw text
    (for the "corrupt JSON" case).
    """
    from operon_mcp_server import channel_tag

    settings_dir = tmp_path / "home_claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    if payload is not None:
        if isinstance(payload, (dict, list)):
            settings_path.write_text(json.dumps(payload), encoding="utf-8")
        else:
            # raw text for corrupt-JSON case
            settings_path.write_text(str(payload), encoding="utf-8")
    monkeypatch.setattr(channel_tag, "USER_SETTINGS_PATH", settings_path)
    return settings_path


# ====================================================================
# Case 1: enabledPlugins absent -> @inline
# ====================================================================


def test_enabled_plugins_absent_returns_inline(
    stub_plugin, monkeypatch: pytest.MonkeyPatch
):
    from operon_mcp_server import channel_tag

    tmp_path, plugin_name = stub_plugin
    # settings.json has no enabledPlugins key.
    _redirect_settings(monkeypatch, tmp_path, {"otherKey": "ignored"})
    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@inline"


def test_settings_file_missing_returns_inline(
    stub_plugin, monkeypatch: pytest.MonkeyPatch, caplog
):
    from operon_mcp_server import channel_tag

    tmp_path, plugin_name = stub_plugin
    # No file written at all.
    _redirect_settings(monkeypatch, tmp_path, payload=None)
    caplog.set_level(logging.DEBUG, logger=channel_tag._log.name)
    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@inline"
    # DEBUG log fired about the missing file.
    assert any(
        "missing" in rec.getMessage() and "@inline" in rec.getMessage()
        for rec in caplog.records
    )


# ====================================================================
# Case 2: plugin in enabledPlugins under marketplace X -> @X
# ====================================================================


def test_marketplace_install_returns_marketplace_name(
    stub_plugin, monkeypatch: pytest.MonkeyPatch
):
    from operon_mcp_server import channel_tag

    tmp_path, plugin_name = stub_plugin
    _redirect_settings(
        monkeypatch,
        tmp_path,
        {
            "enabledPlugins": {
                f"{plugin_name}@operon-plugin-marketplace": True,
                "other-plugin@other-marketplace": True,
            }
        },
    )
    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@operon-plugin-marketplace"


def test_marketplace_install_arbitrary_marketplace_name(
    stub_plugin, monkeypatch: pytest.MonkeyPatch
):
    """Helper must not hardcode the marketplace name -- a third-party
    redistribution would use a different one."""
    from operon_mcp_server import channel_tag

    tmp_path, plugin_name = stub_plugin
    _redirect_settings(
        monkeypatch,
        tmp_path,
        {"enabledPlugins": {f"{plugin_name}@test-marketplace": True}},
    )
    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@test-marketplace"


def test_falsy_value_skipped_falls_back_to_inline(
    stub_plugin, monkeypatch: pytest.MonkeyPatch
):
    """A `false` / null / empty value under enabledPlugins is treated
    as not-installed -> @inline."""
    from operon_mcp_server import channel_tag

    tmp_path, plugin_name = stub_plugin
    _redirect_settings(
        monkeypatch,
        tmp_path,
        {"enabledPlugins": {f"{plugin_name}@disabled-marketplace": False}},
    )
    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@inline"


# ====================================================================
# Case 3: enabledPlugins present but plugin missing -> @inline
# ====================================================================


def test_plugin_missing_from_enabledPlugins_returns_inline(
    stub_plugin, monkeypatch: pytest.MonkeyPatch
):
    from operon_mcp_server import channel_tag

    tmp_path, plugin_name = stub_plugin
    _redirect_settings(
        monkeypatch,
        tmp_path,
        {
            "enabledPlugins": {
                "another-plugin@another-marketplace": True,
                "yet-another@yet-another-marketplace": True,
            }
        },
    )
    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@inline"


# ====================================================================
# Case 4: corrupt settings.json -> @inline + DEBUG warning
# ====================================================================


def test_corrupt_settings_json_returns_inline_with_warning(
    stub_plugin, monkeypatch: pytest.MonkeyPatch, caplog
):
    from operon_mcp_server import channel_tag

    tmp_path, plugin_name = stub_plugin
    _redirect_settings(monkeypatch, tmp_path, payload="not valid json {")
    caplog.set_level(logging.DEBUG, logger=channel_tag._log.name)
    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@inline"
    assert any(
        "unreadable" in rec.getMessage() and "@inline" in rec.getMessage()
        for rec in caplog.records
    )


def test_settings_not_a_json_object_returns_inline(
    stub_plugin, monkeypatch: pytest.MonkeyPatch
):
    from operon_mcp_server import channel_tag

    tmp_path, plugin_name = stub_plugin
    # Valid JSON but a list instead of object.
    _redirect_settings(monkeypatch, tmp_path, ["not", "a", "dict"])
    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@inline"


def test_enabledPlugins_not_a_dict_returns_inline(
    stub_plugin, monkeypatch: pytest.MonkeyPatch
):
    from operon_mcp_server import channel_tag

    tmp_path, plugin_name = stub_plugin
    # enabledPlugins is the wrong type (list instead of dict).
    _redirect_settings(
        monkeypatch, tmp_path, {"enabledPlugins": ["wrong", "shape"]}
    )
    tag = channel_tag.channel_tag_for_self()
    assert tag == f"plugin:{plugin_name}@inline"


# ====================================================================
# Plugin-name resolution failures
# ====================================================================


def test_no_plugin_root_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If CLAUDE_PLUGIN_ROOT is unset, helper returns None so the
    caller skips emitting --channels=."""
    from operon_mcp_server import channel_tag

    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    _redirect_settings(monkeypatch, tmp_path, payload=None)
    assert channel_tag.channel_tag_for_self() is None


def test_plugin_json_missing_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from operon_mcp_server import channel_tag

    # Plugin root exists but no plugin.json inside.
    plugin_root = tmp_path / "empty_plugin_root"
    plugin_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    _redirect_settings(monkeypatch, tmp_path, payload=None)
    assert channel_tag.channel_tag_for_self() is None


def test_plugin_json_missing_name_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from operon_mcp_server import channel_tag

    plugin_root = tmp_path / "noname_plugin_root"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": "0.0.0"}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    _redirect_settings(monkeypatch, tmp_path, payload=None)
    assert channel_tag.channel_tag_for_self() is None
