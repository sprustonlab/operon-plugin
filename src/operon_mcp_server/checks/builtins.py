"""Built-in advance check implementations. LEAF MODULE.

Imports only from `operon_mcp_server.checks.protocol` (and stdlib +
`re`). Per SPEC §11.2; the five built-in check types are:

- `command-output-check` -- run a shell command (30s timeout), regex
  the stdout
- `file-exists-check` -- one or more paths exist
- `file-content-check` -- regex match in any line of any matching path
- `manual-confirm` -- `elicitation/create` form prompt to the user
  (LLM is not in the loop). The elicitation callable is injected by
  the engine -- this module does NOT import the MCP SDK.
- `artifact-dir-ready-check` -- `set_artifact_dir` has been called for
  this run (checked via `<run-dir>/state.json` presence + non-empty
  `artifact_dir`)

Cross-platform per SPEC §2: `pathlib.Path`, `subprocess.run(...,
encoding="utf-8", timeout=...)`, `re.search`, `os.replace` (used by
the engine for atomic phase-state writes, not here).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .protocol import Check, CheckDecl, CheckResult

#: Default subprocess timeout for `command-output-check` (SPEC §11).
DEFAULT_COMMAND_TIMEOUT_S = 30.0


# -- path helpers --------------------------------------------------------


def _resolve_against(path: str | Path, base_dir: str | Path | None) -> Path:
    """Resolve `path` against `base_dir` if relative; expand `~` first.

    The order matters: `~` expansion FIRST
    (a path with a literal `~` segment is not `is_absolute()` so without
    expansion `base_dir` would be silently prepended to `~/...`).
    """
    p = Path(path).expanduser()
    if p.is_absolute() or base_dir is None:
        return p
    return Path(base_dir) / p


def _coerce_paths(
    path: str | Path | None,
    paths: list[str | Path] | None,
    base_dir: str | Path | None,
) -> list[Path]:
    """Build the resolved path list for file-exists / file-content checks."""
    raw: list[str | Path] = []
    if path is not None:
        raw.append(path)
    if paths is not None:
        raw.extend(paths)
    if not raw:
        raise ValueError(
            "file-exists-check / file-content-check requires either "
            "'path' or 'paths' to be set"
        )
    return [_resolve_against(p, base_dir) for p in raw]


# -- check implementations ----------------------------------------------


class CommandOutputCheck:
    """Pass when shell command's stdout matches a regex (30s timeout)."""

    def __init__(
        self,
        command: str,
        pattern: str,
        cwd: str | Path | None = None,
    ) -> None:
        if not command:
            raise ValueError("command-output-check requires 'command'")
        if not pattern:
            raise ValueError("command-output-check requires 'pattern'")
        self.command = command
        self.compiled_pattern = re.compile(pattern)
        self.cwd = str(cwd) if cwd is not None else None

    async def check(self) -> CheckResult:
        # `subprocess.run` is blocking; offload to a worker thread so we
        # respect the async protocol without freezing the event loop.
        def _run() -> tuple[int, str, str]:
            try:
                proc = subprocess.run(
                    self.command,
                    shell=True,
                    capture_output=True,
                    timeout=DEFAULT_COMMAND_TIMEOUT_S,
                    cwd=self.cwd,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                )
                return proc.returncode, proc.stdout, proc.stderr
            except subprocess.TimeoutExpired:
                return -1, "", f"timeout after {DEFAULT_COMMAND_TIMEOUT_S}s"
            except OSError as exc:
                return -1, "", str(exc)

        rc, stdout, stderr = await asyncio.to_thread(_run)
        if rc == -1 and "timeout" in stderr:
            return CheckResult(
                passed=False,
                evidence=f"Command timed out after {DEFAULT_COMMAND_TIMEOUT_S}s: "
                f"{self.command}",
            )
        if rc == -1 and stderr:
            return CheckResult(
                passed=False, evidence=f"Command failed: {stderr}"[:300]
            )
        match = self.compiled_pattern.search(stdout)
        if match:
            return CheckResult(
                passed=True,
                evidence=f"Pattern matched: {match.group(0)[:200]}",
            )
        excerpt = "\n".join(stdout.strip().splitlines()[:3])
        where = f" (cwd={self.cwd})" if self.cwd else ""
        return CheckResult(
            passed=False,
            evidence=(
                f"Pattern '{self.compiled_pattern.pattern}' not found"
                f"{where}: {excerpt}"
            )[:300],
        )


