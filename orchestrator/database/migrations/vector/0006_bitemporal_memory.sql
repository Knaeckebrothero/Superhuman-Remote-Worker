-- migration:     0006_bitemporal_memory.sql
-- description:   Agent memory overhaul Phase 4 (docs/features/agent_memory_overhaul.md
--                §5 Phase 4) — bi-temporal supersede on the `memories` table.
--                Adds the columns the supersede lifecycle reads/writes and
--                teaches default retrieval to serve only currently-valid rows.
--
--                Columns (locked decision #6 — valid + transaction time, in
--                pgvector columns, no graph required):
--                  - valid_from   TIMESTAMPTZ  valid-time start (defaults to
--                                 creation; backfilled = created_at for existing
--                                 rows so point-in-time validity queries are honest)
--                  - valid_to     TIMESTAMPTZ  valid-time end. NULL = currently
--                                 valid = served. This single predicate is the
--                                 default-retrieval filter.
--                  - superseded_at TIMESTAMPTZ transaction-time of the retirement
--                                 (when WE recorded it — may differ from valid_to,
--                                 which is when the fact stopped being true in the
--                                 world; that gap is the bi-temporal point).
--                  - superseded_by UUID self-FK to the replacement row (ON DELETE
--                                 SET NULL — same table, same DB, real FK is safe;
--                                 the cross-DB no-FK convention is only for
--                                 job_id/project_id pointing at the app DB).
--
--                Behaviour-preserving by construction: every existing and every
--                new row has valid_to = NULL, so `AND valid_to IS NULL` is true
--                for the whole store today and changes nothing returned. Rows are
--                only retired by the ingestion-verdict writer, which ships behind
--                `memory.ingestion.enabled` (default off) in a later slice. The
--                retrieval filter itself is NOT flag-gated — a retired memory must
--                never be served regardless of config.
--
--                Scope of the function rewrites — the THREE memory hybrid-search
--                functions only, bodies otherwise byte-identical to 0002 (halfvec
--                subvector casts + SET hnsw.iterative_scan preserved); each CTE
--                gains `AND valid_to IS NULL` (the trigger-phrase join uses
--                `m.valid_to IS NULL`). knowledge_hybrid_search /
--                knowledge_multi_project_hybrid_search are deliberately untouched:
--                knowledge_index already supersedes via its `status` column
--                (`status = 'active'` filter), and the KB is the model's active
--                notebook (P0) — its supersede is a model action (kb_update), not
--                a passive verdict.
-- depends-on:    0002_hybrid_search_halfvec_casts.sql
-- expected:      < 1s on dev/eval — ALTER ADD COLUMN (fast default), one UPDATE
--                backfill of valid_from, two partial btrees, three CREATE OR
--                REPLACE FUNCTION. No vector data touched, no table rewrite.
-- locks:         brief ACCESS EXCLUSIVE on `memories` for the ALTERs; pg_proc row
--                locks for the functions.
-- transactional: YES.

-- ---------------------------------------------------------------------------
-- 1. Columns
-- ---------------------------------------------------------------------------
ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS superseded_by UUID;

-- Self-FK for the supersede link. Guarded so re-runs / pre-existing constraint
-- don't error (the runner is checksum-gated, but keep it idempotent in spirit).
DO $$ BEGIN
    ALTER TABLE memories
        ADD CONSTRAINT fk_memories_superseded_by
        FOREIGN KEY (superseded_by) REFERENCES memories(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

-- Backfill valid_from = created_at for rows that predate the column so
-- point-in-time validity reflects original creation, not migration time.
UPDATE memories SET valid_from = created_at WHERE valid_from IS NULL AND created_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Indexes — partial on the currently-valid filter, mirroring the existing
--    idx_memories_ttl_active partial-index precedent.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_memories_job_valid ON memories(job_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_memories_project_valid ON memories(project_id) WHERE valid_to IS NULL;

-- ---------------------------------------------------------------------------
-- 3. Function rewrites — add `valid_to IS NULL` to every channel of the three
--    memory hybrid-search functions. Bodies are byte-identical to 0002 except
--    for the added predicate.
-- ---------------------------------------------------------------------------
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
                AND valid_to IS NULL
            ORDER BY dist LIMIT match_count * 3)

            UNION ALL

            -- Trigger phrase embedding matches → parent memory_id
            (SELECT rm.memory_id AS id, subvector(rm.embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000) AS dist
            FROM memory_retrieval_messages rm
            INNER JOIN memories m ON rm.memory_id = m.id
            WHERE m.job_id = job_id_param AND rm.embedding IS NOT NULL AND m.importance >= importance_floor
                AND (m.remaining_turns IS NULL OR m.remaining_turns <= 0)
                AND m.valid_to IS NULL
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
        AND valid_to IS NULL
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE job_id = job_id_param AND importance >= importance_floor
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
        AND valid_to IS NULL
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
                AND valid_to IS NULL
            ORDER BY dist LIMIT match_count * 3)

            UNION ALL

            -- Trigger phrase embedding matches → parent memory_id
            (SELECT rm.memory_id AS id, subvector(rm.embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000) AS dist
            FROM memory_retrieval_messages rm
            INNER JOIN memories m ON rm.memory_id = m.id
            WHERE m.project_id = project_id_param AND rm.embedding IS NOT NULL AND m.importance >= importance_floor
                AND (m.remaining_turns IS NULL OR m.remaining_turns <= 0)
                AND m.valid_to IS NULL
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
        AND valid_to IS NULL
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE project_id = project_id_param AND importance >= importance_floor
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
        AND valid_to IS NULL
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
                AND valid_to IS NULL
            ORDER BY dist LIMIT match_count * 3)

            UNION ALL

            -- Trigger phrase embedding matches → parent memory_id
            (SELECT rm.memory_id AS id, subvector(rm.embedding, 1, 4000)::halfvec(4000) <=> subvector(query_embedding, 1, 4000)::halfvec(4000) AS dist
            FROM memory_retrieval_messages rm
            INNER JOIN memories m ON rm.memory_id = m.id
            WHERE m.project_id = ANY(project_ids_param) AND rm.embedding IS NOT NULL AND m.importance >= importance_floor
                AND (m.remaining_turns IS NULL OR m.remaining_turns <= 0)
                AND m.valid_to IS NULL
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
        AND valid_to IS NULL
    ORDER BY rank_ix LIMIT match_count * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE project_id = ANY(project_ids_param) AND importance >= importance_floor
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
        AND valid_to IS NULL
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
