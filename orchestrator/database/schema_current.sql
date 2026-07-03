-- =============================================================================
-- GENERATED FILE — DO NOT EDIT BY HAND.
--
-- Canonical current schema for the 'app' database, produced by replaying every
-- migration under orchestrator/database/migrations/app/ from zero into a
-- throwaway container and dumping the result.
--
-- Source of truth : orchestrator/database/migrations/app/*.sql
-- Regenerate      : scripts/schema-snapshot.sh app
-- CI enforces that this file matches a fresh regeneration (db-migrations.yml).
-- The frozen orchestrator/database/{schema,vector_schema}.sql snapshots are a
-- separate, historical concern; THIS file tracks the live migration chain.
--
-- Runtime-only objects NOT present here (created outside the migration runner):
--   * LangGraph checkpointer tables (checkpoints, checkpoint_blobs,
--     checkpoint_writes, checkpoint_migrations) — created by
--     AsyncPostgresSaver.setup() (src/agent.py) only under
--     CHECKPOINTER_BACKEND=postgres, in the control-plane DB by default.
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
-- Name: sudo_request_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.sudo_request_status AS ENUM (
    'pending',
    'approved',
    'denied',
    'expired',
    'auto_approved',
    'auto_denied'
);


--
-- Name: notify_thread_permission_update(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.notify_thread_permission_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.status <> OLD.status THEN
        PERFORM pg_notify(
            'thread_permission_updates',
            json_build_object(
                'id', NEW.id,
                'thread_id', NEW.thread_id,
                'status', NEW.status
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: update_updated_at_column(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_updated_at_column() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;


SET default_table_access_method = heap;

--
-- Name: agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agents (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    config_name character varying(100) NOT NULL,
    hostname character varying(255),
    pod_ip character varying(45),
    pod_port integer DEFAULT 8001,
    pid integer,
    status character varying(20) DEFAULT 'booting'::character varying NOT NULL,
    current_job_id uuid,
    registered_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    last_completed_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb,
    agent_mode character varying(20) DEFAULT 'worker'::character varying NOT NULL,
    thread_id uuid,
    intents jsonb DEFAULT '{}'::jsonb NOT NULL,
    pod_uid text,
    aux_degraded boolean DEFAULT false NOT NULL,
    CONSTRAINT valid_agent_status CHECK (((status)::text = ANY ((ARRAY['booting'::character varying, 'ready'::character varying, 'working'::character varying, 'session'::character varying, 'draining'::character varying, 'completed'::character varying, 'failed'::character varying, 'offline'::character varying])::text[])))
);


--
-- Name: COLUMN agents.intents; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.intents IS 'Orchestrator-set drain/upgrade intents. Read by the heartbeat response and reconciler; never written by the agent.';


--
-- Name: COLUMN agents.pod_uid; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.pod_uid IS 'K8s-assigned metadata.uid of the agent pod, self-reported via the Kubernetes downward API. Used by the session router to set ownerReferences on per-session Service/Ingress resources so K8s GC tears them down when the pod is deleted. NULL outside of K8s.';


--
-- Name: COLUMN agents.aux_degraded; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.aux_degraded IS 'Latest heartbeat-reported auxiliary-model health: TRUE while the agent''s auxiliary LLM (memory extraction/curation/assembly, session titles) is sustained-failing. Set from metrics.aux.degraded; detail in metadata.aux. See docs/issues/surface_silent_aux_failures.md (aux Phase 2).';


--
-- Name: auth_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_tokens (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    user_id uuid NOT NULL,
    name text NOT NULL,
    token_hash text NOT NULL,
    token_prefix character varying(12) NOT NULL,
    scope text DEFAULT 'user'::text NOT NULL,
    origin text,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    kind text NOT NULL,
    scopes text[],
    last_four character(4),
    last_used_ip inet,
    superseded_by uuid,
    CONSTRAINT auth_tokens_kind_check CHECK ((kind = ANY (ARRAY['mcp'::text, 'api'::text])))
);


--
-- Name: TABLE auth_tokens; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.auth_tokens IS 'Bearer-auth tokens: kind=mcp (legacy Claude Code/CLI flow) and kind=api (PATs for n8n/automation). See docs/features/auth_bff_and_api_tokens.md §3.';


--
-- Name: COLUMN auth_tokens.kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.auth_tokens.kind IS 'mcp=legacy MCP token (srw_<32-byte> prefix; scope column carries user/all/project:<uuid>). api=PAT (ak_<43-char> prefix; scopes column carries jobs:read / chat:write / etc).';


--
-- Name: COLUMN auth_tokens.scopes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.auth_tokens.scopes IS 'Action-level scopes for kind=api rows. NULL for kind=mcp. Validator currently runs permissive — any scope grants any endpoint until PR 4 wires per-endpoint @require_scope decorators.';


--
-- Name: COLUMN auth_tokens.last_four; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.auth_tokens.last_four IS 'Last 4 chars of the plaintext token. Surfaced in UI as ak_…vC2 / srw_…vC2 hints. NULL on rows created before this migration.';


--
-- Name: COLUMN auth_tokens.superseded_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.auth_tokens.superseded_by IS 'Set by /rotate. Old row keeps validating for a 24h grace window so an automation can roll over without an outage; cleanup loop revokes.';


--
-- Name: automations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.automations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    owner_id uuid NOT NULL,
    project_id uuid,
    name text NOT NULL,
    description text,
    trigger_type text NOT NULL,
    cron_expr text,
    timezone text DEFAULT 'UTC'::text NOT NULL,
    catchup_window_seconds integer DEFAULT 86400 NOT NULL,
    event_filter jsonb,
    enabled boolean DEFAULT true NOT NULL,
    expert text NOT NULL,
    prompt text NOT NULL,
    config_override jsonb DEFAULT '{}'::jsonb NOT NULL,
    autonomy text DEFAULT 'review'::text NOT NULL,
    priority integer DEFAULT 5 NOT NULL,
    max_chain_depth integer DEFAULT 10 NOT NULL,
    max_fires_per_day integer DEFAULT 100 NOT NULL,
    fires_today_count integer DEFAULT 0 NOT NULL,
    fires_today_date date,
    next_run_at timestamp with time zone,
    last_scheduled_at timestamp with time zone,
    last_dispatched_at timestamp with time zone,
    last_fired_at timestamp with time zone,
    last_job_id uuid,
    last_status text,
    run_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT automations_trigger_type_check CHECK ((trigger_type = ANY (ARRAY['cron'::text, 'event'::text]))),
    CONSTRAINT cron_trigger_has_expr CHECK (((trigger_type <> 'cron'::text) OR (cron_expr IS NOT NULL))),
    CONSTRAINT event_trigger_has_filter CHECK (((trigger_type <> 'event'::text) OR (event_filter IS NOT NULL)))
);


--
-- Name: TABLE automations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.automations IS 'Job templates fired by the cron_dispatcher (trigger_type=cron) or event_dispatcher (trigger_type=event, v0.5+). One row = one rule of the form "when X happens, create job Y". Design: docs/features/automations_v0.md.';


--
-- Name: COLUMN automations.timezone; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.automations.timezone IS 'IANA timezone name (e.g. ''Europe/Berlin''), not a UTC offset. zoneinfo resolves DST at fire time; storing an offset would silently mis-fire across DST transitions.';


--
-- Name: COLUMN automations.catchup_window_seconds; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.automations.catchup_window_seconds IS 'Grace window for missed cron fires when the orchestrator was down. If the next due fire is older than this many seconds, the dispatcher skips it and advances to the next future tick rather than back-filling a stale run. Default 24h; intentionally generous so a weekly Monday automation still fires after an 18h overnight outage.';


--
-- Name: COLUMN automations.event_filter; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.automations.event_filter IS 'JSONB filter for v0.5 event triggers. Unused in v0 (NULL for all rows). Shape: {event_type, expert?, tags_any?, tags_all?, min_priority?, parent_automation_id?}. See docs/features/automations.md §Event Triggers.';


--
-- Name: COLUMN automations.max_fires_per_day; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.automations.max_fires_per_day IS 'Soft rate cap. fires_today_count is incremented at fire time; over-cap fires are dropped and the automation is auto-disabled with a notification to the owner. The fires_today_date column rolls the counter daily.';


--
-- Name: COLUMN automations.last_scheduled_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.automations.last_scheduled_at IS 'Cron-canonical time of the last fire (what the expression said). Compare against last_dispatched_at to detect scheduler drift.';


--
-- Name: COLUMN automations.last_dispatched_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.automations.last_dispatched_at IS 'Wall-clock time the dispatcher actually fired the last run. Usually within seconds of last_scheduled_at; large drift = orchestrator was down or the dispatcher was lagging.';


--
-- Name: capability_grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.capability_grants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    scope_kind text NOT NULL,
    scope_id uuid,
    key text NOT NULL,
    value_json jsonb NOT NULL,
    granted_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT capability_grants_scope_kind_check CHECK ((scope_kind = ANY (ARRAY['user'::text, 'project'::text, 'global'::text])))
);


--
-- Name: TABLE capability_grants; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.capability_grants IS 'Scoped capability entitlements (Slice 2). user>project>global>default, restrict-only, deny-by-default. Deleting a user/project must delete its grant rows in app code — no cascade fires.';


--
-- Name: config_overrides; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.config_overrides (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    family character varying(64),
    kind character varying(32) NOT NULL,
    name character varying(128) NOT NULL,
    content text,
    content_format character varying(16) DEFAULT 'text'::character varying,
    notes text,
    created_by uuid,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    value_json jsonb,
    CONSTRAINT config_overrides_kind_check CHECK (((kind)::text = ANY ((ARRAY['prompts'::character varying, 'instructions'::character varying, 'settings'::character varying, 'guardrails'::character varying])::text[]))),
    CONSTRAINT config_overrides_payload_check CHECK (((((kind)::text = ANY ((ARRAY['prompts'::character varying, 'instructions'::character varying])::text[])) AND (content IS NOT NULL) AND (value_json IS NULL)) OR (((kind)::text = ANY ((ARRAY['settings'::character varying, 'guardrails'::character varying])::text[])) AND (value_json IS NOT NULL) AND (content IS NULL)))),
    CONSTRAINT prompt_overrides_content_format_check CHECK (((content_format)::text = ANY ((ARRAY['text'::character varying, 'markdown'::character varying, 'jinja'::character varying, 'yaml'::character varying])::text[])))
);


--
-- Name: TABLE config_overrides; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.config_overrides IS 'DB-backed overrides for the bundled config matrix (prompts, instructions, settings, guardrails). One row overrides one (family, kind, name); NULL family = global. File matrix is the immutable floor. Design: docs/superpowers/specs/2026-05-31-config-matrix-db-overrides-design.md.';


--
-- Name: COLUMN config_overrides.family; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.config_overrides.family IS 'Model family (family_of(model)); NULL applies to all families. Matched against MatrixResolver.model_family at resolution time.';


--
-- Name: COLUMN config_overrides.kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.config_overrides.kind IS 'Resolver subsection (MatrixResolver.MATRIX_SUBSECTION): prompts | instructions.';


--
-- Name: datasources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.datasources (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    name text NOT NULL,
    description text,
    type text NOT NULL,
    connection_url text,
    credentials jsonb DEFAULT '{}'::jsonb,
    cli_hint text,
    default_branch text,
    created_by uuid,
    is_global boolean DEFAULT false NOT NULL,
    job_id uuid,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    project_id uuid
);


--
-- Name: experts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.experts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(100) NOT NULL,
    display_name character varying(200) NOT NULL,
    description text,
    icon character varying(100) DEFAULT 'smart_toy'::character varying NOT NULL,
    color character varying(7) DEFAULT '#6B7280'::character varying NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    expert_type character varying(10) NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    prompts jsonb DEFAULT '{}'::jsonb NOT NULL,
    owner_id uuid NOT NULL,
    is_global boolean DEFAULT false NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT experts_expert_type_check CHECK (((expert_type)::text = ANY ((ARRAY['worker'::character varying, 'session'::character varying])::text[])))
);


--
-- Name: TABLE experts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.experts IS 'DB-backed user/admin experts (overlay over bundled config/experts/). config = fragment vs the expert_type base; prompts = {persona, instructions, strategic, tactical, summarization} (Part 2 — one family-agnostic version per segment; model adaptation stays in the systemprompt_<family> wrapper). Design: docs/features/global_expert_management.md.';


--
-- Name: external_contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_contacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    display_name text NOT NULL,
    email text NOT NULL,
    added_by uuid,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: job_datasources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_datasources (
    job_id uuid NOT NULL,
    datasource_id uuid NOT NULL,
    linked_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: TABLE job_datasources; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.job_datasources IS 'Explicit datasource selection for a job (the picker is the source of truth). Resolution returns only these links; replaces the legacy clone-to-job-scoped datasources.job_id mechanism.';


--
-- Name: jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jobs (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    description text NOT NULL,
    document_path text,
    document_content bytea,
    context jsonb DEFAULT '{}'::jsonb,
    status character varying(50) DEFAULT 'created'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    completed_at timestamp with time zone,
    error_message text,
    error_details jsonb,
    total_tokens_used integer DEFAULT 0,
    total_requests integer DEFAULT 0,
    config_name character varying(100) DEFAULT 'default'::character varying,
    config_override jsonb,
    resolved_config jsonb,
    assigned_agent_id uuid,
    priority integer DEFAULT 5 NOT NULL,
    user_id uuid,
    project_id uuid,
    branch_name character varying(200),
    merge_status character varying(50),
    repo_merge_statuses jsonb DEFAULT '{}'::jsonb,
    freeze_data jsonb,
    parent_job_id uuid,
    repo_name character varying(200),
    creation_order smallint,
    worktree_path character varying(500),
    delegation_context text,
    cloud_diff_baseline_commit text,
    diff_status text,
    exported_folder_handle text,
    exported_at timestamp with time zone,
    expert_id uuid,
    CONSTRAINT jobs_diff_status_check CHECK (((diff_status IS NULL) OR (diff_status = ANY (ARRAY['pending'::text, 'accepted'::text, 'rejected'::text])))),
    CONSTRAINT valid_status CHECK (((status)::text = ANY ((ARRAY['created'::character varying, 'processing'::character varying, 'completed'::character varying, 'failed'::character varying, 'cancelled'::character varying, 'pending_review'::character varying, 'paused'::character varying, 'reviewing'::character varying, 'waiting'::character varying, 'waiting_for_reply'::character varying])::text[])))
);


--
-- Name: COLUMN jobs.cloud_diff_baseline_commit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.cloud_diff_baseline_commit IS 'Gitea commit hash captured at job-start over the mounted project folder (projects/<slug>/). The agent edits the working tree in place; the diff against this baseline at job-completion is what gets written back to the cloud on accept. Set only for project-attached Mode A jobs; NULL for loose Mode B jobs.';


--
-- Name: COLUMN jobs.diff_status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.diff_status IS 'State of the project-folder diff review. NULL = no diff captured (job not finished, or no project attached, or empty diff). ''pending'' = diff captured, awaiting user accept/reject. ''accepted'' = diff applied to the cloud folder. ''rejected'' = diff discarded without write-back.';


--
-- Name: COLUMN jobs.exported_folder_handle; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.exported_folder_handle IS 'Mode B only: opaque handle of the shared cloud folder created when the user clicked "Export to shared folder" on a completed loose job. Re-export is refused while non-NULL.';


--
-- Name: COLUMN jobs.exported_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.exported_at IS 'Timestamp the Mode B shared-folder export was made. Paired with exported_folder_handle.';


--
-- Name: job_summary; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.job_summary AS
 SELECT j.id,
    j.status,
    j.config_name,
    j.assigned_agent_id,
    j.user_id,
    j.project_id,
    j.parent_job_id,
    j.priority,
    j.branch_name,
    j.repo_name,
    j.merge_status,
    j.freeze_data,
    j.created_at,
    j.completed_at,
    j.total_tokens_used,
    j.total_requests
   FROM public.jobs j;


--
-- Name: llm_endpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_endpoints (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    user_id uuid,
    label text NOT NULL,
    base_url text NOT NULL,
    api_key text,
    key_prefix character varying(12),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: magic_link_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.magic_link_tokens (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    token_hash text NOT NULL,
    purpose text NOT NULL,
    user_id uuid,
    approval_id uuid,
    thread_id uuid,
    intended_decision text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    consumed_decision text,
    CONSTRAINT magic_link_tokens_intended_decision_check CHECK (((intended_decision IS NULL) OR (intended_decision = ANY (ARRAY['approved'::text, 'denied'::text]))))
);


--
-- Name: TABLE magic_link_tokens; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.magic_link_tokens IS 'Opaque single-use tokens for email magic-links. Plaintext lives only in the email body; DB stores SHA-256 hashes. See docs/features/headless_persistent_sessions.md §4.';


--
-- Name: COLUMN magic_link_tokens.token_hash; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.magic_link_tokens.token_hash IS 'SHA-256 of the raw token bytes (hex-encoded).';


--
-- Name: COLUMN magic_link_tokens.intended_decision; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.magic_link_tokens.intended_decision IS 'For approve/deny links: the action this specific token authorizes. Some flows emit one token per decision, others ask the user to pick on the confirmation page.';


--
-- Name: COLUMN magic_link_tokens.used_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.magic_link_tokens.used_at IS 'Single-use enforcement: CAS UPDATE ... WHERE used_at IS NULL.';


--
-- Name: message_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.message_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid,
    user_id uuid,
    thread_id character varying(12) NOT NULL,
    direction character varying(10) NOT NULL,
    recipient_email text,
    subject text NOT NULL,
    message text NOT NULL,
    mode character varying(10),
    status character varying(20) NOT NULL,
    error_message text,
    created_at timestamp with time zone DEFAULT now(),
    email_message_id text,
    read_at timestamp with time zone
);


--
-- Name: models; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.models (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    provider_kind text NOT NULL,
    provider_ref text NOT NULL,
    model_id text NOT NULL,
    display_label text NOT NULL,
    capabilities text[] NOT NULL,
    family text NOT NULL,
    context_window integer,
    reasoning_level text,
    params_json jsonb,
    enabled boolean DEFAULT true NOT NULL,
    seeded_from text,
    notes text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT models_capabilities_check CHECK (((cardinality(capabilities) >= 1) AND (capabilities <@ ARRAY['chat'::text, 'auxiliary'::text, 'embedding'::text, 'vision'::text, 'whisper'::text, 'tts'::text]))),
    CONSTRAINT models_provider_kind_check CHECK ((provider_kind = ANY (ARRAY['system'::text, 'endpoint'::text])))
);


--
-- Name: notification_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_queue (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    job_id uuid,
    thread_id character varying(12),
    subject text NOT NULL,
    message text NOT NULL,
    channels jsonb DEFAULT '{}'::jsonb NOT NULL,
    queued_at timestamp with time zone DEFAULT now(),
    delivered_at timestamp with time zone
);


--
-- Name: processed_inbound_emails; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.processed_inbound_emails (
    email_message_id text NOT NULL,
    processed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE processed_inbound_emails; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.processed_inbound_emails IS 'Insert-as-claim dedup guard for the IMAP poller (HA / M1). The poller INSERTs an inbound RFC822 Message-ID here (ON CONFLICT DO NOTHING) and routes the reply only if it won the insert, so the transient dual-leader window cannot inject the same reply into a job twice. One row per processed inbound email; prunable.';


--
-- Name: project_api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_api_keys (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    project_id uuid NOT NULL,
    provider character varying(50) NOT NULL,
    api_key text NOT NULL,
    key_prefix character varying(12) NOT NULL,
    label text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT valid_project_api_key_provider CHECK (((provider)::text = ANY ((ARRAY['openai'::character varying, 'anthropic'::character varying, 'google'::character varying, 'groq'::character varying, 'openrouter'::character varying, 'mistral'::character varying, 'vision'::character varying])::text[])))
);


--
-- Name: project_datasources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_datasources (
    project_id uuid NOT NULL,
    datasource_id uuid NOT NULL,
    linked_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    read_only boolean,
    description text
);


--
-- Name: project_experts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_experts (
    project_id uuid NOT NULL,
    expert_id uuid NOT NULL,
    default_for character varying(10),
    config_override jsonb,
    linked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_experts_default_for_check CHECK (((default_for)::text = ANY ((ARRAY['worker'::character varying, 'session'::character varying])::text[])))
);


--
-- Name: project_loops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_loops (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    owner_id uuid,
    status text DEFAULT 'running'::text NOT NULL,
    goal text,
    acceptance_criteria text,
    user_prompt text,
    model text,
    role_sequence jsonb DEFAULT '["scholar", "critic", "developer"]'::jsonb NOT NULL,
    seq_index integer DEFAULT 0 NOT NULL,
    max_iterations integer,
    remaining_iterations integer,
    run_until timestamp with time zone,
    max_consecutive_failures integer DEFAULT 3 NOT NULL,
    current_job_id uuid,
    total_jobs_run integer DEFAULT 0 NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    last_error text,
    stop_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    workspace_backend text,
    CONSTRAINT project_loop_has_budget CHECK (((max_iterations IS NOT NULL) OR (run_until IS NOT NULL))),
    CONSTRAINT project_loop_role_sequence_nonempty CHECK (((jsonb_typeof(role_sequence) = 'array'::text) AND (jsonb_array_length(role_sequence) >= 1))),
    CONSTRAINT project_loop_workspace_backend_valid CHECK (((workspace_backend IS NULL) OR (workspace_backend = ANY (ARRAY['sandbox'::text, 'vm'::text, 'virtual'::text, 'none'::text])))),
    CONSTRAINT project_loops_status_check CHECK ((status = ANY (ARRAY['running'::text, 'paused'::text, 'stopped'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: TABLE project_loops; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.project_loops IS 'Control row for the project self-improvement loop. One active row per project advances one job at a time via the _advance_project_loop completion hook, bounded by max_iterations / run_until / max_consecutive_failures. Design: docs/features/project_self_improvement_loop.md.';


--
-- Name: COLUMN project_loops.acceptance_criteria; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.acceptance_criteria IS 'Definition of Done the Critic verifies against. Research: LLM self-evaluation of progress is near-random, so "done" must anchor to external criteria, never the agent''s own confidence.';


--
-- Name: COLUMN project_loops.role_sequence; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.role_sequence IS 'Ordered JSONB array of expert config names rotated one per spawned job (e.g. ["scholar","critic","developer"]); seq_index points at the next.';


--
-- Name: COLUMN project_loops.stop_reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.stop_reason IS 'Why the loop ended: budget (iterations exhausted), deadline (run_until passed), failures (consecutive-failure cap), goal_met (Critic decided), or user (manual stop).';


--
-- Name: COLUMN project_loops.workspace_backend; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.workspace_backend IS 'Optional per-loop workspace tier override. NULL = each spawned job uses the default (sandbox). When set, create_loop_job injects config_override.workspace.backend for every job — e.g. ''vm'' gives every role a root VM. Mirrors the per-loop model override.';


--
-- Name: project_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_members (
    project_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role character varying(50) DEFAULT 'editor'::character varying NOT NULL,
    added_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT valid_member_role CHECK (((role)::text = ANY ((ARRAY['owner'::character varying, 'editor'::character varying, 'viewer'::character varying])::text[])))
);


--
-- Name: project_repositories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_repositories (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    project_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    repo_url text NOT NULL,
    credentials jsonb DEFAULT '{}'::jsonb,
    role character varying(50) DEFAULT 'source'::character varying NOT NULL,
    read_only boolean DEFAULT false NOT NULL,
    is_managed boolean DEFAULT false NOT NULL,
    branch text DEFAULT 'main'::text,
    clone_path text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_reference_read_only CHECK ((((role)::text <> 'reference'::text) OR (read_only = true))),
    CONSTRAINT valid_repo_role CHECK (((role)::text = ANY ((ARRAY['jobs'::character varying, 'source'::character varying, 'reference'::character varying])::text[])))
);


--
-- Name: projects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.projects (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    name text NOT NULL,
    description text,
    goal text,
    status character varying(50) DEFAULT 'active'::character varying,
    is_default boolean DEFAULT false NOT NULL,
    default_config_name character varying(100),
    default_config_override jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    nextcloud_folder_id integer,
    cloud_storage_read_only boolean DEFAULT false NOT NULL,
    main_cloud_backend text,
    main_cloud_folder_handle text,
    network_tier text DEFAULT 'internet-only'::text NOT NULL,
    CONSTRAINT projects_network_tier_check CHECK ((network_tier = ANY (ARRAY['internet-only'::text, 'home-allowed'::text]))),
    CONSTRAINT valid_project_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'paused'::character varying, 'completed'::character varying, 'archived'::character varying])::text[])))
);


--
-- Name: COLUMN projects.network_tier; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.projects.network_tier IS 'Workspace pod-network egress tier. Controls which CIDRs the project''s workspace pods can reach. The orchestrator emits this value as the srw.io/network-tier pod label; the matching helm NetworkPolicy (one per tier) enforces the allowlist. See docs/features/workspace_network_isolation.md §3.';


--
-- Name: rollup_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rollup_state (
    name text NOT NULL,
    last_closed_day date,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE rollup_state; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.rollup_state IS 'Rollup watermarks — one row per named rollup (e.g. usage_daily). last_closed_day = newest UTC day fully rolled up (NULL = never run). Advanced atomically with the rollup upsert (cross-DB exactly-once).';


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
-- Name: security_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    event_type text NOT NULL,
    user_id uuid,
    auth_method text,
    real_is_admin boolean DEFAULT false NOT NULL,
    view_as boolean DEFAULT false NOT NULL,
    resource_type text NOT NULL,
    resource_id text,
    method text,
    path text,
    detail text,
    client_ip text
);


--
-- Name: TABLE security_events; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.security_events IS 'Denied-access audit log. One row per 403 raised by a security/access.py gate (plus _require_admin and IDE proxy denials). Written best-effort — a failed insert never blocks the 403. Pruned by the retention sweeper. See docs/features/security_event_log.md.';


--
-- Name: COLUMN security_events.event_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.security_events.event_type IS 'access_denied (resource gates) | admin_denied (_require_admin). Open enum — future: login_failed, token_revoked, ...';


--
-- Name: COLUMN security_events.user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.security_events.user_id IS 'Authenticated caller. Deliberately no FK — rows outlive user deletion.';


--
-- Name: COLUMN security_events.view_as; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.security_events.view_as IS 'TRUE when an admin had the X-Admin-View-As shadow on: the denial came from narrowed visibility, not a genuine cross-user attempt.';


--
-- Name: skill_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.skill_files (
    skill_id uuid NOT NULL,
    path text NOT NULL,
    content text NOT NULL
);


--
-- Name: skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.skills (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(100) NOT NULL,
    display_name character varying(200) NOT NULL,
    description text,
    icon character varying(100) DEFAULT 'extension'::character varying NOT NULL,
    color character varying(7) DEFAULT '#6B7280'::character varying NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    owner_id uuid NOT NULL,
    is_global boolean DEFAULT false NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE skills; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.skills IS 'DB-backed user/admin Agent Skills (overlay over bundled config/skills/). name/description denormalized from the canonical SKILL.md in skill_files. Design: docs/features/agent_skills.md.';


--
-- Name: srw_pre_auth_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.srw_pre_auth_states (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    state text NOT NULL,
    pkce_verifier text NOT NULL,
    return_to text DEFAULT '/'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone
);


--
-- Name: TABLE srw_pre_auth_states; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.srw_pre_auth_states IS 'Server-side OAuth state + PKCE verifier between /auth/login and /auth/callback. 5-minute TTL, single-use via consumed_at CAS.';


--
-- Name: srw_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.srw_sessions (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    user_id uuid NOT NULL,
    kc_sub text NOT NULL,
    kc_sid text,
    access_token text NOT NULL,
    refresh_token text NOT NULL,
    id_token text NOT NULL,
    access_expires_at timestamp with time zone NOT NULL,
    absolute_expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    user_agent text,
    created_ip inet,
    revoked_at timestamp with time zone
);


--
-- Name: TABLE srw_sessions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.srw_sessions IS 'BFF session rows. Cookie value = id (UUID). KC tokens held server-side and refreshed in place. See docs/features/auth_bff_and_api_tokens.md §1.2.';


--
-- Name: COLUMN srw_sessions.kc_sid; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.srw_sessions.kc_sid IS 'Keycloak session ID from id_token claim "sid". Used for back-channel logout.';


--
-- Name: COLUMN srw_sessions.absolute_expires_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.srw_sessions.absolute_expires_at IS 'Anchored to Keycloak refresh-token TTL at session creation. After this, no refresh attempt is made; user must re-authenticate.';


--
-- Name: COLUMN srw_sessions.last_seen_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.srw_sessions.last_seen_at IS 'Idle timeout anchor: validator rejects if last_seen_at + idle < now(). Touched on every authenticated request.';


--
-- Name: sudo_approval_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sudo_approval_requests (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    job_id uuid NOT NULL,
    vm_name character varying(255) NOT NULL,
    command text NOT NULL,
    arguments text[] DEFAULT '{}'::text[],
    working_directory text,
    requesting_user character varying(255) NOT NULL,
    target_user character varying(255) DEFAULT 'root'::character varying NOT NULL,
    status public.sudo_request_status DEFAULT 'pending'::public.sudo_request_status NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    decided_by character varying(255),
    decision_reason text,
    ttl_seconds integer DEFAULT 300 NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + '00:05:00'::interval) NOT NULL,
    nats_reply_subject text,
    metadata jsonb DEFAULT '{}'::jsonb,
    request_type character varying(20) DEFAULT 'sudo_command'::character varying NOT NULL
);


--
-- Name: sudo_auto_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sudo_auto_rules (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    pattern text NOT NULL,
    action character varying(20) NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    description text,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    CONSTRAINT valid_action CHECK (((action)::text = ANY ((ARRAY['approve'::character varying, 'deny'::character varying, 'review'::character varying])::text[])))
);


--
-- Name: system_api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_api_keys (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    provider character varying(50) NOT NULL,
    api_key text NOT NULL,
    key_prefix character varying(12) NOT NULL,
    label text,
    seeded_from text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    discovery_cache_json jsonb,
    discovery_cache_at timestamp with time zone,
    CONSTRAINT valid_system_api_key_provider CHECK (((provider)::text = ANY ((ARRAY['openai'::character varying, 'anthropic'::character varying, 'google'::character varying, 'groq'::character varying, 'openrouter'::character varying, 'mistral'::character varying, 'vision'::character varying])::text[])))
);


--
-- Name: system_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_settings (
    key text NOT NULL,
    value jsonb NOT NULL,
    credentials_ref text,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_by text
);


--
-- Name: thread_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_events (
    id bigint NOT NULL,
    thread_id uuid NOT NULL,
    epoch integer NOT NULL,
    seq bigint NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE thread_events; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_events IS 'Append-only wire-frame log for persistent-session SSE replay. See docs/features/headless_persistent_sessions.md.';


--
-- Name: COLUMN thread_events.epoch; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_events.epoch IS 'Bumped on agent pod restart with cold checkpoint. Client cursors whose epoch != current force a full re-sync (GONE_BEYOND_HORIZON).';


--
-- Name: COLUMN thread_events.seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_events.seq IS 'Monotonic per (thread_id, epoch). Allocated by the agent.';


--
-- Name: COLUMN thread_events.kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_events.kind IS 'Frame method: token, thinking, tool.started, tool.completed, turn.started, turn.completed, permission.request, ready, error, session.ended, etc. Same vocabulary as the legacy WS frames.';


--
-- Name: thread_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.thread_events ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.thread_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: thread_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_messages (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    thread_id uuid NOT NULL,
    role character varying(20) NOT NULL,
    content text,
    tool_calls jsonb,
    turn_number integer,
    metrics jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    tool_call_id character varying(255),
    thinking text,
    reasoning jsonb,
    tool_results jsonb,
    provider text,
    provider_raw jsonb,
    additional_kwargs jsonb,
    response_metadata jsonb,
    seq bigint NOT NULL
);


--
-- Name: COLUMN thread_messages.tool_call_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_messages.tool_call_id IS 'Set only on role=''tool'' rows. Mirrors LangChain ToolMessage.tool_call_id and points back to the matching tool_calls[].id on the AIMessage row that produced this result. NULL on ai/human/system rows.';


--
-- Name: COLUMN thread_messages.thinking; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_messages.thinking IS 'Set only on role=''ai'' rows for reasoning models. Captures additional_kwargs.reasoning_content (non-Anthropic) or the concatenated content of type=''thinking'' blocks (Anthropic). NULL when the model does not emit a visible reasoning channel.';


--
-- Name: COLUMN thread_messages.reasoning; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_messages.reasoning IS 'Normalized reasoning items (role=ai). Written going forward; supersedes the legacy `thinking` TEXT column (kept for historical reads).';


--
-- Name: COLUMN thread_messages.provider; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_messages.provider IS 'Provider tag for the row, e.g. openai-chat | openai-responses | anthropic. Selects the replay path in src/llm/session_components.py.';


--
-- Name: COLUMN thread_messages.provider_raw; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_messages.provider_raw IS 'Verbatim provider response payload for audit + faithful same-provider replay (forward-compat with Responses API / Anthropic). Captured from the COMPLETED (non-streamed) response.';


--
-- Name: COLUMN thread_messages.seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_messages.seq IS 'Monotonic per-row insertion order (global BIGSERIAL). The resume cursor: a role=''summary'' row records metrics.boundary_seq = seq of the last message it covers, and resume loads summary + rows with seq > boundary_seq. Backfilled in ≈ insertion order on existing rows; strictly monotonic for new rows.';


--
-- Name: thread_messages_seq_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.thread_messages_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: thread_messages_seq_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.thread_messages_seq_seq OWNED BY public.thread_messages.seq;


--
-- Name: thread_mounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_mounts (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    thread_id uuid NOT NULL,
    mount_kind character varying(32) NOT NULL,
    target_path text NOT NULL,
    source_kind character varying(32) NOT NULL,
    source_ref uuid,
    backend_id text,
    cloud_handle text,
    webdav_url text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    target_user_sub text,
    CONSTRAINT valid_mount_kind CHECK (((mount_kind)::text = ANY ((ARRAY['project'::character varying, 'project_default'::character varying, 'repo'::character varying])::text[]))),
    CONSTRAINT valid_source_kind CHECK (((source_kind)::text = ANY ((ARRAY['project_folder'::character varying, 'user_home'::character varying, 'repo'::character varying])::text[])))
);


--
-- Name: TABLE thread_mounts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_mounts IS 'Canonical record of which cloud surfaces are attached to each persistent thread. Source of truth for project attachment; deprecates threads.metadata.project_ids.';


--
-- Name: COLUMN thread_mounts.target_path; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_mounts.target_path IS 'Workspace-relative path where this mount appears to the agent. Empty string means the mount lives at the workspace root (used by project_default in Phase 2). No leading slash.';


--
-- Name: COLUMN thread_mounts.source_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_mounts.source_ref IS 'projects.id when source_kind=''project_folder''. NULL for ''user_home'' (resolved via the thread''s user_id at payload-build time).';


--
-- Name: COLUMN thread_mounts.target_user_sub; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_mounts.target_user_sub IS 'Keycloak sub of the user being impersonated for this mount. Set only for project_default mounts (user-home at workspace root); NULL for project / repo mounts that the service account can read directly. Drives RFC 8693 token-exchange in the agent''s OpenCloud sync.';


--
-- Name: thread_notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_notifications (
    id bigint NOT NULL,
    thread_id uuid NOT NULL,
    request_id uuid,
    kind text NOT NULL,
    sent_at timestamp with time zone DEFAULT now() NOT NULL,
    delivery_status text,
    email_to text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: TABLE thread_notifications; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_notifications IS 'Audit + dedup + rate-limit log for headless-session emails. One row per outbound email regardless of delivery status.';


--
-- Name: COLUMN thread_notifications.kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_notifications.kind IS 'Notification kind: permission_pending, agent_paused, error, etc.';


--
-- Name: COLUMN thread_notifications.delivery_status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_notifications.delivery_status IS 'sent | failed | skipped_rate_limit | skipped_dedup. Skipped rows are still recorded for observability.';


--
-- Name: thread_notifications_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.thread_notifications ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.thread_notifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: thread_permission_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_permission_requests (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    thread_id uuid NOT NULL,
    tool_call_id text NOT NULL,
    tool_name text NOT NULL,
    tool_args jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    decided_by text,
    expires_at timestamp with time zone DEFAULT (now() + '00:05:00'::interval) NOT NULL,
    CONSTRAINT thread_permission_requests_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'denied'::text, 'expired'::text])))
);


--
-- Name: TABLE thread_permission_requests; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_permission_requests IS 'Per-thread tool-permission requests. Agent INSERTs on permission_check, updates flow via UPDATE → trigger → NOTIFY → agent LISTEN.';


--
-- Name: COLUMN thread_permission_requests.tool_call_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_permission_requests.tool_call_id IS 'The LangChain tool_call id from the AI message — opaque, used to correlate the decision back to the originating call.';


--
-- Name: COLUMN thread_permission_requests.decided_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_permission_requests.decided_by IS 'User id, MCP token id, or "system" (for timeout-driven expiry).';


--
-- Name: threads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.threads (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    title text DEFAULT 'Untitled Session'::text,
    user_id uuid,
    project_id uuid,
    agent_id uuid,
    status character varying(20) DEFAULT 'created'::character varying NOT NULL,
    permission_mode character varying(20) DEFAULT 'supervised'::character varying NOT NULL,
    config_name character varying(100),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    last_activity timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    ended_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb,
    total_turns integer DEFAULT 0,
    total_tokens integer DEFAULT 0,
    nc_session_folder text,
    nc_share_id integer,
    main_cloud_backend text,
    main_cloud_session_handle text,
    main_cloud_share_handle text,
    events_epoch integer DEFAULT 0 NOT NULL,
    awaiting_user_since timestamp with time zone,
    extend_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT valid_permission_mode CHECK (((permission_mode)::text = ANY ((ARRAY['supervised'::character varying, 'auto_accept'::character varying, 'autonomous'::character varying])::text[]))),
    CONSTRAINT valid_thread_status CHECK (((status)::text = ANY ((ARRAY['created'::character varying, 'active'::character varying, 'idle'::character varying, 'awaiting_user'::character varying, 'suspended'::character varying, 'ended'::character varying])::text[])))
);


--
-- Name: COLUMN threads.events_epoch; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.events_epoch IS 'Current event-log epoch for this thread. Agent bumps on attach when a cold checkpoint restart loses its in-memory seq counter; client cursors from older epochs trigger a full re-sync.';


--
-- Name: COLUMN threads.awaiting_user_since; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.awaiting_user_since IS 'Set by the agent when it reaches a natural pause untethered. The attention_sleep_sweeper suspends the workspace when this exceeds headless_attention_sleep_minutes. Cleared on reattach.';


--
-- Name: COLUMN threads.extend_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.extend_count IS 'Number of magic-link extend-window clicks since this awaiting_user session began. Capped at 4 (= 4h ceiling at default 60min/extend).';


--
-- Name: usage_daily; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_daily (
    day date NOT NULL,
    user_id uuid,
    project_id uuid,
    category text NOT NULL,
    resource text NOT NULL,
    unit text NOT NULL,
    quantity numeric NOT NULL,
    cost_usd numeric DEFAULT 0 NOT NULL,
    events bigint NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE usage_daily; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.usage_daily IS 'Daily pre-aggregated usage rollup (app-DB read model). Mirrors the auditdb usage_events ledger summed per UTC day x (user, project, category, resource, unit). /api/usage serves this for CLOSED days (day <= rollup_state watermark) and the raw ledger for the open tail. Maintained by services/usage_rollup.py via full-replace upserts — never hand-written. Per-job cost stays on raw usage_events (ref_id is not a dim here).';


--
-- Name: usage_rates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rates (
    category text NOT NULL,
    resource text NOT NULL,
    unit text NOT NULL,
    rate_usd numeric NOT NULL,
    effective_from timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE usage_rates; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.usage_rates IS 'Effective-dated $/unit price config for the usage ledger (app DB; admin-editable). The orchestrator snapshots the newest rate <= an event ts onto usage_events.rate_usd/cost_usd at write time. Ships empty: quantities are metered immediately, dollars only once rates are seeded.';


--
-- Name: COLUMN usage_rates.resource; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_rates.resource IS 'Specific resource id or ''*'' (category default). Resolver prefers the specific row, falls back to ''*''.';


--
-- Name: user_api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_api_keys (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    user_id uuid NOT NULL,
    provider character varying(50) NOT NULL,
    api_key text NOT NULL,
    key_prefix character varying(12) NOT NULL,
    label text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT valid_user_api_key_provider CHECK (((provider)::text = ANY ((ARRAY['openai'::character varying, 'anthropic'::character varying, 'google'::character varying, 'groq'::character varying, 'openrouter'::character varying, 'mistral'::character varying, 'vision'::character varying])::text[])))
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    display_name text NOT NULL,
    avatar_color character varying(7) DEFAULT '#89b4fa'::character varying,
    email text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    is_admin boolean DEFAULT false NOT NULL,
    can_use_vm boolean DEFAULT false NOT NULL,
    keycloak_sub text,
    settings jsonb DEFAULT '{}'::jsonb,
    default_project_id uuid,
    is_approved boolean DEFAULT false NOT NULL,
    approved_at timestamp with time zone,
    approved_by uuid,
    preferred_username text
);


--
-- Name: COLUMN users.is_approved; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users.is_approved IS 'App-side admission flag. Checked per request by require_approved_user. Owned by the orchestrator, not the Keycloak realm role. See docs/features/app_side_admission.md.';


--
-- Name: COLUMN users.approved_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users.approved_by IS 'Admin who approved this user. NULL + approved_at set = migrated from Keycloak role / system; a real UUID = a human clicked approve.';


--
-- Name: workspace_intervals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_intervals (
    id bigint NOT NULL,
    owner_kind text NOT NULL,
    owner_id uuid NOT NULL,
    tier text,
    cpu_millicores integer NOT NULL,
    mem_bytes bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    materialized_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE workspace_intervals; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.workspace_intervals IS 'Open/close bookkeeping for workspace-pod compute (Slice 4b). One row per pod lifetime; a materializer loop converts CLOSED rows into immutable usage_events (vcpu-hour + gib-hour) and stamps materialized_at. App DB (orchestrator mutates it); the cost ledger is in srw-auditdb.';


--
-- Name: workspace_intervals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.workspace_intervals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: workspace_intervals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workspace_intervals_id_seq OWNED BY public.workspace_intervals.id;


--
-- Name: thread_messages seq; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_messages ALTER COLUMN seq SET DEFAULT nextval('public.thread_messages_seq_seq'::regclass);


--
-- Name: workspace_intervals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_intervals ALTER COLUMN id SET DEFAULT nextval('public.workspace_intervals_id_seq'::regclass);


--
-- Name: agents agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id);


--
-- Name: automations automations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automations
    ADD CONSTRAINT automations_pkey PRIMARY KEY (id);


--
-- Name: capability_grants capability_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.capability_grants
    ADD CONSTRAINT capability_grants_pkey PRIMARY KEY (id);


--
-- Name: datasources datasources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_pkey PRIMARY KEY (id);


--
-- Name: experts experts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experts
    ADD CONSTRAINT experts_pkey PRIMARY KEY (id);


--
-- Name: external_contacts external_contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_contacts
    ADD CONSTRAINT external_contacts_pkey PRIMARY KEY (id);


--
-- Name: job_datasources job_datasources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_datasources
    ADD CONSTRAINT job_datasources_pkey PRIMARY KEY (job_id, datasource_id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: llm_endpoints llm_endpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_endpoints
    ADD CONSTRAINT llm_endpoints_pkey PRIMARY KEY (id);


--
-- Name: magic_link_tokens magic_link_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.magic_link_tokens
    ADD CONSTRAINT magic_link_tokens_pkey PRIMARY KEY (id);


--
-- Name: magic_link_tokens magic_link_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.magic_link_tokens
    ADD CONSTRAINT magic_link_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: auth_tokens mcp_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT mcp_tokens_pkey PRIMARY KEY (id);


--
-- Name: auth_tokens mcp_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT mcp_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: message_log message_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_log
    ADD CONSTRAINT message_log_pkey PRIMARY KEY (id);


--
-- Name: models models_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.models
    ADD CONSTRAINT models_pkey PRIMARY KEY (id);


--
-- Name: notification_queue notification_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_queue
    ADD CONSTRAINT notification_queue_pkey PRIMARY KEY (id);


--
-- Name: processed_inbound_emails processed_inbound_emails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processed_inbound_emails
    ADD CONSTRAINT processed_inbound_emails_pkey PRIMARY KEY (email_message_id);


--
-- Name: project_api_keys project_api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_api_keys
    ADD CONSTRAINT project_api_keys_pkey PRIMARY KEY (id);


--
-- Name: project_datasources project_datasources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_datasources
    ADD CONSTRAINT project_datasources_pkey PRIMARY KEY (project_id, datasource_id);


--
-- Name: project_experts project_experts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_experts
    ADD CONSTRAINT project_experts_pkey PRIMARY KEY (project_id, expert_id);


--
-- Name: project_loops project_loops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_loops
    ADD CONSTRAINT project_loops_pkey PRIMARY KEY (id);


--
-- Name: project_members project_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_pkey PRIMARY KEY (project_id, user_id);


--
-- Name: project_repositories project_repositories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_repositories
    ADD CONSTRAINT project_repositories_pkey PRIMARY KEY (id);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: config_overrides prompt_overrides_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config_overrides
    ADD CONSTRAINT prompt_overrides_pkey PRIMARY KEY (id);


--
-- Name: rollup_state rollup_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rollup_state
    ADD CONSTRAINT rollup_state_pkey PRIMARY KEY (name);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (filename);


--
-- Name: security_events security_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_events
    ADD CONSTRAINT security_events_pkey PRIMARY KEY (id);


--
-- Name: skill_files skill_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skill_files
    ADD CONSTRAINT skill_files_pkey PRIMARY KEY (skill_id, path);


--
-- Name: skills skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skills
    ADD CONSTRAINT skills_pkey PRIMARY KEY (id);


--
-- Name: srw_pre_auth_states srw_pre_auth_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.srw_pre_auth_states
    ADD CONSTRAINT srw_pre_auth_states_pkey PRIMARY KEY (id);


--
-- Name: srw_sessions srw_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.srw_sessions
    ADD CONSTRAINT srw_sessions_pkey PRIMARY KEY (id);


--
-- Name: sudo_approval_requests sudo_approval_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sudo_approval_requests
    ADD CONSTRAINT sudo_approval_requests_pkey PRIMARY KEY (id);


--
-- Name: sudo_auto_rules sudo_auto_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sudo_auto_rules
    ADD CONSTRAINT sudo_auto_rules_pkey PRIMARY KEY (id);


--
-- Name: system_api_keys system_api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_api_keys
    ADD CONSTRAINT system_api_keys_pkey PRIMARY KEY (id);


--
-- Name: system_api_keys system_api_keys_provider_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_api_keys
    ADD CONSTRAINT system_api_keys_provider_key UNIQUE (provider);


--
-- Name: system_settings system_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_settings
    ADD CONSTRAINT system_settings_pkey PRIMARY KEY (key);


--
-- Name: thread_events thread_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_events
    ADD CONSTRAINT thread_events_pkey PRIMARY KEY (id);


--
-- Name: thread_messages thread_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_messages
    ADD CONSTRAINT thread_messages_pkey PRIMARY KEY (id);


--
-- Name: thread_mounts thread_mounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_mounts
    ADD CONSTRAINT thread_mounts_pkey PRIMARY KEY (id);


--
-- Name: thread_notifications thread_notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_notifications
    ADD CONSTRAINT thread_notifications_pkey PRIMARY KEY (id);


--
-- Name: thread_permission_requests thread_permission_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_permission_requests
    ADD CONSTRAINT thread_permission_requests_pkey PRIMARY KEY (id);


--
-- Name: threads threads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.threads
    ADD CONSTRAINT threads_pkey PRIMARY KEY (id);


--
-- Name: models uq_model_provider_v2; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.models
    ADD CONSTRAINT uq_model_provider_v2 UNIQUE (provider_kind, provider_ref, model_id);


--
-- Name: thread_mounts uq_thread_mount_path; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_mounts
    ADD CONSTRAINT uq_thread_mount_path UNIQUE (thread_id, target_path);


--
-- Name: usage_rates usage_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rates
    ADD CONSTRAINT usage_rates_pkey PRIMARY KEY (category, resource, unit, effective_from);


--
-- Name: user_api_keys user_api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_api_keys
    ADD CONSTRAINT user_api_keys_pkey PRIMARY KEY (id);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_keycloak_sub_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_keycloak_sub_key UNIQUE (keycloak_sub);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: workspace_intervals workspace_intervals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_intervals
    ADD CONSTRAINT workspace_intervals_pkey PRIMARY KEY (id);


--
-- Name: idx_agents_current_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agents_current_job ON public.agents USING btree (current_job_id);


--
-- Name: idx_agents_last_heartbeat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agents_last_heartbeat ON public.agents USING btree (last_heartbeat);


--
-- Name: idx_agents_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agents_mode ON public.agents USING btree (agent_mode);


--
-- Name: idx_agents_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agents_status ON public.agents USING btree (status);


--
-- Name: idx_agents_thread_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agents_thread_id ON public.agents USING btree (thread_id);


--
-- Name: idx_auth_tokens_superseded_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_tokens_superseded_by ON public.auth_tokens USING btree (superseded_by) WHERE (superseded_by IS NOT NULL);


--
-- Name: idx_auth_tokens_user_kind_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_tokens_user_kind_active ON public.auth_tokens USING btree (user_id, kind, created_at DESC) WHERE (revoked_at IS NULL);


--
-- Name: idx_automations_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_automations_due ON public.automations USING btree (next_run_at) WHERE ((enabled = true) AND (trigger_type = 'cron'::text) AND (next_run_at IS NOT NULL));


--
-- Name: idx_automations_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_automations_event ON public.automations USING gin (event_filter jsonb_path_ops) WHERE ((enabled = true) AND (trigger_type = 'event'::text));


--
-- Name: idx_automations_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_automations_owner ON public.automations USING btree (owner_id);


--
-- Name: idx_automations_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_automations_project ON public.automations USING btree (project_id) WHERE (project_id IS NOT NULL);


--
-- Name: idx_config_override_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_config_override_lookup ON public.config_overrides USING btree (family, kind, name);


--
-- Name: idx_datasources_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_datasources_created_by ON public.datasources USING btree (created_by);


--
-- Name: idx_datasources_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_datasources_job_id ON public.datasources USING btree (job_id);


--
-- Name: idx_datasources_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_datasources_project_id ON public.datasources USING btree (project_id);


--
-- Name: idx_datasources_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_datasources_type ON public.datasources USING btree (type);


--
-- Name: idx_experts_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_experts_owner ON public.experts USING btree (owner_id);


--
-- Name: idx_experts_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_experts_type ON public.experts USING btree (expert_type);


--
-- Name: idx_ext_contacts_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ext_contacts_project ON public.external_contacts USING btree (project_id);


--
-- Name: idx_grants_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_grants_scope ON public.capability_grants USING btree (scope_kind, scope_id);


--
-- Name: idx_job_datasources_ds; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_datasources_ds ON public.job_datasources USING btree (datasource_id);


--
-- Name: idx_jobs_assigned_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_assigned_agent ON public.jobs USING btree (assigned_agent_id);


--
-- Name: idx_jobs_config_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_config_name ON public.jobs USING btree (config_name);


--
-- Name: idx_jobs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_created_at ON public.jobs USING btree (created_at DESC);


--
-- Name: idx_jobs_diff_status_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_diff_status_pending ON public.jobs USING btree (id) WHERE (diff_status = 'pending'::text);


--
-- Name: idx_jobs_dispatchable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_dispatchable ON public.jobs USING btree (priority DESC, created_at) WHERE ((assigned_agent_id IS NULL) AND (freeze_data IS NULL) AND ((status)::text = ANY ((ARRAY['created'::character varying, 'paused'::character varying])::text[])));


--
-- Name: idx_jobs_parent_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_parent_job_id ON public.jobs USING btree (parent_job_id);


--
-- Name: idx_jobs_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_priority ON public.jobs USING btree (priority DESC);


--
-- Name: idx_jobs_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_project_id ON public.jobs USING btree (project_id);


--
-- Name: idx_jobs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_status ON public.jobs USING btree (status);


--
-- Name: idx_jobs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_user_id ON public.jobs USING btree (user_id);


--
-- Name: idx_llm_endpoints_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_llm_endpoints_user ON public.llm_endpoints USING btree (user_id);


--
-- Name: idx_mcp_tokens_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_tokens_hash ON public.auth_tokens USING btree (token_hash);


--
-- Name: idx_mcp_tokens_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_tokens_user ON public.auth_tokens USING btree (user_id);


--
-- Name: idx_message_log_email_msgid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_message_log_email_msgid ON public.message_log USING btree (email_message_id) WHERE (email_message_id IS NOT NULL);


--
-- Name: idx_message_log_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_message_log_job ON public.message_log USING btree (job_id);


--
-- Name: idx_message_log_rate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_message_log_rate ON public.message_log USING btree (job_id, created_at) WHERE ((direction)::text = 'outbound'::text);


--
-- Name: idx_message_log_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_message_log_thread ON public.message_log USING btree (thread_id);


--
-- Name: idx_message_log_user_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_message_log_user_created ON public.message_log USING btree (user_id, created_at);


--
-- Name: idx_mlt_approval; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mlt_approval ON public.magic_link_tokens USING btree (approval_id, created_at DESC) WHERE (approval_id IS NOT NULL);


--
-- Name: idx_mlt_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mlt_token_hash ON public.magic_link_tokens USING btree (token_hash);


--
-- Name: idx_mlt_unused_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mlt_unused_expiry ON public.magic_link_tokens USING btree (expires_at) WHERE (used_at IS NULL);


--
-- Name: idx_models_capabilities_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_models_capabilities_enabled ON public.models USING gin (capabilities) WHERE (enabled = true);


--
-- Name: idx_models_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_models_provider ON public.models USING btree (provider_kind, provider_ref);


--
-- Name: idx_notif_queue_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notif_queue_pending ON public.notification_queue USING btree (user_id, queued_at) WHERE (delivered_at IS NULL);


--
-- Name: idx_project_api_keys_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_api_keys_project ON public.project_api_keys USING btree (project_id);


--
-- Name: idx_project_datasources_ds; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_datasources_ds ON public.project_datasources USING btree (datasource_id);


--
-- Name: idx_project_loops_one_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_project_loops_one_active ON public.project_loops USING btree (project_id) WHERE (status = ANY (ARRAY['running'::text, 'paused'::text]));


--
-- Name: idx_project_loops_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_loops_owner ON public.project_loops USING btree (owner_id) WHERE (owner_id IS NOT NULL);


--
-- Name: idx_project_loops_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_loops_project ON public.project_loops USING btree (project_id);


--
-- Name: idx_project_members_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_members_user ON public.project_members USING btree (user_id);


--
-- Name: idx_project_repos_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_repos_project ON public.project_repositories USING btree (project_id);


--
-- Name: idx_projects_network_tier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_projects_network_tier ON public.projects USING btree (network_tier) WHERE (network_tier <> 'internet-only'::text);


--
-- Name: idx_projects_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_projects_status ON public.projects USING btree (status);


--
-- Name: idx_security_events_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_events_created_at ON public.security_events USING btree (created_at DESC);


--
-- Name: idx_security_events_user_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_events_user_created ON public.security_events USING btree (user_id, created_at DESC);


--
-- Name: idx_skills_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_skills_owner ON public.skills USING btree (owner_id);


--
-- Name: idx_srw_pre_auth_states_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_srw_pre_auth_states_expires ON public.srw_pre_auth_states USING btree (expires_at);


--
-- Name: idx_srw_pre_auth_states_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_srw_pre_auth_states_state ON public.srw_pre_auth_states USING btree (state) WHERE (consumed_at IS NULL);


--
-- Name: idx_srw_sessions_absolute_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_srw_sessions_absolute_expires ON public.srw_sessions USING btree (absolute_expires_at);


--
-- Name: idx_srw_sessions_kc_sid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_srw_sessions_kc_sid ON public.srw_sessions USING btree (kc_sid) WHERE (kc_sid IS NOT NULL);


--
-- Name: idx_srw_sessions_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_srw_sessions_user_active ON public.srw_sessions USING btree (user_id) WHERE (revoked_at IS NULL);


--
-- Name: idx_sudo_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sudo_expiry ON public.sudo_approval_requests USING btree (expires_at) WHERE (status = 'pending'::public.sudo_request_status);


--
-- Name: idx_sudo_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sudo_job ON public.sudo_approval_requests USING btree (job_id, requested_at DESC);


--
-- Name: idx_sudo_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sudo_pending ON public.sudo_approval_requests USING btree (status, requested_at DESC) WHERE (status = 'pending'::public.sudo_request_status);


--
-- Name: idx_sudo_rules_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sudo_rules_active ON public.sudo_auto_rules USING btree (priority) WHERE (enabled = true);


--
-- Name: idx_thread_events_thread_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_events_thread_created ON public.thread_events USING btree (thread_id, created_at);


--
-- Name: idx_thread_events_thread_epoch_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_thread_events_thread_epoch_seq ON public.thread_events USING btree (thread_id, epoch, seq);


--
-- Name: idx_thread_messages_thread_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_messages_thread_seq ON public.thread_messages USING btree (thread_id, seq);


--
-- Name: idx_thread_messages_thread_turn_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_messages_thread_turn_created ON public.thread_messages USING btree (thread_id, turn_number, created_at);


--
-- Name: idx_thread_mounts_source_ref; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_mounts_source_ref ON public.thread_mounts USING btree (source_ref) WHERE (source_ref IS NOT NULL);


--
-- Name: idx_thread_mounts_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_mounts_thread ON public.thread_mounts USING btree (thread_id);


--
-- Name: idx_threads_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_threads_agent ON public.threads USING btree (agent_id);


--
-- Name: idx_threads_awaiting_user_since; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_threads_awaiting_user_since ON public.threads USING btree (awaiting_user_since) WHERE ((status)::text = 'awaiting_user'::text);


--
-- Name: idx_threads_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_threads_project ON public.threads USING btree (project_id);


--
-- Name: idx_threads_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_threads_status ON public.threads USING btree (status);


--
-- Name: idx_threads_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_threads_user ON public.threads USING btree (user_id);


--
-- Name: idx_tn_thread_request; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tn_thread_request ON public.thread_notifications USING btree (thread_id, request_id) WHERE (request_id IS NOT NULL);


--
-- Name: idx_tn_thread_sent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tn_thread_sent ON public.thread_notifications USING btree (thread_id, sent_at DESC);


--
-- Name: idx_tpr_pending_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tpr_pending_expiry ON public.thread_permission_requests USING btree (expires_at) WHERE (status = 'pending'::text);


--
-- Name: idx_tpr_thread_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tpr_thread_pending ON public.thread_permission_requests USING btree (thread_id, requested_at DESC) WHERE (status = 'pending'::text);


--
-- Name: idx_tpr_thread_requested; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tpr_thread_requested ON public.thread_permission_requests USING btree (thread_id, requested_at DESC);


--
-- Name: idx_user_api_keys_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_api_keys_user ON public.user_api_keys USING btree (user_id);


--
-- Name: idx_users_keycloak_sub; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_keycloak_sub ON public.users USING btree (keycloak_sub);


--
-- Name: schema_migrations_dirty_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX schema_migrations_dirty_idx ON public.schema_migrations USING btree (filename) WHERE (success = false);


--
-- Name: uq_config_override; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_config_override ON public.config_overrides USING btree (COALESCE(family, ''::character varying), kind, name);


--
-- Name: uq_datasource_name_type_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_datasource_name_type_owner ON public.datasources USING btree (name, type, COALESCE(created_by, '00000000-0000-0000-0000-000000000000'::uuid));


--
-- Name: uq_experts_name_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_experts_name_owner ON public.experts USING btree (name, owner_id);


--
-- Name: uq_ext_contact_project_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_ext_contact_project_email ON public.external_contacts USING btree (project_id, email);


--
-- Name: uq_grants_scope_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_grants_scope_key ON public.capability_grants USING btree (scope_kind, scope_id, key) NULLS NOT DISTINCT;


--
-- Name: uq_llm_endpoint_label_system; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_llm_endpoint_label_system ON public.llm_endpoints USING btree (label) WHERE (user_id IS NULL);


--
-- Name: uq_llm_endpoint_label_user; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_llm_endpoint_label_user ON public.llm_endpoints USING btree (user_id, label) WHERE (user_id IS NOT NULL);


--
-- Name: uq_project_api_keys_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_project_api_keys_provider ON public.project_api_keys USING btree (project_id, provider);


--
-- Name: uq_project_default_expert; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_project_default_expert ON public.project_experts USING btree (project_id, default_for) WHERE (default_for IS NOT NULL);


--
-- Name: uq_project_jobs_repo; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_project_jobs_repo ON public.project_repositories USING btree (project_id) WHERE ((role)::text = 'jobs'::text);


--
-- Name: uq_skills_name_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_skills_name_owner ON public.skills USING btree (name, owner_id);


--
-- Name: uq_sudo_request_reply_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_sudo_request_reply_subject ON public.sudo_approval_requests USING btree (nats_reply_subject) WHERE (nats_reply_subject IS NOT NULL);


--
-- Name: INDEX uq_sudo_request_reply_subject; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON INDEX public.uq_sudo_request_reply_subject IS 'At most one sudo_approval_requests row per NATS reply subject. The insert-as-claim dedup slot for fan-out NATS sudo requests (HA / M2-L4) — on_sudo_request claims it before acting so replicas:2 cannot double-insert or double-prompt. NULL reply subjects (vm_upgrade path) are unconstrained.';


--
-- Name: uq_tn_sent_request_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_tn_sent_request_kind ON public.thread_notifications USING btree (request_id, kind) WHERE (delivery_status = 'sent'::text);


--
-- Name: INDEX uq_tn_sent_request_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON INDEX public.uq_tn_sent_request_kind IS 'At most one delivery_status=''sent'' row per (request_id, kind). The insert-as-claim dedup slot for headless emails (HA / M1) — send_permission_pending_email claims it before sending so the transient dual-leader window cannot double-send. NULL request_id rows are unconstrained.';


--
-- Name: uq_user_api_keys_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_user_api_keys_provider ON public.user_api_keys USING btree (user_id, provider);


--
-- Name: usage_daily_dims_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX usage_daily_dims_idx ON public.usage_daily USING btree (day, user_id, project_id, category, resource, unit) NULLS NOT DISTINCT;


--
-- Name: usage_daily_user_day_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_daily_user_day_idx ON public.usage_daily USING btree (user_id, day);


--
-- Name: workspace_intervals_open_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX workspace_intervals_open_uq ON public.workspace_intervals USING btree (owner_kind, owner_id) WHERE (ended_at IS NULL);


--
-- Name: workspace_intervals_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX workspace_intervals_pending_idx ON public.workspace_intervals USING btree (ended_at) WHERE ((ended_at IS NOT NULL) AND (materialized_at IS NULL));


--
-- Name: thread_permission_requests thread_permission_notify_trigger; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER thread_permission_notify_trigger AFTER UPDATE ON public.thread_permission_requests FOR EACH ROW EXECUTE FUNCTION public.notify_thread_permission_update();


--
-- Name: datasources update_datasources_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_datasources_updated_at BEFORE UPDATE ON public.datasources FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: jobs update_jobs_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_jobs_updated_at BEFORE UPDATE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: project_api_keys update_project_api_keys_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_project_api_keys_updated_at BEFORE UPDATE ON public.project_api_keys FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: project_repositories update_project_repositories_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_project_repositories_updated_at BEFORE UPDATE ON public.project_repositories FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: projects update_projects_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_projects_updated_at BEFORE UPDATE ON public.projects FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: user_api_keys update_user_api_keys_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_user_api_keys_updated_at BEFORE UPDATE ON public.user_api_keys FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: agents agents_current_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_current_job_id_fkey FOREIGN KEY (current_job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: auth_tokens auth_tokens_superseded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT auth_tokens_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES public.auth_tokens(id) ON DELETE SET NULL;


--
-- Name: automations automations_last_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automations
    ADD CONSTRAINT automations_last_job_id_fkey FOREIGN KEY (last_job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: automations automations_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automations
    ADD CONSTRAINT automations_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: automations automations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automations
    ADD CONSTRAINT automations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: capability_grants capability_grants_granted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.capability_grants
    ADD CONSTRAINT capability_grants_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: datasources datasources_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: datasources datasources_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: datasources datasources_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: experts experts_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experts
    ADD CONSTRAINT experts_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: experts experts_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experts
    ADD CONSTRAINT experts_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: external_contacts external_contacts_added_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_contacts
    ADD CONSTRAINT external_contacts_added_by_fkey FOREIGN KEY (added_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: external_contacts external_contacts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_contacts
    ADD CONSTRAINT external_contacts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: jobs fk_jobs_assigned_agent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT fk_jobs_assigned_agent FOREIGN KEY (assigned_agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: job_datasources job_datasources_datasource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_datasources
    ADD CONSTRAINT job_datasources_datasource_id_fkey FOREIGN KEY (datasource_id) REFERENCES public.datasources(id) ON DELETE CASCADE;


--
-- Name: job_datasources job_datasources_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_datasources
    ADD CONSTRAINT job_datasources_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: jobs jobs_expert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_expert_id_fkey FOREIGN KEY (expert_id) REFERENCES public.experts(id) ON DELETE SET NULL;


--
-- Name: jobs jobs_parent_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_parent_job_id_fkey FOREIGN KEY (parent_job_id) REFERENCES public.jobs(id);


--
-- Name: jobs jobs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: jobs jobs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: llm_endpoints llm_endpoints_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_endpoints
    ADD CONSTRAINT llm_endpoints_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: magic_link_tokens magic_link_tokens_approval_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.magic_link_tokens
    ADD CONSTRAINT magic_link_tokens_approval_id_fkey FOREIGN KEY (approval_id) REFERENCES public.thread_permission_requests(id) ON DELETE CASCADE;


--
-- Name: magic_link_tokens magic_link_tokens_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.magic_link_tokens
    ADD CONSTRAINT magic_link_tokens_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: magic_link_tokens magic_link_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.magic_link_tokens
    ADD CONSTRAINT magic_link_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: auth_tokens mcp_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT mcp_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: message_log message_log_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_log
    ADD CONSTRAINT message_log_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: message_log message_log_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_log
    ADD CONSTRAINT message_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: notification_queue notification_queue_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_queue
    ADD CONSTRAINT notification_queue_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: notification_queue notification_queue_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_queue
    ADD CONSTRAINT notification_queue_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: project_api_keys project_api_keys_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_api_keys
    ADD CONSTRAINT project_api_keys_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_datasources project_datasources_datasource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_datasources
    ADD CONSTRAINT project_datasources_datasource_id_fkey FOREIGN KEY (datasource_id) REFERENCES public.datasources(id) ON DELETE CASCADE;


--
-- Name: project_datasources project_datasources_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_datasources
    ADD CONSTRAINT project_datasources_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_experts project_experts_expert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_experts
    ADD CONSTRAINT project_experts_expert_id_fkey FOREIGN KEY (expert_id) REFERENCES public.experts(id) ON DELETE CASCADE;


--
-- Name: project_experts project_experts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_experts
    ADD CONSTRAINT project_experts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_loops project_loops_current_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_loops
    ADD CONSTRAINT project_loops_current_job_id_fkey FOREIGN KEY (current_job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: project_loops project_loops_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_loops
    ADD CONSTRAINT project_loops_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: project_loops project_loops_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_loops
    ADD CONSTRAINT project_loops_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_members project_members_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_members project_members_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: project_repositories project_repositories_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_repositories
    ADD CONSTRAINT project_repositories_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: config_overrides prompt_overrides_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config_overrides
    ADD CONSTRAINT prompt_overrides_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: config_overrides prompt_overrides_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config_overrides
    ADD CONSTRAINT prompt_overrides_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: skill_files skill_files_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skill_files
    ADD CONSTRAINT skill_files_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.skills(id) ON DELETE CASCADE;


--
-- Name: skills skills_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skills
    ADD CONSTRAINT skills_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: skills skills_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skills
    ADD CONSTRAINT skills_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: srw_sessions srw_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.srw_sessions
    ADD CONSTRAINT srw_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: sudo_approval_requests sudo_approval_requests_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sudo_approval_requests
    ADD CONSTRAINT sudo_approval_requests_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: thread_events thread_events_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_events
    ADD CONSTRAINT thread_events_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: thread_messages thread_messages_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_messages
    ADD CONSTRAINT thread_messages_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: thread_mounts thread_mounts_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_mounts
    ADD CONSTRAINT thread_mounts_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: thread_notifications thread_notifications_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_notifications
    ADD CONSTRAINT thread_notifications_request_id_fkey FOREIGN KEY (request_id) REFERENCES public.thread_permission_requests(id) ON DELETE SET NULL;


--
-- Name: thread_notifications thread_notifications_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_notifications
    ADD CONSTRAINT thread_notifications_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: thread_permission_requests thread_permission_requests_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_permission_requests
    ADD CONSTRAINT thread_permission_requests_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: threads threads_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.threads
    ADD CONSTRAINT threads_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: threads threads_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.threads
    ADD CONSTRAINT threads_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: threads threads_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.threads
    ADD CONSTRAINT threads_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: user_api_keys user_api_keys_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_api_keys
    ADD CONSTRAINT user_api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: users users_approved_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: users users_default_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_default_project_id_fkey FOREIGN KEY (default_project_id) REFERENCES public.projects(id);


--
-- PostgreSQL database dump complete
--
