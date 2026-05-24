"""Pytest configuration for operon-plugin E2E scenarios.

The test bed is real subscription-path Claude Code with the JSONL
transcript as observer. Two drivers are available:

- ``pty`` (default) -- `tests/_harness/pty_driver.PtyClaudeDriver`.
  Gives CC a controlling pty; uses pyte for ANSI rendering. Required
  for sub-acts 5+ where the team-panel widget must be readable.
- ``tmux`` (legacy) -- `tests/_harness/tmux_driver.TmuxClaudeDriver`.
  Works for sub-acts 1-4; documented limitation on 5+ (teammate
  subprocess overlays the shared pane; team-panel widget invisible).

Select with the env var ``OPERON_TEST_DRIVER=tmux|pty`` or the
pytest CLI flag ``--driver=tmux|pty``. The scenario imports the
selected class via the ``claude_driver_cls`` fixture.
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


def pytest_addoption(parser):
    parser.addoption(
        "--driver",
        action="store",
        default=None,
        choices=("tmux", "pty"),
        help=(
            "Claude Code driver to use for E2E scenarios. Defaults "
            "to the OPERON_TEST_DRIVER env var, then 'pty'."
        ),
    )


@pytest.fixture(scope="session")
def claude_driver_cls(pytestconfig):
    """Return the selected Claude driver CLASS (tmux or pty).

    Selection order: --driver CLI flag, then OPERON_TEST_DRIVER env
    var, then default 'pty'. The class is returned (not an instance)
    so the scenario constructs each driver as needed.
    """
    choice = (
        pytestconfig.getoption("--driver")
        or os.environ.get("OPERON_TEST_DRIVER")
        or "pty"
    ).lower()
    if choice == "tmux":
        from _harness.tmux_driver import TmuxClaudeDriver
        return TmuxClaudeDriver
    if choice == "pty":
        from _harness.pty_driver import PtyClaudeDriver, _PTY_DEPS_AVAILABLE
        if not _PTY_DEPS_AVAILABLE:
            pytest.fail(
                "OPERON_TEST_DRIVER=pty selected but pexpect/pyte are "
                "not installed. Run `uv sync --group dev` or pick the "
                "tmux driver."
            )
        return PtyClaudeDriver
    pytest.fail(f"unknown driver choice: {choice!r}")


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
