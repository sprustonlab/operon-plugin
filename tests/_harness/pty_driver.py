"""Pty-based Claude Code driver.

The tmux-based driver (`tmux_driver.py`) presents an empirically
insufficient test bed for sub-acts 5+: when the lead spawns teammates
via Agent(run_in_background=true), the teammate subprocesses inherit
the lead's pty and their CC TUI overlays the lead's pane. The team-
panel widget (the `╒═ <name>` row decoration) never renders, so
focus_main_thread / focus_teammate_thread can't navigate (M2i
investigation, /tmp/operon-tui-investigation/).

This driver gives CC a CONTROLLING pty via `pexpect`, and emulates the
xterm alt-screen + ANSI/CSI rendering via `pyte`. The lead's TUI
remains the foreground render; teammate subprocesses go to the
background (no welcome overlay observed in Boaz's interactive shell
empirically). The harness reads pyte's screen buffer to detect the
`╒═` marker and drive Shift+Up/Down navigation.

API surface mirrors `tmux_driver.TmuxClaudeDriver` so the scenario
code is driver-agnostic (selectable via conftest).

Cross-platform note: pexpect uses POSIX pty primitives; pyte is pure
Python. This driver does not run on Windows (CC's TUI driver
canonically uses POSIX pty conventions anyway). The harness on a
Windows host should set `OPERON_TEST_DRIVER=tmux` or skip.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path

# Defer external imports so this module can be imported in environments
# without pexpect/pyte installed (the conftest's driver-selection logic
# falls back to tmux there).
try:
    import pexpect  # type: ignore
    import pyte  # type: ignore
    _PTY_DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover -- dev-extras only.
    pexpect = None  # type: ignore
    pyte = None  # type: ignore
    _PTY_DEPS_AVAILABLE = False


class PtyDriverError(RuntimeError):
    """Raised on driver-side failures (spawn errors, missing deps, etc.)."""


#: Map of named-key tokens to the raw escape sequences sent to CC's
#: input stream. Matches the syntax used by tmux's `send-keys` named
#: keys so the scenario code can call ``send_special("S-Up")``
#: interchangeably across drivers.
_KEY_MAP: dict[str, str] = {
    "Enter": "\r",
    "Tab": "\t",
    "Space": " ",
    "BSpace": "\x7f",  # DEL (which most TUIs treat as Backspace).
    "Backspace": "\x08",
    "Escape": "\x1b",
    "Esc": "\x1b",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "S-Up": "\x1b[1;2A",
    "S-Down": "\x1b[1;2B",
    "S-Right": "\x1b[1;2C",
    "S-Left": "\x1b[1;2D",
    "C-c": "\x03",
    "C-g": "\x07",
    "C-u": "\x15",
    "C-l": "\x0c",
}


def pretrust_workspace(cwd: Path) -> None:
    """Pre-accept Claude Code's workspace-trust dialog for ``cwd``.

    Identical logic to ``tmux_driver.pretrust_workspace``: edits
    ``~/.claude.json`` to add an entry under ``projects`` with
    ``hasTrustDialogAccepted: True``. Without this, CC's first
    interactive launch in an unknown directory blocks on a TUI
    workspace-trust prompt the harness can't easily drive past.
    """
    cfg_path = Path.home() / ".claude.json"
    if not cfg_path.exists():
        return
    raw = cfg_path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        return
    projects = cfg.setdefault("projects", {})
    key = str(cwd.resolve())
    proj = projects.setdefault(
        key,
        {
            "allowedTools": [],
            "mcpContextUris": [],
            "mcpServers": {},
            "enabledMcpjsonServers": [],
            "disabledMcpjsonServers": [],
            "hasTrustDialogAccepted": True,
        },
    )
    proj["hasTrustDialogAccepted"] = True
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".harness-tmp")
    tmp.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, cfg_path)


@dataclass
class PtyClaudeDriver:
    """Drive an interactive ``claude`` process attached to a pty.

    Mirrors the tmux driver's constructor and method surface so the
    scenario code can be driver-agnostic.
    """

    session_name: str  # not used by pty, kept for API parity
    cwd: Path
    plugin_dir: Path
    session_uuid: str = field(default_factory=lambda: str(_uuid.uuid4()))
    extra_env: dict[str, str] = field(default_factory=dict)
    width: int = 200
    height: int = 60
    dangerously_skip_permissions: bool = True
    model: str = "sonnet"
    started: bool = False

    # Pyte / pexpect state (not constructor args; populated by start()).
    _child: object = None
    _stream: object = None
    _screen: object = None
    _raw_buffer: bytearray = field(default_factory=bytearray)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reader_thread: threading.Thread | None = None
    _reader_stop: threading.Event = field(default_factory=threading.Event)

    # Constants mirroring tmux_driver.
    FOCUS_MARKER: str = "╒═"

    # ----- lifecycle -----

    def start(self, claude_extra_args: list[str] | None = None) -> None:
        if not _PTY_DEPS_AVAILABLE:
            raise PtyDriverError(
                "pexpect + pyte not installed. Add them to the [dev] "
                "dependency group or set OPERON_TEST_DRIVER=tmux."
            )
        if self.started:
            raise PtyDriverError("driver already started")
        pretrust_workspace(self.cwd)

        env = os.environ.copy()
        env.update(self.extra_env)
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("LANG", "en_US.UTF-8")

        claude_argv = [
            "--plugin-dir", str(self.plugin_dir),
            "--session-id", self.session_uuid,
            "--model", self.model,
        ]
        if self.dangerously_skip_permissions:
            claude_argv.append("--dangerously-skip-permissions")
        if claude_extra_args:
            claude_argv.extend(claude_extra_args)

        # Spawn claude as a child of a controlling pty. encoding=None
        # gives us raw bytes; pyte parses them. dimensions sets the
        # initial winsize so CC's TUI renders at the requested
        # geometry (empirical: a wide enough terminal is required for
        # the team-panel widget to render; below ~80 cols it collapses).
        try:
            self._child = pexpect.spawn(
                "claude",
                claude_argv,
                cwd=str(self.cwd),
                env=env,
                dimensions=(self.height, self.width),
                encoding=None,
                codec_errors="ignore",
                timeout=None,
            )
        except pexpect.ExceptionPexpect as e:
            raise PtyDriverError(f"pexpect.spawn failed: {e}") from e

        # Pyte screen + byte stream.
        self._screen = pyte.HistoryScreen(
            self.width, self.height, history=2000, ratio=1.0
        )
        self._stream = pyte.ByteStream(self._screen)

        # Background reader: drain bytes from pexpect into pyte +
        # a flat ANSI byte buffer.
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="pty-reader", daemon=True
        )
        self._reader_thread.start()
        self.started = True

        # Drive past any one-time TUI prompts (workspace trust was
        # pre-accepted on disk; --dangerously-skip-permissions still
        # surfaces a "Yes, I accept" confirmation on first launch).
        self._dismiss_startup_prompts()

    def _reader_loop(self) -> None:
        """Drain pexpect output into pyte + buffer; daemon thread."""
        while not self._reader_stop.is_set():
            if self._child is None:
                return
            try:
                chunk = self._child.read_nonblocking(size=4096, timeout=0.2)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                return
            except OSError:
                return
            if chunk is None:
                continue
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="ignore")
            with self._lock:
                self._raw_buffer.extend(chunk)
                try:
                    self._stream.feed(chunk)
                except Exception:  # pragma: no cover -- defensive.
                    # pyte raised on malformed bytes; skip the bad
                    # chunk rather than tearing down the reader.
                    pass

    def _dismiss_startup_prompts(self, deadline_s: float = 20.0) -> None:
        """Drive past Claude Code's one-time bypass-acceptance dialog.

        Empirically (CC v2.1.150): the dialog text contains
        ``Bypass Permissions`` + ``Yes, I accept``. Send Down + Enter
        to select option 2. Idempotent: if the dialog never appears
        (already dismissed in this CC global state), returns silently.
        """
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            buf = self.screen_text()
            if "Bypass Permissions" in buf and "Yes, I accept" in buf:
                self.send_special("Down")
                time.sleep(0.1)
                self.send_special("Enter")
                time.sleep(0.5)
                return
            time.sleep(0.25)

    def kill(self) -> None:
        if not self.started:
            return
        self._reader_stop.set()
        if self._child is not None:
            try:
                self._child.close(force=True)
            except (OSError, pexpect.ExceptionPexpect):
                pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
        self.started = False

    def close(self) -> None:
        self.kill()

    def session_pid(self) -> int | None:
        """Return the claude process pid, or None if not running."""
        if self._child is None:
            return None
        try:
            if self._child.isalive():
                return int(self._child.pid)
        except (AttributeError, OSError):
            return None
        return None

    def session_exists(self) -> bool:
        return self.session_pid() is not None

    # ----- I/O -----

    def send(self, text: str, enter: bool = True) -> None:
        if self._child is None:
            raise PtyDriverError("send() before start()")
        try:
            self._child.send(text.encode("utf-8", errors="ignore"))
            if enter:
                self._child.send(b"\r")
        except (OSError, pexpect.ExceptionPexpect) as e:
            raise PtyDriverError(f"pexpect.send failed: {e}") from e

    def send_special(self, key: str) -> None:
        """Send a named key sequence (``Enter``, ``S-Up``, ``C-c``, ...)."""
        seq = _KEY_MAP.get(key)
        if seq is None:
            raise PtyDriverError(f"unknown key name: {key!r}")
        if self._child is None:
            raise PtyDriverError("send_special() before start()")
        try:
            self._child.send(seq.encode("utf-8", errors="ignore"))
        except (OSError, pexpect.ExceptionPexpect) as e:
            raise PtyDriverError(f"pexpect.send (special) failed: {e}") from e

    # ----- screen state -----

    def screen_text(self) -> str:
        """Return pyte's currently-rendered screen as a single string."""
        with self._lock:
            if self._screen is None:
                return ""
            try:
                lines = list(self._screen.display)
            except Exception:
                lines = []
            return "\n".join(lines)

    def screen_ansi(self) -> bytes:
        """Return the raw bytes captured since start (for debug)."""
        with self._lock:
            return bytes(self._raw_buffer)

    # Compatibility shim for scenarios written against tmux_driver.
    def capture_pane(self) -> str:
        return self.screen_text()

    # ----- focus navigation (mirrors tmux_driver helpers) -----

    def current_focus(self) -> str | None:
        """Return the row name currently in focus (carrying ``╒═``)."""
        buf = self.screen_text()
        for line in buf.splitlines():
            idx = line.find(self.FOCUS_MARKER)
            if idx < 0:
                continue
            tail = line[idx + len(self.FOCUS_MARKER):].strip()
            if not tail:
                continue
            return tail.split()[0].strip(".·")
        return None

    def wait_for_team_panel(
        self, timeout_s: float = 30.0, poll_s: float = 0.5
    ) -> bool:
        """Block until the lead's team-panel widget appears."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.FOCUS_MARKER in self.screen_text():
                return True
            time.sleep(poll_s)
        return False

    def focus_main_thread(
        self, settle_s: float = 0.5, max_hops: int = 6
    ) -> bool:
        """Move focus to the lead's team-lead row."""
        if not self.wait_for_team_panel(timeout_s=30.0):
            return False
        for _ in range(max_hops + 1):
            if self.current_focus() == "team-lead":
                time.sleep(settle_s)
                return True
            self.send_special("S-Up")
            time.sleep(0.25)
        time.sleep(settle_s)
        return self.current_focus() == "team-lead"

    def focus_teammate_thread(
        self, teammate_name: str, settle_s: float = 0.5, max_hops: int = 8
    ) -> bool:
        """Move focus into ``teammate_name``'s channel."""
        if not self.wait_for_team_panel(timeout_s=30.0):
            return False
        if self.current_focus() == teammate_name:
            time.sleep(settle_s)
            return True
        for _ in range(max_hops):
            self.send_special("S-Down")
            time.sleep(0.25)
            if self.current_focus() == teammate_name:
                time.sleep(settle_s)
                return True
        for _ in range(max_hops):
            self.send_special("S-Up")
            time.sleep(0.25)
            if self.current_focus() == teammate_name:
                time.sleep(settle_s)
                return True
        time.sleep(settle_s)
        return self.current_focus() == teammate_name

    def accept_elicit_form(
        self,
        wait_for_substring: str,
        timeout_s: float = 60.0,
        poll_s: float = 0.5,
    ) -> bool:
        """Wait for an MCP elicit-form dialog and accept it.

        CC's elicit-form for a confirm-bool field renders as:
            ❯ * <field name>: ☐
              Accept    Decline
        Sequence: Space (toggle checkbox), Down (move to Accept),
        Enter. Returns True iff the form was observed and driven.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            buf = self.screen_text()
            if (
                wait_for_substring in buf
                and "Accept" in buf
                and "Decline" in buf
            ):
                self.send_special("Space")
                time.sleep(0.2)
                self.send_special("Down")
                time.sleep(0.2)
                self.send_special("Enter")
                time.sleep(0.5)
                return True
            time.sleep(poll_s)
        return False
