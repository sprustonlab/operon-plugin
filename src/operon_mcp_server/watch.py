"""Background filesystem-watch loop for the MCP subprocess.

Per SPEC.md section 6.6, each MCP subprocess on startup launches a
background filesystem-watch thread that observes ITS OWN mailbox
directories. On a new envelope, the watcher:

- claims the file atomically via `os.replace` to `processed/` (SPEC §6.6
  reader contract);
- routes the envelope to its handler:
    * `kind=deliver_message` -> push a `notifications/claude/channel`
      notification into THIS subprocess's session (the only direction
      `claude/channel` supports per RESEARCH §J.3);
    * `kind=interrupt`       -> same channel push as deny-context; the
      `PreToolUse` hook (Phase 6+) reads the control directory at
      next tool dispatch;
    * `kind=close`           -> invokes `claude stop <session_id>` on
      this subprocess's own session for graceful shutdown.

Self-identification: the watch loop reads `OPERON_AGENT_HANDLE` from
the env on startup, resolves the agent name via `_handles/<handle>.json`,
and only starts watching once identity is bound. If identity cannot be
resolved (no env handle, no handle file, malformed record), the watch
loop does NOT start -- the subprocess can still serve `whoami` for
diagnostics, and the watch loop will be retried on the next
`tools/list` request.

Cross-platform: `watchdog` provides `inotify` (Linux), `FSEvents`
(macOS), `ReadDirectoryChangesW` (Windows). Cross-platform per SPEC §2.

Channel push transport: the MCP Python SDK's `send_notification`
accepts only typed `ServerNotification` variants, none of which is
`notifications/claude/channel`. We therefore construct a raw
`JSONRPCNotification` and write it directly to the same write_stream
that `ServerSession` uses. anyio's `MemoryObjectSendStream` is MPMC,
so concurrent sends from the SDK's protocol loop and our watch loop
are safe.

Thread bridge: `watchdog.Observer` runs callbacks on its own internal
thread. We use a stdlib `queue.Queue` (thread-safe) as the buffer
between watchdog and the asyncio event loop. The async side reads via
`anyio.to_thread.run_sync(q.get, timeout)` with a short timeout so
cancellation is honored without a dedicated portal.
"""

from __future__ import annotations

import json
import logging
import queue as stdlib_queue
import subprocess
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import mcp.types as mcp_types
from anyio.streams.memory import MemoryObjectSendStream
from mcp.shared.message import SessionMessage
from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import identity, mailbox, paths

_log = logging.getLogger(__name__)

#: JSON-RPC method name for the channel push (Claude Code extension).
#: See `https://code.claude.com/docs/en/channels-reference`:
#: "Your server emits `notifications/claude/channel` with two params..."
CHANNEL_NOTIFICATION_METHOD = "notifications/claude/channel"

#: Backlog cap on the FS-event queue. If writers swamp the inbox faster
#: than the async side processes, `Queue.put_nowait` drops new events
#: with a loud warning; the next startup sweep recovers them.
_QUEUE_BUFSIZE = 256

#: Poll timeout for queue.get; the async side wakes up this often even
#: when idle so anyio cancellation can be honored without a shutdown
#: sentinel.
_POLL_TIMEOUT_S = 0.5


# -- watchdog handler ---------------------------------------------------


