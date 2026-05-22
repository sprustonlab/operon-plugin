"""Tmux-based Claude Code driver.

Per TEST_SPECIFICATION.md "Test bed" section: tmux is the driver; JSONL
transcript is the observer. This module starts a tmux session running
``claude --plugin-dir <plugin> --session-id <uuid> ...``, sends
keystrokes via tmux ``send-keys``, captures the pane buffer for
diagnostics, and tears the session down.

Idle detection lives in :mod:`transcript_observer`, not here. This
module's contract is purely driver-side: launching, typing, killing.

Cross-platform note: tmux is POSIX-only (Linux/macOS). Per the project's
testing standard, real Claude Code is the test bed; tmux-driven scenarios
do not run on Windows. The conftest skip rule disallows :mod:`pytest`
skips, so the test simply errors out on platforms without tmux. Run the
test on a Linux/macOS host.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path


class TmuxDriverError(RuntimeError):
    pass


def pretrust_workspace(cwd: Path) -> None:
    """Pre-accept Claude Code's workspace-trust dialog for ``cwd``.

    Without this, the first interactive launch in an unknown directory
    blocks on the 'Is this a project you created or one you trust?'
    prompt. The harness drives via the JSONL transcript, not the TUI,
    so blocking on a TUI prompt would deadlock the scenario.

    We edit ``~/.claude.json`` to add an entry under the ``projects``
    dict with ``hasTrustDialogAccepted: True``. The file is touched
    atomically (tmp + os.replace) so partial-write recovery works on
    a kill.
    """
    cfg_path = Path.home() / ".claude.json"
    if not cfg_path.exists():
        return  # nothing to patch; new CC installs may handle differently
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


def _tmux_bin() -> str:
    path = shutil.which("tmux")
    if not path:
        raise TmuxDriverError(
            "tmux is not on PATH. The harness requires tmux as the driver. "
            "Install tmux on Linux (apt/yum) or macOS (brew install tmux)."
        )
    return path


def _run(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


@dataclass
class TmuxClaudeDriver:
    """Drive an interactive ``claude`` process inside a tmux session.

    The session name uniquely identifies this driver instance. The pane
    is sized large to reduce wrap-induced send-keys quirks.
    """

    session_name: str
    cwd: Path
    plugin_dir: Path
    session_uuid: str = field(default_factory=lambda: str(_uuid.uuid4()))
    extra_env: dict[str, str] = field(default_factory=dict)
    width: int = 220
    height: int = 64
    # Use ``--dangerously-skip-permissions`` instead of
    # ``--permission-mode bypassPermissions`` because the latter shows
    # a one-time TUI acceptance prompt on first launch in a session.
    # The "dangerously" flag is the documented non-interactive
    # equivalent ("Recommended only for sandboxes with no internet
    # access"); the test bed is exactly such a sandbox.
    dangerously_skip_permissions: bool = True
    #: Model pinned per Q17 coordinator answer -- Sonnet for test
    #: runs (5x cheaper than Opus; full-run cost ~$2-4 range under the
    #: 250K TOKEN_CAP). ``--model`` accepts aliases ("sonnet",
    #: "opus") or full names ("claude-sonnet-4-6").
    model: str = "sonnet"
    started: bool = False

    def _tmux(self, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        return _run([_tmux_bin(), *args], timeout=timeout)

    def session_exists(self) -> bool:
        r = self._tmux("has-session", "-t", self.session_name)
        return r.returncode == 0

    def session_pid(self) -> int | None:
        r = self._tmux("list-panes", "-t", self.session_name,
                       "-F", "#{pane_pid}")
        if r.returncode != 0:
            return None
        first = (r.stdout or "").strip().splitlines()
        if not first:
            return None
        try:
            return int(first[0])
        except ValueError:
            return None

    def start(self, claude_extra_args: list[str] | None = None) -> None:
        """Launch tmux session running ``claude --plugin-dir ... --session-id ...``.

        Caller can pass extra args (e.g. ``--permission-mode``,
        ``--add-dir``) via ``claude_extra_args``.
        """
        if self.session_exists():
            raise TmuxDriverError(
                f"tmux session {self.session_name!r} already exists"
            )
        # Pre-accept the workspace-trust dialog so the first launch in
        # an unknown directory does not block on a TUI prompt.
        pretrust_workspace(self.cwd)
        env = os.environ.copy()
        env.update(self.extra_env)
        # Compose the claude command line. ``--session-id`` pins the
        # transcript path so the observer can find it deterministically.
        claude_cmd = [
            "claude",
            "--plugin-dir", str(self.plugin_dir),
            "--session-id", self.session_uuid,
            "--model", self.model,
        ]
        if self.dangerously_skip_permissions:
            claude_cmd.append("--dangerously-skip-permissions")
        if claude_extra_args:
            claude_cmd.extend(claude_extra_args)
        # tmux command shell: cd into cwd, exec claude.
        shell_line = (
            f"cd {self._shell_quote(str(self.cwd))} && "
            f"exec {' '.join(self._shell_quote(a) for a in claude_cmd)}"
        )
        r = self._tmux(
            "new-session", "-d",
            "-s", self.session_name,
            "-x", str(self.width),
            "-y", str(self.height),
            "bash", "-lc", shell_line,
        )
        if r.returncode != 0:
            raise TmuxDriverError(
                f"tmux new-session failed: rc={r.returncode} "
                f"stdout={r.stdout!r} stderr={r.stderr!r}"
            )
        # Propagate the experimental flag for Agent Teams etc.
        # tmux runs the command in its own shell; env at new-session time is
        # inherited from our parent shell. Inject explicit setenv just in case.
        for k, v in self.extra_env.items():
            self._tmux("setenv", "-t", self.session_name, k, v)
        self.started = True
        # Drive any one-time TUI acceptance prompts (workspace-trust
        # was pre-accepted on disk above, but
        # ``--dangerously-skip-permissions`` shows a separate "Yes, I
        # accept" dialog on first launch in a session). Poll the pane
        # buffer for the dialog text and drive past it.
        self._dismiss_startup_prompts()

    def _dismiss_startup_prompts(self, deadline_s: float = 15.0) -> None:
        """Drive past Claude Code's one-time TUI acceptance prompts.

        Specifically: ``Bypass Permissions mode`` (Yes, I accept). The
        pane buffer is polled. If the dialog text is detected, the
        harness sends Down + Enter to select option 2. If no dialog
        appears within ``deadline_s`` seconds, the function returns
        without action (the prompt may have been pre-dismissed by a
        prior session or skipped by CC).
        """
        deadline = time.time() + deadline_s
        dismissed = False
        while time.time() < deadline:
            buf = self.capture_pane()
            if "Bypass Permissions" in buf and "Yes, I accept" in buf:
                # Move highlight from "No, exit" (default) to "Yes, I accept".
                self.send_special("Down")
                time.sleep(0.1)
                self.send_special("Enter")
                dismissed = True
                # After dismissal, give CC a moment to re-render.
                time.sleep(0.5)
                break
            time.sleep(0.25)
        # Even if we didn't see the prompt, that's fine -- the JSONL
        # appearance check downstream will detect a hung startup.

    @staticmethod
    def _shell_quote(s: str) -> str:
        if not s:
            return "''"
        if all(c.isalnum() or c in "-_/.:=" for c in s):
            return s
        return "'" + s.replace("'", "'\\''") + "'"

    def send(self, text: str, enter: bool = True) -> None:
        """Type ``text`` into the pane. If ``enter``, append Enter."""
        if not self.session_exists():
            raise TmuxDriverError(
                f"tmux session {self.session_name!r} not running"
            )
        r = self._tmux("send-keys", "-t", self.session_name, "-l", text)
        if r.returncode != 0:
            raise TmuxDriverError(f"send-keys failed: {r.stderr!r}")
        if enter:
            r2 = self._tmux("send-keys", "-t", self.session_name, "Enter")
            if r2.returncode != 0:
                raise TmuxDriverError(f"send-keys Enter failed: {r2.stderr!r}")

    def send_special(self, key: str) -> None:
        """Send a special key by name (e.g. ``Enter``, ``Escape``, ``C-c``)."""
        r = self._tmux("send-keys", "-t", self.session_name, key)
        if r.returncode != 0:
            raise TmuxDriverError(f"send-keys {key!r} failed: {r.stderr!r}")

    def capture_pane(self) -> str:
        """Return the current pane buffer as a string (for diagnostics)."""
        r = self._tmux("capture-pane", "-t", self.session_name, "-p", "-J", "-S", "-2000")
        return r.stdout or ""

    def accept_elicit_form(
        self,
        wait_for_substring: str,
        timeout_s: float = 60.0,
        poll_s: float = 0.5,
    ) -> bool:
        """Wait for an MCP elicit-form dialog in the pane and accept it.

        The dialog observed empirically in CC v2.1.148:

            MCP server "<name>" requests your input

            <prompt text>

            ❯ * <field name>: ☐
              Accept    Decline

            Esc to cancel · ↑/↓ to navigate · Backspace to unset
              · Space to toggle

        For a single-checkbox confirm form, the sequence to accept
        is: Space (toggle the checkbox to true), Down (move to the
        Accept button), Enter. Returns True if the form text was
        observed and driven through; False on timeout.
        """
        deadline = time.time() + timeout_s
        seen = False
        while time.time() < deadline:
            buf = self.capture_pane()
            if wait_for_substring in buf and "Accept" in buf and "Decline" in buf:
                seen = True
                # Space toggles the checkbox.
                self.send_special("Space")
                time.sleep(0.2)
                # Down moves to Accept button.
                self.send_special("Down")
                time.sleep(0.2)
                # Enter confirms.
                self.send_special("Enter")
                # Give the form a moment to dismiss.
                time.sleep(0.5)
                return True
            time.sleep(poll_s)
        return seen

    def kill(self) -> None:
        """Kill the tmux session, terminating the claude process."""
        if not self.session_exists():
            return
        # Send graceful interrupt first; some claude versions catch it
        # and write trailing transcript bytes before dying.
        try:
            self.send_special("C-c")
            time.sleep(0.3)
        except TmuxDriverError:
            pass
        # Then hard kill.
        self._tmux("kill-session", "-t", self.session_name)
        # Belt-and-braces: if the pane PID is still around, SIGKILL it.
        pid = self.session_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.started = False
