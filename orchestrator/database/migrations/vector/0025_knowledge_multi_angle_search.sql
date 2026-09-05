-- migration:     0025_knowledge_multi_angle_search.sql
-- description:   Multi-angle KB search (D9/D11): the three existing arms plus
--                an `exact` trigram arm over knowledge_index.content/title and a
--                `tag` boost arm, RRF-fused, with per-arm attribution. Called by
--                KnowledgeStore.search_chunks ONLY when exact/tags are supplied;
--                knowledge_chunk_hybrid_search is untouched so every existing
--                caller's ranking is byte-identical
--                (kb_retrieval_hardening_and_slice_d_additive.md H6).
--
--                SHAPE. plpgsql + dynamic `EXECUTE ... USING`, exactly as
--                0017_hybrid_search_plpgsql_dynamic_execute.sql: as LANGUAGE sql
--                the body is planned once against parameter PLACEHOLDERS and the
--                HNSW ordering operator can never be matched, so the dense arm
--                seq-scans. Read 0017's header before touching this. The
--                `SET "hnsw.iterative_scan"` clause is part of that fix and must
--                stay. The dense/sparse arms below are COPIES of 0017's
--                knowledge_chunk_hybrid_search arms (copied, not shared — H6
--                forbids editing the old function), re-numbered for this
--                function's 13 parameters.
--
--                ARM GATING. dense is empty when $2 IS NULL, sparse when
--                $1 = '', exact when $9 is empty, tag when $11 is empty — the
--                guards sit inside each CTE's WHERE so Postgres folds them to a
--                one-time filter. recency is gated on `$1 <> '' OR $2 IS NOT
--                NULL`, i.e. it fires only when there is a semantic query for it
--                to freshen. That gate is NOT in the old function and is
--                deliberate: an ungated recency arm returns every active note in
--                the knowledge base, so a pure `exact=`/`tags=` lookup (no query
--                text, no embedding) would come back with the entire corpus and
--                the filter arms would degrade to a re-ranking of everything.
--                With the gate, `exact=[...]` alone returns exactly the notes
--                that matched, attributed `{exact}`.
--
--                NULLS LAST. The recency and tag arms order by
--                `modified_at DESC NULLS LAST`; the old function's plain
--                `DESC` floats never-modified notes to rank 1. Fixed HERE ONLY —
--                changing the old function is precisely what H6 forbids.
--
--                EXACT ARM. Note-level, one row per note containing ANY term as
--                a case-insensitive substring (so it takes
--                idx_knowledge_content_trgm from 0024), ranked by the best
--                trigram similarity across terms and across content/title. The
--                ILIKE pattern is built by plain concatenation and is NOT
--                escaped: `exact` terms are identifiers, and `%`/`_` inside one
--                act as wildcards. If the tool layer ever needs literal-only
--                semantics it escapes before calling (KnowledgeStore.grep_notes
--                already does this for its own patterns).
--
--                ATTRIBUTION. `arms` is ordered dense, sparse, recency, exact,
--                tag, filtered to the arms that actually matched.
-- depends-on:    0024_knowledge_content_trgm_index.notx.sql
-- expected:      Instant. CREATE FUNCTION only; no table is read or rewritten.
-- locks:         brief ACCESS EXCLUSIVE on the pg_proc catalog row; no user
--                table is touched.
-- transactional: YES.

-- LOAD PGVECTOR FIRST. `SET "hnsw.iterative_scan"` below is a function-level SET
-- clause, and PostgreSQL validates those through validate_option_array_item():
-- when the parameter is UNRECOGNISED (a placeholder custom GUC) that check
-- requires SUPERUSER and fails with `permission denied to set parameter`.
-- pgvector defines hnsw.* in its _PG_init(), which only runs once the library is
-- loaded into the session -- so in a fresh migration session the GUC is still a
-- placeholder and CREATE FUNCTION is denied for the non-superuser app role.
-- `LOAD 'vector'` cannot be used (non-superusers are refused access to the
-- library by name); touching any pgvector-typed value loads it just as well.
-- 0017 and its predecessors escaped this only because they were applied while
-- the app role was still superuser, before the CloudNativePG migration --
-- already-applied migrations are grandfathered, NEW ones are not. Any future
-- migration that creates a function with an hnsw.* SET clause needs this too.
DO $load_pgvector$ BEGIN PERFORM '[1]'::vector; END $load_pgvector$;

CREATE OR REPLACE FUNCTION public.knowledge_chunk_multi_angle_search(
    query_text text, query_embedding vector, kb_ids_param uuid[],
    version_param text DEFAULT NULL::text, match_count integer DEFAULT 15,
    dense_weight double precision DEFAULT 0.6,
    sparse_weight double precision DEFAULT 0.3,
    recency_weight double precision DEFAULT 0.1,
    exact_terms text[] DEFAULT '{}'::text[],
    exact_weight double precision DEFAULT 0.6,
    tag_terms text[] DEFAULT '{}'::text[],
    tag_weight double precision DEFAULT 0.2,
    rrf_k integer DEFAULT 60)
 RETURNS TABLE (note_row uuid, rrf_score double precision, arms text[])
 LANGUAGE plpgsql
 SET "hnsw.iterative_scan" TO 'relaxed_order'