class _MailboxEventHandler(FileSystemEventHandler):
    """watchdog handler that enqueues envelope paths into a stdlib Queue.

    The callbacks fire on watchdog's internal thread; `queue.Queue` is
    thread-safe so we can put directly without an event-loop bridge.
    """

    def __init__(self, fs_queue: "stdlib_queue.Queue[Path]") -> None:
        self._q = fs_queue

    def _enqueue(self, raw_path: str) -> None:
        path = Path(raw_path)
        if path.suffix != ".json":
            return
        # The `processed/` subdir holds claimed envelopes; ignore moves
        # into it (those are OUR own consume_envelope() calls).
        if mailbox.PROCESSED_DIRNAME in path.parts:
            return
        # Skip the writer's temp file pattern `<name>.tmp.<pid>.<hex>`;
        # we only want the final envelope path after os.replace.
        if ".tmp." in path.name:
            return
        try:
            self._q.put_nowait(path)
        except stdlib_queue.Full:
            _log.warning("watch queue full; dropping event for %s", path)

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        self._enqueue(event.src_path)

    def on_moved(self, event: FileMovedEvent) -> None:
        # `os.replace(tmp, target)` from a writer surfaces as `on_moved`
        # with `dest_path` = the final envelope path. We watch the
        # destination because that is the visible-to-readers state.
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None)
        if dest:
            self._enqueue(dest)


# -- envelope dispatch --------------------------------------------------


async def _channel_push(
    write_stream: MemoryObjectSendStream[SessionMessage],
    content: str,
    meta: dict[str, str] | None = None,
) -> None:
    """Emit a `notifications/claude/channel` push into our OWN session.

    Cross-session push is not supported per RESEARCH §J.3; this only
    surfaces in the subprocess's own session, which IS what the watch
    loop wants (the envelope arrived in this Agent's mailbox, so the
    target IS us).

    Raw `JSONRPCNotification` write to the write_stream is necessary
    because the SDK's `send_notification` only accepts typed
    `ServerNotification` variants and `claude/channel` is an extension.
    """
    params: dict[str, Any] = {"content": content}
    if meta:
        params["meta"] = meta
    notification = mcp_types.JSONRPCNotification(
        jsonrpc="2.0",
        method=CHANNEL_NOTIFICATION_METHOD,
        params=params,
    )
    session_message = SessionMessage(
        message=mcp_types.JSONRPCMessage(notification),
    )
    try:
        await write_stream.send(session_message)
    except anyio.ClosedResourceError:
        # Peer (Claude Code) disconnected; the parent task group will
        # shut us down. Avoid noisy stacktrace.
        _log.debug("channel push aborted: write stream closed")


