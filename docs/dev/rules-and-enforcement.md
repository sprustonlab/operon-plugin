# Rules and Enforcement

Guardrail Rules are declarative deny/warn/log policies matched on a tool
call plus its input, scoped by role and phase. The schema and merge logic
live in `src/operon_mcp_server/rules.py`; the plugin-tier rule set lives in
`plugins/operon-plugin/rules.yaml`; enforcement happens in the
[PreToolUse hook](hooks.md). This page covers the Rule schema, the tier
merge, the evaluation algorithm, and the escape-token lifecycle. For what
a block looks like and how to clear one, see
[Guardrails](../user-guide/guardrails.md).

## Enforcement tiers

A Rule's `enforcement` is exactly one of three tiers:

| Tier | Behavior | Cleared by |
|------|----------|-----------|
| `deny` | Hard block. The action does not run. | an **override** (one-shot, user-approved) |
| `warn` | Soft block. The action does not run until acknowledged. | an **ack** (TTL-bounded, model-issued) |
| `log` | Silent audit. The action runs; an audit row is recorded. | nothing -- it never blocks |

When multiple Rules match one call, `_evaluate` picks the
most-restrictive: `deny` shadows `warn` shadows `log`
(`_ENFORCEMENT_SEVERITY`). Ties break by declaration order.

The two escape tokens -- override and ack -- are together the **escape
token** umbrella. See [the token lifecycle](#escape-tokens-overrides-and-acks)
below.

## Rule schema

A Rule is parsed into the frozen `Rule` dataclass. The YAML shape (the
same whether in `rules.yaml` or a workflow `rules:` block):

```yaml
- id: no_push_before_testing        # required, unique within its tier
  trigger: PreToolUse/Bash          # str or list; "PreToolUse/<Tool>" or bare "PreToolUse"
  enforcement: deny                 # deny | warn | log
  detect:
    pattern: "git push"             # regex applied to tool_input[field]
    field: command                  # default "command"
  exclude_if_matches: "\\.(md)$"    # optional; short-circuit (no match) when this matches
  message: "Code should not be pushed before it reaches the testing phase."  # surfaced to the model on a block
  roles: [coordinator]              # only these roles (default: all)
  exclude_roles: []                 # never these roles
  phases: []                        # only these phases (default: all)
  exclude_phases: [testing-implementation, documentation, signoff]
```

Field notes:

- `trigger` -- `PreToolUse/Bash` fires only for `Bash`; a bare
  `PreToolUse` matches every tool. The part after `/` is matched against
  the hook's `tool_name`.
- `detect.field` -- which `tool_input` key the `pattern` runs against.
  Defaults to `command` (the Bash fast path); use `file_path` for
  Edit/Write rules. A non-string field value is JSON-encoded before
  matching.
- `exclude_if_matches` -- a regex that, when it matches the same field,
  suppresses the Rule (used e.g. to let the Coordinator write `.md` files
  while warning on code).
- `roles` / `phases` and their `exclude_` counterparts -- the projection.
  An empty `roles` means all roles; an empty `phases` means all phases.

## The plugin-tier rules

`plugins/operon-plugin/rules.yaml` ships four global Rules that apply
across all workflows:

| id | tier | fires on |
|----|------|----------|
| `no_rm_rf` | deny | `rm -rf /` on an absolute path |
| `warn_force_push` | warn | `git push --force` / `-f` |
| `warn_sudo` | warn | a command beginning with `sudo ` |
| `log_git_operations` | log | any command beginning with `git ` |

`project_team.yaml`'s `rules:` block adds workflow-scoped Rules on top --
for example `no_push_before_testing` (deny, excluded in the late phases)
and `no_direct_code_coordinator` (warn, Coordinator-only). The workflow
tier is additive and has its own id namespace by convention.

## Tier merge

`load_merged_rules` layers four tiers in this order, higher wins **per
rule id**:

1. plugin -- `${CLAUDE_PLUGIN_ROOT}/rules.yaml`
2. user -- `~/.operon/rules.yaml`
3. project -- `<project>/.operon/rules.yaml`
4. workflow-embedded -- the active manifest's `rules:` block (additive;
   independent id namespace)

So a user or project `rules.yaml` can override a plugin rule by reusing
its id, while workflow rules stack on without colliding. Both the bare
top-level list form (`- id: ...`) and the wrapped form (`rules: [...]`)
are accepted when parsing a file.

