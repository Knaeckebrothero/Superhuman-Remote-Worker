---
tags:
  - feature
  - design
  - sessions
  - orchestration
  - agent-lifecycle
  - jobs
  - notification
aliases:
  - session wake on job completion
  - async session delegation
  - wake on job complete
  - session job notifications
  - lifecycle event bus
related:
  - "[[subagent_delegation]]"
  - "[[headless_persistent_sessions]]"
  - "[[automations]]"
  - "[[notify_user_tool]]"
  - "[[sessions]]"
  - "[[agent_lifecycle]]"
  - "[[sudo_permissions]]"
  - "[[stuck_agent_recovery]]"
  - "[[ephemeral_workspaces]]"
---

# Session Wake on Job Completion

> A session launches a worker job ("run a research agent on topic X"), the user closes the tab, and twenty minutes later the job finishes. Today the result just sits silently in the database. This feature lets the session be woken — or at least notified — when a job *it created* completes, so it can continue. It is **subagent delegation, where the parent is an interactive chat session and the wait is asynchronous.**

**Status:** Design phase. Not scheduled for build — written now to shelve and resume after the pilots, and to co-design the shared event bus with the [[automations]] event-trigger half before either is built.
**Filed:** 2026-06-17

## Motivation

A session can already create worker jobs (`create_worker_job`, `src/tools/orchestrator/jobs.py`). The missing half is the *return path*: when that job finishes, nothing tells the session. The orchestrator flips a status column in the database and goes quiet — no event, no broadcast (confirmed: job completion is a pure DB write; the session's only option today is to sit in a polling loop calling `get_worker_job`, which burns turns and does not survive the session going idle).

The concrete flow we want:

1. A session spins up a job and tells the user "I'll continue when this finishes."
2. The user closes the browser. The session goes idle and is suspended to storage ([[headless_persistent_sessions]] already does this).
3. The job completes.
4. The session is woken (or the user is notified), the result is delivered, and work continues.

This is **not** schedule-driven. We are emphatically *not* waking sessions on a cron tick — a weekly automation firing should never spin up a chat session. This feature is the opposite end of the pipe from [[automations]]: automations *create* jobs when something happens; this feature *reacts* to one specific job — the one this session created — finishing.

### Why this is mostly assembly, not invention

The three hard problems are each already solved by a sibling feature:

| Problem | Already solved by | Mechanism |
|---|---|---|
| Wake a waiter when a job it spawned completes | [[subagent_delegation]] | `_handle_delegation_child_completion()` (`orchestrator/main.py:5119`) transitions the parent `waiting → paused → dispatch` and resumes it. Checkpoint + wake, not long-poll. |
| Wake a session that has gone idle / had its pod torn down | [[headless_persistent_sessions]] | `suspended → active` restore path: rehydrate workspace from S3, rebind agent, resume the loop. Already triggered by reattach and magic-link. |
| Decide whether a woken session may act on a job | [[headless_persistent_sessions]] + thread model | Thread `permission_mode` (`supervised` / `auto_accept` / `autonomous`) already gates how eager an untethered session is. |

Delegation does exactly the wake-on-completion dance — but synchronously (the parent is frozen on the VM) and only job→job. This feature generalizes the *waiter* from "a parent worker job" to "an interactive session," and makes the wait *asynchronous* (the session can be closed, or keep chatting, instead of frozen) by routing through the headless suspend/restore path instead of a job checkpoint.

## The lift: a shared Lifecycle Event Bus

Both this feature and the [[automations]] event-trigger half need the same primitive: *a job hits a terminal state → fan that fact out to whoever cares.* Rather than each feature growing its own completion hook, we lift that into one first-class shared service.

```
        job completes / approved / cancelled / times out
                         │  (emitted AFTER the DB writes commit)
                         ▼
              emit_lifecycle_event(LifecycleEvent)
                         │
              ┌──────────┴──────────┐   ← shared bus: in-process pub/sub now,
              │  Lifecycle Event Bus │     NATS-promotable when multi-replica
              └──────────┬──────────┘
        ┌────────────────┼─────────────────────┐
        ▼                ▼                       ▼
 automations          session_wake          (future: notify-user,
 _dispatcher          _dispatcher            outbound webhooks, …)
 "X job done →        "the job MY session
  create job from      created is done →
  template"            wake / notify me"
   (Automations)        (this feature)
```

**What is shared (the lift):** event *emission* from the terminal-job paths + the *fan-out* bus. A single `orchestrator/services/lifecycle_events.py` that anything can subscribe to.

**What stays each feature's own:**

- **Automations keeps** its table, CRUD UI, cron dispatcher (cron is a *time* source, unrelated to lifecycle events), and its action ("create a job from a saved template").
- **This feature keeps** the session-wake subscription — *transient and programmatic*, not a saved automation row — and its action ("restore / notify the owning thread," never "create a job").

They reuse the **bus**, not each other's tables or handlers. Two `(event, action)` pairs on one pipe.

**Why now is the right time to lift it:** the automations *event* half is **not built yet** (only the cron half shipped, 2026-05-18/20). There is no shipped code to refactor — we simply specify `emit_lifecycle_event` as shared-from-birth. Whenever the automations event-trigger work happens, it subscribes to the bus instead of owning it.

### `LifecycleEvent` contract

Inherited from [[automations]] (it is the public contract `event_filter` matches against), with one field added so the session-wake consumer can route the event without re-querying the job:

```python
LifecycleEvent(
    type="job_complete",            # 'job_complete' in v1; 'phase_complete' deferred
    job_id=...,
    status=...,                     # 'completed' | 'failed' | 'pending_review' | 'cancelled'
    expert=...,
    project_id=...,
    user_id=...,
    parent_job_id=...,
    created_by_thread_id=...,       # NEW — set only when the job opted in via wake_on_complete (nullable)
    tags=[...],
    priority=...,
    chain_id=...,                   # chain bookkeeping shared with automations' runaway guards
    chain_depth=...,
    timestamp=...,
)
```

**Emission points.** Identical to the requirement [[automations]] already documents: `emit_lifecycle_event(...)` must fire from **all four terminal-state paths** — `complete_job()` (`orchestrator/main.py:~8231`), `approve_job()` (`~6917`), manual cancellation, and the timeout sweeper — **after** the relevant DB writes commit. Emitting after commit is what guarantees a consumer can read the finished job's outputs when it reacts.

**Transport.** In-process `asyncio` pub/sub, multiple subscribers. **Not** `LISTEN/NOTIFY` — inheriting the settled decision from both [[automations]] and [[headless_persistent_sessions]] (the Recall.ai commit-serializing-lock problem). Promotable to NATS / Redis Streams if we ever run multiple orchestrator replicas.

## Session ↔ job linkage

`create_worker_job` already passes `context.thread_id` at creation (`src/tools/orchestrator/jobs.py:208-209`), but it is only used to inherit `user_id` / `project_id` — it is **not persisted as a queryable backref** on the job. Two new columns on `jobs` close that and carry the opt-in:

```sql
ALTER TABLE jobs ADD COLUMN created_by_thread_id UUID REFERENCES threads(id) ON DELETE SET NULL;
ALTER TABLE jobs ADD COLUMN wake_on_complete    BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX idx_jobs_wake_on_complete
    ON jobs (created_by_thread_id)
    WHERE wake_on_complete = true;
```

No new table — a session-wake subscription is just these two columns. **Wake is opt-in per job**, matching the "watcher" mental model (the session explicitly asks to be woken for *this* job; it does not fire on every job a session ever spawned). The opt-in is a new parameter on the tool:

```python
create_worker_job(
    description=...,
    config=...,
    wake_on_complete=True,   # NEW — register this session to be woken when the job finishes
)
```

## The `session_wake_dispatcher`

A new subscriber on the bus (sibling to automations' `event_dispatcher`):

```python
async def _handle(event: LifecycleEvent) -> None:
    # created_by_thread_id is populated on the event only when the job was
    # created with wake_on_complete=true, so its presence IS the opt-in.
    if not event.created_by_thread_id:
        return
    thread = await get_thread(event.created_by_thread_id)
    if thread is None or thread.status == "ended":
        return                                   # never wake a terminal session
    await deliver_completion(thread, event)      # behavior depends on depth + permission_mode
```

`deliver_completion` branches on the session's current physical state — the wake mechanism differs but reuses existing paths entirely:

- **Live** (`active` / `awaiting_user`, pod up): inject a synthetic `job_complete` message into the loop's input queue (`_loop_user_queue` in `src/api/persistent_app.py`). If the session is mid-turn, the event waits for the next natural pause rather than interrupting.
- **Suspended** (snapshotted, pod down): drive the existing headless `suspended → active` restore path (`workspace_suspension_service`), then deliver the completion as the first input on resume.
- **Ended**: do nothing (out of scope to revive a terminated session).

## Phases (the three depths)

Each phase is independently shippable and strictly more capable than the last. The early phases sidestep the risk surfaces (see [Risks & preconditions](#risks--preconditions)); the last one is gated behind fixing them.

### Phase 1 — Bus + linkage + **notify-only**

The foundation plus the cheapest useful behavior.

- [ ] `orchestrator/services/lifecycle_events.py`: `LifecycleEvent`, `emit_lifecycle_event`, in-process pub/sub.
- [ ] Wire `emit_lifecycle_event` into the four terminal-state paths, after DB commit.
- [ ] Migration: `jobs.created_by_thread_id` + `jobs.wake_on_complete` + index.
- [ ] `create_worker_job` accepts `wake_on_complete`; persist both columns on create.
- [ ] `session_wake_dispatcher`: on a matching event, **(a)** append a `job_complete` entry to the thread's event log so the session sees it on next reattach, and **(b)** fire the [[headless_persistent_sessions]] notification fan-out ("your job X finished," with a reattach link). **No agent spin-up.**
- [ ] Tests: job created with `wake_on_complete` → completes → event emitted → log entry written + notification fired; job *without* the flag → nothing.

Phase 1 alone delivers the demo ("you'll get told when it's done") with near-zero new risk — it never resumes a suspended pod and never lets the agent act, so it touches neither the resume-OOM nor the zombie-session surfaces.

### Phase 2 — **Wake-to-notify**

Re-activate a suspended/idle session just far enough to triage, then park.

- [ ] `session_wake_dispatcher` drives the restore path for `suspended` sessions and the queue-inject path for live ones.
- [ ] On resume, the session gets the completion delivered, produces a short summary/triage of the result, then reaches a **natural pause** (text output, no tool call) — which hands straight back to the headless attention-sleep + notification machinery.
- [ ] The agent does **not** approve, continue, or spawn follow-up work autonomously.
- [ ] Coalesce wakes: if several of a session's jobs finish close together, one restore, batched delivery.
- [ ] Tests: suspended session + job completes → restore → summary turn → re-park; concurrent completions coalesce to one wake.

### Phase 3 — **Wake-to-act** (north star)

The session resumes and, per its authority, acts on the result on its own — true async delegation.

- [ ] Permission gate (below): map `permission_mode` to allowed autonomous actions.
- [ ] A woken `autonomous` session may `approve_worker_job` / `resume_worker_job` / continue its own plan / spawn follow-up jobs — subject to sudo hard gates and chain caps.
- [ ] Runaway guards wired (below).
- [ ] Tests: autonomous session auto-approves a creator-owned completed job and continues; supervised session is blocked from auto-acting and instead surfaces to the user.

> **Phase 3 is blocked on two preconditions** — see [Risks & preconditions](#risks--preconditions). Do not ship it before they are met.

## Permission model

Two gates, both reusing existing primitives:

1. **Scope.** A session may only act on jobs where `job.created_by_thread_id == thread.id`. (Orchestrator's job endpoints currently let *any* internal-keyed agent approve *any* job — this feature adds the creator-scoped check for session-initiated actions.)
2. **Authority** — the thread's `permission_mode`:

   | `permission_mode` | Max behavior on a creator-owned completion |
   |---|---|
   | `supervised` | Wake-to-notify only; any action (approve/continue) must be surfaced to the user. No autonomous acting. |
   | `auto_accept` | May auto-approve/continue routine completions; sudo-gated actions still surface. |
   | `autonomous` | Full wake-to-act; reviews/approves/continues on its own, still bounded by sudo hard gates + chain caps. |

This mirrors how [[headless_persistent_sessions]] already reuses autonomy levels to decide "how eager when untethered."

## Risks & preconditions

Honest blockers for the autonomous depth (Phase 3). Phases 1–2 are unaffected.

1. **The session-zombie drain bug is still open.** Agents wedged in `status=session` already dodge every recycler and survive deploys (manual `kubectl delete pod` is the only cleanup as of 2026-06-16; see `docs/done/lifecycle_session_agents_without_thread_never_drain.md` / [[agent_lifecycle]]). A feature that *programmatically re-activates suspended sessions on external events* is a new way to manufacture wedged sessions. **Precondition: the session drain path must be airtight before Phase 3.** Phase 1 avoids this entirely (no resume); Phase 2 resumes but always re-parks via the headless path, so it inherits whatever drain correctness headless has.
2. **Resume is the OOM surface.** Waking a suspended session is the restore path, which has OOM-killed the orchestrator before (the 1Gi→2Gi bump; restore reads the whole snapshot tar into RAM). N jobs finishing at once = N concurrent restores. **Phase 2 must rate-limit / serialize restores and coalesce wakes; Phase 3 inherits that.**
3. **Runaway chains.** A session that wakes, acts, and spawns more jobs is a chain — exactly the `$47K`-loop failure mode [[automations]] already guards. Reuse its guards: `chain_id` / `chain_depth`, `max_chain_depth`, `max_fires_per_day`, per-chain cost cap, and fingerprint-based loop detection. The session is the **chain root**.

## Out of scope

- **Schedule-driven session wake.** Sessions are never woken by cron — that is automations' job-factory direction. Explicitly excluded (it is the failure mode the user flagged).
- **Reviving `ended` sessions.** Wake applies only to `active` / `awaiting_user` / `suspended`.
- **Cross-session wake.** v1 only wakes the *creating* session; waking some other session is not supported.
- **Multi-job fan-in barriers** ("wake me only when *all five* of my jobs finish"). v1 wakes per-job. Delegation's all-siblings-done barrier is the precedent if we want it later — see open questions.
- **Headless budgets.** Per-run / cumulative token caps are their own feature ([[headless_persistent_sessions]] §6 defers them).
- **Bus durability beyond in-process.** NATS/Redis promotion waits for a multi-replica orchestrator.

## Open questions

1. **Fan-in.** Per-job wake is simplest, but "continue once *all* my jobs finish" is a natural ask. Add a lightweight `wait_group` later, or push users to one job with delegated children? Lean: per-job in v1, revisit.
2. **Default for `wake_on_complete`.** Opt-in per job is the safe default. Should sessions that *block on* a job (the user said "continue once it finishes") get it auto-set? Probably yes for that phrasing — needs an agent-prompt convention.
3. **Mid-turn delivery.** Confirmed approach: queue the completion for the next natural pause rather than interrupting an in-flight turn. Validate this doesn't starve under a chatty session.
4. **Reattach UX.** How the woken session presents "job done" — reuse the headless reattach summary ("here's what happened while you were away")? Likely yes.
5. **Bus promotion timing.** In-process is fine single-replica; pin the trigger condition (orchestrator HA) so we don't promote prematurely.

## Related code

- `orchestrator/services/lifecycle_events.py` — **NEW**, the lifted bus (`LifecycleEvent`, `emit_lifecycle_event`, pub/sub).
- `orchestrator/main.py` — `complete_job()` (~8231), `approve_job()` (~6917), manual-cancel path, timeout sweeper: emission points. `_handle_delegation_child_completion()` (5119): the proven wake-the-waiter precedent to mirror.
- `orchestrator/services/workspace_suspension_service.py` — the `suspended → active` restore path the suspended-session wake reuses.
- `src/api/persistent_app.py` — `_loop_user_queue` (inject point for live sessions); `_terminate_session` / restore wiring.
- `src/tools/orchestrator/jobs.py` — `create_worker_job` (add `wake_on_complete`); `:208-209` thread_id pass-through; the existing `approve_worker_job` / `resume_worker_job` / `cancel` / `pause` session tools.
- `orchestrator/database/migrations/app/` — new migration: `jobs.created_by_thread_id` + `jobs.wake_on_complete`.
- `orchestrator/database/schema.sql` — `jobs` table; `threads` table (`permission_mode`, status set).
- [[automations]] — sibling consumer of the bus (event-trigger half); source of the `LifecycleEvent` contract and the runaway guards.
- [[headless_persistent_sessions]] — suspend/restore, notification fan-out, attention-sleep, permission/autonomy reuse.
- [[notify_user_tool]] — SMTP plumbing the notify-only depth delivers through.

## Decision log

- **2026-06-17:** Anchor the feature on [[subagent_delegation]] (wake-the-waiter-on-completion), **not** [[automations]]. The automations relationship is bus-sharing only; sessions are never woken by schedules. (User clarification.)
- **2026-06-17:** Lift the lifecycle event bus to a first-class shared primitive; automations event-triggers become a sibling consumer. Free now because the automations event half isn't built yet. (User decision — "lift it to a higher level and have both systems share/reuse the functionality.")
- **2026-06-17:** Wake is opt-in per job via `wake_on_complete`, not automatic for all session-created jobs. Matches the "watcher" mental model.
- **2026-06-17:** Storage = two columns on `jobs` (`created_by_thread_id`, `wake_on_complete`), no new table.
- **2026-06-17:** `LifecycleEvent` carries `created_by_thread_id` so the session-wake consumer needs no DB round-trip — bus contract co-designed for both consumers from day one.
- **2026-06-17:** Permission via thread `permission_mode`, creator-scoped only.
- **2026-06-17:** Three depths as phases; Phase 3 (wake-to-act) gated behind the open session-zombie drain fix + bounding the resume-OOM path.
- **2026-06-17:** No `LISTEN/NOTIFY` for fan-out (inherit the Recall.ai lesson); in-process pub/sub, NATS-promotable.
- **2026-06-17:** Build deferred (feature-freeze / runway). Document now, resume after the pilots.
