---
tags:
  - feature
  - orchestrator
  - cockpit
  - scheduling
  - triggers
aliases:
  - scheduled jobs
  - cron jobs
  - time-based triggers
  - routines tab
related:
  - "[[shared_browser]]"
  - "[[notify_user_tool]]"
---

# Feature: Routines (Time-Based Job Triggers)

Design document for a native scheduling system that fires jobs on a recurring time schedule, with the orchestrator API documented as the escape hatch for richer external trigger systems (n8n, Zapier, custom webhooks).

**Status:** Design phase.

## Motivation

Today the only way to start a job is for a human (or an MCP client like Claude Code) to call `POST /api/jobs` directly. There is no way to say "every Monday morning, give me three Instagram-ready post drafts about physiotherapy."

This gap matters because the most natural framing for a non-technical user is **"I want this to happen on a schedule,"** not **"I want to manually create a job."** A friend who saw a demo immediately asked for exactly this: a weekly recurring task that produces content drafts and emails them. He is not going to install n8n, mint an API token, configure a webhook, and write a workflow JSON to get there. If the system can't do it natively in three clicks, he won't use it at all.

The two ways this could be solved:

1. **Externalize entirely.** Document the existing `POST /api/jobs` API and tell users to schedule it from n8n / Zapier / cron / GitHub Actions / Apple Shortcuts.
2. **Build it in.** Add a `routines` table, a once-per-minute ticker in the orchestrator, and a "Routines" tab in the cockpit that's a thin CRUD over the table.

Option 1 is the lazy answer and looks attractive on paper ("we don't reimplement Zapier"). In practice it kills the conversion. Every external dependency is a step where the casual user drops off. The friction of "now go set up a separate tool" is the difference between a feature that exists and a feature that gets used.

Option 2 covers ~80% of real demand (recurring time-based work) with a small, boring implementation that reuses everything we already have. The remaining 20% — inbound email triggers, Slack events, multi-step branching — is genuinely a different beast and is correctly solved by pointing users at a real workflow tool. We do both: native time triggers as the default path, documented orchestrator API as the escape hatch.

This document specifies the native path.

## Industry Context

### How Others Do It

| System | Pattern | Notes |
|--------|---------|-------|
| **GitHub Actions `schedule:`** | Cron expression in workflow YAML | UTC-only, 5-minute minimum granularity, fires the workflow as if a webhook had triggered it |
| **Vercel Cron Jobs** | Cron expression in `vercel.json`, hits an HTTP route | Reuses the existing serverless handler — the cron is just a job factory |
| **GitLab Pipeline Schedules** | UI form with cron + variables + branch | Stored in DB, runs the existing pipeline definition with the variables injected |
| **Temporal Schedules** | First-class `Schedule` primitive separate from `Workflow` | Most powerful and most complex — overlap policies, jitter, pause windows |
| **n8n / Zapier / Make** | Trigger node at the start of a workflow | Time trigger is one of many; their value is the *graph* of nodes downstream |
| **Linux cron / systemd timers** | `cron` daemon scans `crontab` files; `systemd` reloads `.timer` units | The original. Nothing more is needed for time-based dispatch |
| **Airflow** | DAG `schedule_interval` + executor | Heavy. Built for batch data pipelines, not user-facing routines |

### Key Takeaways

1. **Every successful native scheduler is a thin layer over an existing execution path.** Vercel Cron doesn't have its own runtime — it just hits the handler. GitHub Actions doesn't have a scheduled-workflow type — it fires the same workflow. The schedule is a job factory, not a separate execution model. We should follow the same pattern: a routine creates a normal job through the existing dispatch path. Nothing in the agent or the workspace knows it was triggered by a schedule.

