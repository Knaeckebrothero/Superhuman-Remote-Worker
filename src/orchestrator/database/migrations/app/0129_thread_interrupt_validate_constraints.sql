-- migration:     0129_thread_interrupt_validate_constraints.sql
-- description:   Validate the exact interrupt-admission window shape and the
--                composite interrupt-receipt foreign key added by 0127.
-- depends-on:    0127_thread_interrupt_inbox.sql (the receipt index in 0128 is
--                independent and the runner applies .notx files last)
-- expected:      Proportional to run_queue + thread_events. Validation uses
--                SHARE UPDATE EXCLUSIVE locks, allowing normal reads/writes.
-- locks:         SHARE UPDATE EXCLUSIVE on run_queue and thread_events.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout      = '2s';
SET LOCAL statement_timeout = '10min';

ALTER TABLE run_queue
    VALIDATE CONSTRAINT run_queue_interrupt_admission_shape;

ALTER TABLE thread_events
    VALIDATE CONSTRAINT thread_events_interrupt_request_thread_fkey;

COMMIT;
