-- =============================================================================
-- GENERATED FILE — DO NOT EDIT BY HAND.
--
-- Canonical current schema for the 'audit' database, produced by replaying every
-- migration under orchestrator/database/migrations/audit/ from zero into a
-- throwaway container and dumping the result.
--
-- Source of truth : orchestrator/database/migrations/audit/*.sql
-- Regenerate      : scripts/schema-snapshot.sh audit
-- CI enforces that this file matches a fresh regeneration (db-migrations.yml).
-- The frozen orchestrator/database/{schema,vector_schema}.sql snapshots are a
-- separate, historical concern; THIS file tracks the live migration chain.
--
-- Runtime-only objects NOT present here (created outside the migration runner):
--   * Monthly audit partition children beyond the migration-seeded ones —
--     created at runtime by services/audit_partitions.py
--     (CREATE TABLE ... (LIKE parent)) as time advances.
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
-- Name: agent_audit_p2026_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_audit_p2026_07 (
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
ALTER TABLE ONLY public.agent_audit_p2026_07 ALTER COLUMN payload SET COMPRESSION lz4;
ALTER TABLE ONLY public.agent_audit_p2026_07 ALTER COLUMN metadata SET COMPRESSION lz4;


--
-- Name: agent_audit_p2026_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_audit_p2026_08 (
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
ALTER TABLE ONLY public.agent_audit_p2026_08 ALTER COLUMN payload SET COMPRESSION lz4;
ALTER TABLE ONLY public.agent_audit_p2026_08 ALTER COLUMN metadata SET COMPRESSION lz4;


--
-- Name: agent_audit_p2026_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_audit_p2026_09 (
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
ALTER TABLE ONLY public.agent_audit_p2026_09 ALTER COLUMN payload SET COMPRESSION lz4;
ALTER TABLE ONLY public.agent_audit_p2026_09 ALTER COLUMN metadata SET COMPRESSION lz4;


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
-- Name: chat_history_p2026_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_history_p2026_07 (
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
ALTER TABLE ONLY public.chat_history_p2026_07 ALTER COLUMN inputs SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p2026_07 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p2026_07 ALTER COLUMN reasoning SET COMPRESSION lz4;


--
-- Name: chat_history_p2026_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_history_p2026_08 (
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
ALTER TABLE ONLY public.chat_history_p2026_08 ALTER COLUMN inputs SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p2026_08 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p2026_08 ALTER COLUMN reasoning SET COMPRESSION lz4;


--
-- Name: chat_history_p2026_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_history_p2026_09 (
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
ALTER TABLE ONLY public.chat_history_p2026_09 ALTER COLUMN inputs SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p2026_09 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.chat_history_p2026_09 ALTER COLUMN reasoning SET COMPRESSION lz4;


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
-- Name: llm_requests_p2026_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_requests_p2026_07 (
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
ALTER TABLE ONLY public.llm_requests_p2026_07 ALTER COLUMN request SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_07 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_07 ALTER COLUMN metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_07 ALTER COLUMN auxiliary_metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_07 ALTER COLUMN metrics SET COMPRESSION lz4;


--
-- Name: llm_requests_p2026_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_requests_p2026_08 (
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
ALTER TABLE ONLY public.llm_requests_p2026_08 ALTER COLUMN request SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_08 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_08 ALTER COLUMN metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_08 ALTER COLUMN auxiliary_metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_08 ALTER COLUMN metrics SET COMPRESSION lz4;


--
-- Name: llm_requests_p2026_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_requests_p2026_09 (
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
ALTER TABLE ONLY public.llm_requests_p2026_09 ALTER COLUMN request SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_09 ALTER COLUMN response SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_09 ALTER COLUMN metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_09 ALTER COLUMN auxiliary_metadata SET COMPRESSION lz4;
ALTER TABLE ONLY public.llm_requests_p2026_09 ALTER COLUMN metrics SET COMPRESSION lz4;


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
    details jsonb DEFAULT '{}'::jsonb NOT NULL
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
-- Name: usage_events_p2026_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_events_p2026_07 (
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
    details jsonb DEFAULT '{}'::jsonb NOT NULL
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.usage_events_p2026_07 ALTER COLUMN details SET COMPRESSION lz4;


--
-- Name: usage_events_p2026_08; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_events_p2026_08 (
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
    details jsonb DEFAULT '{}'::jsonb NOT NULL
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.usage_events_p2026_08 ALTER COLUMN details SET COMPRESSION lz4;


--
-- Name: usage_events_p2026_09; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_events_p2026_09 (
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
    details jsonb DEFAULT '{}'::jsonb NOT NULL
)
WITH (fillfactor='100', autovacuum_vacuum_insert_scale_factor='0.05', autovacuum_vacuum_insert_threshold='10000', autovacuum_analyze_scale_factor='0.02', autovacuum_freeze_min_age='0');
ALTER TABLE ONLY public.usage_events_p2026_09 ALTER COLUMN details SET COMPRESSION lz4;


--
-- Name: agent_audit_p2026_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit ATTACH PARTITION public.agent_audit_p2026_07 FOR VALUES FROM ('2026-07-01 00:00:00+00') TO ('2026-08-01 00:00:00+00');


--
-- Name: agent_audit_p2026_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit ATTACH PARTITION public.agent_audit_p2026_08 FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00');


--
-- Name: agent_audit_p2026_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit ATTACH PARTITION public.agent_audit_p2026_09 FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');


--
-- Name: chat_history_p2026_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history ATTACH PARTITION public.chat_history_p2026_07 FOR VALUES FROM ('2026-07-01 00:00:00+00') TO ('2026-08-01 00:00:00+00');


--
-- Name: chat_history_p2026_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history ATTACH PARTITION public.chat_history_p2026_08 FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00');


--
-- Name: chat_history_p2026_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history ATTACH PARTITION public.chat_history_p2026_09 FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');


--
-- Name: llm_requests_p2026_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests ATTACH PARTITION public.llm_requests_p2026_07 FOR VALUES FROM ('2026-07-01 00:00:00+00') TO ('2026-08-01 00:00:00+00');


--
-- Name: llm_requests_p2026_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests ATTACH PARTITION public.llm_requests_p2026_08 FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00');


--
-- Name: llm_requests_p2026_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests ATTACH PARTITION public.llm_requests_p2026_09 FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');


--
-- Name: usage_events_p2026_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events ATTACH PARTITION public.usage_events_p2026_07 FOR VALUES FROM ('2026-07-01 00:00:00+00') TO ('2026-08-01 00:00:00+00');


--
-- Name: usage_events_p2026_08; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events ATTACH PARTITION public.usage_events_p2026_08 FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00');


--
-- Name: usage_events_p2026_09; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events ATTACH PARTITION public.usage_events_p2026_09 FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');


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
-- Name: agent_audit_p2026_07 agent_audit_p2026_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit_p2026_07
    ADD CONSTRAINT agent_audit_p2026_07_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: agent_audit_p2026_08 agent_audit_p2026_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit_p2026_08
    ADD CONSTRAINT agent_audit_p2026_08_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: agent_audit_p2026_09 agent_audit_p2026_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit_p2026_09
    ADD CONSTRAINT agent_audit_p2026_09_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: chat_history chat_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history
    ADD CONSTRAINT chat_history_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: chat_history_p2026_07 chat_history_p2026_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history_p2026_07
    ADD CONSTRAINT chat_history_p2026_07_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: chat_history_p2026_08 chat_history_p2026_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history_p2026_08
    ADD CONSTRAINT chat_history_p2026_08_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: chat_history_p2026_09 chat_history_p2026_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history_p2026_09
    ADD CONSTRAINT chat_history_p2026_09_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: llm_requests llm_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests
    ADD CONSTRAINT llm_requests_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: llm_requests_p2026_07 llm_requests_p2026_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests_p2026_07
    ADD CONSTRAINT llm_requests_p2026_07_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: llm_requests_p2026_08 llm_requests_p2026_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests_p2026_08
    ADD CONSTRAINT llm_requests_p2026_08_pkey PRIMARY KEY (id, "timestamp");


--
-- Name: llm_requests_p2026_09 llm_requests_p2026_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_requests_p2026_09
    ADD CONSTRAINT llm_requests_p2026_09_pkey PRIMARY KEY (id, "timestamp");


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
-- Name: usage_events_p2026_07 usage_events_p2026_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events_p2026_07
    ADD CONSTRAINT usage_events_p2026_07_pkey PRIMARY KEY (id, ts);


--
-- Name: usage_events_p2026_08 usage_events_p2026_08_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events_p2026_08
    ADD CONSTRAINT usage_events_p2026_08_pkey PRIMARY KEY (id, ts);


--
-- Name: usage_events_p2026_09 usage_events_p2026_09_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events_p2026_09
    ADD CONSTRAINT usage_events_p2026_09_pkey PRIMARY KEY (id, ts);


--
-- Name: agent_audit_job_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_job_id_idx ON ONLY public.agent_audit USING btree (job_id, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_job_step_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_job_step_idx ON ONLY public.agent_audit USING btree (job_id, step_type, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p2026_07_job_id_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p2026_07_job_id_id_idx ON public.agent_audit_p2026_07 USING btree (job_id, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p2026_07_job_id_step_type_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p2026_07_job_id_step_type_id_idx ON public.agent_audit_p2026_07 USING btree (job_id, step_type, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_pre_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_pre_id_idx ON ONLY public.agent_audit USING btree (pre_id) WHERE (event_phase = 'post'::text);


--
-- Name: agent_audit_p2026_07_pre_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p2026_07_pre_id_idx ON public.agent_audit_p2026_07 USING btree (pre_id) WHERE (event_phase = 'post'::text);


--
-- Name: agent_audit_p2026_08_job_id_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p2026_08_job_id_id_idx ON public.agent_audit_p2026_08 USING btree (job_id, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p2026_08_job_id_step_type_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p2026_08_job_id_step_type_id_idx ON public.agent_audit_p2026_08 USING btree (job_id, step_type, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p2026_08_pre_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p2026_08_pre_id_idx ON public.agent_audit_p2026_08 USING btree (pre_id) WHERE (event_phase = 'post'::text);


--
-- Name: agent_audit_p2026_09_job_id_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p2026_09_job_id_id_idx ON public.agent_audit_p2026_09 USING btree (job_id, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p2026_09_job_id_step_type_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p2026_09_job_id_step_type_id_idx ON public.agent_audit_p2026_09 USING btree (job_id, step_type, id) WHERE (event_phase = 'pre'::text);


--
-- Name: agent_audit_p2026_09_pre_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_audit_p2026_09_pre_id_idx ON public.agent_audit_p2026_09 USING btree (pre_id) WHERE (event_phase = 'post'::text);


--
-- Name: chat_history_job_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chat_history_job_ts_idx ON ONLY public.chat_history USING btree (job_id, "timestamp");


--
-- Name: chat_history_p2026_07_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chat_history_p2026_07_job_id_timestamp_idx ON public.chat_history_p2026_07 USING btree (job_id, "timestamp");


--
-- Name: chat_history_p2026_08_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chat_history_p2026_08_job_id_timestamp_idx ON public.chat_history_p2026_08 USING btree (job_id, "timestamp");


--
-- Name: chat_history_p2026_09_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chat_history_p2026_09_job_id_timestamp_idx ON public.chat_history_p2026_09 USING btree (job_id, "timestamp");


--
-- Name: llm_requests_job_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_requests_job_ts_idx ON ONLY public.llm_requests USING btree (job_id, "timestamp");


--
-- Name: llm_requests_p2026_07_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_requests_p2026_07_job_id_timestamp_idx ON public.llm_requests_p2026_07 USING btree (job_id, "timestamp");


--
-- Name: llm_requests_p2026_08_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_requests_p2026_08_job_id_timestamp_idx ON public.llm_requests_p2026_08 USING btree (job_id, "timestamp");


--
-- Name: llm_requests_p2026_09_job_id_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_requests_p2026_09_job_id_timestamp_idx ON public.llm_requests_p2026_09 USING btree (job_id, "timestamp");


--
-- Name: schema_migrations_dirty_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX schema_migrations_dirty_idx ON public.schema_migrations USING btree (filename) WHERE (success = false);


--
-- Name: usage_events_dedupe_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX usage_events_dedupe_idx ON ONLY public.usage_events USING btree (source, source_id, unit, ts);


--
-- Name: usage_events_ref_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_ref_idx ON ONLY public.usage_events USING btree (ref_id, ts);


--
-- Name: usage_events_p2026_07_ref_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p2026_07_ref_id_ts_idx ON public.usage_events_p2026_07 USING btree (ref_id, ts);


--
-- Name: usage_events_p2026_07_source_source_id_unit_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX usage_events_p2026_07_source_source_id_unit_ts_idx ON public.usage_events_p2026_07 USING btree (source, source_id, unit, ts);


--
-- Name: usage_events_user_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_user_ts_idx ON ONLY public.usage_events USING btree (user_id, ts);


--
-- Name: usage_events_p2026_07_user_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p2026_07_user_id_ts_idx ON public.usage_events_p2026_07 USING btree (user_id, ts);


--
-- Name: usage_events_p2026_08_ref_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p2026_08_ref_id_ts_idx ON public.usage_events_p2026_08 USING btree (ref_id, ts);


--
-- Name: usage_events_p2026_08_source_source_id_unit_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX usage_events_p2026_08_source_source_id_unit_ts_idx ON public.usage_events_p2026_08 USING btree (source, source_id, unit, ts);


--
-- Name: usage_events_p2026_08_user_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p2026_08_user_id_ts_idx ON public.usage_events_p2026_08 USING btree (user_id, ts);


--
-- Name: usage_events_p2026_09_ref_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p2026_09_ref_id_ts_idx ON public.usage_events_p2026_09 USING btree (ref_id, ts);


--
-- Name: usage_events_p2026_09_source_source_id_unit_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX usage_events_p2026_09_source_source_id_unit_ts_idx ON public.usage_events_p2026_09 USING btree (source, source_id, unit, ts);


--
-- Name: usage_events_p2026_09_user_id_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_events_p2026_09_user_id_ts_idx ON public.usage_events_p2026_09 USING btree (user_id, ts);


--
-- Name: agent_audit_p2026_07_job_id_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_id_idx ATTACH PARTITION public.agent_audit_p2026_07_job_id_id_idx;


--
-- Name: agent_audit_p2026_07_job_id_step_type_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_step_idx ATTACH PARTITION public.agent_audit_p2026_07_job_id_step_type_id_idx;


--
-- Name: agent_audit_p2026_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pkey ATTACH PARTITION public.agent_audit_p2026_07_pkey;


--
-- Name: agent_audit_p2026_07_pre_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pre_id_idx ATTACH PARTITION public.agent_audit_p2026_07_pre_id_idx;


--
-- Name: agent_audit_p2026_08_job_id_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_id_idx ATTACH PARTITION public.agent_audit_p2026_08_job_id_id_idx;


--
-- Name: agent_audit_p2026_08_job_id_step_type_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_step_idx ATTACH PARTITION public.agent_audit_p2026_08_job_id_step_type_id_idx;


--
-- Name: agent_audit_p2026_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pkey ATTACH PARTITION public.agent_audit_p2026_08_pkey;


--
-- Name: agent_audit_p2026_08_pre_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pre_id_idx ATTACH PARTITION public.agent_audit_p2026_08_pre_id_idx;


--
-- Name: agent_audit_p2026_09_job_id_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_id_idx ATTACH PARTITION public.agent_audit_p2026_09_job_id_id_idx;


--
-- Name: agent_audit_p2026_09_job_id_step_type_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_job_step_idx ATTACH PARTITION public.agent_audit_p2026_09_job_id_step_type_id_idx;


--
-- Name: agent_audit_p2026_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pkey ATTACH PARTITION public.agent_audit_p2026_09_pkey;


--
-- Name: agent_audit_p2026_09_pre_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.agent_audit_pre_id_idx ATTACH PARTITION public.agent_audit_p2026_09_pre_id_idx;


--
-- Name: chat_history_p2026_07_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_job_ts_idx ATTACH PARTITION public.chat_history_p2026_07_job_id_timestamp_idx;


--
-- Name: chat_history_p2026_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_pkey ATTACH PARTITION public.chat_history_p2026_07_pkey;


--
-- Name: chat_history_p2026_08_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_job_ts_idx ATTACH PARTITION public.chat_history_p2026_08_job_id_timestamp_idx;


--
-- Name: chat_history_p2026_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_pkey ATTACH PARTITION public.chat_history_p2026_08_pkey;


--
-- Name: chat_history_p2026_09_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_job_ts_idx ATTACH PARTITION public.chat_history_p2026_09_job_id_timestamp_idx;


--
-- Name: chat_history_p2026_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.chat_history_pkey ATTACH PARTITION public.chat_history_p2026_09_pkey;


--
-- Name: llm_requests_p2026_07_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_job_ts_idx ATTACH PARTITION public.llm_requests_p2026_07_job_id_timestamp_idx;


--
-- Name: llm_requests_p2026_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_pkey ATTACH PARTITION public.llm_requests_p2026_07_pkey;


--
-- Name: llm_requests_p2026_08_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_job_ts_idx ATTACH PARTITION public.llm_requests_p2026_08_job_id_timestamp_idx;


--
-- Name: llm_requests_p2026_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_pkey ATTACH PARTITION public.llm_requests_p2026_08_pkey;


--
-- Name: llm_requests_p2026_09_job_id_timestamp_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_job_ts_idx ATTACH PARTITION public.llm_requests_p2026_09_job_id_timestamp_idx;


--
-- Name: llm_requests_p2026_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.llm_requests_pkey ATTACH PARTITION public.llm_requests_p2026_09_pkey;


--
-- Name: usage_events_p2026_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_pkey ATTACH PARTITION public.usage_events_p2026_07_pkey;


--
-- Name: usage_events_p2026_07_ref_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_ref_idx ATTACH PARTITION public.usage_events_p2026_07_ref_id_ts_idx;


--
-- Name: usage_events_p2026_07_source_source_id_unit_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_dedupe_idx ATTACH PARTITION public.usage_events_p2026_07_source_source_id_unit_ts_idx;


--
-- Name: usage_events_p2026_07_user_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_user_ts_idx ATTACH PARTITION public.usage_events_p2026_07_user_id_ts_idx;


--
-- Name: usage_events_p2026_08_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_pkey ATTACH PARTITION public.usage_events_p2026_08_pkey;


--
-- Name: usage_events_p2026_08_ref_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_ref_idx ATTACH PARTITION public.usage_events_p2026_08_ref_id_ts_idx;


--
-- Name: usage_events_p2026_08_source_source_id_unit_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_dedupe_idx ATTACH PARTITION public.usage_events_p2026_08_source_source_id_unit_ts_idx;


--
-- Name: usage_events_p2026_08_user_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_user_ts_idx ATTACH PARTITION public.usage_events_p2026_08_user_id_ts_idx;


--
-- Name: usage_events_p2026_09_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_pkey ATTACH PARTITION public.usage_events_p2026_09_pkey;


--
-- Name: usage_events_p2026_09_ref_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_ref_idx ATTACH PARTITION public.usage_events_p2026_09_ref_id_ts_idx;


--
-- Name: usage_events_p2026_09_source_source_id_unit_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_dedupe_idx ATTACH PARTITION public.usage_events_p2026_09_source_source_id_unit_ts_idx;


--
-- Name: usage_events_p2026_09_user_id_ts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.usage_events_user_ts_idx ATTACH PARTITION public.usage_events_p2026_09_user_id_ts_idx;


--
-- PostgreSQL database dump complete
--
