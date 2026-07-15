-- =============================================================================
-- GENERATED FILE — DO NOT EDIT BY HAND.
--
-- Canonical current schema for the 'vector' database, produced by replaying every
-- migration under orchestrator/database/migrations/vector/ from zero into a
-- throwaway container and dumping the result.
--
-- Source of truth : orchestrator/database/migrations/vector/*.sql
-- Regenerate      : scripts/schema-snapshot.sh vector
-- CI enforces that this file matches a fresh regeneration (db-migrations.yml).
-- The frozen orchestrator/database/{schema,vector_schema}.sql snapshots are a
-- separate, historical concern; THIS file tracks the live migration chain.
--
-- (No runtime-only objects for this family.)
-- ==============================================================================

--
-- PostgreSQL database dump
--



SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;


--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: confidence_level; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.confidence_level AS ENUM (
    'high',
    'medium',
    'low'
);


--
-- Name: extraction_method; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.extraction_method AS ENUM (
    'direct_quote',
    'paraphrase',
    'inference',
    'aggregation',
    'negative'
);


--
-- Name: source_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.source_type AS ENUM (
    'document',
    'website',
    'database',
    'custom'
);


--
-- Name: verification_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.verification_status AS ENUM (
    'pending',
    'verified',
    'failed',
    'unverified'
);


SET default_table_access_method = heap;

--
-- Name: knowledge_index; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_index (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    note_id character varying(100) NOT NULL,
    project_id uuid NOT NULL,
    title text NOT NULL,
    note_type character varying(50) NOT NULL,
    status character varying(50) DEFAULT 'active'::character varying,
    confidence character varying(20),
    tags text[] DEFAULT '{}'::text[],
    keywords text[] DEFAULT '{}'::text[],
    job_id uuid,
    phase integer,
    content text NOT NULL,
    retrieval_messages text[] DEFAULT '{}'::text[],
    embedding public.vector(4096),
    search_doc tsvector,
    created_at timestamp with time zone,
    modified_at timestamp with time zone,
    indexed_at timestamp with time zone DEFAULT now(),
    content_hash character varying(64),
    remaining_cycles integer,
    last_verified_cycle integer,
    kb_id uuid,
    path text,
    blob_sha character varying(64),
    superseded_by character varying(100),
    invalidated_at timestamp with time zone,
    embedding_version text,
    CONSTRAINT valid_note_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'resolved'::character varying, 'superseded'::character varying, 'archived'::character varying])::text[]))),
    CONSTRAINT valid_note_type CHECK (((note_type)::text = ANY ((ARRAY['goal'::character varying, 'plan'::character varying, 'decision'::character varying, 'learning'::character varying, 'code'::character varying, 'source'::character varying, 'question'::character varying, 'state'::character varying, 'retrospective'::character varying, 'datasource'::character varying])::text[])))
);


--
-- Name: knowledge_chunk_hybrid_search(text, public.vector, uuid[], text, integer, double precision, double precision, double precision, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.knowledge_chunk_hybrid_search(query_text text, query_embedding public.vector, kb_ids_param uuid[], version_param text DEFAULT NULL::text, match_count integer DEFAULT 15, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 60) RETURNS SETOF public.knowledge_index
    LANGUAGE sql
    SET "hnsw.iterative_scan" TO 'relaxed_order'
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


--
-- Name: knowledge_hybrid_search(text, public.vector, uuid, integer, double precision, double precision, double precision, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.knowledge_hybrid_search(query_text text, query_embedding public.vector, project_id_param uuid, match_count integer DEFAULT 10, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 50) RETURNS SETOF public.knowledge_index
    LANGUAGE sql
    SET "hnsw.iterative_scan" TO 'relaxed_order'
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


--
-- Name: knowledge_multi_project_hybrid_search(text, public.vector, uuid[], integer, double precision, double precision, double precision, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.knowledge_multi_project_hybrid_search(query_text text, query_embedding public.vector, project_ids_param uuid[], match_count integer DEFAULT 10, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 50) RETURNS SETOF public.knowledge_index
    LANGUAGE sql
    SET "hnsw.iterative_scan" TO 'relaxed_order'
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


--
-- Name: memories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memories (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    job_id uuid NOT NULL,
    agent_id character varying(100),
    content text NOT NULL,
    summary character varying(500),
    memory_type character varying(50) DEFAULT 'factual'::character varying,
    source character varying(50) DEFAULT 'observer'::character varying,
    keywords text[] DEFAULT '{}'::text[],
    embedding public.vector(4096),
    sparse_keywords tsvector,
    importance double precision DEFAULT 0.5,
    source_turn_start integer,
    source_turn_end integer,
    source_phase integer,
    token_count integer DEFAULT 0,
    access_count integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    last_accessed timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    project_id uuid,
    remaining_turns integer,
    valid_from timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    valid_to timestamp with time zone,
    superseded_at timestamp with time zone,
    superseded_by uuid,
    CONSTRAINT valid_memory_source CHECK (((source)::text = ANY ((ARRAY['observer'::character varying, 'todo'::character varying, 'compaction'::character varying, 'phase_archive'::character varying, 'tool_error'::character varying])::text[]))),
    CONSTRAINT valid_memory_type CHECK (((memory_type)::text = ANY ((ARRAY['factual'::character varying, 'procedural'::character varying, 'error_solution'::character varying, 'vocabulary'::character varying, 'relational'::character varying])::text[])))
);


--
-- Name: memory_hybrid_search(text, public.vector, uuid, integer, double precision, double precision, double precision, integer, double precision); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.memory_hybrid_search(query_text text, query_embedding public.vector, job_id_param uuid, match_count integer DEFAULT 10, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 50, importance_floor double precision DEFAULT 0.0) RETURNS SETOF public.memories
    LANGUAGE sql
    SET "hnsw.iterative_scan" TO 'relaxed_order'
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


--
-- Name: memory_multi_project_hybrid_search(text, public.vector, uuid[], integer, double precision, double precision, double precision, integer, double precision); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.memory_multi_project_hybrid_search(query_text text, query_embedding public.vector, project_ids_param uuid[], match_count integer DEFAULT 10, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 50, importance_floor double precision DEFAULT 0.0) RETURNS SETOF public.memories
    LANGUAGE sql
    SET "hnsw.iterative_scan" TO 'relaxed_order'
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


--
-- Name: memory_project_hybrid_search(text, public.vector, uuid, integer, double precision, double precision, double precision, integer, double precision); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.memory_project_hybrid_search(query_text text, query_embedding public.vector, project_id_param uuid, match_count integer DEFAULT 10, dense_weight double precision DEFAULT 0.6, sparse_weight double precision DEFAULT 0.3, recency_weight double precision DEFAULT 0.1, rrf_k integer DEFAULT 50, importance_floor double precision DEFAULT 0.0) RETURNS SETOF public.memories
    LANGUAGE sql
    SET "hnsw.iterative_scan" TO 'relaxed_order'
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


--
-- Name: citations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.citations (
    id integer NOT NULL,
    job_id uuid NOT NULL,
    claim text NOT NULL,
    verbatim_quote text,
    quote_context text NOT NULL,
    quote_language text,
    relevance_reasoning text,
    confidence public.confidence_level DEFAULT 'high'::public.confidence_level,
    extraction_method public.extraction_method DEFAULT 'direct_quote'::public.extraction_method,
    source_id integer NOT NULL,
    locator jsonb NOT NULL,
    verification_status public.verification_status DEFAULT 'pending'::public.verification_status,
    verification_notes text,
    similarity_score real,
    matched_location jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text
);


--
-- Name: citations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.citations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: citations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.citations_id_seq OWNED BY public.citations.id;


--
-- Name: job_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_sources (
    job_id uuid NOT NULL,
    source_id integer NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: kb_index_watermark; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_index_watermark (
    kb_id uuid NOT NULL,
    repo_name text,
    branch text,
    indexed_commit character varying(64),
    pipeline_version text,
    updated_at timestamp with time zone DEFAULT now(),
    source_head character varying(64),
    status text DEFAULT 'ready'::text NOT NULL,
    last_attempt_at timestamp with time zone,
    last_success_at timestamp with time zone,
    last_error text,
    notes_done integer,
    notes_total integer,
    CONSTRAINT kb_index_watermark_status_valid CHECK ((status = ANY (ARRAY['pending'::text, 'indexing'::text, 'ready'::text, 'partial'::text, 'failed'::text])))
);


--
-- Name: knowledge_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_chunks (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    note_row uuid NOT NULL,
    kb_id uuid NOT NULL,
    chunk_ix integer NOT NULL,
    heading_path text,
    content text NOT NULL,
    embedding public.vector(4096),
    search_doc tsvector,
    embedding_version text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: knowledge_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_links (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    source_note_row uuid NOT NULL,
    kb_id uuid NOT NULL,
    source_id character varying(100) NOT NULL,
    target_id character varying(100) NOT NULL,
    rel_type text DEFAULT 'references'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: memory_retrieval_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_retrieval_messages (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    memory_id uuid NOT NULL,
    message text NOT NULL,
    embedding public.vector(4096),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    filename text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_by text DEFAULT CURRENT_USER NOT NULL,
    execution_ms integer NOT NULL,
    success boolean DEFAULT true NOT NULL,
    error text
);


--
-- Name: source_annotations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.source_annotations (
    id integer NOT NULL,
    source_id integer NOT NULL,
    job_id uuid NOT NULL,
    annotation_type text DEFAULT 'note'::text NOT NULL,
    content text NOT NULL,
    page_reference text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text
);


--
-- Name: source_annotations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.source_annotations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: source_annotations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.source_annotations_id_seq OWNED BY public.source_annotations.id;


--
-- Name: source_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.source_embeddings (
    id integer NOT NULL,
    source_id integer NOT NULL,
    job_id uuid NOT NULL,
    chunk_index integer DEFAULT 0 NOT NULL,
    chunk_text text NOT NULL,
    embedding public.vector(4096),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: source_embeddings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.source_embeddings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: source_embeddings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.source_embeddings_id_seq OWNED BY public.source_embeddings.id;


--
-- Name: source_tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.source_tags (
    id integer NOT NULL,
    source_id integer NOT NULL,
    job_id uuid NOT NULL,
    tag text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: source_tags_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.source_tags_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: source_tags_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.source_tags_id_seq OWNED BY public.source_tags.id;


--
-- Name: sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sources (
    id integer NOT NULL,
    type public.source_type NOT NULL,
    identifier text NOT NULL,
    name text NOT NULL,
    version text,
    content text NOT NULL,
    content_hash text,
    metadata jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: sources_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sources_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sources_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sources_id_seq OWNED BY public.sources.id;


--
-- Name: citations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.citations ALTER COLUMN id SET DEFAULT nextval('public.citations_id_seq'::regclass);


--
-- Name: source_annotations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_annotations ALTER COLUMN id SET DEFAULT nextval('public.source_annotations_id_seq'::regclass);


--
-- Name: source_embeddings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_embeddings ALTER COLUMN id SET DEFAULT nextval('public.source_embeddings_id_seq'::regclass);


--
-- Name: source_tags id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_tags ALTER COLUMN id SET DEFAULT nextval('public.source_tags_id_seq'::regclass);


--
-- Name: sources id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sources ALTER COLUMN id SET DEFAULT nextval('public.sources_id_seq'::regclass);


--
-- Name: citations citations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.citations
    ADD CONSTRAINT citations_pkey PRIMARY KEY (id);


--
-- Name: job_sources job_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_sources
    ADD CONSTRAINT job_sources_pkey PRIMARY KEY (job_id, source_id);


--
-- Name: kb_index_watermark kb_index_watermark_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_index_watermark
    ADD CONSTRAINT kb_index_watermark_pkey PRIMARY KEY (kb_id);


--
-- Name: knowledge_chunks knowledge_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_chunks
    ADD CONSTRAINT knowledge_chunks_pkey PRIMARY KEY (id);


--
-- Name: knowledge_index knowledge_index_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_index
    ADD CONSTRAINT knowledge_index_pkey PRIMARY KEY (id);


--
-- Name: knowledge_links knowledge_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_links
    ADD CONSTRAINT knowledge_links_pkey PRIMARY KEY (id);


--
-- Name: memories memories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memories
    ADD CONSTRAINT memories_pkey PRIMARY KEY (id);


--
-- Name: memory_retrieval_messages memory_retrieval_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_retrieval_messages
    ADD CONSTRAINT memory_retrieval_messages_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (filename);


--
-- Name: source_annotations source_annotations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_annotations
    ADD CONSTRAINT source_annotations_pkey PRIMARY KEY (id);


--
-- Name: source_embeddings source_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_embeddings
    ADD CONSTRAINT source_embeddings_pkey PRIMARY KEY (id);


--
-- Name: source_embeddings source_embeddings_source_id_job_id_chunk_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_embeddings
    ADD CONSTRAINT source_embeddings_source_id_job_id_chunk_index_key UNIQUE (source_id, job_id, chunk_index);


--
-- Name: source_tags source_tags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_tags
    ADD CONSTRAINT source_tags_pkey PRIMARY KEY (id);


--
-- Name: source_tags source_tags_source_id_job_id_tag_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_tags
    ADD CONSTRAINT source_tags_source_id_job_id_tag_key UNIQUE (source_id, job_id, tag);


--
-- Name: sources sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_pkey PRIMARY KEY (id);


--
-- Name: knowledge_chunks uq_knowledge_chunk; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_chunks
    ADD CONSTRAINT uq_knowledge_chunk UNIQUE (note_row, chunk_ix);


--
-- Name: knowledge_index uq_knowledge_project_note; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_index
    ADD CONSTRAINT uq_knowledge_project_note UNIQUE (project_id, note_id);


--
-- Name: sources uq_sources_content_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT uq_sources_content_hash UNIQUE (content_hash);


--
-- Name: idx_annotations_content_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_annotations_content_fts ON public.source_annotations USING gin (to_tsvector('simple'::regconfig, content));


--
-- Name: idx_annotations_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_annotations_job ON public.source_annotations USING btree (job_id);


--
-- Name: idx_annotations_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_annotations_source ON public.source_annotations USING btree (source_id);


--
-- Name: idx_annotations_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_annotations_type ON public.source_annotations USING btree (annotation_type);


--
-- Name: idx_citations_claim_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_citations_claim_fts ON public.citations USING gin (to_tsvector('english'::regconfig, claim));


--
-- Name: idx_citations_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_citations_created_at ON public.citations USING btree (created_at);


--
-- Name: idx_citations_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_citations_created_by ON public.citations USING btree (created_by);


--
-- Name: idx_citations_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_citations_job_id ON public.citations USING btree (job_id);


--
-- Name: idx_citations_locator; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_citations_locator ON public.citations USING gin (locator);


--
-- Name: idx_citations_source_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_citations_source_id ON public.citations USING btree (source_id);


--
-- Name: idx_citations_verification_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_citations_verification_status ON public.citations USING btree (verification_status);


--
-- Name: idx_knowledge_chunks_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_chunks_embedding ON public.knowledge_chunks USING hnsw (((public.subvector(embedding, 1, 4000))::public.halfvec(4000)) public.halfvec_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_knowledge_chunks_kb_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_chunks_kb_version ON public.knowledge_chunks USING btree (kb_id, embedding_version);


--
-- Name: idx_knowledge_chunks_note; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_chunks_note ON public.knowledge_chunks USING btree (note_row);


--
-- Name: idx_knowledge_chunks_search; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_chunks_search ON public.knowledge_chunks USING gin (search_doc);


--
-- Name: idx_knowledge_embedding_halfvec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_embedding_halfvec ON public.knowledge_index USING hnsw (((public.subvector(embedding, 1, 4000))::public.halfvec(4000)) public.halfvec_cosine_ops) WITH (m='16', ef_construction='256');


--
-- Name: idx_knowledge_index_stale; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_index_stale ON public.knowledge_index USING btree (project_id) WHERE ((remaining_cycles <= 0) AND ((status)::text = 'active'::text));


--
-- Name: idx_knowledge_kb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_kb ON public.knowledge_index USING btree (kb_id);


--
-- Name: idx_knowledge_links_note_row; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_links_note_row ON public.knowledge_links USING btree (source_note_row);


--
-- Name: idx_knowledge_links_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_links_source ON public.knowledge_links USING btree (kb_id, source_id);


--
-- Name: idx_knowledge_links_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_links_target ON public.knowledge_links USING btree (kb_id, target_id);


--
-- Name: idx_knowledge_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_project ON public.knowledge_index USING btree (project_id);


--
-- Name: idx_knowledge_project_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_project_status ON public.knowledge_index USING btree (project_id, status);


--
-- Name: idx_knowledge_project_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_project_type ON public.knowledge_index USING btree (project_id, note_type);


--
-- Name: idx_knowledge_search; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_search ON public.knowledge_index USING gin (search_doc);


--
-- Name: idx_knowledge_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_tags ON public.knowledge_index USING gin (tags);


--
-- Name: idx_memories_embedding_halfvec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_embedding_halfvec ON public.memories USING hnsw (((public.subvector(embedding, 1, 4000))::public.halfvec(4000)) public.halfvec_cosine_ops) WITH (m='16', ef_construction='256');


--
-- Name: idx_memories_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_job ON public.memories USING btree (job_id);


--
-- Name: idx_memories_job_accessed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_job_accessed ON public.memories USING btree (job_id, last_accessed DESC);


--
-- Name: idx_memories_job_importance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_job_importance ON public.memories USING btree (job_id, importance DESC);


--
-- Name: idx_memories_job_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_job_type ON public.memories USING btree (job_id, memory_type);


--
-- Name: idx_memories_job_valid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_job_valid ON public.memories USING btree (job_id) WHERE (valid_to IS NULL);


--
-- Name: idx_memories_keywords; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_keywords ON public.memories USING gin (keywords);


--
-- Name: idx_memories_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_project ON public.memories USING btree (project_id);


--
-- Name: idx_memories_project_importance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_project_importance ON public.memories USING btree (project_id, importance DESC);


--
-- Name: idx_memories_project_ttl_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_project_ttl_active ON public.memories USING btree (project_id, remaining_turns) WHERE (remaining_turns > 0);


--
-- Name: idx_memories_project_valid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_project_valid ON public.memories USING btree (project_id) WHERE (valid_to IS NULL);


--
-- Name: idx_memories_sparse; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_sparse ON public.memories USING gin (sparse_keywords);


--
-- Name: idx_memories_ttl_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_ttl_active ON public.memories USING btree (job_id, remaining_turns) WHERE (remaining_turns > 0);


--
-- Name: idx_memory_retrieval_messages_embedding_halfvec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_retrieval_messages_embedding_halfvec ON public.memory_retrieval_messages USING hnsw (((public.subvector(embedding, 1, 4000))::public.halfvec(4000)) public.halfvec_cosine_ops) WITH (m='16', ef_construction='256');


--
-- Name: idx_mrm_memory; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mrm_memory ON public.memory_retrieval_messages USING btree (memory_id);


--
-- Name: idx_source_embeddings_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_source_embeddings_job ON public.source_embeddings USING btree (job_id);


--
-- Name: idx_source_embeddings_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_source_embeddings_source ON public.source_embeddings USING btree (source_id);


--
-- Name: idx_sources_content_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_content_fts ON public.sources USING gin (to_tsvector('simple'::regconfig, content));


--
-- Name: idx_sources_content_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_content_hash ON public.sources USING btree (content_hash);


--
-- Name: idx_sources_identifier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_identifier ON public.sources USING btree (identifier);


--
-- Name: idx_sources_metadata; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_metadata ON public.sources USING gin (metadata);


--
-- Name: idx_sources_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_name ON public.sources USING btree (name);


--
-- Name: idx_sources_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_type ON public.sources USING btree (type);


--
-- Name: idx_tags_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tags_job ON public.source_tags USING btree (job_id);


--
-- Name: idx_tags_tag; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tags_tag ON public.source_tags USING btree (tag);


--
-- Name: schema_migrations_dirty_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX schema_migrations_dirty_idx ON public.schema_migrations USING btree (filename) WHERE (success = false);


--
-- Name: uq_knowledge_kb_path; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_knowledge_kb_path ON public.knowledge_index USING btree (kb_id, path) WHERE ((kb_id IS NOT NULL) AND (path IS NOT NULL));


--
-- Name: citations citations_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.citations
    ADD CONSTRAINT citations_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id);


--
-- Name: memories fk_memories_superseded_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memories
    ADD CONSTRAINT fk_memories_superseded_by FOREIGN KEY (superseded_by) REFERENCES public.memories(id) ON DELETE SET NULL;


--
-- Name: job_sources job_sources_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_sources
    ADD CONSTRAINT job_sources_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id);


--
-- Name: knowledge_chunks knowledge_chunks_note_row_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_chunks
    ADD CONSTRAINT knowledge_chunks_note_row_fkey FOREIGN KEY (note_row) REFERENCES public.knowledge_index(id) ON DELETE CASCADE;


--
-- Name: knowledge_links knowledge_links_source_note_row_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_links
    ADD CONSTRAINT knowledge_links_source_note_row_fkey FOREIGN KEY (source_note_row) REFERENCES public.knowledge_index(id) ON DELETE CASCADE;


--
-- Name: memory_retrieval_messages memory_retrieval_messages_memory_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_retrieval_messages
    ADD CONSTRAINT memory_retrieval_messages_memory_id_fkey FOREIGN KEY (memory_id) REFERENCES public.memories(id) ON DELETE CASCADE;


--
-- Name: source_annotations source_annotations_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_annotations
    ADD CONSTRAINT source_annotations_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id) ON DELETE CASCADE;


--
-- Name: source_embeddings source_embeddings_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_embeddings
    ADD CONSTRAINT source_embeddings_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id) ON DELETE CASCADE;


--
-- Name: source_tags source_tags_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_tags
    ADD CONSTRAINT source_tags_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--
