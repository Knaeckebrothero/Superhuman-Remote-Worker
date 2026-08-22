-- migration:     0179_sudo_requests_entity_check.sql
-- description:   Exactly one of job_id / thread_id owns a sudo approval request.
--                Separate file from 0178 so the CHECK can be validated on its
--                own (squawk: constraint additions are their own step).
-- depends-on:    0178_sudo_requests_thread_scope.sql
-- expected:      < 1s. Validates existing rows (all have job_id) in one scan
--                of a small table.
-- locks:         SHARE ROW EXCLUSIVE on sudo_approval_requests during validation.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.sudo_approval_requests
    ADD CONSTRAINT sudo_approval_requests_one_entity
    CHECK (num_nonnulls(job_id, thread_id) = 1);

COMMIT;
