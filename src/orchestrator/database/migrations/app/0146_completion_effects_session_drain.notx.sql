-- migration:     0146_completion_effects_session_drain.notx.sql
-- description:   Add the non-partial ordered scan used by independent
--                session-turn effect drainers and retention batches.
-- depends-on:    0145_session_turn_memory_effects.sql
-- expected:      Proportional to completion_effects; CONCURRENTLY keeps
--                ordinary effect producers and finalizers available.
-- locks:         ShareUpdateExclusiveLock only (CONCURRENTLY).
-- transactional: NO (.notx -- CREATE INDEX CONCURRENTLY cannot run in a txn)
-- runbook:       If CONCURRENTLY is interrupted it leaves an INVALID index.
--                IF NOT EXISTS will not rebuild that same-name shell. Recover:
--                    DROP INDEX CONCURRENTLY IF EXISTS
--                        idx_completion_effects_session_drain;
--                then repair the dirty migration row and rerun. Detect with:
--                    SELECT indexrelid::regclass FROM pg_index
--                    WHERE NOT indisvalid
--                      AND indexrelid::regclass::text =
--                          'idx_completion_effects_session_drain';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_completion_effects_session_drain
    ON completion_effects (producer_kind, state, run_after, created_at);
