"""Guardrail Rules: parsing, matching, evaluation, and audit logging.

Per SPEC §12 (Guardrail Rules) + §7 (`evaluate` tool row) + §17
(`guardrail_log.jsonl` schema). Ports the matching semantics from
`claudechic/guardrails/rules.py` verbatim into a single file because
the operon scope is narrower (no `Injection` separate from `Rule`,
no `/rules` slash command surface yet -- those land later).

Architecture:

- `Rule` (frozen dataclass): parsed manifest entry. Fields mirror
  claudechic's existing schema so a user can hand-edit
  `<plugin>/rules.yaml` or a workflow's `rules:` section using the
  same conventions.
- `Decision` (frozen dataclass): output of `_evaluate`. Crosses the
  rules-engine -> tool seam.
- `parse_rules_file(path)`: loads a top-level `- id: ...` list of
  Rule entries OR a `{"rules": [...]}` wrapper (claudechic uses the
  latter inside workflow manifests; the standalone `rules.yaml`
  files use the bare list form). Both shapes are accepted.
- `load_merged_rules(workflow_root)`: layers plugin-tier
  `<plugin>/rules.yaml` + user-tier `~/.operon/rules.yaml` +
  project-tier `<project>/.operon/rules.yaml` + the active
  workflow's `rules:` section. Higher tier wins per rule id.
- `match_rule(rule, tool_name, tool_input, role, phase)`: returns
  True iff the rule should fire for this call.
- `_evaluate(tool_name, tool_input, role, phase, rules)`: PURE
  function. Iterates rules, applies match filter, picks the
  most-restrictive matching enforcement (deny > warn > log).
- `append_log_event(row)`: append-only JSONL writer to
  `<run-dir>/guardrail_log.jsonl`. Per SPEC §6.6 we open with
  `O_APPEND`; lines are <4 KiB so PIPE_BUF atomicity holds.
- `command_hash(rule_id, tool_name, tool_input)`: SHA-256 used by
  `request_override` / `acknowledge_warning` to filename tokens
  (Phase 7 work; the function lives here per SPEC §12 ownership).

Cross-platform per SPEC §2: pathlib, encoding="utf-8", os.O_APPEND
for the JSONL writer (same flag on Windows; the discipline of
keeping rows under 4 KiB is the actual atomicity mechanism).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import paths

_log = logging.getLogger(__name__)

#: Filename for the per-run guardrail audit log (SPEC §17).
GUARDRAIL_LOG_FILENAME = "guardrail_log.jsonl"

#: Filename of the standalone rules manifest at each tier.
RULES_FILENAME = "rules.yaml"

#: Enforcement levels in descending severity order. When multiple
#: rules match, `_evaluate` picks the one with the highest severity:
#: deny shadows warn shadows log.
_ENFORCEMENT_SEVERITY = {"deny": 3, "warn": 2, "log": 1}

#: Map enforcement -> Decision.action. Identity-mapped here; kept as
#: a constant so future split semantics (e.g. "deny" -> "deny" but
#: with `override_token_required: true`) don't drift the API surface.
_ENFORCEMENT_TO_ACTION = {"deny": "deny", "warn": "warn", "log": "log"}


class RulesError(RuntimeError):
    """Raised on rule-parsing / -loading failures."""


# -- Rule + Decision dataclasses ---------------------------------------


@dataclass(frozen=True)
class Rule:
    """Parsed manifest entry. Mirrors claudechic's `Rule` shape."""

    id: str
    trigger: tuple[str, ...]
    enforcement: str  # "deny" | "warn" | "log"
    detect_pattern: re.Pattern[str] | None = None
    detect_field: str = "command"
    exclude_pattern: re.Pattern[str] | None = None
    message: str = ""
    roles: tuple[str, ...] = ()
    exclude_roles: tuple[str, ...] = ()
    phases: tuple[str, ...] = ()
    exclude_phases: tuple[str, ...] = ()
    #: One of `plugin`, `user`, `project`, `workflow` for diagnostics.
    tier: str = "plugin"


