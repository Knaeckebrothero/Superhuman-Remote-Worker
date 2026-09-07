-- migration:     0005_usage_events_project_ts_idx.sql
-- description:   Add the raw project/window read index required by typed v2
--                usage authorization and reconciliation paths.
-- depends-on:    0004_validate_and_seed_infrastructure_usage_v2.sql
-- expected:      Proportional to retained usage_events. PostgreSQL 16 cannot
--                CREATE INDEX CONCURRENTLY on a partitioned parent, so this
--                builds the parent plus every attached leaf in one migration.
-- locks:         SHARE locks on usage_events leaves while each index builds;
--                inserts are blocked for the build. Schedule as a maintenance
--                migration on large ledgers and retry on lock_timeout.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '30min';
SET LOCAL idle_in_transaction_session_timeout = '35min';
SET LOCAL timezone                            = 'UTC';

-- Creating the index on the partitioned parent recursively creates attached
-- leaf indexes and ensures partitions attached later carry the same index.
CREATE INDEX IF NOT EXISTS usage_events_project_ts_idx
    ON usage_events (project_id, ts);

COMMIT;
