-- migration:     0207_threads_parent_job_idx.notx.sql
-- description:   Partial index on threads.parent_job_id (0206) for the per-job
--                subagent roster (GET /api/jobs/{job_id}/subagents) and the
--                (parent_job_id, parent_tool_call_id) replay lookup, built
--                concurrently.
-- depends-on:    0206_threads_subagent_kind.sql
-- expected:      One index build over threads with no exclusive lock. Partial,
--                so it holds only child rows: every session row keeps a NULL
--                parent, and a full index would add an entry per session plus
--                write amplification on a hot table for nothing. The tool
--                call id is deliberately not a second key column — a job has
--                at most tens of children, so the parent_job_id entry already
--                narrows the replay lookup to a handful of rows.
-- locks:         SHARE UPDATE EXCLUSIVE only (CONCURRENTLY).
-- transactional: no
-- rollout:       Deliberately NOT "IF NOT EXISTS", following 0132's and
--                0203's runbook: IF NOT EXISTS reports success against an
--                INVALID same-name shell left by a failed concurrent build,
--                which would record 0207 as applied while every roster read
--                seq-scanned threads. It buys nothing on the success path
--                either — the runner re-reads schema_migrations and skips an
--                applied .notx migration. So a failed build must be repaired
--                explicitly:
--                    SELECT i.indisvalid, i.indisready
--                      FROM pg_index AS i
--                      JOIN pg_class AS c ON c.oid = i.indexrelid
--                     WHERE c.relname = 'idx_threads_parent_job';
--                DROP INDEX CONCURRENTLY the invalid shell, repair the dirty
--                ledger row, then re-run. The squawk-ignore below acknowledges
--                exactly that prefer-robust-stmts trade-off (0203 shipped the
--                same shape before the pinned-linter pass).

-- squawk-ignore prefer-robust-stmts
CREATE INDEX CONCURRENTLY idx_threads_parent_job
    ON public.threads (parent_job_id)
    WHERE parent_job_id IS NOT NULL;
