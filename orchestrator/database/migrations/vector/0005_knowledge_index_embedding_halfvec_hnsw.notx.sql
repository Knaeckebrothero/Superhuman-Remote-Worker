-- migration:     0005_knowledge_index_embedding_halfvec_hnsw.notx.sql
-- description:   Companion to 0002 (B2): HNSW index over the 4000-dim
--                halfvec prefix of knowledge_index.embedding (dense channel
--                of knowledge_hybrid_search / the multi-project variant).
--                Same shape and rationale as 0003.
-- depends-on:    0002_hybrid_search_halfvec_casts.sql
-- expected:      sub-second at current sizes (dev: 169 rows). Concurrent
--                build; safe on a populated table.
-- locks:         ShareUpdateExclusiveLock — CREATE INDEX CONCURRENTLY
--                doesn't block reads or writes.
-- transactional: NO. CONCURRENTLY can't run inside a transaction block;
--                hence the .notx.sql suffix the runner recognises. One
--                statement per file: asyncpg executes a multi-statement
--                string as one implicit transaction, which CONCURRENTLY
--                also rejects.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_knowledge_embedding_halfvec
    ON knowledge_index USING hnsw ((subvector(embedding, 1, 4000)::halfvec(4000)) halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 256);
