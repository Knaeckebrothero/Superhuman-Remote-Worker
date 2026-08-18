-- migration:     0115_datasource_tombstones.sql
-- description:   Give deleted connectors a readable name so a session that
--                still references one can say WHICH connector vanished,
--                per docs/features/session_config_drift_resume.md §5.1.
-- depends-on:    0114_compute_interval_epoch_shape_repair.sql
-- expected:      < 100ms. New, empty table; no existing-table changes.
-- locks:         None on existing tables. Brief AccessExclusiveLock while
--                the new table itself is created.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Deleted connectors leave a name behind so a session that still references
-- one can say WHICH connector vanished. Append-only; never joined in a hot
-- path. See docs/features/session_config_drift_resume.md §5.1.
CREATE TABLE IF NOT EXISTS datasource_tombstones (
    id          UUID PRIMARY KEY,
    name        TEXT NOT NULL,
    deleted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_by  UUID
);

COMMENT ON TABLE datasource_tombstones IS
    'Names of deleted connectors, kept so drifted session config can label a dangling datasource_id instead of showing a bare uuid.';

COMMIT;
