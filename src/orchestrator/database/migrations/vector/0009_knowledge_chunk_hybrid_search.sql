-- migration:     0009_knowledge_chunk_hybrid_search.sql
-- description:   OKF files-canonical KB — slice 3 PR4 (docs/features/okf_knowledge_base.md
--                §5.1 / §11 slice-3 PR4). The RETRIEVAL half of the cutover: the
--                function `search_knowledge`/`kb_search` will call once PR4 flips
--                the store method over. 0008 laid down `knowledge_chunks` (the
--                dense vector moved off the note row); this function fuses over
--                those chunk rows and collapses the best chunk back to its note.
--
--                knowledge_chunk_hybrid_search — RRF (Reciprocal Rank Fusion) over
--                three arms, mirroring knowledge_hybrid_search's 0.6/0.3/0.1 shape:
--                  - dense:   chunk embedding <=> query (HNSW on the halfvec(4000)
--                             prefix, idx_knowledge_chunks_embedding from 0008),
--                             over-fetched then collapsed to the BEST chunk rank
--                             per note (MIN(rank_ix) GROUP BY note_row).
--                  - sparse:  chunk search_doc @@ query (GIN, chunk-granular tsvector),
--                             same best-chunk-per-note collapse.
--                  - recency: note-level modified_at, but ONLY for notes that have
--                             at least one chunk of the current pipeline version —
--                             so file-less legacy "ghost" rows (active, no path, no
--                             chunks) stay invisible to chunk retrieval, matching
--                             the design's promise. Without this EXISTS guard the
--                             recency arm would resurrect ghosts on freshness alone.
--
--                Scope + drift guards:
--                  - kb_ids_param uuid[]  ANY(...) — a project's KB is kb_id =
--                    project_id today; the array lets a caller search several KBs
--                    at once (the multi-project path the note-level function split
--                    into a second function; one array-scoped function covers both).
--                  - version_param text   WHEN NOT NULL, filters chunks to a single
--                    embedding_version so mixed-model vectors can't silently drift
--                    (great results on fresh rows, garbage on stale, no error). NULL
--                    disables it (single-model deployments).
--                  - status = 'active' preserved EXACTLY (stricter than "not
--                    superseded" — also excludes resolved/archived).
--
--                Returns SETOF knowledge_index (note-level rows), so the
--                KnowledgeRecord mapping and the kb_search tool signature are
--                unchanged; over-fetch/rerank/truncate happen in the store method.
--                rrf_k default 60 (the literature standard; 50 on the note-level
--                function is the older value — a tunable, not a rewrite).
-- depends-on:    0008_kb_index_chunking.sql
-- expected:      < 1s — one CREATE OR REPLACE FUNCTION, no data touched, no locks
--                beyond the catalog entry. Idempotent (CREATE OR REPLACE).
-- transactional: YES.

CREATE OR REPLACE FUNCTION knowledge_chunk_hybrid_search(
    query_text text,
    query_embedding vector,
    kb_ids_param uuid[],
    version_param text DEFAULT NULL,
    match_count int DEFAULT 15,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 60
) RETURNS SETOF knowledge_index LANGUAGE sql
SET hnsw.iterative_scan = relaxed_order
AS $$
-- Dense arm: top chunks by vector distance (HNSW), collapsed to the best-ranked
-- chunk per note. Over-fetch match_count * 4 chunks so a note whose best chunk
-- ranks behind several other notes' chunks still survives the collapse.
WITH dense AS (
    SELECT mid, MIN(rank_ix) AS rank_ix FROM (
        SELECT c.note_row AS mid,
               ROW_NUMBER() OVER (
                   ORDER BY subvector(c.embedding, 1, 4000)::halfvec(4000)
                            <=> subvector(query_embedding, 1, 4000)::halfvec(4000)
               ) AS rank_ix
        FROM knowledge_chunks c
        JOIN knowledge_index ki ON ki.id = c.note_row
        WHERE c.kb_id = ANY(kb_ids_param)
          AND ki.status = 'active'
          AND c.embedding IS NOT NULL
          AND (version_param IS NULL OR c.embedding_version = version_param)
        ORDER BY subvector(c.embedding, 1, 4000)::halfvec(4000)
                 <=> subvector(query_embedding, 1, 4000)::halfvec(4000)
        LIMIT match_count * 4
    ) ranked_chunks
    GROUP BY mid
),
-- Sparse arm: chunk-granular tsvector match, same best-chunk-per-note collapse.
sparse AS (
    SELECT mid, MIN(rank_ix) AS rank_ix FROM (
        SELECT c.note_row AS mid,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank_cd(c.search_doc, websearch_to_tsquery('english', query_text)) DESC
               ) AS rank_ix
        FROM knowledge_chunks c
        JOIN knowledge_index ki ON ki.id = c.note_row
        WHERE c.kb_id = ANY(kb_ids_param)
          AND ki.status = 'active'
          AND (version_param IS NULL OR c.embedding_version = version_param)
          AND c.search_doc @@ websearch_to_tsquery('english', query_text)
        ORDER BY ts_rank_cd(c.search_doc, websearch_to_tsquery('english', query_text)) DESC
        LIMIT match_count * 4
    ) ranked_chunks
    GROUP BY mid
),
-- Recency arm: note-level freshness, restricted to notes that actually carry
-- chunks of the current pipeline version (keeps ghosts out — see header).
recent AS (
    SELECT ki.id AS mid, ROW_NUMBER() OVER (ORDER BY ki.modified_at DESC) AS rank_ix
    FROM knowledge_index ki
    WHERE ki.kb_id = ANY(kb_ids_param) AND ki.status = 'active'
      AND EXISTS (
          SELECT 1 FROM knowledge_chunks c
          WHERE c.note_row = ki.id
            AND (version_param IS NULL OR c.embedding_version = version_param)
      )
    ORDER BY rank_ix LIMIT match_count
)
SELECT ki.* FROM (
    SELECT COALESCE(d.mid, s.mid, r.mid) AS mid,
        COALESCE(1.0 / (rrf_k + d.rank_ix), 0.0) * dense_weight +
        COALESCE(1.0 / (rrf_k + s.rank_ix), 0.0) * sparse_weight +
        COALESCE(1.0 / (rrf_k + r.rank_ix), 0.0) * recency_weight AS rrf_score
    FROM dense d
             FULL OUTER JOIN sparse s ON d.mid = s.mid
             FULL OUTER JOIN recent r ON COALESCE(d.mid, s.mid) = r.mid) ranked
                     JOIN knowledge_index ki ON ranked.mid = ki.id
ORDER BY ranked.rrf_score DESC LIMIT match_count
$$;
