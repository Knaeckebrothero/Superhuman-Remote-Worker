-- migration:     0017_hybrid_search_plpgsql_dynamic_execute.sql
-- description:   Make the six hybrid-search functions actually USE the halfvec
--                HNSW indexes that 0002-0005 built. Fixes the unfinished half of
--                memory_bugs.md B2 — see
--                docs/issues/hnsw_indexes_never_used_inside_hybrid_search_functions.md
--
--                THE DEFECT. B2 created the indexes and re-created these functions
--                with matching subvector/halfvec casts, then recorded a
--                "planner-gated hedge: small scopes keep btree+sort, large scopes
--                flip to HNSW". The flip never happens. As LANGUAGE sql, each body
--                is planned once against parameter PLACEHOLDERS, and with a
--                placeholder on the probe side of `<=>` the HNSW ordering operator
--                cannot be matched — so the index path is not out-costed, it is
--                structurally absent. Measured on the main dev cluster at 30.7k
--                chunks / 29.1k memories: kb_search 12-14 s, memory retrieval
--                22-46 s, ~60 s/turn of injection overhead on EVERY job and session.
--
--                WHY NOT A CHEAPER FIX. All three GUC levers were tried and all
--                three failed: plan_cache_mode=force_custom_plan (no change),
--                max_parallel_workers_per_gather=0 (worse), and enable_seqscan=off
--                (no change — which is what proves the path is never considered
--                rather than merely mis-costed). Removing the `SET` clause so the
--                function can inline was also built and benchmarked: no effect.
--                There is no configuration fix.
--
--                THE FIX. Convert each function to plpgsql and issue the same body
--                through a dynamic EXECUTE. plpgsql's EXECUTE builds a ONE-SHOT
--                plan, so the planner sees real parameter values, folds them as
--                constants, matches the ordering operator, and takes the index.
--                Costs one extra plan per call (~20 ms) against seconds saved.
--
--                EVIDENCE (k3d harness at production parity, 32.9k chunks, same
--                index, pgvector 0.8.2; three runs each):
--                  dense arm as top-level PREPARE ... 21 ms      Index Scan
--                  deployed LANGUAGE sql shape ...... 5.0-5.9 s  Parallel Seq Scan
--                  this migration ................... 0.48-0.88 s Index Scan
--                  SET clause removed (rejected) .... 4.8-5.9 s  Parallel Seq Scan
--
--                SHAPE OF THE CHANGE. Signatures, argument names, defaults, return
--                types and SET clauses are unchanged, so every caller
--                (KnowledgeStore.search_chunks, the RecallStore paths) is
--                untouched. The bodies are the deployed text verbatim, with named
--                parameters mechanically rewritten to positional $n on word
--                boundaries and passed back via USING in declaration order. No
--                logic, weighting, filter or ordering was edited. Note that $n also
--                appears in the bodies' own comments as a result of that rewrite.
--
--                REVERTING. Re-run 0002 (note-level + memory functions) and 0009
--                (knowledge_chunk_hybrid_search) to restore the LANGUAGE sql
--                bodies; nothing else in this migration needs undoing.

CREATE OR REPLACE FUNCTION public.knowledge_chunk_hybrid_search(query_text text, query_embedding vector, kb_ids_param uuid[], version_param text DEFAULT NULL::text, match_count integer DEFAULT 15, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 60)
 RETURNS SETOF knowledge_index
 LANGUAGE plpgsql
 SET "hnsw.iterative_scan" TO 'relaxed_order'