class FileExistsCheck:
    """Pass when at least one of the configured paths exists."""

    def __init__(
        self,
        path: str | Path | None = None,
        base_dir: str | Path | None = None,
        paths: list[str | Path] | None = None,
    ) -> None:
        self.paths = _coerce_paths(path, paths, base_dir)

    async def check(self) -> CheckResult:
        for p in self.paths:
            if p.exists():
                return CheckResult(passed=True, evidence=f"File found: {p}")
        if len(self.paths) == 1:
            return CheckResult(
                passed=False, evidence=f"File not found: {self.paths[0]}"
            )
        listing = ", ".join(str(p) for p in self.paths)
        return CheckResult(
            passed=False, evidence=f"None of these files exist: {listing}"
        )


class FileContentCheck:
    """Pass when at least one configured file's content matches a regex."""

    def __init__(
        self,
        path: str | Path | None = None,
        pattern: str = "",
        base_dir: str | Path | None = None,
        paths: list[str | Path] | None = None,
    ) -> None:
        if not pattern:
            raise ValueError("file-content-check requires 'pattern'")
        self.paths = _coerce_paths(path, paths, base_dir)
        self.compiled_pattern = re.compile(pattern)

    async def check(self) -> CheckResult:
        misses: list[str] = []
        for p in self.paths:
            if not p.exists():
                misses.append(f"not found: {p}")
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                misses.append(f"cannot read {p}: {exc}")
                continue
            for i, line in enumerate(content.splitlines(), 1):
                if self.compiled_pattern.search(line):
                    return CheckResult(
                        passed=True,
                        evidence=f"{p} line {i}: {line.strip()}"[:200],
                    )
            misses.append(f"pattern not in {p}")
        return CheckResult(
            passed=False,
            evidence=(
                f"Pattern '{self.compiled_pattern.pattern}' not matched "
                f"({'; '.join(misses)})"
            )[:300],
        )


#: Callable shape for the elicitation seam. The engine constructs one
#: of these closing over its `ServerSession.elicit_form` reference and
#: passes it in `params["_elicit"]` for manual-confirm checks. The
#: builtins module never imports the MCP SDK -- this preserves the
#: leaf-module discipline declared in SPEC §16 (`checks/`).
ElicitCallable = Callable[[str], Awaitable[bool]]


class ManualConfirm:
    """Pass on user-accept + `confirm: true` (via injected elicitation seam).

    Per SPEC §11 the schema is fixed: `{"type": "object", "properties":
    {"confirm": {"type": "boolean", "title": "Approve advance?"}},
    "required": ["confirm"]}`. The engine builds the elicitation
    closure with that schema baked in; this check only needs to invoke
    it and surface accept/decline.
    """

    def __init__(self, prompt: str, elicit: ElicitCallable) -> None:
        if not prompt:
            raise ValueError("manual-confirm requires 'prompt' (or 'question')")
        if elicit is None:
            raise ValueError(
                "manual-confirm requires '_elicit' callable injected by engine"
            )
        self.prompt = prompt
        self.elicit = elicit

    async def check(self) -> CheckResult:
        try:
            confirmed = await self.elicit(self.prompt)
        except Exception as exc:
            return CheckResult(
                passed=False,
                evidence=f"Elicitation failed: {exc}",
            )
        if confirmed:
            return CheckResult(passed=True, evidence="User confirmed")
        return CheckResult(passed=False, evidence="User declined")


