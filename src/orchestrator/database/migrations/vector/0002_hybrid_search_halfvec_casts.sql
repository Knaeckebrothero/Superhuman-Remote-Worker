-- migration:     0002_hybrid_search_halfvec_casts.sql
-- description:   B2 (docs/issues/memory_bugs.md): make dense retrieval
--                HNSW-indexable. 0001's DO-block index attempts always
--                failed silently — the vector type caps HNSW at 2000 dims
--                and every embedding column is vector(4096); verified zero
--                vector indexes on both dev and prod (2026-06-10). halfvec
--                HNSW caps at 4000 dims (not 4096!), so this uses the
--                documented pgvector pattern for >4000-dim columns: order
--                by subvector(embedding, 1, 4000)::halfvec(4000), matching
--                the expression indexes added in 0003-0005. qwen3-embedding
--                is MRL-trained, so prefix cosine tracks full cosine
--                (rank-identical on live dev rows when tested).
--
--                Scope of change — the five hybrid-search functions only,
--                bodies otherwise byte-identical to 0001:
--                  - dense-channel distance expressions get the subvector/
--                    halfvec cast on both operands. These distances feed
--                    only RRF rank fusion and are never returned or stored,
--                    so fp16 prefix precision affects ranking only.
--                  - SET hnsw.iterative_scan = relaxed_order per function:
--                    with the default 'off', a filtered HNSW scan stops at
--                    ef_search candidates and a scope filter (job_id /
--                    project_id) can silently empty the dense channel. The
--                    setting is a no-op until the planner actually picks
--                    the HNSW index — small scopes keep using the scope
--                    btrees + sort, which is exact and optimal. (A SET
--                    clause also disables SQL-function inlining; these are
--                    only ever called top-level, so that is irrelevant.)
--
--                Deliberately untouched: find_similar() in recall_store.py
--                (dedup threshold semantics want exact full-precision
--                distance; not a hot path) and source_embeddings (0 rows on
--                every cluster and no dense read path exists anywhere).
--                ef_search tuning is deferred to overhaul Phase 3
--                (docs/features/agent_memory_overhaul.md).
-- depends-on:    0001_initial.sql
-- expected:      < 1s — five CREATE OR REPLACE FUNCTION, no data touched.
--                Note the runner applies this transactional file before the
--                0003-0005 .notx index builds in the same startup; in that
--                brief window the casts run unindexed (correct results,
--                existing btree/seq plans).
-- locks:         row locks in pg_proc only; no table locks.
-- transactional: YES.

CREATE OR REPLACE FUNCTION memory_hybrid_search(
    query_text text,
    query_embedding vector(4096),
    job_id_param uuid,
    match_count int DEFAULT 10,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 50,
    importance_floor float DEFAULT 0.0
) RETURNS SETOF memories LANGUAGE sql
SET hnsw.iterative_scan = relaxed_order
AS $$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY best_dist) AS rank_ix
    FROM (
        SELECT id, MIN(dist) AS best_dist
        FROM (
            -- Content embedding matches
            (SELECT id, subvector(embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000) AS dist
            FROM memories WHERE job_id = job_id_param AND embedding IS NOT NULL AND importance >= importance_floor
                AND (remaining_turns IS NULL OR remaining_turns <= 0)
            ORDER BY dist LIMIT match_count * 3)

            UNION ALL

            -- Trigger phrase embedding matches → parent memory_id
            (SELECT rm.memory_id AS id, subvector(rm.embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000) AS dist
            FROM memory_retrieval_messages rm
            INNER JOIN memories m ON rm.memory_id = m.id
            WHERE m.job_id = job_id_param AND rm.embedding IS NOT NULL AND m.importance >= importance_floor
                AND (m.remaining_turns IS NULL OR m.remaining_turns <= 0)
            ORDER BY dist LIMIT match_count * 5)
        ) all_matches
        GROUP BY id
    ) best_per_memory
    LIMIT match_count * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(sparse_keywords, websearch_to_tsquery('english', query_text)) DESC) AS rank_ix
    FROM memories WHERE job_id = job_id_param AND sparse_keywords @@ websearch_to_tsquery('english', query_text) AND importance >= importance_floor
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE job_id = job_id_param AND importance >= importance_floor
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
    ORDER BY rank_ix LIMIT match_count
)
SELECT memories.* FROM (
    SELECT COALESCE(d.id, s.id, r.id) AS mid,
        COALESCE(1.0 / (rrf_k + d.rank_ix), 0.0) * dense_weight +
        COALESCE(1.0 / (rrf_k + s.rank_ix), 0.0) * sparse_weight +
        COALESCE(1.0 / (rrf_k + r.rank_ix), 0.0) * recency_weight AS rrf_score
    FROM dense d
    FULL OUTER JOIN sparse s ON d.id = s.id
    FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id
) ranked
JOIN memories ON ranked.mid = memories.id
ORDER BY ranked.rrf_score DESC
LIMIT match_count
$$;

