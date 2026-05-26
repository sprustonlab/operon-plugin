"""Check protocol + result type. LEAF MODULE (stdlib only).

This module declares the
Check-Engine seam: a frozen `CheckResult` value object and a
`CheckDecl` declaration record. The executable `Check` is an async
protocol; the engine calls `check()` without knowing the
implementation.

No upward imports. The phase engine (`workflow.py`) imports from
here; this module never imports from `workflow.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single advance check.

    Crosses the Check-Engine seam. `passed` drives the AND-of-checks
    semantics in the phase advance protocol (SPEC §11.1 step 1);
    `evidence` is the human-readable explanation surfaced to the
    Coordinator and (via the tool result) to the user.
    """

    passed: bool
    evidence: str


@dataclass(frozen=True)
class CheckDecl:
    """Declarative check shape parsed from workflow YAML.

    Distinct from `Check` (the runnable). The engine builds Checks
    from Decls via `builtins._build_check`. `params` is the type-
    specific parameter dict; for `manual-confirm` checks the engine
    injects an `_elicit` callable into params before building, since
    the elicitation transport is engine-tier not leaf-tier.
    """

    type: str
    params: dict[str, Any] = field(default_factory=dict)
    on_failure: dict[str, Any] | None = None


@runtime_checkable
class Check(Protocol):
    """Async protocol for all advance checks (SPEC §11)."""

    async def check(self) -> CheckResult: ...
