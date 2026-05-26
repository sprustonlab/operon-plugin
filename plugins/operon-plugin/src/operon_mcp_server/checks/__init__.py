"""Built-in advance checks for the operon phase engine.

LEAF MODULE per SPEC §16: stdlib + `re` + `asyncio.to_thread`. No
imports from `workflow.py`, `roster.py`, `mailbox.py`, or anywhere
else upward. Imported BY `workflow.py` (the engine).
"""

from .builtins import (
    ArtifactDirReadyCheck,
    CommandOutputCheck,
    ElicitCallable,
    FileContentCheck,
    FileExistsCheck,
    ManualConfirm,
    build_check,
    known_check_types,
    parse_decl,
)
from .protocol import Check, CheckDecl, CheckResult

__all__ = [
    "ArtifactDirReadyCheck",
    "Check",
    "CheckDecl",
    "CheckResult",
    "CommandOutputCheck",
    "ElicitCallable",
    "FileContentCheck",
    "FileExistsCheck",
    "ManualConfirm",
    "build_check",
    "known_check_types",
    "parse_decl",
]
