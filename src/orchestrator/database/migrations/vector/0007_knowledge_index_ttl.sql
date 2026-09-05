-- migration:     0007_knowledge_index_ttl.sql
-- description:   KB convergence (docs/features/kb_convergence_ttl_reverification.md)
--                — give knowledge_index the TTL lifecycle the `memories` table
--                already has (0006). Adds a per-note cycle counter so stale
--                governance notes can be re-verified (refresh/supersede/merge/
--                retire) by the new knowledge-assembler aux pass instead of
--                staying `active` and competing in search forever (loop_review
--                F13).
--
--                Columns:
--                  - remaining_cycles INT  TTL in *loop cycles*. NULL = no TTL
--                                 (durable facts: decision/learning/code). The
--                                 orchestrator decrements this once per cycle
--                                 (at the developer→scholar wrap); a note that
--                                 reaches <= 0 enters the stale queue and is
--                                 re-verified. Set per note_type at write time
--                                 by the app (KnowledgeStore._ttl_for_note_type),
--                                 NOT here — existing rows stay NULL (durable),
--                                 so this migration retires nothing on its own.
--                  - last_verified_cycle INT  bookkeeping: total_jobs_run at the
--                                 last re-verification (observability; nullable).
--
--                Retirement itself needs NO function rewrite: the knowledge
--                hybrid-search functions already filter `status = 'active'`
--                (see 0006 header §3), so a note the assembler marks superseded/
--                archived drops out of retrieval + injection automatically.
-- depends-on:    0001_initial.sql
-- expected:      < 1s on dev — two ALTER ADD COLUMN (fast, no default rewrite)
--                + one partial btree. No vector data touched, no table rewrite.
-- locks:         brief ACCESS EXCLUSIVE on `knowledge_index` for the ALTERs.
-- transactional: YES.

-- ---------------------------------------------------------------------------
-- 1. Columns
-- ---------------------------------------------------------------------------
ALTER TABLE knowledge_index ADD COLUMN IF NOT EXISTS remaining_cycles INT;
ALTER TABLE knowledge_index ADD COLUMN IF NOT EXISTS last_verified_cycle INT;

-- ---------------------------------------------------------------------------
-- 2. Stale-queue index — partial on (project_id) for active notes whose TTL has
--    run out. This is the exact predicate KnowledgeStore.get_stale_notes uses.
--    NULL remaining_cycles (durable) is excluded by `remaining_cycles <= 0`.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_knowledge_index_stale
    ON knowledge_index (project_id)
    WHERE remaining_cycles <= 0 AND status = 'active';
