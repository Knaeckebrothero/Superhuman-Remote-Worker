-- migration:     0004_memory_retrieval_messages_embedding_halfvec_hnsw.notx.sql
-- description:   Companion to 0002 (B2): HNSW index over the 4000-dim
--                halfvec prefix of memory_retrieval_messages.embedding
--                (trigger-phrase channel of the hybrid-search dense CTE).
--                Same shape and rationale as 0003.
-- depends-on:    0002_hybrid_search_halfvec_casts.sql
-- expected:      sub-second at current sizes (dev: 3332 rows). Concurrent
--                build; safe on a populated table.
-- locks:         ShareUpdateExclusiveLock — CREATE INDEX CONCURRENTLY
--                doesn't block reads or writes.
-- transactional: NO. CONCURRENTLY can't run inside a transaction block;
--                hence the .notx.sql suffix the runner recognises. One
--                statement per file: asyncpg executes a multi-statement
--                string as one implicit transaction, which CONCURRENTLY
--                also rejects.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memory_retrieval_messages_embedding_halfvec
    ON memory_retrieval_messages USING hnsw ((subvector(embedding, 1, 4000)::halfvec(4000)) halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 256);