def _format_channel_content(envelope: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Render an envelope into channel `content` + `meta` per SPEC §7.

    `meta` keys must be identifiers (letters, digits, underscores) per
    the channels-reference contract; keys with hyphens are silently
    dropped by Claude Code. We use snake_case to stay within that.
    """
    payload = envelope.get("payload") or {}
    sender = envelope.get("sender", "<unknown>")
    correlation_id = envelope.get("correlation_id", "")
    kind = envelope.get("kind", "")

    meta: dict[str, str] = {
        "sender": str(sender),
        "kind": str(kind),
        "correlation_id": str(correlation_id),
    }

    if kind == mailbox.KIND_DELIVER_MESSAGE:
        text = str(payload.get("message_text", ""))
        if payload.get("requires_answer"):
            meta["requires_answer"] = "true"
        content = text
    elif kind == mailbox.KIND_INTERRUPT:
        redirect = payload.get("redirect_prompt")
        if redirect:
            content = (
                f"INTERRUPT from {sender}: your current activity has been "
                f"interrupted. Redirect: {redirect}"
            )
        else:
            content = (
                f"INTERRUPT from {sender}: your current activity has been interrupted."
            )
    elif kind == mailbox.KIND_CLOSE:
        content = (
            f"CLOSE from {sender}: this Agent is being shut down. "
            "Any in-flight work will not be resumed."
        )
    elif kind == mailbox.KIND_NUDGE:
        # Phase 8: surface the nudge in the agent's session. Includes
        # the original sender + counts so the LLM has the context to
        # reply.
        from_who = str(payload.get("from", sender))
        n = payload.get("nudge_count", "?")
        rem = payload.get("remaining_nudges", "?")
        body = str(payload.get("message", ""))
        content = (
            f"NUDGE #{n} (remaining {rem}) about an unreplied "
            f"requires_answer message from {from_who!r}.\n{body}"
        )
        meta["nudge_count"] = str(n)
    elif kind == mailbox.KIND_NUDGE_CHECK:
        # Control envelope; no channel push intended (it's a
        # server-internal signal). _process_envelope skips the push
        # call for this kind. We return a placeholder content so the
        # call site is uniform.
        content = ""
    else:
        # Defensive: unrecognized kinds are surfaced verbatim so the
        # operator can see them.
        content = (
            f"Unrecognized envelope kind {kind!r} from {sender}: "
            f"{json.dumps(payload, ensure_ascii=False)}"
        )

    return content, meta


def _self_stop(session_id: str) -> None:
    """Invoke `claude stop <daemonShort>` for graceful self-shutdown.

    Called when this Agent's own watch loop sees a `kind=close`
    envelope. Best-effort: failures are logged but do not crash the
    subprocess (the Coordinator's `close_agent` invocation also runs
    `claude stop` directly, so this is a redundant safety net).

    Phase 5 carryover #4: `claude stop` takes the 8-char daemonShort
    (first 8 chars of the session_id UUID), not the full UUID. See
    `close_agent._do_close` for the same derivation.
    """
    daemon_short = session_id.split("-", 1)[0]
    try:
        proc = subprocess.run(
            ["claude", "stop", daemon_short],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            _log.warning(
                "self `claude stop %s` exited rc=%d: stdout=%r stderr=%r",
                daemon_short,
                proc.returncode,
                proc.stdout[:200],
                proc.stderr[:200],
            )
        else:
            _log.info("self `claude stop %s` succeeded", daemon_short)
    except FileNotFoundError:
        _log.error("self-stop failed: `claude` binary not on PATH")
    except subprocess.TimeoutExpired:
        _log.error("self-stop timed out for daemon_short=%s", daemon_short)
    except OSError as exc:
        _log.error("self-stop OSError for daemon_short=%s: %s", daemon_short, exc)


async def _process_envelope(
    path: Path,
    write_stream: MemoryObjectSendStream[SessionMessage],
    self_session_id: str | None,
    self_agent_name: str | None = None,
) -> None:
    """Read, claim, dispatch one envelope. Idempotent on race."""
    _log.debug("process envelope: path=%s", path)
    # 1) Claim atomically. If another reader won the race, bail.
    try:
        claimed = mailbox.consume_envelope(path)
    except mailbox.MailboxError as exc:
        _log.error("Failed to claim %s: %s", path, exc)
        return
    if claimed is None:
        _log.debug("envelope %s already claimed", path)
        return
    _log.debug("claimed envelope: %s -> %s", path, claimed)

    # 2) Read the claimed file.
    try:
        envelope = mailbox.read_envelope(claimed)
    except mailbox.MailboxError as exc:
        _log.error("Failed to read claimed envelope %s: %s", claimed, exc)
        return

    kind = envelope.get("kind")

    # 3) Phase 8: nudge_check control envelopes are server-internal
    #    signals. We process them BEFORE the channel-push step
    #    because we don't want to leak them to the LLM. The Stop hook
    #    drops these into mailbox/<self>/control/ to ask the MCP to
    #    run a past-due-nudge check.
    if kind == mailbox.KIND_NUDGE_CHECK:
        if self_agent_name:
            try:
                # Local import to avoid a circular dep at module
                # load time (nudge imports rules which imports paths
                # which is fine, but keeping the import local is
                # cheap insurance).
                from . import nudge as _nudge

                _nudge.fire_due_nudges(self_agent_name)
            except Exception as exc:
                _log.exception("nudge_check failed: %s", exc)
        return

    # 4) Push channel notification (for inbox-kind envelopes).
    content, meta = _format_channel_content(envelope)
    _log.debug(
        "pushing channel notification: kind=%s sender=%s content_len=%d",
        kind,
        envelope.get("sender"),
        len(content),
    )
    await _channel_push(write_stream, content=content, meta=meta)
    _log.debug("channel push sent for %s", claimed.name)

    # 5) Phase 8: deliver_message envelopes with requires_answer=true
    #    register a pending-reply obligation + schedule the initial
    #    nudge timer. This runs INSIDE the MCP event loop, so the
    #    SPEC §6.6 single-writer rule on _pending_reply_to.json holds.
    if (
        kind == mailbox.KIND_DELIVER_MESSAGE
        and isinstance(envelope.get("payload"), dict)
        and envelope["payload"].get("requires_answer")
        and self_agent_name
    ):
        try:
            from . import nudge as _nudge

            entry = _nudge.add_pending(
                agent_name=self_agent_name,
                correlation_id=envelope["correlation_id"],
                sender=str(envelope.get("sender", "")),
                sender_handle="",  # not known here; can be enriched
            )
            _nudge.schedule_initial_timer(self_agent_name, entry)
        except Exception as exc:
            _log.exception("nudge add_pending / schedule failed: %s", exc)

    # 6) Side effects per kind.
    if kind == mailbox.KIND_CLOSE:
        if self_session_id:
            # Defer slightly so the channel push has a chance to flush
            # before we kill ourselves.
            await anyio.sleep(0.5)
            await anyio.to_thread.run_sync(_self_stop, self_session_id)
        else:
            _log.warning(
                "kind=close envelope %s received but no self_session_id "
                "known; channel pushed but no self-stop.",
                claimed,
            )


def _drain_existing(
    directories: list[Path],
    fs_queue: "stdlib_queue.Queue[Path]",
) -> None:
    """Enqueue any envelopes left over from before startup.

    `watchdog.Observer` only fires on events after `start()`; files
    that landed while this subprocess was not running need an explicit
    sweep. Called once on startup, before processing watch events.
    """
    for directory in directories:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix != ".json":
                continue
            if mailbox.PROCESSED_DIRNAME in entry.parts:
                continue
            if ".tmp." in entry.name:
                continue
            try:
                fs_queue.put_nowait(entry)
            except stdlib_queue.Full:
                _log.warning("startup sweep dropped %s (queue full)", entry)


# -- identity binding ---------------------------------------------------


#: Maximum total wait when polling for identity at startup. Identity
#: is set by `spawn_agent` BEFORE the MCP subprocess launches, so it
#: should resolve on the first poll; the retry budget is paranoia for
#: rare cases where `_handles/<handle>.json` lands a beat late.
_IDENTITY_MAX_WAIT_S = 10.0

#: Interval between identity-resolution polls during the startup wait.
_IDENTITY_POLL_INTERVAL_S = 0.5


def resolve_self() -> tuple[str | None, str | None]:
    """Resolve (agent_name, session_id) for this subprocess.

    Returns (None, None) if identity is not yet bound. Callers should
    treat this as "not ready yet" and retry shortly -- the bootstrap
    loop polls at `_IDENTITY_POLL_INTERVAL_S` intervals.
    """
    handle = identity.read_env_handle()
    if handle is None:
        return None, None
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        _log.warning("watch: handle read failed: %s", exc)
        return None, None
    if record is None:
        return None, None
    name = record.get("agent_name")
    session_id = record.get("session_id")
    if not isinstance(name, str) or not name:
        return None, None
    return name, session_id if isinstance(session_id, str) else None


# -- public entry points ------------------------------------------------


async def run_watch_loop(
    agent_name: str,
    write_stream: MemoryObjectSendStream[SessionMessage],
    self_session_id: str | None,
) -> None:
    """Run the watch loop for `agent_name` until cancellation.

    Sets up `watchdog.Observer` over `mailbox/<self>/inbox/` and
    `mailbox/<self>/control/`, drains pre-existing envelopes, then
    processes events until the parent task group is cancelled (which
    happens when the MCP transport closes).
    """
    try:
        inbox = mailbox.inbox_dir(agent_name)
        control = mailbox.control_dir(agent_name)
    except paths.OperonPathError as exc:
        _log.warning("watch loop not started for %r: %s", agent_name, exc)
        return

    # Ensure target directories exist; watchdog.Observer.schedule()
    # raises if the path is missing.
    inbox.mkdir(parents=True, exist_ok=True)
    control.mkdir(parents=True, exist_ok=True)
    mailbox.processed_dir(inbox).mkdir(parents=True, exist_ok=True)
    mailbox.processed_dir(control).mkdir(parents=True, exist_ok=True)

    fs_queue: "stdlib_queue.Queue[Path]" = stdlib_queue.Queue(maxsize=_QUEUE_BUFSIZE)
    handler = _MailboxEventHandler(fs_queue)
    observer = Observer()
    observer.schedule(handler, str(inbox), recursive=False)
    observer.schedule(handler, str(control), recursive=False)
    observer.start()
    _log.info(
        "watch loop started for agent=%r inbox=%s control=%s",
        agent_name,
        inbox,
        control,
    )

    # Sweep existing files into the queue once before processing the
    # live stream.
    _drain_existing([inbox, control], fs_queue)

    try:
        while True:
            try:
                # Run the blocking get in a worker thread so cancellation
                # at the await boundary is honored. The 0.5s timeout
                # bounds the worker thread's lifetime per iteration.
                path = await anyio.to_thread.run_sync(
                    partial(fs_queue.get, True, _POLL_TIMEOUT_S),
                    abandon_on_cancel=True,
                )
            except stdlib_queue.Empty:
                continue
            except Exception as exc:  # pragma: no cover (defensive)
                _log.exception("watch loop: queue.get raised: %s", exc)
                continue
            try:
                await _process_envelope(
                    path,
                    write_stream,
                    self_session_id,
                    self_agent_name=agent_name,
                )
            except Exception as exc:  # pragma: no cover (defensive)
                _log.exception("watch loop: error processing %s: %s", path, exc)
    finally:
        # Stop watchdog on cancellation. join() may briefly block; the
        # 2s ceiling is a hard upper bound on shutdown latency.
        try:
            observer.stop()
            observer.join(timeout=2.0)
        except Exception:  # pragma: no cover (defensive)
            pass
        _log.info("watch loop stopped for agent=%r", agent_name)


async def bootstrap_and_run(
    write_stream: MemoryObjectSendStream[SessionMessage],
) -> None:
    """Resolve identity (with bounded retry) then run the watch loop.

    Eager-start architecture (Phase 4 fix): scheduled by `server._run()`
    on the long-lived task group, parallel to `server.run()`. The
    bootstrap polls `resolve_self()` until identity is bound -- because
    `claude --bg` sessions may never invoke `tools/list` on their own,
    the previous lazy-start-on-tools/list path would hang indefinitely
    for any worker whose initial prompt did not require a tool call.

    Identity is bound by `spawn_agent` BEFORE the subprocess launches
    (handle file is written first, then `Popen` with the env override),
    so the first poll should succeed for any agent spawned via
    `spawn_agent`. The retry budget covers manual seedings and rare
    filesystem propagation delays.

    If identity does not resolve within `_IDENTITY_MAX_WAIT_S`, the
    bootstrap logs and exits -- the subprocess can still serve
    `whoami` for diagnostics, but the mailbox watch loop never starts.
    """
    _log.info("watch bootstrap: polling for identity")
    elapsed = 0.0
    while elapsed < _IDENTITY_MAX_WAIT_S:
        name, session_id = resolve_self()
        if name is not None:
            _log.info(
                "watch bootstrap: identity resolved agent=%r session_id=%s after %.1fs",
                name,
                session_id,
                elapsed,
            )
            await run_watch_loop(name, write_stream, session_id)
            return
        await anyio.sleep(_IDENTITY_POLL_INTERVAL_S)
        elapsed += _IDENTITY_POLL_INTERVAL_S
    _log.warning(
        "watch bootstrap: identity not bound within %.1fs; watch loop "
        "will not start for this subprocess (whoami still serves)",
        _IDENTITY_MAX_WAIT_S,
    )
