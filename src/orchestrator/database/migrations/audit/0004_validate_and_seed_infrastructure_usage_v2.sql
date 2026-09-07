-- migration:     0004_validate_and_seed_infrastructure_usage_v2.sql
-- description:   Validate the typed usage-event checks and seed every retained
--                audit day as dirty for the gated usage_daily_v2 bootstrap.
-- depends-on:    0003_infrastructure_usage_events_v2.sql
-- expected:      Proportional to retained usage_events. Constraint validation
--                and the distinct-day seed scan the attached partitions.
-- locks:         SHARE UPDATE EXCLUSIVE during CHECK validation; ordinary
--                usage inserts continue and the 0003 trigger tracks them.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '30min';
SET LOCAL idle_in_transaction_session_timeout = '35min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE usage_events
    VALIDATE CONSTRAINT usage_events_period_bounds_v2_check;

ALTER TABLE usage_events
    VALIDATE CONSTRAINT usage_events_infra_v2_contract_check;

ALTER TABLE usage_events
    VALIDATE CONSTRAINT usage_events_event_kind_v2_check;

-- Existing immutable rows predate the dirty trigger. Seed one initial token for
-- every retained day; concurrently inserted rows either increment an existing
-- token or create their own, and ON CONFLICT never lowers that revision.
INSERT INTO usage_rollup_dirty_days (day, revision, updated_at)
SELECT
    (ts AT TIME ZONE 'UTC')::DATE,
    1,
    statement_timestamp()
FROM usage_events
GROUP BY (ts AT TIME ZONE 'UTC')::DATE
ON CONFLICT (day) DO NOTHING;

ANALYZE usage_rollup_dirty_days;

COMMIT;
