-- migration:     0131_drop_jobs_verification_uniq.notx.sql
-- description:   Remove any same-name verification index shell before the
--                exact unique index is rebuilt by 0132. This handles both a
--                valid unledgered index and an INVALID shell left by an
--                interrupted or duplicate-blocked concurrent build.
-- depends-on:    0130_jobs_verification_dedupe.sql
-- expected:      Usually a no-op. DROP INDEX CONCURRENTLY waits out users of
--                an existing shell without blocking ordinary jobs writes.
-- locks:         ShareUpdateExclusiveLock only (CONCURRENTLY).
-- transactional: NO (.notx -- DROP INDEX CONCURRENTLY cannot run in a txn)
-- runbook:       Repairing a dirty 0132 requires clearing the successful 0131
--                ledger row too, so this drop reruns before create. Confirm
--                health after 0132 with pg_index.indisvalid/indisready.

DROP INDEX CONCURRENTLY IF EXISTS jobs_verification_uniq;