2. **Cron expressions are good enough.** Every system that tried to invent its own scheduling DSL (Airflow's `schedule_interval`, various low-code tools) ended up adding cron support eventually. Cron is universally understood, has mature parsers in every language (croniter for Python), and covers every realistic recurring pattern. There is no reason to invent.

3. **Granularity is a non-issue at the minute level.** GitHub Actions can't go below 5 minutes. Vercel charges extra for sub-hourly. Most "real" routines are daily or weekly. A 60-second tick with minute-resolution cron is more than enough for everyone except niche high-frequency users — and those users are exactly who should be on n8n or a real cron daemon anyway.

4. **The hard part is not the scheduler, it's the UI.** A backend cron loop is twenty lines of code. A "Routines" tab that a non-technical user can actually use without reading docs is the whole feature. Skip this and the implementation might as well not exist.

5. **Performance is a non-concern.** A `SELECT ... WHERE next_run_at <= now() AND enabled = true ORDER BY next_run_at LIMIT 100` once per minute is rounding error compared to what the orchestrator already does (the auto-assign dispatcher polls every few seconds at `orchestrator/main.py:1802`, every agent heartbeats every 5 seconds, IMAP poll every 30 seconds). One more background task in the existing `asyncio.create_task` lineup at `orchestrator/main.py:2421-2435` adds nothing measurable.

6. **Specific patterns we're borrowing from named systems** — explicit attribution so the design choices are auditable:
   - **CTE + `FOR UPDATE SKIP LOCKED`** — from river, graphile-worker, PgQueuer. The de facto modern Postgres-as-queue idiom.
   - **`catchup_window_seconds`** — from Sidekiq-Cron's `reschedule_grace_time` and Temporal Schedules' `CatchupWindow`. Both default to a small window; we go larger (24h) because user routines are typically low-frequency.
   - **Fire-each-overdue-once + advance to future** — Sidekiq-Cron's behavior. Avoids Airflow's notorious old `catchup=True` backfill flood.
   - **Compute next from previous scheduled time** — explicit lesson from multiple Rails / Python blog post-mortems on schedule drift.
   - **Temporal Schedules overlap-policy vocabulary** (`SKIP`, `BUFFER_ONE`, `ALLOW_ALL`, `CANCEL_OTHER`, `TERMINATE_OTHER`) — adopted now (even though v1 only ships `ALLOW_ALL`) so we don't have to rename later.
   - **`scheduled_for` vs `dispatched_at` as separate columns** — Airflow's `execution_date` → `logical_date` rename war story. Two distinct concepts; never conflate.
   - **`cronstrue` + jittered presets** — from cron-job.org and GitLab Schedules' UI patterns. Top-of-hour pile-up is the well-documented GitHub Actions failure mode.

## Design

### Approach: Native Scheduler + API Escape Hatch

A new `routines` table in Postgres stores recurring schedule definitions. A new background task in the orchestrator (`routine_dispatcher`) ticks every 60 seconds, finds due routines, and creates jobs through the existing `POST /api/jobs` code path. A new "Routines" tab in the cockpit is CRUD over the table. A docs page shows users how to call the orchestrator API from n8n / Zapier / curl for the cases the native scheduler doesn't cover.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Cockpit: Routines tab (CRUD)                                        │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ Name | Schedule (cron)  | Expert    | Enabled | Next run       │  │
│  │ Phys.| 0 7 * * 1        | scholar   |   ✓     | Mon 07:00      │  │
│  └────────────────────────────────────────────────────────────────┘  │
└────────────────────────────┬─────────────────────────────────────────┘
                             │  REST: /api/routines
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Orchestrator                                                        │
│                                                                      │
│   routines table  ◄─────  routine_dispatcher (60s tick)              │
│        │                          │                                 │
│        │                          ▼                                 │
│        │                  for each due routine:                     │
│        │                    create job via existing path            │
│        │                    advance next_run_at via croniter        │
│        ▼                                                            │
│   POST /api/jobs  ─────►  jobs table  ─────►  auto_assign_dispatcher│
│                                                       │             │
└───────────────────────────────────────────────────────┼─────────────┘
                                                        ▼
                                                 (existing flow)
                                                  agent picks up job
```

**Key property:** the routine is a *job factory*. Once a routine fires, the resulting job is indistinguishable from any other job. No new code paths in the agent, the workspace, the completion service, the freeze logic, or the cockpit job detail view. Everything that already works for manually-created jobs works for scheduled jobs for free.

### Why Native, Not Outsourced

| Concern | Native scheduler | API + n8n |
|---------|------------------|-----------|
| Setup friction for non-technical user | Three clicks in the cockpit | Install n8n, mint API token, configure webhook, write workflow |
| Ops surface | One Postgres table, one background task | A second service with its own DB, secrets, deployment, upgrades |
| Visibility into scheduled state | First-class — cockpit shows "next run", "last run", "last status" | Lives in n8n's UI, separate from job history |
| Failure correlation | Routine and resulting job in the same DB; one query joins them | Cross-system; manual correlation |
| Coverage of complex triggers | Time only (v1) | Anything n8n can do |
| Maintenance burden | ~300 LOC + a UI tab | Zero on our side, but the user owns an entire workflow tool |

The right answer is **both**, and the order is **native first**. Native handles the common case with no friction; the API handles the long tail for users who already live in n8n.

### Why a Separate Dispatcher, Not a Unified Queue

A reasonable alternative is to collapse the routine dispatcher entirely: store the cron expression on the `jobs` table itself, let the existing auto-assign dispatcher consider only jobs whose `next_run_at <= now()`, and skip the separate `routines` table + tick loop. The priority queue *is* the scheduler. One concept, fewer moving parts.

This was considered and rejected for three reasons:

1. **Template vs instance.** A routine is a *recipe* that produces many jobs over time; a job is a single execution. Storing the recipe on the same row that represents the execution means a routine that fires 100 times either spawns 100 rows (and the original row is ambiguous — is it the recipe or the first instance?) or rewrites itself in place (and then run history needs a separate audit table anyway). Two concepts, two tables.

2. **Stale config.** A pre-created job that won't fire for six days has its prompt, model, and config baked in *now*. If the user edits the routine 10 minutes before the next fire — a common UX flow — the queued job has the old prompt. Fire-at-trigger creates the job with *current* routine config every time. That's what users expect, and what every mature scheduler does (Vercel Cron, GitHub Actions, Temporal Schedules — all materialize on fire, not on definition).

3. **Queue clutter.** With pre-creation, the job list contains `created`-status rows that won't actually run for days. Jobs and routines have different list-view semantics — "what's pending right now" vs. "what's scheduled to happen later" — and forcing them into one view degrades both.

The cost of keeping them separate is small: one Postgres table, one 60-second async loop, one CRUD UI tab. The benefits — clean template/instance split, fresh config at fire time, separable list views — justify it. Priority then composes cleanly on top: routines decide *when* a job materializes; the existing priority queue decides *what order* materialized jobs run. Two orthogonal concerns, two systems, no conflation.

### Schema: `routines` Table

```sql
CREATE TABLE IF NOT EXISTS routines (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id      UUID REFERENCES projects(id) ON DELETE CASCADE,

    name            TEXT NOT NULL,
    description     TEXT,

    -- Trigger
    cron_expr               TEXT NOT NULL,         -- e.g. "0 7 * * 1"
    timezone                TEXT NOT NULL DEFAULT 'UTC',  -- IANA name, e.g. "Europe/Berlin"
    enabled                 BOOLEAN NOT NULL DEFAULT true,
    catchup_window_seconds  INTEGER NOT NULL DEFAULT 86400,  -- drop fires older than this

    -- Job template
    expert          TEXT NOT NULL,         -- e.g. "scholar"
    prompt          TEXT NOT NULL,         -- the description sent to the agent
    config_override JSONB NOT NULL DEFAULT '{}'::jsonb,
    autonomy        TEXT NOT NULL DEFAULT 'review',
    priority        INTEGER NOT NULL DEFAULT 5,  -- copied to jobs.priority at fire time

    -- Scheduling state
    next_run_at         TIMESTAMPTZ NOT NULL,  -- the cron-computed time the next fire is *due*
    last_scheduled_at   TIMESTAMPTZ,           -- the cron-computed time of the most recent fire
    last_dispatched_at  TIMESTAMPTZ,           -- wall-clock time the most recent fire was actually dispatched
    last_job_id         UUID REFERENCES jobs(id) ON DELETE SET NULL,
    last_status         TEXT,                  -- mirrored from last job for UI convenience

    -- Bookkeeping
    run_count       INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_routines_due
    ON routines (next_run_at)
    WHERE enabled = true;

CREATE INDEX IF NOT EXISTS idx_routines_owner ON routines (owner_id);
CREATE INDEX IF NOT EXISTS idx_routines_project ON routines (project_id);
```

The partial index on `(next_run_at) WHERE enabled = true` is the only index the dispatcher query needs and stays small even with thousands of disabled routines.

`config_override` is the same JSONB shape that `POST /api/jobs` already accepts. This means a routine can pin a model, attach a datasource, set a budget, or override any other config knob — without the routines feature having to know what's overridable.

`autonomy` defaults to `review` so scheduled jobs pause for the user after `job_complete` — a routine that emails its output should not also auto-publish. The user can change it to `full` for fully unattended routines.

**`last_scheduled_at` vs `last_dispatched_at`** — these are two different concepts and conflating them is the single most common scheduler bug (Airflow spent years renaming `execution_date` to `logical_date` for exactly this reason). `last_scheduled_at` is *what the cron expression said* — the planned time. `last_dispatched_at` is *what the wall clock said when the dispatcher actually fired* — usually a few seconds later, sometimes hours later if the orchestrator was down. The cockpit shows both. Drift between them is the user-visible signal that the scheduler is unhealthy.

**`catchup_window_seconds`** — the grace window for missed fires. If the orchestrator was down longer than this when a routine becomes due, the dispatcher logs the miss and skips to the next future tick rather than firing a stale run. The 24-hour default is intentionally generous: a weekly Monday-morning routine should still fire even if the orchestrator was down for 18 hours overnight. Users with high-frequency routines can lower it. This is the "grace_time" pattern from Sidekiq-Cron and the `CatchupWindow` from Temporal Schedules.

**`priority`** — a template field, not a new mechanism. Copied verbatim into the resulting `jobs.priority` at fire time. Same scale, same default (5), same semantics as a manually-created job. See the Priority subsection below for how the existing dispatcher uses it.

### Priority

Routines reuse the existing `jobs.priority` column (`INTEGER NOT NULL DEFAULT 5`, defined at `orchestrator/database/schema.sql:536` with index `idx_jobs_priority` at line 604) and the existing two-phase auto-assign dispatcher at `orchestrator/main.py:1752`. Phase 1 directly assigns free agents to the highest-priority pending jobs — `get_dispatchable_jobs` in `orchestrator/database/postgres.py:1931` orders by `priority DESC, created_at ASC`. Phase 2 preempts lowest-priority running jobs when higher-priority pending ones can't otherwise be scheduled. Both phases are already shipped; the routines feature does not extend or modify them.

`routines.priority` is therefore a **template field**: `create_job_from_routine` copies it verbatim into the new job's `priority` column, and from there the dispatcher takes over with no special handling for routine-spawned jobs. A routine looks identical to a manually-created job from the dispatcher's point of view.

Trigger-time and run-order are deliberately kept orthogonal. The cron expression decides *when* a job comes into existence; priority decides *what order ready jobs run in*. A `Low (1)` daily-digest routine fires at 03:00 regardless of load, then sits behind any `Normal (5)` work until capacity opens — the "fill the gaps" pattern. A `High (8)` time-critical routine fires at 09:00 and preempts running `Low` work via Phase 2. Users get this composition for free, without the routines feature having to reason about scheduling order at all.

### Endpoint Surface

```
GET    /api/routines                    # list (filtered by owner / project)
POST   /api/routines                    # create
GET    /api/routines/{id}               # read
PATCH  /api/routines/{id}               # update (name, cron, prompt, enabled, etc.)
DELETE /api/routines/{id}               # delete
POST   /api/routines/{id}/run-now       # fire immediately, independent of schedule
POST   /api/routines/{id}/pause         # convenience: enabled = false
POST   /api/routines/{id}/resume        # convenience: enabled = true; recompute next_run_at
GET    /api/routines/{id}/runs          # paginated list of jobs this routine has spawned
```

All routes use existing Keycloak / MCP token auth and ownership checks (a routine is visible to its owner and to project members if `project_id` is set).

`run-now` is important for the UX — it lets the user verify their routine works without waiting for the next scheduled tick. It is also the bridge to the API escape hatch: external systems that already have their own scheduler can hit `run-now` instead of reimplementing the trigger.

### Background Task: `routine_dispatcher`

A new async loop, mounted alongside the existing background tasks at `orchestrator/main.py:2421-2435`:

```python
# Pseudo-code in orchestrator/services/routine_dispatcher.py
async def routine_dispatcher(shutdown_event: asyncio.Event) -> None:
    while not shutdown_event.is_set():
        try:
            await _tick()
        except Exception:
            logger.exception("routine_dispatcher tick failed")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass

async def _tick() -> None:
    now = datetime.now(timezone.utc)
    async with db.transaction():
        # Single CTE: claim due routines and create their jobs in one statement.
        # Pattern borrowed from river / graphile-worker / PgQueuer.
        # SKIP LOCKED makes this safe across multiple orchestrator replicas.
        rows = await db.fetch(
            """
            WITH due AS (
                SELECT id, cron_expr, timezone, next_run_at, catchup_window_seconds, priority
                FROM routines
                WHERE enabled = true AND next_run_at <= $1
                ORDER BY next_run_at
                FOR UPDATE SKIP LOCKED
                LIMIT 100
            )
            SELECT * FROM due;
            """,
            now,
        )
        for r in rows:
            scheduled_for = r["next_run_at"]
            stale = (now - scheduled_for).total_seconds() > r["catchup_window_seconds"]

            # Compute next_run from the *previous scheduled time*, never from now().
            # Anchoring on now() causes clock drift on minute-level schedules and
            # silently shifts the schedule whenever a tick is late.
            tz = ZoneInfo(r["timezone"])
            cron = croniter(r["cron_expr"], scheduled_for.astimezone(tz))
            next_run = cron.get_next(datetime).astimezone(timezone.utc)
            # If we're firing late, walk forward until next_run is in the future,
            # so a long downtime collapses to one fire, not many.
            while next_run <= now:
                next_run = cron.get_next(datetime).astimezone(timezone.utc)

            if stale:
                logger.warning("routine %s skipped: stale by %ds", r["id"],
                               (now - scheduled_for).total_seconds())
                metrics.schedule_missed_total.labels(reason="stale").inc()
            else:
                # Same transaction as the routine UPDATE — gives us exactly-once
                # fire semantics without an explicit idempotency key.
                job_id = await create_job_from_routine(r, scheduled_for=scheduled_for)
                metrics.schedule_fire_total.labels(routine=str(r["id"])).inc()
                metrics.schedule_lag_seconds.observe((now - scheduled_for).total_seconds())

            await db.execute(
                """
                UPDATE routines
                SET next_run_at = $1,
                    last_scheduled_at = $2,
                    last_dispatched_at = $3,
                    last_job_id = COALESCE($4, last_job_id),
                    run_count = run_count + 1,
                    updated_at = $3
                WHERE id = $5
                """,
                next_run, scheduled_for, now,
                None if stale else job_id, r["id"],
            )
```

**Key properties:**

- **CTE + `FOR UPDATE SKIP LOCKED`** — selecting due rows and processing them inside a single transaction is the pattern used by river, graphile-worker, and PgQueuer. The locked rows are released on commit; multiple orchestrator replicas can tick in parallel and never collide. This also means the **transactional approach gives us idempotency for free**: if the tick crashes between job INSERT and routine UPDATE, the transaction rolls back and the next tick re-tries the same routine with the same `scheduled_for`. No `(routine_id, scheduled_fire_time)` unique constraint required. (If we ever split job creation across services — e.g., the agent dispatcher becomes an HTTP call inside `create_job_from_routine` — we revisit this and add the unique constraint as a fallback.)
- **`next_run` is computed from the previous `scheduled_for`, not from `now()`.** Anchoring on wall-clock time causes drift on minute-level schedules: a 1.5-second tick on `* * * * *` shifts the schedule forward by 1.5s every minute. Always advance from the cron-canonical previous time.
- **The `while next_run <= now` loop** collapses long downtime to a single fire. If the orchestrator was down for 6 hours on a `0 * * * *` schedule, we fire once for the most recent due hour (or skip if past the catchup window) and advance `next_run_at` to the next *future* hour — no flood of 6 backfilled jobs.
- **`create_job_from_routine` calls the existing job-creation service** used by `POST /api/jobs`. The created job carries `context.routine_id = <routine.id>` and `context.scheduled_for = <iso8601>` so it's filterable in the job list and audit-visible. The new job's `priority` is populated from `r["priority"]` — the existing two-phase auto-assign dispatcher (`orchestrator/main.py:1752`) handles ordering and preemption with no scheduler-side changes.
- **`LIMIT 100`** caps per-tick work. Sudden spikes (downtime recovery) are absorbed across multiple ticks. Jitter the `next_run_at` of recovered routines slightly to desynchronize fleets — see "Top-of-hour herd" in the risks table.
- The 60s sleep is a `wait_for(shutdown_event)` so the task tears down cleanly on orchestrator shutdown.

**Catch-up policy:** governed by `catchup_window_seconds` (default 24h). Within the window: fire **once** and advance to the next future tick. Outside the window: log a `schedule_missed_total{reason="stale"}` metric, skip the fire, advance. This matches Sidekiq-Cron's `reschedule_grace_time` and Temporal Schedules' `CatchupWindow`. The dropped-flood anti-pattern (Airflow's old `catchup=True` default that Airflow themselves changed in 2.x) is explicitly avoided.

**Overlap policy (v1):** a routine that's still running when its next tick comes due fires anyway, creating a second concurrent job. This matches cron itself and Kubernetes CronJob's `concurrencyPolicy: Allow` default. **If we add per-routine policy in v2, we should borrow Temporal Schedules' vocabulary** — `ALLOW_ALL` (current behavior), `SKIP` (drop the new fire if previous still running), `BUFFER_ONE` (queue at most one), `CANCEL_OTHER`, `TERMINATE_OTHER`. These names are the de facto standard and are what users coming from other systems will already know.

### Cockpit: "Routines" Tab

A new top-level navigation item in the cockpit, alongside Jobs / Projects / Threads. Two views:

**List view** — table of routines with columns: Name, Schedule (humanized — "Every Monday at 07:00"), Expert, Last run status, Next run, Enabled toggle, Actions (Run now, Edit, Delete).

**Editor view** — a form, not a JSON editor:

- Name + description (free text)
- Schedule:
  - "Every day / week / month at..." quick presets (90% of real use)
  - "Custom" reveals a raw cron field with a live "next 5 runs" preview *and* a natural-language explanation ("At 09:00 on Monday")
  - Timezone picker (defaults to user's browser timezone)
- Expert: dropdown populated from `/api/experts`
- Project: optional dropdown
- Prompt: textarea (this is the agent's task description)
- Advanced (collapsed by default):
  - Autonomy dropdown
  - Model override
  - Priority dropdown — `Low (1)` / `Normal (5)` / `High (8)` / `Critical (10)`. Mirrors the scale used by manual job creation so users see one priority mental model across the cockpit; the value is copied verbatim into `jobs.priority` at fire time and from there the existing two-phase dispatcher handles ordering + preemption. Default `Normal (5)`. Use `Low` for "fill the gaps" routines (daily digests, opportunistic indexing) and `High` for time-critical routines that should preempt running work
  - Catchup window (defaulted, hidden behind "Advanced")
  - Config JSON editor for power users

**Critical UX rule:** the editor must be usable without reading docs. The friend test — would a non-technical user with no context understand this in 60 seconds — gates everything. If the cron field is the first thing someone sees, the feature is dead. The presets exist specifically so most users never touch cron syntax.

**Use [`cronstrue`](https://github.com/bradymholt/cronstrue) for the natural-language explanation.** It's the de facto standard JS library — MIT licensed, ~30 KB, supports localization (we already ship German + English in the cockpit). Pair it with a small `next-runs.ts` helper that uses [`cron-parser`](https://github.com/harrisiirak/cron-parser) to render the upcoming 5 fires. Showing both "what you typed means" and "when it will actually fire next" together is the single highest-leverage UX feature in this whole tab — every mature scheduler that has it gets praise; every one that lacks it gets bug reports.

**Avoid top-of-hour presets in the picker.** GitHub Actions' documented pain point: every user picks `0 * * * *`, so every scheduled workflow piles up at the top of every hour and the dispatcher is overloaded. Our presets should default to jittered offsets like `:07` instead of `:00` ("Every hour at 7 minutes past"). The user doesn't care about the difference; the dispatcher does. Same applies to "Every day" — prefer `07:13` over `07:00`. Users who explicitly want round-hour can switch to Custom.

A small "Documentation" panel on the Routines tab (or a link to a docs page) shows:

> Need a more complex trigger — inbound email, a Slack message, a webhook, branching logic? Routines only handle time-based schedules. Use the orchestrator API directly from a workflow tool like n8n: `POST https://orchestrator.example.com/api/jobs` with your token. [Link to API reference.]

This is the escape hatch made discoverable. Users who outgrow native routines learn the API exists *at the moment they need it*, not a year later.

### Run History

Each routine's `runs` page is a filtered job list: `WHERE context->>'routine_id' = $1 ORDER BY created_at DESC`. No new tables, no new aggregation. The job detail view already shows everything (logs, files, freeze data, completion status). A small badge on those jobs ("Triggered by routine: Weekly Physio Posts") closes the loop.

### Time Correctness

The single biggest lesson from Airflow / Celery / Quartz / Sidekiq postmortems is that **scheduler bugs are almost always about time, not about code** — DST transitions, leap seconds, NTP drift, naive-vs-aware datetimes, timezone-at-write vs timezone-at-fire. This subsection is the reviewer checklist for every PR that touches the dispatcher.

1. **Always `TIMESTAMPTZ`, never `TIMESTAMP`.** All datetime columns and all Python datetimes in the routine path must be timezone-aware. `datetime.now()` (naive) is banned in this code path; use `datetime.now(timezone.utc)`.
2. **Always IANA names, never offsets.** Store `Europe/Berlin`, not `+01:00`. Offsets lose DST information and are wrong half the year. Validate against `zoneinfo.available_timezones()` on write — reject unknown zones with a 400 instead of silently falling back to UTC.
3. **Use `zoneinfo`, not `pytz`.** `pytz`'s `localize()` API has well-known footguns (you can't construct an aware datetime with the constructor); `zoneinfo` is stdlib since Python 3.9 and Just Works. The codebase already uses `zoneinfo` elsewhere — stay consistent.
4. **Always advance from `last_scheduled_at`, not from `now()`.** This is enforced in the dispatcher pseudocode above. Anchoring on wall-clock causes minute-level schedules to drift; this is a real bug class documented in multiple Rails/Python blog post-mortems.
5. **Validate cron expressions with `croniter.is_valid(expr)` at the API boundary.** Constructing `croniter(expr)` is lazy — some malformed expressions only throw when you call `get_next()`. Reject on write, not on first fire.
6. **Test the DST boundaries explicitly.** Add fixtures in `test_routine_dispatcher.py` for `Europe/Berlin` spring-forward (the 02:30 fire that never exists) and fall-back (the 02:30 fire that exists twice). croniter handles both correctly *when given a `ZoneInfo`-aware datetime*; the test exists to catch regressions where someone passes a naive datetime through and breaks the invariant.
7. **`scheduled_for` is the cron-canonical time, `dispatched_at` is the wall-clock time.** Never conflate. The job's `context.scheduled_for` is the value cron picked; the job's `created_at` is when the dispatcher actually wrote it. If they drift apart, the *gap is the metric* — see Observability below.

### Observability

A scheduler is invisible until it breaks; then it's invisible *and* the user can't tell why their Monday post never came. Ship metrics from day one — the marginal cost is a few `prometheus_client` lines and the marginal value when something's wrong is enormous.

Borrow names from Temporal Schedules and the Google SRE patterns rather than inventing:

| Metric | Type | What it tells you |
|--------|------|-------------------|
| `routine_schedule_lag_seconds` | histogram | `dispatched_at - scheduled_for`. The headline metric. p99 > 60s means something is wrong. |
| `routine_tick_duration_seconds` | histogram | How long one `_tick()` takes. If it approaches 60s the dispatcher is falling behind. |
| `routine_tick_rows_selected` | histogram | Routines processed per tick. Sudden spikes = downtime recovery in progress. |
| `routine_fire_total{routine_id}` | counter | Successful fires per routine — for per-routine SLOs. |
| `routine_missed_total{reason}` | counter | Skipped fires. Labels: `stale` (past catchup window), `disabled_mid_tick`, `validation_error`. |
| `routine_enabled_count` | gauge | Currently enabled routines. Sanity-check against the UI. |
| `routine_next_run_drift_seconds` | histogram | `computed_next_run - expected_next_run` after a fire. Catches DST and croniter regressions. |

Log lines complement metrics. Every fire logs: `routine_id`, `scheduled_for`, `dispatched_at`, `lag_seconds`, `job_id`, outcome. Every skip logs the reason. The audit trail is the answer to "did it run?" — metrics aren't enough.

**Per-routine fire history table — defer.** A `routine_fires` table with one row per fire is the obvious next step (Sidekiq-Cron, Temporal, Airflow all have one), but it's also a write-amplified log that needs retention policy + index management. For v1 the existing `jobs` table is the history (filter by `context->>'routine_id'`). Promote to a dedicated table only when query performance demands it.

### What Could Go Wrong

| Risk | Mitigation |
|------|-----------|
| Orchestrator down at scheduled time | On startup, dispatcher fires overdue routines once each (not backfill flood). Past `catchup_window_seconds`, fires are dropped with a logged metric |
| Multiple orchestrator replicas double-firing routines | `FOR UPDATE SKIP LOCKED` in the CTE — only one replica grabs each due row, the rest see it as locked and move on |
| Tick crashes between job INSERT and routine UPDATE | Both writes happen in the same Postgres transaction. Crash → rollback → next tick re-tries the same routine with the same `scheduled_for`. Exactly-once fire without an idempotency key |
| Schedule drift on minute-level routines | `next_run_at` is always computed from previous `scheduled_for`, never from `now()`. Enforced in pseudocode and covered by `test_routine_dispatcher.py::test_no_drift_under_slow_ticks` |
| Cron expression invalid | Validate with `croniter.is_valid(expr)` on `POST /api/routines`; reject with 400. Lazy validation via the `croniter()` constructor is *not* sufficient — some malformed expressions only throw on `get_next()` |
| Unknown timezone string | Validate against `zoneinfo.available_timezones()` on write; reject with 400 instead of silently falling back to UTC |
| Timezone DST edge cases (skipped/duplicated hour) | croniter handles DST correctly *when given a `ZoneInfo`-aware datetime*. Explicit fixtures for `Europe/Berlin` spring-forward and fall-back in `test_routine_dispatcher.py` |
| User sets a cron that fires every minute and runs a 30-min job | v1 allows overlap (cron-style). Phase 2 adds Temporal-style `overlap_policy` (`SKIP` / `BUFFER_ONE` / `ALLOW_ALL` / `CANCEL_OTHER`). When added, UI default should be `SKIP` |
| Top-of-hour herd from `0 * * * *` schedules | Cockpit presets default to jittered offsets (`:07` not `:00`). Same lesson GitHub Actions documents. Custom expressions remain unrestricted |
| A routine queues up jobs because a typo set it to `* * * * *` | Minimum-interval floor on routine creation (default 5 minutes for non-admin users, configurable per tier — Vercel and GitHub Actions both enforce floors). Soft cap of 20 routines per user |
| Routine creates jobs that always fail | Cockpit shows `last_status` prominently. Phase 2: auto-disable after N consecutive failures and email the owner via the existing notification system. Silent auto-disable is worse than no auto-disable |
| Cost runaway from a frequent expensive routine | Per-routine daily/monthly LLM cost cap → auto-pause when exceeded. Defer to Phase 2; reuse existing budget infrastructure |
| Orphaned routines after user deletion | `ON DELETE CASCADE` from owner_id |
| Performance impact on orchestrator | Negligible — one indexed CTE query per minute. The auto-assign dispatcher at `orchestrator/main.py:1802` already polls more aggressively; this is rounding error |
| User moves timezones, expects routines to follow | Routines store their own timezone, denormalized from the user profile at creation. Moving the user profile does *not* retroactively shift existing routines (Vercel Cron got this right; GitHub Actions still gets it wrong). Cockpit shows the routine's tz prominently in the editor |
| User confusion: "I disabled it but the in-flight job still ran" | UI text on the disable toggle: "Disabling stops future runs; jobs already started will continue" |

## Implementation Plan

### Phase 1 — Native Time-Based Routines

#### Files to Create

| File | Purpose |
|------|---------|
| `orchestrator/services/routine_dispatcher.py` | Background tick loop + routine→job factory |
| `tests/test_routine_dispatcher.py` | Tick logic, DST fixtures (Europe/Berlin spring-forward + fall-back), no-drift-under-slow-ticks, multi-replica `SKIP LOCKED` safety, transactional rollback on crash, catchup window respected |
| `tests/test_routines_api.py` | CRUD endpoints, validation, auth, ownership |
| `cockpit/src/app/features/routines/routines-list.component.ts` | List view |
| `cockpit/src/app/features/routines/routines-list.component.html` | Template |
| `cockpit/src/app/features/routines/routines-editor.component.ts` | Editor form with cron presets |
| `cockpit/src/app/features/routines/routines-editor.component.html` | Template |
| `cockpit/src/app/features/routines/routines.service.ts` | API client |
| `cockpit/src/app/features/routines/cron-preview.component.ts` | "Next 3 runs" live preview widget |
| `docs/routines_api.md` | User-facing docs page: how to call `POST /api/jobs` from n8n / curl |

#### Files to Modify

| File | Change |
|------|--------|
| `orchestrator/database/schema.sql` | Add `routines` table + indexes (idempotent `DO $$...$$` block) |
| `orchestrator/database/postgres.py` | Add `routines_*` query helpers |
| `orchestrator/main.py` | Mount `/api/routines` routes; start `routine_dispatcher` task in lifespan startup alongside the existing background tasks at lines 2421-2435 |
| `requirements.txt` | Add `croniter` if not already present (`zoneinfo` is stdlib since 3.9 — no `pytz`) |
| `cockpit/package.json` | Add `cronstrue` (natural-language explanation) and `cron-parser` (next-runs preview) |
| `cockpit/src/app/app.routes.ts` | Add `/routines` route, lazy-loaded |
| `cockpit/src/app/layout/...` | Add "Routines" item to the main navigation |

#### Implementation Order

1. **Schema + helpers** — Add the table and the query helpers. Run `init.py` to apply.
2. **API endpoints** — CRUD + `run-now` + `pause`/`resume`. Tests for auth and validation.
3. **Dispatcher** — The 60s tick loop. Test against a real Postgres with frozen time.
4. **Cockpit list view** — Read-only first. Verify routines created via API show up correctly.
5. **Cockpit editor** — Form with presets, cron preview, validation. The UX gate from "Cockpit" section above applies here — if it's not friend-test passable, iterate before shipping.
6. **Run history view** — Filtered job list per routine.
7. **Docs page** — `routines_api.md` showing the n8n / curl escape hatch.

### Phase 2 — Polish (Defer Until v1 Has Real Users)

- **Auto-disable on repeated failures.** After N consecutive failed runs, auto-disable the routine and notify the owner via the existing notification system. Threshold is per-routine, default 3. Adds a `consecutive_failures` column.
- **Per-routine `overlap_policy`.** Borrow Temporal Schedules vocabulary: `ALLOW_ALL` (current v1 behavior), `SKIP`, `BUFFER_ONE`, `CANCEL_OTHER`, `TERMINATE_OTHER`. UI default should be `SKIP` even though existing routines stay on `ALLOW_ALL` for back-compat.
- **Per-routine cost cap.** `daily_cost_cap_usd` / `monthly_cost_cap_usd` columns; auto-pause + notify when exceeded. Reuses existing per-job cost tracking.
- **`routine_fires` history table.** Promote from "filter the jobs table" to a dedicated history table when query performance demands it. Include retention policy (default 90 days) from day one to avoid Sidekiq-Cron's "history table full" failure mode.
- **Routine duplication / templates.** "Save as template" so a user can spin up similar routines without retyping.
- **Project-level routine sharing.** Already supported by the schema (`project_id`); needs UI affordances for "share with project."
- **Notification on first run.** Email the owner the first time a routine fires successfully, as confirmation that the schedule works.

### Phase 3 (Future) — Beyond Time

These are explicitly **out of scope** for this design doc and are listed here only to confirm we know they exist and have a story for them.

- **Inbound email triggers.** The IMAP poller already exists at `orchestrator/services/imap_poller.py` for reply-to-agent-message handling. Extending it to fire jobs from arbitrary inbound mail is a separate feature with its own design — message routing, parsing, sender allowlists, abuse prevention. Don't conflate it with this.
- **Webhook triggers.** A `POST /api/triggers/webhook/{slug}` endpoint that fires a configured job. Trivial to build, but requires a separate UI for managing trigger URLs and secrets. Also a separate feature.
- **Slack / Discord / Matrix events.** Externalize. n8n exists.
- **Multi-step branching workflows.** Externalize. n8n exists. The agent itself *is* the multi-step engine — wrapping it in another workflow engine is the wrong layer.

The existence of these future features is **the reason the API escape hatch matters in v1**. We don't have to build them ourselves to have a credible answer when users ask.

## Open Questions

1. **Where does the "Routines" nav item live?** Top-level alongside Jobs/Projects feels right for discoverability, but it could also nest under a project. Probably both: top-level shows all of the user's routines across projects; project detail view shows a filtered subset.

2. **How does this interact with persistent threads?** A routine could plausibly send a message into an existing persistent thread instead of creating a fresh job. Worth considering for v2 — the schema already supports it via `config_override`, but the UI doesn't expose it.

3. **Should the system suggest routines?** After a user manually runs the same expert with similar prompts a few times, a "Make this a routine?" prompt would be high-signal. Defer until basic routines ship and we see usage.

4. **MCP exposure.** Should there be `create_routine` / `list_routines` MCP tools so Claude Code can manage routines on the user's behalf? Probably yes, but small. Add to `orchestrator/mcp/` after the REST API stabilizes.

5. **Quiet hours.** The notification system already has a quiet-hours digest loop. Should routines respect quiet hours (defer firing until morning) or ignore them? Probably ignore — the user explicitly set the schedule. If they wanted quiet hours respected, they'd schedule it differently.

## Future Extensions

- **One-shot scheduled jobs.** "Run this job on Friday at 3pm, once." Same table, `cron_expr` becomes optional, add a `run_at` field. Auto-disables after firing.
- **Deadline-window jobs.** A complementary primitive to cron schedules: instead of a point-in-time trigger, a `deadline_at TIMESTAMPTZ` and an optional `not_before TIMESTAMPTZ` define a window in which the job must run. The dispatcher picks the job up during idle windows (Low priority by default) and force-promotes its priority as `deadline_at` approaches — implementing the natural "run this once before tomorrow morning, ideally when nothing else is happening" pattern that low-priority cron only approximates. Implementable on the same table: `cron_expr` becomes nullable, add `deadline_at` and `not_before`, dispatcher learns one new ranking rule (`urgency_boost = clamp((now - not_before) / (deadline_at - not_before), 0, 1)`). Pairs naturally with the one-shot item above; both are "non-recurring trigger" variants on the same schema.
- **Conditional routines.** "Run weekly *only if* the previous run produced output." Cheap to add once routines are stable: check `last_status` before firing.
- **Routine chains.** "When routine A finishes, fire routine B." The completion service already has hooks for delegation; routines could subscribe.
- **Public routine catalog.** Curated routines that users can install with one click ("Weekly news digest", "Daily standup summary"). Templates with placeholder prompts.
- **Cost forecasting.** Show the estimated monthly LLM cost of a routine based on past run history before the user enables it.
