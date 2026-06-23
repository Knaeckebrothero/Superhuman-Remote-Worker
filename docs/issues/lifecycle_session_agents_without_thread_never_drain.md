---
tags:
  - issue
  - lifecycle
  - persistent-sessions
related:
  - "[[session_agent_drift_drain_kills_idle_sessions]]"
  - "[[agent_lifecycle_management]]"
  - "[[unified_instance_lifecycle]]"
---

# Agents wedged in `session` with no thread are never drained

**Status:** 🔴 **OPEN / ACTIVE** — this specific gap is still unfixed as of
2026-06-23 (several *adjacent* lifecycle bugs have shipped fixes; this one has
not). Until the stopgap below deploys, manual `kubectl delete pod` of the wedged
session pod is the only *live* remediation. The **proper solution** — the
unified-lifecycle root-cause fix ([[unified_instance_lifecycle]]:
orchestrator-set *intent* vs agent-reported *observation*, so a 5s heartbeat
can't re-assert `session` over an orchestrator drain) — **has been postponed
twice** (2026-05-09 and 2026-06-12; see the *Deferral log* below). A targeted
**guarded-reap stopgap** (option A under *Proposed fix*, actuated via pod-delete
— `reap_orphaned_session_agents`) was implemented 2026-06-23 (uncommitted,
pending review); it is explicitly **not** the proper solution and does not
discharge the deferral.

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

## Deferral log — proper solution postponed ×2

The root-cause fix has been consciously deferred on two occasions. Recorded
here so the 2026-06-23 stopgap decision is made with eyes open and the proper
fix is not quietly abandoned. Rows 1–2 are the two postponements; row (3) logs
the current stopgap so it isn't a silent *third* deferral.

| # | Date | Trigger | Chosen instead | Why deferred |
|---|------|---------|----------------|--------------|
| 1 | 2026-05-09 | 5 session-zombie pods surviving ~50 deploys ([[agent_lifecycle_management]]) | Wrote the proper fix up as [[unified_instance_lifecycle]] but left it "Not yet implemented"; leaned on the 2026-05-08 partial mitigations (in-pod watchdog `4c64a30` + detach sweep `mark_stuck_session_agents_ready` `de52290`) | Full refactor carries ~8 open design questions; cheap mitigations shipped first |
| 2 | 2026-06-12 | This exact `session`+NULL-thread wedge recurred (`38c90a93`, `c7b8c626`) | Filed this doc with a proposed guard / status-repair — **neither coded**; manual cleanup only | Root cause "unverified"; the adjacent 2026-06-10 incident ([[session_agent_drift_drain_kills_idle_sessions]]) made reaping session agents feel risky; proper fix still owed to the unified refactor |
| (3) | 2026-06-23 | 3rd instance found 2026-06-16 (`srw-agent-j-b9e040ce` / agent `639ce8f6`, bound to thread `05220a87`, ended 06-12 14:58) | Implemented the **guarded-reap stopgap** (option A) — `reap_orphaned_session_agents` + pod-delete in `stale_agent_detector` (uncommitted, pending review). Flip-to-ready (option B) was rejected — it doesn't stick on a *live* zombie (heartbeat re-asserts `session`) | Lowest-risk relief that composes with the refactor — **proper fix remains deferred**; this is a stopgap, not a discharge |

### Why the proper fix keeps losing

- **Incident precedent.** Reaping session agents already killed live idle
  sessions once (2026-06-10, [[session_agent_drift_drain_kills_idle_sessions]]);
  the response deliberately hardened session agents *against* reaping
  (`is_idle()` returns False whenever `thread_id` is set), so any new reap path
  now carries regression risk and a "verify the root cause first" bar.
- **Unverified root cause.** How `session`+NULL-thread arises is still not
  pinned down; this code is race-prone (the 06-10 arc surfaced extra
  re-entrancy + 409 resume races).
- **Refactor not built.** The proper home ([[unified_instance_lifecycle]]) is a
  2026-05-09 proposal, still unimplemented; the reconciler is mid-migration onto
  its `Reapable` abstraction (the workspace-reaper was stage 1).
- **Priority.** Zombies are rare (1–2 at a time) with a working manual
  workaround, so they keep losing to revenue/feature work.

## Cleanup performed / recurrences

- **2026-06-12** — pods deleted, rows set `offline` (agents `f0e12a32`,
  `26e630ec`). Log tails archived in the incident transcript.
- **2026-06-16** — 3rd instance identified during investigation:
  `srw-agent-j-b9e040ce` (agent `639ce8f6-46cf-442e-abc1-8d4d40121e84`), bound
  to thread `05220a87` ("Building a RAG Chatbot Demo") which ended
  `2026-06-12T14:58:44Z` — the pod had been a zombie ~3.6 days, idle
  (`Current job: idle`, ~0% CPU on the agent process), surviving every redeploy.
  Manual delete is the standing remediation — **TODO: confirm this pod was
  actually reaped** (recommended, not verified as done).

## Proposed fix

Defense in depth, either/both:

- **Guarded reap (reconciler guard)** ⟵ **implemented stopgap, 2026-06-23**
  (uncommitted, pending review): treat `status='session'` with `thread_id IS
  NULL` and `current_job_id IS NULL` as reapable after a grace period (it holds
  nothing user-visible) and **delete the pod**. Shipped as
  `PostgresDB.reap_orphaned_session_agents` — grace via a DB-stamped
  `intents.session_orphaned_at` (DB clock; survives restart; cleared the moment
  the agent leaves the orphan state), called from the `stale_agent_detector`
  loop, which deletes each returned pod via `agent_provisioner.delete_agent_pod`.
  The row then ages to `offline` (heartbeat timeout) and is GC'd. Works on
  old-image agents too (no agent-side dependency). Scoped to `thread_id IS NULL`
  so it never touches a thread-bound live session (the 2026-06-10 incident).
  Tests: `tests/test_idle_timeout.py::TestReapOrphanedSessionAgents` (+ sweep
  ordering).
- **Status repair (flip to `ready`)** — considered and **rejected** for the
  stopgap: flipping an orphan to `ready` does **not stick** on a *live* zombie
  because the agent re-asserts `session` on its next 5s heartbeat
  (`_heartbeat_status` returns `session` while `_session` is set). Only deleting
  the pod sticks. A genuine agent-side repair (clear `_session` / exit when the
  bound thread is gone) is a complementary, forward-only fix but needs the
  unverified root cause pinned down first — folded into the proper fix.

> **The implemented stopgap is not the proper solution.** It reaps the symptom
> (provably-empty wedged pods); it does not fix why they arise or stop a *live*
> agent re-asserting `session`. The root-cause fix is the orchestrator
> intent/observed split in [[unified_instance_lifecycle]]; until that ships,
> this issue stays **OPEN** even with the reap running.

## Pointers

- Broad analysis + history: [[agent_lifecycle_management]]
- Proper-solution design (unimplemented): [[unified_instance_lifecycle]]
- Adjacent resolved incident (why reaping session agents is risky):
  [[session_agent_drift_drain_kills_idle_sessions]]
- Code: `orchestrator/services/lifecycle/agent_manager.py` (`is_idle`,
  `signal_drain_pending`); `orchestrator/database/postgres.py`
  (`mark_stuck_session_agents_ready`, `reap_orphaned_session_agents`, heartbeat
  handler); `orchestrator/main.py::stale_agent_detector` (reap wiring +
  pod-delete); agent-side `src/api/persistent_app.py` (heartbeat-intent handler,
  thread-status watchdog).