CREATE OR REPLACE FUNCTION memory_project_hybrid_search(
    query_text text,
    query_embedding vector(4096),
    project_id_param uuid,
    match_count int DEFAULT 10,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 50,
    importance_floor float DEFAULT 0.0
) RETURNS SETOF memories LANGUAGE sql
SET hnsw.iterative_scan = relaxed_order
AS $$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY best_dist) AS rank_ix
    FROM (
        SELECT id, MIN(dist) AS best_dist
        FROM (
            -- Content embedding matches
            (SELECT id, subvector(embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000) AS dist
            FROM memories WHERE project_id = project_id_param AND embedding IS NOT NULL AND importance >= importance_floor
                AND (remaining_turns IS NULL OR remaining_turns <= 0)
            ORDER BY dist LIMIT match_count * 3)

            UNION ALL

            -- Trigger phrase embedding matches → parent memory_id
            (SELECT rm.memory_id AS id, subvector(rm.embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000) AS dist
            FROM memory_retrieval_messages rm
            INNER JOIN memories m ON rm.memory_id = m.id
            WHERE m.project_id = project_id_param AND rm.embedding IS NOT NULL AND m.importance >= importance_floor
                AND (m.remaining_turns IS NULL OR m.remaining_turns <= 0)
            ORDER BY dist LIMIT match_count * 5)
        ) all_matches
        GROUP BY id
    ) best_per_memory
    LIMIT match_count * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(sparse_keywords, websearch_to_tsquery('english', query_text)) DESC) AS rank_ix
    FROM memories WHERE project_id = project_id_param AND sparse_keywords @@ websearch_to_tsquery('english', query_text) AND importance >= importance_floor
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE project_id = project_id_param AND importance >= importance_floor
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
    ORDER BY rank_ix LIMIT match_count
)
SELECT memories.* FROM (
    SELECT COALESCE(d.id, s.id, r.id) AS mid,
        COALESCE(1.0 / (rrf_k + d.rank_ix), 0.0) * dense_weight +
        COALESCE(1.0 / (rrf_k + s.rank_ix), 0.0) * sparse_weight +
        COALESCE(1.0 / (rrf_k + r.rank_ix), 0.0) * recency_weight AS rrf_score
    FROM dense d
    FULL OUTER JOIN sparse s ON d.id = s.id
    FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id
) ranked
                           JOIN memories ON ranked.mid = memories.id
ORDER BY ranked.rrf_score DESC LIMIT match_count
$$;

CREATE
OR REPLACE FUNCTION memory_multi_project_hybrid_search(
    query_text text,
    query_embedding vector(4096),
    project_ids_param uuid[],
    match_count int DEFAULT 10,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 50,
    importance_floor float DEFAULT 0.0
) RETURNS SETOF memories LANGUAGE sql
SET hnsw.iterative_scan = relaxed_order
AS $$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY best_dist) AS rank_ix
    FROM (
        SELECT id, MIN(dist) AS best_dist
        FROM (
            -- Content embedding matches
            (SELECT id, subvector(embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000) AS dist
            FROM memories WHERE project_id = ANY(project_ids_param) AND embedding IS NOT NULL AND importance >= importance_floor
                AND (remaining_turns IS NULL OR remaining_turns <= 0)
            ORDER BY dist LIMIT match_count * 3)

            UNION ALL

            -- Trigger phrase embedding matches → parent memory_id
            (SELECT rm.memory_id AS id, subvector(rm.embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000) AS dist
            FROM memory_retrieval_messages rm
            INNER JOIN memories m ON rm.memory_id = m.id
            WHERE m.project_id = ANY(project_ids_param) AND rm.embedding IS NOT NULL AND m.importance >= importance_floor
                AND (m.remaining_turns IS NULL OR m.remaining_turns <= 0)
            ORDER BY dist LIMIT match_count * 5)
        ) all_matches
        GROUP BY id
    ) best_per_memory
    LIMIT match_count * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(sparse_keywords, websearch_to_tsquery('english', query_text)) DESC) AS rank_ix
    FROM memories WHERE project_id = ANY(project_ids_param) AND sparse_keywords @@ websearch_to_tsquery('english', query_text) AND importance >= importance_floor
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE project_id = ANY(project_ids_param) AND importance >= importance_floor
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
    ORDER BY rank_ix LIMIT match_count
)
SELECT memories.*
FROM (SELECT COALESCE(d.id, s.id, r.id)                                AS mid,
             COALESCE(1.0 / (rrf_k + d.rank_ix), 0.0) * dense_weight +
             COALESCE(1.0 / (rrf_k + s.rank_ix), 0.0) * sparse_weight +
             COALESCE(1.0 / (rrf_k + r.rank_ix), 0.0) * recency_weight AS rrf_score
      FROM dense d
               FULL OUTER JOIN sparse s ON d.id = s.id
               FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id) ranked
