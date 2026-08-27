-- migration:     0081_job_change_records_survive_job_deletion.sql
-- description:   Keep structured terminal history after execution-row cleanup.
-- depends-on:    0080_job_change_records.sql
-- expected:      < 1s; drops one empty/new foreign-key constraint.
-- locks:         Brief ACCESS EXCLUSIVE lock on job_change_records.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE job_change_records
    DROP CONSTRAINT IF EXISTS job_change_records_job_id_fkey;

COMMENT ON COLUMN job_change_records.job_id IS
    'Execution job identifier without a foreign key: project history survives '
    'job-row and isolated-repository cleanup.';

COMMIT;
