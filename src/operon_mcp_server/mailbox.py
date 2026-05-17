"""Mailbox filesystem operations for inter-agent messaging.

Per SPEC.md section 16 (`mailbox.py` row) and section 17 (mailbox path
schemas), this module owns atomic envelope I/O for the per-Agent mailbox
directories under `<run-dir>/mailbox/<agent_name>/`. It is the single
seam between the in-memory messaging tools (`message_agent`,
`broadcast_message`, `interrupt_agent`, `close_agent`) and the on-disk
protocol.

Per-Agent mailbox layout (SPEC section 17):

    <run-dir>/mailbox/<agent_name>/
        inbox/<ulid>.json            # deliver_message envelopes
        inbox/processed/<ulid>.json  # claimed by target's watch loop
        control/<ulid>.json          # interrupt + close envelopes
        control/processed/<ulid>.json
        acks/<correlation_id>.json   # reply acks (Phase 8)
        _pending_reply_to.json       # reply-obligation state (Phase 8)

MessageEnvelope shape (SPEC section 17):

    {
      "schema_version": 1,
      "sender": "<agent_name>",
      "target": "<agent_name>",
      "kind": "deliver_message" | "interrupt" | "close",
      "payload": {...},
      "correlation_id": "<ulid>",
      "created_at": "<iso8601>"
    }

Concurrency contract (SPEC section 6.6): writers drop unique-id files
via `os.replace` from a temp path inside the same directory so the file
appears atomically. Readers claim a file by `os.replace` to the sibling
`processed/` subdirectory before acting on it -- this is the single
visible action that removes the file from the watch loop's pickup set
and is safe for concurrent watchers (one wins; the loser sees ENOENT).

Cross-platform: `pathlib.Path` everywhere, UTF-8 on every read/write,
`os.replace` for atomic rename per SPEC section 2.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

#: Current envelope schema version (SPEC section 17). Future schema
#: changes must increment this value AND keep readers backward
#: compatible -- consumers should tolerate older shapes if the live
#: deployment may have unconsumed envelopes from before the bump.
SCHEMA_VERSION = 1

#: Envelope kind discriminator values (SPEC section 17). `redirect` is
#: NOT a separate kind: redirect prompts ride on the `interrupt` kind's
#: payload (`payload.redirect_prompt`).
KIND_DELIVER_MESSAGE = "deliver_message"
KIND_INTERRUPT = "interrupt"
KIND_CLOSE = "close"

_ALLOWED_KINDS = frozenset({KIND_DELIVER_MESSAGE, KIND_INTERRUPT, KIND_CLOSE})

#: Subdirectory name where consumed envelopes are moved (SPEC 6.6).
PROCESSED_DIRNAME = "processed"


class MailboxError(RuntimeError):
    """Raised on mailbox I/O or validation failures."""


# -- path helpers --------------------------------------------------------


def mailbox_root(start: Path | None = None) -> Path:
    """Return `<run-dir>/mailbox/` for the active run."""
    return paths.active_run_dir(start) / "mailbox"


def agent_mailbox(agent_name: str, start: Path | None = None) -> Path:
    """Return `<run-dir>/mailbox/<agent_name>/`.

    Caller is responsible for ensuring `agent_name` is a valid roster
    entry; this function performs no roster lookup so it can be called
    from contexts where the roster is unavailable (e.g. tests).
    """
    if not agent_name:
        raise MailboxError("agent_name must be a non-empty string")
    return mailbox_root(start) / agent_name


def inbox_dir(agent_name: str, start: Path | None = None) -> Path:
    """Return `<run-dir>/mailbox/<agent_name>/inbox/`."""
    return agent_mailbox(agent_name, start) / "inbox"


def control_dir(agent_name: str, start: Path | None = None) -> Path:
    """Return `<run-dir>/mailbox/<agent_name>/control/`."""
    return agent_mailbox(agent_name, start) / "control"


def acks_dir(agent_name: str, start: Path | None = None) -> Path:
    """Return `<run-dir>/mailbox/<agent_name>/acks/`."""
    return agent_mailbox(agent_name, start) / "acks"


def processed_dir(parent: Path) -> Path:
    """Return the sibling `processed/` directory under an envelope dir."""
    return parent / PROCESSED_DIRNAME


# -- envelope construction ----------------------------------------------


def _now_iso() -> str:
    """Return current UTC time as ISO-8601, suitable for envelope JSON."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_envelope_id() -> str:
    """Return a fresh sortable id for an envelope filename.

    Per SPEC §6.6 amended (this phase) the id may be a ULID or a
    UUID-hex; we use UUIDv4 here for stdlib-only purity. The amendment
    documents this allowance so spec readers do not expect a strict
    ULID-only convention.

    Time prefix is included so a glob of an empty directory returns
    files in roughly creation order, which helps debugging without
    paying the ULID dependency cost.
    """
    # Millisecond timestamp ensures rough ordering on a directory listing.
    ts_ms = int(time.time() * 1000)
    return f"{ts_ms:013d}-{uuid.uuid4().hex}"


