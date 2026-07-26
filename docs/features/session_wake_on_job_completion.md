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
  - "[[unified_tool_cards]]"
---

# Session Wake on Job Completion

> A session launches a worker job ("run a research agent on topic X"), the user closes the tab, and twenty minutes later the job finishes. Today the result just sits silently in the database. This feature lets the session be woken — or at least notified — when a job *it created* completes, so it can continue. It is **subagent delegation, where the parent is an interactive chat session and the wait is asynchronous.**

**Status:** Design phase, **v1 scope pinned 2026-07-26** — not built. Co-designing the shared bus with the [[automations]] event-trigger half remains the end state, but v1 deliberately skips it; see [v1 shortcut](#v1-shortcut--no-bus-for-a-live-session).
**Filed:** 2026-06-17 · **Revised:** 2026-07-26

## Motivation

A session can already create worker jobs (`create_worker_job`, `src/tools/orchestrator/jobs.py`). The missing half is the *return path*: when that job finishes, nothing tells the session. The orchestrator flips a status column in the database and goes quiet — no event, no broadcast (confirmed: job completion is a pure DB write; the session's only option today is to sit in a polling loop calling `get_worker_job`, which burns turns and does not survive the session going idle).

The concrete flow we want:

1. A session spins up a job and tells the user "I'll continue when this finishes."
2. The user closes the browser. The session goes idle and is suspended to storage ([[headless_persistent_sessions]] already does this).
3. The job completes.
4. The session is woken (or the user is notified), the result is delivered, and work continues.

This is **not** schedule-driven. We are emphatically *not* waking sessions on a cron tick — a weekly automation firing should never spin up a chat session. This feature is the opposite end of the pipe from [[automations]]: automations *create* jobs when something happens; this feature *reacts* to one specific job — the one this session created — finishing.

### The return path is also the scheduler

Framing this as "a notification feature" undersells it. Every job a session
creates dispatches immediately — `create_job` ends in `_trigger_dispatch()`
(`orchestrator/main.py:8447`) — and there is no "start after" for ad-hoc
session-created jobs. So a session that lays out a real plan cannot execute it.
A concrete one, produced unprompted by a live session on 2026-07-25:

```
Three parallel Designer theme jobs
        ↓
One comparative Critic job
        ↓
Human theme selection
        ↓
One Designer style-guide job
        ↓
Human approval
        ↓
Developer implementation job
```

— with the session's own caveat: *"I would not run this job at the same time as
theme exploration because it would lock in decisions too early."*

Its only options today are to fire all six at once (wrong, and expensive) or to
hold the plan and launch each stage when the previous one lands. The second
requires exactly this feature. **The wake is what makes multi-stage session work
possible without a workflow engine** — the conversation becomes the scheduler.

Two consequences worth stating up front:

- **Do not build job dependency graphs.** The gates in a real plan are *human*
  gates ("Human theme selection", "Human approval"). A DAG engine would automate
  away the part the user is there for. See [Out of scope](#out-of-scope).
- **A worker job is not a subagent.** Three jobs can be north of 10k LLM
  requests. That cost asymmetry is why the per-job wake is a *cost-control
  lever*, not overhead: it is the only point at which a batch heading in the
  wrong direction can be stopped — a woken session holds `cancel_worker_job`
  and can kill the siblings before they spend. It also drives the fan-in
  decision below.

### Why this is mostly assembly, not invention

The three hard problems are each already solved by a sibling feature:

| Problem | Already solved by | Mechanism |
|---|---|---|
| Wake a waiter when a job it spawned completes | [[subagent_delegation]] | `_handle_delegation_child_completion()` (`orchestrator/main.py:11425`) transitions the parent `waiting → paused → dispatch` and resumes it. Checkpoint + wake, not long-poll. |
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

**Emission points.** Identical to the requirement [[automations]] already documents: `emit_lifecycle_event(...)` must fire from **all four terminal-state paths** — `complete_job()` (`orchestrator/main.py:13606`), `approve_job()` (`:10636`), manual cancellation, and the timeout sweeper — **after** the relevant DB writes commit. Emitting after commit is what guarantees a consumer can read the finished job's outputs when it reacts.

**Transport.** In-process `asyncio` pub/sub, multiple subscribers. **Not** `LISTEN/NOTIFY` — inheriting the settled decision from both [[automations]] and [[headless_persistent_sessions]] (the Recall.ai commit-serializing-lock problem). Promotable to NATS / Redis Streams if we ever run multiple orchestrator replicas.

### v1 shortcut — no bus for a live session

Verified against the tree 2026-07-26: for a session that is **live** (pod up),
every piece of delivery plumbing already exists.

- The orchestrator already POSTs agent pods directly, and has for a long time:
  `/job/start` (`orchestrator/main.py:2648`), `/job/resume` (`:2883`),
  `/job/pause` (`:2967`), `/session/attach` (`:3164`).
- The session agent already exposes `POST /api/input`
  (`src/api/persistent_app.py:2455`) → `_accept_user_input` (`:2500`), which
  persists the message *before* acknowledging and then enqueues it onto
  `_loop_user_queue` (`:2041`). The persist-then-enqueue order is deliberate
  (`session_silent_failure_audit.md` #1) and is exactly the durability an
  injected completion wants.
- `thread_messages.role` is an unconstrained `VARCHAR(20)`
  (`migrations/app/0001_initial.sql:1041`) — a third role for an injected
  completion needs no migration on *that* table.

So **v1 is a direct call from the completion path, not a bus.** The bus stays
the end state (it is what the [[automations]] event half subscribes to), but it
is not on v1's critical path and nothing here blocks promoting to it later —
the emission point is the same line in `complete_job` either way.

## Session ↔ job linkage

`create_worker_job` already passes `context.thread_id` at creation
(`src/tools/orchestrator/jobs.py:569`), but it is only used to inherit
`user_id` / `project_id` / datasources — it is **not persisted as a queryable
backref** on the job. That absence is also why a session cannot ask "which jobs
did *I* create" today: `list_worker_jobs` filters by status only. Two new
columns on `jobs` close it:

```sql
ALTER TABLE jobs ADD COLUMN created_by_thread_id UUID REFERENCES threads(id) ON DELETE SET NULL;
ALTER TABLE jobs ADD COLUMN wake_on_complete    BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX idx_jobs_wake_on_complete
    ON jobs (created_by_thread_id)
    WHERE wake_on_complete = true;
```

No new table — a session-wake subscription is just these two columns.

**Revised 2026-07-26: wake is not opt-in per job.** The original design made
`wake_on_complete` a tool parameter the model sets per call ("watcher" mental
model). Dropped, because its failure mode is silent: the agent forgets the flag
and then simply never learns its job finished — indistinguishable from the bug
this feature exists to fix. An *unwanted* wake, by contrast, costs one cheap
turn that the agent immediately goes back to sleep from.

So v1 wakes for **every job a session created**. The column stays — it is cheap,
it is the natural off-switch for a later user setting ("don't wake me for jobs
in this project"), and it keeps the event contract stable — but it is set
server-side at create: `true` when the creator is a session thread, `false`
otherwise. No new tool parameter, nothing for the model to remember.

## The `session_wake_dispatcher`

A new subscriber on the bus (sibling to automations' `event_dispatcher`):

```python
async def _handle(event: LifecycleEvent) -> None:
    # created_by_thread_id is populated for any session-created job (v1 sets
    # wake_on_complete server-side); its presence IS the subscription.
    if not event.created_by_thread_id:
        return
    thread = await get_thread(event.created_by_thread_id)
    if thread is None or thread.status == "ended":
        return                                   # never wake a terminal session
    await deliver_completion(thread, event)      # behavior depends on depth + permission_mode
```

`deliver_completion` branches on the session's current physical state — the wake mechanism differs but reuses existing paths entirely:

- **Live** (`active` / `awaiting_user`, pod up): `POST /api/input` on the agent pod, which persists then enqueues onto `_loop_user_queue` (`src/api/persistent_app.py:2455`, `:2500`). If the session is mid-turn, the event waits for the next natural pause rather than interrupting. **This is the whole of v1.**
- **Suspended** (snapshotted, pod down): drive the existing headless `suspended → active` restore path (`orchestrator/services/workspace_suspension.py`), then deliver the completion as the first input on resume. **Deferred to Phase 2** — until then a suspended session sees the completion in its event log on reattach.
- **Ended**: do nothing (out of scope to revive a terminated session).

### The delivery payload carries the sibling set

Once `created_by_thread_id` exists the orchestrator knows every outstanding job
of the waking thread, so the notice says

> job 2 of 3 outstanding finished — 1 still running, 1 failed

rather than a bare "job X done". This is not cosmetic: without it the agent must
spend a `list_worker_jobs` round-trip on *every* wake just to decide whether
this is the moment to act. With it, the decision is free.

### What the agent does with a wake

The convention is a **decision at each wake, not a barrier**: inspect the result
now, or note it and go back to sleep until the next one lands. The agent chooses,
and it speaks up when there is something worth saying — a result that changes the
plan, or a failure. Three quiet acknowledgements in a row is a fine outcome; so
is one wake that reads the first result, concludes the batch is heading the wrong
way, and cancels the siblings.

What it must *not* become is a status feed: narrating all six completions of a
fan-out turns the conversation into a job queue and defeats the point. That is a
prompt convention, not a mechanism.

## Phases (the three depths)

Each phase is independently shippable and strictly more capable than the last. The early phases sidestep the risk surfaces (see [Risks & preconditions](#risks--preconditions)); the last one is gated behind fixing them.

### Phase 1 — Linkage + live delivery  ← **this is v1** (pinned 2026-07-26)

Scope narrowed from the original "bus + notify-only": the bus is dropped from
the critical path (see [v1 shortcut](#v1-shortcut--no-bus-for-a-live-session)),
and delivery to a *live* session is included rather than deferred, because it is
the cheap half and it is the half the workflow actually needs.

- [ ] Migration: `jobs.created_by_thread_id` + `jobs.wake_on_complete` + partial index.
- [ ] `create_job` persists both — `created_by_thread_id` from the `thread_id`
      the tool already sends, `wake_on_complete` defaulted server-side. No tool
      signature change.
- [ ] On completion, resolve the owning thread and deliver:
      **live** → `POST /api/input` on the agent pod;
      **suspended / detached** → append to the thread event log so it lands on
      reattach, plus the existing [[headless_persistent_sessions]] notification
      fan-out. **No pod restore, no agent spin-up for a suspended session.**
- [ ] Delivery payload carries the sibling set (see above).
- [ ] Prompt convention for what the agent does with a wake — inspect vs. defer,
      and do not narrate every completion.
- [ ] Tests: session-created job completes → live session receives the input;
      thread suspended → log entry + notification, no restore; thread `ended` →
      nothing; a job with no `created_by_thread_id` → nothing.

Phase 1 delivers the actual workflow (schedule → keep working → get woken →
inspect or defer → schedule the next stage) while touching neither the
resume-OOM nor the zombie-session surfaces: it never resumes a suspended pod and
never lets the agent act without the user present.

**Precondition — the tools are off by default.** `config/session_base.yaml:119`
sets `orchestrator: [ ]`, and `assistant` (the default expert for new sessions)
inherits it unchanged; no bundled config enables the group. It is a user-facing
toggle — "Fleet Management" in Agent Settings, and a *live* one
(`LIVE_TOOL_CATEGORIES`, `cockpit/.../agent-settings.types.ts:37`). So a default
session cannot create jobs at all, and this feature is invisible to it. Whether
to flip the default for `assistant` is a separate product call — nine tool
schemas are permanent per-request context and job creation spends real money —
but it needs deciding, or v1 ships dark.

### Phase 2 — **Wake-to-notify**

Re-activate a suspended/idle session just far enough to triage, then park. The
live half moved into Phase 1 (2026-07-26), so what remains here is exactly the
suspended case — which is also where the resume-OOM risk lives.

- [ ] `session_wake_dispatcher` drives the restore path for `suspended` sessions (live sessions are already handled by Phase 1's direct inject).
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
- **Multi-job fan-in barriers** ("wake me only when *all five* of my jobs finish") — **decided against, not merely deferred** (2026-07-26). The obvious v1 need looks like a barrier: schedule three designer jobs, wait for all three, review, schedule the next stage. Per-job wake instead means three wakes for one decision point, two of which may do nothing. That is the right trade:
  - the two "extra" wakes are not waste — each is a genuine early look, and with `cancel_worker_job` in hand the agent can stop a batch heading the wrong way *before* the siblings spend their share of 10k+ requests. A barrier would suppress exactly the moment worth having;
  - a failed job surfaces immediately instead of at batch end;
  - a barrier over a job that never reaches a terminal state is a hang; per-job degrades gracefully;
  - no wait-group table, no partial-failure or cancelled-member semantics.
- **Job dependency graphs / a workflow engine.** The sequencing gap is real (see [The return path is also the scheduler](#the-return-path-is-also-the-scheduler)) but the answer is the wake, not a DAG. The gates in real plans are human gates; automating them away removes the reason the user is in the loop, and chaining 10k-request jobs unattended is how you spend 100k requests walking in the wrong direction.
- **Headless budgets.** Per-run / cumulative token caps are their own feature ([[headless_persistent_sessions]] §6 defers them).
- **Bus durability beyond in-process.** NATS/Redis promotion waits for a multi-replica orchestrator.

## Open questions

*Resolved 2026-07-26: fan-in (now a decision under [Out of scope](#out-of-scope)) and the `wake_on_complete` default (now server-side, under [Session ↔ job linkage](#session--job-linkage)).*

1. **Who is speaking?** An injected completion must not render as a *user*
   message — the transcript would lie and the agent would think the user spoke.
   `thread_messages.role` is unconstrained so a third role is free; the open part
   is what the cockpit does with it. Leading candidate: no new message at all —
   transition the existing `create_worker_job` tool card in place to its returned
   state. See [[unified_tool_cards]].
2. **Card rendering for a fan-out.** `create_worker_job` has no entry in
   `cockpit/src/app/core/tools/tool-descriptors.ts` today, so it renders as the
   generic card. Three jobs from one turn: three cards, or one grouped card with
   three rows (the `Delegate-A-Compact-List-Rows` mockup)? Grouping the calls
   within a single assistant turn client-side would get the grouped look with no
   backend concept — needs checking whether the renderer can see sibling calls.
3. **Mid-turn delivery.** Confirmed approach: queue the completion for the next natural pause rather than interrupting an in-flight turn. Validate this doesn't starve under a chatty session.
4. **Reattach UX.** How the woken session presents "job done" — reuse the headless reattach summary ("here's what happened while you were away")? Likely yes.
5. **Bus promotion timing.** In-process is fine single-replica; pin the trigger condition (orchestrator HA) so we don't promote prematurely.

## Related code

*Line numbers re-verified 2026-07-26; the pre-revision refs had drifted badly.*

- `orchestrator/services/lifecycle_events.py` — the lifted bus. **Not on v1's path** (see the v1 shortcut); confirmed non-existent, and `emit_lifecycle_event` appears only in design docs.
- `orchestrator/main.py` — `complete_job()` (`:13606`), `approve_job()` (`:10636`), manual-cancel path, timeout sweeper: emission points. `_handle_delegation_child_completion()` (`:11425`): the proven wake-the-waiter precedent to mirror. `create_job()` (`:8077`), ending in `_trigger_dispatch()` (`:8447`) — why there is no "start after". Agent-pod POST precedent: `:2648`, `:2883`, `:2967`, `:3164`.
- `orchestrator/services/workspace_suspension.py` — the `suspended → active` restore path the Phase 2 wake reuses. (Was cited as `workspace_suspension_service.py`; no such file.)
- `src/api/persistent_app.py` — `POST /api/input` (`:2455`) → `_accept_user_input` (`:2500`) → `_loop_user_queue` (`:2041`): the v1 inject point for live sessions.
- `src/tools/orchestrator/jobs.py` — `create_worker_job` (`:493`), `thread_id` pass-through (`:569`); the existing `approve_worker_job` / `resume_worker_job` / `cancel` / `pause` / `get_job_workspace_file` session tools the woken agent acts through.
- `config/session_base.yaml:119` — `orchestrator: [ ]`; the default-off precondition.
- `orchestrator/database/migrations/app/0001_initial.sql:1041` — `thread_messages.role` is an unconstrained `VARCHAR(20)`.
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
- **2026-07-26:** v1 scope pinned to Phase 1 = **linkage + live delivery**. The bus leaves the critical path: for a live session the plumbing already exists end to end (orchestrator→pod POSTs, `/api/input`, persist-then-enqueue, unconstrained message role), so v1 is a direct call. The bus stays the end state for the [[automations]] event half; the emission point is the same line either way.
- **2026-07-26:** **No fan-in barrier** — decided against, not deferred. Per-job wake, and the agent does its own waiting. The "extra" wakes are the feature, not overhead: each is an early look at a 10k-request batch, and the woken agent holds `cancel_worker_job`. A barrier would suppress the only moment a wrong-direction batch can be stopped, and would hang on a job that never terminates. (User decision — "I would wake the AI after every job. It can check it out or go back to sleep until the next finishes.")
- **2026-07-26:** **`wake_on_complete` is no longer a tool parameter.** Set server-side: true for session-created jobs. Opt-in's failure mode is silent (agent forgets the flag → never learns), while a surplus wake costs one cheap turn. Column retained as the off-switch for a future user setting. Reverses the 2026-06-17 opt-in decision.
- **2026-07-26:** Delivery payload carries the **sibling set**, so the agent decides inspect-vs-defer without a `list_worker_jobs` round-trip on every wake.
- **2026-07-26:** Reframed motivation: the return path **is the scheduler**. Session-created jobs all dispatch immediately, so multi-stage plans are currently unexecutable; the wake is what makes them possible. Corollary — **do not build job dependency graphs**; the gates in real plans are human gates.
- **2026-07-26:** Recorded the default-off precondition (`orchestrator: [ ]` in `session_base`, "Fleet Management" toggle). Flipping the `assistant` default is a separate product call, but without it v1 ships dark.
- **2026-07-26:** The **proposal seam** (agent drafts a job config → user reviews settings → user starts) stays out of this feature and is deferred generally. A per-job Start/Edit/Discard card cannot express the ordering or the human gates in a real multi-stage plan — making it honest means building a DAG editor — and the retired "builder" already established that nobody read the generated configs. Prose proposal in chat plus "go" is both cheaper and better. Salvaged piece: show the *resolved* config read-only on the created card, for auditability without a draft state.
