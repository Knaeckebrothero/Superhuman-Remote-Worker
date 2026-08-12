-- migration:     0116_events_seq_hwm.sql
-- description:   Per-epoch seq high-water mark for the thread event journal
--                (stateless agents M2, docs/features/stateless_agents.md
--                §5.3.2). events_seq_hwm records the highest seq ever
--                allocated in the thread's CURRENT events_epoch and survives
--                retention pruning of the thread_events rows themselves, so
--                an attach can seed its seq allocator above every seq a
--                client may have cached even after the rows are gone. This
--                is what makes conditional epoch REUSE on attach safe
--                (killing the full-cache-wipe client cascade on every
--                reattach) and what the system-frame allocator
--                (src/shared/event_journal/) increments to allocate.
-- depends-on:    0004_thread_events.sql
-- expected:      < 5s. ADD COLUMN with a constant default is catalog-only on
--                PG11+; the backfill UPDATE touches every threads row once
--                (small table, thousands of rows at most).
-- locks:         Brief ACCESS EXCLUSIVE on threads for the ADD COLUMN; the
--                backfill takes ordinary row locks on threads.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '10min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE threads
    ADD COLUMN events_seq_hwm BIGINT NOT NULL DEFAULT 0;

-- Seed from the surviving rows of each thread's current epoch. An epoch whose
-- rows were already fully pruned backfills to 0 — indistinguishable from a
-- never-used epoch — which is why the attach-time resolver treats an empty
-- epoch with a non-zero epoch number as beyond-retention and bumps instead of
-- reusing. From here on the journal writer's fenced flush and the
-- system-frame allocator maintain the mark themselves.
UPDATE threads
SET events_seq_hwm = COALESCE(
    (SELECT MAX(te.seq)
     FROM thread_events te
     WHERE te.thread_id = threads.id
       AND te.epoch = threads.events_epoch),
    0
);

COMMENT ON COLUMN threads.events_seq_hwm IS
    'Highest seq ever allocated in the CURRENT events_epoch. Survives retention pruning of the thread_events rows themselves; reset to 0 atomically on every epoch bump. Maintained by the agent journal writer''s fenced flush (GREATEST over the batch in the same statement) and pre-incremented by the system-frame allocator (src/shared/event_journal). Attach seeds its in-process counter from GREATEST(events_seq_hwm, MAX(seq) of the epoch). See docs/features/stateless_agents.md §5.3.2.';

-- Refresh the events_epoch semantics comment (last set in 0060): allocation
-- is no longer unconditional per attach.
COMMENT ON COLUMN threads.events_epoch IS
    'Current event-log writer generation (client-visible). Bumped only deliberately: rewind, a reaper/steal takeover, or an attach that finds the previous session life terminal (terminal thread status, a terminal lifecycle frame in the epoch, or the epoch wholly beyond retention). Clean reattaches REUSE the epoch so cached client cursors stay valid; an older-epoch cursor triggers authoritative re-sync (gone_beyond_horizon). See docs/features/stateless_agents.md §5.3.2.';

COMMIT;
