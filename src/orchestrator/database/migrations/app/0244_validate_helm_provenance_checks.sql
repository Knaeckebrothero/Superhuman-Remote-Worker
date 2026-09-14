-- migration:     0244_validate_helm_provenance_checks.sql
-- description:   Validate the four source CHECK constraints 0243 added
--                NOT VALID.
-- depends-on:    0243_helm_provenance_columns.sql
-- expected:      < 1s. Scans four small admin-curated tables; every row was
--                written with a value from the enum (column default or the
--                0243 backfill).
-- locks:         SHARE UPDATE EXCLUSIVE on each table; no writes are blocked.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '1min';

ALTER TABLE system_api_keys VALIDATE CONSTRAINT system_api_keys_source_check;
ALTER TABLE llm_endpoints VALIDATE CONSTRAINT llm_endpoints_source_check;
ALTER TABLE models VALIDATE CONSTRAINT models_source_check;
ALTER TABLE system_settings VALIDATE CONSTRAINT system_settings_source_check;

COMMIT;