AS $function$
BEGIN
RETURN QUERY EXECUTE $q$
-- Dense arm: top chunks by vector distance (HNSW), collapsed to the best-ranked
-- chunk per note. Over-fetch $5 * 4 chunks so a note whose best chunk
-- ranks behind several other notes' chunks still survives the collapse.
WITH dense AS (
    SELECT mid, MIN(rank_ix) AS rank_ix FROM (
        SELECT c.note_row AS mid,
               ROW_NUMBER() OVER (
                   ORDER BY subvector(c.embedding, 1, 4000)::halfvec(4000)
                            <=> subvector($2, 1, 4000)::halfvec(4000)
               ) AS rank_ix
        FROM knowledge_chunks c
        JOIN knowledge_index ki ON ki.id = c.note_row
        WHERE c.kb_id = ANY($3)
          AND ki.status = 'active'
          AND c.embedding IS NOT NULL
          AND ($4 IS NULL OR c.embedding_version = $4)
        ORDER BY subvector(c.embedding, 1, 4000)::halfvec(4000)
                 <=> subvector($2, 1, 4000)::halfvec(4000)
        LIMIT $5 * 4
    ) ranked_chunks
    GROUP BY mid
),
-- Sparse arm: chunk-granular tsvector match, same best-chunk-per-note collapse.
sparse AS (
    SELECT mid, MIN(rank_ix) AS rank_ix FROM (
        SELECT c.note_row AS mid,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank_cd(c.search_doc, websearch_to_tsquery('english', $1)) DESC
               ) AS rank_ix
        FROM knowledge_chunks c
        JOIN knowledge_index ki ON ki.id = c.note_row
        WHERE c.kb_id = ANY($3)
          AND ki.status = 'active'
          AND ($4 IS NULL OR c.embedding_version = $4)
          AND c.search_doc @@ websearch_to_tsquery('english', $1)
        ORDER BY ts_rank_cd(c.search_doc, websearch_to_tsquery('english', $1)) DESC
        LIMIT $5 * 4
    ) ranked_chunks
    GROUP BY mid
),
-- Recency arm: note-level freshness, restricted to notes that actually carry
-- chunks of the current pipeline version (keeps ghosts out — see header).
recent AS (
    SELECT ki.id AS mid, ROW_NUMBER() OVER (ORDER BY ki.modified_at DESC) AS rank_ix
    FROM knowledge_index ki
    WHERE ki.kb_id = ANY($3) AND ki.status = 'active'
      AND EXISTS (
          SELECT 1 FROM knowledge_chunks c
          WHERE c.note_row = ki.id
            AND ($4 IS NULL OR c.embedding_version = $4)
      )
    ORDER BY rank_ix LIMIT $5
)
SELECT ki.* FROM (
    SELECT COALESCE(d.mid, s.mid, r.mid) AS mid,
        COALESCE(1.0 / ($9 + d.rank_ix), 0.0) * $6 +
        COALESCE(1.0 / ($9 + s.rank_ix), 0.0) * $7 +
        COALESCE(1.0 / ($9 + r.rank_ix), 0.0) * $8 AS rrf_score
    FROM dense d
             FULL OUTER JOIN sparse s ON d.mid = s.mid
             FULL OUTER JOIN recent r ON COALESCE(d.mid, s.mid) = r.mid) ranked
                     JOIN knowledge_index ki ON ranked.mid = ki.id
ORDER BY ranked.rrf_score DESC LIMIT $5
$q$ USING query_text, query_embedding, kb_ids_param, version_param, match_count, dense_weight, sparse_weight, recency_weight, rrf_k;
END;
$function$;


CREATE OR REPLACE FUNCTION public.knowledge_hybrid_search(query_text text, query_embedding vector, project_id_param uuid, match_count integer DEFAULT 10, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 50)
 RETURNS SETOF knowledge_index
 LANGUAGE plpgsql
 SET "hnsw.iterative_scan" TO 'relaxed_order'
AS $function$
BEGIN
RETURN QUERY EXECUTE $q$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY subvector(embedding, 1, 4000)::halfvec(4000) <=> subvector($2, 1, 4000)::halfvec(4000)) AS rank_ix
    FROM knowledge_index
    WHERE project_id = $3 AND status = 'active' AND embedding IS NOT NULL
    ORDER BY rank_ix LIMIT $4 * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(search_doc, websearch_to_tsquery('english', $1)) DESC) AS rank_ix
    FROM knowledge_index
    WHERE project_id = $3 AND status = 'active'
      AND search_doc @@ websearch_to_tsquery('english', $1)
    ORDER BY rank_ix LIMIT $4 * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY modified_at DESC) AS rank_ix
    FROM knowledge_index
    WHERE project_id = $3 AND status = 'active'
    ORDER BY rank_ix LIMIT $4
)
SELECT ki.* FROM (
    SELECT COALESCE(d.id, s.id, r.id) AS mid,
        COALESCE(1.0 / ($8 + d.rank_ix), 0.0) * $5 +
        COALESCE(1.0 / ($8 + s.rank_ix), 0.0) * $6 +
        COALESCE(1.0 / ($8 + r.rank_ix), 0.0) * $7 AS rrf_score
    FROM dense d
             FULL OUTER JOIN sparse s ON d.id = s.id
             FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id) ranked
                     JOIN knowledge_index ki ON ranked.mid = ki.id
