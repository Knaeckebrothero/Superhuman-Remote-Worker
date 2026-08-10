-- migration:     0128_thread_interrupt_receipt_idx.notx.sql
-- description:   Enforce one durable journal receipt per exact-lease
--                interrupt request without blocking the existing event log.
-- depends-on:    0127_thread_interrupt_inbox.sql
-- expected:      Proportional to thread_events; CONCURRENTLY keeps ordinary
--                reads and writes available while PostgreSQL scans it.
-- locks:         ShareUpdateExclusiveLock only (CONCURRENTLY).
-- transactional: NO (.notx -- CREATE INDEX CONCURRENTLY cannot run in a txn)
-- runbook:       If CONCURRENTLY is interrupted it leaves an INVALID index.
--                IF NOT EXISTS will not rebuild that same-name shell. Recover:
--                    DROP INDEX CONCURRENTLY IF EXISTS
--                        idx_thread_events_interrupt_request;
--                then repair the dirty migration row and rerun. Detect with:
--                    SELECT indexrelid::regclass FROM pg_index
--                    WHERE NOT indisvalid
--                      AND indexrelid::regclass::text =
--                          'idx_thread_events_interrupt_request';

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_thread_events_interrupt_request
    ON thread_events (interrupt_request_id)
    WHERE interrupt_request_id IS NOT NULL;