## Evaluation

`match_rule` is a pure function; its filter order mirrors the historical
pipeline: **trigger -> role -> phase -> exclude -> detect**. A Rule fires
only if the tool name is in its trigger set, the caller's role passes the
role filter, the current phase passes the phase filter, the
`exclude_if_matches` pattern does *not* match, and the `detect.pattern`
*does* match (a Rule with no `detect.pattern` matches every input for its
trigger).

`_evaluate` iterates the merged list, keeps the highest-severity match,
short-circuits on the first `deny`, and returns a `Decision`
(`action`, `rule_id`, `message`, `enforcement`,
`override_token_required`). Both `match_rule` and `_evaluate` are pure --
they take the rule list, role, and phase as arguments and touch no global
state, which is what lets the hook and the tests call them directly.

## Where enforcement runs

The Rules are *evaluated* by the [PreToolUse hook](hooks.md), not by an
MCP tool. The hook resolves `(role, phase)`, calls `load_merged_rules`
then `_evaluate`, and translates the `Decision` into a Claude Code
`permissionDecision`. A `deny`/`warn` decision is emitted as a hook
`deny` with a hint to call `request_override` / `acknowledge_warning`
(operon deliberately does not emit the native `ask`, which permission mode
can bypass). A `log` decision allows and writes an audit row. See the hook
page for the full request/response shape and the fail-closed safety net.

## Escape tokens: overrides and acks

Tokens authorize the model to bypass a Rule on its next retry. They live
under the run-dir, one subdir per agent handle:

```
<run-dir>/overrides/<agent_handle>/<rule_id>-<uuid4>.json
<run-dir>/acks/<agent_handle>/<rule_id>-<uuid4>.json
```

| | override (deny) | ack (warn) |
|---|-----------------|------------|
| issued by | `request_override` (after user accepts an elicitation) | `acknowledge_warning` (model self-issues) |
| lifetime | one-shot -- consumed (file unlinked) on first matching hook fire | TTL-bounded (default 60s); valid for repeated retries until it expires |
| trust model | user-gated; the model cannot self-grant | model-gated; clears the soft block |

`write_token` writes a token atomically. `find_active_token` looks up the
first non-expired token for `(kind, rule_id, agent_handle)` and lazily
garbage-collects expired ack tokens it passes. `consume_token` unlinks a
one-shot override. The PreToolUse hook is the consumer: on a `deny`
decision it checks for an override and, if present, consumes it and
converts the block to allow; on a `warn` decision it checks for an ack and,
if present, allows without consuming.

The filename embeds the `rule_id` so the hook's lookup is a cheap
`glob(<rule_id>-*.json)`. The per-handle subdir keeps one agent's tokens
from matching another's. `command_hash(rule_id, tool_name, tool_input)`
(SHA-256) is available for callers that want a deterministic per-command
token name.

## Audit log

Every fired Rule (deny/warn/log) and every override/ack appends one JSONL
row to `<run-dir>/guardrail_log.jsonl` via `append_log_event`. Rows are
written with `O_APPEND` and kept under 4 KiB so line-atomicity holds
across processes; an oversized row is truncated (fat fields first) and, in
the pathological case, replaced with a minimal placeholder rather than
dropped. `build_log_event` constructs the row schema (timestamp, type,
outcome, rule_id, agent, role, current_phase, tool_name,
tool_input_summary, enforcement, message).

## Adding a Rule

1. **Pick the tier.** A catastrophic, always-on block goes in
   `plugins/operon-plugin/rules.yaml`. A workflow-specific policy goes in
   the manifest's `rules:` block. A site policy goes in the user/project
   `rules.yaml`.
2. **Write the entry** with a unique `id`, a `trigger`, an
   `enforcement`, and a `detect.pattern` (plus `field` if it is not
   `command`). Scope with `roles`/`phases` as needed.
3. **For a catastrophic deny**, also add a mirrored entry to the hook's
   `_FAILCLOSED_DENY` set (see [Hooks](hooks.md)) so it fires even when
   `rules.yaml` is missing or disabled. Keep the regex and `rule_id`
   identical in both places. This second copy is reserved for
   unrecoverable-class actions only; keep it small (under five entries).
4. **Verify projection.** The `_smoke` workflow's embedded rules
   (`smoke_vision_no_curl`, `smoke_worker_write_warn`) exercise
   role+phase projection; mirror that shape if your rule is scoped.
