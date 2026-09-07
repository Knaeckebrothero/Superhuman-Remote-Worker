-- migration:     0055_datasource_config.sql
-- description:   Add non-secret, type-specific datasource configuration.
--                OKF knowledge base datasources use config.root_path while
--                repository credentials remain in the encrypted credentials
--                column.
-- depends-on:    0054_jobs_execution_lease.sql
-- expected:      < 1s on normal tables; brief table lock for ADD COLUMN.
-- locks:         Brief ACCESS EXCLUSIVE on datasources.
-- transactional: yes
-- ============================================================================

ALTER TABLE datasources
    ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN datasources.config IS
    'Non-secret type-specific datasource configuration. Credentials and tokens must not be stored here.';
