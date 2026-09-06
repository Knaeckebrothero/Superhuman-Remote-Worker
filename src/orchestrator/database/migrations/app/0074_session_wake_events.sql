-- Officer (centurion) wake event outbox — docs/features/centurion.md §4,
-- build plan in docs/features/centurion_implementation_notes.md (S3).
--
-- The existing session-wake outbox is a set of columns ON the jobs row
-- (wake_state/…), keyed to jobs.created_by_thread_id and four terminal
-- statuses — it cannot represent non-job events (sudo, fleet), `paused`,
-- jobs the officer didn't create, or the officer's own durable sleep timer.
-- This table is the general outbox for waking officer sessions:
--
--   * insert-time coalescing: one pending row per (thread, source, dedup_key)
--   * durable timers: source='timer' rows carry fire_at (the sleep tool files
--     them; the drain claims rows only once due) — decision 2026-07-29:
--     timers are Postgres-owned so pod/node death never loses the schedule
--   * per-source debounce: claim-side lookback over recent 'sent' rows
--   * delivery: claim → inject one coalesced sitrep per thread → finish;
--     'sending' + claimed_at implement a visibility timeout like the jobs
--     outbox; 'dead' after max attempts.

CREATE TABLE session_wake_events (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    project_id  UUID,
    -- 'timer' | 'job_transition' | 'sudo_request' | 'fleet' | 'loop'
    -- | 'respawn' | 'conference'  (deliberately unconstrained: new sources
    -- must not need a migration)
    source      TEXT NOT NULL,
    dedup_key   TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'sending', 'sent', 'dead')),
    attempts    INT NOT NULL DEFAULT 0,
    -- NULL = deliver on next drain; future = durable timer (source='timer').
    fire_at     TIMESTAMPTZ,
    claimed_at  TIMESTAMPTZ,
    sent_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE session_wake_events IS
    'Durable wake outbox for officer (centurion) sessions: events + sleep timers. See docs/features/centurion.md §4.';
COMMENT ON COLUMN session_wake_events.fire_at IS
    'NULL = deliver on next drain. Future timestamp = durable timer; the drain claims the row only once due. source=timer rows are upserted (fire_at replaced) rather than coalesced.';

-- Insert-time coalescing: at most one pending row per (thread, source, key).
-- source='timer' uses this as the upsert target (ON CONFLICT DO UPDATE
-- fire_at); every other source uses ON CONFLICT DO NOTHING.
CREATE UNIQUE INDEX uq_session_wake_events_pending
    ON session_wake_events (thread_id, source, dedup_key)
    WHERE state = 'pending';

-- Claim scan: pending/sending rows in arrival order (fire_at gating is a
-- residual predicate — the officer population is small).
CREATE INDEX idx_session_wake_events_claim
    ON session_wake_events (created_at)
    WHERE state IN ('pending', 'sending');

-- Per-source debounce lookback over recently delivered rows.
CREATE INDEX idx_session_wake_events_debounce
    ON session_wake_events (thread_id, source, sent_at)
    WHERE state = 'sent';
