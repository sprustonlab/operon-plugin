"""Per-step recorder.

Per TEST_SPECIFICATION.md "Step recording" section:

  Every step the harness records uses two named fields:
    user_observable=<envelope-register narrative string>
    precise_state=<assertion-register dict with substrate references>

Each scenario gets a :class:`StepRecorder` instance. Records are
appended in memory and to a JSONL file under the artifact bundle so a
failed scenario leaves a complete trail.

Cross-platform: ``pathlib.Path``, UTF-8, ASCII-only.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StepRecord:
    ts: float
    sub_act: str
    user_observable: str
    precise_state: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "sub_act": self.sub_act,
            "user_observable": self.user_observable,
            "precise_state": self.precise_state,
        }


@dataclass
class StepRecorder:
    out_path: Path
    records: list[StepRecord] = field(default_factory=list)
    current_sub_act: str = ""

    def set_sub_act(self, sub_act: str) -> None:
        self.current_sub_act = sub_act

    def record(self, user_observable: str, precise_state: dict[str, Any]) -> None:
        rec = StepRecord(
            ts=time.time(),
            sub_act=self.current_sub_act,
            user_observable=user_observable,
            precise_state=precise_state,
        )
        self.records.append(rec)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with self.out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec.to_json(), ensure_ascii=False) + "\n")
