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

## Pointers

- `orchestrator/services/lifecycle/reconciler.py` (drift loop, stats)
- `orchestrator/services/lifecycle/agent_manager.py:135` (`is_idle`),
  `:146` (`signal_drain_pending` + heartbeat-callback contract)
- Agent-side heartbeat drain handler (in-pod 5s tick consumer of
  `intents.should_drain`)
- Incident log: `docs/tests/rclone_cloud_mount_dev_cluster.md` §13
