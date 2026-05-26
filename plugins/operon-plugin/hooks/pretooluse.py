#!/usr/bin/env python3
"""PreToolUse hook entrypoint (Phase 6 fix + Phase 6 followup +
Land 5 WA1).

Replaces the prior `type: mcp_tool` hook wiring per SPEC §8 + §12.
The mcp_tool form had a chicken-and-egg: every tool call (including
calls TO operon's own MCP tools) fires PreToolUse, which routes via
MCP, which causes recursion / "MCP server not connected" errors
during the connection race. Hookify's reference plugin (Anthropic's
own example) uses `type: command` for exactly this reason.

Two responsibilities branch on `tool_name`:

  * `tool_name == "Agent"` -> WA1 transcript injection (Land 5, see
    v2.9 section 5.1). The hook reads operon-installed sidechain
    transcripts for the about-to-spawn teammate's `agentType` and
    prepends them to `tool_input.prompt` so the re-spawned teammate
    has first-person recall. The Agent branch BYPASSES the guardrail
    evaluation pipeline (guardrails and WA1 do not interact) and
    does NOT write to guardrail_log.jsonl. All failure modes are
    fail-open: a discovery / read / decode error logs to stderr and
    returns the input unchanged so the spawn proceeds normally.
  * Bash|Edit|Write|MultiEdit|NotebookEdit -> guardrail-rule
    evaluation, the original Phase 6 path.

This script implements TWO evaluation passes per the Phase 6
followup brief:

  1. FAIL-CLOSED hardcoded deny set (defense-in-depth). Small,
     curated, catastrophic-class patterns evaluated with stdlib
     `re` ONLY. Fires even when rules.yaml is missing, identity
     is unresolvable, or PyYAML is unavailable. This is the
     unconditional safety gate.

  2. FULL rules engine. Loads the merged 3-tier rules.yaml +
     workflow-embedded rules.yaml block, projects through (role,
     current_phase), runs `operon_mcp_server.rules._evaluate`.
     Handles warn / log / role-scoped / phase-scoped rules.
     Fails-open on rules-load errors (the fail-CLOSED pass above
     has already cleared the catastrophic-class patterns).

Both passes share the same audit-row schema and write to
`guardrail_log.jsonl`. The hardcoded set's `rule_id` mirrors the
corresponding entry in `plugins/operon-plugin/rules.yaml` so a
single source-of-truth grep links them. If `rules.yaml` is hand-
edited to disable a rule that's also in the hardcoded set, the
hardcoded copy STILL FIRES -- this is the intended defense-in-
depth: a deliberate `disabled_rules` entry cannot turn off the
hardcoded safety net.

Hook input shape (per Claude Code hooks-reference):
    {
      "session_id": "...",
      "hook_event_name": "PreToolUse",
      "tool_name": "Bash",
      "tool_input": {"command": "..."}
    }

Output shape (per Claude Code hooks-reference):
    {
      "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" | "deny" | "ask",
        "permissionDecisionReason": "<message>"
      }
    }

Identity:
- `OPERON_AGENT_HANDLE` env -> `_handles/<handle>.json` for
  agent_name + role
- `phase_state.json` for current_phase
Both are leaf-tier reads; no MCP calls.

Cross-platform per SPEC §2: pathlib, encoding="utf-8", no
platform-gated APIs.

PYTHONPATH expectation: the companion `pretooluse-wrapper`
(bash/cmd) prepends `${CLAUDE_PLUGIN_ROOT}/src` so this script can
`import operon_mcp_server.rules` without `pip install -e .`. The
wrapper also resolves a python with the runtime deps (mcp,
watchdog, yaml) -- only `yaml` is actually needed by this hook
path, but the dep set is the same as the MCP server's so a single
ladder covers both.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

# `operon_mcp_server.identity` reads OPERON_AGENT_HANDLE + the
# handle file; no MCP imports.
from operon_mcp_server import bootstrap, identity, paths, rules, workflow


# ===========================================================================
# FAIL-CLOSED HARDCODED DENY SET
# ===========================================================================
# Catastrophic-class deny patterns that MUST fire even when:
#   - rules.yaml is missing / unreadable / malformed
#   - PyYAML import fails (shouldn't given our wrapper, but defense-in-depth)
#   - identity is unresolvable (no OPERON_AGENT_HANDLE, no handle file)
#   - phase_state.json is corrupt
#   - any other failure of the full rules engine
#
# These run BEFORE `rules.load_merged_rules` / `_evaluate`, evaluated with
# stdlib `re` only -- no operon-package imports beyond what's already at the
# top of this file. The full rules engine (which lives in rules.yaml and
# adds role/phase projection) handles everything else; this top set is the
# safety net.
#
# To keep both code paths in sync: each entry's `rule_id` MUST match the
# corresponding entry in `plugins/operon-plugin/rules.yaml`. The same
# pattern + message live in both places. Defense-in-depth: if the rules.yaml
# definition gets disabled via `disabled_rules`, this hardcoded copy still
# fires.
#
# Adding a new entry: this set should stay small (<5) and reserved for
# patterns whose violation is unrecoverable. Lower-stakes rules belong in
# rules.yaml ONLY, not duplicated here.
# ===========================================================================

_FAILCLOSED_DENY: list[dict[str, Any]] = [
    {
        "rule_id": "no_rm_rf",
        "tools": ("Bash",),
        "field": "command",
        # Mirror of `plugins/operon-plugin/rules.yaml` `no_rm_rf` detect.
        # If you change the regex here, grep rules.yaml for "no_rm_rf"
        # and update both.
        "pattern": re.compile(r"rm\s+-rf\s+/"),
        "message": (
            "rm -rf on an absolute path can irreversibly destroy data. "
            "(operon-plugin fail-closed safety gate)"
        ),
    },
]

#: Optional verbose-logging env var, mirrors the MCP server's
#: OPERON_DEBUG. When set to a truthy value, hook diagnostics land
#: on stderr (which Claude Code captures into the transcript).
_DEBUG_ENV = "OPERON_DEBUG"


def _maybe_enable_debug() -> None:
    flag = os.environ.get(_DEBUG_ENV, "").strip().lower()
    if flag in {"", "0", "false", "no"}:
        return
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG,
        format="[pretooluse] %(levelname)s: %(message)s",
    )


_log = logging.getLogger(__name__)


def _allow_output() -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


def _deny_output(message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message or "Blocked by operon-plugin rule.",
        }
    }


def _deny_with_ack_hint(rule_id: str, rule_message: str) -> dict[str, Any]:
    """Phase 7: warn-tier rules emit DENY with a hint to call
    acknowledge_warning. Never emits `ask` -- Claude Code's native
    permission prompt is bypassable by permission mode and out of
    our control; we want a closed-loop signal to the LLM."""
    msg = (
        f"The user chose to block this action for this reason: "
        f"{rule_message.strip()}\n\n"
        f"If you still believe this is the appropriate action, you can call "
        f'mcp__operon__acknowledge_warning(rule_id="{rule_id}", '
        f'reason="<explain why>") to proceed, then retry the tool.'
    )
    return _deny_output(msg)


def _deny_with_override_hint(rule_id: str, rule_message: str) -> dict[str, Any]:
    """Phase 7: deny-tier rules emit DENY with a hint to call
    request_override. The override is a user-gated escape hatch
    (elicitation/create dialog); the LLM cannot self-grant."""
    msg = (
        f"The user chose to block this action for this reason: "
        f"{rule_message.strip()}\n\n"
        f"If you still believe this is the appropriate action, you can call "
        f'mcp__operon__request_override(rule_id="{rule_id}", '
        f'reason="<explain why>") to ask the user to approve it, then retry '
        f"the tool."
    )
    return _deny_output(msg)


def _failclosed_deny_check(
    tool_name: str, tool_input: dict[str, Any]
) -> tuple[str, str] | None:
    """Run the hardcoded fail-closed deny patterns.

    Returns `(rule_id, message)` if a pattern matches, else None.
    Pure stdlib (`re` only); does not import rules.yaml or invoke
    the full rules engine. Safe to call even when the rest of the
    operon environment is broken.
    """
    for entry in _FAILCLOSED_DENY:
        if tool_name not in entry["tools"]:
            continue
        field = entry["field"]
        val = tool_input.get(field) if isinstance(tool_input, dict) else None
        if not isinstance(val, str):
            continue
        if entry["pattern"].search(val):
            return entry["rule_id"], entry["message"]
    return None


def _try_log_failclosed(
    rule_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    agent_name: str | None,
    role: str | None,
    current_phase: str | None,
    message: str,
    outcome: str = "blocked",
) -> None:
    """Best-effort audit-row write for a fail-closed event.

    `outcome` defaults to "blocked" (the deny path). The override-
    consumed branch passes `outcome="overridden"` so the audit row
    correctly reflects that the LLM was allowed to proceed. Phase 7
    bug fix: this was previously hardcoded to "blocked", which caused
    the failclosed override path to emit allow on the wire but record
    a blocked audit row -- exactly the symptom Boaz hit.

    All errors are suppressed so a broken audit-log path doesn't
    block the deny itself.
    """
    try:
        rules.append_log_event(
            rules.build_log_event(
                event_type="rule_fired_log",
                outcome=outcome,
                rule_id=rule_id,
                agent=agent_name,
                role=role,
                current_phase=current_phase,
                tool_name=tool_name,
                tool_input=tool_input,
                enforcement="deny",
                message=f"[failclosed] {message}",
            )
        )
    except Exception as exc:
        _log.warning("failclosed audit append skipped: %s", exc)


#: Hook-input ``agent_type`` values that we treat as lead-side and
#: therefore IGNORE for the role override. CC 2.1.150 was observed
#: to omit ``agent_type`` entirely on lead-originated tool calls
#: (the empirical signal Land 8 relies on), but we defensively
#: filter ``"team-lead"`` in case a future CC version starts
#: sending the team-config-default value for lead calls. The
#: bootstrap-resolved role from ``_handles/<handle>.json``
#: (``coordinator``) is the correct answer in that case.
_HOOK_AGENT_TYPE_IGNORE = frozenset({"team-lead"})


def _resolve_identity(
    hook_input: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Return (agent_name, role, current_phase).

    Land 8 (2026-05-23 dispatch + Boaz's empirical probe of
    /tmp/operon-role-check/.operon-pretooluse-probe.log): CC
    2.1.150's PreToolUse hook propagates the originating
    teammate's spawn-time ``subagent_type`` as ``agent_type`` in
    the hook input. The field is present + non-empty on
    teammate-originated tool calls (e.g. ``"implementer"``,
    ``"composability"``) and ABSENT on lead-originated calls.
    Probe data:

      LEAD     tool_name=Agent  agent_type=<MISSING>
      TEAMMATE tool_name=Write  agent_type="implementer"

    Both calls multiplex through the same MCP transport so
    session_id / transcript_path are lead-uniform (the B.0 wall).
    ``agent_type`` is the per-call signal we never inspected
    before this probe.

    Resolution order:

      1. If ``hook_input["agent_type"]`` is a non-empty string and
         is not in :data:`_HOOK_AGENT_TYPE_IGNORE`, use it as the
         resolved role (this scopes guardrail rules to the
         originating teammate, not the lead).
      2. Else fall back to the bootstrap-discovered Coordinator
         handle's ``role`` field (the existing Land-4-regression
         fix path -- correctly resolves to ``"coordinator"`` for
         the lead).

    ``agent_name`` and ``current_phase`` still come from the
    bootstrap handle + phase_state.json. We deliberately do NOT
    override ``agent_name`` here: operon's singleton-MCP identity
    IS the lead's Coordinator handle. The audit-log row keeps
    that fact (``agent: "Coordinator"``) but the ``role`` field
    correctly reflects the originating teammate so rule
    projections fire on the right scope.

    All-None fallbacks (no handle, no hook_input override) are
    safe: role/phase-scoped rules simply don't match when None.
    """
    role_override: str | None = None
    if isinstance(hook_input, dict):
        candidate = hook_input.get("agent_type")
        if (
            isinstance(candidate, str)
            and candidate
            and candidate not in _HOOK_AGENT_TYPE_IGNORE
        ):
            role_override = candidate

    current_phase: str | None = None
    try:
        state = workflow.read_phase_state()
        cp = state.get("current_phase")
        if isinstance(cp, str) and cp:
            current_phase = cp
    except workflow.WorkflowError:
        current_phase = None

    handle = identity.read_env_handle()
    if handle is None:
        # No bootstrap-resolved handle. Still surface the
        # hook-input role override so rule projections can scope
        # correctly even without operon-side identity binding.
        return None, role_override, current_phase
    try:
        record = identity.read_handle_file(handle)
    except identity.IdentityError as exc:
        _log.warning("identity read failed: %s", exc)
        return None, role_override, current_phase
    if record is None:
        return None, role_override, current_phase
    name = record.get("agent_name")
    bootstrap_role = record.get("role")

    if role_override is not None:
        resolved_role: str | None = role_override
    elif isinstance(bootstrap_role, str) and bootstrap_role:
        resolved_role = bootstrap_role
    else:
        resolved_role = None

    return (
        name if isinstance(name, str) and name else None,
        resolved_role,
        current_phase,
    )