def build_envelope(
    *,
    sender: str,
    target: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Construct a `MessageEnvelope` dict matching SPEC section 17.

    Generates a fresh `correlation_id` if not supplied. The same id is
    used as the filename stem when the envelope is written, so the
    sender can pair acks to the original send without parsing payload.

    Raises `MailboxError` on invalid `kind`. Caller is responsible for
    ensuring `payload` is JSON-serializable.
    """
    if kind not in _ALLOWED_KINDS:
        raise MailboxError(
            f"Invalid envelope kind {kind!r}; must be one of {sorted(_ALLOWED_KINDS)}."
        )
    if not sender:
        raise MailboxError("sender must be a non-empty string")
    if not target:
        raise MailboxError("target must be a non-empty string")
    if correlation_id is None:
        correlation_id = _new_envelope_id()
    return {
        "schema_version": SCHEMA_VERSION,
        "sender": sender,
        "target": target,
        "kind": kind,
        "payload": payload or {},
        "correlation_id": correlation_id,
        "created_at": _now_iso(),
    }


# -- atomic write -------------------------------------------------------


def _atomic_write_json(target_path: Path, payload: dict[str, Any]) -> Path:
    """Write `payload` atomically to `target_path` (temp + os.replace).

    Per SPEC §6.6: writers drop unique-id files via `os.replace` from a
    temp path inside the SAME directory (cross-filesystem renames are
    not atomic on POSIX, so the temp stays in the target dir).
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = target_path.with_name(
        f"{target_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, target_path)
    except OSError as exc:
        # Best-effort temp cleanup; failure here is non-fatal.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise MailboxError(
            f"Failed to write envelope '{target_path}': {exc}"
        ) from exc
    return target_path


def write_envelope(
    envelope: dict[str, Any],
    target_agent: str,
    kind: str,
    start: Path | None = None,
) -> Path:
    """Write `envelope` into the appropriate mailbox dir for `target_agent`.

    Returns the final on-disk path. Atomic per SPEC §6.6.

    Routing (SPEC §17 + §7):
      - `kind="deliver_message"` -> `mailbox/<target>/inbox/<id>.json`
      - `kind="interrupt"`       -> `mailbox/<target>/control/<id>.json`
      - `kind="close"`           -> `mailbox/<target>/control/<id>.json`
    """
    if kind not in _ALLOWED_KINDS:
        raise MailboxError(
            f"Invalid envelope kind {kind!r}; must be one of {sorted(_ALLOWED_KINDS)}."
        )
    if envelope.get("kind") != kind:
        raise MailboxError(
            f"Envelope kind {envelope.get('kind')!r} does not match write kind {kind!r}."
        )
    if envelope.get("target") != target_agent:
        raise MailboxError(
            f"Envelope target {envelope.get('target')!r} does not match write target "
            f"{target_agent!r}."
        )
    correlation_id = envelope.get("correlation_id")
    if not isinstance(correlation_id, str) or not correlation_id:
        raise MailboxError("envelope.correlation_id must be a non-empty string")

    if kind == KIND_DELIVER_MESSAGE:
        directory = inbox_dir(target_agent, start)
    else:  # interrupt or close
        directory = control_dir(target_agent, start)
    target_path = directory / f"{correlation_id}.json"
    return _atomic_write_json(target_path, envelope)


# -- read + consume -----------------------------------------------------


def read_envelope(path: Path) -> dict[str, Any]:
    """Read and parse a `MessageEnvelope` JSON file.

    Raises `MailboxError` on read or parse failure. Performs minimal
    validation (required top-level keys + recognized `kind`); does not
    re-validate payload contents.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MailboxError(f"Failed to read envelope '{path}': {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MailboxError(f"Envelope '{path}' is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MailboxError(
            f"Envelope '{path}' must be a JSON object, got {type(data).__name__}."
        )
    kind = data.get("kind")
    if kind not in _ALLOWED_KINDS:
        raise MailboxError(
            f"Envelope '{path}' has invalid kind {kind!r}; "
            f"must be one of {sorted(_ALLOWED_KINDS)}."
        )
    for key in ("sender", "target", "correlation_id"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise MailboxError(
                f"Envelope '{path}' missing or empty field {key!r}."
            )
    return data


def consume_envelope(path: Path) -> Path | None:
    """Atomically claim an envelope by moving it to the sibling `processed/`.

    Returns the new path on success, or `None` if the envelope had
    already been claimed (`os.replace` raised `FileNotFoundError`).
    Other `OSError`s are re-raised as `MailboxError` so callers cannot
    silently lose envelopes.

    SPEC §6.6: "Readers claim a file by os.replace to a sibling
    processed/ subdirectory before acting on it. No simultaneous
    reader/writer ever opens the same file path."
    """
    dest_dir = processed_dir(path.parent)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    try:
        os.replace(path, dest)
    except FileNotFoundError:
        # Another reader won the race; envelope is already claimed.
        return None
    except OSError as exc:
        raise MailboxError(
            f"Failed to claim envelope '{path}' -> '{dest}': {exc}"
        ) from exc
    return dest