class ArtifactDirReadyCheck:
    """Pass when `set_artifact_dir` has been called for the current run.

    SPEC §11 + §17: `set_artifact_dir` persists `artifact_dir` to
    `<run-dir>/state.json`. This check reads that file and passes
    if the value is set and non-empty.
    """

    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file

    async def check(self) -> CheckResult:
        if not self.state_file.is_file():
            return CheckResult(
                passed=False,
                evidence=(
                    "Artifact directory not set -- call "
                    "`set_artifact_dir(...)` MCP tool before advancing."
                ),
            )
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return CheckResult(
                passed=False,
                evidence=f"Cannot read {self.state_file}: {exc}",
            )
        artifact_dir = data.get("artifact_dir") if isinstance(data, dict) else None
        if not isinstance(artifact_dir, str) or not artifact_dir:
            return CheckResult(
                passed=False,
                evidence=(
                    "state.json present but `artifact_dir` is missing/empty; "
                    "call set_artifact_dir(...) again."
                ),
            )
        return CheckResult(
            passed=True, evidence=f"artifact_dir set: {artifact_dir}"
        )


# -- registry + decl -> check builder -----------------------------------


_BUILTIN_TYPES = frozenset(
    {
        "command-output-check",
        "file-exists-check",
        "file-content-check",
        "manual-confirm",
        "artifact-dir-ready-check",
    }
)


def _build_check(decl: CheckDecl) -> Check:
    """Map a `CheckDecl` to the executable `Check`.

    The engine injects type-specific seam parameters into `decl.params`
    before calling this:
    - `base_dir`: the workflow root, for path resolution in file-*
      checks.
    - `_elicit`: the elicitation callable, for manual-confirm.
    - `state_file`: the run's `state.json` path, for
      artifact-dir-ready-check.
    - `cwd`: subprocess cwd for command-output-check (engine pins to
      workflow root or project root per the manifest).
    """
    p = dict(decl.params)
    t = decl.type
    if t == "command-output-check":
        return CommandOutputCheck(
            command=p["command"], pattern=p["pattern"], cwd=p.get("cwd")
        )
    if t == "file-exists-check":
        return FileExistsCheck(
            path=p.get("path"),
            paths=p.get("paths"),
            base_dir=p.get("base_dir"),
        )
    if t == "file-content-check":
        return FileContentCheck(
            path=p.get("path"),
            pattern=p.get("pattern", ""),
            paths=p.get("paths"),
            base_dir=p.get("base_dir"),
        )
    if t == "manual-confirm":
        prompt = p.get("prompt") or p.get("question") or "Approve advance?"
        return ManualConfirm(prompt=prompt, elicit=p.get("_elicit"))
    if t == "artifact-dir-ready-check":
        return ArtifactDirReadyCheck(state_file=p["state_file"])
    raise ValueError(f"Unknown check type: {t!r}")


def known_check_types() -> set[str]:
    """Return the set of built-in check type identifiers."""
    return set(_BUILTIN_TYPES)


def build_check(decl: CheckDecl) -> Check:
    """Public alias for `_build_check`."""
    return _build_check(decl)


def parse_decl(raw: dict[str, Any]) -> CheckDecl:
    """Parse one workflow-yaml check entry into a `CheckDecl`.

    Expected shape:

        - type: file-exists-check
          path: "${ARTIFACT_DIR}/STATUS.md"
          on_failure:
            message: "STATUS.md not committed"

    All non-{type, on_failure} keys become `params`. The engine
    later layers seam params (`_elicit`, `state_file`, `base_dir`,
    `cwd`) on top of these manifest-derived params.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"check decl must be a mapping; got {type(raw).__name__}")
    t = raw.get("type")
    if not isinstance(t, str) or not t:
        raise ValueError("check decl missing 'type'")
    on_failure = raw.get("on_failure")
    if on_failure is not None and not isinstance(on_failure, dict):
        raise ValueError("check decl 'on_failure' must be a mapping if present")
    params = {k: v for k, v in raw.items() if k not in {"type", "on_failure"}}
    return CheckDecl(type=t, params=params, on_failure=on_failure)