def _load_active_workflow_manifest():
    """Return (manifest_dict, source_path) for the active run's
    workflow YAML, or (None, None) if no workflow is active.
    Used to layer workflow-embedded rules on top of 3-tier rules.yaml.
    """
    try:
        state = workflow.read_phase_state()
        wid = state.get("workflow_id")
        if not isinstance(wid, str) or not wid:
            return None, None
        decl = workflow.load_workflow(wid)
    except workflow.WorkflowError:
        return None, None
    try:
        import yaml as _yaml

        data = _yaml.safe_load(decl.source_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    return data, decl.source_path


def _session_owns_active_run(hook_input: dict[str, Any] | None) -> bool:
    """Return True iff the calling session owns the active run.

    The hook input carries the real Claude Code `session_id` (unlike the
    MCP server, which only ever sees a synthesized bootstrap id). We
    compare it to the `owner_session_id` stamped on the run's state.json
    at activate / restore time. Fails toward "not owner" on any error so
    the workflow tier is dropped rather than misapplied.
    """
    call_session_id = None
    if isinstance(hook_input, dict):
        call_session_id = hook_input.get("session_id")
    try:
        return workflow.session_owns_active_run(call_session_id)
    except Exception:  # noqa: BLE001 -- ownership check never blocks
        return False


def _emit(payload: dict[str, Any]) -> None:
    """Write the hook decision JSON to stdout and exit 0."""
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    raise SystemExit(0)


# ===========================================================================
# ROLLED BACK: Land 7 amendment 1 reject-teammate-identity-MCP branch
# ===========================================================================
# The Land 7 amendment 1 (commit 773cfbf) added a PreToolUse branch that
# compared the hook's session_id to the team's leadSessionId to deny
# teammate-originated calls to operon's identity MCP tools (whoami /
# get_agent_info / get_applicable_rules). Boaz's 2026-05-22
# /tmp/operon-land7-test demo showed the deny never fires: under
# in-process Agent Teams, ALL teammate MCP calls multiplex through the
# lead's single stdio MCP transport, so PreToolUse always sees the
# lead's session_id regardless of which teammate originated the call.
# Same architectural wall as the B.0 probe surfaced for MCP `_meta`:
# Anthropic's runtime does not differentiate teammate-originated from
# lead-originated at the MCP layer. The PreToolUse hook inherits the
# same limitation.
#
# The amendment-1 branch + helpers are removed in this commit. The
# hooks.json matcher is also reverted to its pre-amendment-1 shape.
# Static positive guidance (amendment 2 -- ~/.claude/agents/<role>.md
# footer) and the simplified dynamic WA1 directive (amendment 3) are
# the surfaces that REMAIN for steering teammates away from MCP
# identity tools; they remind the LLM to use [OPERON_QUERY] via
# SendMessage. The enforcement story is "the LLM follows the
# guidance" rather than "the hook blocks the wrong path", which is
# a softer guarantee but the strongest the platform supports today.
# Future work: if Anthropic exposes a per-caller signal at the
# PreToolUse layer, the reject branch can be reinstated with that
# signal as the discriminator instead of session_id.
# ===========================================================================


# ===========================================================================
# Land 5: WA1 transcript injection for the `Agent` tool
# ===========================================================================
# Per v2.9 section 5.1: on every Agent tool call the hook prepends the
# target teammate's prior sidechain transcripts (mtime-ascending) to
# `tool_input.prompt`. Fresh spawns (no prior transcripts) return the
# input unchanged. All errors fail open -- the spawn must work even if
# transcript discovery fails. Guardrail-log is NOT written from this
# path (the audit log is for the deny/warn/log evaluation pipeline,
# not for WA1).
# ===========================================================================


def _wa1_cwd_mangled() -> str:
    """Project-dir name for the current cwd per Claude Code convention.

    Claude Code derives the `~/.claude/projects/<name>` directory by
    replacing path separators in the absolute cwd with `-`. Verified
    empirically on POSIX against
    `~/.claude/projects/-tmp-operon-land4-test/` (Land 4 demo,
    2026-05-21).

    Cross-platform per SPEC section 2: we replace `\\`, `/`, and `:`
    so a Windows cwd like `C:\\Users\\me\\proj` mangles to
    `C--Users-me-proj` instead of being left unchanged. On POSIX the
    `\\` and `:` replacements are no-ops (a resolved POSIX path holds
    neither), so this is byte-identical to the prior `/`-only form on
    the tested platform.
    """
    from pathlib import Path

    abs_cwd = str(Path.cwd().resolve())
    return abs_cwd.replace("\\", "-").replace("/", "-").replace(":", "-")


def _wa1_discover_transcripts(agent_type: str) -> list:
    """Return absolute paths of `agent-<hash>.jsonl` transcripts whose
    sibling `agent-<hash>.meta.json` has `agentType == agent_type`,
    sorted by file mtime ascending.

    Walks `~/.claude/projects/<cwd-mangled>/*/subagents/` across all
    parent-session directories so a teammate that participated in
    multiple lead sessions has its transcripts unioned and
    temporally ordered. Returns `[]` on any filesystem / parse
    failure (fail-open).
    """
    from pathlib import Path

    project_dir = Path.home() / ".claude" / "projects" / _wa1_cwd_mangled()
    if not project_dir.is_dir():
        return []
    matches: list = []
    try:
        meta_iter = project_dir.glob("*/subagents/agent-*.meta.json")
    except OSError:
        return []
    for meta_path in meta_iter:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        if meta.get("agentType") != agent_type:
            continue
        jsonl_path = meta_path.parent / (
            meta_path.name[: -len(".meta.json")] + ".jsonl"
        )
        if not jsonl_path.is_file():
            continue
        try:
            mtime = jsonl_path.stat().st_mtime
        except OSError:
            continue
        matches.append((mtime, jsonl_path))
    matches.sort(key=lambda t: t[0])
    return [str(p) for _mt, p in matches]


def _wa1_build_identity_directive(name: str) -> str:
    """Build the ``[OPERON IDENTITY]`` directive prepended to every
    Agent spawn's first turn.

    Land 7 amendment 3 (2026-05-22): the verbose query-protocol
    instructions previously embedded here moved into the static
    ``~/.claude/agents/<role>.md`` definition (see
    ``subagent_install._OPERON_CONTEXT_QUERIES_FOOTER``). The
    static MD path is more reliable than this dynamic hook
    injection because the MD is loaded as part of the role's
    identity at every spawn whether or not the WA1 hook fires.
    The directive itself now only carries the per-spawn dynamic
    info (the teammate's ``name``); the rest lives in the role
    definition.
    """
    return (
        f'[OPERON IDENTITY] Your operon team-member name is "{name}". '
        "See your role definition for how to query operon for context."
    )


def _wa1_inject_transcripts(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Return an updated `tool_input` dict with the operon identity
    directive (Land 6) and prior sidechain transcripts (Land 5 WA1)
    prepended to the prompt.

    Order of prepended blocks (when each is present):
      1. ``[OPERON IDENTITY] ...`` -- always emitted when ``name``
         resolves from ``tool_input``. Even on a fresh spawn (no
         transcripts) the directive still fires; that is what
         distinguishes teammate vs lead on subsequent mcp calls.
      2. ``[PRIOR SESSION TRANSCRIPTS -- restored by operon WA1]``
         + concatenated JSONL -- only when prior transcripts exist
         for the teammate's ``agentType`` (Land 5 unchanged).
      3. ``---`` separator -- only when at least one of the blocks
         above was prepended.
      4. The original ``tool_input.prompt``.

    Fail-open: every failure path returns the input unchanged (or
    with whatever blocks were resolvable) so the spawn proceeds.
    """
    if not isinstance(tool_input, dict):
        sys.stderr.write("[pretooluse][wa1] tool_input not a dict; fail-open\n")
        return tool_input

    # The runtime's Agent tool input shape is documented as carrying
    # `subagent_type`, `name`, `team_name`, `prompt`. We resolve the
    # teammate's agentType from `subagent_type` (the lookup key for
    # Anthropic's subagent definition; same value our
    # subagent_install module installs under `<role>.md`). Fall back
    # to `name` if subagent_type is absent for any reason.
    agent_type = tool_input.get("subagent_type")
    if not isinstance(agent_type, str) or not agent_type:
        agent_type = tool_input.get("name")
    if not isinstance(agent_type, str) or not agent_type:
        sys.stderr.write(
            "[pretooluse][wa1] no subagent_type/name in tool_input; fail-open\n"
        )
        return tool_input

    # Land 6: the OPERON IDENTITY directive uses the runtime's
    # ``name`` field directly (the teammate's roster slot in the team
    # config's members[]). The transcript-discovery agent_type above
    # may equal it, but the two resolutions are independent.
    teammate_name = tool_input.get("name")
    if not isinstance(teammate_name, str) or not teammate_name:
        teammate_name = None

    original_prompt = tool_input.get("prompt")
    if not isinstance(original_prompt, str):
        original_prompt = ""

    try:
        transcripts = _wa1_discover_transcripts(agent_type)
    except Exception as exc:  # noqa: BLE001 -- fail-open on any error
        sys.stderr.write(f"[pretooluse][wa1] discovery error: {exc!r}; fail-open\n")
        transcripts = []

    # Concatenate raw JSONL contents in mtime-ascending order
    # (matches v2.9 section 5.1 step 3: "concatenate the contents in
    # that order").
    parts: list[str] = []
    for path_str in transcripts:
        try:
            with open(path_str, encoding="utf-8") as fp:
                parts.append(fp.read())
        except OSError as exc:
            sys.stderr.write(
                f"[pretooluse][wa1] failed to read {path_str}: {exc}; "
                f"skipping that transcript\n"
            )
            continue

    # Compose. Both blocks are independently optional; ``---``
    # separator + the original prompt are emitted only when at least
    # one prepended block is present.
    blocks: list[str] = []
    if teammate_name:
        blocks.append(_wa1_build_identity_directive(teammate_name))
    if parts:
        concatenated = "\n".join(parts)
        blocks.append(
            f"[PRIOR SESSION TRANSCRIPTS -- restored by operon WA1]\n\n{concatenated}"
        )

    if not blocks:
        # Nothing to inject. Log a diagnostic so the absence is
        # visible in the operon stderr capture.
        if not parts:
            sys.stderr.write(
                f"[pretooluse][wa1] no prior transcripts for agentType="
                f"{agent_type!r}; fresh spawn, no identity name -- "
                f"no mutation\n"
            )
        return tool_input

    mutated_prompt = "\n\n".join(blocks) + "\n\n---\n\n" + original_prompt
    new_input = dict(tool_input)
    new_input["prompt"] = mutated_prompt
    sys.stderr.write(
        f"[pretooluse][wa1] injected identity_directive={bool(teammate_name)} "
        f"transcripts={len(parts)} "
        f"({sum(len(p) for p in parts)} bytes) for agentType={agent_type!r}\n"
    )
    return new_input


def _wa1_output(updated_input: dict[str, Any]) -> dict[str, Any]:
    """Build the PreToolUse hook response that mutates tool input.

    Per Claude Code hooks-reference: returning
    `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
    "updatedInput": <dict>}}` replaces the tool input the runtime
    feeds to the model's spawn. No `permissionDecision` field --
    WA1 never blocks; it only mutates.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated_input,
        }
    }


