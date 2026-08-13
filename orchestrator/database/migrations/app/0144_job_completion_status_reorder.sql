-- migration:     0144_job_completion_status_reorder.sql
-- description:   Persist the independently revertible status-reorder
--                admission decision on every completion command.
-- depends-on:    0143_job_completion_accept_status.sql
-- expected:      < 5s. One catalog-only constant-default column addition.
-- locks:         Brief ACCESS EXCLUSIVE on job_completion_commands.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE job_completion_commands
    ADD COLUMN status_reorder_enabled BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN job_completion_commands.status_reorder_enabled IS
    'Status-reorder policy captured at fresh admission. False preserves the '
    'legacy status-first order; exact retries and resumed finalization use '
    'this stored decision rather than the process-global rollout flag.';

COMMIT;
