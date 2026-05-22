"""Pytest configuration for operon-plugin E2E scenarios.

Per TEST_SPECIFICATION.md, the test bed is real subscription-path
Claude Code driven via tmux with the JSONL transcript as observer.
No mocks beyond filesystem-state fixtures.
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

# Ensure the _harness package is importable from tests/.
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(scope="session")
def operon_plugin_dir() -> Path:
    """Absolute path to the bundled operon-plugin directory."""
    root = Path(__file__).resolve().parents[1]
    plugin = root / "plugins" / "operon-plugin"
    assert plugin.is_dir(), f"operon-plugin dir not found at {plugin}"
    return plugin


@pytest.fixture()
def tmp_cwd() -> Path:
    """A throwaway working directory for one scenario run.

    Returns a path under ``/tmp`` rather than pytest's ``tmp_path``.
    Two reasons:
      1. Claude Code's transcript-dir creation appears to silently
         skip cwds whose mangled name contains certain patterns
         (e.g. embedded dots like ``.tmp_vscode``); the pytest default
         on this host lives under ``$HOME/.tmp_vscode/...``.
      2. The mangled-dir name embeds the full cwd; long pytest paths
         produce unwieldy names in ``~/.claude/projects/``.

    The directory is created fresh per scenario and cleaned up by the
    enclosing test only on success (failure leaves it for forensics).
    """
    import shutil as _shutil
    import time as _time
    base = Path("/tmp") / "operon-tests"
    base.mkdir(parents=True, exist_ok=True)
    cwd = base / f"scenario-{int(_time.time())}-{os.getpid()}"
    if cwd.exists():
        _shutil.rmtree(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    yield cwd


@pytest.fixture()
def scenario_session_uuid() -> str:
    """A fresh session-id UUID for one scenario run."""
    return str(uuid.uuid4())