ORDER BY ranked.rrf_score DESC LIMIT $4
$q$ USING query_text, query_embedding, project_id_param, match_count, dense_weight, sparse_weight, recency_weight, rrf_k;
END;
$function$;


CREATE OR REPLACE FUNCTION public.knowledge_multi_project_hybrid_search(query_text text, query_embedding vector, project_ids_param uuid[], match_count integer DEFAULT 10, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 50)
 RETURNS SETOF knowledge_index
 LANGUAGE plpgsql
 SET "hnsw.iterative_scan" TO 'relaxed_order'
AS $function$
BEGIN
RETURN QUERY EXECUTE $q$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY subvector(embedding, 1, 4000)::halfvec(4000) <=> subvector($2, 1, 4000)::halfvec(4000)) AS rank_ix
    FROM knowledge_index
    WHERE project_id = ANY($3) AND status = 'active' AND embedding IS NOT NULL
    ORDER BY rank_ix LIMIT $4 * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(search_doc, websearch_to_tsquery('english', $1)) DESC) AS rank_ix
    FROM knowledge_index
    WHERE project_id = ANY($3) AND status = 'active'
      AND search_doc @@ websearch_to_tsquery('english', $1)
    ORDER BY rank_ix LIMIT $4 * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY modified_at DESC) AS rank_ix
    FROM knowledge_index
    WHERE project_id = ANY($3) AND status = 'active'
    ORDER BY rank_ix LIMIT $4
)
SELECT ki.*
FROM (SELECT COALESCE(d.id, s.id, r.id)                           AS mid,
             COALESCE(1.0 / ($8 + d.rank_ix), 0.0) * $5 +
        COALESCE(1.0 / ($8 + s.rank_ix), 0.0) * $6 +
        COALESCE(1.0 / ($8 + r.rank_ix), 0.0) * $7 AS rrf_score
    FROM dense d
    FULL OUTER JOIN sparse s ON d.id = s.id
    FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id
) ranked
JOIN knowledge_index ki ON ranked.mid = ki.id
ORDER BY ranked.rrf_score DESC
LIMIT $4
$q$ USING query_text, query_embedding, project_ids_param, match_count, dense_weight, sparse_weight, recency_weight, rrf_k;
END;
$function$;


CREATE OR REPLACE FUNCTION public.memory_hybrid_search(query_text text, query_embedding vector, job_id_param uuid, match_count integer DEFAULT 10, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 50, importance_floor double precision DEFAULT 0.0)
 RETURNS SETOF memories
 LANGUAGE plpgsql
 SET "hnsw.iterative_scan" TO 'relaxed_order'
