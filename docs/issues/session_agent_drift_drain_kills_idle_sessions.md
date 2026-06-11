---
tags:
  - issue
  - lifecycle
  - persistent-sessions
related:
  - "[[rclone_cloud_mount]]"
---

# Every develop push drift-drains live idle session agents

**Filed:** 2026-06-10, from the rclone dev-cluster runbook incident
(`docs/tests/rclone_cloud_mount_dev_cluster.md` §13, incident 1).

## What happened

Mid-test, CI's deploy commit `e189f683` (agent → `sha-ea373fd`) landed on
dev. Fleet synced, Reloader bounced the orchestrator, and within one
reconciler tick every build-sha-drifted agent pod was gone — including a
just-created, **user-attached but idle** session agent
(`drift: 4, drained: 1, skipped_busy: 3`). The session UI still showed
`Connected`; the next user input hit "Restoring suspended workspace", raced
the half-deleted workspace pod (409), returned 503, and the session showed
"ended".

On dev this means **every push to develop kills all idle live sessions**.

## Mechanism (verified in code)

Two distinct paths fire on drift, and the session kill comes from the
second:

1. `orchestrator/services/lifecycle/reconciler.py` — drift-drain proper is
   already gated on `manager.is_idle(inst)`, and
   `agent_manager.py:is_idle` returns `False` when `thread_id` is set.
   Session-bound agents are therefore `skipped_busy` here. Working as
   intended.
2. `agent_manager.py:signal_drain_pending` — fired on **every** drift
   detection, idle and busy. It writes `intents.should_drain=true`; the
   in-pod heartbeat callback reads it on the next 5s tick and, per its own
   docstring, "self-exits (idle worker, **persistent session**)". An idle
   session agent (no active turn) obeys and dies even though a user is
   attached.

The follow-on 409/503 is the resume race: the workspace pod is still
terminating when the next input triggers "Restoring suspended workspace".

## Why it matters

- Dev pushes are frequent; long-lived test/demo sessions on dev cannot
  survive a working day.
- The UI gives no signal — `Connected` until the next input fails.
- Any long-running validation (e.g. the rclone Phase 6 step-4 runbook
  re-run, token-refresh soak tests) needs sessions that outlive a deploy.

## Options

a. **Exempt user-attached idle sessions from self-exit drain** — in the
   in-pod heartbeat drain handler, treat `thread_id`-bound agents like busy
   workers: defer until the session is released/suspended by its own
   lifecycle (timeout, explicit end). Stale-image session agents then
   linger until natural suspension — acceptable on dev, and prod deploys
   are rare.
b. **Drain = clean suspend** — instead of self-exit, run the full suspend
   path (flush, release workspace, mark thread suspended) so the next input
   resumes cleanly on a new-build agent. Closes the 409/503 race as a side
   effect and keeps fleets converging to the new build.
c. **Fix only the resume race** (409 on half-deleted workspace pod →
   retry/wait instead of 503) and accept the session bounce.

(b) is the right end state; (a) is the one-line mitigation if (b) doesn't
fit in a small PR. (c) is worth doing regardless as resume hardening.

## Fix (implemented 2026-06-10, option b)

Drain on a session-bound agent is now a **clean suspend** that converges on
the attention-sleep terminal state (thread `suspended`, workspace
snapshotted to S3, both pods gone, agent binding cleared). The next user
input walks the existing suspended-restore path on a new-build agent.

Agent side (`src/api/persistent_app.py`):

- `_handle_heartbeat_intents` is now state-aware: no session → exit as
  before; **turn in flight → defer** (re-checked every 5s heartbeat via the
  new `_session_parked()` gate — `_awaiting_input` set exactly around the
  `_loop_get_user_input` queue wait, plus `_tool_inflight` / queued-input
  checks). A drain can no longer kill a running turn (the old handler
  cancelled the loop task unconditionally).
- Parked → `_drain_suspend_session()`: broadcasts `session.suspended` to
  subscribers, runs `_terminate_session("drain", mark_thread=False)`
  (flush + git push + cleanup WITHOUT the `ended` write), then calls the
  new orchestrator endpoint below. Falls back to the legacy `ended` detach
  if the orchestrator can't suspend (suspension service disabled, snapshot
  failure).