@dataclass(frozen=True)
class Decision:
    """Output of `_evaluate`. Crosses the rules-engine -> tool seam.

    `action` values per SPEC §7 `evaluate` row:
    - `"allow"`: no rule matched, OR a `log` rule matched (`evaluate`
      writes the log event then returns allow so the tool proceeds).
    - `"warn"`: a `warn` rule matched; hook translates to
      `permissionDecision: "ask"`.
    - `"deny"`: a `deny` rule matched; hook translates to
      `permissionDecision: "deny"`.
    - `"log"`: ONLY used internally; `evaluate` reshapes to "allow"
      before returning.
    """

    action: str
    rule_id: str | None = None
    message: str = ""
    enforcement: str | None = None
    override_token_required: bool = False


# -- YAML parsing -------------------------------------------------------


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Normalize a YAML scalar/list to a tuple of strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return ()


def _parse_one_rule(entry: dict[str, Any], *, tier: str) -> Rule:
    """Parse one YAML entry into a `Rule`. Raises `RulesError` on shape errors."""
    rid = entry.get("id")
    if not isinstance(rid, str) or not rid:
        raise RulesError(f"rule entry missing non-empty 'id': {entry!r}")

    trigger = _as_tuple(entry.get("trigger"))
    if not trigger:
        raise RulesError(f"rule {rid!r}: missing 'trigger' (e.g. PreToolUse/Bash)")

    enforcement = entry.get("enforcement", "deny")
    if enforcement not in _ENFORCEMENT_SEVERITY:
        raise RulesError(
            f"rule {rid!r}: invalid enforcement {enforcement!r}; "
            f"must be one of {sorted(_ENFORCEMENT_SEVERITY)}"
        )

    detect = entry.get("detect") or {}
    detect_pattern: re.Pattern[str] | None = None
    detect_field = "command"
    if isinstance(detect, dict):
        pat = detect.get("pattern")
        if isinstance(pat, str) and pat:
            try:
                detect_pattern = re.compile(pat)
            except re.error as exc:
                raise RulesError(
                    f"rule {rid!r}: detect.pattern invalid regex: {exc}"
                ) from exc
        df = detect.get("field")
        if isinstance(df, str) and df:
            detect_field = df

    exclude_pattern: re.Pattern[str] | None = None
    excl = entry.get("exclude_if_matches")
    if isinstance(excl, str) and excl:
        try:
            exclude_pattern = re.compile(excl)
        except re.error as exc:
            raise RulesError(
                f"rule {rid!r}: exclude_if_matches invalid regex: {exc}"
            ) from exc

    return Rule(
        id=rid,
        trigger=trigger,
        enforcement=enforcement,
        detect_pattern=detect_pattern,
        detect_field=detect_field,
        exclude_pattern=exclude_pattern,
        message=str(entry.get("message", "")),
        roles=_as_tuple(entry.get("roles")),
        exclude_roles=_as_tuple(entry.get("exclude_roles")),
        phases=_as_tuple(entry.get("phases")),
        exclude_phases=_as_tuple(entry.get("exclude_phases")),
        tier=tier,
    )


