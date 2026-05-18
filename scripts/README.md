# scripts/

Auxiliary scripts for developing and smoke-testing operon-plugin.
These are NOT shipped inside the plugin package -- they are developer
tooling that lives in the repo for convenience.

## Phase 4 smoke check

Verifies inter-agent messaging end-to-end:
mailbox envelope I/O, the `watchdog`-driven filesystem-watch loop,
and the `notifications/claude/channel` push into each Agent's own
session (RESEARCH §J.3 -- channel push is one-way to its OWN session
only; cross-session reach is filesystem mailbox plus per-subprocess
watch loop).

### 1. Bootstrap the scratch project

Run the setup helper. It wipes `/tmp/test-operon/` and writes the
four Coordinator bootstrap files atomically via `os.replace`:

```bash
python /groups/spruston/home/moharb/operon-plugin/scripts/smoke_phase4_setup.py
```

The script prints the `export OPERON_AGENT_HANDLE=...` and `cd ...`
lines on stdout. Copy/paste them into your shell:

```bash
export OPERON_AGENT_HANDLE=<uuid printed by the script>
cd /tmp/test-operon
```

The bootstrap creates:

- `.operon/_active.json` -- points at `msg-test-1`
- `.operon/msg-test-1/phase_state.json` -- `current_phase: vision`,
  `workflow_id: _smoke`
- `.operon/msg-test-1/_handles/<coord_handle>.json` -- Coordinator
  identity record
- `.operon/msg-test-1/agents.json` -- empty roster

It deliberately does NOT create `mailbox/<agent>/inbox/`,
`mailbox/<agent>/control/`, `mailbox/<agent>/acks/`, or `overrides/`.
Those are created lazily by `mailbox.write_envelope()` on first use,
which is the production code path the smoke check should exercise.

### 2. Launch the Coordinator's Claude Code session