Orchestrator side (`orchestrator/main.py`):

- New internal endpoint `POST /api/agents/threads/{thread_id}/suspend`:
  `suspend_thread_workspace()` (snapshot → teardown → agent-pod delete) +
  CAS `status → 'suspended'` from created/active/awaiting_user, clearing
  `threads.agent_id` so the next attach can't target the dead agent.
  Idempotent on already-suspended threads.
- The agent-path `'ended'` write (`PUT /api/agents/threads/{id}/status`) is
  now guarded with `WHERE status <> 'suspended'` — a late shutdown-handler
  `ended` (SIGTERM during pod deletion) or a lost-response fallback can no
  longer clobber an orchestrator-driven suspend. This also fixes the same
  latent clobber race in the attention-sleep sweeper path.

Cockpit:

- `session.suspended` event handler: system message + keeps the composer
  enabled (`threadStatus='suspended'`, which — unlike `ended` — renders no
  resume card); `ThreadStatus` union extended with
  `awaiting_user`/`suspended` to match the backend.

Tests: `tests/test_drain_intent.py` rewritten for the state machine (defer
while busy / queued / tool-inflight, suspend when parked, fallback on
suspend failure, idempotency); cockpit spec asserts suspended ≠ ended.

### Re-entrancy race found during verification (also fixed)

The first live k3d run exposed a second bug this fix depends on:
cancelling the loop task does NOT propagate CancelledError out of
`run_persistent_loop` — the loop swallows it in the input wait and returns
CLEANLY, so `_loop_completion_handler` observed a normal exit and
re-entered `_terminate_session("loop_complete")` mid-teardown. Its
default `mark_thread=True` wrote `ended` before the suspend endpoint ran,
which then correctly refused (thread no longer active). Historically this
double-teardown only duplicated work and `ended` writes; under
drain-suspend it was status-corrupting. Fixed with a module-level
`_terminating` re-entrancy guard in `_terminate_session` (second caller
no-ops); regression test
`test_loop_complete_reentry_skipped_during_drain_teardown` reproduces the
exact sequence.

### Verification (local k3d, 2026-06-10 evening)

Simulated drift (`UPDATE agents SET intents = intents ||
'{"should_drain": true, ...}'` — byte-identical to `signal_drain_pending`)
against a live user-attached idle session (thread `c8022661`, turn 0,
cockpit tab open):

- Heartbeat tick delivered the intent; agent logged "Drain intent received
  … suspending session and exiting" (parked branch).
- Cockpit rendered the `session.suspended` system message ("Session
  suspended for a platform update. Send a message to resume…") — the
  incident's silent `Connected` is gone.
- Run 2 (with the re-entrancy guard) proved the sequencing via
  orchestrator log order: `POST …/suspend` arrived FIRST with the thread
  still active, then the single fallback `ended` write — run 1 (pre-guard)
  had them inverted.
- Local k3d has no snapshot store (`S3_ENDPOINT not set — disabled`), so
  the endpoint returned `suspended=false` and the agent took the designed
  legacy-ended fallback; clicking Resume brought the session back to
  `active` with fresh agent + workspace pods.
- NOT verifiable locally: the full `suspended` terminal state (snapshot →
  CAS → suspended-restore wake). Exercise on dev (S3 present) after the
  next deploy — expected log line: "Drain-suspend complete for thread …".

Still open from this issue: (c) resume-restore 409 hardening — a user input
landing in the (now much smaller) window while the old workspace pod is
still terminating can still race pod re-creation. `ensure_workspace`
already waits on `suspending`; the remaining gap is AlreadyExists handling
in the restore-path pod create.

## Pointers

- `orchestrator/services/lifecycle/reconciler.py` (drift loop, stats)
- `orchestrator/services/lifecycle/agent_manager.py:135` (`is_idle`),
  `:146` (`signal_drain_pending` + heartbeat-callback contract)
- Agent-side heartbeat drain handler (in-pod 5s tick consumer of
  `intents.should_drain`)
- Incident log: `docs/tests/rclone_cloud_mount_dev_cluster.md` §13
