---
tags:
  - feature
  - orchestrator
  - cockpit
  - triggers
  - automations
  - agent-lifecycle
aliases:
  - routines
  - scheduled jobs
  - cron jobs
  - event triggers
  - job triggers
  - agent lifecycle automations
related:
  - "[[shared_browser]]"
  - "[[notify_user_tool]]"
---

# Feature: Automations (Schedule-Based and Event-Based Job Triggers)

Design document for a native trigger system that creates jobs in response to time-based schedules **or** job-completion events, unified under a single "Automations" tab in the cockpit. The orchestrator API is documented as the escape hatch for the long tail (webhooks, inbound email, Slack, complex branching).

**Status:** Design phase. Supersedes the earlier `routines.md` (cron-only) framing — that scope is now the cron half of this feature.

## What "Automation" Means Here

An **automation** is a saved rule of the form *"when X happens, create job Y."* X is a **trigger** — a cron expression *or* a job-lifecycle event matching a filter. Y is a **job template** — expert, prompt, config, autonomy, priority.

The two trigger families we ship in v1:

1. **Cron triggers** — *"Every Monday at 07:00, run the scholar to summarize last week."* Time-based, periodic.
2. **Event triggers** — *"When any scholar job tagged `feature_request` completes, run the critic to review the open backlog."* Reactive, fires off job lifecycle events.

Both kinds live in the same `automations` table, surface in the same cockpit tab, and create jobs through the same `POST /api/jobs` code path. **The unification is the point**: users manage all their automations in one place even though there are two dispatchers under the hood.

## Motivation

Today the only way to start a job is for a human (or an MCP client like Claude Code) to call `POST /api/jobs`. There is no way to say *"every Monday morning, give me three Instagram-ready post drafts about physiotherapy,"* and no way to say *"when the developer agent finishes, automatically fire the critic to review the result."*

These two gaps look different but reduce to the same primitive. Both are *"when X happens, create a job"* — the only thing that differs is what X is.

1. **Casual users want schedules.** A friend who saw a demo immediately asked: "Can it post for me every Monday?" He is not going to install n8n, mint an API token, configure a webhook, and write a workflow JSON to get there. If the system can't do it natively in three clicks, he won't use it at all.

2. **Power users want self-improving agent loops.** The compelling use case is a multi-agent feedback chain — scholar identifies feature requests, critic prioritizes them, developer implements the chosen one, scholar starts the next research cycle, a separate critic evaluates the resulting application state in parallel, and the cycle continues. None of that is expressible today; it can only be assembled by hand, one job at a time.

The two ways this could be solved:

1. **Externalize entirely.** Document the existing `POST /api/jobs` API and tell users to schedule from n8n / Zapier / cron, and to wire job-to-job triggers via webhooks.
2. **Build it in.** Add an `automations` table, a cron dispatcher, an event dispatcher subscribed to job-completion hooks, and an "Automations" tab in the cockpit.

Option 1 is the lazy answer and looks attractive on paper ("we don't reimplement Zapier"). In practice it kills the conversion. Every external dependency is a step where the casual user drops off, and event-based triggers are even worse — exposing internal lifecycle events to an external workflow engine requires a webhook plumbing layer that doesn't exist today and that no user will configure on their own.

Option 2 covers ~95% of real demand for the work users do *inside* this system (recurring time-based work + reactive agent-to-agent chains). The remaining tail — inbound email triggers, Slack events, branching with conditional sub-workflows — is genuinely a different beast and is correctly solved by pointing users at a real workflow tool. We do both: native triggers as the default path, documented orchestrator API as the escape hatch.

This document specifies the native path.

## Industry Context

### How Others Do It

| System | Pattern | Notes |
|--------|---------|-------|
| **GitHub Actions `schedule:`** | Cron in workflow YAML | UTC-only, 5-minute minimum, fires the workflow as if a webhook had triggered it |
| **GitHub Actions `workflow_run:`** | Event trigger: workflow B fires when workflow A completes | The pure event-trigger pattern. Filters on workflow name + completion status. Closely matches what we want for event triggers |
| **Vercel Cron Jobs** | Cron in `vercel.json`, hits an HTTP route | Schedule is a job factory, not a separate runtime |
| **GitLab Pipeline Schedules** | UI form with cron + variables + branch | DB-backed, thin layer over existing pipeline |
| **Temporal Schedules + Signals** | `Schedule` for time, `Signal` for inbound events | Two distinct primitives; we collapse them into one user-facing concept (Automation) with two trigger types |
| **n8n / Zapier / Make** | Trigger node at the start of a workflow | Time, event, webhook all expressed as the first node — same unification we're following |
| **Airflow `TriggerDagRunOperator`** | DAG can fire another DAG on completion | The event-trigger pattern, expressed as an explicit graph node. Clunkier than pub/sub |
| **Kubernetes CronJob + EventBridge / Argo Events** | Separate primitives for time and events | Two systems, two UIs. Worse UX than a unified concept |
| **Linux cron + systemd path/socket activation** | `cron` daemon + event-driven `systemd` units | The original of both halves. Always two separate systems |
| **HomeAssistant Automations** | "When ... then ..." rules unifying time, state changes, and events | The best UX precedent for the unified-concept approach. We borrow the name |
| **Airflow** | DAG `schedule_interval` + executor | Heavy. Built for batch data pipelines, not user-facing automations |

### Key Takeaways

1. **Every successful native trigger system is a thin layer over an existing execution path.** Vercel Cron doesn't have its own runtime — it just hits the handler. GitHub Actions doesn't have a "scheduled workflow" type — it fires the same workflow. The trigger is a job factory, not a separate execution model. We follow the same pattern: an automation creates a normal job through the existing dispatch path. Nothing in the agent or workspace knows it was triggered.

