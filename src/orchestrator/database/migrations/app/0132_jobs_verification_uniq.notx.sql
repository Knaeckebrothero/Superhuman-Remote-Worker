-- migration:     0132_jobs_verification_uniq.notx.sql
-- description:   Enforce one verification critic per parent and round, ever.
--                Status is deliberately absent from the immutable predicate.
-- depends-on:    0130_jobs_verification_dedupe.sql,
--                0131_drop_jobs_verification_uniq.notx.sql
-- expected:      Proportional to jobs; CONCURRENTLY keeps ordinary reads and
--                writes available while PostgreSQL scans the table.
-- locks:         ShareUpdateExclusiveLock only (CONCURRENTLY).
-- transactional: NO (.notx -- CREATE INDEX CONCURRENTLY cannot run in a txn)
-- runbook:       Never add IF NOT EXISTS: it reports success against an
--                INVALID same-name shell. Detect one explicitly with:
--                    SELECT i.indisvalid, i.indisready
--                    FROM pg_index AS i
--                    JOIN pg_class AS c ON c.oid = i.indexrelid
--                    WHERE c.relname = 'jobs_verification_uniq';
--                On failure, repair the dirty migration ledger rows for both
--                0131 and 0132 and rerun so 0131 drops the shell first.

CREATE UNIQUE INDEX CONCURRENTLY jobs_verification_uniq
    ON jobs (parent_job_id, (context->>'verification_round'))
    WHERE context->>'verification_target' IS NOT NULL
      AND jsonb_exists(context, 'verification_round');