def _coerce_rule_list(data: Any, source: Path) -> list[dict[str, Any]]:
    """Extract a rules list from either the wrapped or bare YAML shape.

    Standalone `rules.yaml` files use a bare top-level list. Workflow
    manifests embed rules under a `rules:` key. Accept both.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        rules = data.get("rules")
        if rules is None:
            return []
        if isinstance(rules, list):
            return rules
        raise RulesError(
            f"'rules' in '{source}' must be a list; got {type(rules).__name__}."
        )
    raise RulesError(
        f"'{source}' must be a YAML list or mapping at top level; got {type(data).__name__}."
    )


def parse_rules_file(path: Path, *, tier: str) -> list[Rule]:
    """Parse a rules-list YAML file. Returns [] if the file is absent."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesError(f"Failed to read rules file '{path}': {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesError(f"Failed to parse '{path}': {exc}") from exc
    raw_rules = _coerce_rule_list(data, path)
    rules: list[Rule] = []
    for entry in raw_rules:
        if not isinstance(entry, dict):
            raise RulesError(
                f"rule entry in '{path}' must be a mapping; got {type(entry).__name__}"
            )
        rules.append(_parse_one_rule(entry, tier=tier))
    return rules


def parse_workflow_rules(manifest: dict[str, Any], *, source: Path) -> list[Rule]:
    """Parse the `rules:` block of a workflow manifest, if present."""
    raw_rules = manifest.get("rules") if isinstance(manifest, dict) else None
    if raw_rules is None:
        return []
    if not isinstance(raw_rules, list):
        raise RulesError(
            f"Workflow manifest '{source}' 'rules:' must be a list; "
            f"got {type(raw_rules).__name__}."
        )
    rules: list[Rule] = []
    for entry in raw_rules:
        if not isinstance(entry, dict):
            raise RulesError(
                f"rule entry in '{source}' rules: must be a mapping; "
                f"got {type(entry).__name__}"
            )
        rules.append(_parse_one_rule(entry, tier="workflow"))
    return rules


# -- 3-tier merge -------------------------------------------------------


def _plugin_rules_path() -> Path | None:
    """Return `${CLAUDE_PLUGIN_ROOT}/rules.yaml` or None if env unset."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return None
    return Path(plugin_root) / RULES_FILENAME


def _user_rules_path() -> Path:
    return Path.home() / paths.OPERON_DIRNAME / RULES_FILENAME


def _project_rules_path() -> Path | None:
    try:
        return paths.project_root() / paths.OPERON_DIRNAME / RULES_FILENAME
    except paths.OperonPathError:
        return None


def load_merged_rules(
    workflow_manifest: dict[str, Any] | None = None,
    workflow_source: Path | None = None,
) -> list[Rule]:
    """Layer plugin + user + project + workflow rules. Higher tier wins per id.

    Returns the merged Rule list in evaluation order:

    1. Plugin tier (`${CLAUDE_PLUGIN_ROOT}/rules.yaml`)
    2. User tier (`~/.operon/rules.yaml`)
    3. Project tier (`<project>/.operon/rules.yaml`)
    4. Workflow-embedded rules (manifest `rules:` section)

    Rule ids that appear at multiple tiers are deduplicated by
    keeping the HIGHEST-tier definition (project beats user beats
    plugin; workflow is independent and additive -- it has its own
    id namespace by convention).

    Returns [] if none of the tiers has a rules manifest.
    """
    by_id: dict[str, Rule] = {}

    # Plugin tier
    plugin_path = _plugin_rules_path()
    if plugin_path is not None:
        for r in parse_rules_file(plugin_path, tier="plugin"):
            by_id[r.id] = r

    # User tier
    for r in parse_rules_file(_user_rules_path(), tier="user"):
        by_id[r.id] = r

    # Project tier
    project_path = _project_rules_path()
    if project_path is not None:
        for r in parse_rules_file(project_path, tier="project"):
            by_id[r.id] = r

    rules: list[Rule] = list(by_id.values())

    # Workflow-embedded rules (additive; independent id namespace).
    if workflow_manifest is not None:
        try:
            rules.extend(
                parse_workflow_rules(
                    workflow_manifest, source=workflow_source or Path("<workflow>")
                )
            )
        except RulesError as exc:
            _log.warning("workflow rules: skipped due to parse error: %s", exc)

    return rules


# -- matching + evaluation ----------------------------------------------


def _trigger_matches(rule: Rule, tool_name: str) -> bool:
    """Return True iff `rule.trigger` includes `tool_name` (post-`/`)."""
    for trig in rule.trigger:
        parts = trig.split("/", 1)
        if len(parts) == 2:
            if parts[1] == tool_name:
                return True
        elif parts[0] in {"PreToolUse"}:
            # Bare "PreToolUse" matches all tools (claudechic convention).
            return True
    return False


def _get_input_field(tool_input: dict[str, Any], field_name: str) -> str:
    """Pull a field from `tool_input` for pattern matching."""
    val = tool_input.get(field_name) if isinstance(tool_input, dict) else None
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False)


def _role_filter_skips(rule: Rule, role: str | None) -> bool:
    """Return True iff this rule should be skipped for `role`."""
    if rule.roles and (role is None or role not in rule.roles):
        return True
    if rule.exclude_roles and role and role in rule.exclude_roles:
        return True
    return False


def _phase_filter_skips(rule: Rule, current_phase: str | None) -> bool:
    """Return True iff this rule should be skipped for `current_phase`."""
    if not rule.phases and not rule.exclude_phases:
        return False
    if current_phase is None:
        return bool(rule.phases)
    if rule.phases and current_phase not in rule.phases:
        return True
    if rule.exclude_phases and current_phase in rule.exclude_phases:
        return True
    return False


def match_rule(
    rule: Rule,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    role: str | None,
    current_phase: str | None,
) -> bool:
    """Return True iff `rule` fires for this call.

    Pure function. The order mirrors claudechic's pipeline: trigger
    > role > phase > exclude > detect.
    """
    if not _trigger_matches(rule, tool_name):
        return False
    if _role_filter_skips(rule, role):
        return False
    if _phase_filter_skips(rule, current_phase):
        return False
    if rule.detect_pattern is None:
        # No detect pattern -> matches all tool inputs for this tool
        return True
    text = _get_input_field(tool_input, rule.detect_field)
    if rule.exclude_pattern is not None and rule.exclude_pattern.search(text):
        return False
    return bool(rule.detect_pattern.search(text))


def _evaluate(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    role: str | None,
    current_phase: str | None,
    rules: list[Rule],
) -> Decision:
    """PURE function. Iterate rules, return the most-restrictive match.

    "Most-restrictive" = highest `_ENFORCEMENT_SEVERITY`. Iteration
    order is the declaration order in the merged list; ties are
    broken by declaration order (first-wins).
    """
    best: Rule | None = None
    best_sev = 0
    for r in rules:
        if not match_rule(
            r,
            tool_name=tool_name,
            tool_input=tool_input,
            role=role,
            current_phase=current_phase,
        ):
            continue
        sev = _ENFORCEMENT_SEVERITY[r.enforcement]
        if sev > best_sev:
            best = r
            best_sev = sev
        # Short-circuit: deny is the maximum severity.
        if best_sev == _ENFORCEMENT_SEVERITY["deny"]:
            break

    if best is None:
        return Decision(action="allow")
    action = _ENFORCEMENT_TO_ACTION[best.enforcement]
    return Decision(
        action=action,
        rule_id=best.id,
        message=best.message,
        enforcement=best.enforcement,
        override_token_required=(best.enforcement == "deny"),
    )


# -- guardrail_log.jsonl writer -----------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def guardrail_log_path(start: Path | None = None) -> Path:
    """Return `<run-dir>/guardrail_log.jsonl`."""
    return paths.active_run_dir(start) / GUARDRAIL_LOG_FILENAME


def append_log_event(row: dict[str, Any], start: Path | None = None) -> None:
    """Append one JSONL event to `<run-dir>/guardrail_log.jsonl`.

    Per SPEC §6.6: O_APPEND + lines wrapped under 4 KiB. The discipline
    of small rows is what makes this safe -- O_APPEND alone does not
    guarantee atomicity across processes on every filesystem.
    """
    try:
        path = guardrail_log_path(start)
    except paths.OperonPathError as exc:
        # No active run -> nothing to log against; suppress so the
        # tool call's primary path is not derailed by audit-only
        # failure.
        _log.warning("guardrail log skipped (no active run): %s", exc)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    if len(line.encode("utf-8")) >= 4096:
        # SPEC §6.6 contract: rows must stay <4 KiB so PIPE_BUF
        # line-atomicity holds. Truncate the `message` field rather
        # than dropping the row entirely.
        truncated = dict(row)
        for k in ("message", "tool_input_summary"):
            if k in truncated and isinstance(truncated[k], str):
                truncated[k] = truncated[k][:512] + "..."
        line = json.dumps(truncated, ensure_ascii=False) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def build_log_event(
    *,
    event_type: str,
    outcome: str,
    rule_id: str | None,
    agent: str | None,
    role: str | None,
    current_phase: str | None,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    enforcement: str | None,
    message: str | None = None,
) -> dict[str, Any]:
    """Construct a `guardrail_log.jsonl` row per SPEC §17 schema."""
    tool_input_summary: str
    if tool_input is None:
        tool_input_summary = ""
    elif isinstance(tool_input, dict):
        # Prefer the `command` field (Bash fast path) if present;
        # else canonical JSON.
        cmd = tool_input.get("command")
        if isinstance(cmd, str) and cmd:
            tool_input_summary = cmd[:300]
        else:
            try:
                tool_input_summary = json.dumps(
                    tool_input, sort_keys=True, ensure_ascii=False
                )[:300]
            except (TypeError, ValueError):
                tool_input_summary = str(tool_input)[:300]
    else:
        tool_input_summary = str(tool_input)[:300]

    row: dict[str, Any] = {
        "timestamp": _now_iso(),
        "type": event_type,
        "outcome": outcome,
        "rule_id": rule_id,
        "agent": agent,
        "role": role,
        "current_phase": current_phase,
        "tool_name": tool_name,
        "tool_input_summary": tool_input_summary,
        "enforcement": enforcement,
    }
    if message:
        row["message"] = message
    return row


# -- command_hash (Phase 7 will use; lives here per SPEC §12 ownership) -


def _extract_command(tool_input: dict[str, Any]) -> str:
    """Extract the `command` representation for hashing (SPEC §12)."""
    if not isinstance(tool_input, dict):
        return str(tool_input)
    cmd = tool_input.get("command")
    if isinstance(cmd, str) and cmd:
        return cmd
    try:
        return json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(sorted(tool_input.items()))


def command_hash(rule_id: str, tool_name: str, tool_input: dict[str, Any]) -> str:
    """Compute SHA-256 hex per SPEC §12 command-hash algorithm.

    Used as the filename for `overrides/<command_hash>.json` (Phase 7)
    and `acks/<command_hash>.json`. Identical inputs produce identical
    filenames so the requesting and consuming sides agree.
    """
    cmd = _extract_command(tool_input)
    digest = hashlib.sha256(
        f"{rule_id}:{tool_name}:{cmd}".encode("utf-8")
    ).hexdigest()
    return digest


# ===========================================================================
# Phase 7: ack + override token files
# ===========================================================================
#
# Tokens authorize the LLM to bypass a rule on its next retry. Two kinds:
#
#   - ACK tokens (warn rules): written by `acknowledge_warning`. Per-handle,
#     per-rule, with a TTL (default 60s) so a stale ack from a prior turn
#     doesn't accidentally unblock a future warn fire.
#   - OVERRIDE tokens (deny rules): written by `request_override` after the
#     user accepts the elicitation. One-shot -- consumed on first use; the
#     hook deletes the file after honoring it.
#
# Path layout under the active run-dir:
#
#   acks/<agent_handle>/<rule_id>-<uuid4>.json
#   overrides/<agent_handle>/<rule_id>-<uuid4>.json
#
# Per-handle subdirs keep one Agent's tokens from accidentally matching
# another's. Filename includes the rule_id so the hook's lookup is a cheap
# `glob(rule_id-*.json)`.
#
# Token JSON shape (frozen `Token` dataclass below; same shape for both
# kinds, distinguished by directory):
#
#   {
#     "rule_id": "<id>",
#     "agent_handle": "<uuid>",
#     "kind": "ack" | "override",
#     "reason": "<llm-supplied explanation>",
#     "issued_at": "<iso8601>",
#     "expires_at": "<iso8601>" or null,
#     "one_shot": false | true
#   }
#
# Lifecycle:
#   ack tokens: issued with TTL; valid until expires_at; deleted lazily on
#     read after expiration.
#   override tokens: issued one-shot (no TTL by default); consumed by the
#     hook on first match (file unlinked atomically).

ACKS_DIRNAME = "acks"
OVERRIDES_DIRNAME = "overrides"

#: Default TTL for ack tokens. Long enough for the LLM to call
#: acknowledge_warning + retry the gated tool call within the same turn;
#: short enough that a stale ack doesn't bypass a warn fire in a later
#: turn.
ACK_TOKEN_TTL_SECONDS = 60


@dataclass(frozen=True)
class Token:
    """Materialized token from disk. Read-only; mutation goes through
    the writer / consumer helpers below."""

    rule_id: str
    agent_handle: str
    kind: str  # "ack" or "override"
    reason: str
    issued_at: str
    expires_at: str | None
    one_shot: bool
    path: Path  # source file, for consumption


def _acks_dir(agent_handle: str, start: Path | None = None) -> Path:
    return paths.active_run_dir(start) / ACKS_DIRNAME / agent_handle


def _overrides_dir(agent_handle: str, start: Path | None = None) -> Path:
    return paths.active_run_dir(start) / OVERRIDES_DIRNAME / agent_handle


def _parse_iso(ts: str | None):
    """Parse an ISO-8601 timestamp from token JSON. Returns None on any
    parse error -- callers treat None as 'no expiry'."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _token_is_expired(token: Token, now=None) -> bool:
    if token.expires_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    exp = _parse_iso(token.expires_at)
    if exp is None:
        return False
    return now >= exp


def _read_token(path: Path) -> Token | None:
    """Read a token JSON file. Returns None on missing / unparseable."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    rid = data.get("rule_id")
    handle = data.get("agent_handle")
    kind = data.get("kind")
    if not all(isinstance(x, str) and x for x in (rid, handle, kind)):
        return None
    return Token(
        rule_id=rid,
        agent_handle=handle,
        kind=kind,
        reason=str(data.get("reason", "")),
        issued_at=str(data.get("issued_at", "")),
        expires_at=data.get("expires_at"),
        one_shot=bool(data.get("one_shot", False)),
        path=path,
    )


def find_active_token(
    *,
    kind: str,
    rule_id: str,
    agent_handle: str,
    start: Path | None = None,
) -> Token | None:
    """Find the first non-expired token of `kind` for `(rule_id,
    agent_handle)`. Lazily deletes expired tokens encountered during
    the scan.

    `kind` must be "ack" or "override". Returns None when no matching
    valid token exists.
    """
    if kind == "ack":
        dir_fn = _acks_dir
    elif kind == "override":
        dir_fn = _overrides_dir
    else:
        raise ValueError(f"unknown token kind: {kind!r}")
    try:
        tokens_dir = dir_fn(agent_handle, start)
    except paths.OperonPathError:
        return None
    if not tokens_dir.is_dir():
        return None
    for path in sorted(tokens_dir.glob(f"{rule_id}-*.json")):
        token = _read_token(path)
        if token is None:
            continue
        if token.kind != kind:
            continue
        if token.rule_id != rule_id:
            continue
        if _token_is_expired(token):
            # Lazy GC of expired tokens. Best-effort; ignore failures.
            try:
                path.unlink()
            except OSError:
                pass
            continue
        return token
    return None


def consume_token(token: Token) -> bool:
    """Delete a one-shot token's file. No-op for TTL-only tokens.

    Returns True iff the file was unlinked (or didn't exist). False
    on unexpected OSError -- caller treats False as "consumption
    failed; do not honor" to be safe.
    """
    if not token.one_shot:
        return True
    try:
        token.path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        _log.warning("token consume failed for %s: %s", token.path, exc)
        return False


def write_token(
    *,
    kind: str,
    rule_id: str,
    agent_handle: str,
    reason: str,
    ttl_seconds: int | None = None,
    one_shot: bool = False,
    start: Path | None = None,
) -> Path:
    """Write a new token to disk. Atomic via temp + os.replace.

    Returns the final path. Raises `RulesError` on filesystem
    failure. Caller is responsible for any preceding validation
    (rule_id existence, enforcement level, caller identity).
    """
    if kind not in {"ack", "override"}:
        raise ValueError(f"unknown token kind: {kind!r}")
    if not rule_id or not agent_handle:
        raise ValueError("rule_id and agent_handle must be non-empty")

    now = datetime.now(timezone.utc)
    issued_at = now.isoformat(timespec="seconds")
    expires_at: str | None = None
    if ttl_seconds is not None:
        from datetime import timedelta
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="seconds"
        )

    if kind == "ack":
        tokens_dir = _acks_dir(agent_handle, start)
    else:
        tokens_dir = _overrides_dir(agent_handle, start)
    tokens_dir.mkdir(parents=True, exist_ok=True)

    import uuid as _uuid
    token_id = _uuid.uuid4().hex
    target = tokens_dir / f"{rule_id}-{token_id}.json"
    payload = {
        "rule_id": rule_id,
        "agent_handle": agent_handle,
        "kind": kind,
        "reason": reason,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "one_shot": one_shot,
    }
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp = target.with_name(
        f"{target.name}.tmp.{os.getpid()}.{_uuid.uuid4().hex}"
    )
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RulesError(f"failed to write {kind} token: {exc}") from exc
    return target