JOIN memories ON ranked.mid = memories.id
ORDER BY ranked.rrf_score DESC
LIMIT match_count
$$;

CREATE OR REPLACE FUNCTION knowledge_hybrid_search(
    query_text text,
    query_embedding vector,
    project_id_param uuid,
    match_count int DEFAULT 10,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 50
) RETURNS SETOF knowledge_index LANGUAGE sql
SET hnsw.iterative_scan = relaxed_order
AS $$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY subvector(embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000)) AS rank_ix
    FROM knowledge_index
    WHERE project_id = project_id_param AND status = 'active' AND embedding IS NOT NULL
    ORDER BY rank_ix LIMIT match_count * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(search_doc, websearch_to_tsquery('english', query_text)) DESC) AS rank_ix
    FROM knowledge_index
    WHERE project_id = project_id_param AND status = 'active'
      AND search_doc @@ websearch_to_tsquery('english', query_text)
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY modified_at DESC) AS rank_ix
    FROM knowledge_index
    WHERE project_id = project_id_param AND status = 'active'
    ORDER BY rank_ix LIMIT match_count
)
SELECT ki.* FROM (
    SELECT COALESCE(d.id, s.id, r.id) AS mid,
        COALESCE(1.0 / (rrf_k + d.rank_ix), 0.0) * dense_weight +
        COALESCE(1.0 / (rrf_k + s.rank_ix), 0.0) * sparse_weight +
        COALESCE(1.0 / (rrf_k + r.rank_ix), 0.0) * recency_weight AS rrf_score
    FROM dense d
             FULL OUTER JOIN sparse s ON d.id = s.id
             FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id) ranked
                     JOIN knowledge_index ki ON ranked.mid = ki.id
ORDER BY ranked.rrf_score DESC LIMIT match_count
$$;

CREATE
OR REPLACE FUNCTION knowledge_multi_project_hybrid_search(
    query_text text,
    query_embedding vector,
    project_ids_param uuid[],
    match_count int DEFAULT 10,
    dense_weight float DEFAULT 0.6,
    sparse_weight float DEFAULT 0.3,
    recency_weight float DEFAULT 0.1,
    rrf_k int DEFAULT 50
) RETURNS SETOF knowledge_index LANGUAGE sql
SET hnsw.iterative_scan = relaxed_order
AS $$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY subvector(embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000)) AS rank_ix
    FROM knowledge_index
    WHERE project_id = ANY(project_ids_param) AND status = 'active' AND embedding IS NOT NULL
    ORDER BY rank_ix LIMIT match_count * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(search_doc, websearch_to_tsquery('english', query_text)) DESC) AS rank_ix
    FROM knowledge_index
    WHERE project_id = ANY(project_ids_param) AND status = 'active'
      AND search_doc @@ websearch_to_tsquery('english', query_text)
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY modified_at DESC) AS rank_ix
    FROM knowledge_index
    WHERE project_id = ANY(project_ids_param) AND status = 'active'
    ORDER BY rank_ix LIMIT match_count
)
SELECT ki.*
FROM (SELECT COALESCE(d.id, s.id, r.id)                           AS mid,
             COALESCE(1.0 / (rrf_k + d.rank_ix), 0.0) * dense_weight +
        COALESCE(1.0 / (rrf_k + s.rank_ix), 0.0) * sparse_weight +
        COALESCE(1.0 / (rrf_k + r.rank_ix), 0.0) * recency_weight AS rrf_score
    FROM dense d
    FULL OUTER JOIN sparse s ON d.id = s.id
    FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id
) ranked
JOIN knowledge_index ki ON ranked.mid = ki.id
ORDER BY ranked.rrf_score DESC
LIMIT match_count
$$;
