# Guardrails

Guardrails do two jobs. First, they stop an agent from taking a
destructive or off-role action -- deleting files, pushing code before it
is tested, or writing code the Coordinator was meant to delegate.
Second, they let you steer the team: when you notice a behavior you want
to discourage, you encode it once as a Rule instead of adding another
line to a `CLAUDE.md` that every agent then has to carry in its context
window.

A **guardrail Rule** is the unit of that policy: a declarative rule
matched on a tool call plus its input, scoped by role and/or phase. When
a matching call is attempted, operon steps in before it runs (enforced
by the [PreToolUse hook](../dev/hooks.md)).

Rules come from four places, layered in this order:

1. **Plugin** -- `plugins/operon-plugin/rules.yaml`, shipped with the
   plugin and active in every workflow.
2. **User** -- `~/.operon/rules.yaml`, your own rules across all your
   projects.
3. **Project** -- `<project>/.operon/rules.yaml`, rules for one project.
   This is authored config you commit, even though it sits under
   `.operon/` -- set the project's `.gitignore` to keep it (see
   [What lives under `.operon/`](sessions.md#what-lives-under-operon)).
4. **Workflow** -- a `rules:` block inside a workflow manifest, active
   while that workflow runs.

The first three tiers override by id: when the same id appears in more
than one, the higher tier wins -- project over user, user over plugin.
So you tighten, loosen, or replace a shipped rule by redefining its id
in your own `~/.operon/rules.yaml` or the project's
`.operon/rules.yaml`, without touching the plugin.

Workflow rules are layered on top of that merged set rather than
replacing entries in it. Enforcement is decided by evaluating every
matching rule and applying the most restrictive one (deny over warn over
log). A workflow rule can therefore tighten what a broader rule allows,
but it cannot relax a stricter rule a higher tier already imposes.

## The three enforcement tiers

Every Rule has exactly one enforcement tier:

| Tier | What happens | How you clear it |
| ---- | ------------ | ---------------- |
| **deny** | Hard block. The action does not run. | An [override](#overrides-deny-tier) (user-approved). |
| **warn** | Soft block. The action runs only after acknowledgment. | An [ack](#acks-warn-tier) (agent self-service). |
| **log** | Silent audit. The action runs; a log entry is recorded; no prompt. | Nothing -- it never blocks. |

When a deny or warn stops an action, the message the agent sees leads
with the user's reason for the Rule and then points to the escape path
-- so the agent learns why it was stopped and how to proceed, not just
that it was blocked.

### Deny

Reserve deny for actions you would almost never want to go through
unreviewed -- where the right answer is "come ask me first." That is why
only *you* can authorize one and the agent cannot clear it itself: a
deny says you think there is little chance the action should be
permitted as-is.

A deny-tier Rule hard-blocks the action: nothing runs. The block reason
tells the agent to call `mcp__operon__request_override(...)` with the
rule id, which asks you to approve the action before it can proceed --
the full flow is under [Overrides](#overrides-deny-tier). For example,
`rules.yaml` ships `no_rm_rf`, which denies `rm -rf` on an absolute path;
the `project_team` workflow adds `no_push_before_testing` (denies `git
push` outside the testing/documentation/signoff phases) and
`no_force_push`.

### Warn

Reach for warn when you usually want to stop an action but the agent
should be able to continue once it understands why. The Rule's message
is where you give that reason -- a warn is how you explain to the agent
what to avoid and let it proceed with that in mind, no user needed.

A warn-tier Rule also blocks the action up front, but here the agent can
clear the block itself: the block reason tells it to call
`mcp__operon__acknowledge_warning(...)` and retry -- the full flow is
under [Acks](#acks-warn-tier). `rules.yaml` ships `warn_force_push`
(force-push rewrites history) and `warn_sudo` (any `sudo` invocation).
The `project_team` workflow adds warns such as `no_direct_code_coordinator`
(the Coordinator should delegate code writing) and `no_close_leadership`.

### Log

Use log when you are not sure an action is worth gating yet. It never
blocks -- it records the action and its context so you can review the
trail later and decide whether to promote it to a warn or a deny.

Nothing is visible to the agent. The action proceeds and an entry is
written to the run's audit log. `rules.yaml` ships `log_git_operations`,
which records every git command for the audit trail.

## Escape tokens

An **escape token** is how an agent clears a gating Rule. There are two
kinds.

### Overrides (deny tier)

An override clears a deny. The agent requests one and you decide whether
it goes through:

```text
mcp__operon__request_override(rule_id="<id>", reason="<why>")
```

operon shows you the rule and the agent's stated reason and asks you to
approve or decline. On approval a one-shot token is written; it
authorizes a single matching action and is gone once used.
`request_override` is restricted to the Coordinator -- a worker that
needs one asks the Coordinator.

### Acks (warn tier)

An ack clears a warn. The agent acknowledges it itself, which records
the decision and lets the action proceed:

```text
mcp__operon__acknowledge_warning(rule_id="<id>")
```

The token is short-lived (default 60-second TTL) so it stays scoped to
the current turn and a stale ack cannot silently unblock a future warn.
Any agent, including workers, may acknowledge.

## What applies to the current agent

```text
/rules
```

The `/rules` skill (backed by `mcp__operon__get_applicable_rules`)
renders a `## Constraints` block scoped to the calling agent's current
`(role, phase)`, with three sections: the advance checks, the active
Rules, and that agent's active escape tokens. For an ack it shows a
`seconds_remaining` countdown; expired acks are garbage-collected during
the scan. If the agent holds any active tokens, the skill calls out the
count and nearest TTL above the listing.

!!! note
    `/rules` introspects the calling agent's own scope only. Passing
    another agent's name returns `cross_agent_not_implemented`.

For the rule-matching schema, tier resolution, and how the hook
consumes tokens, see the
[Contributor/Architecture Guide -> Rules and Enforcement](../dev/rules-and-enforcement.md)
and [Hooks](../dev/hooks.md). The full tool list is in the
[MCP Tools Reference](mcp-tools-reference.md).