From the same shell (so `OPERON_AGENT_HANDLE` propagates into the MCP
subprocess's `os.environ` per SPEC §6.5):

```bash
claude --plugin-dir /groups/spruston/home/moharb/operon-plugin/plugins/operon-plugin/
```

To diagnose watch-loop / identity-binding issues, also export
`OPERON_DEBUG=1` before launching Claude Code. The Coordinator's
MCP subprocess (and every worker it spawns, via the env propagation
in `spawn_agent._spawn_subprocess`) will then emit DEBUG-level logs
to its stderr. Claude Code captures each MCP subprocess's stderr to
`~/.cache/claude-cli-nodejs/<cwd-mangled>/mcp-logs-plugin-operon-plugin-operon/<timestamp>.jsonl`
(each line wrapped as `{"error": "Server stderr: ..."}`). Tail that
file to watch real-time worker boot:

```bash
OPERON_DEBUG=1 claude --plugin-dir /groups/spruston/home/moharb/operon-plugin/plugins/operon-plugin/
# In a separate shell, after spawning a worker:
tail -f ~/.cache/claude-cli-nodejs/$(pwd | sed 's|/|-|g')/mcp-logs-plugin-operon-plugin-operon/*.jsonl
```

To enable the LLM-visible channel push (so the worker's session
actually sees the `<channel>` tag in its conversation context), add
`--channels=plugin:operon-plugin@inline` to the Coordinator's
`claude` launch. The marketplace-installed case picks up the right
tag automatically via `channel_tag.channel_tag_for_self`, which
reads `~/.claude/settings.json` `enabledPlugins`; dev-loaded users
who launch with `--plugin-dir` use `@inline`. The production
`--channels=` flag is parsed unconditionally on every supervisor
restart, so it survives Claude Code's `/agents`-view attach-
respawn -- unlike the prior `--dangerously-load-development-channels`
flag, whose interactive-confirmation state did not persist past
respawn.

> **Note on misleading toast banner (Claude Code 2.1.143).** When
> you launch with `--channels=plugin:operon-plugin@inline`, the
> startup banner displays two cosmetic toast lines:
>
> ```
> plugin:operon-plugin@inline * plugin not installed
> plugin:operon-plugin@inline * not on the approved channels allowlist
> ```
>
> These are **misleading**. They are emitted by a UI banner
> validator that runs alongside, but is independent of, the actual
> channel-gate decision. The functional gate (covered by Boaz's
> binary patches) DOES pass, and channels ARE registered. Verify by
> tailing the operon MCP log immediately after launch:
>
> ```bash
> tail -f ~/.cache/claude-cli-nodejs/$(pwd | sed 's|/|-|g')/mcp-logs-plugin-operon-plugin-operon/*.jsonl | grep -E "Channel notifications (registered|skipped)"
> ```
>
> If you see `"Channel notifications registered"`, the gate
> passed and channel push works normally. If you see
> `"Channel notifications skipped: <reason>"`, that's the real
> failure mode -- capture the reason and report it.

Without the flag, the mailbox filesystem transport still works
end-to-end (envelopes move from `inbox/` to `inbox/processed/`,
audit trail unaffected) but Claude Code logs "Channel notifications
skipped: server <id> not in --channels list for this session" for
each push and the LLM never sees the message tag.

### 3. Run the 5-step smoke check inside the Coordinator session

Issue these tool calls in order. Each step's success condition is
listed under it.

**Step 1 -- `whoami`**

```
mcp__operon__whoami()
```

Expected: returns `{"name": "Coordinator", "role": "coordinator",
"workflow_id": "_smoke", "current_phase": "vision", "cwd":
"/tmp/test-operon", "session_id": "manual-test-session"}`.

**Step 2 -- spawn worker-A**

```
mcp__operon__spawn_agent(
  name="worker-A",
  path="/tmp/test-operon",
  prompt="You are worker-A. Wait for messages.",
  type="worker"
)
```

Expected: success payload with a fresh `handle` and `session_id`.
After the call, `cat /tmp/test-operon/.operon/msg-test-1/agents.json`
shows one row for `worker-A`.

**Step 3 -- spawn worker-B**

```
mcp__operon__spawn_agent(
  name="worker-B",
  path="/tmp/test-operon",
  prompt="You are worker-B. Wait for messages.",
  type="worker"
)
```

Expected: `agents.json` now contains two rows (worker-A and
worker-B), each with a distinct `handle` and `session_id`.

**Step 4 -- message worker-A**

```
mcp__operon__message_agent(
  name="worker-A",
  message="ping",
  requires_answer=false
)
```

Expected: returns `{"correlation_id": "<id>", "delivered_to":
"worker-A", "envelope_path": "..."}`. Within ~1 second the envelope
file moves out of
`/tmp/test-operon/.operon/msg-test-1/mailbox/worker-A/inbox/` and
into the sibling `inbox/processed/` directory -- this is the target's
own watch loop claiming the envelope and pushing it into worker-A's
session.

**Step 5 -- broadcast to both workers**

```
mcp__operon__broadcast_message(
  names=["worker-A", "worker-B", "Coordinator"],
  message="hello all",
  requires_answer=false
)
```

Expected: returns `{"sender": "Coordinator", "delivered": [{worker-A
...}, {worker-B ...}], "failed": [], "skipped_self": true,
"broadcast_results_path": "..."}`. The Coordinator's own name is
silently filtered out per SPEC §7 `broadcast_message`. Two new audit
rows appear in
`/tmp/test-operon/.operon/msg-test-1/broadcast_results.jsonl`.

### 4. What to look for on disk

The shell-side confirmations are the canonical evidence that
cross-session channel push worked. Run these after step 5:

```bash
# Roster: two worker rows.
cat /tmp/test-operon/.operon/msg-test-1/agents.json

# Each worker has its envelopes already moved to processed/, meaning
# its own MCP subprocess saw the envelope and pushed it via channel.
ls /tmp/test-operon/.operon/msg-test-1/mailbox/worker-A/inbox/processed/
ls /tmp/test-operon/.operon/msg-test-1/mailbox/worker-B/inbox/processed/

# inbox/ itself is empty (or contains only the processed/ subdir).
ls /tmp/test-operon/.operon/msg-test-1/mailbox/worker-A/inbox/
ls /tmp/test-operon/.operon/msg-test-1/mailbox/worker-B/inbox/

# Audit log for the broadcast.
cat /tmp/test-operon/.operon/msg-test-1/broadcast_results.jsonl
```

Success criteria:

- `agents.json` contains exactly two rows (worker-A, worker-B).
- `worker-A/inbox/processed/` contains 2 envelopes (the
  `message_agent` and the broadcast).
- `worker-B/inbox/processed/` contains 1 envelope (the broadcast).
- `worker-A/inbox/` and `worker-B/inbox/` are empty (apart from the
  `processed/` subdir).
- `broadcast_results.jsonl` has 2 lines, both with `"outcome":
  "delivered"`.

If an envelope stays in `inbox/` instead of moving to `processed/`,
the target's watch loop is not running (likely identity binding
failed -- check `mcp__operon__whoami` from inside the worker session
to diagnose).

To eyeball the channel push from inside a worker's session, run
`claude agents` from `/tmp/test-operon/` to list session ids, then
`claude attach <worker-A session id>` to see the worker's transcript;
the `<channel source="operon" sender="Coordinator" kind=
"deliver_message" ...>ping</channel>` event should appear there.
