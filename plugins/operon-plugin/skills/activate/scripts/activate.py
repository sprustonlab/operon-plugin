#!/usr/bin/env python
r"""Shared activation script for operon per-workflow slash commands.

Invoked via each per-workflow SKILL.md as a Bash dynamic-context
injector, through the sibling `activate-wrapper` launcher:

    !`"${CLAUDE_PLUGIN_ROOT}/skills/activate/scripts/activate-wrapper" <workflow_id> $ARGUMENTS`

The wrapper resolves a working Python interpreter (a bare `python3` /
`python`, skipping the Windows Store stub via a pre-flight check, then
`uv run --no-project python`) and exec's this script. SKILL.md never
shells out to bare `python`, which on Windows resolves to the Microsoft
Store alias and fails.

Per SPEC §13b. The script runs as a stdlib-only subprocess of Claude
Code's foreground (Coordinator) session. It is responsible for:

1. Parsing argv. `argv[1]` is the workflow id hardcoded in the SKILL.md
   body. `argv[2:]` are the user-typed `$ARGUMENTS` tokens; the first
   non-empty token (if any) is the candidate run_name.

2. Validating `run_name` client-side per SPEC §7 `activate_workflow`
   rules: filesystem-safe (no `/`, `\`, `:`, `*`, `?`, `<`, `>`, `|`, `"`),
   no leading `.`, non-empty, <=50 chars.

3. If no run_name supplied: prompting interactively via stdin (best-
   effort fallback). When stdin is not a TTY (typical in
   non-interactive `--bg` contexts), the script emits a one-line
   ERROR diagnostic telling the user to retry with an explicit name.

4. On valid input: emitting a single `OPERON_DISPATCH ...` directive
   line to stdout. The companion SKILL.md instructs the LLM to read
   this line and dispatch `mcp__operon__activate_workflow` with the
   parsed `workflow_id` + `run_name`.

5. On invalid input: emitting a single `ERROR: <reason>` line and
   exiting non-zero. SKILL.md tells the LLM to relay the ERROR
   verbatim and NOT to call `activate_workflow`.

Why dispatch via the LLM rather than calling `mcp__operon__activate_workflow`
directly from this script: the plugin install layout does not ship the
`operon_mcp_server` Python source under the plugin cache (only the
MCP server binary shim is bundled). A subprocess cannot import the
server's tool modules in-process, and there is no separate JSON-RPC
endpoint exposed for client-side scripts to call. Routing through the
LLM (which has `mcp__operon__activate_workflow` in `allowed-tools`)
gives the same end-state with a deterministic, validated dispatch
input. The LLM's discretion is bounded to "call the tool with the
exact args the script printed" -- no role-framing involved.

True `elicitation/create` from this subprocess (per the literal SPEC
§13b wording) requires a server-side `elicit_form` MCP tool or a host-
side bidirectional channel; both are deferred to a follow-up phase.
The stdin-prompt fallback covers the interactive case until then.

Cross-platform per SPEC §2: stdlib only, no shell intermediary,
`encoding='utf-8'` on every text I/O path (Python defaults handle
stdin/stdout/print correctly under PYTHONIOENCODING=utf-8 which the
operon-mcp-server shim sets; the prints here use plain ASCII anyway).
ASCII-only source per the cross-platform rules.
"""

from __future__ import annotations

import sys

# -- Validation rules ---------------------------------------------------
#
# Mirrors `src/operon_mcp_server/tools/activate_workflow.py` SPEC §7.
# Kept literal here (rather than importing) because the script is
# stdlib-only by design -- see module docstring.

#: Characters that break paths on at least one supported OS.
_DISALLOWED_CHARS = frozenset('/\\:*?<>|"')

#: Length cap; keeps the run_name under Windows MAX_PATH headroom
#: combined with `<project>/.operon/<run_name>/...` ancestry.
_MAX_RUN_NAME_LEN = 50