AS $function$
BEGIN
RETURN QUERY EXECUTE $q$
-- Dense arm: copy of knowledge_chunk_hybrid_search's dense arm, empty when
-- $2 IS NULL. Over-fetch $5 * 4 chunks so a note whose best chunk ranks behind
-- several other notes' chunks still survives the per-note collapse.
WITH dense AS (
    SELECT mid, MIN(rank_ix) AS rank_ix FROM (
        SELECT c.note_row AS mid,
               ROW_NUMBER() OVER (
                   ORDER BY subvector(c.embedding, 1, 4000)::halfvec(4000)
                            <=> subvector($2, 1, 4000)::halfvec(4000)
               ) AS rank_ix
        FROM knowledge_chunks c
        JOIN knowledge_index ki ON ki.id = c.note_row
        WHERE $2 IS NOT NULL
          AND c.kb_id = ANY($3)
          AND ki.status = 'active'
          AND c.embedding IS NOT NULL
          AND ($4 IS NULL OR c.embedding_version = $4)
        ORDER BY subvector(c.embedding, 1, 4000)::halfvec(4000)
                 <=> subvector($2, 1, 4000)::halfvec(4000)
        LIMIT $5 * 4
    ) ranked_chunks
    GROUP BY mid
),
-- Sparse arm: copy of the existing chunk-granular tsvector arm, empty when
-- $1 = ''.
sparse AS (
    SELECT mid, MIN(rank_ix) AS rank_ix FROM (
        SELECT c.note_row AS mid,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank_cd(c.search_doc, websearch_to_tsquery('english', $1)) DESC
               ) AS rank_ix
        FROM knowledge_chunks c
        JOIN knowledge_index ki ON ki.id = c.note_row
        WHERE $1 <> ''
          AND c.kb_id = ANY($3)
          AND ki.status = 'active'
          AND ($4 IS NULL OR c.embedding_version = $4)
          AND c.search_doc @@ websearch_to_tsquery('english', $1)
        ORDER BY ts_rank_cd(c.search_doc, websearch_to_tsquery('english', $1)) DESC
        LIMIT $5 * 4
    ) ranked_chunks
    GROUP BY mid
),
-- Recency arm: note-level freshness, restricted to notes that actually carry
-- chunks of the current pipeline version (keeps ghosts out). Two deliberate
-- differences from the old function, both explained in the header: NULLS LAST,
-- and the "there is a semantic query to freshen" gate.
recent AS (
    SELECT ki.id AS mid,
           ROW_NUMBER() OVER (ORDER BY ki.modified_at DESC NULLS LAST) AS rank_ix
    FROM knowledge_index ki
    WHERE ($1 <> '' OR $2 IS NOT NULL)
      AND ki.kb_id = ANY($3) AND ki.status = 'active'
      AND EXISTS (
          SELECT 1 FROM knowledge_chunks c
          WHERE c.note_row = ki.id
            AND ($4 IS NULL OR c.embedding_version = $4)
      )
    ORDER BY rank_ix LIMIT $5
),
-- Exact arm: one row per note containing ANY term (case-insensitive substring),
-- ranked by the best trigram similarity across terms. Uses
-- idx_knowledge_content_trgm (0024).
exact AS (
    SELECT x.id AS mid, ROW_NUMBER() OVER (ORDER BY x.best DESC) AS rank_ix
    FROM (
        SELECT ki.id,
               MAX(GREATEST(similarity(ki.content, t.term),
                            similarity(ki.title, t.term))) AS best
        FROM knowledge_index ki, unnest($9) AS t(term)
        WHERE cardinality($9) > 0
          AND ki.kb_id = ANY($3) AND ki.status = 'active'
          AND ki.path IS NOT NULL
          AND (ki.content ILIKE '%' || t.term || '%'
               OR ki.title ILIKE '%' || t.term || '%')
        GROUP BY ki.id
    ) x
    ORDER BY rank_ix LIMIT $5 * 4
),
-- Tag arm: a BOOST, not a filter — notes carrying any requested tag, by recency.
tagged AS (
    SELECT ki.id AS mid,
           ROW_NUMBER() OVER (ORDER BY ki.modified_at DESC NULLS LAST) AS rank_ix
    FROM knowledge_index ki
    WHERE cardinality($11) > 0
      AND ki.kb_id = ANY($3) AND ki.status = 'active'
      AND ki.path IS NOT NULL AND ki.tags && $11
    ORDER BY rank_ix LIMIT $5 * 4
),
fused AS (
    SELECT COALESCE(d.mid, s.mid, r.mid, e.mid, g.mid) AS mid,
           COALESCE(1.0 / ($13 + d.rank_ix), 0.0) * $6 +
           COALESCE(1.0 / ($13 + s.rank_ix), 0.0) * $7 +
           COALESCE(1.0 / ($13 + r.rank_ix), 0.0) * $8 +
           COALESCE(1.0 / ($13 + e.rank_ix), 0.0) * $10 +
           COALESCE(1.0 / ($13 + g.rank_ix), 0.0) * $12 AS score,
           ARRAY_REMOVE(ARRAY[
               CASE WHEN d.mid IS NOT NULL THEN 'dense'::text END,
               CASE WHEN s.mid IS NOT NULL THEN 'sparse'::text END,
               CASE WHEN r.mid IS NOT NULL THEN 'recency'::text END,
               CASE WHEN e.mid IS NOT NULL THEN 'exact'::text END,
               CASE WHEN g.mid IS NOT NULL THEN 'tag'::text END], NULL) AS arms
    FROM dense d
             FULL OUTER JOIN sparse s ON d.mid = s.mid
             FULL OUTER JOIN recent r ON COALESCE(d.mid, s.mid) = r.mid
             FULL OUTER JOIN exact  e ON COALESCE(d.mid, s.mid, r.mid) = e.mid
             FULL OUTER JOIN tagged g ON COALESCE(d.mid, s.mid, r.mid, e.mid) = g.mid
)
SELECT mid, score, arms FROM fused ORDER BY score DESC LIMIT $5
$q$ USING query_text, query_embedding, kb_ids_param, version_param, match_count,
          dense_weight, sparse_weight, recency_weight, exact_terms, exact_weight,
          tag_terms, tag_weight, rrf_k;
END;
$function$;
