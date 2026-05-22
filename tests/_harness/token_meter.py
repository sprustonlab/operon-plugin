"""Cumulative token meter sourced from JSONL ``result`` blocks.

Per TEST_SPECIFICATION.md "Token cap" section + Q9 coordinator answer:

  Token source = stream-JSON ``result`` blocks. Per-scenario
  ``TOKEN_CAP`` constant at module level. Token accumulator tracks
  cumulative spend across sub-acts. Sum input + output + cache_creation
  (cache_read excluded -- it does not reflect spend).

Claude Code's interactive TUI also writes per-iteration ``usage`` blocks
under ``message.usage`` in the JSONL stream. This meter scans for
either shape and de-duplicates by ``uuid``.

Cross-platform: ``pathlib.Path``, UTF-8, ASCII-only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TokenBreakdown:
    """One usage tally, summed per Q9 rules (in + out + cache_creation)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def billable(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
        )

    def __add__(self, other: "TokenBreakdown") -> "TokenBreakdown":
        return TokenBreakdown(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens
                + other.cache_creation_input_tokens
            ),
        )


@dataclass
class TokenMeter:
    """Accumulating token meter that reads a JSONL transcript file.

    Call :meth:`refresh` to re-scan the transcript and update the
    cumulative total. The meter dedupes usage records by record uuid so
    repeated scans are idempotent.
    """

    transcript_path: Path
    cap: int
    seen_uuids: set[str] = field(default_factory=set)
    cumulative: TokenBreakdown = field(default_factory=TokenBreakdown)
    history: list[tuple[str, TokenBreakdown]] = field(default_factory=list)

    def _extract_usage(self, rec: dict) -> TokenBreakdown | None:
        """Pull a usage block from a JSONL record if it carries one."""
        usage = None
        if "usage" in rec and isinstance(rec["usage"], dict):
            usage = rec["usage"]
        else:
            msg = rec.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                usage = msg["usage"]
        if not usage:
            return None
        return TokenBreakdown(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_creation_input_tokens=int(
                usage.get("cache_creation_input_tokens", 0) or 0
            ),
        )

    def refresh(self) -> TokenBreakdown:
        """Re-scan the JSONL file; return the cumulative billable totals."""
        if not self.transcript_path.exists():
            return self.cumulative
        with self.transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = rec.get("uuid") or rec.get("request_id")
                if not rid or rid in self.seen_uuids:
                    continue
                usage = self._extract_usage(rec)
                if usage is None:
                    continue
                self.seen_uuids.add(rid)
                self.cumulative = self.cumulative + usage
        return self.cumulative

    def checkpoint(self, label: str) -> TokenBreakdown:
        """Refresh + record a labelled snapshot in history. Returns total."""
        before = TokenBreakdown(
            input_tokens=self.cumulative.input_tokens,
            output_tokens=self.cumulative.output_tokens,
            cache_creation_input_tokens=self.cumulative.cache_creation_input_tokens,
        )
        self.refresh()
        delta = TokenBreakdown(
            input_tokens=self.cumulative.input_tokens - before.input_tokens,
            output_tokens=self.cumulative.output_tokens - before.output_tokens,
            cache_creation_input_tokens=(
                self.cumulative.cache_creation_input_tokens
                - before.cache_creation_input_tokens
            ),
        )
        self.history.append((label, delta))
        return self.cumulative

    def assert_under_cap(self, label: str = "") -> None:
        """Raise AssertionError if cumulative billable exceeds cap."""
        total = self.cumulative.billable
        if total > self.cap:
            trail = "\n".join(
                f"  {lbl}: +{br.billable} (in={br.input_tokens} "
                f"out={br.output_tokens} cc={br.cache_creation_input_tokens})"
                for lbl, br in self.history
            )
            raise AssertionError(
                f"TOKEN_CAP breached at {label!r}: cumulative billable "
                f"{total} > cap {self.cap}\nHistory:\n{trail}"
            )
