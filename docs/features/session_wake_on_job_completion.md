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

**Status:** **Phase 1 SHIPPED + LIVE-GATED.** Implemented 2026-07-26
(`320cc112`), gated on dev 2026-07-27 — all five checks passed; see
[Live verification](#live-verification--passed-on-dev-2026-07-27). One defect
found by the gate (the durable branch's out-of-band notification never sent) is
fixed but **not yet deployed**. Phases 2–3 not started. Co-designing the shared
bus with the [[automations]] event-trigger half remains the end state, but v1
deliberately skips it; see
[v1 shortcut](#v1-shortcut--no-bus-but-the-direct-post-is-not-the-mechanism).
**Filed:** 2026-06-17 · **Revised:** 2026-07-26

> **Read this first if you are implementing.** The 2026-07-26 audit overturned
> several things the original design assumed. In descending order of "would have
> shipped a bug": don't leader-gate the completion hook; there is no terminal-state
> choke point to hook; the `thread_events` fallback is not viable; `ended` threads
> are resumable and must not be skipped; adding the FK opens a cross-tenant hole on
> the public job-create path; and a new `thread_messages.role` is silently dropped
> on pod recycle unless `_db_rows_to_lc_messages` is taught about it.

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

**Emission points — the original "all four terminal-state paths" is wrong.**
Corrected 2026-07-26 after an audit: **there is no terminal-state choke point in
this codebase**, and the real count is ~4× four. Beyond `complete_job()`
(`orchestrator/main.py:13606`) and `approve_job()` (`:10636`), a job also reaches
a terminal state via:

| Path | Site | Has a CAS claim? |
|---|---|---|
| Cancellation | `main.py:8725` → `postgres.cancel_job` | **yes** (`WHERE status NOT IN ('completed','cancelled')`) |
| Cascade-cancel of children | `main.py:8797` | no — set-based multi-row UPDATE, N rows terminal with zero hooks |
| **Critic-approved target** | `_set_target_to_autonomy_status` `main.py:11134` | no — **the job reaches `completed` with no `/complete` call of its own** |
| Diff accept / reject | `main.py:15463`, `:15514` | no |
| Dispatch-time grant/config failures | 12 sites (`:2432`, `:2442`, `:2483`, `:2570`, `:2619`, `:2750`, `:2841`, `:2853`, `:5264`, `:5340`, `:5618`, `:5666`) | no |
| Stale-verification reap | `cancel_stale_verification_subjobs` | no |
| Delegation timeout sweeper | `main.py:11669` | yes |
| LLM-outage fail | `fail_llm_outage_job` | yes |
| VM-upgrade approval expiry | `main.py:927` | predicate, unchecked |

Missing one means a session that waits forever — **indistinguishable from the
bug this feature exists to fix**. The repo has been bitten by exactly this class
twice already (`main.py:11815-11822` documents one: *"A sweep-fail is a direct DB
write — no /complete ever fires, so the subjob unblock handlers never run."*).

So the shape is **not** "hook the terminal paths." It is: one
`maybe_wake_session(job, terminal_status)` decision function called from the
paths we do cover, **plus a sweeper backstop** that catches everything else by
polling for unfired claims. Model the backstop on
`orchestrator/services/project_loop_sweeper.py` — read its module docstring
first; it documents a live two-replica double-spawn incident and the age-grace
that fixed it.

**Transport.** In-process `asyncio` pub/sub, multiple subscribers.

**Not `LISTEN/NOTIFY` — but the original rationale was an overreach.** The
inherited reasoning cited the Recall.ai commit-serializing-lock problem. That
lock is real (it is in `src/backend/commands/async.c`, an `AccessExclusiveLock`
on "database 0" held through commit, and Tom Lane confirms upstream that holding
it through commit "is the true scalability blockage"), **but the incident behind
it involved tens of thousands of concurrent writers.** At two replicas it is
irrelevant, and a reviewer could rightly knock the argument down. Note also that
sources disagree on whether it is fixed: the PG 19 commit often cited as the fix
(282b1cde) addresses the *listener-wakeup* fan-out, not the commit lock.

The rationale that actually holds:

- **No durability for absent listeners.** Notifications go only to *currently
  listening* sessions. A replica restarting or mid-rollout misses the event
  permanently, with no replay. That alone disqualifies it as the mechanism for a
  must-happen side effect.
- **Wrong fan-out shape.** Both replicas listening both receive it, so you still
  need single-firing on top; one replica listening makes that replica a SPOF.
- **Silent coalescing:** identical payloads on one channel in one transaction are
  delivered once.
- The queue is 8 GB and **transactions calling NOTIFY fail at commit when it
  fills** — a stuck listener can take down all writers on that channel.

LISTEN/NOTIFY would be legitimate only as a *latency hint* ("wake the poller
now") layered over a durable claim. Given a 20-second poll and the post-commit
fast path below, it buys nothing for the cost of a persistent listener
connection and reconnect handling. Skip it.

### v1 shortcut — no bus, but the direct POST is *not* the mechanism

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
- `/api/input` requires **no authentication at all** — no session token, no
  internal key; the agent NetworkPolicy is egress-only. Convenient, but it means
  the wake is unauthenticated and unattributed. Do not build anything on the
  agent *trusting* that a notice came from the orchestrator.

**The correction that matters:** a direct POST from the completion path cannot be
the mechanism, because nothing here is transactional. `postgres_db.acquire()`
yields a raw pooled connection and there are **zero** `conn.transaction()` calls
in `main.py` — every statement autocommits. So "emit after commit" is trivially
satisfied, and equally trivially lost: a SIGKILL during a rolling deploy, an
OOMKill, or a swallowed exception between the status write and the POST loses
the wake permanently and *silently*. Brandur Leach's framing of this dual-write
window is the one to keep in mind — it is "far more nefarious [because] you
almost certainly won't notice when it happens."

So the mechanism is a **durable claim** (next section), and the post-commit POST
is a **latency optimization layered on top of it**. Because the fast path goes
through the same atomic claim, a lost fast path is invisible — the backstop
poller re-claims the row after a visibility timeout. This inversion is the single
most important structural point in the design.

The bus stays the end state (it is what the [[automations]] event half
subscribes to), but it is not on v1's critical path, and the emission point is
the same line either way.

## Session ↔ job linkage

`create_worker_job` already passes `context.thread_id` at creation
(`src/tools/orchestrator/jobs.py:569`), but it is only used to inherit
`user_id` / `project_id` / datasources — it is **not persisted as a queryable
backref** on the job. That absence is also why a session cannot ask "which jobs
did *I* create" today: `list_worker_jobs` filters by status only. Two new
columns on `jobs` close it:

**Migration shape, corrected 2026-07-26.** The inline `ADD COLUMN … REFERENCES`
above is not how this repo adds an FK to `jobs`, and `CREATE INDEX` and
transactional DDL may not share a file. **Two migrations**, `0070` and `0071`
(`0069_automation_expert_id.sql` is current head; `0066` is a permanent gap —
leave it):

`0070_jobs_created_by_thread.sql` (transactional) — copy the two-phase FK shape
from `0028_experts.sql:58-65`, which added `jobs.expert_id` for exactly this
case and is written that way specifically to keep squawk green:

```sql
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS created_by_thread_id UUID;
DO $$ BEGIN
    ALTER TABLE jobs ADD CONSTRAINT jobs_created_by_thread_id_fkey
        FOREIGN KEY (created_by_thread_id) REFERENCES threads(id)
        ON DELETE SET NULL NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE jobs VALIDATE CONSTRAINT jobs_created_by_thread_id_fkey;

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS wake_on_complete BOOLEAN NOT NULL DEFAULT false;
-- claim columns — see "The claim" below
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS wake_state       TEXT NOT NULL DEFAULT 'none';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS wake_claimed_at  TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS wake_attempts    INTEGER NOT NULL DEFAULT 0;
```

`0071_jobs_wake_pending_idx.notx.sql` — `CREATE INDEX CONCURRENTLY IF NOT EXISTS`,
single statement, with the `-- runbook:` INVALID-index recovery note copied from
`0046_jobs_dispatchable_partial_idx.notx.sql:40-47` (`IF NOT EXISTS` silently
no-ops over an INVALID index left by an interrupted build).

`ON DELETE SET NULL` is not cosmetic: threads are genuinely hard-deleted
(`postgres.py:delete_thread`), so without it those deletes start failing.

Two CI gates that will bite otherwise: the `artifact` job replays every migration
and regenerates `schema_current.sql`, failing if it differs — **run
`scripts/schema-snapshot.sh` and commit the result in the same commit**. And per
`docs/issues/ci_migration_lint_bypassed_by_deploy.md`, once a migration applies
on develop its checksum is frozen; a squawk warning found afterwards cannot be
fixed by editing the file, only by permanently excluding it from the lint. Get
squawk clean *before* the push lands.

### ⚠ Adding the FK opens a cross-tenant wake-injection hole — fix in the same commit

The two job-creation paths validate `thread_id` asymmetrically:

- **Internal path** (`X-Internal-Key`, what the session agent uses) — validated.
  `_resolve_internal_job_creation_scope` (`main.py:7910-7936`) fetches the thread
  and 403s if it is missing or the user does not match. Safe.
- **Public / cockpit path** (`require_approved_user`) — **`job.thread_id` is
  never fetched or checked.** `_strip_public_job_reserved_markers`
  (`main.py:4221-4238`) nulls `parent_job_id`, `creation_order`, `worktree_path`
  and `delegation_context` — but *not* `thread_id`. Its only downstream use
  swallows lookup failures, so today a bogus or someone else's thread id is
  silently ignored.

Once we persist it and wake on it, "silently ignored" becomes an attacker
choosing a victim's live session and having a completion payload POSTed into it.
There is a second, lesser consequence: a valid-but-nonexistent UUID becomes a
`ForeignKeyViolationError` → HTTP 500 where it used to be a no-op.

**Fix:** add `thread_id` to `_strip_public_job_reserved_markers` — consistent
with every other system-only marker, and correct because the cockpit has no
reason to send it. If the cockpit ever must, validate thread existence **and**
`thread.user_id == caller.id` instead. No test covers public-path `thread_id`
today; add one.

### The claim

The wake is a **non-idempotent network send** — `_accept_user_input` mints a
fresh `msg_<uuid4>` per call and unconditionally enqueues, so a duplicate is a
visible message in the user's transcript *plus* a second paid LLM turn. It
therefore needs a claim, and the claim must be taken **before** the send.

We inherit no claim from the delegation precedent. `_handle_delegation_child_completion`'s
entire dedup is one line — `claim_delegation_resume`, a CAS on the *waiter's* row
(`WHERE status='waiting' RETURNING id`), where the legal `waiting → paused`
transition *is itself* the already-woken flag. A thread has no equivalent: it is
legitimately `active` before, during and after a wake. **The claim must be
materialized.**

Claim on the `jobs` row (single-message-per-row outbox — same correctness as a
full outbox table, no new table, and it is durable and inspectable in a way a
lock is not):

```sql
UPDATE jobs j
   SET wake_state = 'sending',
       wake_claimed_at = now(),
       wake_attempts = wake_attempts + 1
  FROM (SELECT id FROM jobs
         WHERE wake_state = 'pending'
            OR (wake_state = 'sending' AND wake_claimed_at < now() - interval '2 minutes')
         ORDER BY updated_at
         FOR UPDATE SKIP LOCKED
         LIMIT 20) s
 WHERE j.id = s.id
RETURNING j.id, j.created_by_thread_id, j.status, j.wake_attempts;
```

Atomicity is guaranteed by documented Read Committed semantics, not luck: a
concurrent `UPDATE` re-evaluates its `WHERE` against the *new* row version, so
the loser matches nothing and gets zero rows back. Exactly one replica wins.

**Two hard rules.** Commit the claim, *then* make the HTTP call — never hold the
transaction open across it (Brandur measured ~15× lock-acquisition degradation
and ~100k dead tuples/hour from exactly that mistake). And keep the index partial
on the unfired predicate so it holds ~0 rows at steady state:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_wake_pending
    ON jobs (updated_at)
    WHERE wake_state IN ('pending', 'sending');
```

Cap `wake_attempts` (8, exponential backoff) → `wake_state='dead'`, and alert on
`count(*) WHERE wake_state='dead'`. That single metric is the whole observability
story.

**Do not leader-gate the completion hook.** `_trigger_dispatch`'s leader gate
(`main.py:5886`) sits right next to where the hook goes and invites a
copy-paste — but it exists because *dispatch is a singleton*, not for dedup. The
agent's `/complete` POST is already load-balanced to exactly one replica, so
leader-gating the wake would fire it only when the agent happens to hit the
leader and **silently drop ~50% of wakes**. This is the most likely wrong turn
in the whole design.

**Do not reuse `claim_sent_notification` as-is.** It conflicts on
`(request_id, kind)`; with a NULL `request_id` the arbiter never matches (NULLs
are distinct), so `DO NOTHING` never fires, the INSERT always returns an id, and
the guard always reports "I won" — zero dual-replica protection while looking
correct. Its *shape* (claim-before-send, downgrade-on-failure, outcome-log-as-
suppression-list) is right; its arbiter is not.

Dedup key is `(job_id, terminal_status)`, keyed per status so that
`pending_review → completed` via approve is a second, legitimate wake. Do **not**
use `jobs.completed_at` as the key — it is set by an unguarded separate
statement, re-set by `approve_job`, and not set at all by
`update_job_status(status='failed')`.

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

- **Live** (`active` / `awaiting_user`, pod up): `POST /api/input` on the agent pod, which persists then enqueues onto `_loop_user_queue` (`src/api/persistent_app.py:2455`, `:2500`). If the session is mid-turn, the event waits for the next natural pause rather than interrupting. **This is the whole of v1's fast path.**
- **Suspended / detached** (pod down): write the notice durably (below) so it lands on reattach, plus the user-facing notification. **No pod restore in v1** — that is Phase 2, and it is where the resume-OOM risk lives.
- **Ended**: **not a do-nothing case** — corrected 2026-07-26. An `active` thread whose pod dies is marked `ended`, not `suspended` (`postgres.py:5113-5143`), and ended threads are **user-resumable** (`main.py:21967`). Treating `ended` as terminal would silently drop completions for a supported case. Take the durable branch; only a thread the user has explicitly finished with should be skipped.

### Liveness: there is no reusable predicate, and the obvious helper is a trap

Fourteen inline thread→pod resolutions exist across four different predicates;
`main.py:21752` says so in a comment. Two specific hazards:

**Do not reuse `_resolve_thread_for_forwarding`** (`main.py:22437`). It looks
exactly right and is wrong for three reasons: it **restores suspended workspaces
as a side effect** (`:22460-22467`) — the Phase-2 resume-OOM path Phase 1
explicitly promises not to touch; it omits the `status != 'offline'` guard every
job path applies; and it requires a `user` dict for owner-auth with no
internal-caller variant.

**Copy `GET /api/sessions/{thread_id}/connection`** instead
(`orchestrator/routers/sessions.py:365-419`) — the only path that combines DB
state, a live probe, and self-healing: `thread.agent_id` non-null → agent exists
and not `offline` (else clear the stale binding) → `pod_ip` present → status in
`(ready, working, session)` → `probe_ready(pod_ip, pod_port)`
(`services/session_lifecycle.py:77-93`, 2 s `GET /ready`). The probe is not
optional: `agent.status` is heartbeat-driven and lags reality by **up to ~4
minutes** (`mark_stale_agents_offline`, 3-minute timeout on a 60 s tick), and
heartbeat freshness is deliberately *not* used against zombies because zombies
heartbeat normally.

**`awaiting_user` races a sweeper.** `mark_orphaned_threads_suspended`
(`postgres.py:5145-5175`) rewrites exactly `awaiting_user`/`suspended` threads
with an offline agent to `suspended` with `agent_id = NULL`, on a 60 s tick. So
`awaiting_user` is precisely the state that can flip out from under a delivery
attempt. Delivery must re-read thread+agent *inside* the attempt and treat a 503
as "fall back to the durable branch," never as an error.

Use the defensive `int(agent.get('pod_port') or 8001)` form, and a split
`httpx.Timeout(read, connect=3.0)` — a flat 30 s against a black-holed pod IP
burns 30 s in the completion path. `_detach_agent_session` (`main.py:4662`) is
the right shape to copy for a background call: status gate, short connect,
swallow everything, return False.

### The durable branch is `thread_messages`, not `thread_events`

Corrected 2026-07-26 — **the original event-log fallback is not viable.**
`thread_events` is a single-writer table whose `seq` is allocated **by the agent,
in process memory**, monotonic per `(thread_id, epoch)`. Two failure modes:
appending into a *live* epoch from the orchestrator silently destroys a batch of
the agent's frames; appending into a *suspended* epoch is unreachable anyway,
because replay is hard-scoped to `threads.events_epoch`, which is bumped
unconditionally on every attach.

Write the notice to **`thread_messages`** instead: `BIGSERIAL` allocated by the
database, no retention prune (`thread_events` is 7 d active / 24 h ended — and
an `ended` thread drops to 24 h, which a "user comes back Monday" case would
blow through), and the orchestrator already writes that table
(`postgres.py:5404`).

**The user-facing half lives elsewhere.** What the *cockpit* shows while a job
runs and when it lands — the live job card, and the Approve /
Resume-with-feedback / Open-diff actions on its returned state — is
[[unified_tool_cards]] slice 4. The two are **independent**: the cockpit can
watch a job with nothing but its id, so the card ships with or without this
feature, and vice versa. This doc is only about the *agent* learning. They meet
at one point: the leading answer to "how is a completion rendered" is to
transition the card in place rather than emit a new message (open question #1).

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
prompt convention, not a mechanism — and §"Where the convention lives" says where.

## The injected message

### Role: `HumanMessage` to the model, `role='event'` in the transcript

Two sides, both precedented.

**To the model — a durable `HumanMessage` at the history tail.** The codebase has
two structural families of injected content: *transient* (todos, memory,
knowledge, citation feedback — re-rendered every request, tail-anchored so the
cacheable prefix stays byte-identical, and deliberately excluded from
summarization) and *durable* (`[JOB_FROZEN]`, `[JOB_COMPLETED]`,
`[CRITIC_VERDICT]`, `[TRANSITION_REJECTED]`, `[FEEDBACK_RESUME]` — real messages
that survive compaction). **A job completion is a one-time fact, so it is
durable.** Do not use the synthetic `AIMessage`+`ToolMessage` pair pattern: that
is the transient family, it would be stripped from summarization, and it inherits
the Gemini function-call-ordering constraint. Do not use `SystemMessage` either —
there is exactly one and it is rebuilt from config every turn.

**To the transcript — a new `thread_messages.role = 'event'`.** The precedent is
exact and already shipped: `role='summary'` and `role='error'` are
non-conversational roles written straight to `thread_messages`, and the cockpit
already branches on both (`summary` → compaction divider, `error` → system line).
`role` is an unconstrained `VARCHAR(20)`, so no migration on that table.

Reusing `/api/input` unchanged is **not** acceptable: `_accept_user_input`
hardcodes `HumanMessage`, `_ROLE_MAP` maps it to `"human"`, and the row then
renders as a **user bubble** — the "transcript would lie" failure this open
question was about.

**Minimum change:** add a role/kind parameter to `_accept_user_input` so the
persisted row is `role='event'` while the in-memory message stays a
`HumanMessage`. That preserves persist-then-enqueue durability and fixes the
lying transcript in one edit — and mid-turn deferral then needs no new code at
all, because the queue is already drained only at the top of the loop.

**Three traps that will silently break it:**

1. `src/database/postgres_db.py:401` filters `role NOT IN ('summary','error')` on
   history reload. Leave `'event'` **out** of that exclusion — we want it in
   context.
2. `_db_rows_to_lc_messages` (`src/api/persistent_app.py:4830-4876`) is an
   `if/elif/elif` over `human|user`, `ai|assistant`, `tool` with **no else
   branch**. An unhandled role is silently dropped, so the notice would vanish
   from the model's context on the next pod recycle while remaining in the DB and
   the UI. Add `elif role == "event"`.
3. `_save_turn_ai_messages` reconciles a turn by walking backwards until it hits
   a `HumanMessage`. Keeping the in-memory message a `HumanMessage` is what keeps
   that walk correct — another reason not to introduce a novel LangChain type.

Two side effects of the `HumanMessage` carrier to suppress explicitly:
`_early_title_from_prompt` fires when `turn_count <= 2` (a wake could retitle the
session from job-completion text), and `_loop_last_user_content` gets
overwritten.

### The live cockpit shows nothing unless we add a frame

`_accept_user_input` broadcasts nothing, and no frame carries user-message
content — the cockpit builds a user turn only from its own optimistic dispatch on
send, or from a history reload. So an injected completion produces a turn that
starts and streams a reply **with no visible prompt**: the user watches the agent
talk to itself. A new broadcast kind plus a cockpit reducer case is **required
work in Phase 1**, not polish. With two replicas the SSE client and the
completing request are on different replicas ~50% of the time, so it must go
through the existing NATS→SSE bridge, not a local broadcast.

### Text template

The closest analogue is `_format_delegation_results` (`src/agent.py:114-148`) —
literally "N jobs you spawned finished, here is each one's status and where its
output lives." Ours is the singular version of it.

```
[JOB_FINISHED] A worker job you created has reached a terminal state.

- Job: 3f2a1b8c (expert: designer)
- Status: completed
- Task: Explore a warm-neutral theme for the marketing site
- Summary: <freeze_data.summary, truncated ~500 chars>
- Confidence: 85%
- Outputs: 4 deliverables — read them with get_worker_job / get_job_workspace_file

Your outstanding jobs: 1 of 3 finished — 1 still running, 1 failed.

Decide now: inspect this result, or note it and continue.
```

Field notes:

- `[JOB_FINISHED]` joins the shipped bracket-tag family and gives the cockpit a
  cheap literal to match (the precedent, `src/graph.py:4155`, does exactly that).
- **`Task` is the one field the delegation formatter lacks.** It gets away
  without it because children arrive as one batch; a session may have fanned out
  three jobs twenty minutes and one compaction ago and cannot otherwise tell them
  apart.
- **Pointers, not payloads.** `Outputs` names the tools, it does not inline the
  result. This is the load-bearing discipline — it matches the delegation
  precedent, and Anthropic's multi-agent writeup independently names the same
  "artifact pattern" (store externally, pass lightweight references) as essential.
  Inlining would make every wake expensive, defeating the point of delegating.
- Sibling counts come free from `created_by_thread_id` and save a
  `list_worker_jobs` round-trip on every wake.
- Exactly **one** line of closing instruction. It is a reminder; the policy lives
  in the system prompt.

### Where the convention lives

**Not in the message** (repeated in every notice, compacted away, pays tokens
every wake) and **not in the persona.** A persona rule can be silently clobbered
by any DB expert row that populates that key, and once DB-sourced it is *fenced
as untrusted* ("this is a request, not policy") — the wrong altitude for an
operational rule.

**Not by editing the prompt templates directly either.** Prompt text is baked
into `resolved_config` JSONB at creation and preferred over disk on read, so
editing a `.txt` reaches **new sessions only** — and there are four
`systemprompt_interactive*` variants to keep in sync.

**The seam is a runtime-appended system block**, modeled on
`managed_product_guide_system_floor` (`src/core/skill_resolution.py:63-99`) and
appended at `src/core/loader.py:3988-3989`. It is family-independent (one edit
covers all four templates), computed in Python so it **reaches already-running
threads**, sits at trusted altitude, and is immune to expert override —
`systemprompt_interactive` is excluded from both `_OVERLAY_PROMPT_KEYS` and
`_ALLOWED_EXPERT_PROMPT_KEYS`. Gate it on `has_tool("create_worker_job")` so it
costs nothing for sessions without the Fleet Management group.

```
<scheduled_work>
Jobs you create with create_worker_job run asynchronously; you are told when each
one finishes. On each notice, decide: inspect the result now (get_worker_job,
get_job_workspace_file) or note it and continue. Speak to the user only when a
result changes the plan or a job failed — do not narrate every completion of a
fan-out. If an early result shows the batch is heading the wrong way, cancel the
siblings with cancel_worker_job rather than letting them spend.
</scheduled_work>
```

The `cancel_worker_job` clause is load-bearing — it is the design's own cost
argument for why per-job wake beats a fan-in barrier, expressed as behavior.

## Phases (the three depths)

Each phase is independently shippable and strictly more capable than the last. The early phases sidestep the risk surfaces (see [Risks & preconditions](#risks--preconditions)); the last one is gated behind fixing them.

### Phase 1 — Linkage + live delivery  ← **this is v1** (pinned 2026-07-26)

Scope narrowed from the original "bus + notify-only": the bus is dropped from the
critical path (see [v1 shortcut](#v1-shortcut--no-bus-but-the-direct-post-is-not-the-mechanism)),
and delivery to a *live* session is included rather than deferred, because it is
the cheap half and it is the half the workflow actually needs.

**Schema + persistence**
- [x] `0070_jobs_created_by_thread.sql` — two-phase FK per `0028`, `wake_on_complete`, claim columns, `wake_state` CHECK.
- [x] `0071_jobs_wake_pending_idx.notx.sql` — partial index, `CONCURRENTLY`, single statement, runbook note.
- [x] Regenerate + commit `schema_current.sql` via `scripts/schema-snapshot.sh`. Squawk clean (pinned v2.59.0 binary, 0 issues).
- [x] `postgres.create_job` — two new kwargs, INSERT + RETURNING. (Scholar/critic subjob call sites pass only `parent_job_id`, so they correctly do **not** inherit the backref.)
- [x] `main.py create_job` — persist `created_by_thread_id` (root creations only) and `wake_on_complete`.
- [x] **Security: strip `thread_id` on the public path** + tests. Ships in the same commit as the FK.
- [x] Expose the columns on `get_job` / both list projections + the cockpit `Job` interface.

**Firing**
- [x] `maybe_wake_session(db, job_id, terminal_status)` — sets `wake_state='pending'`; called from `complete_job` (between the loop-advance block and `_trigger_dispatch()`, and **before** `_archive_and_cleanup_workspace`), `approve_job`, the cancel path, and `_set_target_to_autonomy_status`. Takes an **id, not a job dict** — terminal paths hand around dicts from half a dozen different projections, and a dict-based pre-filter would silently no-op wherever the projection lacks the column.
- [x] Claim-and-send worker: atomic `UPDATE … FOR UPDATE SKIP LOCKED … RETURNING`, commit, *then* POST.
- [x] Fast path — `kick_drain()` after the request commits. Losing it is harmless by construction.
- [x] Backstop sweeper, 20 s, **not** leader-gated.
- [x] Attempt cap (8) → `wake_state='dead'`, surfaced at `GET /api/stats/session-wakes` (admin).

**Delivery**
- [x] Liveness predicate copied from `GET /connection`, including `probe_ready`; deliberately **without** its self-heal (a background delivery must not clear a thread's agent binding). Thread+agent re-read inside the attempt; any non-200 → durable branch.
- [x] Live → `POST /api/input` with a `role='event'` parameter; suspended/detached/ended → `thread_messages` row + `notification_service.dispatch`. No restore.
- [x] `_db_rows_to_lc_messages` gains an `elif role == "event"`; `postgres_db.py`'s `('summary','error')` exclusion left untouched.
- [x] `_early_title_from_prompt` suppressed for injected input.
- [x] Payload carries the sibling set and pointers, never job output.

**Surfaces**
- [x] `session.event` broadcast frame + cockpit reducer case + `historyToTurns` branch.
- [x] `<scheduled_work>` system-prompt block (`loader.scheduled_work_system_floor`), gated on `create_worker_job` being in the tool set.

**Tests** — 66 new (`test_session_wake.py` 38, `test_session_wake_claim_db.py` 12 against a real Postgres via testcontainers, `test_session_wake_event_role.py` 11, `test_session_wake_linkage.py` 5) plus 4 cockpit vitest cases.
- [x] Live session receives the input; suspended/ended/awaiting_user/idle → durable row + notification, **no restore**; no `created_by_thread_id` → claim consumed, nothing delivered.
- [x] Same terminal status delivered once; `pending_review → completed` wakes again.
- [x] Two concurrent claimers → exactly one winner.
- [x] Crash after claim → re-claimed past the visibility timeout, attempt burned.
- [x] Terminal job whose hook never ran → claimed by status alone.
- [x] `role='event'` survives a simulated pod recycle (the `_db_rows_to_lc_messages` regression).
- [x] **Live gate on dev — PASSED 2026-07-27**, all five checks. See [Live verification](#live-verification--passed-on-dev-2026-07-27). It found one real defect (silent notification drop on the durable branch), fixed with regression tests but not yet deployed.

Phase 1 delivers the actual workflow (schedule → keep working → get woken →
inspect or defer → schedule the next stage) while touching neither the
resume-OOM nor the zombie-session surfaces: it never resumes a suspended pod and
never lets the agent act without the user present.

### Two things the implementation added that the design did not specify

Recorded here because both are load-bearing and neither is obvious from the
design above.

**1. `jobs.wake_notified_status` — the dedup key needed a home.** The design
says the key is `(job_id, terminal_status)` but the migration shape only had
`wake_state`, which cannot express "already delivered *completed*, but
*cancelled* would be new." Without the column, either every terminal transition
re-wakes (duplicate transcript message + a paid turn) or the first one wins
forever (an approve is never announced). It also collapses the
approve-lands-before-the-send race into a single wake carrying the newer status.
`jobs.completed_at` cannot serve: it is set by an unguarded separate statement,
re-set by `approve_job`, and not set at all by `update_job_status('failed')`.

**2. The backstop finds owed wakes by *status*, not by unfired claim.** The
design's sweeper polls "for unfired claims" — but a claim only exists once a
hook ran, so that sweeper would have caught a *crashed sender* and missed every
path with no hook at all: the 12 dispatch-time grant/config failures,
`fail_llm_outage_job`, VM-upgrade approval expiry. Those are exactly the paths
the emission-points table flags as hookless. The claim therefore has a third
arm — `wake_state='none' AND status IN (terminal) AND wake_notified_status IS
DISTINCT FROM status` — and the partial index predicate widens to match
(`wake_on_complete AND created_by_thread_id IS NOT NULL AND wake_state IN
('none','pending','sending')`, still bounded because a delivered wake leaves it
permanently).

The consequence is worth stating plainly: **adding a hook to a
newly-discovered terminal path is now an optimization, never a bug fix.** A
missed hook costs one 20-second tick, not a session that waits forever. That
inverts the design's own "missing one means a session that waits forever" risk,
which was the single scariest line in the emission-points analysis.

### Live verification — PASSED on dev 2026-07-27

Run against the dev cluster (`main` context, ns `superhuman-remote-worker`) on
orchestrator + agent image `sha-1391831`, migrations `0070`/`0071` applied and
`idx_jobs_wake_pending` VALID. Thread `3af91400`, jobs `38e17406` and
`d7d6f511`.

1. **Backref — PASS.** A Fleet-Management session called `create_worker_job`;
   the row came back with `created_by_thread_id = 3af91400` and
   `wake_on_complete = t`, both set server-side.
2. **Live wake — PASS, and it closed the loop.** The job completed and the
   notice arrived as `role='event'`, turn 2, with expert, status, task, summary,
   confidence and the `get_worker_job` pointer. The woken agent then *called
   `get_worker_job` and reported to the user* — the behavior the
   `<scheduled_work>` rule prescribes, unprompted. Enqueue → delivery was 51 ms
   via the post-commit fast path (`wake_attempts=1`).
3. **Durable branch — PASS.** The session pod was deleted mid-flight; the thread
   went `awaiting_user → ended` (confirming the audit's correction — a dead pod
   yields `ended`, not `suspended`) while `agents.status` still read `session`
   for ~4 minutes. The wake probed, found the pod gone, and wrote the notice to
   `thread_messages`; no pod was restored. On resume the model recalled **both**
   notices by job id and task — so the history filter admits `role='event'` and
   the `_db_rows_to_lc_messages` branch works. The silent trap is closed.
4. **`dead` — PASS.** `GET /api/stats/session-wakes` →
   `{pending:0, sending:0, sent:2, dead:0}`.
5. **Prompt block — PASS.** Built inside the deployed agent image from the real
   `session_base` config and interactive template: with `create_worker_job` the
   prompt is 5402 chars and carries `<scheduled_work>` at offset 4905
   (tail-anchored, trusted altitude); without it, 4903 chars and no block — so
   it costs a default session exactly nothing.

**Two mechanisms proved themselves that no unit test could reach.** The two
wakes were enqueued by *different replicas* and neither double-delivered —
the non-leader-gated claim working as designed. And job `d7d6f511`'s wake was
delivered by the **sweeper backstop**, not the request's `kick_drain` (the log
line carries no `request_id` and is the sweeper's own): the durability path is
not theoretical, it fired on the second job of the gate.

**One real defect found and fixed — the out-of-band notification never sent.**
`services.email` logged `Refusing to send email to undeliverable recipient(s):
['']`. `notification_service.dispatch` takes `user_id` for *preference lookup
only*; its email leg sends to `recipient_email or ""`, so omitting that argument
does not fall back to the user's address — it hands the empty string to the
email service, which refuses and merely logs. Nothing raises, so the
notification was silently dropped while the wake still reported success. That is
the half of the durable branch that actually reaches a user who closed the tab,
so the drop defeated the branch's purpose. `_notify_owner` now resolves the user
row and passes `recipient_email` / `recipient_name`, mirroring
`_notify_operator_freeze` — the existing caller that got this right. Two
regression tests pin it. **Not yet re-verified live: that needs a deploy.**

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
4. **The Phase 1 / Phase 3 boundary is blurrier than this doc claimed** (added 2026-07-26). "Wake-to-act" is listed as Phase 3 behind preconditions — but a live woken session **already holds its tools**, so v1 can schedule the next stage on its own the moment it is woken. The real Phase 1 mitigation is that *the user is present*, not that the agent is restrained. Say so honestly rather than relying on a phase label.
5. **No termination condition exists.** By Anthropic's coordination taxonomy this design is "agent teams + shared state" with the session as coordinator, and they name explicitly that this shape needs a time budget, a convergence threshold, or a designated completion agent. Nothing stops a woken session from scheduling indefinitely. Not a v1 blocker (the user is present, and jobs are individually expensive enough to notice) but it must be solved before any headless variant.
6. **Coalescing does not come free.** The worker-job precedent (`queued_replies`) batches naturally because a worker has phase boundaries to defer to; a session's only boundary is the turn boundary, so N completions = N queue items = N turns with no dedup seam in v1. Confirm empirically that a 6-way fan-out finishing near-simultaneously produces six cheap turns and not a thrash. Claude Code's own background-task notifications have two open bugs of exactly this shape — dropped notifications on simultaneous completion, and a notification leaking into the next turn so the model answers it instead of the user — both symptoms of an *in-memory* notification queue racing user input. The durable claim plus drain-at-loop-top is what avoids them.

## Out of scope

- **Schedule-driven session wake.** Sessions are never woken by cron — that is automations' job-factory direction. Explicitly excluded (it is the failure mode the user flagged).
- ~~**Reviving `ended` sessions.**~~ **Withdrawn 2026-07-26** — this was based on a wrong premise. An `active` thread whose pod dies is marked `ended`, not `suspended`, and ended threads are user-resumable, so "wake applies only to active/awaiting_user/suspended" would silently drop completions for a supported case. `ended` takes the durable branch. What remains out of scope is *spinning a pod back up* for one — that is Phase 2 regardless of status.
- **Cross-session wake.** v1 only wakes the *creating* session; waking some other session is not supported.
- **Multi-job fan-in barriers** ("wake me only when *all five* of my jobs finish") — **decided against, not merely deferred** (2026-07-26). The obvious v1 need looks like a barrier: schedule three designer jobs, wait for all three, review, schedule the next stage. Per-job wake instead means three wakes for one decision point, two of which may do nothing. That is the right trade:
  - the two "extra" wakes are not waste — each is a genuine early look, and with `cancel_worker_job` in hand the agent can stop a batch heading the wrong way *before* the siblings spend their share of 10k+ requests. A barrier would suppress exactly the moment worth having;
  - a failed job surfaces immediately instead of at batch end;
  - a barrier over a job that never reaches a terminal state is a hang; per-job degrades gracefully;
  - no wait-group table, no partial-failure or cancelled-member semantics.

  **Note the field leans the other way, and why that does not move us.** A 2026-07-26 survey found barrier/join is the prevailing shape: Trigger.dev makes waitpoints structurally many-to-many, LangGraph resumes N parallel interrupts with a single keyed resume map, Temporal warns that "Signals can come in faster than your Signal draining happens" against a hard 51,200-event history limit, and Anthropic's research system chose a synchronous join deliberately to avoid coordination complexity. None of those systems has a task unit costing 10k+ LLM requests, so none of them weighs *wake early so you can cancel the rest* — which is our whole reason. And their recommended mitigation, Anthropic's "coalesce at the source: one wake-up carrying *3 of 5 finished, here are the ids*," is exactly the sibling-set payload specified above. We are closer to their guidance than the headline difference suggests.
- **Job dependency graphs / a workflow engine.** The sequencing gap is real (see [The return path is also the scheduler](#the-return-path-is-also-the-scheduler)) but the answer is the wake, not a DAG. The gates in real plans are human gates; automating them away removes the reason the user is in the loop, and chaining 10k-request jobs unattended is how you spend 100k requests walking in the wrong direction.
- **Headless budgets.** Per-run / cumulative token caps are their own feature ([[headless_persistent_sessions]] §6 defers them).
- **Bus durability beyond in-process.** NATS/Redis promotion waits for a multi-replica orchestrator.

## Open questions

*Resolved 2026-07-26: fan-in (a decision under [Out of scope](#out-of-scope)); the `wake_on_complete` default (server-side, under [Session ↔ job linkage](#session--job-linkage)); "who is speaking" and mid-turn delivery (both under [The injected message](#the-injected-message)).*

1. ~~**Who is speaking?**~~ **Resolved:** `role='event'` in `thread_messages`, joining the shipped non-conversational roles `summary` and `error`, carried in-memory as a `HumanMessage`. The cockpit already branches on unknown-to-chat roles for those two. The card still transitions in place — but the durable notice is the mechanism, not the card. Three implementation traps are listed in that section; the `_db_rows_to_lc_messages` one is silent and severe.
2. *(Card rendering — moved to [[unified_tool_cards]] slice 4, 2026-07-26.)*
3. ~~**Mid-turn delivery.**~~ **Resolved, and it needs no code:** `_loop_user_queue` has exactly one consumer, awaited once per iteration at the top of the loop, and nothing peeks mid-turn — so deferral to the next turn boundary is already the behavior. This is also the mature choice elsewhere (LangGraph Platform's default `multitask_strategy` is `enqueue`; its `interrupt` option warns that a tool call may be left half-initiated). The residual caveat is **latency, not correctness**: a wake lands only after the current turn fully completes, including workspace sync push and the git commit/push, which on a long research turn can be many minutes. Accept it; there is no mechanism to put content in front of a model mid-turn (`/api/interrupt` carries no payload — it only ends the turn).
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
- `orchestrator/database/migrations/app/` — head is `0069_automation_expert_id.sql`; ours are `0070` + `0071`. FK pattern to copy: `0028_experts.sql:58-65`. `.notx.sql` runbook note: `0046_jobs_dispatchable_partial_idx.notx.sql:40-47`. Runner: `orchestrator/database/migrate.py`; conventions `docs/db_migration.md`.
- `orchestrator/database/schema.sql` — `jobs` table; `threads` table (`permission_mode`, status set: `created|active|idle|awaiting_user|suspended|ended`).
- **Claim / dedup precedents** — `claim_sent_notification` (`orchestrator/services/headless_notifications.py:229-262`, the shape to copy, but see the NULL-arbiter warning); `claim_delegation_resume` (`postgres.py:4433`); `claim_project_loop_stage_barrier`; `fetch_next_due_cron_automation` (the `SKIP LOCKED` precedent).
- **Backstop sweeper precedent** — `orchestrator/services/project_loop_sweeper.py:1-60`; its docstring documents a two-replica double-spawn incident and the age-grace fix. Read before writing ours.
- **Leader election** — `orchestrator/services/leader_election.py`; lock ids in `orchestrator/database/lock_ids.py`. Loop classification audit: `docs/tests/orchestrator_ha_background_loop_sweep.md`. **We do not use this.**
- **Liveness** — `orchestrator/routers/sessions.py:365-419` (`GET /connection`, the predicate to copy) + `probe_ready` (`services/session_lifecycle.py:77-93`). Anti-pattern: `_resolve_thread_for_forwarding` (`main.py:22437`). Background-call shape: `_detach_agent_session` (`main.py:4662`).
- **Message-shape precedents** — `_format_delegation_results` (`src/agent.py:114-148`, the closest analogue); bracket-tag family in `src/core/phase.py:562, 629, 744, 892, 960` and `src/graph.py:4059`; transient-vs-durable families in `src/core/workspace_injection.py:41-171`.
- **Role handling** — `src/database/postgres_db.py:401` (history role filter); `_db_rows_to_lc_messages` (`src/api/persistent_app.py:4830-4876`, the missing `else`); `_ROLE_MAP` (`:5175-5184`); cockpit `historyToTurns` (`persistent-chat.service.ts:3492-3553`, incl. the `summary`/`error` branches).
- **Prompt seam** — `managed_product_guide_system_floor` (`src/core/skill_resolution.py:63-99`), appended at `src/core/loader.py:3988-3989`. Override allow-lists: `orchestrator/services/config_resolver.py:34-40`, `orchestrator/main.py:25133-25148`. Flag is `CONFIG_DB_OVERRIDES_ENABLED` (**not** `PROMPT_DB_OVERRIDES_ENABLED` — renamed by migration 0022; the old name survives only in a stale comment).
- [[automations]] — sibling consumer of the bus (event-trigger half); source of the `LifecycleEvent` contract and the runaway guards.
- [[unified_tool_cards]] — slice 4, the job card: the user-facing half (live status + the review actions), and the leading candidate for how a completion is rendered. Independent of this feature; `cockpit/src/app/core/tools/tool-descriptors.ts` has no `create_worker_job` entry today.
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

### 2026-07-26 Phase 1 build

- **`jobs.wake_notified_status` added.** The `(job_id, terminal_status)` dedup key had no storage in the specified schema. Without it, either every terminal transition re-wakes or the first one wins forever. See [Two things the implementation added](#two-things-the-implementation-added-that-the-design-did-not-specify).
- **The backstop finds owed wakes by status, not by unfired claim** — a claim only exists once a hook ran, so the specified sweeper would have caught crashed senders and missed every hookless terminal path. This inverts the design's scariest risk: a missed hook now costs one 20 s tick, not a session that waits forever. Adding hooks became an optimization.
- **Hooks take a job id, not a job dict.** Terminal paths pass dicts assembled by half a dozen queries with different projections; a dict-based pre-filter would silently no-op wherever the projection lacks the column — the same silent-drop class the feature exists to remove. One indexed PK update is cheaper than that risk.
- **`_resolve_live_agent` copies `GET /connection` minus its self-heal.** `/connection` clears a dead `thread.agent_id` as a user-driven repair on a foreground request; a background delivery has no business mutating session bindings on the way past.
- **The live-visibility frame comes from the agent, not the orchestrator.** The audit called for routing it through the NATS→SSE bridge to survive the 2-replica split. Unnecessary: the SSE stream polls `thread_events` in the *database*, and the agent's own `_broadcast` writes there — so the agent emitting `session.event` from `_accept_user_input` reaches WS subscribers and SSE alike, on any replica.
- **`role='event'` rides on `additional_kwargs[PERSIST_ROLE_KEY]`**, read at the single serialization point. Both writers — the accept-time persist and the loop's turn-start reconcile — upsert the same row by id, so honoring the role in only one would let the reconcile flip it back to `'human'` the instant the turn started.
- **`pending_review` is a wake-worthy terminal status**, so a frozen job announces itself and its later approval separately.
- **Coverage:** 66 Python tests (12 against a real Postgres via testcontainers — the claim's guarantees are Postgres semantics, so mocking them would test the mock) plus 4 cockpit vitest cases. Live gate on dev still owed.

### 2026-07-26 implementation audit (8 parallel investigations)

- **The direct POST is a latency optimization, not the mechanism.** Nothing in the completion path is transactional (`postgres_db.acquire()` yields a raw connection; zero `conn.transaction()` in `main.py`), so a crash between the status write and the POST loses the wake silently. A durable claim on the `jobs` row is the mechanism; the post-commit send sits on top of it. Reverses the earlier "v1 is a direct call" framing without reinstating the bus.
- **Do NOT leader-gate the completion hook.** It would drop ~50% of wakes, because the agent's `/complete` is already load-balanced to one replica. Identified as the most likely wrong turn in the design.
- **There is no terminal-state choke point.** "All four terminal-state paths" was an undercount by ~4×; a critic-approved job reaches `completed` with no `/complete` call at all. Shape is one decision function on the covered paths plus a non-leader-gated sweeper backstop with an age-grace.
- **No free CAS.** The delegation precedent's dedup is a CAS on the waiter's *status transition*; a thread is legitimately `active` before, during and after a wake, so the claim must be materialized. Claim **before** the send — `_accept_user_input` is not idempotent, so a duplicate is a visible transcript message plus a paid LLM turn.
- **`thread_events` cannot carry the durable fallback** — single-writer table, agent-owned in-memory `seq`, and replay hard-scoped to an epoch that is bumped on every attach. Moved to `thread_messages`.
- **`ended` is not a do-nothing case** — an `active` thread whose pod dies becomes `ended`, and ended threads are user-resumable. Withdraws the original out-of-scope bullet.
- **Adding the FK opens a cross-tenant wake-injection hole** on the public job-create path, which never validates `thread_id`. Fix ships in the same commit.
- **`role='event'` resolves "who is speaking"** — joins the shipped `summary`/`error` non-conversational roles, carried in-memory as a `HumanMessage`. Three traps documented; the `_db_rows_to_lc_messages` missing-`else` one silently erases the notice on pod recycle.
- **Mid-turn deferral needs no code** and matches the field default (LangGraph Platform's `enqueue`). Residual cost is latency, not correctness.
- **The LISTEN/NOTIFY rejection rationale was an overreach** and is rewritten around durability rather than the commit lock — the lock is real but irrelevant at two replicas, and sources disagree on whether PG 19 fixed it.
- **The prompt convention goes in a runtime-appended system block**, not the persona (clobberable by a DB expert row *and* fenced as untrusted) and not the templates directly (baked into `resolved_config`, so edits reach new sessions only).
- **Acknowledged that the field prefers barrier/join** (Trigger.dev, LangGraph, Temporal, Anthropic) and recorded why the per-job decision stands anyway — none of those systems has a 10k-request task unit, and their own coalescing advice is the sibling-set payload we already specified.
- **New risks recorded:** the Phase 1/Phase 3 boundary is blurrier than claimed (a live woken session already holds its tools); no termination condition exists for the coordinator shape; coalescing does not transfer from the worker precedent.