def main() -> None:
    _maybe_enable_debug()

    # Land 4 regression fix (2026-05-22): the hook is a SEPARATE
    # subprocess that Claude Code spawns from the lead's CC process
    # for every PreToolUse event. Pre-Land-4 multi-process MCP let
    # the hook inherit OPERON_AGENT_HANDLE from each spawned
    # worker's subprocess env; Land 4's collapse to singleton-MCP
    # closed that path for the lead (the lead's CC env does NOT
    # carry OPERON_AGENT_HANDLE; only the lead's MCP subprocess
    # sets it via bootstrap's in-process `_CACHED_HANDLE`, which
    # is NOT visible from the hook subprocess). Empirically:
    # /tmp/operon-manual-test/.operon/default/guardrail_log.jsonl
    # rule_fired_log rows showed `agent: null, role: null` for
    # lead-issued Bash calls while ack_issued rows in the same
    # operon-session showed `agent: "Coordinator",
    # role: "coordinator"` (because the ack tool runs IN the MCP
    # subprocess, with the cache populated).
    #
    # Fix: at hook startup, discover the Coordinator's handle from
    # disk and pin it in this subprocess's process-local cache so
    # the existing `identity.read_env_handle()` call sites below
    # resolve correctly. The discovery is read-only (never bootstraps
    # a fresh operon-session from the hook -- creating .operon/
    # subtrees as a side-effect of a tool call would be surprising).
    # On no-active-operon-session, the cache stays None and all
    # downstream identity resolution returns None gracefully (same
    # as today; this fix only restores behavior for the populated
    # case).
    try:
        discovered = bootstrap.discover_coordinator_handle()
    except Exception as exc:  # noqa: BLE001 -- discovery must never crash hook
        _log.warning("hook bootstrap discovery failed: %s; continuing", exc)
        discovered = None
    if discovered is not None:
        identity._set_cached_handle(discovered)

    raw = sys.stdin.read()
    if not raw.strip():
        _log.debug("empty stdin; fail-open")
        _emit(_allow_output())
        return  # unreachable; SystemExit above

    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning("stdin not JSON (%s); fail-open", exc)
        _emit(_allow_output())
        return

    tool_name = hook_input.get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name:
        _log.debug("no tool_name in hook input; fail-open")
        _emit(_allow_output())
        return

    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        # Some hook payloads stringify nested objects.
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
                if not isinstance(tool_input, dict):
                    tool_input = {}
            except json.JSONDecodeError:
                tool_input = {}
        else:
            tool_input = {}

    # ---- LAND 5: WA1 TRANSCRIPT INJECTION FOR THE Agent TOOL --------
    # Branch BEFORE the guardrail evaluation pipeline. Agent calls are
    # not subject to operon's guardrail rules (the rules target
    # side-effectful tools like Bash/Edit/Write). WA1 mutates
    # tool_input.prompt to prepend prior sidechain transcripts so a
    # re-spawned teammate has first-person recall; on fail-open paths
    # the input is returned unchanged and the spawn proceeds normally.
    # No audit log row is written here (guardrail_log.jsonl is for the
    # deny/warn/log pipeline, not for WA1).
    if tool_name == "Agent":
        try:
            updated = _wa1_inject_transcripts(tool_input)
        except Exception as exc:  # noqa: BLE001 -- fail-open on any error
            sys.stderr.write(f"[pretooluse][wa1] fatal in inject: {exc!r}; fail-open\n")
            updated = tool_input
        _emit(_wa1_output(updated))
        return

    # Land 7 amendment-1 reject-teammate-MCP branch was rolled back
    # here (2026-05-22). See the rollback rationale block higher in
    # this file: in-process teammates multiplex MCP through the
    # lead's session, so session_id is lead-uniform at the hook
    # layer; the deny path never fired. The static MD footer
    # installed by subagent_install.py and the WA1 directive
    # injected at spawn-time both steer the teammate's LLM toward
    # the [OPERON_QUERY] inbox channel without needing a hook-side
    # block.

    # ---- FAIL-CLOSED PASS ------------------------------------------
    # Hardcoded safety patterns evaluated BEFORE the full rules engine.
    # These fire even when rules.yaml is missing / identity is
    # unresolvable / phase_state is corrupt -- the defense-in-depth
    # gate for catastrophic-class actions. Stdlib `re` only.
    failclosed = _failclosed_deny_check(tool_name, tool_input)

    # Resolve identity / phase NEXT, but only for audit-log enrichment
    # of the failclosed deny (if it fires) and for the full rules
    # engine path below. Identity resolution failure does NOT bypass
    # the deny.
    #
    # Land 8: pass hook_input through so `_resolve_identity` can
    # apply the CC 2.1.150 ``agent_type``-based role override when
    # a teammate originates the call.
    agent_name, role, current_phase = _resolve_identity(hook_input)
    _log.debug(
        "tool=%s role=%r phase=%r agent=%r failclosed=%s",
        tool_name,
        role,
        current_phase,
        agent_name,
        bool(failclosed),
    )

    if failclosed is not None:
        rule_id, message = failclosed
        # Phase 7: a Coordinator-approved override token (one-shot)
        # bypasses fail-closed deny. Audit row tagged
        # `overridden_failclosed` so the lineage is clear in the
        # JSONL.
        handle = identity.read_env_handle()
        if handle:
            try:
                token = rules.find_active_token(
                    kind="override",
                    rule_id=rule_id,
                    agent_handle=handle,
                )
            except Exception as exc:
                _log.warning("failclosed token lookup error: %s", exc)
                token = None
            if token is not None:
                consumed = rules.consume_token(token)
                if consumed:
                    _try_log_failclosed(
                        rule_id=rule_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        agent_name=agent_name,
                        role=role,
                        current_phase=current_phase,
                        message=(
                            f"overridden_failclosed: {message} "
                            f"(token reason: {token.reason})"
                        ),
                        # Phase 7 bug fix: was previously omitted
                        # which defaulted to "blocked" (the cause of
                        # Boaz's audit-trail discrepancy). Override
                        # was honored on the wire (allow emitted,
                        # token consumed) but the audit row said
                        # blocked. Now correctly tagged overridden.
                        outcome="overridden",
                    )
                    _emit(_allow_output())
                    return
        _try_log_failclosed(
            rule_id=rule_id,
            tool_name=tool_name,
            tool_input=tool_input,
            agent_name=agent_name,
            role=role,
            current_phase=current_phase,
            message=message,
        )
        _emit(_deny_with_override_hint(rule_id, message))
        return  # unreachable; SystemExit above

    # ---- FULL RULES ENGINE -----------------------------------------
    # The 3-tier rules.yaml + workflow-embedded path, with (role,
    # phase) projection. Fails-open on parse / load errors -- the
    # fail-CLOSED safety net above has already cleared the
    # catastrophic-class patterns by this point.
    workflow_manifest, workflow_source = _load_active_workflow_manifest()
    # Session-ownership gate. A run's workflow-embedded rules apply only
    # to the session that activated or resumed it. A different session
    # open in the same project -- e.g. a fresh session after the owner
    # quit, before it calls restore_operon_session -- must NOT inherit
    # the paused run's workflow rules. The 3-tier rules.yaml
    # (plugin/user/project) still applies to everyone; only the
    # workflow tier is dropped for a non-owner.
    if workflow_manifest is not None and not _session_owns_active_run(hook_input):
        workflow_manifest, workflow_source = None, None
    try:
        rule_list = rules.load_merged_rules(
            workflow_manifest=workflow_manifest,
            workflow_source=workflow_source,
        )
    except rules.RulesError as exc:
        _log.warning("rules load failed (%s); fail-open", exc)
        _emit(_allow_output())
        return

    decision = rules._evaluate(
        tool_name,
        tool_input,
        role=role,
        current_phase=current_phase,
        rules=rule_list,
    )

    # Build + write audit log row (best-effort; absent run-dir is OK).
    def _log_row(outcome: str, enforcement: str) -> None:
        try:
            rules.append_log_event(
                rules.build_log_event(
                    event_type="rule_fired_log",
                    outcome=outcome,
                    rule_id=decision.rule_id,
                    agent=agent_name,
                    role=role,
                    current_phase=current_phase,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    enforcement=enforcement,
                    message=decision.message,
                )
            )
        except (paths.OperonPathError, OSError) as exc:
            _log.warning("audit-log append failed: %s", exc)

    if decision.action == "log":
        _log_row("allowed", "log")
        _emit(_allow_output())
        return

    if decision.action == "deny":
        # Phase 7: check for an active one-shot override token. If
        # present, consume it (delete the file) and convert deny to
        # allow with `outcome=overridden` audit.
        handle = identity.read_env_handle()
        if handle and decision.rule_id:
            try:
                token = rules.find_active_token(
                    kind="override",
                    rule_id=decision.rule_id,
                    agent_handle=handle,
                )
            except Exception as exc:
                _log.warning("override token lookup error: %s", exc)
                token = None
            if token is not None and rules.consume_token(token):
                _log_row("overridden", "deny")
                _emit(_allow_output())
                return
        _log_row("blocked", "deny")
        _emit(
            _deny_with_override_hint(decision.rule_id or "<unknown>", decision.message)
        )
        return

    if decision.action == "warn":
        # Phase 7: check for an active ack token (60s TTL). If
        # present, convert warn-deny to allow with `outcome=acked`.
        # Ack tokens are NOT one-shot; they remain valid for their
        # TTL so multiple retries within the same turn don't need
        # repeat acks.
        handle = identity.read_env_handle()
        if handle and decision.rule_id:
            try:
                token = rules.find_active_token(
                    kind="ack",
                    rule_id=decision.rule_id,
                    agent_handle=handle,
                )
            except Exception as exc:
                _log.warning("ack token lookup error: %s", exc)
                token = None
            if token is not None:
                _log_row("acked", "warn")
                _emit(_allow_output())
                return
        _log_row("blocked", "warn")
        # Phase 7: warn rules emit DENY (not ask) with a hint to call
        # acknowledge_warning. Claude Code's native `ask` is bypassable
        # by permission mode; deny+ack-hint keeps the loop closed
        # to the LLM, not the user.
        _emit(_deny_with_ack_hint(decision.rule_id or "<unknown>", decision.message))
        return

    # action == "allow" (no rule matched)
    _emit(_allow_output())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        # Last-ditch fail-open. Hook errors are non-blocking by Claude
        # Code's contract anyway; we'd rather not block legitimate
        # tool calls when our hook itself crashes. Emit a diagnostic
        # to stderr so the failure is visible.
        sys.stderr.write(f"[pretooluse] fatal: {exc!r}\n")
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                    }
                }
            )
        )
        sys.exit(0)
