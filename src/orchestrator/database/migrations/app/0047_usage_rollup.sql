-- migration:     0047_usage_rollup.sql
-- description:   Usage rollup mirror — usage_daily (daily pre-aggregated usage,
--                the app-DB read model /api/usage serves for CLOSED days) plus
--                rollup_state (the cross-DB watermark). The raw ledger lives in
--                the auditdb (usage_events, migrations/audit/0002); this is the
--                small, joinable rollup the Cockpit dashboard reads without
--                scanning the firehose. Maintained by services/usage_rollup.py,
--                which re-aggregates a trailing window from the auditdb and
--                full-replace upserts here, advancing the watermark in the SAME
--                app-DB transaction (the cross-DB exactly-once trick — audit rows
--                are read, never locked; only the app write is atomic).
--                docs/features/database_roadmap.md Phase 6 (D-1).
-- depends-on:    0001_initial.sql
-- expected:      < 1s (two empty CREATE TABLEs on a fresh DB).
-- locks:         Brief ACCESS EXCLUSIVE per CREATE TABLE (brand-new objects).
-- transactional: yes
--
-- Design notes:
--   * Dims (day, user_id, project_id, category, resource, unit) are a SUPERSET
--     chosen so all three read endpoints project from this one table:
--     /api/usage groups by (category, unit); /breakdown by (user|model|project);
--     /timeseries by (day, key). Per-job/thread cost (ref_id) is deliberately
--     NOT a dim — those queries stay on the raw ledger, which is indexed for it
--     (usage_events_ref_idx). Rolling ref_id in would explode cardinality.
--   * user_id / project_id are NULLABLE (unattributed fleet-key traffic is a
--     real, common bucket). The unique index is NULLS NOT DISTINCT (PG15+) so
--     those NULL-dim rows collapse into ONE bucket per (day, category, resource,
--     unit) and the full-replace upsert's ON CONFLICT fires — otherwise every
--     pass would INSERT a fresh NULL-dim duplicate that never conflicts.
--   * Measures are a full REPLACE on conflict, never additive — the rollup
--     re-aggregates whole closed days from an append-only source, so a re-run
--     overwrites in place (idempotent; retries and the trailing re-close window
--     converge instead of double-counting).
-- ============================================================================

SET LOCAL timezone = 'UTC';

CREATE TABLE IF NOT EXISTS usage_daily (
    day         DATE         NOT NULL,
    user_id     UUID,
    project_id  UUID,
    category    TEXT         NOT NULL,
    resource    TEXT         NOT NULL,
    unit        TEXT         NOT NULL,
    quantity    NUMERIC      NOT NULL,
    cost_usd    NUMERIC      NOT NULL DEFAULT 0,
    events      BIGINT       NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE usage_daily IS
    'Daily pre-aggregated usage rollup (app-DB read model). Mirrors the auditdb '
    'usage_events ledger summed per UTC day x (user, project, category, resource, '
    'unit). /api/usage serves this for CLOSED days (day <= rollup_state watermark) '
    'and the raw ledger for the open tail. Maintained by services/usage_rollup.py '
    'via full-replace upserts — never hand-written. Per-job cost stays on raw '
    'usage_events (ref_id is not a dim here).';

-- NULLS NOT DISTINCT (PG15+): unattributed rows (user_id/project_id NULL) must
-- collapse to one bucket per (day, category, resource, unit) so the upsert
-- updates in place. Without it every pass would insert a fresh NULL-dim row that
-- the ON CONFLICT never matches, and the rollup would double-count on re-run.
CREATE UNIQUE INDEX usage_daily_dims_idx
    ON usage_daily (day, user_id, project_id, category, resource, unit)
    NULLS NOT DISTINCT;

-- Non-admin serving is self-scoped (user_id = caller); the unique index leads
-- with `day`, so it cannot serve that filter. One helper for the common path.
CREATE INDEX usage_daily_user_day_idx ON usage_daily (user_id, day);

-- ============================================================================
-- rollup_state — one watermark row per named rollup. last_closed_day is the
-- newest UTC day fully aggregated into usage_daily; days after it are served
-- raw. Advanced in the SAME transaction as the usage_daily upsert so the pair is
-- exactly-once across the audit-read / app-write boundary (a crash rolls both
-- back and the next pass re-closes the same trailing window).
-- ============================================================================
CREATE TABLE IF NOT EXISTS rollup_state (
    name            TEXT PRIMARY KEY,
    last_closed_day DATE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE rollup_state IS
    'Rollup watermarks — one row per named rollup (e.g. usage_daily). '
    'last_closed_day = newest UTC day fully rolled up (NULL = never run). '
    'Advanced atomically with the rollup upsert (cross-DB exactly-once).';

INSERT INTO rollup_state (name, last_closed_day)
VALUES ('usage_daily', NULL)
ON CONFLICT (name) DO NOTHING;