AS $function$
BEGIN
RETURN QUERY EXECUTE $q$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY best_dist) AS rank_ix
    FROM (
        SELECT id, MIN(dist) AS best_dist
        FROM (
            -- Content embedding matches
            (SELECT id, subvector(embedding, 1, 4000)::halfvec(4000) <=> subvector($2, 1, 4000)::halfvec(4000) AS dist
            FROM memories WHERE job_id = $3 AND embedding IS NOT NULL AND importance >= $9
                AND (remaining_turns IS NULL OR remaining_turns <= 0)
                AND valid_to IS NULL
            ORDER BY dist LIMIT $4 * 3)

            UNION ALL

            -- Trigger phrase embedding matches → parent memory_id
            (SELECT rm.memory_id AS id, subvector(rm.embedding, 1, 4000)::halfvec(4000) <=> subvector($2, 1, 4000)::halfvec(4000) AS dist
            FROM memory_retrieval_messages rm
            INNER JOIN memories m ON rm.memory_id = m.id
            WHERE m.job_id = $3 AND rm.embedding IS NOT NULL AND m.importance >= $9
                AND (m.remaining_turns IS NULL OR m.remaining_turns <= 0)
                AND m.valid_to IS NULL
            ORDER BY dist LIMIT $4 * 5)
        ) all_matches
        GROUP BY id
    ) best_per_memory
    LIMIT $4 * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(sparse_keywords, websearch_to_tsquery('english', $1)) DESC) AS rank_ix
    FROM memories WHERE job_id = $3 AND sparse_keywords @@ websearch_to_tsquery('english', $1) AND importance >= $9
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
        AND valid_to IS NULL
    ORDER BY rank_ix LIMIT $4 * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE job_id = $3 AND importance >= $9
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
        AND valid_to IS NULL
    ORDER BY rank_ix LIMIT $4
)
SELECT memories.* FROM (
    SELECT COALESCE(d.id, s.id, r.id) AS mid,
        COALESCE(1.0 / ($8 + d.rank_ix), 0.0) * $5 +
        COALESCE(1.0 / ($8 + s.rank_ix), 0.0) * $6 +
        COALESCE(1.0 / ($8 + r.rank_ix), 0.0) * $7 AS rrf_score
    FROM dense d
    FULL OUTER JOIN sparse s ON d.id = s.id
    FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id
) ranked
JOIN memories ON ranked.mid = memories.id
ORDER BY ranked.rrf_score DESC
LIMIT $4
$q$ USING query_text, query_embedding, job_id_param, match_count, dense_weight, sparse_weight, recency_weight, rrf_k, importance_floor;
END;
$function$;


CREATE OR REPLACE FUNCTION public.memory_project_hybrid_search(query_text text, query_embedding vector, project_id_param uuid, match_count integer DEFAULT 10, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 50, importance_floor double precision DEFAULT 0.0)
 RETURNS SETOF memories
 LANGUAGE plpgsql
 SET "hnsw.iterative_scan" TO 'relaxed_order'
AS $function$
BEGIN
RETURN QUERY EXECUTE $q$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY best_dist) AS rank_ix
    FROM (
        SELECT id, MIN(dist) AS best_dist
        FROM (
            -- Content embedding matches
            (SELECT id, subvector(embedding, 1, 4000)::halfvec(4000) <=> subvector($2, 1, 4000)::halfvec(4000) AS dist
            FROM memories WHERE project_id = $3 AND embedding IS NOT NULL AND importance >= $9
                AND (remaining_turns IS NULL OR remaining_turns <= 0)
                AND valid_to IS NULL
            ORDER BY dist LIMIT $4 * 3)

            UNION ALL

            -- Trigger phrase embedding matches → parent memory_id
            (SELECT rm.memory_id AS id, subvector(rm.embedding, 1, 4000)::halfvec(4000) <=> subvector($2, 1, 4000)::halfvec(4000) AS dist
            FROM memory_retrieval_messages rm
            INNER JOIN memories m ON rm.memory_id = m.id
            WHERE m.project_id = $3 AND rm.embedding IS NOT NULL AND m.importance >= $9
                AND (m.remaining_turns IS NULL OR m.remaining_turns <= 0)
                AND m.valid_to IS NULL
            ORDER BY dist LIMIT $4 * 5)
        ) all_matches
        GROUP BY id
    ) best_per_memory
    LIMIT $4 * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(sparse_keywords, websearch_to_tsquery('english', $1)) DESC) AS rank_ix
    FROM memories WHERE project_id = $3 AND sparse_keywords @@ websearch_to_tsquery('english', $1) AND importance >= $9
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
        AND valid_to IS NULL
    ORDER BY rank_ix LIMIT $4 * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE project_id = $3 AND importance >= $9
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
        AND valid_to IS NULL
    ORDER BY rank_ix LIMIT $4
)
SELECT memories.* FROM (
    SELECT COALESCE(d.id, s.id, r.id) AS mid,
        COALESCE(1.0 / ($8 + d.rank_ix), 0.0) * $5 +
        COALESCE(1.0 / ($8 + s.rank_ix), 0.0) * $6 +
        COALESCE(1.0 / ($8 + r.rank_ix), 0.0) * $7 AS rrf_score
    FROM dense d
    FULL OUTER JOIN sparse s ON d.id = s.id
    FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id
) ranked
                           JOIN memories ON ranked.mid = memories.id