def _validate_run_name(run_name: str) -> str | None:
    """Return None if `run_name` passes every SPEC §7 rule.

    Otherwise return a one-line human-readable diagnostic. The
    diagnostic intentionally mirrors `activate_workflow.py`'s error
    messages so the user sees the same vocabulary regardless of
    which validation layer caught it.
    """
    if not run_name:
        return "run_name must be a non-empty string"
    if len(run_name) > _MAX_RUN_NAME_LEN:
        return (
            f"run_name exceeds {_MAX_RUN_NAME_LEN} chars "
            f"(got {len(run_name)})"
        )
    if run_name.startswith("."):
        return "run_name may not start with '.'"
    bad = sorted(c for c in run_name if c in _DISALLOWED_CHARS)
    if bad:
        return (
            "run_name contains disallowed character(s): "
            f"{''.join(bad)!r} (none of /, \\, :, *, ?, <, >, |, \")"
        )
    return None


# -- Elicitation fallback (stdin) ---------------------------------------


def _elicit_run_name(workflow_id: str) -> str | None:
    """Prompt the user for a run_name via stdin.

    Returns the typed line (stripped) on success, or None when:
      - stdin is not a TTY (Claude Code bg / non-interactive),
      - the user enters an empty line,
      - the user sends EOF / KeyboardInterrupt.

    This is a best-effort fallback for the SPEC §13b
    `elicitation/create` step. True MCP elicitation from a subprocess
    is not yet supported; see module docstring.
    """
    if not sys.stdin.isatty():
        return None
    prompt = (
        f"Name for this {workflow_id} operon-session "
        "(snake_case, e.g. my_feature_refactor): "
    )
    try:
        line = input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None
    line = line.strip()
    return line or None


# -- Dispatch directive shape -------------------------------------------
#
# A single stdout line that the companion SKILL.md instructs the LLM
# to parse. Format is intentionally simple key=value pairs separated
# by single spaces, so a minimal regex on the SKILL side is enough.
# Neither workflow_id nor (post-validation) run_name can contain
# spaces or `=`, so the format is unambiguous.

_DISPATCH_PREFIX = "OPERON_DISPATCH"
_DISPATCH_TOOL = "mcp__operon__activate_workflow"


def _emit_dispatch(workflow_id: str, run_name: str) -> None:
    """Print the dispatch directive line consumed by the SKILL.md body."""
    print(
        f"{_DISPATCH_PREFIX} "
        f"tool={_DISPATCH_TOOL} "
        f"workflow_id={workflow_id} "
        f"run_name={run_name}"
    )


# -- Main ---------------------------------------------------------------


def main(argv: list[str]) -> int:
    """Entry point. Returns process exit code.

    Exit codes:
      0  -- validation passed, dispatch directive emitted
      1  -- user-facing error (invalid run_name, missing run_name on
            non-TTY stdin, etc.)
      2  -- argv shape error (missing workflow_id); should not happen
            in production because SKILL.md always passes the hardcoded
            workflow_id as argv[1].
    """
    if len(argv) < 2 or not argv[1].strip():
        print(
            "ERROR: activate.py requires <workflow_id> as argv[1]. "
            "This script is invoked by per-workflow SKILL.md files; "
            "it should not be run by hand."
        )
        return 2
    workflow_id = argv[1].strip()

    # Claude Code may pass `$ARGUMENTS` as a single space-joined token
    # or as multiple tokens depending on shell quoting. Treat the first
    # non-empty token as the candidate run_name; ignore any trailing
    # tokens (the user typed extra noise).
    user_args = [a.strip() for a in argv[2:] if a.strip()]
    run_name: str | None = user_args[0] if user_args else None

    if run_name is None:
        run_name = _elicit_run_name(workflow_id)
        if run_name is None:
            # No TTY for fallback elicitation and no argument supplied.
            # Tell the user how to retry. Stays single-line so the LLM
            # can relay it verbatim per SKILL.md.
            print(
                f"ERROR: /{workflow_id} requires a run_name. Invoke as: "
                f"/{workflow_id} <run_name> (snake_case, filesystem-safe, "
                f"<={_MAX_RUN_NAME_LEN} chars)."
            )
            return 1

    diag = _validate_run_name(run_name)
    if diag is not None:
        print(f"ERROR: {diag}")
        return 1

    _emit_dispatch(workflow_id, run_name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
