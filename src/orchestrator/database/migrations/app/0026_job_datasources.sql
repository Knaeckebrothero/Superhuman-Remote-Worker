-- migration:     0026_job_datasources.sql
-- description:   Job ↔ datasource junction table. Records the explicit
--                datasource selection for a job (mirrors project_datasources
--                and the thread metadata.datasource_ids list). Part of the
--                explicit-only datasource attachment rollout
--                (docs/features/multi_datasource_support.md): resolution now
--                returns only the datasources explicitly selected for a job
--                instead of force-attaching every unlinked "global" datasource.
--
--                Replaces the legacy clone-to-job-scoped mechanism
--                (datasources.job_id, flagged for removal): selections are now
--                links to the canonical datasource rows, not credential-copying
--                clones. datasources.job_id is left in place for one release so
--                in-flight pre-migration jobs keep resolving via the fallback
--                in resolve_datasources_for_job; it is removed in a later
--                cleanup migration once no live job predates this one.
-- depends-on:    0001_initial.sql
-- expected:      < 1s on dev DB. New empty table; no data movement.
-- locks:         AccessExclusiveLock on the new table only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS job_datasources (
    job_id        UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    datasource_id UUID NOT NULL REFERENCES datasources(id) ON DELETE CASCADE,
    linked_at     TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (job_id, datasource_id)
);

CREATE INDEX IF NOT EXISTS idx_job_datasources_ds ON job_datasources(datasource_id);

COMMENT ON TABLE job_datasources IS
    'Explicit datasource selection for a job (the picker is the source of '
    'truth). Resolution returns only these links; replaces the legacy '
    'clone-to-job-scoped datasources.job_id mechanism.';

COMMIT;
