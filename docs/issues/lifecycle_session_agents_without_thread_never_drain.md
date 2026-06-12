---
tags:
  - issue
  - lifecycle
  - persistent-sessions
related:
  - "[[session_agent_drift_drain_kills_idle_sessions]]"
---

# Agents wedged in `session` with no thread are never drained

**Filed:** 2026-06-12, from the minimax-m3 session incident follow-up
(thread `b9b392f7`). Two agent pods on dev had survived **5–7 days** of
image bumps (`srw-agent-j-38c90a93` on `sha-0bb1715` from 06-05,
`srw-agent-j-c7b8c626` on `sha-9661199` from 06-07), heartbeating
forever while every sibling drained on schedule.

## State observed

Both DB rows: `status='session'`, `thread_id=NULL`, `current_job_id=NULL`,
live heartbeats, `intents={"should_drain": true, "drain_reason":
"stale_image"}`. Pod logs end in an infinite heartbeat loop after:

```
06-05 15:25:38 persistent_app  - WebSocket disconnected: thread=8982dd43-… (loop continues)
06-05 15:45:01 persistent_session - Remote workspace backend disconnected
06-07 09:38:36 dual_app        - Drain intent received (reason=stale_image) — will freeze at next phase boundary
```

(`c7b8c626` shows the same shape, with `thread=None` on disconnect.)

## Two stacked gaps

1. **Agent side (pre-06-10 images):** the dual-mode agent handled the
   drain intent with *worker* semantics — "freeze at next phase
   boundary". A parked session has no running graph, so no phase
   boundary ever arrives. The 2026-06-10 clean-suspend fix
   (defer-busy / suspend-parked) addresses this for current images,
   but any agent that wedges into `session` without a bound thread is
   outside the suspend path too — there's nothing to suspend.

2. **Reconciler side (still current):**
   `AgentInstanceManager.is_idle()`
   (`orchestrator/services/lifecycle/agent_manager.py`) requires
   `status == 'ready'`. An agent stranded in `status='session'` is
   never idle, so the reconciler only re-stamps the (ignored) drain
   intent every tick. Deadlock: orchestrator politely waits, agent
   never moves.

## Why `session` + NULL thread exists at all

The session detached (WebSocket gone, workspace disconnected, thread
unbound) without the agent flipping its row back to `ready`. Whether
that flip is missing in general or only in a crash/edge path is
unverified — but the reconciler should be robust to it regardless.

## Cleanup performed 2026-06-12

Pods deleted, rows set `offline` (agents `f0e12a32`, `26e630ec`). Log
tails archived in the incident transcript.

## Proposed fix

Defense in depth, either/both:

- **Reconciler guard:** treat `status='session'` with `thread_id IS
  NULL` and `current_job_id IS NULL` as drainable after a grace period
  (it holds nothing user-visible). This covers any future agent-side
  wedge, including old-image agents that can't be fixed retroactively.
- **Status repair:** when a dual-mode agent fully detaches a session
  (loop parked, thread unbound), flip the row back to `ready` so the
  normal idle-drain path applies.
