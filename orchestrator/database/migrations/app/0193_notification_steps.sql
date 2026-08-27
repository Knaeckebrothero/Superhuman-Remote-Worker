-- migration:     0193_notification_steps.sql
-- description:   Pending channel steps for the unified notification feed — the
--                escalate-on-timeout engine's work table (knowledge-base/knowledge/
--                features/unified_notification_system.md D5/D6/D8). A delay is not
--                its own row: it is the ``due_at`` of the channel step that follows
--                it. Slice 2 of three: one new table only.
-- depends-on:    0192_notifications.sql
-- expected:      < 5s. One new empty table; no existing table is touched.
-- locks:         none on existing tables (the FK takes SHARE ROW EXCLUSIVE on
--                notifications, which is empty-or-tiny and has no writers yet).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- One row per *deferred* channel step of a notification. Zero-delay steps never
-- land here (record() runs them inline); a step is written when its severity
-- class says "wait, then mail unless …" or when quiet hours defer an immediate
-- one. The sweeper claims due rows (FOR UPDATE SKIP LOCKED + a claimed_at
-- lease), evaluates ``conditions`` AT DUE TIME against the live notification and
-- its source, and settles the row: done (a delivery was attempted), skipped
-- (a condition or preference said no), cancelled (the source was resolved
-- first), failed (the channel gave up after its retries).
CREATE TABLE public.notification_steps (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    notification_id UUID NOT NULL
                        REFERENCES public.notifications(id) ON DELETE CASCADE,
    step_index      INTEGER NOT NULL,
    step_kind       TEXT NOT NULL,
    due_at          TIMESTAMPTZ NOT NULL,
    conditions      JSONB NOT NULL DEFAULT '[]'::jsonb,
    batch_key       TEXT,
    state           TEXT NOT NULL DEFAULT 'pending',
    attempt         INTEGER NOT NULL DEFAULT 0,
    claimed_by      TEXT,
    claimed_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at      TIMESTAMPTZ,
    detail          TEXT,
    CONSTRAINT notification_step_kind
        CHECK (step_kind IN ('email', 'ntfy', 'slack_webhook', 'discord_webhook', 'push')),
    CONSTRAINT notification_step_state
        CHECK (state IN ('pending', 'done', 'skipped', 'cancelled', 'failed')),
    CONSTRAINT notification_step_settled
        CHECK ((state = 'pending') = (settled_at IS NULL)),
    CONSTRAINT notification_step_conditions_shape
        CHECK (jsonb_typeof(conditions) = 'array'),
    CONSTRAINT notification_step_claim
        CHECK ((claimed_by IS NULL) = (claimed_at IS NULL)),
    CONSTRAINT uq_notification_step UNIQUE (notification_id, step_index)
);

-- The sweeper's scan: what is due, oldest first.
CREATE INDEX ix_notification_steps_due
    ON public.notification_steps (due_at)
    WHERE state = 'pending';
-- Cancellation on resolve: every open step of one notification.
CREATE INDEX ix_notification_steps_open_by_notification
    ON public.notification_steps (notification_id)
    WHERE state = 'pending';

COMMENT ON TABLE public.notification_steps IS
    'Deferred channel steps of the unified notification feed (escalate-on-timeout). '
    'A row is the promise "at due_at, unless conditions say otherwise, deliver via '
    'step_kind". Resolving the source cancels its pending steps; the sweeper settles '
    'the rest. detail carries the skip reason, failure message or batch id.';

COMMIT;
