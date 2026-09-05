-- =============================================================================
-- GENERATED FILE — DO NOT EDIT BY HAND.
--
-- Canonical current schema for the 'audit' database, produced by replaying every
-- migration under src/orchestrator/database/migrations/audit/ from zero into a
-- throwaway container and dumping the result.
--
-- Source of truth : src/orchestrator/database/migrations/audit/*.sql
-- Regenerate      : scripts/schema-snapshot.sh audit
-- CI enforces that this file matches a fresh regeneration (db-migrations.yml).
-- The frozen src/orchestrator/database/{schema,vector_schema}.sql snapshots are a
-- separate, historical concern; THIS file tracks the live migration chain.
--
-- Runtime-only objects NOT present here (created outside the migration runner):
--   * Monthly audit partition children beyond the migration-seeded ones —
--     created at runtime by services/audit_partitions.py
--     (CREATE TABLE ... (LIKE parent)) as time advances.
--
-- The monthly partitions below are named _p1970_01, _p1970_02, ... and bounded
-- on 1970 dates. Those months are SYNTHETIC. The migrations seed them relative to
-- now() (current month + 2 lookahead), so a literal dump would rename every
-- leaf on the 1st of each month and report schema drift where none exists; the
-- snapshot rewrites the rolling window onto a fixed epoch instead. Real
-- databases carry the actual months. Only the count, bounds width, storage
-- params, indexes and constraints of these leaves are meaningful here.
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
-- Name: mark_usage_rollup_dirty_days_v2(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.mark_usage_rollup_dirty_days_v2() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    INSERT INTO public.usage_rollup_dirty_days (day, revision, updated_at)
    SELECT
        (inserted.ts AT TIME ZONE 'UTC')::DATE,
        1,
        statement_timestamp()
    FROM inserted_usage_events AS inserted
    GROUP BY (inserted.ts AT TIME ZONE 'UTC')::DATE
    ON CONFLICT (day) DO UPDATE
    SET revision = public.usage_rollup_dirty_days.revision + 1,
        updated_at = EXCLUDED.updated_at;

    RETURN NULL;
END;
$$;


--
-- Name: reject_usage_event_mutation_v2(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_usage_event_mutation_v2() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION
        'usage_events is append-only; publish a typed correction instead'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: round_half_even_v2(numeric, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.round_half_even_v2(input_value numeric, decimal_scale integer) RETURNS numeric
    LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    scale_factor NUMERIC;
    shifted_value NUMERIC;
    integral_part NUMERIC;
    fractional_part NUMERIC;
BEGIN
    IF decimal_scale < 0 OR decimal_scale > 38 THEN
        RAISE EXCEPTION 'decimal_scale must be between 0 and 38'
            USING ERRCODE = '22023';
    END IF;

    scale_factor := power(10::NUMERIC, decimal_scale);
    shifted_value := input_value * scale_factor;
    integral_part := trunc(shifted_value);
    fractional_part := abs(shifted_value - integral_part);

    IF fractional_part > 0.5
       OR (fractional_part = 0.5 AND mod(abs(integral_part), 2) = 1) THEN
        integral_part := integral_part + sign(shifted_value);
    END IF;

    RETURN integral_part / scale_factor;
END;
$$;


--
-- Name: FUNCTION round_half_even_v2(input_value numeric, decimal_scale integer); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.round_half_even_v2(input_value numeric, decimal_scale integer) IS 'Immutable Decimal ROUND_HALF_EVEN equivalent for v2 NUMERIC contracts.';


--
-- Name: agent_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_audit (
    id bigint NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    iteration integer,
    step_type text NOT NULL,
    node_name text,
    phase text,
    phase_number integer,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    event_phase text DEFAULT 'pre'::text NOT NULL,
    pre_id bigint,
    request_id bigint,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb,
    CONSTRAINT agent_audit_event_phase_check CHECK ((event_phase = ANY (ARRAY['pre'::text, 'post'::text]))),
    CONSTRAINT agent_audit_pre_id_check CHECK (((event_phase = 'pre'::text) = (pre_id IS NULL)))
)
PARTITION BY RANGE ("timestamp");
ALTER TABLE ONLY public.agent_audit ALTER COLUMN payload SET COMPRESSION lz4;
ALTER TABLE ONLY public.agent_audit ALTER COLUMN metadata SET COMPRESSION lz4;


--
-- Name: TABLE agent_audit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_audit IS 'Append-only agent step trace (tool calls, LLM calls, checks, errors, phase transitions, memory ops). Two-phase calls = two rows (pre/post) correlated by pre_id; one LOGICAL step per pre row. Monthly partitions, 90-day retention. NEVER UPDATE rows here and NEVER add a GIN index on payload (see file header).';


--
-- Name: COLUMN agent_audit.step_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_audit.step_type IS 'Open set; 13 values observed: initialize, llm, tool, check, warning, error, phase_transition, phase_complete, feedback_resume, memory_inject, memory_dedup, memory_store, memory_retrieve.';


--
-- Name: COLUMN agent_audit.event_phase; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_audit.event_phase IS '''pre'' = written at dispatch (tool/llm result fields null inside payload); ''post'' = second INSERT carrying the result delta that the Mongo store applied as an in-place $set.';


--
-- Name: COLUMN agent_audit.pre_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_audit.pre_id IS 'On post rows: agent_audit.id of the pre row this completes. Soft self-reference, deliberately NOT a FK (self-referential FKs between partitions sit on the most corruption-prone partitioning code path of the 15.9/16.5/16.9 minor-release fixes, and add nothing here).';


--
-- Name: COLUMN agent_audit.request_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_audit.request_id IS 'Soft reference to llm_requests.id (set on llm post rows; surfaced to the wire inside payload.llm.request_id). DELIBERATELY NOT A FOREIGN KEY: partitioned-to-partitioned FKs make partition retention permanently unexecutable when windows differ (chat_history keeps 365d against llm_requests'' 90d, so referenced partitions would still be referenced at detach time and every DETACH errors), SHARE-lock the referencing tables during detach, and ride the same bug-ridden code path as pre_id above. Orphaned ids after the referenced row ages out are by design — treat as opaque correlation ids (GitLab loose-FK / TimescaleDB norm for log stores).';


--
-- Name: COLUMN agent_audit.payload; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_audit.payload IS 'The writer''s free-form data dict (Mongo merged it at document top level; readers re-splat it over the row dict for wire parity). Known quirk preserved: at step_type=phase_complete the payload contains a "phase" OBJECT that shadows the phase TEXT column when splatted — readers must not assume phase=strategic|tactical on those rows. NO GIN INDEX — see file header.';


--
-- Name: agent_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_audit_id_seq OWNED BY public.agent_audit.id;


SET default_table_access_method = heap;

--
-- Name: agent_audit_p1970_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_audit_p1970_01 (
    id bigint DEFAULT nextval('public.agent_audit_id_seq'::regclass) NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    iteration integer,
    step_type text NOT NULL,
    node_name text,
    phase text,
    phase_number integer,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    event_phase text DEFAULT 'pre'::text NOT NULL,
    pre_id bigint,
    request_id bigint,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb,
    CONSTRAINT agent_audit_event_phase_check CHECK ((event_phase = ANY (ARRAY['pre'::text, 'post'::text]))),
    CONSTRAINT agent_audit_pre_id_check CHECK (((event_phase = 'pre'::text) = (pre_id IS NULL)))
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.agent_audit_p1970_01 ALTER COLUMN payload SET COMPRESSION lz4;
ALTER TABLE ONLY public.agent_audit_p1970_01 ALTER COLUMN metadata SET COMPRESSION lz4;


--
-- Name: agent_audit_p1970_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_audit_p1970_02 (
    id bigint DEFAULT nextval('public.agent_audit_id_seq'::regclass) NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    iteration integer,
    step_type text NOT NULL,
    node_name text,
    phase text,
    phase_number integer,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    event_phase text DEFAULT 'pre'::text NOT NULL,
    pre_id bigint,
    request_id bigint,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb,
    CONSTRAINT agent_audit_event_phase_check CHECK ((event_phase = ANY (ARRAY['pre'::text, 'post'::text]))),
    CONSTRAINT agent_audit_pre_id_check CHECK (((event_phase = 'pre'::text) = (pre_id IS NULL)))
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.agent_audit_p1970_02 ALTER COLUMN payload SET COMPRESSION lz4;
ALTER TABLE ONLY public.agent_audit_p1970_02 ALTER COLUMN metadata SET COMPRESSION lz4;


--
-- Name: agent_audit_p1970_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_audit_p1970_03 (
    id bigint DEFAULT nextval('public.agent_audit_id_seq'::regclass) NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    iteration integer,
    step_type text NOT NULL,
    node_name text,
    phase text,
    phase_number integer,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    event_phase text DEFAULT 'pre'::text NOT NULL,
    pre_id bigint,
    request_id bigint,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb,
    CONSTRAINT agent_audit_event_phase_check CHECK ((event_phase = ANY (ARRAY['pre'::text, 'post'::text]))),
    CONSTRAINT agent_audit_pre_id_check CHECK (((event_phase = 'pre'::text) = (pre_id IS NULL)))
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.agent_audit_p1970_03 ALTER COLUMN payload SET COMPRESSION lz4;
ALTER TABLE ONLY public.agent_audit_p1970_03 ALTER COLUMN metadata SET COMPRESSION lz4;


--
-- Name: chat_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_history (
    id bigint NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    iteration integer,
    model text,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    phase text,
    phase_number integer,
    request_id bigint,
    inputs jsonb NOT NULL,
    response jsonb NOT NULL,
    reasoning jsonb
)
PARTITION BY RANGE ("timestamp");
ALTER TABLE ONLY public.chat_history ALTER COLUMN inputs SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history ALTER COLUMN reasoning SET COMPRESSION lz4;


--
-- Name: TABLE chat_history; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.chat_history IS 'Conversational delta per main-loop LLM turn: inputs since the last AI message + the response, with previews. Monthly partitions, 365-day retention (longer than llm_requests — a reason request_id is a soft reference, not a FK).';


--
-- Name: COLUMN chat_history.iteration; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.chat_history.iteration IS 'Nullable but always written by the archiver (Mongo wrote null explicitly; readers treat null as absent-equivalent).';


--
-- Name: COLUMN chat_history.request_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.chat_history.request_id IS 'Soft reference to llm_requests.id (no FK — see agent_audit.request_id). Dangles by design once the llm_requests row ages out of its 90-day window.';


--
-- Name: COLUMN chat_history.inputs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.chat_history.inputs IS 'List of {type: human|tool, content, content_preview<=500, tool_call_id?, tool_name?} — messages after the last AIMessage, system messages excluded.';


--
-- Name: COLUMN chat_history.response; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.chat_history.response IS '{content, content_preview<=500, has_tool_calls, tool_calls?:[{id, name, args_preview<=200}]}.';


--
-- Name: chat_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chat_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chat_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chat_history_id_seq OWNED BY public.chat_history.id;


--
-- Name: chat_history_p1970_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_history_p1970_01 (
    id bigint DEFAULT nextval('public.chat_history_id_seq'::regclass) NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    iteration integer,
    model text,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    phase text,
    phase_number integer,
    request_id bigint,
    inputs jsonb NOT NULL,
    response jsonb NOT NULL,
    reasoning jsonb
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.chat_history_p1970_01 ALTER COLUMN inputs SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p1970_01 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p1970_01 ALTER COLUMN reasoning SET COMPRESSION lz4;


--
-- Name: chat_history_p1970_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_history_p1970_02 (
    id bigint DEFAULT nextval('public.chat_history_id_seq'::regclass) NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    iteration integer,
    model text,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    phase text,
    phase_number integer,
    request_id bigint,
    inputs jsonb NOT NULL,
    response jsonb NOT NULL,
    reasoning jsonb
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.chat_history_p1970_02 ALTER COLUMN inputs SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p1970_02 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p1970_02 ALTER COLUMN reasoning SET COMPRESSION lz4;


--
-- Name: chat_history_p1970_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_history_p1970_03 (
    id bigint DEFAULT nextval('public.chat_history_id_seq'::regclass) NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    iteration integer,
    model text,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    phase text,
    phase_number integer,
    request_id bigint,
    inputs jsonb NOT NULL,
    response jsonb NOT NULL,
    reasoning jsonb
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.chat_history_p1970_03 ALTER COLUMN inputs SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p1970_03 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p1970_03 ALTER COLUMN reasoning SET COMPRESSION lz4;


--
-- Name: llm_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_requests (
    id bigint NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    call_type text DEFAULT 'main'::text NOT NULL,
    model text NOT NULL,
    iteration integer,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    request jsonb NOT NULL,
    response jsonb NOT NULL,
    metadata jsonb,
    auxiliary_metadata jsonb,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL
)
PARTITION BY RANGE ("timestamp");
ALTER TABLE ONLY public.llm_requests ALTER COLUMN request SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests ALTER COLUMN metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests ALTER COLUMN auxiliary_metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests ALTER COLUMN metrics SET COMPRESSION lz4;


--
-- Name: TABLE llm_requests; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.llm_requests IS 'Full LLM request/response archive, one row per call. Written by the agent''s LLMArchiver only; read by /api/requests/{id} and /api/jobs/{id}/llm-requests. Monthly partitions, 90-day retention.';


--
-- Name: COLUMN llm_requests.agent_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_requests.agent_type IS 'Naming crossover preserved from Mongo: carries config.agent_id values (e.g. "universal", "vision", "transcription"), not a pod identity. There is no agent_id column because the writer has no such field.';


--
-- Name: COLUMN llm_requests.call_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_requests.call_type IS 'main | summarization | memory_extraction | memory_assembly | knowledge_curation | auxiliary | vision | transcription (open set).';


--
-- Name: COLUMN llm_requests."timestamp"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_requests."timestamp" IS 'Writer-supplied UTC insert time (partition key). DEFAULT now() is a backstop only.';


--
-- Name: COLUMN llm_requests.request; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_requests.request IS 'Full untruncated message bodies: {messages:[...], message_count, tools?, tool_count?, model_kwargs?}. Dominates row size — LZ4 TOAST.';


--
-- Name: COLUMN llm_requests.metrics; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.llm_requests.metrics IS '{input_chars, output_chars, tool_calls, token_usage:{...}}. token_usage lives HERE (nested), not top-level — the reader surfaces it in /llm-requests.';


--
-- Name: llm_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.llm_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: llm_requests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.llm_requests_id_seq OWNED BY public.llm_requests.id;


--
-- Name: llm_requests_p1970_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_requests_p1970_01 (
    id bigint DEFAULT nextval('public.llm_requests_id_seq'::regclass) NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    call_type text DEFAULT 'main'::text NOT NULL,
    model text NOT NULL,
    iteration integer,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    request jsonb NOT NULL,
    response jsonb NOT NULL,
    metadata jsonb,
    auxiliary_metadata jsonb,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.llm_requests_p1970_01 ALTER COLUMN request SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_01 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_01 ALTER COLUMN metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_01 ALTER COLUMN auxiliary_metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_01 ALTER COLUMN metrics SET COMPRESSION lz4;


--
-- Name: llm_requests_p1970_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_requests_p1970_02 (
    id bigint DEFAULT nextval('public.llm_requests_id_seq'::regclass) NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    call_type text DEFAULT 'main'::text NOT NULL,
    model text NOT NULL,
    iteration integer,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    request jsonb NOT NULL,
    response jsonb NOT NULL,
    metadata jsonb,
    auxiliary_metadata jsonb,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.llm_requests_p1970_02 ALTER COLUMN request SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_02 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_02 ALTER COLUMN metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_02 ALTER COLUMN auxiliary_metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_02 ALTER COLUMN metrics SET COMPRESSION lz4;


--
-- Name: llm_requests_p1970_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_requests_p1970_03 (
    id bigint DEFAULT nextval('public.llm_requests_id_seq'::regclass) NOT NULL,
    job_id uuid NOT NULL,
    agent_type text,
    call_type text DEFAULT 'main'::text NOT NULL,
    model text NOT NULL,
    iteration integer,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    latency_ms integer,
    request jsonb NOT NULL,
    response jsonb NOT NULL,
    metadata jsonb,
    auxiliary_metadata jsonb,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.llm_requests_p1970_03 ALTER COLUMN request SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_03 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_03 ALTER COLUMN metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_03 ALTER COLUMN auxiliary_metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p1970_03 ALTER COLUMN metrics SET COMPRESSION lz4;


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    filename text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    applied_by text DEFAULT CURRENT_USER NOT NULL,
    execution_ms integer NOT NULL,
    success boolean DEFAULT true NOT NULL,
    error text
);


--
-- Name: usage_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_events (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid,
    project_id uuid,
    ref_kind text,
    ref_id uuid,
    category text NOT NULL,
    resource text NOT NULL,
    quantity numeric NOT NULL,
    unit text NOT NULL,
    rate_usd numeric,
    cost_usd numeric,
    source text NOT NULL,
    source_id text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    measurement_basis text,
    cost_domain text,
    resource_class text,
    attribution_scope text,
    measurement_algorithm text,
    source_capacity_value numeric,
    source_capacity_unit text,
    source_cluster text,
    source_kind text,
    source_uid text,
    source_lifecycle_id uuid,
    source_interval_id uuid,
    event_kind text,
    corrects_source text,
    corrects_source_id text,
    corrects_unit text,
    corrects_ts timestamp with time zone,
    correction_group_id uuid,
    correction_reason text,
    correction_actor_id uuid,
    discovered_at timestamp with time zone,
    payload_hash text,
    CONSTRAINT usage_events_event_kind_v2_check CHECK ((((source = 'infra-allocation-v2'::text) AND (event_kind = ANY (ARRAY['usage'::text, 'late-usage'::text])) AND (quantity >= (0)::numeric) AND (corrects_source IS NULL) AND (corrects_source_id IS NULL) AND (corrects_unit IS NULL) AND (corrects_ts IS NULL) AND (correction_group_id IS NULL) AND (correction_reason IS NULL) AND (correction_actor_id IS NULL) AND (((event_kind = 'usage'::text) AND (discovered_at IS NULL)) OR ((event_kind = 'late-usage'::text) AND (discovered_at IS NOT NULL) AND (discovered_at >= period_end)))) OR ((source = 'infra-allocation-correction-v2'::text) AND (event_kind = 'correction'::text) AND (corrects_source = 'infra-allocation-v2'::text) AND (corrects_source_id IS NOT NULL) AND (corrects_source_id <> ''::text) AND (corrects_unit IS NOT NULL) AND (corrects_unit = unit) AND (corrects_ts IS NOT NULL) AND (corrects_ts = period_start) AND (correction_group_id IS NOT NULL) AND (correction_reason IS NOT NULL) AND (correction_reason <> ''::text) AND (correction_actor_id IS NOT NULL) AND ((discovered_at IS NULL) OR (discovered_at >= period_end))) OR ((source <> ALL (ARRAY['infra-allocation-v2'::text, 'infra-allocation-correction-v2'::text])) AND (event_kind IS NULL) AND (corrects_source IS NULL) AND (corrects_source_id IS NULL) AND (corrects_unit IS NULL) AND (corrects_ts IS NULL) AND (correction_group_id IS NULL) AND (correction_reason IS NULL) AND (correction_actor_id IS NULL) AND (discovered_at IS NULL)))),
    CONSTRAINT usage_events_infra_v2_contract_check CHECK (((source <> ALL (ARRAY['infra-allocation-v2'::text, 'infra-allocation-correction-v2'::text])) OR ((period_start IS NOT NULL) AND (period_end IS NOT NULL) AND (ts = period_start) AND (period_end <= (date_trunc('day'::text, period_start, 'UTC'::text) + '1 day'::interval)) AND (measurement_basis IS NOT NULL) AND (cost_domain IS NOT NULL) AND (resource_class IS NOT NULL) AND (resource_class <> ''::text) AND (attribution_scope IS NOT NULL) AND (measurement_algorithm IS NOT NULL) AND (measurement_algorithm <> ''::text) AND (source_capacity_value IS NOT NULL) AND (source_capacity_value <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(source_capacity_value) < '100000000000000000000'::numeric) AND (source_capacity_value = trunc(source_capacity_value, 18)) AND (source_capacity_value >= (0)::numeric) AND (source_capacity_value = trunc(source_capacity_value)) AND (source_capacity_unit IS NOT NULL) AND (source_capacity_unit <> ''::text) AND (source_cluster IS NOT NULL) AND (source_cluster <> ''::text) AND (source_kind IS NOT NULL) AND (source_uid IS NOT NULL) AND (source_uid <> ''::text) AND (source_lifecycle_id IS NOT NULL) AND (source_interval_id IS NOT NULL) AND (event_kind IS NOT NULL) AND (payload_hash IS NOT NULL) AND (payload_hash ~ '^[0-9a-f]{64}$'::text) AND (jsonb_typeof(details) = 'object'::text) AND (category <> ''::text) AND (resource <> ''::text) AND (unit <> ''::text) AND (cost_domain = ANY (ARRAY['workload-allocation'::text, 'physical-asset'::text, 'idle'::text, 'overhead'::text])) AND (attribution_scope = ANY (ARRAY['customer'::text, 'shared-platform'::text, 'unknown'::text])) AND (((attribution_scope = 'customer'::text) AND (ref_kind IS NOT NULL) AND (ref_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (ref_id IS NOT NULL) AND (user_id IS NOT NULL)) OR ((attribution_scope = ANY (ARRAY['shared-platform'::text, 'unknown'::text])) AND (user_id IS NULL) AND (project_id IS NULL))) AND (((source_kind = 'pod'::text) AND (category = 'compute'::text) AND (measurement_basis = 'scheduler-request'::text) AND (resource_class = 'kubernetes-pod'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'vcpu-hour'::text) AND (source_capacity_unit = 'millicore'::text)) OR ((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)))) OR ((source_kind = 'vmi'::text) AND (category = 'compute'::text) AND (measurement_basis = 'guest-provisioned'::text) AND (resource_class = 'virtual-machine'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'vcpu-hour'::text) AND (source_capacity_unit = 'millicore'::text)) OR ((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)))) OR ((source_kind = 'pvc'::text) AND (category = 'storage'::text) AND (measurement_basis = 'claim-requested'::text) AND (resource_class = 'persistent-volume-claim'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)) OR ((unit = 'claim-hour'::text) AND (source_capacity_unit = 'instance'::text) AND (source_capacity_value = (1)::numeric)))) OR ((source_kind = 'volume'::text) AND (category = 'storage'::text) AND (measurement_basis = 'volume-provisioned'::text) AND (resource_class = 'persistent-volume'::text) AND (cost_domain = 'physical-asset'::text) AND (((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)) OR ((unit = 'volume-hour'::text) AND (source_capacity_unit = 'instance'::text) AND (source_capacity_value = (1)::numeric))))) AND (((rate_usd IS NULL) AND (cost_usd IS NULL)) OR ((rate_usd IS NOT NULL) AND (rate_usd <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(rate_usd) < '100000000000000000000'::numeric) AND (rate_usd = trunc(rate_usd, 18)) AND (rate_usd >= (0)::numeric) AND (cost_usd IS NOT NULL) AND (cost_usd <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(cost_usd) < '100000000000000000000'::numeric) AND (cost_usd = trunc(cost_usd, 18)) AND
CASE
    WHEN ((quantity = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) OR (rate_usd = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) OR (cost_usd = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))) THEN false
    ELSE (cost_usd = public.round_half_even_v2((quantity * rate_usd), 18))
END)) AND (quantity <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(quantity) < '100000000000000000000'::numeric) AND (quantity = trunc(quantity, 18))))),
    CONSTRAINT usage_events_period_bounds_v2_check CHECK ((((period_start IS NULL) = (period_end IS NULL)) AND ((period_start IS NULL) OR (period_end > period_start))))
)
PARTITION BY RANGE (ts);
ALTER TABLE ONLY public.usage_events ALTER COLUMN details SET COMPRESSION lz4;


--
-- Name: TABLE usage_events; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.usage_events IS 'Append-only usage/cost ledger, one row per metered resource dimension. Written by the orchestrator (compute intervals + LiteLLM spend-log materialization); read by /api/usage and the usage_daily rollup. Monthly partitions on ts (audit-store machinery), partition column is ts not timestamp. NEVER UPDATE rows and NEVER add a GIN index on details.';


--
-- Name: COLUMN usage_events.ts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_events.ts IS 'Usage/interval-end time (partition key, UTC). Emitter-supplied so the dedupe index is stable; DEFAULT now() is a backstop only.';


--
-- Name: COLUMN usage_events.category; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_events.category IS 'llm | compute | query | storage (open set). query/storage reserved.';


--
-- Name: COLUMN usage_events.quantity; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_events.quantity IS 'Scalar amount in `unit`. One row per dimension keeps this 1:1 with a usage_rates (category, resource, unit) row.';


--
-- Name: COLUMN usage_events.rate_usd; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_events.rate_usd IS 'Snapshot of the rate applied at write time — history is immutable, a later rate edit never rewrites past cost. NULL when the resource is unpriced (homelab models today): quantity is still recorded, cost is just absent.';


--
-- Name: COLUMN usage_events.source_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_events.source_id IS 'Per-source idempotency id: LiteLLM request_id (LLM) or a deterministic workspace-interval key (compute). With source + unit it dedupes re-emits (ON CONFLICT DO NOTHING) so the at-least-once emitters cannot double-count.';


--
-- Name: COLUMN usage_events.details; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_events.details IS '{pod_name, started_at, ended_at, cpu_millicores, mem_bytes, model, request_id, ...} — free-form context. LZ4 TOAST. NO GIN INDEX.';


--
-- Name: COLUMN usage_events.period_start; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_events.period_start IS 'Typed infrastructure half-open segment start; NULL for legacy point events.';


--
-- Name: COLUMN usage_events.payload_hash; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_events.payload_hash IS 'Lowercase SHA-256 of the versioned canonical typed event payload.';


--
-- Name: usage_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.usage_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: usage_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.usage_events_id_seq OWNED BY public.usage_events.id;


--
-- Name: usage_events_p1970_01; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_events_p1970_01 (
    id bigint DEFAULT nextval('public.usage_events_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid,
    project_id uuid,
    ref_kind text,
    ref_id uuid,
    category text NOT NULL,
    resource text NOT NULL,
    quantity numeric NOT NULL,
    unit text NOT NULL,
    rate_usd numeric,
    cost_usd numeric,
    source text NOT NULL,
    source_id text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    measurement_basis text,
    cost_domain text,
    resource_class text,
    attribution_scope text,
    measurement_algorithm text,
    source_capacity_value numeric,
    source_capacity_unit text,
    source_cluster text,
    source_kind text,
    source_uid text,
    source_lifecycle_id uuid,
    source_interval_id uuid,
    event_kind text,
    corrects_source text,
    corrects_source_id text,
    corrects_unit text,
    corrects_ts timestamp with time zone,
    correction_group_id uuid,
    correction_reason text,
    correction_actor_id uuid,
    discovered_at timestamp with time zone,
    payload_hash text,
    CONSTRAINT usage_events_event_kind_v2_check CHECK ((((source = 'infra-allocation-v2'::text) AND (event_kind = ANY (ARRAY['usage'::text, 'late-usage'::text])) AND (quantity >= (0)::numeric) AND (corrects_source IS NULL) AND (corrects_source_id IS NULL) AND (corrects_unit IS NULL) AND (corrects_ts IS NULL) AND (correction_group_id IS NULL) AND (correction_reason IS NULL) AND (correction_actor_id IS NULL) AND (((event_kind = 'usage'::text) AND (discovered_at IS NULL)) OR ((event_kind = 'late-usage'::text) AND (discovered_at IS NOT NULL) AND (discovered_at >= period_end)))) OR ((source = 'infra-allocation-correction-v2'::text) AND (event_kind = 'correction'::text) AND (corrects_source = 'infra-allocation-v2'::text) AND (corrects_source_id IS NOT NULL) AND (corrects_source_id <> ''::text) AND (corrects_unit IS NOT NULL) AND (corrects_unit = unit) AND (corrects_ts IS NOT NULL) AND (corrects_ts = period_start) AND (correction_group_id IS NOT NULL) AND (correction_reason IS NOT NULL) AND (correction_reason <> ''::text) AND (correction_actor_id IS NOT NULL) AND ((discovered_at IS NULL) OR (discovered_at >= period_end))) OR ((source <> ALL (ARRAY['infra-allocation-v2'::text, 'infra-allocation-correction-v2'::text])) AND (event_kind IS NULL) AND (corrects_source IS NULL) AND (corrects_source_id IS NULL) AND (corrects_unit IS NULL) AND (corrects_ts IS NULL) AND (correction_group_id IS NULL) AND (correction_reason IS NULL) AND (correction_actor_id IS NULL) AND (discovered_at IS NULL)))),
    CONSTRAINT usage_events_infra_v2_contract_check CHECK (((source <> ALL (ARRAY['infra-allocation-v2'::text, 'infra-allocation-correction-v2'::text])) OR ((period_start IS NOT NULL) AND (period_end IS NOT NULL) AND (ts = period_start) AND (period_end <= (date_trunc('day'::text, period_start, 'UTC'::text) + '1 day'::interval)) AND (measurement_basis IS NOT NULL) AND (cost_domain IS NOT NULL) AND (resource_class IS NOT NULL) AND (resource_class <> ''::text) AND (attribution_scope IS NOT NULL) AND (measurement_algorithm IS NOT NULL) AND (measurement_algorithm <> ''::text) AND (source_capacity_value IS NOT NULL) AND (source_capacity_value <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(source_capacity_value) < '100000000000000000000'::numeric) AND (source_capacity_value = trunc(source_capacity_value, 18)) AND (source_capacity_value >= (0)::numeric) AND (source_capacity_value = trunc(source_capacity_value)) AND (source_capacity_unit IS NOT NULL) AND (source_capacity_unit <> ''::text) AND (source_cluster IS NOT NULL) AND (source_cluster <> ''::text) AND (source_kind IS NOT NULL) AND (source_uid IS NOT NULL) AND (source_uid <> ''::text) AND (source_lifecycle_id IS NOT NULL) AND (source_interval_id IS NOT NULL) AND (event_kind IS NOT NULL) AND (payload_hash IS NOT NULL) AND (payload_hash ~ '^[0-9a-f]{64}$'::text) AND (jsonb_typeof(details) = 'object'::text) AND (category <> ''::text) AND (resource <> ''::text) AND (unit <> ''::text) AND (cost_domain = ANY (ARRAY['workload-allocation'::text, 'physical-asset'::text, 'idle'::text, 'overhead'::text])) AND (attribution_scope = ANY (ARRAY['customer'::text, 'shared-platform'::text, 'unknown'::text])) AND (((attribution_scope = 'customer'::text) AND (ref_kind IS NOT NULL) AND (ref_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (ref_id IS NOT NULL) AND (user_id IS NOT NULL)) OR ((attribution_scope = ANY (ARRAY['shared-platform'::text, 'unknown'::text])) AND (user_id IS NULL) AND (project_id IS NULL))) AND (((source_kind = 'pod'::text) AND (category = 'compute'::text) AND (measurement_basis = 'scheduler-request'::text) AND (resource_class = 'kubernetes-pod'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'vcpu-hour'::text) AND (source_capacity_unit = 'millicore'::text)) OR ((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)))) OR ((source_kind = 'vmi'::text) AND (category = 'compute'::text) AND (measurement_basis = 'guest-provisioned'::text) AND (resource_class = 'virtual-machine'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'vcpu-hour'::text) AND (source_capacity_unit = 'millicore'::text)) OR ((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)))) OR ((source_kind = 'pvc'::text) AND (category = 'storage'::text) AND (measurement_basis = 'claim-requested'::text) AND (resource_class = 'persistent-volume-claim'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)) OR ((unit = 'claim-hour'::text) AND (source_capacity_unit = 'instance'::text) AND (source_capacity_value = (1)::numeric)))) OR ((source_kind = 'volume'::text) AND (category = 'storage'::text) AND (measurement_basis = 'volume-provisioned'::text) AND (resource_class = 'persistent-volume'::text) AND (cost_domain = 'physical-asset'::text) AND (((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)) OR ((unit = 'volume-hour'::text) AND (source_capacity_unit = 'instance'::text) AND (source_capacity_value = (1)::numeric))))) AND (((rate_usd IS NULL) AND (cost_usd IS NULL)) OR ((rate_usd IS NOT NULL) AND (rate_usd <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(rate_usd) < '100000000000000000000'::numeric) AND (rate_usd = trunc(rate_usd, 18)) AND (rate_usd >= (0)::numeric) AND (cost_usd IS NOT NULL) AND (cost_usd <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(cost_usd) < '100000000000000000000'::numeric) AND (cost_usd = trunc(cost_usd, 18)) AND
CASE
    WHEN ((quantity = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) OR (rate_usd = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) OR (cost_usd = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))) THEN false
    ELSE (cost_usd = public.round_half_even_v2((quantity * rate_usd), 18))
END)) AND (quantity <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(quantity) < '100000000000000000000'::numeric) AND (quantity = trunc(quantity, 18))))),
    CONSTRAINT usage_events_period_bounds_v2_check CHECK ((((period_start IS NULL) = (period_end IS NULL)) AND ((period_start IS NULL) OR (period_end > period_start))))
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.usage_events_p1970_01 ALTER COLUMN details SET COMPRESSION lz4;


--
-- Name: usage_events_p1970_02; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_events_p1970_02 (
    id bigint DEFAULT nextval('public.usage_events_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid,
    project_id uuid,
    ref_kind text,
    ref_id uuid,
    category text NOT NULL,
    resource text NOT NULL,
    quantity numeric NOT NULL,
    unit text NOT NULL,
    rate_usd numeric,
    cost_usd numeric,
    source text NOT NULL,
    source_id text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    measurement_basis text,
    cost_domain text,
    resource_class text,
    attribution_scope text,
    measurement_algorithm text,
    source_capacity_value numeric,
    source_capacity_unit text,
    source_cluster text,
    source_kind text,
    source_uid text,
    source_lifecycle_id uuid,
    source_interval_id uuid,
    event_kind text,
    corrects_source text,
    corrects_source_id text,
    corrects_unit text,
    corrects_ts timestamp with time zone,
    correction_group_id uuid,
    correction_reason text,
    correction_actor_id uuid,
    discovered_at timestamp with time zone,
    payload_hash text,
    CONSTRAINT usage_events_event_kind_v2_check CHECK ((((source = 'infra-allocation-v2'::text) AND (event_kind = ANY (ARRAY['usage'::text, 'late-usage'::text])) AND (quantity >= (0)::numeric) AND (corrects_source IS NULL) AND (corrects_source_id IS NULL) AND (corrects_unit IS NULL) AND (corrects_ts IS NULL) AND (correction_group_id IS NULL) AND (correction_reason IS NULL) AND (correction_actor_id IS NULL) AND (((event_kind = 'usage'::text) AND (discovered_at IS NULL)) OR ((event_kind = 'late-usage'::text) AND (discovered_at IS NOT NULL) AND (discovered_at >= period_end)))) OR ((source = 'infra-allocation-correction-v2'::text) AND (event_kind = 'correction'::text) AND (corrects_source = 'infra-allocation-v2'::text) AND (corrects_source_id IS NOT NULL) AND (corrects_source_id <> ''::text) AND (corrects_unit IS NOT NULL) AND (corrects_unit = unit) AND (corrects_ts IS NOT NULL) AND (corrects_ts = period_start) AND (correction_group_id IS NOT NULL) AND (correction_reason IS NOT NULL) AND (correction_reason <> ''::text) AND (correction_actor_id IS NOT NULL) AND ((discovered_at IS NULL) OR (discovered_at >= period_end))) OR ((source <> ALL (ARRAY['infra-allocation-v2'::text, 'infra-allocation-correction-v2'::text])) AND (event_kind IS NULL) AND (corrects_source IS NULL) AND (corrects_source_id IS NULL) AND (corrects_unit IS NULL) AND (corrects_ts IS NULL) AND (correction_group_id IS NULL) AND (correction_reason IS NULL) AND (correction_actor_id IS NULL) AND (discovered_at IS NULL)))),
    CONSTRAINT usage_events_infra_v2_contract_check CHECK (((source <> ALL (ARRAY['infra-allocation-v2'::text, 'infra-allocation-correction-v2'::text])) OR ((period_start IS NOT NULL) AND (period_end IS NOT NULL) AND (ts = period_start) AND (period_end <= (date_trunc('day'::text, period_start, 'UTC'::text) + '1 day'::interval)) AND (measurement_basis IS NOT NULL) AND (cost_domain IS NOT NULL) AND (resource_class IS NOT NULL) AND (resource_class <> ''::text) AND (attribution_scope IS NOT NULL) AND (measurement_algorithm IS NOT NULL) AND (measurement_algorithm <> ''::text) AND (source_capacity_value IS NOT NULL) AND (source_capacity_value <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(source_capacity_value) < '100000000000000000000'::numeric) AND (source_capacity_value = trunc(source_capacity_value, 18)) AND (source_capacity_value >= (0)::numeric) AND (source_capacity_value = trunc(source_capacity_value)) AND (source_capacity_unit IS NOT NULL) AND (source_capacity_unit <> ''::text) AND (source_cluster IS NOT NULL) AND (source_cluster <> ''::text) AND (source_kind IS NOT NULL) AND (source_uid IS NOT NULL) AND (source_uid <> ''::text) AND (source_lifecycle_id IS NOT NULL) AND (source_interval_id IS NOT NULL) AND (event_kind IS NOT NULL) AND (payload_hash IS NOT NULL) AND (payload_hash ~ '^[0-9a-f]{64}$'::text) AND (jsonb_typeof(details) = 'object'::text) AND (category <> ''::text) AND (resource <> ''::text) AND (unit <> ''::text) AND (cost_domain = ANY (ARRAY['workload-allocation'::text, 'physical-asset'::text, 'idle'::text, 'overhead'::text])) AND (attribution_scope = ANY (ARRAY['customer'::text, 'shared-platform'::text, 'unknown'::text])) AND (((attribution_scope = 'customer'::text) AND (ref_kind IS NOT NULL) AND (ref_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (ref_id IS NOT NULL) AND (user_id IS NOT NULL)) OR ((attribution_scope = ANY (ARRAY['shared-platform'::text, 'unknown'::text])) AND (user_id IS NULL) AND (project_id IS NULL))) AND (((source_kind = 'pod'::text) AND (category = 'compute'::text) AND (measurement_basis = 'scheduler-request'::text) AND (resource_class = 'kubernetes-pod'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'vcpu-hour'::text) AND (source_capacity_unit = 'millicore'::text)) OR ((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)))) OR ((source_kind = 'vmi'::text) AND (category = 'compute'::text) AND (measurement_basis = 'guest-provisioned'::text) AND (resource_class = 'virtual-machine'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'vcpu-hour'::text) AND (source_capacity_unit = 'millicore'::text)) OR ((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)))) OR ((source_kind = 'pvc'::text) AND (category = 'storage'::text) AND (measurement_basis = 'claim-requested'::text) AND (resource_class = 'persistent-volume-claim'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)) OR ((unit = 'claim-hour'::text) AND (source_capacity_unit = 'instance'::text) AND (source_capacity_value = (1)::numeric)))) OR ((source_kind = 'volume'::text) AND (category = 'storage'::text) AND (measurement_basis = 'volume-provisioned'::text) AND (resource_class = 'persistent-volume'::text) AND (cost_domain = 'physical-asset'::text) AND (((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)) OR ((unit = 'volume-hour'::text) AND (source_capacity_unit = 'instance'::text) AND (source_capacity_value = (1)::numeric))))) AND (((rate_usd IS NULL) AND (cost_usd IS NULL)) OR ((rate_usd IS NOT NULL) AND (rate_usd <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(rate_usd) < '100000000000000000000'::numeric) AND (rate_usd = trunc(rate_usd, 18)) AND (rate_usd >= (0)::numeric) AND (cost_usd IS NOT NULL) AND (cost_usd <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(cost_usd) < '100000000000000000000'::numeric) AND (cost_usd = trunc(cost_usd, 18)) AND
CASE
    WHEN ((quantity = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) OR (rate_usd = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) OR (cost_usd = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))) THEN false
    ELSE (cost_usd = public.round_half_even_v2((quantity * rate_usd), 18))
END)) AND (quantity <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(quantity) < '100000000000000000000'::numeric) AND (quantity = trunc(quantity, 18))))),
    CONSTRAINT usage_events_period_bounds_v2_check CHECK ((((period_start IS NULL) = (period_end IS NULL)) AND ((period_start IS NULL) OR (period_end > period_start))))
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.usage_events_p1970_02 ALTER COLUMN details SET COMPRESSION lz4;


--
-- Name: usage_events_p1970_03; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_events_p1970_03 (
    id bigint DEFAULT nextval('public.usage_events_id_seq'::regclass) NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid,
    project_id uuid,
    ref_kind text,
    ref_id uuid,
    category text NOT NULL,
    resource text NOT NULL,
    quantity numeric NOT NULL,
    unit text NOT NULL,
    rate_usd numeric,
    cost_usd numeric,
    source text NOT NULL,
    source_id text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    measurement_basis text,
    cost_domain text,
    resource_class text,
    attribution_scope text,
    measurement_algorithm text,
    source_capacity_value numeric,
    source_capacity_unit text,
    source_cluster text,
    source_kind text,
    source_uid text,
    source_lifecycle_id uuid,
    source_interval_id uuid,
    event_kind text,
    corrects_source text,
    corrects_source_id text,
    corrects_unit text,
    corrects_ts timestamp with time zone,
    correction_group_id uuid,
    correction_reason text,
    correction_actor_id uuid,
    discovered_at timestamp with time zone,
    payload_hash text,
    CONSTRAINT usage_events_event_kind_v2_check CHECK ((((source = 'infra-allocation-v2'::text) AND (event_kind = ANY (ARRAY['usage'::text, 'late-usage'::text])) AND (quantity >= (0)::numeric) AND (corrects_source IS NULL) AND (corrects_source_id IS NULL) AND (corrects_unit IS NULL) AND (corrects_ts IS NULL) AND (correction_group_id IS NULL) AND (correction_reason IS NULL) AND (correction_actor_id IS NULL) AND (((event_kind = 'usage'::text) AND (discovered_at IS NULL)) OR ((event_kind = 'late-usage'::text) AND (discovered_at IS NOT NULL) AND (discovered_at >= period_end)))) OR ((source = 'infra-allocation-correction-v2'::text) AND (event_kind = 'correction'::text) AND (corrects_source = 'infra-allocation-v2'::text) AND (corrects_source_id IS NOT NULL) AND (corrects_source_id <> ''::text) AND (corrects_unit IS NOT NULL) AND (corrects_unit = unit) AND (corrects_ts IS NOT NULL) AND (corrects_ts = period_start) AND (correction_group_id IS NOT NULL) AND (correction_reason IS NOT NULL) AND (correction_reason <> ''::text) AND (correction_actor_id IS NOT NULL) AND ((discovered_at IS NULL) OR (discovered_at >= period_end))) OR ((source <> ALL (ARRAY['infra-allocation-v2'::text, 'infra-allocation-correction-v2'::text])) AND (event_kind IS NULL) AND (corrects_source IS NULL) AND (corrects_source_id IS NULL) AND (corrects_unit IS NULL) AND (corrects_ts IS NULL) AND (correction_group_id IS NULL) AND (correction_reason IS NULL) AND (correction_actor_id IS NULL) AND (discovered_at IS NULL)))),
    CONSTRAINT usage_events_infra_v2_contract_check CHECK (((source <> ALL (ARRAY['infra-allocation-v2'::text, 'infra-allocation-correction-v2'::text])) OR ((period_start IS NOT NULL) AND (period_end IS NOT NULL) AND (ts = period_start) AND (period_end <= (date_trunc('day'::text, period_start, 'UTC'::text) + '1 day'::interval)) AND (measurement_basis IS NOT NULL) AND (cost_domain IS NOT NULL) AND (resource_class IS NOT NULL) AND (resource_class <> ''::text) AND (attribution_scope IS NOT NULL) AND (measurement_algorithm IS NOT NULL) AND (measurement_algorithm <> ''::text) AND (source_capacity_value IS NOT NULL) AND (source_capacity_value <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(source_capacity_value) < '100000000000000000000'::numeric) AND (source_capacity_value = trunc(source_capacity_value, 18)) AND (source_capacity_value >= (0)::numeric) AND (source_capacity_value = trunc(source_capacity_value)) AND (source_capacity_unit IS NOT NULL) AND (source_capacity_unit <> ''::text) AND (source_cluster IS NOT NULL) AND (source_cluster <> ''::text) AND (source_kind IS NOT NULL) AND (source_uid IS NOT NULL) AND (source_uid <> ''::text) AND (source_lifecycle_id IS NOT NULL) AND (source_interval_id IS NOT NULL) AND (event_kind IS NOT NULL) AND (payload_hash IS NOT NULL) AND (payload_hash ~ '^[0-9a-f]{64}$'::text) AND (jsonb_typeof(details) = 'object'::text) AND (category <> ''::text) AND (resource <> ''::text) AND (unit <> ''::text) AND (cost_domain = ANY (ARRAY['workload-allocation'::text, 'physical-asset'::text, 'idle'::text, 'overhead'::text])) AND (attribution_scope = ANY (ARRAY['customer'::text, 'shared-platform'::text, 'unknown'::text])) AND (((attribution_scope = 'customer'::text) AND (ref_kind IS NOT NULL) AND (ref_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (ref_id IS NOT NULL) AND (user_id IS NOT NULL)) OR ((attribution_scope = ANY (ARRAY['shared-platform'::text, 'unknown'::text])) AND (user_id IS NULL) AND (project_id IS NULL))) AND (((source_kind = 'pod'::text) AND (category = 'compute'::text) AND (measurement_basis = 'scheduler-request'::text) AND (resource_class = 'kubernetes-pod'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'vcpu-hour'::text) AND (source_capacity_unit = 'millicore'::text)) OR ((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)))) OR ((source_kind = 'vmi'::text) AND (category = 'compute'::text) AND (measurement_basis = 'guest-provisioned'::text) AND (resource_class = 'virtual-machine'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'vcpu-hour'::text) AND (source_capacity_unit = 'millicore'::text)) OR ((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)))) OR ((source_kind = 'pvc'::text) AND (category = 'storage'::text) AND (measurement_basis = 'claim-requested'::text) AND (resource_class = 'persistent-volume-claim'::text) AND (cost_domain = 'workload-allocation'::text) AND (((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)) OR ((unit = 'claim-hour'::text) AND (source_capacity_unit = 'instance'::text) AND (source_capacity_value = (1)::numeric)))) OR ((source_kind = 'volume'::text) AND (category = 'storage'::text) AND (measurement_basis = 'volume-provisioned'::text) AND (resource_class = 'persistent-volume'::text) AND (cost_domain = 'physical-asset'::text) AND (((unit = 'gib-hour'::text) AND (source_capacity_unit = 'byte'::text)) OR ((unit = 'volume-hour'::text) AND (source_capacity_unit = 'instance'::text) AND (source_capacity_value = (1)::numeric))))) AND (((rate_usd IS NULL) AND (cost_usd IS NULL)) OR ((rate_usd IS NOT NULL) AND (rate_usd <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(rate_usd) < '100000000000000000000'::numeric) AND (rate_usd = trunc(rate_usd, 18)) AND (rate_usd >= (0)::numeric) AND (cost_usd IS NOT NULL) AND (cost_usd <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(cost_usd) < '100000000000000000000'::numeric) AND (cost_usd = trunc(cost_usd, 18)) AND
CASE
    WHEN ((quantity = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) OR (rate_usd = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) OR (cost_usd = ANY (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))) THEN false
    ELSE (cost_usd = public.round_half_even_v2((quantity * rate_usd), 18))
END)) AND (quantity <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (abs(quantity) < '100000000000000000000'::numeric) AND (quantity = trunc(quantity, 18))))),
    CONSTRAINT usage_events_period_bounds_v2_check CHECK ((((period_start IS NULL) = (period_end IS NULL)) AND ((period_start IS NULL) OR (period_end > period_start))))
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.usage_events_p1970_03 ALTER COLUMN details SET COMPRESSION lz4;


--
-- Name: usage_rollup_dirty_days; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rollup_dirty_days (
    day date NOT NULL,
    revision bigint DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_rollup_dirty_days_revision_check CHECK ((revision > 0))
);


--
-- Name: TABLE usage_rollup_dirty_days; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.usage_rollup_dirty_days IS 'Monotonic per-UTC-day audit change tokens for repeatable v2 daily rebuilds.';


--
-- Name: agent_audit_p1970_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit ATTACH PARTITION public.agent_audit_p1970_01 FOR VALUES FROM ('1970-01-01 00:00:00+00') TO ('1970-02-01 00:00:00+00');


--
-- Name: agent_audit_p1970_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit ATTACH PARTITION public.agent_audit_p1970_02 FOR VALUES FROM ('1970-02-01 00:00:00+00') TO ('1970-03-01 00:00:00+00');


--
-- Name: agent_audit_p1970_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit ATTACH PARTITION public.agent_audit_p1970_03 FOR VALUES FROM ('1970-03-01 00:00:00+00') TO ('1970-04-01 00:00:00+00');


--
-- Name: chat_history_p1970_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history ATTACH PARTITION public.chat_history_p1970_01 FOR VALUES FROM ('1970-01-01 00:00:00+00') TO ('1970-02-01 00:00:00+00');


--
-- Name: chat_history_p1970_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history ATTACH PARTITION public.chat_history_p1970_02 FOR VALUES FROM ('1970-02-01 00:00:00+00') TO ('1970-03-01 00:00:00+00');


--
-- Name: chat_history_p1970_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history ATTACH PARTITION public.chat_history_p1970_03 FOR VALUES FROM ('1970-03-01 00:00:00+00') TO ('1970-04-01 00:00:00+00');


--
-- Name: llm_requests_p1970_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests ATTACH PARTITION public.llm_requests_p1970_01 FOR VALUES FROM ('1970-01-01 00:00:00+00') TO ('1970-02-01 00:00:00+00');


--
-- Name: llm_requests_p1970_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests ATTACH PARTITION public.llm_requests_p1970_02 FOR VALUES FROM ('1970-02-01 00:00:00+00') TO ('1970-03-01 00:00:00+00');


--
-- Name: llm_requests_p1970_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests ATTACH PARTITION public.llm_requests_p1970_03 FOR VALUES FROM ('1970-03-01 00:00:00+00') TO ('1970-04-01 00:00:00+00');


--
-- Name: usage_events_p1970_01; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events ATTACH PARTITION public.usage_events_p1970_01 FOR VALUES FROM ('1970-01-01 00:00:00+00') TO ('1970-02-01 00:00:00+00');


--
-- Name: usage_events_p1970_02; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events ATTACH PARTITION public.usage_events_p1970_02 FOR VALUES FROM ('1970-02-01 00:00:00+00') TO ('1970-03-01 00:00:00+00');


--
-- Name: usage_events_p1970_03; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events ATTACH PARTITION public.usage_events_p1970_03 FOR VALUES FROM ('1970-03-01 00:00:00+00') TO ('1970-04-01 00:00:00+00');


--
-- Name: agent_audit id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit ALTER COLUMN id SET DEFAULT nextval('public.agent_audit_id_seq'::regclass);


--
-- Name: chat_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history ALTER COLUMN id SET DEFAULT nextval('public.chat_history_id_seq'::regclass);


--
-- Name: llm_requests id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests ALTER COLUMN id SET DEFAULT nextval('public.llm_requests_id_seq'::regclass);


--
-- Name: usage_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events ALTER COLUMN id SET DEFAULT nextval('public.usage_events_id_seq'::regclass);


--
-- Name: agent_audit agent_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit
    ADD CONSTRAINT agent_audit_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: agent_audit_p1970_01 agent_audit_p1970_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit_p1970_01
    ADD CONSTRAINT agent_audit_p1970_01_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: agent_audit_p1970_02 agent_audit_p1970_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit_p1970_02
    ADD CONSTRAINT agent_audit_p1970_02_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: agent_audit_p1970_03 agent_audit_p1970_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit_p1970_03
    ADD CONSTRAINT agent_audit_p1970_03_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: chat_history chat_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history
    ADD CONSTRAINT chat_history_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: chat_history_p1970_01 chat_history_p1970_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history_p1970_01
    ADD CONSTRAINT chat_history_p1970_01_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: chat_history_p1970_02 chat_history_p1970_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history_p1970_02
    ADD CONSTRAINT chat_history_p1970_02_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: chat_history_p1970_03 chat_history_p1970_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history_p1970_03
    ADD CONSTRAINT chat_history_p1970_03_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: llm_requests llm_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests
    ADD CONSTRAINT llm_requests_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: llm_requests_p1970_01 llm_requests_p1970_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests_p1970_01
    ADD CONSTRAINT llm_requests_p1970_01_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: llm_requests_p1970_02 llm_requests_p1970_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests_p1970_02
    ADD CONSTRAINT llm_requests_p1970_02_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: llm_requests_p1970_03 llm_requests_p1970_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests_p1970_03
    ADD CONSTRAINT llm_requests_p1970_03_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (filename);


--
-- Name: usage_events usage_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events
    ADD CONSTRAINT usage_events_pkey PRIMARY KEY (id, ts);


--
-- Name: usage_events_p1970_01 usage_events_p1970_01_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events_p1970_01
    ADD CONSTRAINT usage_events_p1970_01_pkey PRIMARY KEY (id, ts);


--
-- Name: usage_events_p1970_02 usage_events_p1970_02_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events_p1970_02
    ADD CONSTRAINT usage_events_p1970_02_pkey PRIMARY KEY (id, ts);


--
-- Name: usage_events_p1970_03 usage_events_p1970_03_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events_p1970_03
    ADD CONSTRAINT usage_events_p1970_03_pkey PRIMARY KEY (id, ts);


--
-- Name: usage_rollup_dirty_days usage_rollup_dirty_days_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rollup_dirty_days
    ADD CONSTRAINT usage_rollup_dirty_days_pkey PRIMARY KEY (day);


--
-- Name: agent_audit_job_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_job_id_idx ON ONLY public.agent_audit USING btree (job_id, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_job_step_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_job_step_idx ON ONLY public.agent_audit USING btree (job_id, step_type, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p1970_01_job_id_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p1970_01_job_id_id_idx ON public.agent_audit_p1970_01 USING btree (job_id, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p1970_01_job_id_step_type_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p1970_01_job_id_step_type_id_idx ON public.agent_audit_p1970_01 USING btree (job_id, step_type, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_pre_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_pre_id_idx ON ONLY public.agent_audit USING btree (pre_id) WHERE (event_phase = 'post'::text);


--
-- Name: agent_audit_p1970_01_pre_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p1970_01_pre_id_idx ON public.agent_audit_p1970_01 USING btree (pre_id) WHERE (event_phase = 'post'::text);


--
-- Name: agent_audit_p1970_02_job_id_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p1970_02_job_id_id_idx ON public.agent_audit_p1970_02 USING btree (job_id, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p1970_02_job_id_step_type_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p1970_02_job_id_step_type_id_idx ON public.agent_audit_p1970_02 USING btree (job_id, step_type, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p1970_02_pre_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p1970_02_pre_id_idx ON public.agent_audit_p1970_02 USING btree (pre_id) WHERE (event_phase = 'post'::text);


--
-- Name: agent_audit_p1970_03_job_id_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p1970_03_job_id_id_idx ON public.agent_audit_p1970_03 USING btree (job_id, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p1970_03_job_id_step_type_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p1970_03_job_id_step_type_id_idx ON public.agent_audit_p1970_03 USING btree (job_id, step_type, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p1970_03_pre_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p1970_03_pre_id_idx ON public.agent_audit_p1970_03 USING btree (pre_id) WHERE (event_phase = 'post'::text);


--
-- Name: chat_history_job_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chat_history_job_ts_idx ON ONLY public.chat_history USING btree (job_id, "timestamp");


--
-- Name: chat_history_p1970_01_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chat_history_p1970_01_job_id_timestamp_idx ON public.chat_history_p1970_01 USING btree (job_id, "timestamp");


--
-- Name: chat_history_p1970_02_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chat_history_p1970_02_job_id_timestamp_idx ON public.chat_history_p1970_02 USING btree (job_id, "timestamp");


--
-- Name: chat_history_p1970_03_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chat_history_p1970_03_job_id_timestamp_idx ON public.chat_history_p1970_03 USING btree (job_id, "timestamp");


--
-- Name: llm_requests_job_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_requests_job_ts_idx ON ONLY public.llm_requests USING btree (job_id, "timestamp");


--
-- Name: llm_requests_p1970_01_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_requests_p1970_01_job_id_timestamp_idx ON public.llm_requests_p1970_01 USING btree (job_id, "timestamp");


--
-- Name: llm_requests_p1970_02_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_requests_p1970_02_job_id_timestamp_idx ON public.llm_requests_p1970_02 USING btree (job_id, "timestamp");


--
-- Name: llm_requests_p1970_03_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_requests_p1970_03_job_id_timestamp_idx ON public.llm_requests_p1970_03 USING btree (job_id, "timestamp");


--
-- Name: schema_migrations_dirty_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX schema_migrations_dirty_idx ON public.schema_migrations USING btree (filename) WHERE (success = false);


--
-- Name: usage_events_dedupe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX usage_events_dedupe_idx ON ONLY public.usage_events USING btree (source, source_id, unit, ts);


--
-- Name: usage_events_project_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_project_ts_idx ON ONLY public.usage_events USING btree (project_id, ts);


--
-- Name: usage_events_p1970_01_project_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p1970_01_project_id_ts_idx ON public.usage_events_p1970_01 USING btree (project_id, ts);


--
-- Name: usage_events_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_ref_idx ON ONLY public.usage_events USING btree (ref_id, ts);


--
-- Name: usage_events_p1970_01_ref_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p1970_01_ref_id_ts_idx ON public.usage_events_p1970_01 USING btree (ref_id, ts);


--
-- Name: usage_events_p1970_01_source_source_id_unit_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX usage_events_p1970_01_source_source_id_unit_ts_idx ON public.usage_events_p1970_01 USING btree (source, source_id, unit, ts);


--
-- Name: usage_events_user_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_user_ts_idx ON ONLY public.usage_events USING btree (user_id, ts);


--
-- Name: usage_events_p1970_01_user_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p1970_01_user_id_ts_idx ON public.usage_events_p1970_01 USING btree (user_id, ts);


--
-- Name: usage_events_p1970_02_project_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p1970_02_project_id_ts_idx ON public.usage_events_p1970_02 USING btree (project_id, ts);


--
-- Name: usage_events_p1970_02_ref_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p1970_02_ref_id_ts_idx ON public.usage_events_p1970_02 USING btree (ref_id, ts);


--
-- Name: usage_events_p1970_02_source_source_id_unit_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX usage_events_p1970_02_source_source_id_unit_ts_idx ON public.usage_events_p1970_02 USING btree (source, source_id, unit, ts);


--
-- Name: usage_events_p1970_02_user_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p1970_02_user_id_ts_idx ON public.usage_events_p1970_02 USING btree (user_id, ts);


--
-- Name: usage_events_p1970_03_project_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p1970_03_project_id_ts_idx ON public.usage_events_p1970_03 USING btree (project_id, ts);


--
-- Name: usage_events_p1970_03_ref_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p1970_03_ref_id_ts_idx ON public.usage_events_p1970_03 USING btree (ref_id, ts);


--
-- Name: usage_events_p1970_03_source_source_id_unit_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX usage_events_p1970_03_source_source_id_unit_ts_idx ON public.usage_events_p1970_03 USING btree (source, source_id, unit, ts);


--
-- Name: usage_events_p1970_03_user_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p1970_03_user_id_ts_idx ON public.usage_events_p1970_03 USING btree (user_id, ts);


--
-- Name: agent_audit_p1970_01_job_id_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_id_idx ATTACH PARTITION public.agent_audit_p1970_01_job_id_id_idx;


--
-- Name: agent_audit_p1970_01_job_id_step_type_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_step_idx ATTACH PARTITION public.agent_audit_p1970_01_job_id_step_type_id_idx;


--
-- Name: agent_audit_p1970_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pkey ATTACH PARTITION public.agent_audit_p1970_01_pkey;


--
-- Name: agent_audit_p1970_01_pre_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pre_id_idx ATTACH PARTITION public.agent_audit_p1970_01_pre_id_idx;


--
-- Name: agent_audit_p1970_02_job_id_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_id_idx ATTACH PARTITION public.agent_audit_p1970_02_job_id_id_idx;


--
-- Name: agent_audit_p1970_02_job_id_step_type_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_step_idx ATTACH PARTITION public.agent_audit_p1970_02_job_id_step_type_id_idx;


--
-- Name: agent_audit_p1970_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pkey ATTACH PARTITION public.agent_audit_p1970_02_pkey;


--
-- Name: agent_audit_p1970_02_pre_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pre_id_idx ATTACH PARTITION public.agent_audit_p1970_02_pre_id_idx;


--
-- Name: agent_audit_p1970_03_job_id_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_id_idx ATTACH PARTITION public.agent_audit_p1970_03_job_id_id_idx;


--
-- Name: agent_audit_p1970_03_job_id_step_type_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_step_idx ATTACH PARTITION public.agent_audit_p1970_03_job_id_step_type_id_idx;


--
-- Name: agent_audit_p1970_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pkey ATTACH PARTITION public.agent_audit_p1970_03_pkey;


--
-- Name: agent_audit_p1970_03_pre_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pre_id_idx ATTACH PARTITION public.agent_audit_p1970_03_pre_id_idx;


--
-- Name: chat_history_p1970_01_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_job_ts_idx ATTACH PARTITION public.chat_history_p1970_01_job_id_timestamp_idx;


--
-- Name: chat_history_p1970_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_pkey ATTACH PARTITION public.chat_history_p1970_01_pkey;


--
-- Name: chat_history_p1970_02_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_job_ts_idx ATTACH PARTITION public.chat_history_p1970_02_job_id_timestamp_idx;


--
-- Name: chat_history_p1970_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_pkey ATTACH PARTITION public.chat_history_p1970_02_pkey;


--
-- Name: chat_history_p1970_03_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_job_ts_idx ATTACH PARTITION public.chat_history_p1970_03_job_id_timestamp_idx;


--
-- Name: chat_history_p1970_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_pkey ATTACH PARTITION public.chat_history_p1970_03_pkey;


--
-- Name: llm_requests_p1970_01_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_job_ts_idx ATTACH PARTITION public.llm_requests_p1970_01_job_id_timestamp_idx;


--
-- Name: llm_requests_p1970_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_pkey ATTACH PARTITION public.llm_requests_p1970_01_pkey;


--
-- Name: llm_requests_p1970_02_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_job_ts_idx ATTACH PARTITION public.llm_requests_p1970_02_job_id_timestamp_idx;


--
-- Name: llm_requests_p1970_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_pkey ATTACH PARTITION public.llm_requests_p1970_02_pkey;


--
-- Name: llm_requests_p1970_03_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_job_ts_idx ATTACH PARTITION public.llm_requests_p1970_03_job_id_timestamp_idx;


--
-- Name: llm_requests_p1970_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_pkey ATTACH PARTITION public.llm_requests_p1970_03_pkey;


--
-- Name: usage_events_p1970_01_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_pkey ATTACH PARTITION public.usage_events_p1970_01_pkey;


--
-- Name: usage_events_p1970_01_project_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_project_ts_idx ATTACH PARTITION public.usage_events_p1970_01_project_id_ts_idx;


--
-- Name: usage_events_p1970_01_ref_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_ref_idx ATTACH PARTITION public.usage_events_p1970_01_ref_id_ts_idx;


--
-- Name: usage_events_p1970_01_source_source_id_unit_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_dedupe_idx ATTACH PARTITION public.usage_events_p1970_01_source_source_id_unit_ts_idx;


--
-- Name: usage_events_p1970_01_user_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_user_ts_idx ATTACH PARTITION public.usage_events_p1970_01_user_id_ts_idx;


--
-- Name: usage_events_p1970_02_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_pkey ATTACH PARTITION public.usage_events_p1970_02_pkey;


--
-- Name: usage_events_p1970_02_project_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_project_ts_idx ATTACH PARTITION public.usage_events_p1970_02_project_id_ts_idx;


--
-- Name: usage_events_p1970_02_ref_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_ref_idx ATTACH PARTITION public.usage_events_p1970_02_ref_id_ts_idx;


--
-- Name: usage_events_p1970_02_source_source_id_unit_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_dedupe_idx ATTACH PARTITION public.usage_events_p1970_02_source_source_id_unit_ts_idx;


--
-- Name: usage_events_p1970_02_user_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_user_ts_idx ATTACH PARTITION public.usage_events_p1970_02_user_id_ts_idx;


--
-- Name: usage_events_p1970_03_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_pkey ATTACH PARTITION public.usage_events_p1970_03_pkey;


--
-- Name: usage_events_p1970_03_project_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_project_ts_idx ATTACH PARTITION public.usage_events_p1970_03_project_id_ts_idx;


--
-- Name: usage_events_p1970_03_ref_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_ref_idx ATTACH PARTITION public.usage_events_p1970_03_ref_id_ts_idx;


--
-- Name: usage_events_p1970_03_source_source_id_unit_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_dedupe_idx ATTACH PARTITION public.usage_events_p1970_03_source_source_id_unit_ts_idx;


--
-- Name: usage_events_p1970_03_user_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_user_ts_idx ATTACH PARTITION public.usage_events_p1970_03_user_id_ts_idx;


--
-- Name: usage_events usage_events_append_only_v2; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER usage_events_append_only_v2 BEFORE DELETE OR UPDATE ON public.usage_events FOR EACH ROW EXECUTE FUNCTION public.reject_usage_event_mutation_v2();


--
-- Name: usage_events usage_events_rollup_dirty_days; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER usage_events_rollup_dirty_days AFTER INSERT ON public.usage_events REFERENCING NEW TABLE AS inserted_usage_events FOR EACH STATEMENT EXECUTE FUNCTION public.mark_usage_rollup_dirty_days_v2();


--
-- PostgreSQL database dump complete
--