2. **Cron expressions are good enough for time triggers.** Every system that tried to invent its own scheduling DSL (Airflow's `schedule_interval`, various low-code tools) ended up adding cron support eventually. Cron is universally understood, has mature parsers in every language (croniter for Python), and covers every realistic recurring pattern. There is no reason to invent.

3. **Event triggers are pub/sub on lifecycle events, not a graph.** GitHub Actions' `workflow_run`, Argo Events, and Temporal Signals all use a publish-subscribe pattern: the system emits typed events, automations filter on them. The DAG that emerges from chaining automations is *implicit* — the user doesn't draw it, they just create rules. This scales to arbitrary topologies without a visual editor, which is wildly cheaper to build and ship.

4. **Granularity is a non-issue at the minute level (cron) and the lifecycle level (events).** GitHub Actions can't go below 5 minutes. Most "real" automations are daily or weekly. For events, job-completion (and `phase_complete` for a small subset of intra-job rules) is the right granularity — exposing every internal node transition would be an API stability nightmare with no user value.

5. **The hard part is not the dispatchers, it's the UI.** A cron tick is twenty lines of code. An event consumer is forty. An "Automations" tab where a non-technical user can create either kind of trigger without reading docs is the whole feature. Skip this and the implementation might as well not exist.

6. **Performance is a non-concern.** A 60-second cron tick + a pub/sub on completion events is rounding error compared to what the orchestrator already does (the auto-assign dispatcher polls every few seconds at `orchestrator/main.py:1802`, every agent heartbeats every 5 seconds, IMAP poll every 30 seconds). One more background task in the existing `asyncio.create_task` lineup at `orchestrator/main.py:2421-2435` adds nothing measurable.

7. **Specific patterns we're borrowing from named systems** — explicit attribution so the design choices are auditable:
   - **CTE + `FOR UPDATE SKIP LOCKED`** — from river, graphile-worker, PgQueuer. The de facto modern Postgres-as-queue idiom. Used for the cron tick and (for multi-replica safety) the event consumer's candidate selection.
   - **`catchup_window_seconds`** — from Sidekiq-Cron's `reschedule_grace_time` and Temporal Schedules' `CatchupWindow`. Drop fires past the window rather than back-filling a flood.
   - **Fire-each-overdue-once + advance to future** — Sidekiq-Cron's behavior. Avoids Airflow's notorious old `catchup=True` backfill flood.
   - **Compute next from previous scheduled time** — explicit lesson from multiple Rails / Python blog post-mortems on schedule drift.
   - **Temporal Schedules overlap-policy vocabulary** (`SKIP`, `BUFFER_ONE`, `ALLOW_ALL`, `CANCEL_OTHER`, `TERMINATE_OTHER`) — adopted now (even though v1 only ships `ALLOW_ALL`) so we don't have to rename later.
   - **`scheduled_for` vs `dispatched_at` as separate columns** — Airflow's `execution_date` → `logical_date` rename war story.
   - **GitHub Actions `workflow_run`** — the event-trigger filter syntax (named source workflow + completion-status filter) and the explicit "only when triggered by my own workflows" semantic.
   - **HomeAssistant "Automations"** — the user-facing naming and the unified time-and-event mental model. Users coming from HA will recognize the shape immediately.
   - **Argo Events / Temporal Signals** — the lifecycle-event-as-pub/sub backend pattern.
   - **`cronstrue` + jittered presets** — from cron-job.org and GitLab Schedules' UI patterns.

## Design

### Approach: Two Dispatchers, One Table, One Tab

A new `automations` table in Postgres stores trigger + job-template definitions. Two background tasks materialize jobs from due automations:

1. **`cron_dispatcher`** — ticks every 60 seconds, finds enabled cron-triggered automations whose `next_run_at <= now()`, creates jobs, advances `next_run_at`.
2. **`event_dispatcher`** — subscribes to job-lifecycle events emitted by `orchestrator/services/completion.py`. On each event, finds enabled event-triggered automations whose `event_filter` matches, creates jobs.

Both dispatchers create jobs through the existing `POST /api/jobs` code path. The cockpit's "Automations" tab is CRUD over the table. A docs page shows users how to call the orchestrator API from n8n / Zapier / curl for cases the native dispatchers don't cover.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Cockpit: Automations tab (CRUD)                                     │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ Name        | Trigger                  | Expert    | ✓ | Last  │  │
│  │ Mon Posts   | cron 0 7 * * 1           | scholar   | ✓ | OK    │  │
│  │ Auto-Critic | on scholar.completed     | critic    | ✓ | OK    │  │
│  │ Dev Loop    | on critic.completed (A1) | developer | ✓ | OK    │  │
│  └────────────────────────────────────────────────────────────────┘  │
└────────────────────────────┬─────────────────────────────────────────┘
                             │  REST: /api/automations
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Orchestrator                                                        │
│                                                                      │
│   automations  ◄────  cron_dispatcher  (60s tick)                    │
│      table    ◄────  event_dispatcher                                │
│        │                  ▲                                          │
│        │                  │   LifecycleEvent                         │
│        │                  │   (job_complete, phase_complete)         │
│        │            completion.py emits after DB commit              │
│        ▼                                                             │
│   POST /api/jobs  ─────►  jobs table  ─────►  auto_assign_dispatcher │
│                                                       │              │
└───────────────────────────────────────────────────────┼──────────────┘
                                                        ▼
                                                 (existing flow)
                                                  agent picks up job
```

**Key property:** an automation is a *job factory*. Once it fires — for whatever reason — the resulting job is indistinguishable from any other job. No new code paths in the agent, the workspace, the freeze logic, or the cockpit job detail view. Everything that already works for manually-created jobs works for automation-spawned jobs for free.

### Why Native, Not Outsourced

| Concern | Native | API + n8n |
|---------|--------|-----------|
| Setup friction for non-technical user | Three clicks in the cockpit | Install n8n, mint token, configure webhook, write workflow |
| Ops surface | One Postgres table, two background tasks | A second service with its own DB, secrets, deployment, upgrades |
| Visibility into trigger state | First-class — cockpit shows "next run", "last fired", "last status", chain tree | Lives in n8n's UI, separate from job history |
| Failure correlation | Automation and resulting jobs in the same DB; one query joins them | Cross-system; manual correlation |
| Coverage of complex triggers | Cron + job-completion events (v1) | Anything n8n can do (webhooks, email, Slack, branches) |
| Maintenance burden | ~500 LOC + a UI tab | Zero on our side, but the user owns an entire workflow tool |

The right answer is **both**, and the order is **native first**. Native handles the common cases with no friction; the API handles the long tail for users who already live in n8n. Event triggers in v1 absorb a meaningful chunk of what would otherwise have driven users out to n8n — chained agents, conditional fan-out via tags, fan-in via project filters all become native.

### Why a Separate Dispatcher, Not a Unified Queue

A reasonable alternative is to collapse the automations system entirely: store the cron expression / event filter on the `jobs` table itself, let the existing auto-assign dispatcher consider only jobs whose triggers have fired, and skip the separate `automations` table + dispatchers. The priority queue *is* the trigger system. One concept, fewer moving parts.

This was considered and rejected for three reasons:

1. **Template vs instance.** An automation is a *recipe* that produces many jobs over time; a job is a single execution. Storing the recipe on the same row that represents the execution means an automation that fires 100 times either spawns 100 rows (and the original row is ambiguous — is it the recipe or the first instance?) or rewrites itself in place (and then run history needs a separate audit table anyway). Two concepts, two tables.

2. **Stale config.** A pre-created job that won't fire for six days has its prompt, model, and config baked in *now*. If the user edits the automation 10 minutes before the next fire — a common UX flow — the queued job has the old prompt. Fire-at-trigger creates the job with *current* automation config every time. That's what users expect, and what every mature scheduler does (Vercel Cron, GitHub Actions, Temporal Schedules — all materialize on fire, not on definition).

3. **Queue clutter.** With pre-creation, the job list contains `created`-status rows that won't actually run for days. Jobs and automations have different list-view semantics — "what's pending right now" vs. "what's scheduled to happen later" — and forcing them into one view degrades both.

The cost of keeping them separate is small: one Postgres table, two 60-second-or-faster async loops, one CRUD UI tab. The benefits — clean template/instance split, fresh config at fire time, separable list views — justify it. Priority then composes cleanly on top: automations decide *when* a job materializes; the existing priority queue decides *what order* materialized jobs run. Two orthogonal concerns, two systems, no conflation.

### Schema: `automations` Table

```sql
CREATE TABLE IF NOT EXISTS automations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id      UUID REFERENCES projects(id) ON DELETE CASCADE,

    name            TEXT NOT NULL,
    description     TEXT,

    -- Trigger discriminator
    trigger_type    TEXT NOT NULL CHECK (trigger_type IN ('cron', 'event')),

    -- Cron-trigger config (required when trigger_type = 'cron')
    cron_expr               TEXT,
    timezone                TEXT NOT NULL DEFAULT 'UTC',         -- IANA name
    catchup_window_seconds  INTEGER NOT NULL DEFAULT 86400,

    -- Event-trigger config (required when trigger_type = 'event')
    -- See "Event Triggers > Filter Semantics" for the full shape.
    event_filter    JSONB,

    -- Common
    enabled         BOOLEAN NOT NULL DEFAULT true,

    -- Job template
    expert          TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    config_override JSONB NOT NULL DEFAULT '{}'::jsonb,
    autonomy        TEXT NOT NULL DEFAULT 'review',
    priority        INTEGER NOT NULL DEFAULT 5,                  -- copied to jobs.priority at fire time

    -- Loop / runaway safety guards (apply to both trigger types,
    -- but most relevant to event-driven chains).
    max_chain_depth     INTEGER NOT NULL DEFAULT 10,             -- ancestor hops via this automation's chain
    max_fires_per_day   INTEGER NOT NULL DEFAULT 100,            -- soft rate limit; over → auto-disable
    fires_today_count   INTEGER NOT NULL DEFAULT 0,
    fires_today_date    DATE,                                    -- rolling counter resets when this < today

    -- Cron-trigger state
    next_run_at         TIMESTAMPTZ,                             -- nullable for event-only automations
    last_scheduled_at   TIMESTAMPTZ,                             -- cron-canonical time of last fire
    last_dispatched_at  TIMESTAMPTZ,                             -- wall-clock time of last fire dispatch

    -- Common state
    last_fired_at       TIMESTAMPTZ,                             -- wall-clock of last fire (any trigger)
    last_job_id         UUID REFERENCES jobs(id) ON DELETE SET NULL,
    last_status         TEXT,                                    -- mirrored from last job for UI

    -- Bookkeeping
    run_count       INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Cross-trigger consistency
    CONSTRAINT cron_trigger_has_expr
      CHECK (trigger_type <> 'cron' OR cron_expr IS NOT NULL),
    CONSTRAINT event_trigger_has_filter
      CHECK (trigger_type <> 'event' OR event_filter IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_automations_due
    ON automations (next_run_at)
    WHERE enabled = true
      AND trigger_type = 'cron'
      AND next_run_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_automations_event
    ON automations USING GIN (event_filter jsonb_path_ops)
    WHERE enabled = true AND trigger_type = 'event';

CREATE INDEX IF NOT EXISTS idx_automations_owner ON automations (owner_id);
CREATE INDEX IF NOT EXISTS idx_automations_project ON automations (project_id);
```

The two partial indexes mirror the two dispatchers: the cron index stays small even with thousands of event-only automations, and vice versa.

`config_override` is the same JSONB shape that `POST /api/jobs` already accepts. An automation can pin a model, attach a datasource, set a budget, or override any other config knob — without the automations feature having to know what's overridable.

`autonomy` defaults to `review` so automation-spawned jobs pause for the user after `job_complete` — an automation that emails its output should not also auto-publish. The user can change it to `full` for fully unattended automations.

**Why `trigger_type IN ('cron', 'event')` and not `'both'`** — composite triggers ("only fire daily digest if a scholar wrote new content yesterday") are conceptually attractive but require a small DSL to express *AND* vs *OR* semantics, plus a state machine to remember "has the cron fired since the matching event?". That's a real feature, deferred to the Composite Triggers Future Extensions bullet. v1 keeps it strictly disjoint: each automation is one trigger type. Users who want OR-semantics create two automations.

**`last_scheduled_at` vs `last_dispatched_at`** — these are two different concepts (cron only) and conflating them is the single most common scheduler bug (Airflow spent years renaming `execution_date` to `logical_date` for exactly this reason). `last_scheduled_at` is *what the cron expression said* — the planned time. `last_dispatched_at` is *what the wall clock said when the dispatcher actually fired* — usually a few seconds later, sometimes hours later if the orchestrator was down. The cockpit shows both. Drift between them is the user-visible signal that the scheduler is unhealthy.

**`catchup_window_seconds`** — the grace window for missed cron fires (event triggers don't have this concept; events are either delivered or not). If the orchestrator was down longer than this when an automation becomes due, the dispatcher logs the miss and skips to the next future tick rather than firing a stale run. The 24-hour default is intentionally generous: a weekly Monday-morning automation should still fire even if the orchestrator was down for 18 hours overnight.

**`priority`** — a template field, not a new mechanism. Copied verbatim into the resulting `jobs.priority` at fire time. Same scale, same default (5), same semantics as a manually-created job. See the Priority subsection for how the existing dispatcher uses it.

**`max_chain_depth` and `max_fires_per_day`** — load-bearing safety guards introduced to control event-driven chains. With cron there's no equivalent risk because the trigger is fixed-frequency. With events, scholar→critic→developer→scholar→... can loop forever if a filter is too broad or two automations cross-trigger each other. Both guards are detailed in "What Could Go Wrong."

### Priority

Automations reuse the existing `jobs.priority` column (`INTEGER NOT NULL DEFAULT 5`, defined at `orchestrator/database/schema.sql:536` with index `idx_jobs_priority` at line 604) and the existing two-phase auto-assign dispatcher at `orchestrator/main.py:1752`. Phase 1 directly assigns free agents to the highest-priority pending jobs — `get_dispatchable_jobs` in `orchestrator/database/postgres.py:1931` orders by `priority DESC, created_at ASC`. Phase 2 preempts lowest-priority running jobs when higher-priority pending ones can't otherwise be scheduled. Both phases are already shipped; the automations feature does not extend or modify them.

`automations.priority` is therefore a **template field**: `create_job_from_automation` copies it verbatim into the new job's `priority` column, and from there the dispatcher takes over with no special handling for automation-spawned jobs. An automation-spawned job looks identical to a manually-created job from the dispatcher's point of view.

Trigger-time and run-order are deliberately kept orthogonal. The trigger (cron expression or matching event) decides *when* a job comes into existence; priority decides *what order ready jobs run in*. A `Low (1)` daily-digest cron automation fires at 03:00 regardless of load, then sits behind any `Normal (5)` work until capacity opens — the "fill the gaps" pattern. A `High (8)` time-critical automation fires and preempts running `Low` work via Phase 2. Users get this composition for free, without the automations feature having to reason about scheduling order at all.

### Cron Triggers

This is the original "Routines" feature, preserved verbatim under the new name. The cron half handles every "every Monday at 07:00", "every weekday morning", "every hour at :07" pattern.

#### `cron_dispatcher` (60s tick)

A new async loop, mounted alongside the existing background tasks at `orchestrator/main.py:2421-2435`:

```python
# orchestrator/services/cron_dispatcher.py
async def cron_dispatcher(shutdown_event: asyncio.Event) -> None:
    while not shutdown_event.is_set():
        try:
            await _tick()
        except Exception:
            logger.exception("cron_dispatcher tick failed")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass

async def _tick() -> None:
    now = datetime.now(timezone.utc)
    async with db.transaction():
        # Single CTE: claim due cron automations and create their jobs in one statement.
        # SKIP LOCKED makes this safe across multiple orchestrator replicas.
        rows = await db.fetch(
            """
            WITH due AS (
                SELECT id, cron_expr, timezone, next_run_at,
                       catchup_window_seconds, priority,
                       max_fires_per_day, fires_today_count, fires_today_date
                FROM automations
                WHERE enabled = true
                  AND trigger_type = 'cron'
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= $1
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
            tz = ZoneInfo(r["timezone"])
            cron = croniter(r["cron_expr"], scheduled_for.astimezone(tz))
            next_run = cron.get_next(datetime).astimezone(timezone.utc)
            while next_run <= now:
                next_run = cron.get_next(datetime).astimezone(timezone.utc)

            if stale:
                metrics.automation_missed_total.labels(reason="stale").inc()
                job_id = None
            elif not _within_daily_cap(r, now):
                await _trip_daily_cap(r["id"])
                metrics.automation_missed_total.labels(reason="daily_fire_cap").inc()
                job_id = None
            else:
                job_id = await create_job_from_automation(
                    r,
                    trigger='cron',
                    scheduled_for=scheduled_for,
                )
                metrics.automation_fire_total.labels(
                    automation=str(r["id"]), trigger_type='cron'
                ).inc()
                metrics.automation_cron_lag_seconds.observe(
                    (now - scheduled_for).total_seconds()
                )

            await db.execute(
                """
                UPDATE automations
                SET next_run_at = $1,
                    last_scheduled_at = $2,
                    last_dispatched_at = $3,
                    last_fired_at = $3,
                    last_job_id = COALESCE($4, last_job_id),
                    run_count = run_count + 1,
                    fires_today_count = CASE
                        WHEN fires_today_date = $3::date THEN fires_today_count + 1
                        ELSE 1
                    END,
                    fires_today_date = $3::date,
                    updated_at = $3
                WHERE id = $5
                """,
                next_run, scheduled_for, now, job_id, r["id"],
            )
```

**Key properties:**

- **CTE + `FOR UPDATE SKIP LOCKED`** — selecting due rows and processing them inside a single transaction is the pattern used by river, graphile-worker, and PgQueuer. The locked rows are released on commit; multiple orchestrator replicas can tick in parallel and never collide. The transactional approach gives **exactly-once fire semantics for free**: if the tick crashes between job INSERT and automation UPDATE, the transaction rolls back and the next tick re-tries the same automation with the same `scheduled_for`. No `(automation_id, scheduled_fire_time)` unique constraint required.
- **`next_run` is computed from the previous `scheduled_for`, not from `now()`.** Anchoring on wall-clock time causes drift on minute-level schedules: a 1.5-second tick on `* * * * *` shifts the schedule forward by 1.5s every minute. Always advance from the cron-canonical previous time.
- **The `while next_run <= now` loop** collapses long downtime to a single fire. If the orchestrator was down for 6 hours on a `0 * * * *` schedule, we fire once for the most recent due hour (or skip if past the catchup window) and advance `next_run_at` to the next *future* hour — no flood of 6 backfilled jobs.
- **`create_job_from_automation` calls the existing job-creation service** used by `POST /api/jobs`. The created job carries `context.automation_id = <automation.id>`, `context.trigger = 'cron'`, and `context.scheduled_for = <iso8601>` so it's filterable in the job list and audit-visible. The new job's `priority` is populated from `r["priority"]` — the existing two-phase auto-assign dispatcher (`orchestrator/main.py:1752`) handles ordering and preemption with no scheduler-side changes.
- **`LIMIT 100`** caps per-tick work. Sudden spikes (downtime recovery) are absorbed across multiple ticks. Jitter the `next_run_at` of recovered automations slightly to desynchronize fleets.
- The 60s sleep is a `wait_for(shutdown_event)` so the task tears down cleanly on orchestrator shutdown.

**Catch-up policy:** governed by `catchup_window_seconds` (default 24h). Within the window: fire **once** and advance to the next future tick. Outside: log a miss and skip. Matches Sidekiq-Cron's `reschedule_grace_time` and Temporal Schedules' `CatchupWindow`. The dropped-flood anti-pattern (Airflow's old `catchup=True` default) is explicitly avoided.

**Overlap policy (v1):** an automation that's still running when its next tick comes due fires anyway, creating a second concurrent job. This matches cron itself and Kubernetes CronJob's `concurrencyPolicy: Allow` default. Phase 2 adds Temporal-style per-automation `overlap_policy` (`SKIP` / `BUFFER_ONE` / `ALLOW_ALL` / `CANCEL_OTHER` / `TERMINATE_OTHER`).

### Event Triggers

The new half. Designed to express *"when X agent finishes, run Y"* — the pattern that powers self-improving multi-agent loops.

#### Lifecycle Events

The orchestrator already runs every job through `orchestrator/services/completion.py` when an agent reports completion. Today, that service deterministically spawns critic / curator subjobs under fixed conditions. **For event triggers, we generalize the existing hooks into a typed event broadcast:**

```python
# Added at the end of completion processing in orchestrator/services/completion.py,
# AFTER the DB writes that mark the job complete have committed.
await emit_lifecycle_event(LifecycleEvent(
    type="job_complete",
    job_id=job["id"],
    expert=job["config_name"],
    status=job["status"],                  # 'completed' | 'failed' | 'pending_review'
    project_id=job.get("project_id"),
    user_id=job.get("user_id"),
    parent_job_id=job.get("parent_job_id"),
    parent_automation_id=context.get("automation_id"),
    tags=context.get("tags", []),
    priority=job.get("priority"),
    chain_id=context.get("chain_id") or str(job["id"]),
    chain_depth=context.get("chain_depth", 0),
    timestamp=datetime.now(timezone.utc),
))
```

The event is emitted **after** the completion service's DB writes commit, never before — that's what guarantees an automation can read the parent job's outputs from the database when it fires.

`emit_lifecycle_event` is in-process pub/sub today (`asyncio.Queue` consumed by `event_dispatcher`). It can be promoted to NATS / Redis Streams / Postgres `LISTEN` later if we ever run multiple orchestrator replicas. The `LifecycleEvent` payload is small and stable — it is the **public contract** that `event_filter` matches against. Adding fields to it is fine; renaming or removing fields breaks user automations.

`phase_complete` events are emitted by `completion.py` on intra-job phase boundaries when at least one enabled automation subscribes. The default v1 behavior is to subscribe only to `job_complete`, with `phase_complete` ref-counted so the per-phase emission cost is paid only when someone actually filters on it.

#### `event_dispatcher`

```python
# orchestrator/services/event_dispatcher.py
async def event_dispatcher(shutdown_event: asyncio.Event) -> None:
    queue = lifecycle_event_queue()
    while not shutdown_event.is_set():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1)
        except asyncio.TimeoutError:
            continue
        try:
            await _handle(event)
        except Exception:
            logger.exception("event_dispatcher handler failed")

async def _handle(event: LifecycleEvent) -> None:
    # Narrow with JSONB containment (GIN index supports this), then apply
    # the remaining filter clauses in Python.
    candidates = await db.fetch(
        """
        SELECT id, expert, prompt, config_override, autonomy, priority,
               event_filter, max_chain_depth, max_fires_per_day,
               fires_today_count, fires_today_date, owner_id, project_id
        FROM automations
        WHERE enabled = true
          AND trigger_type = 'event'
          AND event_filter @> jsonb_build_object('on', $1)
          AND (event_filter->>'expert' IS NULL OR event_filter->>'expert' = $2)
          AND (event_filter->>'project_id' IS NULL
               OR event_filter->>'project_id' = $3::text)
        FOR UPDATE SKIP LOCKED
        """,
        event.type,
        event.expert,
        str(event.project_id) if event.project_id else None,
    )
    for c in candidates:
        if not _matches_remaining_clauses(c["event_filter"], event):
            continue
        if event.chain_depth + 1 > c["max_chain_depth"]:
            metrics.automation_missed_total.labels(reason="chain_depth_exceeded").inc()
            continue
        if not _within_daily_cap(c, event.timestamp):
            await _trip_daily_cap(c["id"])
            metrics.automation_missed_total.labels(reason="daily_fire_cap").inc()
            continue
        try:
            job_id = await create_job_from_automation(
                c,
                trigger='event',
                triggering_event=event,
            )
        except Exception:
            logger.exception("automation %s fire failed", c["id"])
            metrics.automation_missed_total.labels(reason="fire_failed").inc()
            continue
        # Bookkeeping update (same transaction as job INSERT in the helper)
        ...
        metrics.automation_fire_total.labels(
            automation=str(c["id"]), trigger_type='event'
        ).inc()
        metrics.automation_event_lag_seconds.observe(
            (datetime.now(timezone.utc) - event.timestamp).total_seconds()
        )
```

**Key properties:**

- **In-process `asyncio.Queue` pub/sub in v1.** Single-replica orchestrator today; one queue is enough. Promote to a durable broker (NATS, Redis Streams, Postgres `LISTEN`/`NOTIFY` with a transactional outbox) if/when we run multiple replicas.
- **GIN index on `event_filter` (`jsonb_path_ops`)** keeps candidate selection sub-millisecond even with tens of thousands of event-trigger automations. The remaining filter clauses (`tags_any`, `tags_all`, `min_priority`, `parent_automation_id`) are evaluated in Python after candidate narrowing — they can't be expressed as JSONB containment cheaply.
- **`create_job_from_automation` writes `chain_id` and `chain_depth = parent.chain_depth + 1`** into the new job's `context`. This is what gives `max_chain_depth` something to count. `chain_id` is preserved across all hops descended from the same root fire, so the cockpit can render the full chain as one entity and a Phase-2 per-chain cost cap becomes possible.
- **`SKIP LOCKED`** matters for future multi-replica deployments: when multiple consumers drain the same logical event stream, the row-level lock prevents double-fires.
- **Failure of one candidate doesn't poison the event for the others.** Each candidate is processed independently with its own try/except and metric.
- **Event handling is reactive, not polled.** Lag from event emission to job creation is dominated by queue processing, typically milliseconds — much faster than the cron tick.

#### Filter Semantics

A small, declarative match language. No DSL.

| Field | Type | Semantics |
|-------|------|-----------|
| `on` | string | Required. One of `job_complete`, `phase_complete` |
| `expert` | string | Optional. Match the expert name of the parent job exactly |
| `status` | array of strings | Optional. Match parent job's terminal status. Default `["completed"]` |
| `tags_any` | array of strings | Optional. Fire if any tag in the parent's `context.tags` matches any in this list (OR) |
| `tags_all` | array of strings | Optional. Fire only if all listed tags are present (AND) |
| `project_id` | UUID | Optional. Match the parent job's project |
| `min_priority` | integer | Optional. Match if parent's `priority >= min_priority` |
| `parent_automation_id` | UUID | Optional. Match only if the parent job was itself spawned by this automation. **Used for chain-restricted rules** ("only fire when MY scholar finishes, not any scholar") |
| `parent_user_id` | UUID | Optional. Implicitly set to `owner_id` for "only on my own jobs" — UI default-on for non-shared automations |

`tags` are the user-visible vocabulary that links events. They live in `context.tags` (already supported on jobs) and are the recommended way to express "this kind of job" — `feature_request`, `morning_news`, `weekly_digest`. Tags are set either manually when the user creates a one-off job, by an automation's template, or by an agent that writes its conclusions into context.

**No boolean expressions, no scripting.** A v1 `event_filter` is an AND-of-clauses match. If a user needs branching logic on event content, they reach for the API escape hatch — keeping `event_filter` declarative is the difference between "10 lines of Postgres" and "we wrote a small DSL." The Composite Triggers Future Extension is the honest answer for richer composition.

#### Worked Example: The Self-Improving Feature Loop

The user's example, rendered as four automations:

| ID  | Trigger | Job Template |
|-----|---------|-------------|
| **A1: Research → Critic** | `event: job_complete` where `expert=scholar`, `tags_any=[feature_request]`, `parent_user_id=<self>` | `critic` — "Review the latest scholar research and the open feature request backlog. Pick the next feature to implement and write the rationale into `context.feature_choice`." |
| **A2: Critic → Developer** | `event: job_complete` where `expert=critic`, `parent_automation_id=A1` | `developer` — "Implement the feature described in the parent job's `context.feature_choice`." |
| **A3: Developer → Scholar** | `event: job_complete` where `expert=developer`, `parent_automation_id=A2` | `scholar` — "Research the next round of feature opportunities given the new application state." Tags output: `feature_request` |
| **A4: Developer → AppCritic** | `event: job_complete` where `expert=developer`, `parent_automation_id=A2` | `critic` — "Evaluate the application's current state for new issues and unmet user needs. Write findings into a project note." |

The DAG is implicit. The user wrote four "when X then Y" rules; the system stitched them into the cycle. A3 and A4 fire *in parallel* from the same triggering event (the developer completing under A2). Each automation contributes one node; the chain is data, not code.

The `parent_automation_id` filter is what keeps the chain coherent — without it, A2 would fire on *any* critic completion, not just the one A1 spawned. This is the same pattern HomeAssistant uses when one automation's action triggers another.

Note that A3 closes the loop back to A1 by tagging its output `feature_request`. This is intentional and is **exactly the case that `max_chain_depth` exists to bound**: without it, A1→A2→A3→A1→A2→A3→... loops forever. With `max_chain_depth=10` (default), the cycle runs ten hops deep before the eleventh fire is dropped with a logged metric and a cockpit warning. That gives the user enough rope to do useful work and not enough to hang the cluster.

### Endpoint Surface

```
GET    /api/automations                    # list (filtered by owner / project / trigger_type)
POST   /api/automations                    # create
GET    /api/automations/{id}               # read
PATCH  /api/automations/{id}               # update
DELETE /api/automations/{id}               # delete
POST   /api/automations/{id}/run-now       # fire immediately, independent of trigger
POST   /api/automations/{id}/pause         # convenience: enabled = false
POST   /api/automations/{id}/resume        # convenience: enabled = true; recompute next_run_at if cron
GET    /api/automations/{id}/runs          # paginated list of jobs this automation has spawned
GET    /api/automations/{id}/chain         # tree view of an automation's spawned jobs + descendants
POST   /api/automations/{id}/preview       # for event triggers: returns last N jobs that WOULD have matched
                                           #   over a given recent window (default last 7 days)
```

All routes use existing Keycloak / MCP token auth and ownership checks (an automation is visible to its owner and to project members if `project_id` is set).

`run-now` is important for the UX — it lets the user verify their automation works without waiting for the next tick or matching event. It is also the bridge to the API escape hatch: external systems with their own scheduler can hit `run-now` instead of reimplementing the trigger.

`preview` is the event-trigger equivalent of cron's "next 5 runs" preview — it answers *"what would actually have fired?"* before the user saves. The single most important UX feature for event triggers; same lesson as the cron preview.

### Cockpit: "Automations" Tab

A new top-level navigation item alongside Jobs / Projects / Threads. Two views:

**List view** — table with columns: Name, Trigger (humanized — "Every Monday at 07:00" or "When scholar completes a feature_request job"), Expert, Last fired status, Next fire (cron only), Enabled toggle, Actions (Run now, Edit, Delete).

**Editor view** — a form, not a JSON editor. The first decision is the trigger type; everything else gates on that choice.

- Name + description (free text)
- **Trigger type** — radio with two options:
  - **On a schedule** — cron-only (the original Routines flow)
  - **When something happens** — event-only (new in v1)
- **If "On a schedule":**
  - "Every day / week / month at..." quick presets (90% of real use)
  - "Custom" reveals a raw cron field with `cronstrue` natural-language explanation + live "next 5 runs" preview
  - Timezone picker (defaults to user's browser tz)
- **If "When something happens":**
  - "When this kind of job finishes" — expert dropdown (Any / Scholar / Critic / Developer / ...)
  - "With status" — dropdown (Completed / Any terminal / Failed)
  - "And tagged with any of" — chip input, with autocomplete from existing tags
  - "Only when triggered by my own automations" — checkbox that adds a `parent_automation_id` filter restricted to automations the user owns. **Default ON** — 90% of users want chain-coherent rules
  - **"Last 5 jobs that would have matched"** preview — runs the filter against the past 7 days of completed jobs and shows what would have fired. Single highest-leverage UX feature for event triggers
- Expert (the trigger's *target* — what gets created when fired): dropdown populated from `/api/experts`
- Project: optional dropdown
- Prompt: textarea (the agent's task description)
- Advanced (collapsed by default):
  - Autonomy dropdown
  - Model override
  - Priority dropdown — `Low (1)` / `Normal (5)` / `High (8)` / `Critical (10)`. Mirrors manual job creation's scale; copied verbatim into `jobs.priority` at fire time. Default `Normal (5)`. Use `Low` for "fill the gaps" automations and `High` for time-critical ones that should preempt running work
  - Catchup window (cron only)
  - Max chain depth (event only) — default 10
  - Max fires per day — default 100
  - Config JSON editor for power users

**Critical UX rule:** the editor must be usable without reading docs. Friend-test gates everything — would a non-technical user with no context build a working automation in 60 seconds. If a user picks "When something happens" and sees a 12-field filter form, the feature is dead. Default to: expert dropdown only, with everything else (status, tags, chain restriction beyond the default-on, project filter) collapsed under "Advanced filter."

**Use [`cronstrue`](https://github.com/bradymholt/cronstrue) for the cron natural-language explanation.** MIT, ~30 KB, supports localization (cockpit ships German + English). Pair it with [`cron-parser`](https://github.com/harrisiirak/cron-parser) for the "next 5 runs" preview. Showing both *"what you typed means"* and *"when it will actually fire next"* together is what every mature scheduler that has it gets praise for.

**Avoid top-of-hour presets in the cron picker.** GitHub Actions' documented pain point: every user picks `0 * * * *`, so every scheduled workflow piles up at the top of every hour and the dispatcher is overloaded. Our presets default to jittered offsets like `:07` instead of `:00`. The user doesn't care about the difference; the dispatcher does.

A small "Documentation" panel on the Automations tab links to the docs page:

> Need a more complex trigger — inbound email, a Slack message, an arbitrary webhook, branching workflows? Automations cover schedules and job-completion events. For everything else, use the orchestrator API directly from a workflow tool like n8n: `POST https://orchestrator.example.com/api/jobs` with your token. [Link to API reference.]

This is the escape hatch made discoverable. Users who outgrow native automations learn the API exists *at the moment they need it*, not a year later.

### Run History and Chain View

Each automation's `runs` page is a filtered job list: `WHERE context->>'automation_id' = $1 ORDER BY created_at DESC`. No new tables, no aggregation. The job detail view already shows everything (logs, files, freeze data, completion status). A small badge on those jobs ("Triggered by automation: Weekly Physio Posts") closes the loop.

For event-triggered automations, the **chain view** is more useful — given a root fire, render the tree of jobs that descended from it, with the automation responsible for each hop labeled. Implementation: `WHERE context->>'chain_id' = $1`, then build the tree client-side by walking `parent_job_id`. A chain is the user-facing unit of an event-driven cascade — they want to see the whole story of one Monday's research → critic → dev → ..., not the individual jobs in isolation.

### Time Correctness

Applies only to cron triggers. The single biggest lesson from Airflow / Celery / Quartz / Sidekiq postmortems is that **scheduler bugs are almost always about time, not about code** — DST transitions, leap seconds, NTP drift, naive-vs-aware datetimes, timezone-at-write vs timezone-at-fire. This subsection is the reviewer checklist for every PR that touches `cron_dispatcher`.

1. **Always `TIMESTAMPTZ`, never `TIMESTAMP`.** All datetime columns and all Python datetimes in the cron path must be timezone-aware. `datetime.now()` (naive) is banned in this code path; use `datetime.now(timezone.utc)`.
2. **Always IANA names, never offsets.** Store `Europe/Berlin`, not `+01:00`. Validate against `zoneinfo.available_timezones()` on write; reject unknown zones with 400.
3. **Use `zoneinfo`, not `pytz`.** `pytz`'s `localize()` API has well-known footguns; `zoneinfo` is stdlib since Python 3.9.
4. **Always advance from `last_scheduled_at`, not from `now()`.** Enforced in the dispatcher pseudocode above. Anchoring on wall-clock causes minute-level schedules to drift.
5. **Validate cron expressions with `croniter.is_valid(expr)` at the API boundary.** Some malformed expressions only throw on `get_next()`, not on construction. Reject on write, not on first fire.
6. **Test the DST boundaries explicitly.** Add fixtures in `test_cron_dispatcher.py` for `Europe/Berlin` spring-forward (the 02:30 fire that never exists) and fall-back (the 02:30 fire that exists twice).
7. **`scheduled_for` is the cron-canonical time, `dispatched_at` is the wall-clock time.** Never conflate.

Event triggers don't have a time-correctness story of comparable depth — events are either delivered or not, and `event.timestamp` is set once at emission. The relevant invariant for events is "emit *after* DB commit," covered above.

### Observability

A trigger system is invisible until it breaks; then it's invisible *and* the user can't tell why their Monday post never came or why the developer didn't auto-spawn after the critic. Ship metrics from day one — the marginal cost is a few `prometheus_client` lines and the marginal value when something's wrong is enormous.

| Metric | Type | What it tells you |
|--------|------|-------------------|
| `automation_cron_lag_seconds` | histogram | `dispatched_at - scheduled_for` for cron fires. p99 > 60s = unhealthy |
| `automation_event_lag_seconds` | histogram | `dispatched_at - event.timestamp` for event fires. Sustained > 1s means the event queue is backing up |
| `automation_cron_tick_duration_seconds` | histogram | One `cron_dispatcher._tick()`. Approaching 60s = falling behind |
| `automation_event_handler_duration_seconds` | histogram | One `event_dispatcher._handle()` call |
| `automation_event_queue_depth` | gauge | `lifecycle_event_queue` backlog. Sustained > 0 = consumer behind |
| `automation_fire_total{automation_id, trigger_type}` | counter | Successful fires per automation per trigger type |
| `automation_missed_total{reason}` | counter | Skipped fires. Labels: `stale`, `chain_depth_exceeded`, `daily_fire_cap`, `disabled_mid_tick`, `validation_error`, `fire_failed` |
| `automation_chain_depth` | histogram | Observed `chain_depth` per fire. Long-tail spikes catch runaway loops before they hit the cap |
| `automation_enabled_count{trigger_type}` | gauge | Currently enabled per trigger type. Sanity-check against the UI |
| `automation_next_run_drift_seconds` | histogram | `computed_next_run - expected_next_run` after a cron fire. Catches DST and croniter regressions |

Log lines complement metrics. Every fire logs: `automation_id`, `trigger_type`, `chain_id`, `chain_depth`, `lag_seconds`, `job_id`, outcome. Every skip logs the reason. Every event-queue handle logs the event type and the count of candidates evaluated. The audit trail is the answer to "did it run?" — metrics aren't enough.

**Per-automation fire history table — defer.** A dedicated `automation_fires` table is the obvious next step (Sidekiq-Cron, Temporal, Airflow all have one), but it's a write-amplified log that needs retention policy + index management. For v1 the existing `jobs` table is the history (filter by `context->>'automation_id'`). Promote when query performance demands it.

### What Could Go Wrong

| Risk | Mitigation |
|------|-----------|
| Orchestrator down at scheduled time | On startup, cron dispatcher fires overdue automations once each (not backfill flood). Past `catchup_window_seconds`, fires drop with a logged metric |
| Multiple orchestrator replicas double-firing | `FOR UPDATE SKIP LOCKED` in both dispatchers — only one replica grabs each due / matching row |
| Tick crashes between job INSERT and automation UPDATE | Both writes happen in the same Postgres transaction. Crash → rollback → next tick re-tries. Exactly-once fire without an idempotency key |
| Schedule drift on minute-level cron automations | `next_run_at` is always computed from previous `scheduled_for`, never from `now()` |
| Cron expression invalid | Validate with `croniter.is_valid(expr)` on `POST /api/automations`; reject with 400 |
| Unknown timezone string | Validate against `zoneinfo.available_timezones()` on write; reject with 400 |
| Timezone DST edge cases | croniter handles DST when given a `ZoneInfo`-aware datetime. Explicit fixtures for `Europe/Berlin` spring-forward and fall-back |
| User sets a cron that fires every minute and runs a 30-min job | v1 allows overlap (cron-style). Phase 2 adds Temporal-style `overlap_policy` |
| Top-of-hour herd from `0 * * * *` schedules | Cockpit presets default to jittered offsets (`:07`). Custom expressions remain unrestricted |
| **Runaway event chain (A→B→A→B→...)** | `max_chain_depth` (default 10) drops fires past the cap with `automation_missed_total{reason="chain_depth_exceeded"}`. `chain_depth` propagates through `context.chain_depth`. Cockpit shows a warning banner on automations that hit the cap in the last 24h |
| **Misfiring event automation fires hundreds of times per day** | `max_fires_per_day` (default 100) auto-disables the automation when exceeded with a notification. Rolling daily counter (`fires_today_count` resets when `fires_today_date < today`) |
| **Event filter too broad — matches on jobs the user didn't intend** | `parent_automation_id` filter for chain-restricted rules; `parent_user_id` default-on; cockpit's "last 5 matching jobs" preview shows what would have fired before save; tags as the recommended way to mark intent on the parent side |
| **Event-trigger fires before parent job's outputs are durable** | `emit_lifecycle_event` is called *after* the completion service commits its DB writes, never before. v2 promotes this to a Postgres-backed transactional outbox if we go multi-replica |
| **Cost runaway from a frequent cron automation** | Per-automation daily/monthly LLM cost cap → auto-pause when exceeded. Defer to Phase 2; reuse existing budget infrastructure |
| **Cost runaway across an event chain** | Per-`chain_id` cost cap (Phase 2). `chain_id` is already the join key. Reuses existing per-job cost tracking |
| Always-failing automation | Cockpit shows `last_status` prominently. Phase 2: auto-disable after N consecutive failures and notify the owner. Silent auto-disable is worse than no auto-disable |
| Typo'd `* * * * *` cron | Minimum-interval floor on cron-trigger creation (default 5 minutes for non-admin users). Soft cap of 20 automations per user |
| Orphaned automations after user deletion | `ON DELETE CASCADE` from `owner_id` |
| Performance impact on orchestrator | Negligible — one indexed CTE query per minute + one in-process queue consumer. The auto-assign dispatcher already polls more aggressively |
| User moves timezones, expects automations to follow | Cron automations store their own timezone, denormalized at creation. Moving the user profile does *not* retroactively shift existing automations. Cockpit shows the automation's tz prominently |
| User confusion: "I disabled it but the in-flight job still ran" | UI text on the disable toggle: "Disabling stops new fires; jobs already running (and event-triggered descendants of those jobs) will continue" |
| User confusion about chain ownership when automations are project-shared | Chain view shows the automation responsible for each hop. `parent_user_id` filter default-on means non-shared automations don't fire on collaborators' jobs by accident |

## Implementation Plan

### Phase 1 — Cron + Event Triggers, Unified UI

#### Files to Create

| File | Purpose |
|------|---------|
| `orchestrator/services/cron_dispatcher.py` | 60s tick loop; cron-trigger half |
| `orchestrator/services/event_dispatcher.py` | Lifecycle-event consumer; event-trigger half |
| `orchestrator/services/lifecycle_events.py` | `LifecycleEvent` dataclass, `emit_lifecycle_event`, in-process queue, ref-counted `phase_complete` subscription |
| `tests/test_cron_dispatcher.py` | Tick logic, DST fixtures (Europe/Berlin spring-forward + fall-back), no-drift-under-slow-ticks, multi-replica `SKIP LOCKED`, transactional rollback, catchup window, daily-fire-cap |
| `tests/test_event_dispatcher.py` | Filter matching (each clause), `chain_depth` propagation, `max_chain_depth` enforcement, `max_fires_per_day` enforcement, fan-out (one event → multiple matches), fan-in via tags, failure isolation between candidates, emit-after-commit ordering |
| `tests/test_automations_api.py` | CRUD endpoints, validation per `trigger_type`, auth, ownership, `preview` and `run-now` |
| `cockpit/src/app/features/automations/automations-list.component.ts` | List view |
| `cockpit/src/app/features/automations/automations-editor.component.ts` | Editor with trigger-type radio + branching form |
| `cockpit/src/app/features/automations/cron-preview.component.ts` | "Next 5 runs" preview |
| `cockpit/src/app/features/automations/event-preview.component.ts` | "Last 5 jobs that would have matched" preview |
| `cockpit/src/app/features/automations/chain-tree.component.ts` | Tree view of an event-driven chain |
| `cockpit/src/app/features/automations/automations.service.ts` | API client |
| `docs/automations_api.md` | User-facing docs page: how to call `POST /api/jobs` from n8n / curl |

#### Files to Modify

| File | Change |
|------|--------|
| `orchestrator/database/schema.sql` | Add `automations` table + indexes (idempotent `DO $$...$$` block) |
| `orchestrator/database/postgres.py` | Add `automations_*` query helpers; helpers for the event-filter narrowing query |
| `orchestrator/services/completion.py` | Generalize the existing critic / curator spawn hooks into `emit_lifecycle_event` calls. The existing deterministic behavior is preserved either as a code-level fallback OR as a default-installed system automation (decide in implementation order step 2 below) |
| `orchestrator/main.py` | Mount `/api/automations` routes; start both dispatchers in lifespan startup at lines 2421-2435 |
| `requirements.txt` | Add `croniter` if not already present |
| `cockpit/package.json` | Add `cronstrue` and `cron-parser` |
| `cockpit/src/app/app.routes.ts` | Add `/automations` route, lazy-loaded |
| `cockpit/src/app/layout/...` | Add "Automations" item to main navigation |

#### Implementation Order

1. **Schema + helpers** — Add the `automations` table and query helpers. Run `init.py` to apply. Land `LifecycleEvent` types.
2. **Generalize `completion.py` hooks** — turn the hardcoded critic / curator spawn into `emit_lifecycle_event` calls. Verify existing deterministic behavior is preserved either by a code-level fallback OR by default-installed "system automations" the user can later see/edit. **Decide which here, before downstream work proceeds**: code-level fallback is simpler; default-installed automations make the system more inspectable and align with the "everything is a rule" mental model. Recommend default-installed if migration is tractable.
3. **API endpoints** — CRUD + `run-now` + `pause`/`resume` + `preview`. Tests for trigger-type-conditional validation.
4. **Cron dispatcher** — port the loop + tests from the original routines design, mostly verbatim.
5. **Event dispatcher** — new code. Tests for every filter clause and every safety guard.
6. **Cockpit list view** — read-only first. Verify automations created via API appear correctly.
7. **Cockpit editor** — trigger-type radio + branching form. Friend-test gate before merging.
8. **Cron preview + event preview widgets** — both must ship before the editor is feature-complete.
9. **Chain tree view** — for event-triggered automations. Reuse the job detail view for each node.
10. **Docs page** — `automations_api.md` covering both the n8n / curl escape hatch and inbound webhook patterns for future trigger sources.

### Phase 2 — Polish (Defer Until v1 Has Real Users)

- **Auto-disable on repeated failures.** After N consecutive failed runs, auto-disable the automation and notify the owner. Threshold per-automation, default 3. Adds a `consecutive_failures` column.
- **Per-automation `overlap_policy` (cron).** Borrow Temporal Schedules vocabulary: `ALLOW_ALL`, `SKIP`, `BUFFER_ONE`, `CANCEL_OTHER`, `TERMINATE_OTHER`. UI default `SKIP` for new automations; existing stay on `ALLOW_ALL` for back-compat.
- **Per-automation cost cap.** `daily_cost_cap_usd` / `monthly_cost_cap_usd`. Auto-pause + notify when exceeded.
- **Per-chain cost cap (event chains).** Sum job costs by `chain_id`; trip the cap before A11 fires if A1..A10 already burned the budget.
- **`automation_fires` history table.** Promote when query performance demands it. Include retention policy (default 90 days).
- **Automation duplication / templates.** "Save as template" so similar automations can be created without retyping.
- **Project-level sharing.** `project_id` already supports it; needs UI affordances.
- **Notification on first fire.** Email the owner the first time an automation fires successfully, as confirmation.
- **Default-installed system automations.** If implementation step 2 chose code-level fallback, promote it to default-installed automations now that the system is mature. Users can disable, copy, or modify them.

### Phase 3 (Future) — Beyond This Doc

The lifecycle-event abstraction is the design's **biggest payoff for future work**: each new trigger family becomes a new event source emitting into the same queue, with no changes to the dispatcher or schema.

- **Inbound email triggers.** Generalize `orchestrator/services/imap_poller.py` to emit `inbound_email` lifecycle events. Event dispatcher already handles them. The hard parts (sender allowlists, spam, parsing) stay in `imap_poller`; the trigger plumbing is free.
- **Webhook triggers.** A `POST /api/triggers/webhook/{slug}` endpoint that emits `webhook_received` events. Same dispatcher.
- **Slack / Discord / Matrix events.** Same pattern; another lifecycle event source. Or externalize to n8n if the per-channel auth is too much.
- **Multi-step branching workflows.** Externalize. n8n exists. The agent itself *is* the multi-step engine — wrapping it in another workflow engine is the wrong layer. **With v1's event triggers, much of what users would have reached for n8n for is now native** — chains, conditional fan-out via tags, fan-in via project filters. The escape hatch shrinks but doesn't disappear.

The existence of these future trigger sources is **the reason a clean lifecycle-event abstraction matters in v1**. Each is plumbing on top of an event source, not a new control plane.

## Open Questions

1. **Where does the "Automations" nav item live?** Top-level alongside Jobs/Projects feels right for discoverability, but it could also nest under a project. Probably both: top-level shows all of the user's automations across projects; project detail view shows a filtered subset.

2. **How does this interact with persistent threads?** An automation could plausibly send a message into an existing persistent thread instead of creating a fresh job. Worth considering for v2 — the schema already supports it via `config_override`.

3. **Should the system suggest automations?** After a user manually runs the same chain a few times — scholar then critic then developer, by hand — a "Make this an automation?" prompt would be high-signal. Defer until v1 has usage.

4. **MCP exposure.** Should there be `create_automation` / `list_automations` MCP tools so Claude Code can manage automations on the user's behalf? Probably yes, but small.

5. **Quiet hours.** Cron fires obey quiet hours? Probably ignore — user explicitly set the schedule. Event fires during quiet hours? Trickier — a 3am critic fire might be fine, but its email notification shouldn't wake anyone. Decide separately for fire-time vs. notify-time.

6. **Should `completion.py`'s existing critic / curator spawn become a default-installed system automation, or stay code?** Leaning toward default-installed — it makes the system inspectable and user-tweakable, and there's no second behavior path. But the migration story is delicate (existing users, "system" ownership, rollback if it misbehaves). Decide before step 2 of the implementation order.

7. **Phase-complete events.** The schema and `event_filter` support `on=phase_complete`, but enabling it has a cost (per-phase emit, even if no automation subscribes). The proposed mitigation is ref-counting subscribers per event type; verify this is performant before exposing it in the v1 UI. If not, ship `job_complete` only and add `phase_complete` in Phase 2.

## Future Extensions

- **One-shot scheduled jobs.** "Run this job on Friday at 3pm, once." Same table, `cron_expr` becomes optional, add a `run_at` field. Auto-disables after firing.
- **Deadline-window jobs.** Complementary to cron: a `deadline_at TIMESTAMPTZ` and optional `not_before TIMESTAMPTZ` define a window in which the job must run. Dispatcher picks it up during idle windows (Low priority) and force-promotes priority as `deadline_at` approaches. Implementable on the same table; dispatcher learns one new ranking rule (`urgency_boost = clamp((now - not_before) / (deadline_at - not_before), 0, 1)`). Pairs with one-shot above.
- **Composite triggers.** A `trigger_type='both'` that requires both a cron tick *and* a recent matching event ("only fire the daily digest if a scholar wrote new content yesterday"). Or *either* — explicit AND/OR semantics in the schema. Adds a small state machine ("has the cron fired since the matching event?"). Real feature, deferred until v1 demand justifies it.
- **Conditional automations.** "Run weekly *only if* the previous run produced output" — for cron. For events, `event_filter` already covers most of this via `tags_any` / `min_priority`.
- **Public automation catalog.** Curated automations users install with one click ("Weekly news digest", "Daily standup summary", "Auto-critic on every job"). Templates with placeholder prompts.
- **Cost forecasting.** Estimate the monthly LLM cost of an automation (or a chain) based on past run history before the user enables it.
- **Event filter DSL.** If `event_filter`'s declarative AND-of-clauses turns out to be insufficient in practice, a small expression language (CEL, JMESPath, or our own) is the next step. Cost: real maintenance burden + sandboxing concerns. Reserve for when the shape of demand is clear.
