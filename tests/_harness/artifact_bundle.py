"""Artifact bundle writer.

Per TEST_SPECIFICATION.md "Per-sub-act discipline" section + Q8
coordinator answer, each sub-act emits a snapshot waypoint:

  artifacts/<scenario>/<sub-act-N>/
    events.log.delta
    inboxes/<member>.json
    jsonl.fragment
    tokens.json

The bundle root lives under the test's working directory so pytest can
attach it to failure output if needed.

Cross-platform: ``pathlib.Path``, UTF-8, ``os.replace``, ASCII-only.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ArtifactBundle:
    """Per-scenario artifact bundle. Each sub-act writes one subdir."""

    root: Path
    scenario_name: str
    sub_act_dirs: list[Path] = field(default_factory=list)
    events_log_path: Path | None = None
    inboxes_dir: Path | None = None
    transcript_path: Path | None = None

    def __post_init__(self) -> None:
        self.bundle_dir = self.root / self.scenario_name
        self.bundle_dir.mkdir(parents=True, exist_ok=True)

    def configure_sources(
        self,
        events_log: Path | None,
        inboxes_dir: Path | None,
        transcript: Path | None,
    ) -> None:
        self.events_log_path = events_log
        self.inboxes_dir = inboxes_dir
        self.transcript_path = transcript

    def snapshot(
        self,
        sub_act: str,
        token_state: dict[str, Any],
        notes: dict[str, Any] | None = None,
    ) -> Path:
        """Write a snapshot directory for ``sub_act``.

        Returns the path to the written directory.
        """
        sub_dir = self.bundle_dir / sub_act
        sub_dir.mkdir(parents=True, exist_ok=True)

        # tokens.json (always)
        (sub_dir / "tokens.json").write_text(
            json.dumps({"ts": time.time(), **token_state}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # events.log.delta (best-effort)
        if self.events_log_path and self.events_log_path.exists():
            try:
                shutil.copy(self.events_log_path, sub_dir / "events.log")
            except OSError:
                pass

        # inboxes snapshot
        if self.inboxes_dir and self.inboxes_dir.is_dir():
            inbox_out = sub_dir / "inboxes"
            inbox_out.mkdir(parents=True, exist_ok=True)
            for inbox in self.inboxes_dir.glob("*.json"):
                try:
                    shutil.copy(inbox, inbox_out / inbox.name)
                except OSError:
                    pass

        # jsonl fragment (best-effort full copy; the fragment is "what we've seen
        # so far"). For large transcripts, this might be replaced by a byte-range
        # copy keyed on the prior snapshot offset, but for our 10-sub-act journey
        # a full copy is fine.
        if self.transcript_path and self.transcript_path.exists():
            try:
                shutil.copy(self.transcript_path, sub_dir / "jsonl.fragment")
            except OSError:
                pass

        if notes:
            (sub_dir / "notes.json").write_text(
                json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8"
            )

        self.sub_act_dirs.append(sub_dir)
        return sub_dir
