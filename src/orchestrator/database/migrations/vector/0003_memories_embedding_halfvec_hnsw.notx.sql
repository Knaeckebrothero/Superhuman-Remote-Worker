-- migration:     0003_memories_embedding_halfvec_hnsw.notx.sql
-- description:   Companion to 0002 (B2): HNSW index over the 4000-dim
--                halfvec prefix of memories.embedding, the expression the
--                hybrid-search dense channel now orders by. m/ef_construction
--                match what 0001 intended for this table. Planner-gated
--                growth hedge: small job/project scopes keep the btree+sort
--                plan; this engages as scopes grow.
-- depends-on:    0002_hybrid_search_halfvec_casts.sql
-- expected:      sub-second at current sizes (dev: 942 rows). Concurrent
--                build; safe on a populated table.
-- locks:         ShareUpdateExclusiveLock — CREATE INDEX CONCURRENTLY
--                doesn't block reads or writes against memories.
-- transactional: NO. CONCURRENTLY can't run inside a transaction block;
--                hence the .notx.sql suffix the runner recognises. One
--                statement per file: asyncpg executes a multi-statement
--                string as one implicit transaction, which CONCURRENTLY
--                also rejects.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memories_embedding_halfvec
    ON memories USING hnsw ((subvector(embedding, 1, 4000)::halfvec(4000)) halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 256);