ORDER BY ranked.rrf_score DESC LIMIT $4
$q$ USING query_text, query_embedding, project_id_param, match_count, dense_weight, sparse_weight, recency_weight, rrf_k, importance_floor;
END;
$function$;


CREATE OR REPLACE FUNCTION public.memory_multi_project_hybrid_search(query_text text, query_embedding vector, project_ids_param uuid[], match_count integer DEFAULT 10, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 50, importance_floor double precision DEFAULT 0.0)
 RETURNS SETOF memories
 LANGUAGE plpgsql
 SET "hnsw.iterative_scan" TO 'relaxed_order'
AS $function$
BEGIN
RETURN QUERY EXECUTE $q$
WITH dense AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY best_dist) AS rank_ix
    FROM (
        SELECT id, MIN(dist) AS best_dist
        FROM (
            -- Content embedding matches
            (SELECT id, subvector(embedding, 1, 4000)::halfvec(4000) <=> subvector($2, 1, 4000)::halfvec(4000) AS dist
            FROM memories WHERE project_id = ANY($3) AND embedding IS NOT NULL AND importance >= $9
                AND (remaining_turns IS NULL OR remaining_turns <= 0)
                AND valid_to IS NULL
            ORDER BY dist LIMIT $4 * 3)

            UNION ALL

            -- Trigger phrase embedding matches → parent memory_id
            (SELECT rm.memory_id AS id, subvector(rm.embedding, 1, 4000)::halfvec(4000) <=> subvector($2, 1, 4000)::halfvec(4000) AS dist
            FROM memory_retrieval_messages rm
            INNER JOIN memories m ON rm.memory_id = m.id
            WHERE m.project_id = ANY($3) AND rm.embedding IS NOT NULL AND m.importance >= $9
                AND (m.remaining_turns IS NULL OR m.remaining_turns <= 0)
                AND m.valid_to IS NULL
            ORDER BY dist LIMIT $4 * 5)
        ) all_matches
        GROUP BY id
    ) best_per_memory
    LIMIT $4 * 2
),
sparse AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(sparse_keywords, websearch_to_tsquery('english', $1)) DESC) AS rank_ix
    FROM memories WHERE project_id = ANY($3) AND sparse_keywords @@ websearch_to_tsquery('english', $1) AND importance >= $9
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
        AND valid_to IS NULL
    ORDER BY rank_ix LIMIT $4 * 2
),
recent AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rank_ix
    FROM memories WHERE project_id = ANY($3) AND importance >= $9
        AND (remaining_turns IS NULL OR remaining_turns <= 0)
        AND valid_to IS NULL
    ORDER BY rank_ix LIMIT $4
)
SELECT memories.*
FROM (SELECT COALESCE(d.id, s.id, r.id)                                AS mid,
             COALESCE(1.0 / ($8 + d.rank_ix), 0.0) * $5 +
             COALESCE(1.0 / ($8 + s.rank_ix), 0.0) * $6 +
             COALESCE(1.0 / ($8 + r.rank_ix), 0.0) * $7 AS rrf_score
      FROM dense d
               FULL OUTER JOIN sparse s ON d.id = s.id
               FULL OUTER JOIN recent r ON COALESCE(d.id, s.id) = r.id) ranked
JOIN memories ON ranked.mid = memories.id
ORDER BY ranked.rrf_score DESC
LIMIT $4
$q$ USING query_text, query_embedding, project_ids_param, match_count, dense_weight, sparse_weight, recency_weight, rrf_k, importance_floor;
END;
$function$;
