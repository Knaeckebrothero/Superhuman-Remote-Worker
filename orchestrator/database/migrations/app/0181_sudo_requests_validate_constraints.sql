-- migration:     0181_sudo_requests_validate_constraints.sql
-- description:   Validate the thread ownership FK and exactly-one-entity CHECK
--                after the non-transactional thread lookup index is present.
-- depends-on:    0180_sudo_requests_thread_idx.notx.sql
-- expected:      < 1s. Existing requests are job-scoped and the approval table
--                holds only a few thousand rows.
-- locks:         SHARE UPDATE EXCLUSIVE while each constraint scans the table.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.sudo_approval_requests
    VALIDATE CONSTRAINT sudo_approval_requests_thread_id_fkey;

ALTER TABLE public.sudo_approval_requests
    VALIDATE CONSTRAINT sudo_approval_requests_one_entity;

COMMIT;
