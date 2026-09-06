-- migration:     0024_knowledge_content_trgm_index.notx.sql
-- description:   GIN trigram index over note bodies so `ILIKE '%term%'` and
--                `~*` in KnowledgeStore.grep_notes and the `exact=` search arm
--                take an index scan instead of reading every body
--                (kb_retrieval_hardening_and_slice_d_additive.md WP6, H8).
--                Titles deliberately not indexed: ~9k short rows.
-- depends-on:    0023_pg_trgm_extension.sql
-- expected:      Concurrent build; minutes at 10k rows with 250 KB bodies.
-- locks:         ShareUpdateExclusiveLock only. CREATE INDEX CONCURRENTLY.
-- transactional: NO. One statement only.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_knowledge_content_trgm
    ON knowledge_index USING gin (content gin_trgm_ops);
