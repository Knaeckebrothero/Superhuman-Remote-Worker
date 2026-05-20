-- migration:     0015_create_automations.sql
-- description:   Create the automations table, the storage layer for
--                schedule-based and event-based job triggers per
--                docs/features/automations_v0.md.
--
--                v0 only exercises the cron subset (trigger_type='cron');
--                the table accepts both trigger types from day one so
--                v0.5 (event triggers) can ship without another schema
--                migration. The event-only columns (`event_filter`) and
--                event-only partial index are created here as dormant
--                surface — nullable, zero rows in v0.
--
--                Each row is a *job template* — when a cron tick or
--                lifecycle event fires, the dispatcher materializes a
--                normal row in `jobs` via the existing POST /api/jobs
--                code path. Once created the resulting job is
--                indistinguishable from a manually-created one; no agent /
--                workspace / freeze logic needs to know it came from an
--                automation. The two-way link is `jobs.context->>'automation_id'`
--                (set at fire time) plus `automations.last_job_id` (UI hint).
--
--                Indexes mirror the two dispatchers: a partial index over
--                `(next_run_at)` for the cron 60s tick (kept small even
--                with thousands of event-only automations) and a partial
--                GIN index over `event_filter` for the v0.5 event
--                dispatcher. Owner/project covering indexes for the
--                cockpit list views.
--
--                Safety guards are stored on the row (`max_chain_depth`,
--                `max_fires_per_day`) so the dispatcher can enforce them
--                without a separate config layer. The `fires_today_*`
--                pair is a rolling counter the dispatcher resets when
--                `fires_today_date < CURRENT_DATE`.
-- depends-on:    0014_thread_mounts_target_user_sub.sql
-- expected:      < 100ms. New table + indexes; no existing-data rewrite.
-- locks:         No existing-table locks. New table; the FK additions to
--                users / projects / jobs take a brief ShareRowExclusive
--                on those tables for validation, covered by lock_timeout.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS automations (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id                UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id              UUID REFERENCES projects(id) ON DELETE CASCADE,

    name                    TEXT NOT NULL,
    description             TEXT,
    trigger_type            TEXT NOT NULL CHECK (trigger_type IN ('cron', 'event')),

    -- Cron-trigger fields. Required when trigger_type='cron'; enforced
    -- by the cron_trigger_has_expr CHECK below. `timezone` is an IANA
    -- name (e.g. 'Europe/Berlin'), not an offset, so DST is handled by
    -- zoneinfo at fire time. `catchup_window_seconds` is the grace
    -- window for missed fires (orchestrator down longer than this →
    -- skip the stale tick and advance to the next future one).
    cron_expr               TEXT,
    timezone                TEXT NOT NULL DEFAULT 'UTC',
    catchup_window_seconds  INTEGER NOT NULL DEFAULT 86400,

    -- Event-trigger fields. Unused in v0 (zero rows with
    -- trigger_type='event'); populated in v0.5 via the
    -- `services/event_dispatcher.py` consumer. Required when
    -- trigger_type='event'; enforced by event_trigger_has_filter.
    event_filter            JSONB,

    enabled                 BOOLEAN NOT NULL DEFAULT true,

    -- Job template. Copied verbatim into the new job at fire time:
    -- `expert` → `jobs.config_name`, `prompt` → `jobs.description`,
    -- `config_override` → `jobs.config_override`, etc. `priority` is
    -- the existing 0-10 scale; the auto-assign dispatcher takes over
    -- from there with no special-casing for automation-spawned jobs.
    expert                  TEXT NOT NULL,
    prompt                  TEXT NOT NULL,
    config_override         JSONB NOT NULL DEFAULT '{}'::jsonb,
    autonomy                TEXT NOT NULL DEFAULT 'review',
    priority                INTEGER NOT NULL DEFAULT 5,

    -- Loop / runaway safety guards. Most relevant to event-driven
    -- chains (cron is fixed-frequency), but cron also honors
    -- `max_fires_per_day` — over-cap auto-pauses the automation.
    -- Defaults calibrated against industry references (AI SDK
    -- stepCountIs(20), LangChain max_iterations=15) and the
    -- November 2025 $47K multi-agent runaway postmortem.
    max_chain_depth         INTEGER NOT NULL DEFAULT 10,
    max_fires_per_day       INTEGER NOT NULL DEFAULT 100,
    fires_today_count       INTEGER NOT NULL DEFAULT 0,
    fires_today_date        DATE,

    -- Cron state. `next_run_at` is the planned next fire time in UTC
    -- (computed via croniter against the row's `timezone`). It's
    -- nullable for event-only automations and SET TO NULL when the
    -- automation is paused.
    -- `last_scheduled_at` / `last_dispatched_at` are deliberately
    -- separate — conflating them is the most common scheduler bug
    -- (Airflow's execution_date → logical_date war story).
    -- `last_scheduled_at` = what the cron expression said (planned),
    -- `last_dispatched_at` = wall-clock time the dispatcher actually
    -- fired. Drift between them is the user-visible "scheduler
    -- unhealthy" signal in the cockpit.
    next_run_at             TIMESTAMPTZ,
    last_scheduled_at       TIMESTAMPTZ,
    last_dispatched_at      TIMESTAMPTZ,
    last_fired_at           TIMESTAMPTZ,
    last_job_id             UUID REFERENCES jobs(id) ON DELETE SET NULL,
    last_status             TEXT,

    -- Bookkeeping
    run_count               INTEGER NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT cron_trigger_has_expr
        CHECK (trigger_type <> 'cron' OR cron_expr IS NOT NULL),
    CONSTRAINT event_trigger_has_filter
        CHECK (trigger_type <> 'event' OR event_filter IS NOT NULL)
);

-- Hot path for the cron_dispatcher 60s tick: "enabled cron automations
-- whose next_run_at <= now()". Partial index stays small even with
-- thousands of event-only automations sharing the table.
CREATE INDEX IF NOT EXISTS idx_automations_due
    ON automations (next_run_at)
    WHERE enabled = true
      AND trigger_type = 'cron'
      AND next_run_at IS NOT NULL;

-- Dormant in v0; lit up by the v0.5 event_dispatcher's filter match.
-- jsonb_path_ops keeps the index small and is the right operator class
-- for @> containment queries (the predominant event_filter match shape).
CREATE INDEX IF NOT EXISTS idx_automations_event
    ON automations USING GIN (event_filter jsonb_path_ops)
    WHERE enabled = true
      AND trigger_type = 'event';

-- Cockpit list views: "my automations", "this project's automations".
CREATE INDEX IF NOT EXISTS idx_automations_owner
    ON automations (owner_id);

CREATE INDEX IF NOT EXISTS idx_automations_project
    ON automations (project_id)
    WHERE project_id IS NOT NULL;

COMMENT ON TABLE automations IS
    'Job templates fired by the cron_dispatcher (trigger_type=cron) or '
    'event_dispatcher (trigger_type=event, v0.5+). One row = one rule of '
    'the form "when X happens, create job Y". Design: '
    'docs/features/automations_v0.md.';

COMMENT ON COLUMN automations.timezone IS
    'IANA timezone name (e.g. ''Europe/Berlin''), not a UTC offset. '
    'zoneinfo resolves DST at fire time; storing an offset would silently '
    'mis-fire across DST transitions.';

COMMENT ON COLUMN automations.catchup_window_seconds IS
    'Grace window for missed cron fires when the orchestrator was down. '
    'If the next due fire is older than this many seconds, the dispatcher '
    'skips it and advances to the next future tick rather than back-filling '
    'a stale run. Default 24h; intentionally generous so a weekly Monday '
    'automation still fires after an 18h overnight outage.';

COMMENT ON COLUMN automations.last_scheduled_at IS
    'Cron-canonical time of the last fire (what the expression said). '
    'Compare against last_dispatched_at to detect scheduler drift.';

COMMENT ON COLUMN automations.last_dispatched_at IS
    'Wall-clock time the dispatcher actually fired the last run. Usually '
    'within seconds of last_scheduled_at; large drift = orchestrator was '
    'down or the dispatcher was lagging.';

COMMENT ON COLUMN automations.max_fires_per_day IS
    'Soft rate cap. fires_today_count is incremented at fire time; over-cap '
    'fires are dropped and the automation is auto-disabled with a notification '
    'to the owner. The fires_today_date column rolls the counter daily.';

COMMENT ON COLUMN automations.event_filter IS
    'JSONB filter for v0.5 event triggers. Unused in v0 (NULL for all rows). '
    'Shape: {event_type, expert?, tags_any?, tags_all?, min_priority?, '
    'parent_automation_id?}. See docs/features/automations.md §Event Triggers.';

COMMIT;
