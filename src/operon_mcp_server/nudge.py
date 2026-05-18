"""Reply-nudge mechanism per SPEC §8 + Phase 8 dispatch.

When an Agent receives a `message_agent(..., requires_answer=true)`
envelope, this module:

1. Records a pending-reply entry in
   `<run-dir>/mailbox/<target>/_pending_reply_to.json`.
2. Schedules an asyncio timer in the target's own MCP event loop.
3. On timer fire (or Stop-hook nudge_check signal), if the target has
   not yet replied, writes a `kind="nudge"` envelope into the
   target's own inbox so the target's next read or channel push
   surfaces a reminder.
4. After `OPERON_NUDGE_MAX` nudges (default 3), gives up and writes
   a `nudge_exhausted` audit row.

The pending-reply file is owned single-writer by the target's MCP
subprocess (SPEC §6.6). Cross-process triggers from the Stop hook
go through a `kind=nudge_check` control envelope so the actual
mutation always happens in the MCP event loop -- no `_pending_reply_to.json`
write race.

Reply detection: when an Agent calls `message_agent(name=Y, ...)`,
the same MCP subprocess clears any pending entries whose `sender`
matches Y. Direct write in-process; same single-writer.

Audit events (extends `guardrail_log.jsonl` taxonomy with nudge-
prefixed types):

  - `nudge_armed`       outcome=pending    : new pending entry, timer set
  - `nudge_fired`       outcome=nudged     : nudge envelope written
  - `nudge_cleared`     outcome=cleared    : reply observed, entry removed
  - `nudge_exhausted`   outcome=exhausted  : max nudges reached, gave up
  - `nudge_skipped_stale` outcome=stale    : timer fired but generation mismatch

Cross-platform per SPEC §2: pathlib, encoding="utf-8", os.replace
for atomic rename. Imports stdlib + `rules.append_log_event` + `mailbox`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import mailbox, paths, rules

_log = logging.getLogger(__name__)

#: Schema version for `_pending_reply_to.json` files.
PENDING_SCHEMA_VERSION = 1

#: Default initial + backoff intervals (seconds) between nudges. Each
#: entry is the wait BEFORE that nudge fires. List length = max
#: nudges. Configurable via `OPERON_NUDGE_INTERVALS` env (comma-
#: separated integers). The Phase 8 dispatch specified 15/30/60 as
#: defaults.
DEFAULT_INTERVALS_S = (15, 30, 60)

#: Filename for the per-agent pending-reply state file.
PENDING_FILENAME = "_pending_reply_to.json"


# -- config --------------------------------------------------------------


def _parse_intervals() -> tuple[int, ...]:
    """Parse `OPERON_NUDGE_INTERVALS=<csv>` from env, or default."""
    raw = os.environ.get("OPERON_NUDGE_INTERVALS", "").strip()
    if not raw:
        return DEFAULT_INTERVALS_S
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            n = int(piece)
        except ValueError:
            _log.warning("OPERON_NUDGE_INTERVALS bad value %r; using defaults", piece)
            return DEFAULT_INTERVALS_S
        if n <= 0:
            _log.warning("OPERON_NUDGE_INTERVALS non-positive %r; using defaults", n)
            return DEFAULT_INTERVALS_S
        out.append(n)
    if not out:
        return DEFAULT_INTERVALS_S
    return tuple(out)


def nudge_max() -> int:
    """Max nudges per pending entry. Derived from interval list length."""
    return len(_parse_intervals())


# -- pending-state file I/O ---------------------------------------------


@dataclass
class PendingEntry:
    """One pending-reply obligation tracked in `_pending_reply_to.json`."""

    correlation_id: str
    sender: str
    sender_handle: str
    received_at: str
    nudge_count: int
    next_nudge_at: str
    generation: int

    def is_due(self, now: datetime | None = None) -> bool:
        """Return True iff `next_nudge_at` is in the past."""
        now = now or datetime.now(timezone.utc)
        nxt = _parse_iso(self.next_nudge_at)
        if nxt is None:
            return False
        return now >= nxt


def _parse_iso(ts: str | None) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pending_file_path(agent_name: str, start: Path | None = None) -> Path:
    """Return `<run-dir>/mailbox/<agent>/_pending_reply_to.json`."""
    if not agent_name:
        raise ValueError("agent_name must be non-empty")
    return mailbox.agent_mailbox(agent_name, start) / PENDING_FILENAME


def read_pending_state(
    agent_name: str, start: Path | None = None
) -> list[PendingEntry]:
    """Read the agent's pending list. Returns [] on missing/malformed."""
    try:
        path = pending_file_path(agent_name, start)
    except paths.OperonPathError:
        return []
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("pending") or []
    if not isinstance(raw, list):
        return []
    out: list[PendingEntry] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(
                PendingEntry(
                    correlation_id=str(entry["correlation_id"]),
                    sender=str(entry["sender"]),
                    sender_handle=str(entry.get("sender_handle", "")),
                    received_at=str(entry["received_at"]),
                    nudge_count=int(entry.get("nudge_count", 0)),
                    next_nudge_at=str(entry["next_nudge_at"]),
                    generation=int(entry.get("generation", 1)),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def write_pending_state(
    agent_name: str,
    entries: list[PendingEntry],
    start: Path | None = None,
) -> Path:
    """Atomic write of the agent's pending list."""
    try:
        path = pending_file_path(agent_name, start)
    except paths.OperonPathError as exc:
        raise OSError(str(exc)) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PENDING_SCHEMA_VERSION,
        "pending": [asdict(e) for e in entries],
    }
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


# -- audit helpers -------------------------------------------------------


def _audit(
    *,
    event_type: str,
    outcome: str,
    agent: str | None,
    correlation_id: str,
    sender: str | None,
    nudge_count: int,
    message: str = "",
) -> None:
    """Append a nudge-flavored row to guardrail_log.jsonl. Best-effort."""
    try:
        rules.append_log_event(
            rules.build_log_event(
                event_type=event_type,
                outcome=outcome,
                rule_id=None,
                agent=agent,
                role=None,
                current_phase=None,
                tool_name="",
                tool_input={
                    "correlation_id": correlation_id,
                    "sender": sender,
                    "nudge_count": nudge_count,
                },
                enforcement=None,
                message=message,
            )
        )
    except Exception as exc:  # pragma: no cover (defensive)
        _log.warning("nudge audit append failed: %s", exc)


# -- core operations -----------------------------------------------------


def add_pending(
    *,
    agent_name: str,
    correlation_id: str,
    sender: str,
    sender_handle: str,
    intervals: tuple[int, ...] | None = None,
) -> PendingEntry:
    """Append a new pending entry for `agent_name` and persist.

    Called by the watch loop when it processes a `deliver_message`
    envelope with `requires_answer=true`. Returns the entry so the
    caller can immediately schedule a timer for it.
    """
    intervals = intervals or _parse_intervals()
    now = datetime.now(timezone.utc)
    next_at = (now + timedelta(seconds=intervals[0])).isoformat(timespec="seconds")

    entries = read_pending_state(agent_name)
    # Increment generation across all entries with the same
    # correlation_id (shouldn't happen normally; defensive).
    max_gen = max(
        (e.generation for e in entries if e.correlation_id == correlation_id), default=0
    )
    entry = PendingEntry(
        correlation_id=correlation_id,
        sender=sender,
        sender_handle=sender_handle,
        received_at=now.isoformat(timespec="seconds"),
        nudge_count=0,
        next_nudge_at=next_at,
        generation=max_gen + 1,
    )
    # Drop any existing entry with the same correlation_id (resend supersede).
    entries = [e for e in entries if e.correlation_id != correlation_id]
    entries.append(entry)
    write_pending_state(agent_name, entries)
    _audit(
        event_type="nudge_armed",
        outcome="pending",
        agent=agent_name,
        correlation_id=correlation_id,
        sender=sender,
        nudge_count=0,
        message=f"first nudge at {next_at} (interval {intervals[0]}s)",
    )
    return entry


def clear_pending_for_sender(agent_name: str, sender_name: str) -> list[PendingEntry]:
    """Remove all pending entries from `agent_name`'s file where
    `sender` matches `sender_name`. Audit `nudge_cleared` per removed.

    Called by `message_agent` tool when this agent is sending a
    reply (the message target matches a pending sender). Returns
    the removed entries for diagnostics.
    """
    entries = read_pending_state(agent_name)
    kept: list[PendingEntry] = []
    removed: list[PendingEntry] = []
    for e in entries:
        if e.sender == sender_name:
            removed.append(e)
        else:
            kept.append(e)
    if not removed:
        return []
    try:
        write_pending_state(agent_name, kept)
    except OSError as exc:
        _log.warning("clear_pending_for_sender write failed: %s", exc)
        return []
    for e in removed:
        _audit(
            event_type="nudge_cleared",
            outcome="cleared",
            agent=agent_name,
            correlation_id=e.correlation_id,
            sender=e.sender,
            nudge_count=e.nudge_count,
            message=f"reply observed to {sender_name!r}",
        )
    return removed


def _build_nudge_envelope(
    *,
    sender_name: str,  # the "from" of the nudge -- conventionally the agent itself
    target_agent: str,
    pending: PendingEntry,
    intervals: tuple[int, ...],
) -> dict[str, Any]:
    """Construct a `kind=nudge` envelope to land in `target_agent`'s inbox."""
    remaining = max(0, len(intervals) - pending.nudge_count - 1)
    return mailbox.build_envelope(
        sender=sender_name,
        target=target_agent,
        kind=mailbox.KIND_NUDGE,
        payload={
            "from": pending.sender,
            "correlation_id": pending.correlation_id,
            "nudge_count": pending.nudge_count + 1,
            "remaining_nudges": remaining,
            "message": (
                f"You have an unreplied message from {pending.sender!r}. "
                f"Please reply via message_agent(name={pending.sender!r}, ...)."
            ),
        },
    )


def fire_due_nudges(
    agent_name: str,
    *,
    intervals: tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """Process all past-due entries: fire nudge envelopes, increment
    counts, advance `next_nudge_at`, or exhaust + remove.

    Returns a summary dict for diagnostics:
      {fired: [<correlation_id>...], exhausted: [<id>...], skipped: int}

    Pure I/O (no async). Safe to call from any code path that has
    write access to `_pending_reply_to.json` -- i.e., only the
    target's own MCP subprocess.
    """
    intervals = intervals or _parse_intervals()
    max_nudges = len(intervals)
    entries = read_pending_state(agent_name)
    if not entries:
        return {"fired": [], "exhausted": [], "skipped": 0}

    now = datetime.now(timezone.utc)
    fired: list[str] = []
    exhausted: list[str] = []
    skipped = 0
    new_entries: list[PendingEntry] = []

    for entry in entries:
        if not entry.is_due(now):
            new_entries.append(entry)
            skipped += 1
            continue

        # Past-due. Either fire nudge or exhaust.
        if entry.nudge_count >= max_nudges:
            exhausted.append(entry.correlation_id)
            _audit(
                event_type="nudge_exhausted",
                outcome="exhausted",
                agent=agent_name,
                correlation_id=entry.correlation_id,
                sender=entry.sender,
                nudge_count=entry.nudge_count,
                message=f"giving up after {entry.nudge_count} nudges",
            )
            # Drop entry (do NOT append to new_entries).
            continue

        # Fire one nudge.
        try:
            envelope = _build_nudge_envelope(
                sender_name=agent_name,
                target_agent=agent_name,
                pending=entry,
                intervals=intervals,
            )
            mailbox.write_envelope(
                envelope,
                target_agent=agent_name,
                kind=mailbox.KIND_NUDGE,
            )
        except mailbox.MailboxError as exc:
            _log.warning("nudge envelope write failed: %s", exc)
            # Keep entry but don't increment; we'll retry next iteration.
            new_entries.append(entry)
            continue

        new_count = entry.nudge_count + 1
        # Schedule the next nudge using the interval[new_count] entry
        # if it exists; otherwise the entry will be exhausted on
        # next fire.
        if new_count < max_nudges:
            wait = intervals[new_count]
            next_at = (now + timedelta(seconds=wait)).isoformat(timespec="seconds")
        else:
            # No more nudges scheduled; the next fire_due_nudges call
            # will see nudge_count == max and exhaust.
            wait = intervals[-1]
            next_at = (now + timedelta(seconds=wait)).isoformat(timespec="seconds")

        updated = PendingEntry(
            correlation_id=entry.correlation_id,
            sender=entry.sender,
            sender_handle=entry.sender_handle,
            received_at=entry.received_at,
            nudge_count=new_count,
            next_nudge_at=next_at,
            generation=entry.generation + 1,
        )
        new_entries.append(updated)
        fired.append(entry.correlation_id)
        _audit(
            event_type="nudge_fired",
            outcome="nudged",
            agent=agent_name,
            correlation_id=entry.correlation_id,
            sender=entry.sender,
            nudge_count=new_count,
            message=f"nudge {new_count}/{max_nudges}; next at {next_at}",
        )

    try:
        write_pending_state(agent_name, new_entries)
    except OSError as exc:
        _log.warning("pending state write failed: %s", exc)

    return {"fired": fired, "exhausted": exhausted, "skipped": skipped}


# -- async timer scheduling (MCP-event-loop owned) -----------------------


def _fire_due_sync(agent_name: str) -> None:
    """Sync wrapper for fire_due_nudges; suitable for asyncio.call_later."""
    try:
        fire_due_nudges(agent_name)
    except Exception as exc:  # pragma: no cover (defensive)
        _log.exception("fire_due_nudges raised: %s", exc)


def schedule_initial_timer(
    agent_name: str,
    entry: PendingEntry,
    *,
    intervals: tuple[int, ...] | None = None,
) -> None:
    """Schedule an `asyncio.call_later` to fire the initial nudge.

    Must be called from inside the MCP server's running event loop.
    The callback re-reads pending state on fire and skips entries
    whose generation has advanced (stale-timer protection); the
    actual fire-or-exhaust decision happens in `fire_due_nudges`.

    Subsequent nudges schedule themselves: after each fire, the
    next nudge's `next_nudge_at` is set and a new timer is armed
    by `_reschedule_after_fire`.
    """
    intervals = intervals or _parse_intervals()
    initial_wait = intervals[0]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.warning(
            "schedule_initial_timer called outside event loop; "
            "Stop hook will pick up the nudge eventually"
        )
        return
    loop.call_later(
        initial_wait, _on_timer_fire, agent_name, entry.correlation_id, entry.generation
    )


def _on_timer_fire(agent_name: str, correlation_id: str, generation: int) -> None:
    """asyncio.call_later callback.

    Re-reads pending state; if the entry is still present at the
    captured generation, fires the due check (which writes nudge +
    reschedules). If generation has advanced (a reply or new message
    superseded), no-op + audit `nudge_skipped_stale`.

    Phase 14 fix 6: bail silently if another MCP subprocess holds the
    per-agent lock. This subprocess is backgrounded (e.g. by Claude
    Code's `/agents` view); writing pending state from here would
    violate SPEC §6.6 single-writer because the new (winner)
    subprocess also writes that file.
    """
    # Local import to avoid circular at module load. `locks` imports
    # only `paths`, which `nudge` already imports, so this is cheap.
    from . import locks as _locks

    if not _locks.we_hold_lock(agent_name):
        _log.debug(
            "nudge timer fire skipped: another MCP subprocess holds the "
            "lock for agent=%r correlation_id=%r",
            agent_name,
            correlation_id,
        )
        return

    try:
        entries = read_pending_state(agent_name)
        entry = next(
            (e for e in entries if e.correlation_id == correlation_id),
            None,
        )
        if entry is None:
            # Cleared between schedule and fire.
            _audit(
                event_type="nudge_skipped_stale",
                outcome="stale",
                agent=agent_name,
                correlation_id=correlation_id,
                sender=None,
                nudge_count=0,
                message="entry cleared before fire",
            )
            return
        if entry.generation > generation:
            _audit(
                event_type="nudge_skipped_stale",
                outcome="stale",
                agent=agent_name,
                correlation_id=correlation_id,
                sender=entry.sender,
                nudge_count=entry.nudge_count,
                message=(
                    f"generation advanced {generation}->{entry.generation}; "
                    "skipping stale timer"
                ),
            )
            return

        # Fire due nudges (may include this entry plus any others
        # that have become past-due).
        result = fire_due_nudges(agent_name)

        # Reschedule the next timer for the entry IF it's still
        # pending after fire_due_nudges. We re-read state to find
        # the updated generation.
        if entry.correlation_id in result.get("fired", []):
            updated_entries = read_pending_state(agent_name)
            updated = next(
                (e for e in updated_entries if e.correlation_id == correlation_id),
                None,
            )
            if updated is not None and updated.nudge_count < nudge_max():
                # Schedule next timer based on the entry's
                # next_nudge_at. Compute seconds-until from now.
                nxt = _parse_iso(updated.next_nudge_at)
                if nxt is not None:
                    wait = max(
                        0.0,
                        (nxt - datetime.now(timezone.utc)).total_seconds(),
                    )
                    try:
                        loop = asyncio.get_running_loop()
                        loop.call_later(
                            wait,
                            _on_timer_fire,
                            agent_name,
                            correlation_id,
                            updated.generation,
                        )
                    except RuntimeError:
                        _log.warning("loop unavailable; relying on Stop hook")
    except Exception as exc:  # pragma: no cover (defensive)
        _log.exception("nudge timer fire failed: %s", exc)


# -- Stop-hook signal pathway (control envelope) ------------------------


def signal_nudge_check(agent_name: str, *, reason: str = "stop_hook") -> Path:
    """Write a `kind=nudge_check` control envelope into the agent's own
    mailbox. The watch loop picks it up and runs fire_due_nudges.

    Called by the Stop hook (separate process from MCP server) to
    request a check without writing pending state directly. This
    keeps the SPEC §6.6 single-writer rule on `_pending_reply_to.json`
    intact: only the MCP event loop mutates pending state.
    """
    envelope = mailbox.build_envelope(
        sender=agent_name,
        target=agent_name,
        kind=mailbox.KIND_NUDGE_CHECK,
        payload={"reason": reason},
    )
    return mailbox.write_envelope(
        envelope,
        target_agent=agent_name,
        kind=mailbox.KIND_NUDGE_CHECK,
    )
