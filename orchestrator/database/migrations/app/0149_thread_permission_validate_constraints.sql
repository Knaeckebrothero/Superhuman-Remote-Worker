-- migration:     0149_thread_permission_validate_constraints.sql
-- description:   Validate the exact-lease permission token and composite
--                permission-receipt foreign key added by 0147.
-- depends-on:    0147_thread_permission_lease_receipts.sql (the receipt index
--                in 0148 is independent and the runner applies .notx last)
-- expected:      Proportional to thread_permission_requests + thread_events.
--                Validation allows normal reads and writes.
-- locks:         SHARE UPDATE EXCLUSIVE on thread_permission_requests and
--                thread_events.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout      = '2s';
SET LOCAL statement_timeout = '10min';

ALTER TABLE thread_permission_requests
    VALIDATE CONSTRAINT thread_permission_accepted_lease_positive;

ALTER TABLE thread_events
    VALIDATE CONSTRAINT thread_events_permission_request_thread_fkey;

COMMIT;
