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
-- Name: btree_gist; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;


--
-- Name: EXTENSION btree_gist; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION btree_gist IS 'support for indexing common datatypes in GiST';


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
-- Name: notify_canvas_origin_session_change(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.notify_canvas_origin_session_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL THEN
        PERFORM pg_notify(
            'canvas_session_changes',
            json_build_object('kind', 'session', 'id', NEW.id)::text
        );
    ELSIF OLD.expires_at IS DISTINCT FROM NEW.expires_at THEN
        PERFORM pg_notify(
            'canvas_session_changes',
            json_build_object('kind', 'session_renewed', 'id', NEW.id)::text
        );
    END IF;
    RETURN NEW;
END;
$$;


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
-- Name: protect_infra_usage_day_state_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_infra_usage_day_state_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'open' THEN
            RAISE EXCEPTION
                'infrastructure usage day state must begin open'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'infrastructure usage day state cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'sealed' THEN
        RAISE EXCEPTION
            'sealed infrastructure usage days are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.day <> OLD.day
       OR NEW.updated_at < OLD.updated_at
       OR (OLD.state = 'open' AND NEW.state NOT IN ('open', 'sealing'))
       OR (OLD.state = 'sealing' AND NEW.state NOT IN ('sealing', 'sealed'))
    THEN
        RAISE EXCEPTION
            'infrastructure usage day state advances open to sealing to sealed'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END;
$$;


--
-- Name: protect_resource_interval_revision_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_resource_interval_revision_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'resource interval revisions are retained and cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF (to_jsonb(NEW)
            - 'ended_at' - 'end_time_source' - 'end_uncertainty_us'
            - 'end_reason' - 'last_seen_at' - 'last_confirmed_at'
            - 'last_seen_snapshot_id' - 'materialized_through' - 'updated_at')
       <> (to_jsonb(OLD)
            - 'ended_at' - 'end_time_source' - 'end_uncertainty_us'
            - 'end_reason' - 'last_seen_at' - 'last_confirmed_at'
            - 'last_seen_snapshot_id' - 'materialized_through' - 'updated_at') THEN
        RAISE EXCEPTION
            'event-affecting interval revision fields are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.last_seen_at < OLD.last_seen_at
       OR NEW.last_confirmed_at < OLD.last_confirmed_at
       OR NEW.materialized_through < OLD.materialized_through
       OR NEW.updated_at < OLD.updated_at
       OR (OLD.last_seen_snapshot_id IS NOT NULL
           AND NEW.last_seen_snapshot_id IS NULL) THEN
        RAISE EXCEPTION
            'interval liveness and materialization cursors are monotonic'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.ended_at IS NOT NULL THEN
        IF NEW.ended_at IS DISTINCT FROM OLD.ended_at
           OR NEW.end_time_source IS DISTINCT FROM OLD.end_time_source
           OR NEW.end_uncertainty_us IS DISTINCT FROM OLD.end_uncertainty_us
           OR NEW.end_reason IS DISTINCT FROM OLD.end_reason
           OR NEW.last_seen_at IS DISTINCT FROM OLD.last_seen_at
           OR NEW.last_confirmed_at IS DISTINCT FROM OLD.last_confirmed_at
           OR NEW.last_seen_snapshot_id IS DISTINCT FROM OLD.last_seen_snapshot_id
        THEN
            RAISE EXCEPTION
                'closed interval evidence and end metadata are immutable'
                USING ERRCODE = '55000';
        END IF;
    ELSIF NEW.ended_at IS NULL THEN
        IF NEW.end_time_source IS NOT NULL
           OR NEW.end_uncertainty_us IS NOT NULL
           OR NEW.end_reason IS NOT NULL THEN
            RAISE EXCEPTION
                'open intervals cannot carry end metadata'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;


--
-- Name: protect_resource_inventory_snapshot_item_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_resource_inventory_snapshot_item_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    old_state TEXT;
    new_state TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT manifest_state INTO new_state
        FROM public.resource_inventory_snapshots
        WHERE id = NEW.snapshot_id
        FOR UPDATE;
        IF new_state = 'staging' THEN
            RETURN NEW;
        END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.snapshot_id <> OLD.snapshot_id THEN
            RAISE EXCEPTION
                'snapshot items cannot move between manifests'
                USING ERRCODE = '55000';
        END IF;
        SELECT manifest_state INTO old_state
        FROM public.resource_inventory_snapshots
        WHERE id = OLD.snapshot_id
        FOR UPDATE;
        SELECT manifest_state INTO new_state
        FROM public.resource_inventory_snapshots
        WHERE id = NEW.snapshot_id
        FOR UPDATE;
        IF old_state = 'staging' AND new_state = 'staging' THEN
            RETURN NEW;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        SELECT manifest_state INTO old_state
        FROM public.resource_inventory_snapshots
        WHERE id = OLD.snapshot_id
        FOR UPDATE;
        IF old_state = 'items-expired' THEN
            RETURN OLD;
        END IF;
    END IF;

    RAISE EXCEPTION
        'sealed inventory snapshot items are immutable'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_resource_inventory_snapshot_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_resource_inventory_snapshot_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    actual_count BIGINT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.manifest_state = 'staging' THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'inventory snapshots must begin in the staging state'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.manifest_state = 'staging'
       AND NEW.manifest_state = 'sealed'
       AND NEW.sealed_at IS NOT NULL
       AND NEW.items_expired_at IS NULL
       AND NEW.id = OLD.id
       AND NEW.scope_epoch_id = OLD.scope_epoch_id
       AND NEW.inventory_scope_id = OLD.inventory_scope_id
       AND NEW.collection_started_at = OLD.collection_started_at
       AND NEW.created_at = OLD.created_at THEN
        SELECT count(*)
        INTO actual_count
        FROM public.resource_inventory_snapshot_items
        WHERE snapshot_id = NEW.id;

        IF actual_count <> NEW.item_count THEN
            RAISE EXCEPTION
                'snapshot % declares % items but has %',
                NEW.id, NEW.item_count, actual_count
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.manifest_state = 'sealed'
       AND NEW.manifest_state = 'items-expired'
       AND NEW.items_expired_at IS NOT NULL
       AND NEW.items_expired_at <= statement_timestamp()
       AND OLD.collection_completed_at
           <= statement_timestamp() - INTERVAL '7 days'
       AND (to_jsonb(NEW) - 'manifest_state' - 'items_expired_at')
           = (to_jsonb(OLD) - 'manifest_state' - 'items_expired_at') THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'snapshot metadata may only be finalized once or mark sealed items expired'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_resource_publication_plan_event_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_resource_publication_plan_event_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    parent_state TEXT;
    target_plan_id UUID;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'publication plan events are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'DELETE' THEN
        target_plan_id := OLD.plan_id;
    ELSE
        target_plan_id := NEW.plan_id;
    END IF;

    SELECT plan.state
    INTO parent_state
    FROM public.resource_publication_plans plan
    WHERE plan.id = target_plan_id
    FOR UPDATE;

    IF TG_OP = 'INSERT' AND parent_state = 'planned' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' AND parent_state = 'published' THEN
        RETURN OLD;
    END IF;

    RAISE EXCEPTION
        'plan events may be inserted while planned and deleted only for published retention cleanup'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_resource_publication_plan_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_resource_publication_plan_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    actual_count BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.state = 'published' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'only published plans may enter the reviewed retention cleanup path'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state <> 'planned' THEN
        RAISE EXCEPTION
            'published and conflict publication plans are terminal'
            USING ERRCODE = '55000';
    END IF;

    IF (to_jsonb(NEW)
            - 'state' - 'attempt_count' - 'last_attempt_at'
            - 'sanitized_error' - 'published_at')
       <> (to_jsonb(OLD)
            - 'state' - 'attempt_count' - 'last_attempt_at'
            - 'sanitized_error' - 'published_at') THEN
        RAISE EXCEPTION
            'publication plan intent and hashes are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.attempt_count < OLD.attempt_count
       OR (OLD.last_attempt_at IS NOT NULL AND NEW.last_attempt_at IS NULL)
       OR (OLD.last_attempt_at IS NOT NULL
           AND NEW.last_attempt_at < OLD.last_attempt_at)
       OR (NEW.attempt_count > OLD.attempt_count
           AND NEW.last_attempt_at IS NULL)
       OR (NEW.attempt_count = OLD.attempt_count
           AND NEW.last_attempt_at IS DISTINCT FROM OLD.last_attempt_at) THEN
        RAISE EXCEPTION
            'publication attempt state must advance monotonically'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.state = 'published' THEN
        SELECT count(*)
        INTO actual_count
        FROM public.resource_publication_plan_events event
        WHERE event.plan_id = NEW.id;
        IF actual_count <> NEW.expected_event_count THEN
            RAISE EXCEPTION
                'publication plan % cannot publish an incomplete manifest', NEW.id
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;


--
-- Name: protect_usage_rate_card_version_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_usage_rate_card_version_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.provider_effective_to IS NULL
       AND NEW.provider_effective_to IS NOT NULL
       AND (to_jsonb(NEW) - 'provider_effective_to')
           = (to_jsonb(OLD) - 'provider_effective_to') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        'usage rate-card version terms are immutable; close once and insert a successor'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_usage_rates_v2_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_usage_rates_v2_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.effective_to IS NULL
       AND NEW.effective_to IS NOT NULL
       AND (to_jsonb(NEW) - 'effective_to')
           = (to_jsonb(OLD) - 'effective_to') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        'usage_rates_v2 terms are immutable; close once and insert a successor'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: reconcile_datasource_project_policy_change(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reconcile_datasource_project_policy_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    affected_project_id UUID;
    affected_datasource_id UUID;
    current_revision BIGINT;
BEGIN
    affected_project_id := COALESCE(NEW.project_id, OLD.project_id);
    affected_datasource_id := COALESCE(NEW.datasource_id, OLD.datasource_id);

    UPDATE datasources
    SET policy_revision = policy_revision + 1,
        updated_at = NOW()
    WHERE id = affected_datasource_id
    RETURNING policy_revision INTO current_revision;

    -- A datasource cascade can remove the datasource row before this trigger
    -- runs. Policy revision remains useful diagnostic metadata; the separate
    -- sequence-backed claim_token is the actual stale-worker fence.
    current_revision := COALESCE(current_revision, 1);

    INSERT INTO datasource_project_reconcile_queue (
        project_id,
        datasource_id,
        policy_revision,
        attempts,
        next_attempt_at,
        last_error,
        updated_at
    ) VALUES (
        affected_project_id,
        affected_datasource_id,
        current_revision,
        0,
        NOW(),
        NULL,
        NOW()
    )
    ON CONFLICT (project_id, datasource_id) DO UPDATE
    SET policy_revision = GREATEST(
            datasource_project_reconcile_queue.policy_revision,
            EXCLUDED.policy_revision
        ),
        claim_token = nextval(
            'datasource_project_reconcile_generation_seq'
        ),
        attempts = 0,
        next_attempt_at = NOW(),
        last_error = NULL,
        updated_at = NOW();

    RETURN COALESCE(NEW, OLD);
END;
$$;


--
-- Name: reject_usage_rate_component_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reject_usage_rate_component_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    RAISE EXCEPTION
        'usage rate-card components are immutable; insert a successor version'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: revoke_canvas_sessions_for_bff_session(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.revoke_canvas_sessions_for_bff_session() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE canvas_origin_sessions
    SET revoked_at = COALESCE(revoked_at, now()),
        revocation_reason = COALESCE(revocation_reason, 'parent_session_ended'),
        parent_srw_session_id = NULL,
        updated_at = now()
    WHERE parent_srw_session_id = OLD.id
      AND revoked_at IS NULL;
    RETURN OLD;
END;
$$;


--
-- Name: revoke_canvas_sessions_for_retired_origin(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.revoke_canvas_sessions_for_retired_origin() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.origin_generation IS NOT NULL
       AND OLD.origin_generation IS DISTINCT FROM NEW.origin_generation THEN
        UPDATE canvas_origin_sessions
        SET revoked_at = COALESCE(revoked_at, now()),
            revocation_reason = COALESCE(revocation_reason, 'origin_retired'),
            updated_at = now()
        WHERE thread_id = OLD.thread_id
          AND canvas_id = OLD.canvas_id
          AND origin_generation = OLD.origin_generation
          AND revoked_at IS NULL;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: revoke_canvas_sessions_for_user_admission(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.revoke_canvas_sessions_for_user_admission() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.is_approved IS TRUE AND NEW.is_approved IS NOT TRUE THEN
        UPDATE canvas_origin_sessions
        SET revoked_at = COALESCE(revoked_at, now()),
            revocation_reason = COALESCE(revocation_reason, 'user_not_approved'),
            updated_at = now()
        WHERE user_id = NEW.id AND revoked_at IS NULL;
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


--
-- Name: validate_inventory_epoch_last_complete_snapshot(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.validate_inventory_epoch_last_complete_snapshot() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF NEW.last_complete_snapshot_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM public.resource_inventory_snapshots snapshot
           WHERE snapshot.id = NEW.last_complete_snapshot_id
             AND snapshot.scope_epoch_id = NEW.id
             AND snapshot.complete = TRUE
             AND snapshot.manifest_state IN ('sealed', 'items-expired')
       ) THEN
        RAISE EXCEPTION
            'last_complete_snapshot_id must reference a sealed complete snapshot in this epoch'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: validate_resource_interval_scope_identity(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.validate_resource_interval_scope_identity() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.resource_inventory_scopes scope
        WHERE scope.id = NEW.inventory_scope_id
          AND scope.source_cluster = NEW.source_cluster
          AND scope.namespace IS NOT DISTINCT FROM NEW.namespace
    ) THEN
        RAISE EXCEPTION
            'interval inventory scope does not match cluster/namespace identity'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.last_seen_snapshot_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM public.resource_inventory_snapshots snapshot
           WHERE snapshot.id = NEW.last_seen_snapshot_id
             AND snapshot.inventory_scope_id = NEW.inventory_scope_id
             AND snapshot.complete = TRUE
             AND snapshot.manifest_state IN ('sealed', 'items-expired')
       ) THEN
        RAISE EXCEPTION
            'last_seen_snapshot_id must reference a sealed complete snapshot in the interval scope'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: validate_resource_publication_plan_manifest(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.validate_resource_publication_plan_manifest() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    target_plan_id UUID;
    expected_count INTEGER;
    actual_count BIGINT;
    minimum_ordinal INTEGER;
    maximum_ordinal INTEGER;
BEGIN
    IF TG_TABLE_NAME = 'resource_publication_plans' THEN
        target_plan_id := NEW.id;
    ELSE
        target_plan_id := NEW.plan_id;
    END IF;

    SELECT plan.expected_event_count
    INTO expected_count
    FROM public.resource_publication_plans plan
    WHERE plan.id = target_plan_id;

    SELECT count(*), min(event.ordinal), max(event.ordinal)
    INTO actual_count, minimum_ordinal, maximum_ordinal
    FROM public.resource_publication_plan_events event
    WHERE event.plan_id = target_plan_id;

    IF expected_count IS NULL
       OR actual_count <> expected_count
       OR minimum_ordinal <> 0
       OR maximum_ordinal <> expected_count - 1 THEN
        RAISE EXCEPTION
            'publication plan % declares % contiguous events but has count %, ordinals %..%',
            target_plan_id, expected_count, actual_count,
            minimum_ordinal, maximum_ordinal
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;


--
-- Name: validate_usage_rate_card_component_count(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.validate_usage_rate_card_component_count() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    target_version_id UUID;
    expected_count INTEGER;
    actual_count BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'usage_rate_card_versions_v2' THEN
        target_version_id := NEW.id;
    ELSE
        target_version_id := NEW.version_id;
    END IF;

    SELECT component_count
    INTO expected_count
    FROM public.usage_rate_card_versions_v2
    WHERE id = target_version_id;

    SELECT count(*)
    INTO actual_count
    FROM public.usage_rate_components_v2
    WHERE version_id = target_version_id;

    IF expected_count IS NULL OR actual_count <> expected_count THEN
        RAISE EXCEPTION
            'rate-card version % declares % components but has %',
            target_version_id, expected_count, actual_count
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
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
-- Name: application_expert_defaults; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.application_expert_defaults (
    expert_type character varying(10) NOT NULL,
    expert_id uuid NOT NULL,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT application_expert_defaults_expert_type_check CHECK (((expert_type)::text = ANY ((ARRAY['worker'::character varying, 'session'::character varying])::text[])))
);


--
-- Name: TABLE application_expert_defaults; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.application_expert_defaults IS 'Exactly one DB expert selected by the operator for each root creation type.';


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
    expert_id uuid,
    CONSTRAINT automations_expert_source_check CHECK (((expert_id IS NULL) OR (expert = 'worker_base'::text))),
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
-- Name: COLUMN automations.expert_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.automations.expert_id IS 'Pinned DB-backed worker expert. NULL means `expert` is a bundled config name, or worker_base should resolve the owner''s effective default at fire time.';


--
-- Name: bench_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bench_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid NOT NULL,
    spec jsonb NOT NULL,
    state jsonb DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT bench_runs_spec_check CHECK ((jsonb_typeof(spec) = 'object'::text)),
    CONSTRAINT bench_runs_state_check CHECK ((jsonb_typeof(state) = 'array'::text)),
    CONSTRAINT bench_runs_status_check CHECK ((status = ANY (ARRAY['running'::text, 'paused'::text, 'done'::text, 'cancelled'::text])))
);


--
-- Name: canvas_origin_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.canvas_origin_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_secret_hash character varying(64) NOT NULL,
    user_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    canvas_id character varying(64) DEFAULT 'main'::character varying NOT NULL,
    parent_srw_session_id uuid,
    issued_presentation_revision bigint NOT NULL,
    source_fingerprint text NOT NULL,
    workspace_generation uuid NOT NULL,
    origin_generation uuid NOT NULL,
    embedding_origin text NOT NULL,
    cookie_mode character varying(32) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_renewed_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    revocation_reason character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_canvas_origin_session_cookie_mode CHECK (((cookie_mode)::text = ANY ((ARRAY['development-cookie-free'::character varying, 'psl-isolated'::character varying])::text[]))),
    CONSTRAINT ck_canvas_origin_session_hash CHECK (((session_secret_hash)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_canvas_origin_session_revision CHECK ((issued_presentation_revision > 0)),
    CONSTRAINT ck_canvas_origin_session_revocation CHECK (((revoked_at IS NULL) = (revocation_reason IS NULL)))
);


--
-- Name: TABLE canvas_origin_sessions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.canvas_origin_sessions IS 'Short-lived isolated Canvas gateway credentials; only SHA-256 secret hashes are persisted.';


--
-- Name: canvas_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.canvas_snapshots (
    thread_id uuid NOT NULL,
    canvas_id character varying(64) DEFAULT 'main'::character varying NOT NULL,
    path text NOT NULL,
    renderer character varying(32) NOT NULL,
    media_type text NOT NULL,
    source_version text NOT NULL,
    object_key text NOT NULL,
    byte_size bigint NOT NULL,
    last_modified timestamp with time zone,
    captured_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_canvas_snapshots_object_key CHECK (((char_length(object_key) >= 1) AND (char_length(object_key) <= 1024))),
    CONSTRAINT ck_canvas_snapshots_path_length CHECK (((char_length(path) >= 1) AND (char_length(path) <= 4096))),
    CONSTRAINT ck_canvas_snapshots_size CHECK ((byte_size > 0)),
    CONSTRAINT ck_canvas_snapshots_source_version CHECK ((source_version ~ '^sha256:[0-9a-f]{64}$'::text))
);


--
-- Name: TABLE canvas_snapshots; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.canvas_snapshots IS 'Last published bytes of a file Canvas, held in the object store. One row per Canvas, replaced on each publish. Read-only: never a write target and never merged back into the workspace.';


--
-- Name: COLUMN canvas_snapshots.renderer; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvas_snapshots.renderer IS 'Renderer the bytes were validated for. Deliberately unconstrained, matching canvases.renderer: renderer vocabulary stays app-enforced.';


--
-- Name: COLUMN canvas_snapshots.source_version; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvas_snapshots.source_version IS 'sha256 of the captured bytes. Served only while it equals the live canvases.source_version; any disagreement means stale and is ignored.';


--
-- Name: COLUMN canvas_snapshots.object_key; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvas_snapshots.object_key IS 'Object-store key, canvas/<thread_id>/<canvas_id>/<sha>. Thread-scoped rather than content-addressed so deletion is unambiguous and per-tenant.';


--
-- Name: canvas_view_attachments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.canvas_view_attachments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    canvas_id character varying(64) DEFAULT 'main'::character varying NOT NULL,
    parent_srw_session_id uuid,
    origin_session_id uuid,
    bridge_nonce_hash character varying(64) NOT NULL,
    embedding_origin text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    cookie_mode character varying(32) NOT NULL,
    CONSTRAINT ck_canvas_attachment_cookie_mode CHECK (((cookie_mode)::text = ANY ((ARRAY['development-cookie-free'::character varying, 'psl-isolated'::character varying])::text[]))),
    CONSTRAINT ck_canvas_attachment_nonce_hash CHECK (((bridge_nonce_hash)::text ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: TABLE canvas_view_attachments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.canvas_view_attachments IS 'Non-credential frame/window presence records linked to a shared origin session after bootstrap.';


--
-- Name: canvas_view_bootstraps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.canvas_view_bootstraps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    exchange_token_hash character varying(64),
    attachment_id uuid NOT NULL,
    expected_presentation_revision bigint NOT NULL,
    source_fingerprint text NOT NULL,
    workspace_generation uuid NOT NULL,
    origin_generation uuid NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    consumed_origin_session_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    challenge_hash character varying(64),
    browser_binding_hash character varying(64),
    ready_receipt_hash character varying(64),
    authorized_at timestamp with time zone,
    CONSTRAINT ck_canvas_bootstrap_binding_hash CHECK (((browser_binding_hash IS NULL) OR ((browser_binding_hash)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_canvas_bootstrap_challenge_hash CHECK (((challenge_hash IS NULL) OR ((challenge_hash)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_canvas_bootstrap_exchange_hash CHECK (((exchange_token_hash IS NULL) OR ((exchange_token_hash)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_canvas_bootstrap_exchange_state CHECK ((((challenge_hash IS NULL) AND (browser_binding_hash IS NULL) AND (ready_receipt_hash IS NULL) AND (exchange_token_hash IS NULL) AND (authorized_at IS NULL) AND (consumed_at IS NULL) AND (consumed_origin_session_id IS NULL)) OR ((challenge_hash IS NOT NULL) AND (browser_binding_hash IS NOT NULL) AND (ready_receipt_hash IS NOT NULL) AND (exchange_token_hash IS NULL) AND (authorized_at IS NULL) AND (consumed_at IS NULL) AND (consumed_origin_session_id IS NULL)) OR ((challenge_hash IS NOT NULL) AND (browser_binding_hash IS NOT NULL) AND (ready_receipt_hash IS NOT NULL) AND (exchange_token_hash IS NOT NULL) AND (authorized_at IS NOT NULL) AND (((consumed_at IS NULL) AND (consumed_origin_session_id IS NULL)) OR ((consumed_at IS NOT NULL) AND (consumed_origin_session_id IS NOT NULL)))))),
    CONSTRAINT ck_canvas_bootstrap_receipt_hash CHECK (((ready_receipt_hash IS NULL) OR ((ready_receipt_hash)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_canvas_bootstrap_revision CHECK ((expected_presentation_revision > 0))
);


--
-- Name: TABLE canvas_view_bootstraps; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.canvas_view_bootstraps IS 'Single-use Canvas bootstrap challenge/exchange state; every browser and authorization secret is stored only as a purpose-separated SHA-256 hash.';


--
-- Name: canvases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.canvases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    thread_id uuid NOT NULL,
    canvas_id character varying(64) DEFAULT 'main'::character varying NOT NULL,
    source jsonb,
    title text,
    renderer character varying(32) DEFAULT 'auto'::character varying NOT NULL,
    editable boolean DEFAULT false NOT NULL,
    alt_text text,
    presentation_revision bigint DEFAULT 0 NOT NULL,
    source_fingerprint text,
    source_version text,
    origin_generation uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_canvases_alt_text_length CHECK (((alt_text IS NULL) OR (char_length(alt_text) <= 1000))),
    CONSTRAINT ck_canvases_revision_nonnegative CHECK ((presentation_revision >= 0)),
    CONSTRAINT ck_canvases_source_shape CHECK ((((source IS NULL) AND (source_fingerprint IS NULL) AND (source_version IS NULL) AND (origin_generation IS NULL) AND (title IS NULL) AND (alt_text IS NULL) AND (editable = false) AND ((renderer)::text = 'auto'::text)) OR ((source IS NOT NULL) AND (source_fingerprint IS NOT NULL) AND (title IS NOT NULL)))),
    CONSTRAINT ck_canvases_title_length CHECK (((title IS NULL) OR (char_length(title) <= 200)))
);


--
-- Name: TABLE canvases; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.canvases IS 'Thread-scoped Dynamic Canvas presentation pointers; source content is not copied here.';


--
-- Name: COLUMN canvases.canvas_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvases.canvas_id IS 'Presentation slot. V1 application services accept only main.';


--
-- Name: COLUMN canvases.source; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvases.source IS 'Server-normalized logical source; never contains credentials, proxy URLs, or workspace addresses.';


--
-- Name: COLUMN canvases.presentation_revision; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvases.presentation_revision IS 'Monotonic presentation-domain revision, advanced once per successful state transition.';


--
-- Name: COLUMN canvases.source_fingerprint; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvases.source_fingerprint IS 'sha256 fingerprint of the canonical security-relevant logical source identity.';


--
-- Name: COLUMN canvases.source_version; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvases.source_version IS 'Strong sha256 content version for file-backed sources; distinct from presentation_revision.';


--
-- Name: COLUMN canvases.origin_generation; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvases.origin_generation IS 'Random revocable browser-origin generation for a live application trust unit.';


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
-- Name: cloud_ro_mounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cloud_ro_mounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    thread_id uuid NOT NULL,
    user_id uuid NOT NULL,
    backend text NOT NULL,
    reader_id text NOT NULL,
    grant_handle text NOT NULL,
    credentials text,
    webdav_url text NOT NULL,
    auth_kind text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    etag_baseline jsonb,
    staged_epoch integer DEFAULT 0 NOT NULL,
    staged_at timestamp with time zone,
    staged_summary jsonb
);


--
-- Name: TABLE cloud_ro_mounts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.cloud_ro_mounts IS 'Per-mount read-only reader grants for protected cloud mode. One row per protected session mount; the reconciler revokes active grants whose thread is gone. Credentials are encrypted at rest (postgres._encrypt_optional).';


--
-- Name: COLUMN cloud_ro_mounts.etag_baseline; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cloud_ro_mounts.etag_baseline IS 'path->etag map (files only) captured at engage, re-captured after each apply';


--
-- Name: COLUMN cloud_ro_mounts.staged_epoch; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cloud_ro_mounts.staged_epoch IS 'monotonic staging epoch: bumped on every successful stage push, apply, and reject';


--
-- Name: COLUMN cloud_ro_mounts.staged_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cloud_ro_mounts.staged_at IS 'when the current epoch was pushed; NULL when nothing staged';


--
-- Name: COLUMN cloud_ro_mounts.staged_summary; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.cloud_ro_mounts.staged_summary IS 'manifest counts + content signature for the current epoch (entry lists live in S3); NULL when nothing staged';


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
-- Name: contact_addresses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_addresses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    contact_id uuid NOT NULL,
    owner_user_id uuid NOT NULL,
    channel text NOT NULL,
    address text NOT NULL,
    is_primary boolean DEFAULT false NOT NULL,
    opt_in_status text DEFAULT 'pending'::text NOT NULL,
    last_inbound_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT contact_addresses_channel_check CHECK ((channel = ANY (ARRAY['email'::text, 'whatsapp'::text]))),
    CONSTRAINT contact_addresses_opt_in_status_check CHECK ((opt_in_status = ANY (ARRAY['pending'::text, 'opted_in'::text, 'opted_out'::text])))
);


--
-- Name: contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    owner_user_id uuid NOT NULL,
    display_name text NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: datasource_project_reconcile_generation_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.datasource_project_reconcile_generation_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: datasource_project_reconcile_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.datasource_project_reconcile_queue (
    project_id uuid NOT NULL,
    datasource_id uuid NOT NULL,
    policy_revision bigint NOT NULL,
    claim_token bigint DEFAULT nextval('public.datasource_project_reconcile_generation_seq'::regclass) NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT datasource_project_reconcile_queue_attempts_check CHECK ((attempts >= 0))
);


--
-- Name: TABLE datasource_project_reconcile_queue; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.datasource_project_reconcile_queue IS 'Durable coalescing queue for syncing datasource/project link state to external knowledge stores.';


--
-- Name: COLUMN datasource_project_reconcile_queue.claim_token; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.datasource_project_reconcile_queue.claim_token IS 'Never-reused sequence token rotated on enqueue and claim; guards stale worker completion.';


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
    project_id uuid,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    read_only boolean,
    scope_mode text DEFAULT 'all'::text NOT NULL,
    auto_attach boolean DEFAULT false NOT NULL,
    policy_revision bigint DEFAULT 1 NOT NULL,
    CONSTRAINT datasources_policy_revision_positive CHECK ((policy_revision > 0)),
    CONSTRAINT datasources_scope_mode_check CHECK ((scope_mode = ANY (ARRAY['all'::text, 'projects'::text])))
);


--
-- Name: COLUMN datasources.config; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.datasources.config IS 'Non-secret type-specific datasource configuration. Credentials and tokens must not be stored here.';


--
-- Name: COLUMN datasources.read_only; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.datasources.read_only IS 'Declared read-only flag for public (is_global) datasources. NULL = not applicable. Declarative: credentials are the enforcement boundary; kb datasources are read-only by architecture.';


--
-- Name: COLUMN datasources.scope_mode; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.datasources.scope_mode IS 'Execution availability upper bound: all or every selected work project.';


--
-- Name: COLUMN datasources.auto_attach; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.datasources.auto_attach IS 'Creator-owned creation-time default; never a runtime force attachment.';


--
-- Name: COLUMN datasources.policy_revision; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.datasources.policy_revision IS 'Optimistic concurrency token for scope/default/project-link policy.';


--
-- Name: docker_workspace_leases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.docker_workspace_leases (
    host text NOT NULL,
    port integer NOT NULL,
    status text NOT NULL,
    lease_id uuid,
    owner_kind text,
    owner_id uuid,
    trust_mode text DEFAULT 'unattested'::text NOT NULL,
    host_key_fingerprint text,
    quarantine_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_docker_workspace_attested_fingerprint CHECK (((trust_mode <> 'attested'::text) OR (host_key_fingerprint IS NOT NULL))),
    CONSTRAINT ck_docker_workspace_lease_fingerprint CHECK (((host_key_fingerprint IS NULL) OR ((host_key_fingerprint ~~ 'SHA256:%'::text) AND (char_length(host_key_fingerprint) <= 128) AND (host_key_fingerprint !~ '[[:space:]]'::text)))),
    CONSTRAINT ck_docker_workspace_lease_host CHECK (((host = btrim(host)) AND (host <> ''::text) AND (char_length(host) <= 255) AND (host !~ '[[:cntrl:]]'::text))),
    CONSTRAINT ck_docker_workspace_lease_owner_kind CHECK (((owner_kind IS NULL) OR (owner_kind = ANY (ARRAY['job'::text, 'thread'::text])))),
    CONSTRAINT ck_docker_workspace_lease_owner_pair CHECK (((owner_kind IS NULL) = (owner_id IS NULL))),
    CONSTRAINT ck_docker_workspace_lease_port CHECK (((port >= 1) AND (port <= 65535))),
    CONSTRAINT ck_docker_workspace_lease_status CHECK ((status = ANY (ARRAY['ready'::text, 'releasing'::text, 'released'::text, 'quarantined'::text]))),
    CONSTRAINT ck_docker_workspace_lease_trust_mode CHECK ((trust_mode = ANY (ARRAY['unattested'::text, 'trusted_dev'::text, 'attested'::text]))),
    CONSTRAINT ck_docker_workspace_live_lease_shape CHECK (((status <> ALL (ARRAY['ready'::text, 'releasing'::text])) OR ((owner_kind IS NOT NULL) AND (owner_id IS NOT NULL) AND (lease_id IS NOT NULL))))
);


--
-- Name: TABLE docker_workspace_leases; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.docker_workspace_leases IS 'Durable endpoint authority for pre-provisioned Docker workspaces. No owner FK by design: quarantine survives deleted jobs/threads.';


--
-- Name: COLUMN docker_workspace_leases.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.docker_workspace_leases.status IS 'Only released inventory may be allocated; ready/releasing/quarantined remains occupied even after owner deletion.';


--
-- Name: COLUMN docker_workspace_leases.trust_mode; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.docker_workspace_leases.trust_mode IS 'unattested, explicit same-trust trusted_dev, or controller/bootstrap attested. Existing rows are never promoted by configuration alone.';


--
-- Name: COLUMN docker_workspace_leases.host_key_fingerprint; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.docker_workspace_leases.host_key_fingerprint IS 'Exact provisioner-attested Ed25519 SHA-256 identity for attested inventory; public-key metadata, never a private key.';


--
-- Name: COLUMN docker_workspace_leases.quarantine_reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.docker_workspace_leases.quarantine_reason IS 'Operator-visible recovery reason. First discovery without explicit bootstrap attestation is permanent quarantine until a controller/manual recreation attests the endpoint.';


--
-- Name: expert_default_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.expert_default_audit (
    id bigint NOT NULL,
    actor_user_id uuid,
    target_user_id uuid,
    target_project_id uuid,
    expert_type character varying(10) NOT NULL,
    scope_kind character varying(20) NOT NULL,
    old_expert_id uuid,
    new_expert_id uuid,
    action character varying(20) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT expert_default_audit_action_check CHECK (((action)::text = ANY ((ARRAY['set'::character varying, 'clear'::character varying, 'seed'::character varying, 'update'::character varying])::text[]))),
    CONSTRAINT expert_default_audit_expert_type_check CHECK (((expert_type)::text = ANY ((ARRAY['worker'::character varying, 'session'::character varying])::text[]))),
    CONSTRAINT expert_default_audit_scope_kind_check CHECK (((scope_kind)::text = ANY ((ARRAY['application'::character varying, 'user'::character varying, 'project'::character varying, 'managed'::character varying])::text[])))
);


--
-- Name: expert_default_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.expert_default_audit ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.expert_default_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
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
    owner_id uuid,
    is_global boolean DEFAULT false NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    managed_key character varying(100),
    seed_version integer,
    CONSTRAINT experts_expert_type_check CHECK (((expert_type)::text = ANY ((ARRAY['worker'::character varying, 'session'::character varying])::text[]))),
    CONSTRAINT experts_managed_owner_check CHECK ((((managed_key IS NULL) AND (owner_id IS NOT NULL)) OR ((managed_key IS NOT NULL) AND (owner_id IS NULL) AND (is_global = true))))
);


--
-- Name: TABLE experts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.experts IS 'DB-backed user/admin experts (overlay over bundled config/experts/). config = fragment vs the expert_type base; prompts = {persona, instructions, strategic, tactical, summarization} (Part 2 — one family-agnostic version per segment; model adaptation stays in the systemprompt_<family> wrapper). Design: docs/features/global_expert_management.md.';


--
-- Name: COLUMN experts.managed_key; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.experts.managed_key IS 'Stable platform seed identity. Managed rows are global, ownerless and non-deletable.';


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
-- Name: infra_metering_control; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.infra_metering_control (
    singleton boolean DEFAULT true NOT NULL,
    leader_generation bigint DEFAULT 0 NOT NULL,
    cutover_state text DEFAULT 'disabled'::text NOT NULL,
    cutover_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT infra_metering_control_cutover_check CHECK ((((cutover_state = 'disabled'::text) AND (cutover_at IS NULL)) OR ((cutover_state = ANY (ARRAY['preparing'::text, 'active'::text])) AND (cutover_at IS NOT NULL)))),
    CONSTRAINT infra_metering_control_generation_check CHECK ((leader_generation >= 0)),
    CONSTRAINT infra_metering_control_singleton_check CHECK (singleton)
);


--
-- Name: infra_usage_day_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.infra_usage_day_state (
    day date NOT NULL,
    state text DEFAULT 'open'::text NOT NULL,
    coverage_status text,
    coverage_revision text,
    unknown_ranges jsonb DEFAULT '[]'::jsonb NOT NULL,
    sealed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT infra_usage_day_state_shape_check CHECK (((state = ANY (ARRAY['open'::text, 'sealing'::text, 'sealed'::text])) AND ((coverage_status IS NULL) OR (coverage_status = ANY (ARRAY['complete'::text, 'partial'::text]))) AND ((coverage_status IS NULL) = (coverage_revision IS NULL)) AND ((coverage_revision IS NULL) OR (coverage_revision <> ''::text)) AND (jsonb_typeof(unknown_ranges) = 'array'::text) AND (((state = 'sealed'::text) AND (coverage_status IS NOT NULL) AND (coverage_revision IS NOT NULL) AND (sealed_at IS NOT NULL)) OR ((state <> 'sealed'::text) AND (sealed_at IS NULL)))))
);


--
-- Name: job_change_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_change_records (
    job_id uuid NOT NULL,
    project_id uuid,
    loop_id uuid,
    record_type character varying(32) NOT NULL,
    role character varying(200) NOT NULL,
    iteration integer,
    status character varying(50) NOT NULL,
    repo_name character varying(200),
    branch_name character varying(200),
    delivery_status character varying(50) DEFAULT 'none'::character varying NOT NULL,
    delivery_ref text,
    delivery_sha text,
    completion_notes text DEFAULT ''::text NOT NULL,
    delivery_notes jsonb DEFAULT '[]'::jsonb NOT NULL,
    changes jsonb DEFAULT '[]'::jsonb NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT job_change_records_changes_check CHECK ((jsonb_typeof(changes) = 'array'::text)),
    CONSTRAINT job_change_records_delivery_notes_check CHECK ((jsonb_typeof(delivery_notes) = 'array'::text)),
    CONSTRAINT job_change_records_record_type_check CHECK (((record_type)::text = ANY ((ARRAY['job_record'::character varying, 'loop_record'::character varying])::text[])))
);


--
-- Name: TABLE job_change_records; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.job_change_records IS 'One immutable terminal outcome per job. Replaces retros/*.md in shared project jobs repositories; PostgreSQL is the project-history authority.';


--
-- Name: COLUMN job_change_records.job_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_change_records.job_id IS 'Execution job identifier without a foreign key: project history survives job-row and isolated-repository cleanup.';


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
    runner_kind text DEFAULT 'user'::text NOT NULL,
    lease_expires_at timestamp with time zone,
    created_by_thread_id uuid,
    wake_on_complete boolean DEFAULT false NOT NULL,
    wake_state text DEFAULT 'none'::text NOT NULL,
    wake_claimed_at timestamp with time zone,
    wake_attempts integer DEFAULT 0 NOT NULL,
    wake_notified_status text,
    failed_at timestamp with time zone,
    CONSTRAINT jobs_diff_status_check CHECK (((diff_status IS NULL) OR (diff_status = ANY (ARRAY['pending'::text, 'accepted'::text, 'rejected'::text])))),
    CONSTRAINT jobs_runner_kind_check CHECK ((runner_kind = ANY (ARRAY['user'::text, 'lifecycle'::text, 'service'::text]))),
    CONSTRAINT jobs_wake_state_known CHECK ((wake_state = ANY (ARRAY['none'::text, 'pending'::text, 'sending'::text, 'sent'::text, 'dead'::text]))),
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
-- Name: COLUMN jobs.runner_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.runner_kind IS 'Dispatch runner class. user = owner grants; lifecycle = system subjob with owner capabilities and full autonomy ceiling; service = reserved for ownerless system jobs.';


--
-- Name: COLUMN jobs.created_by_thread_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.created_by_thread_id IS 'Session thread that created this job (NULL for cockpit/automation/child jobs). Queryable backref powering the completion wake and the session''s own "my outstanding jobs" view. Design: docs/features/session_wake_on_job_completion.md.';


--
-- Name: COLUMN jobs.wake_state; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.wake_state IS 'Wake outbox state: none|pending|sending|sent|dead. Claimed by an atomic UPDATE ... FOR UPDATE SKIP LOCKED before the (non-idempotent) send.';


--
-- Name: COLUMN jobs.wake_notified_status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.wake_notified_status IS 'Terminal status last delivered to the creating session. Second half of the (job_id, terminal_status) dedup key — a later, different terminal status (pending_review → completed via approve) is a legitimate second wake.';


--
-- Name: COLUMN jobs.failed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.failed_at IS 'When the job entered ''failed'', set by update_job_status on the transition. Use this, NEVER updated_at, to date a failure: the update_jobs_updated_at trigger fires on FK cascades from gc_offline_agents, which rewrites updated_at to exactly 24h after the assigned agent''s last heartbeat. NULL on rows that failed before migration 0072 — the time is genuinely unknown, not zero. Design: docs/superpowers/specs/2026-07-28-transient-infra-failure-handling-design.md.';


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
    j.total_requests,
    j.error_message,
    j.runner_kind
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
    thread_id character varying(64) NOT NULL,
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
-- Name: COLUMN message_log.thread_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_log.thread_id IS 'Conversation key. Job messages use a short hex token (email threading), loop events use ''loop-'' + 6 chars, officer pages use the persistent session thread UUID (job_id is NULL on those rows — the jobs FK forbids storing a thread id there, and the session log is the reply channel).';


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
    thread_id character varying(64),
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
-- Name: project_contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_contacts (
    project_id uuid NOT NULL,
    contact_id uuid NOT NULL,
    added_by uuid,
    created_at timestamp with time zone DEFAULT now()
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
    current_stage_jobs jsonb DEFAULT '[]'::jsonb NOT NULL,
    scheduling text DEFAULT 'standard'::text NOT NULL,
    campaign jsonb,
    campaign_history jsonb DEFAULT '[]'::jsonb NOT NULL,
    campaign_caps jsonb,
    CONSTRAINT project_loop_campaign_caps_is_object CHECK (((campaign_caps IS NULL) OR (jsonb_typeof(campaign_caps) = 'object'::text))),
    CONSTRAINT project_loop_campaign_history_is_array CHECK ((jsonb_typeof(campaign_history) = 'array'::text)),
    CONSTRAINT project_loop_campaign_is_object CHECK (((campaign IS NULL) OR (jsonb_typeof(campaign) = 'object'::text))),
    CONSTRAINT project_loop_has_budget CHECK (((max_iterations IS NOT NULL) OR (run_until IS NOT NULL) OR (scheduling = 'officer'::text))),
    CONSTRAINT project_loop_role_sequence_nonempty CHECK (((jsonb_typeof(role_sequence) = 'array'::text) AND (jsonb_array_length(role_sequence) >= 1))),
    CONSTRAINT project_loop_scheduling_known CHECK ((scheduling = ANY (ARRAY['standard'::text, 'campaign'::text, 'officer'::text]))),
    CONSTRAINT project_loop_stage_jobs_is_array CHECK ((jsonb_typeof(current_stage_jobs) = 'array'::text)),
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
-- Name: COLUMN project_loops.current_job_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.current_job_id IS 'Display-only mirror of the in-flight turn when its width is 1 (cockpit links, MCP formatters). NULL for fan-out turns and between turns. The engine''s advance/heal correctness keys on current_stage_jobs, never on this column. docs/features/loop_unified_engine.md.';


--
-- Name: COLUMN project_loops.stop_reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.stop_reason IS 'Why the loop ended: budget (iterations exhausted), deadline (run_until passed), failures (consecutive-failure cap), goal_met (Critic decided), or user (manual stop).';


--
-- Name: COLUMN project_loops.workspace_backend; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.workspace_backend IS 'Optional per-loop workspace tier override. NULL = each spawned job uses the default (sandbox). When set, create_loop_job injects config_override.workspace.backend for every job — e.g. ''vm'' gives every role a root VM. Mirrors the per-loop model override.';


--
-- Name: COLUMN project_loops.current_stage_jobs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.current_stage_jobs IS 'In-flight members of the loop''s current turn — the jobs the loop barriers on before rotating, width 1 included (the unified engine''s only advance path). Populated by the advance/start spawn; drained to [] by the atomic last-member barrier, which also nulls current_job_id so the torn-advance signature stays current_job_id IS NULL AND current_stage_jobs = ''[]''. docs/features/loop_unified_engine.md.';


--
-- Name: COLUMN project_loops.scheduling; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.scheduling IS 'Scheduling mode: standard (the role_sequence stage list, one stage per turn — subsumes the old rotation mode and its fan-out stages) or campaign (a checkpoint Critic may expand the execution slot into a multi-stage campaign via a filed plan; formerly planner). Start-time-only. docs/features/loop_unified_engine.md.';


--
-- Name: COLUMN project_loops.campaign; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.campaign IS 'Active campaign control state (planner loops only): stages, cursor, counters, acceptance evidence, status active|review|aborted. Recovery truth lives in member-job context stamps (loop_campaign_id/_index), not here. NULL when no campaign has been planned.';


--
-- Name: COLUMN project_loops.campaign_history; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.campaign_history IS 'Bounded archive of disposed campaigns (outcome ship|kill|abort + counters), newest last, capped app-side.';


--
-- Name: COLUMN project_loops.campaign_caps; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_loops.campaign_caps IS 'Optional per-loop guardrail overrides: {max_stages, max_extensions, abort_failures}. NULL = config defaults. Validated against hard ceilings at loop start.';


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
    CONSTRAINT valid_repo_role CHECK (((role)::text = ANY ((ARRAY['jobs'::character varying, 'source'::character varying, 'reference'::character varying, 'knowledge'::character varying])::text[])))
);


--
-- Name: COLUMN project_repositories.role; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_repositories.role IS 'jobs = the per-project job/output repo (one per project); source = a working code repo; reference = read-only context; knowledge = the dedicated KB vault repo (one per project). Projects without a knowledge repo fall back to the jobs repo for KB resolution.';


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
-- Name: resource_intervals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_intervals (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    inventory_scope_id uuid NOT NULL,
    source_cluster text NOT NULL,
    source_kind text NOT NULL,
    source_uid text NOT NULL,
    source_api_version text NOT NULL,
    source_resource_version text,
    source_lifecycle_id uuid NOT NULL,
    revision_no bigint NOT NULL,
    source_revision text NOT NULL,
    namespace text,
    name text NOT NULL,
    category text NOT NULL,
    resource text NOT NULL,
    measurement_basis text NOT NULL,
    cost_domain text NOT NULL,
    resource_class text NOT NULL,
    attribution_scope text NOT NULL,
    owner_kind text,
    owner_id text,
    user_id uuid,
    project_id uuid,
    attribution_source text NOT NULL,
    attribution_quality text NOT NULL,
    backing_resource_uid text,
    lifecycle_confidence text NOT NULL,
    cpu_millicores bigint,
    memory_bytes bigint,
    storage_bytes bigint,
    capacity_source text NOT NULL,
    capacity_quality text NOT NULL,
    measurement_algorithm text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    start_time_source text NOT NULL,
    start_uncertainty_us bigint NOT NULL,
    ended_at timestamp with time zone,
    end_time_source text,
    end_uncertainty_us bigint,
    last_seen_at timestamp with time zone NOT NULL,
    last_confirmed_at timestamp with time zone NOT NULL,
    last_seen_snapshot_id uuid,
    materialized_through timestamp with time zone NOT NULL,
    end_reason text,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_intervals_attribution_check CHECK ((((attribution_scope = 'customer'::text) AND (owner_kind IS NOT NULL) AND (owner_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (owner_id IS NOT NULL) AND (owner_id <> ''::text) AND (user_id IS NOT NULL) AND (attribution_quality = ANY (ARRAY['exact'::text, 'derived'::text]))) OR ((attribution_scope = 'shared-platform'::text) AND (user_id IS NULL) AND (project_id IS NULL) AND (attribution_quality = ANY (ARRAY['exact'::text, 'derived'::text]))) OR ((attribution_scope = 'unknown'::text) AND (user_id IS NULL) AND (project_id IS NULL) AND (attribution_quality = ANY (ARRAY['ambiguous'::text, 'unknown'::text]))))),
    CONSTRAINT resource_intervals_capacity_check CHECK (((revision_no > 0) AND ((cpu_millicores IS NULL) OR (cpu_millicores >= 0)) AND ((memory_bytes IS NULL) OR (memory_bytes >= 0)) AND ((storage_bytes IS NULL) OR (storage_bytes >= 0)) AND (((category = 'compute'::text) AND (cpu_millicores IS NOT NULL) AND (memory_bytes IS NOT NULL) AND (storage_bytes IS NULL)) OR ((category = 'storage'::text) AND (storage_bytes IS NOT NULL) AND (cpu_millicores IS NULL) AND (memory_bytes IS NULL))))),
    CONSTRAINT resource_intervals_cursor_check CHECK (((last_seen_at >= started_at) AND (last_confirmed_at >= started_at) AND (materialized_through >= started_at) AND (materialized_through <= COALESCE(ended_at, last_confirmed_at)))),
    CONSTRAINT resource_intervals_details_check CHECK ((jsonb_typeof(details) = 'object'::text)),
    CONSTRAINT resource_intervals_dimension_check CHECK (((source_kind = ANY (ARRAY['pod'::text, 'vmi'::text, 'pvc'::text, 'volume'::text])) AND (category = ANY (ARRAY['compute'::text, 'storage'::text])) AND (measurement_basis = ANY (ARRAY['scheduler-request'::text, 'guest-provisioned'::text, 'claim-requested'::text, 'volume-provisioned'::text])) AND (cost_domain = ANY (ARRAY['workload-allocation'::text, 'physical-asset'::text, 'idle'::text, 'overhead'::text])) AND (attribution_scope = ANY (ARRAY['customer'::text, 'shared-platform'::text, 'unknown'::text])) AND ((owner_kind IS NULL) OR (owner_kind = ANY (ARRAY['job'::text, 'thread'::text, 'platform'::text, 'unknown'::text]))) AND (((source_kind = 'pod'::text) AND (category = 'compute'::text) AND (measurement_basis = 'scheduler-request'::text) AND (resource_class = 'kubernetes-pod'::text) AND (cost_domain = 'workload-allocation'::text)) OR ((source_kind = 'vmi'::text) AND (category = 'compute'::text) AND (measurement_basis = 'guest-provisioned'::text) AND (resource_class = 'virtual-machine'::text) AND (cost_domain = 'workload-allocation'::text)) OR ((source_kind = 'pvc'::text) AND (category = 'storage'::text) AND (measurement_basis = 'claim-requested'::text) AND (resource_class = 'persistent-volume-claim'::text) AND (cost_domain = 'workload-allocation'::text)) OR ((source_kind = 'volume'::text) AND (category = 'storage'::text) AND (measurement_basis = 'volume-provisioned'::text) AND (resource_class = 'persistent-volume'::text) AND (cost_domain = 'physical-asset'::text))))),
    CONSTRAINT resource_intervals_end_metadata_check CHECK ((((ended_at IS NULL) AND (end_time_source IS NULL) AND (end_uncertainty_us IS NULL) AND (end_reason IS NULL)) OR ((ended_at IS NOT NULL) AND (end_time_source IS NOT NULL) AND (end_uncertainty_us IS NOT NULL) AND (end_reason IS NOT NULL)))),
    CONSTRAINT resource_intervals_identity_check CHECK (((source_cluster <> ''::text) AND (source_uid <> ''::text) AND (source_api_version <> ''::text) AND (name <> ''::text) AND (resource <> ''::text) AND (resource_class <> ''::text) AND (attribution_source <> ''::text) AND (attribution_quality <> ''::text) AND (lifecycle_confidence <> ''::text) AND (capacity_source <> ''::text) AND (capacity_quality <> ''::text) AND (measurement_algorithm <> ''::text) AND (start_time_source <> ''::text) AND (source_revision ~ '^[0-9a-f]{64}$'::text) AND ((namespace IS NULL) OR (namespace <> ''::text)))),
    CONSTRAINT resource_intervals_quality_check CHECK (((attribution_quality = ANY (ARRAY['exact'::text, 'derived'::text, 'ambiguous'::text, 'unknown'::text, 'invalid'::text])) AND (capacity_quality = ANY (ARRAY['exact'::text, 'derived'::text, 'conservative'::text, 'resize-status-unavailable'::text, 'unsupported'::text, 'unknown'::text, 'invalid'::text])) AND (attribution_quality <> 'invalid'::text) AND (capacity_quality <> ALL (ARRAY['unsupported'::text, 'unknown'::text, 'invalid'::text])) AND (lifecycle_confidence = ANY (ARRAY['backend-confirmed'::text, 'kubernetes-visible'::text, 'backend-unverified'::text])))),
    CONSTRAINT resource_intervals_time_check CHECK (((start_uncertainty_us >= 0) AND ((end_uncertainty_us IS NULL) OR (end_uncertainty_us >= 0)) AND ((ended_at IS NULL) OR (ended_at >= started_at))))
);


--
-- Name: TABLE resource_intervals; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_intervals IS 'App-owned allocation interval revisions; immutable capacity/dimensions with mutable liveness/materialization cursor.';


--
-- Name: resource_inventory_coverage_gaps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_inventory_coverage_gaps (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    scope_epoch_id uuid NOT NULL,
    gap_start timestamp with time zone NOT NULL,
    gap_end timestamp with time zone,
    reason text NOT NULL,
    resolution text DEFAULT 'unresolved'::text NOT NULL,
    resolution_details jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolved_at timestamp with time zone,
    resolved_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_inventory_coverage_gaps_range_check CHECK (((gap_end IS NULL) OR (gap_end > gap_start))),
    CONSTRAINT resource_inventory_coverage_gaps_resolution_check CHECK (((reason <> ''::text) AND (resolution = ANY (ARRAY['unresolved'::text, 'backfilled'::text, 'waived'::text])) AND (jsonb_typeof(resolution_details) = 'object'::text) AND (((resolution = 'unresolved'::text) AND (resolved_at IS NULL) AND (resolved_by IS NULL)) OR ((resolution = 'backfilled'::text) AND (resolved_at IS NOT NULL)) OR ((resolution = 'waived'::text) AND (resolved_at IS NOT NULL) AND (resolved_by IS NOT NULL)))))
);


--
-- Name: resource_inventory_scope_epochs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_inventory_scope_epochs (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    scope_id uuid NOT NULL,
    epoch_number bigint NOT NULL,
    reliable_from timestamp with time zone,
    required_for_rollup boolean DEFAULT false NOT NULL,
    required_from timestamp with time zone,
    retired_at timestamp with time zone,
    coverage_mode text NOT NULL,
    capture_epoch uuid,
    last_attempt_at timestamp with time zone,
    last_complete_at timestamp with time zone,
    last_complete_snapshot_id uuid,
    last_resource_version text,
    controller_epoch text,
    last_sequence bigint,
    leader_generation bigint DEFAULT 0 NOT NULL,
    continuous_since timestamp with time zone,
    complete_through timestamp with time zone,
    snapshot_health text DEFAULT 'initializing'::text NOT NULL,
    continuity_health text DEFAULT 'initializing'::text NOT NULL,
    item_health text DEFAULT 'initializing'::text NOT NULL,
    backend_health text DEFAULT 'initializing'::text NOT NULL,
    publication_health text DEFAULT 'initializing'::text NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    last_item_count integer,
    sanitized_error jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_inventory_scope_epochs_health_check CHECK (((coverage_mode <> ''::text) AND (snapshot_health <> ''::text) AND (continuity_health <> ''::text) AND (item_health <> ''::text) AND (backend_health <> ''::text) AND (publication_health <> ''::text) AND (leader_generation >= 0) AND (consecutive_failures >= 0) AND ((last_sequence IS NULL) OR (last_sequence >= 0)) AND ((last_item_count IS NULL) OR (last_item_count >= 0)) AND ((sanitized_error IS NULL) OR (jsonb_typeof(sanitized_error) = 'object'::text)))),
    CONSTRAINT resource_inventory_scope_epochs_midnight_check CHECK (((required_from IS NULL) OR (required_from = date_trunc('day'::text, required_from, 'UTC'::text)))),
    CONSTRAINT resource_inventory_scope_epochs_number_check CHECK ((epoch_number > 0)),
    CONSTRAINT resource_inventory_scope_epochs_requirement_check CHECK (((required_for_rollup AND (required_from IS NOT NULL) AND (reliable_from IS NOT NULL) AND (required_from >= reliable_from)) OR ((NOT required_for_rollup) AND (required_from IS NULL)))),
    CONSTRAINT resource_inventory_scope_epochs_retirement_check CHECK (((retired_at IS NULL) OR (((reliable_from IS NULL) OR (retired_at >= reliable_from)) AND ((required_from IS NULL) OR (retired_at > required_from)) AND ((continuous_since IS NULL) OR (retired_at >= continuous_since)) AND ((complete_through IS NULL) OR (retired_at >= complete_through)))))
);


--
-- Name: resource_inventory_scopes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_inventory_scopes (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    collector_id text NOT NULL,
    source_cluster text NOT NULL,
    api_resource text NOT NULL,
    namespace text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_inventory_scopes_nonempty_check CHECK (((collector_id <> ''::text) AND (source_cluster <> ''::text) AND (api_resource <> ''::text) AND ((namespace IS NULL) OR (namespace <> ''::text))))
);


--
-- Name: TABLE resource_inventory_scopes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_inventory_scopes IS 'Stable metering inventory scope identity; effective requirements live in scope epochs.';


--
-- Name: resource_inventory_snapshot_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_inventory_snapshot_items (
    snapshot_id uuid NOT NULL,
    source_kind text NOT NULL,
    source_uid text NOT NULL,
    revision_hash text,
    normalized_item jsonb NOT NULL,
    valid_for_metering boolean NOT NULL,
    item_error jsonb,
    CONSTRAINT resource_inventory_snapshot_items_shape_check CHECK (((source_kind <> ''::text) AND (source_uid <> ''::text) AND ((revision_hash IS NULL) OR (revision_hash ~ '^[0-9a-f]{64}$'::text)) AND (jsonb_typeof(normalized_item) = 'object'::text) AND ((item_error IS NULL) OR (jsonb_typeof(item_error) = 'object'::text)) AND ((valid_for_metering AND (revision_hash IS NOT NULL) AND (item_error IS NULL)) OR ((NOT valid_for_metering) AND (item_error IS NOT NULL)))))
);


--
-- Name: resource_inventory_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_inventory_snapshots (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    scope_epoch_id uuid NOT NULL,
    inventory_scope_id uuid NOT NULL,
    collection_started_at timestamp with time zone NOT NULL,
    collection_completed_at timestamp with time zone NOT NULL,
    received_at timestamp with time zone NOT NULL,
    source_snapshot_at timestamp with time zone,
    complete boolean NOT NULL,
    leader_generation bigint NOT NULL,
    resource_version text,
    controller_epoch text,
    sequence bigint,
    item_count integer NOT NULL,
    item_digest text,
    fatal_errors jsonb DEFAULT '[]'::jsonb NOT NULL,
    item_errors jsonb DEFAULT '[]'::jsonb NOT NULL,
    manifest_state text DEFAULT 'staging'::text NOT NULL,
    sealed_at timestamp with time zone,
    items_expired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_inventory_snapshots_manifest_state_check CHECK ((((manifest_state = 'staging'::text) AND (sealed_at IS NULL) AND (items_expired_at IS NULL)) OR ((manifest_state = 'sealed'::text) AND (sealed_at IS NOT NULL) AND (items_expired_at IS NULL)) OR ((manifest_state = 'items-expired'::text) AND (sealed_at IS NOT NULL) AND (items_expired_at IS NOT NULL) AND (items_expired_at >= sealed_at)))),
    CONSTRAINT resource_inventory_snapshots_shape_check CHECK (((leader_generation >= 0) AND (item_count >= 0) AND ((sequence IS NULL) OR (sequence >= 0)) AND ((item_digest IS NULL) OR (item_digest ~ '^[0-9a-f]{64}$'::text)) AND (jsonb_typeof(fatal_errors) = 'array'::text) AND (jsonb_typeof(item_errors) = 'array'::text) AND ((NOT complete) OR (jsonb_array_length(fatal_errors) = 0)) AND ((manifest_state <> 'sealed'::text) OR (NOT complete) OR (item_digest IS NOT NULL)))),
    CONSTRAINT resource_inventory_snapshots_time_check CHECK (((collection_completed_at >= collection_started_at) AND (received_at >= collection_completed_at) AND ((sealed_at IS NULL) OR (sealed_at >= received_at))))
);


--
-- Name: resource_lifecycle_heads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_lifecycle_heads (
    source_lifecycle_id uuid NOT NULL,
    latest_revision_no bigint DEFAULT 0 NOT NULL,
    current_interval_id uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_lifecycle_heads_revision_check CHECK ((latest_revision_no >= 0))
);


--
-- Name: resource_publication_plan_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_publication_plan_events (
    plan_id uuid NOT NULL,
    ordinal integer NOT NULL,
    source text NOT NULL,
    source_id text NOT NULL,
    unit text NOT NULL,
    ts timestamp with time zone NOT NULL,
    event_kind text NOT NULL,
    canonical_rate_version_id uuid,
    row_hash text NOT NULL,
    event_payload jsonb NOT NULL,
    CONSTRAINT resource_publication_plan_events_shape_check CHECK (((ordinal >= 0) AND (source_id <> ''::text) AND (unit <> ''::text) AND (event_kind = ANY (ARRAY['usage'::text, 'late-usage'::text, 'correction'::text])) AND (((event_kind = ANY (ARRAY['usage'::text, 'late-usage'::text])) AND (source = 'infra-allocation-v2'::text)) OR ((event_kind = 'correction'::text) AND (source = 'infra-allocation-correction-v2'::text))) AND (row_hash ~ '^[0-9a-f]{64}$'::text) AND (jsonb_typeof(event_payload) = 'object'::text)))
);


--
-- Name: resource_publication_plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_publication_plans (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    source_interval_id uuid NOT NULL,
    source_revision text NOT NULL,
    plan_kind text NOT NULL,
    plan_revision bigint DEFAULT 0 NOT NULL,
    advances_cursor boolean NOT NULL,
    previous_materialized_through timestamp with time zone,
    correction_group_id uuid,
    period_start timestamp with time zone NOT NULL,
    period_end timestamp with time zone NOT NULL,
    expected_event_count integer NOT NULL,
    payload_schema_version integer NOT NULL,
    hash_algorithm text DEFAULT 'sha256'::text NOT NULL,
    event_set_hash text NOT NULL,
    rate_selection_hash text NOT NULL,
    creator_generation bigint NOT NULL,
    state text DEFAULT 'planned'::text NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_attempt_at timestamp with time zone,
    sanitized_error jsonb,
    published_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_publication_plans_kind_check CHECK (((plan_kind = ANY (ARRAY['usage'::text, 'late-usage'::text, 'correction'::text])) AND (((plan_kind = ANY (ARRAY['usage'::text, 'late-usage'::text])) AND (plan_revision = 0) AND advances_cursor AND (previous_materialized_through IS NOT NULL) AND (correction_group_id IS NULL)) OR ((plan_kind = 'correction'::text) AND (plan_revision > 0) AND (NOT advances_cursor) AND (previous_materialized_through IS NULL) AND (correction_group_id = id))))),
    CONSTRAINT resource_publication_plans_payload_check CHECK (((expected_event_count > 0) AND (payload_schema_version > 0) AND (hash_algorithm = 'sha256'::text) AND (source_revision ~ '^[0-9a-f]{64}$'::text) AND (event_set_hash ~ '^[0-9a-f]{64}$'::text) AND (rate_selection_hash ~ '^[0-9a-f]{64}$'::text) AND (creator_generation > 0) AND (attempt_count >= 0) AND (((attempt_count = 0) AND (last_attempt_at IS NULL)) OR ((attempt_count > 0) AND (last_attempt_at IS NOT NULL))) AND ((last_attempt_at IS NULL) OR (last_attempt_at >= created_at)) AND ((sanitized_error IS NULL) OR (jsonb_typeof(sanitized_error) = 'object'::text)))),
    CONSTRAINT resource_publication_plans_period_check CHECK (((period_end > period_start) AND (period_end <= (date_trunc('day'::text, period_start, 'UTC'::text) + '1 day'::interval)) AND ((NOT advances_cursor) OR (previous_materialized_through = period_start)))),
    CONSTRAINT resource_publication_plans_state_check CHECK (((state = ANY (ARRAY['planned'::text, 'published'::text, 'conflict'::text])) AND (((state = 'published'::text) AND (published_at IS NOT NULL) AND (published_at >= created_at)) OR ((state <> 'published'::text) AND (published_at IS NULL)))))
);


--
-- Name: TABLE resource_publication_plans; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_publication_plans IS 'Irrevocable app-side outbox plans frozen before cross-database audit publication.';


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
-- Name: session_wake_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session_wake_events (
    id bigint NOT NULL,
    thread_id uuid NOT NULL,
    project_id uuid,
    source text NOT NULL,
    dedup_key text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    fire_at timestamp with time zone,
    claimed_at timestamp with time zone,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT session_wake_events_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'sending'::text, 'sent'::text, 'dead'::text])))
);


--
-- Name: TABLE session_wake_events; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.session_wake_events IS 'Durable wake outbox for officer (centurion) sessions: events + sleep timers. See docs/features/centurion.md §4.';


--
-- Name: COLUMN session_wake_events.fire_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.session_wake_events.fire_at IS 'NULL = deliver on next drain. Future timestamp = durable timer; the drain claims the row only once due. source=timer rows are upserted (fire_at replaced) rather than coalesced.';


--
-- Name: session_wake_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.session_wake_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: session_wake_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.session_wake_events_id_seq OWNED BY public.session_wake_events.id;


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

COMMENT ON COLUMN public.threads.events_epoch IS 'Current event-log runtime generation. The agent allocates a new epoch on every DB-backed runtime attach; older client cursors trigger authoritative re-sync.';


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
-- Name: usage_daily_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_daily_v2 (
    day date NOT NULL,
    user_id uuid,
    project_id uuid,
    category text NOT NULL,
    resource text NOT NULL,
    unit text NOT NULL,
    measurement_basis text NOT NULL,
    resource_class text NOT NULL,
    attribution_scope text NOT NULL,
    cost_domain text NOT NULL,
    measurement_algorithm text NOT NULL,
    quantity numeric(38,18) NOT NULL,
    cost_usd numeric(38,18),
    priced_quantity numeric(38,18) NOT NULL,
    unpriced_quantity numeric(38,18) NOT NULL,
    priced_events bigint NOT NULL,
    unpriced_events bigint NOT NULL,
    events bigint NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_daily_v2_coverage_check CHECK (((priced_events >= 0) AND (unpriced_events >= 0) AND (events >= 0) AND (events = (priced_events + unpriced_events)) AND (quantity = (priced_quantity + unpriced_quantity)) AND (quantity <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (priced_quantity <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (unpriced_quantity <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND ((cost_usd IS NULL) OR (cost_usd <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))) AND (((priced_events = 0) AND (cost_usd IS NULL)) OR ((priced_events > 0) AND (cost_usd IS NOT NULL))))),
    CONSTRAINT usage_daily_v2_dimension_check CHECK (((category <> ''::text) AND (resource <> ''::text) AND (unit <> ''::text) AND (measurement_basis <> ''::text) AND (resource_class <> ''::text) AND (attribution_scope = ANY (ARRAY['customer'::text, 'shared-platform'::text, 'unknown'::text])) AND (cost_domain <> ''::text) AND (measurement_algorithm <> ''::text) AND (((attribution_scope = 'customer'::text) AND ((user_id IS NOT NULL) OR (project_id IS NOT NULL))) OR ((attribution_scope = ANY (ARRAY['shared-platform'::text, 'unknown'::text])) AND (user_id IS NULL) AND (project_id IS NULL)))))
);


--
-- Name: TABLE usage_daily_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.usage_daily_v2 IS 'Typed UTC daily usage read model with explicit priced/unpriced coverage; rebuilt from immutable audit events.';


--
-- Name: usage_rate_card_rates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rate_card_rates (
    rate_card_id text NOT NULL,
    category text NOT NULL,
    resource text DEFAULT '*'::text NOT NULL,
    unit text NOT NULL,
    rate numeric NOT NULL,
    capacity_per_billing_unit numeric DEFAULT 1 NOT NULL,
    effective_from timestamp with time zone DEFAULT now() NOT NULL,
    source_sku text,
    source_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_rate_card_rates_capacity_per_billing_unit_check CHECK ((capacity_per_billing_unit > (0)::numeric)),
    CONSTRAINT usage_rate_card_rates_rate_check CHECK ((rate >= (0)::numeric))
);


--
-- Name: COLUMN usage_rate_card_rates.capacity_per_billing_unit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_rate_card_rates.capacity_per_billing_unit IS 'Ledger quantity represented by one unit charged at rate. Enables bundled instance share pricing without arbitrarily splitting CPU and RAM cost.';


--
-- Name: usage_rate_card_versions_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rate_card_versions_v2 (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    card_id text NOT NULL,
    provider text NOT NULL,
    target_service text NOT NULL,
    target_region text NOT NULL,
    currency text NOT NULL,
    pricing_basis text NOT NULL,
    calculator text NOT NULL,
    aggregation_scope text NOT NULL,
    shape_change_policy text NOT NULL,
    provider_effective_from timestamp with time zone NOT NULL,
    provider_effective_to timestamp with time zone,
    source_published_at timestamp with time zone,
    observed_at timestamp with time zone NOT NULL,
    source_version text NOT NULL,
    source_checksum text NOT NULL,
    component_count integer NOT NULL,
    component_manifest_hash text NOT NULL,
    applicability jsonb DEFAULT '{}'::jsonb NOT NULL,
    calculator_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_rate_card_versions_v2_aggregation_scope_check CHECK (((aggregation_scope = ANY (ARRAY['lifecycle'::text, 'concurrency-envelope'::text])) AND ((calculator = 'reference_dominant_share_v1'::text) OR (aggregation_scope = 'lifecycle'::text)) AND ((calculator <> 'reference_dominant_share_v1'::text) OR (aggregation_scope = 'concurrency-envelope'::text)))),
    CONSTRAINT usage_rate_card_versions_v2_calculator_check CHECK ((calculator = ANY (ARRAY['linear_v1'::text, 'exact_flavor_v1'::text, 'reference_dominant_share_v1'::text, 'fargate_v1'::text, 'aci_container_group_v1'::text, 'block_volume_v1'::text, 'azure_managed_disk_v1'::text]))),
    CONSTRAINT usage_rate_card_versions_v2_currency_check CHECK ((currency ~ '^[A-Z]{3}$'::text)),
    CONSTRAINT usage_rate_card_versions_v2_effective_range_check CHECK (((provider_effective_to IS NULL) OR (provider_effective_to > provider_effective_from))),
    CONSTRAINT usage_rate_card_versions_v2_json_check CHECK (((jsonb_typeof(applicability) = 'object'::text) AND (jsonb_typeof(calculator_config) = 'object'::text))),
    CONSTRAINT usage_rate_card_versions_v2_nonempty_check CHECK (((provider <> ''::text) AND (target_service <> ''::text) AND (target_region <> ''::text) AND (source_version <> ''::text) AND (source_checksum <> ''::text) AND (component_count > 0) AND (component_manifest_hash ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT usage_rate_card_versions_v2_pricing_basis_check CHECK ((pricing_basis = ANY (ARRAY['historical-public-list'::text, 'current-price-scenario'::text]))),
    CONSTRAINT usage_rate_card_versions_v2_publication_time_check CHECK (((source_published_at IS NULL) OR (observed_at >= source_published_at))),
    CONSTRAINT usage_rate_card_versions_v2_shape_policy_check CHECK ((shape_change_policy = ANY (ARRAY['continue'::text, 'restart'::text, 'unsupported'::text])))
);


--
-- Name: TABLE usage_rate_card_versions_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.usage_rate_card_versions_v2 IS 'Immutable public-cloud comparison card versions and calculator applicability.';


--
-- Name: usage_rate_cards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rate_cards (
    id text NOT NULL,
    provider text NOT NULL,
    display_name text NOT NULL,
    region text NOT NULL,
    currency text NOT NULL,
    aggregation text NOT NULL,
    source_url text NOT NULL,
    source_label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    exclusions text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 100 NOT NULL,
    source_checked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_rate_cards_aggregation_check CHECK ((aggregation = ANY (ARRAY['sum'::text, 'max'::text]))),
    CONSTRAINT usage_rate_cards_currency_check CHECK ((currency ~ '^[A-Z]{3}$'::text))
);


--
-- Name: TABLE usage_rate_cards; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.usage_rate_cards IS 'Public-cloud list-price comparison cards. Estimates only: never provider invoice data and never canonical usage_events cost.';


--
-- Name: COLUMN usage_rate_cards.aggregation; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_rate_cards.aggregation IS 'sum = add independently billed components; max = dominant-share estimate for a bundled reference instance.';


--
-- Name: usage_rate_components_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rate_components_v2 (
    version_id uuid NOT NULL,
    component text NOT NULL,
    ordinal integer DEFAULT 0 NOT NULL,
    source_sku text,
    source_meter text,
    billing_unit text NOT NULL,
    unit_size numeric(38,18) NOT NULL,
    unit_price numeric(38,18) NOT NULL,
    tier_min numeric(38,18),
    tier_max numeric(38,18),
    included_quantity numeric(38,18),
    source_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_rate_components_v2_shape_check CHECK (((component <> ''::text) AND (billing_unit <> ''::text) AND (ordinal >= 0) AND (unit_size > (0)::numeric) AND (unit_price >= (0)::numeric) AND (unit_size <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND (unit_price <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric])) AND ((tier_min IS NULL) OR (tier_min >= (0)::numeric)) AND ((tier_max IS NULL) OR (tier_max > (0)::numeric)) AND ((tier_min IS NULL) OR (tier_max IS NULL) OR (tier_max > tier_min)) AND ((included_quantity IS NULL) OR (included_quantity >= (0)::numeric)) AND ((tier_min IS NULL) OR (tier_min <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))) AND ((tier_max IS NULL) OR (tier_max <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))) AND ((included_quantity IS NULL) OR (included_quantity <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))) AND (jsonb_typeof(source_metadata) = 'object'::text)))
);


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
-- Name: usage_rates_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rates_v2 (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    cost_domain text NOT NULL,
    measurement_basis text NOT NULL,
    category text NOT NULL,
    resource_class text NOT NULL,
    resource text NOT NULL,
    unit text NOT NULL,
    effective_from timestamp with time zone NOT NULL,
    effective_to timestamp with time zone,
    usd_per_unit numeric(38,18) NOT NULL,
    source text NOT NULL,
    source_version text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_rates_v2_effective_range_check CHECK (((effective_to IS NULL) OR (effective_to > effective_from))),
    CONSTRAINT usage_rates_v2_nonempty_selector_check CHECK (((cost_domain <> ''::text) AND (measurement_basis <> ''::text) AND (category <> ''::text) AND (resource_class <> ''::text) AND (resource <> ''::text) AND (resource <> '*'::text) AND (unit <> ''::text) AND (source <> ''::text) AND (source_version <> ''::text))),
    CONSTRAINT usage_rates_v2_rate_nonnegative_check CHECK (((usd_per_unit >= (0)::numeric) AND (usd_per_unit <> ALL (ARRAY['NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric]))))
);


--
-- Name: TABLE usage_rates_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.usage_rates_v2 IS 'Immutable exact-selector canonical USD ledger rates; absence means unpriced.';


--
-- Name: usage_rollup_day_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rollup_day_state (
    day date NOT NULL,
    applied_audit_revision bigint NOT NULL,
    coverage_status text NOT NULL,
    unknown_ranges jsonb DEFAULT '[]'::jsonb NOT NULL,
    rolled_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_rollup_day_state_shape_check CHECK (((applied_audit_revision > 0) AND (coverage_status = ANY (ARRAY['complete'::text, 'partial'::text, 'unavailable'::text])) AND (jsonb_typeof(unknown_ranges) = 'array'::text)))
);


--
-- Name: usage_rollup_v2_bootstrap_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rollup_v2_bootstrap_state (
    singleton boolean DEFAULT true NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    seeded_through_day date,
    reconciled_through_day date,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    sanitized_error jsonb,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_rollup_v2_bootstrap_shape_check CHECK (((status = ANY (ARRAY['pending'::text, 'running'::text, 'reconciling'::text, 'complete'::text, 'error'::text])) AND ((sanitized_error IS NULL) OR (jsonb_typeof(sanitized_error) = 'object'::text)) AND ((reconciled_through_day IS NULL) OR ((seeded_through_day IS NOT NULL) AND (reconciled_through_day <= seeded_through_day))) AND (((status = 'pending'::text) AND (started_at IS NULL) AND (seeded_through_day IS NULL) AND (reconciled_through_day IS NULL) AND (completed_at IS NULL)) OR ((status = 'running'::text) AND (started_at IS NOT NULL) AND (seeded_through_day IS NULL) AND (reconciled_through_day IS NULL) AND (completed_at IS NULL)) OR ((status = 'reconciling'::text) AND (started_at IS NOT NULL) AND (seeded_through_day IS NOT NULL) AND (completed_at IS NULL)) OR ((status = 'complete'::text) AND (started_at IS NOT NULL) AND (seeded_through_day IS NOT NULL) AND (reconciled_through_day = seeded_through_day) AND (completed_at IS NOT NULL) AND (completed_at >= started_at) AND (sanitized_error IS NULL)) OR ((status = 'error'::text) AND (completed_at IS NULL))))),
    CONSTRAINT usage_rollup_v2_bootstrap_singleton_check CHECK (singleton)
);


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
-- Name: user_expert_defaults; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_expert_defaults (
    user_id uuid NOT NULL,
    expert_type character varying(10) NOT NULL,
    expert_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_expert_defaults_expert_type_check CHECK (((expert_type)::text = ANY ((ARRAY['worker'::character varying, 'session'::character varying])::text[])))
);


--
-- Name: TABLE user_expert_defaults; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.user_expert_defaults IS 'Optional user-owned default expert per type; use is gated at resolution time.';


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
    preferred_username text,
    cloud_identity jsonb DEFAULT '{}'::jsonb NOT NULL
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
-- Name: COLUMN users.cloud_identity; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users.cloud_identity IS 'Per-backend cloud identity cache: {"<backend_id>": {"user_id", "home_browser_url", "resolved_at"}}. Positive results only; maintained by services/cloud/identity.py.';


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
-- Name: session_wake_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_wake_events ALTER COLUMN id SET DEFAULT nextval('public.session_wake_events_id_seq'::regclass);


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
-- Name: application_expert_defaults application_expert_defaults_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.application_expert_defaults
    ADD CONSTRAINT application_expert_defaults_pkey PRIMARY KEY (expert_type);


--
-- Name: automations automations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automations
    ADD CONSTRAINT automations_pkey PRIMARY KEY (id);


--
-- Name: bench_runs bench_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bench_runs
    ADD CONSTRAINT bench_runs_pkey PRIMARY KEY (id);


--
-- Name: canvas_origin_sessions canvas_origin_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_origin_sessions
    ADD CONSTRAINT canvas_origin_sessions_pkey PRIMARY KEY (id);


--
-- Name: canvas_origin_sessions canvas_origin_sessions_session_secret_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_origin_sessions
    ADD CONSTRAINT canvas_origin_sessions_session_secret_hash_key UNIQUE (session_secret_hash);


--
-- Name: canvas_view_attachments canvas_view_attachments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_view_attachments
    ADD CONSTRAINT canvas_view_attachments_pkey PRIMARY KEY (id);


--
-- Name: canvas_view_bootstraps canvas_view_bootstraps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_view_bootstraps
    ADD CONSTRAINT canvas_view_bootstraps_pkey PRIMARY KEY (id);


--
-- Name: canvases canvases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvases
    ADD CONSTRAINT canvases_pkey PRIMARY KEY (id);


--
-- Name: capability_grants capability_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.capability_grants
    ADD CONSTRAINT capability_grants_pkey PRIMARY KEY (id);


--
-- Name: cloud_ro_mounts cloud_ro_mounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cloud_ro_mounts
    ADD CONSTRAINT cloud_ro_mounts_pkey PRIMARY KEY (id);


--
-- Name: contact_addresses contact_addresses_owner_user_id_channel_address_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_addresses
    ADD CONSTRAINT contact_addresses_owner_user_id_channel_address_key UNIQUE (owner_user_id, channel, address);


--
-- Name: contact_addresses contact_addresses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_addresses
    ADD CONSTRAINT contact_addresses_pkey PRIMARY KEY (id);


--
-- Name: contacts contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT contacts_pkey PRIMARY KEY (id);


--
-- Name: datasource_project_reconcile_queue datasource_project_reconcile_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasource_project_reconcile_queue
    ADD CONSTRAINT datasource_project_reconcile_queue_pkey PRIMARY KEY (project_id, datasource_id);


--
-- Name: datasources datasources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_pkey PRIMARY KEY (id);


--
-- Name: docker_workspace_leases docker_workspace_leases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_workspace_leases
    ADD CONSTRAINT docker_workspace_leases_pkey PRIMARY KEY (host, port);


--
-- Name: expert_default_audit expert_default_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expert_default_audit
    ADD CONSTRAINT expert_default_audit_pkey PRIMARY KEY (id);


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
-- Name: infra_metering_control infra_metering_control_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.infra_metering_control
    ADD CONSTRAINT infra_metering_control_pkey PRIMARY KEY (singleton);


--
-- Name: infra_usage_day_state infra_usage_day_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.infra_usage_day_state
    ADD CONSTRAINT infra_usage_day_state_pkey PRIMARY KEY (day);


--
-- Name: job_change_records job_change_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_change_records
    ADD CONSTRAINT job_change_records_pkey PRIMARY KEY (job_id);


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
-- Name: canvas_snapshots pk_canvas_snapshots; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_snapshots
    ADD CONSTRAINT pk_canvas_snapshots PRIMARY KEY (thread_id, canvas_id);


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
-- Name: project_contacts project_contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_contacts
    ADD CONSTRAINT project_contacts_pkey PRIMARY KEY (project_id, contact_id);


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
-- Name: resource_intervals resource_intervals_id_lifecycle_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_intervals
    ADD CONSTRAINT resource_intervals_id_lifecycle_uq UNIQUE (id, source_lifecycle_id);


--
-- Name: resource_intervals resource_intervals_id_revision_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_intervals
    ADD CONSTRAINT resource_intervals_id_revision_uq UNIQUE (id, source_revision);


--
-- Name: resource_intervals resource_intervals_lifecycle_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_intervals
    ADD CONSTRAINT resource_intervals_lifecycle_no_overlap EXCLUDE USING gist (source_lifecycle_id WITH =, tstzrange(started_at, ended_at, '[)'::text) WITH &&);


--
-- Name: resource_intervals resource_intervals_lifecycle_revision_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_intervals
    ADD CONSTRAINT resource_intervals_lifecycle_revision_uq UNIQUE (source_lifecycle_id, revision_no);


--
-- Name: resource_intervals resource_intervals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_intervals
    ADD CONSTRAINT resource_intervals_pkey PRIMARY KEY (id);


--
-- Name: resource_inventory_coverage_gaps resource_inventory_coverage_gaps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_coverage_gaps
    ADD CONSTRAINT resource_inventory_coverage_gaps_pkey PRIMARY KEY (id);


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_id_scope_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scope_epochs
    ADD CONSTRAINT resource_inventory_scope_epochs_id_scope_uq UNIQUE (id, scope_id);


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scope_epochs
    ADD CONSTRAINT resource_inventory_scope_epochs_pkey PRIMARY KEY (id);


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_scope_id_epoch_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scope_epochs
    ADD CONSTRAINT resource_inventory_scope_epochs_scope_id_epoch_number_key UNIQUE (scope_id, epoch_number);


--
-- Name: resource_inventory_scopes resource_inventory_scopes_id_cluster_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scopes
    ADD CONSTRAINT resource_inventory_scopes_id_cluster_uq UNIQUE (id, source_cluster);


--
-- Name: resource_inventory_scopes resource_inventory_scopes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scopes
    ADD CONSTRAINT resource_inventory_scopes_pkey PRIMARY KEY (id);


--
-- Name: resource_inventory_snapshot_items resource_inventory_snapshot_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_snapshot_items
    ADD CONSTRAINT resource_inventory_snapshot_items_pkey PRIMARY KEY (snapshot_id, source_kind, source_uid);


--
-- Name: resource_inventory_snapshots resource_inventory_snapshots_id_epoch_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_snapshots
    ADD CONSTRAINT resource_inventory_snapshots_id_epoch_uq UNIQUE (id, scope_epoch_id);


--
-- Name: resource_inventory_snapshots resource_inventory_snapshots_id_scope_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_snapshots
    ADD CONSTRAINT resource_inventory_snapshots_id_scope_uq UNIQUE (id, inventory_scope_id);


--
-- Name: resource_inventory_snapshots resource_inventory_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_snapshots
    ADD CONSTRAINT resource_inventory_snapshots_pkey PRIMARY KEY (id);


--
-- Name: resource_lifecycle_heads resource_lifecycle_heads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_lifecycle_heads
    ADD CONSTRAINT resource_lifecycle_heads_pkey PRIMARY KEY (source_lifecycle_id);


--
-- Name: resource_publication_plan_events resource_publication_plan_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_publication_plan_events
    ADD CONSTRAINT resource_publication_plan_events_pkey PRIMARY KEY (plan_id, ordinal);


--
-- Name: resource_publication_plan_events resource_publication_plan_events_source_source_id_unit_ts_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_publication_plan_events
    ADD CONSTRAINT resource_publication_plan_events_source_source_id_unit_ts_key UNIQUE (source, source_id, unit, ts);


--
-- Name: resource_publication_plans resource_publication_plans_id_kind_start_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_publication_plans
    ADD CONSTRAINT resource_publication_plans_id_kind_start_uq UNIQUE (id, plan_kind, period_start);


--
-- Name: resource_publication_plans resource_publication_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_publication_plans
    ADD CONSTRAINT resource_publication_plans_pkey PRIMARY KEY (id);


--
-- Name: resource_publication_plans resource_publication_plans_source_interval_id_period_start__key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_publication_plans
    ADD CONSTRAINT resource_publication_plans_source_interval_id_period_start__key UNIQUE (source_interval_id, period_start, period_end, plan_kind, plan_revision);


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
-- Name: session_wake_events session_wake_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_wake_events
    ADD CONSTRAINT session_wake_events_pkey PRIMARY KEY (id);


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
-- Name: canvas_view_bootstraps uq_canvas_bootstrap_attachment; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_view_bootstraps
    ADD CONSTRAINT uq_canvas_bootstrap_attachment UNIQUE (attachment_id);


--
-- Name: canvases uq_canvases_thread_canvas; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvases
    ADD CONSTRAINT uq_canvases_thread_canvas UNIQUE (thread_id, canvas_id);


--
-- Name: experts uq_experts_id_type; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experts
    ADD CONSTRAINT uq_experts_id_type UNIQUE (id, expert_type);


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
-- Name: usage_rate_card_rates usage_rate_card_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rate_card_rates
    ADD CONSTRAINT usage_rate_card_rates_pkey PRIMARY KEY (rate_card_id, category, resource, unit, effective_from);


--
-- Name: usage_rate_card_versions_v2 usage_rate_card_versions_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rate_card_versions_v2
    ADD CONSTRAINT usage_rate_card_versions_v2_pkey PRIMARY KEY (id);


--
-- Name: usage_rate_cards usage_rate_cards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rate_cards
    ADD CONSTRAINT usage_rate_cards_pkey PRIMARY KEY (id);


--
-- Name: usage_rate_components_v2 usage_rate_components_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rate_components_v2
    ADD CONSTRAINT usage_rate_components_v2_pkey PRIMARY KEY (version_id, component, ordinal);


--
-- Name: usage_rates usage_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rates
    ADD CONSTRAINT usage_rates_pkey PRIMARY KEY (category, resource, unit, effective_from);


--
-- Name: usage_rates_v2 usage_rates_v2_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rates_v2
    ADD CONSTRAINT usage_rates_v2_no_overlap EXCLUDE USING gist (cost_domain WITH =, measurement_basis WITH =, category WITH =, resource_class WITH =, resource WITH =, unit WITH =, tstzrange(effective_from, effective_to, '[)'::text) WITH &&);


--
-- Name: usage_rates_v2 usage_rates_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rates_v2
    ADD CONSTRAINT usage_rates_v2_pkey PRIMARY KEY (id);


--
-- Name: usage_rollup_day_state usage_rollup_day_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rollup_day_state
    ADD CONSTRAINT usage_rollup_day_state_pkey PRIMARY KEY (day);


--
-- Name: usage_rollup_v2_bootstrap_state usage_rollup_v2_bootstrap_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rollup_v2_bootstrap_state
    ADD CONSTRAINT usage_rollup_v2_bootstrap_state_pkey PRIMARY KEY (singleton);


--
-- Name: user_api_keys user_api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_api_keys
    ADD CONSTRAINT user_api_keys_pkey PRIMARY KEY (id);


--
-- Name: user_expert_defaults user_expert_defaults_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_expert_defaults
    ADD CONSTRAINT user_expert_defaults_pkey PRIMARY KEY (user_id, expert_type);


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
-- Name: cloud_ro_mounts_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cloud_ro_mounts_status_idx ON public.cloud_ro_mounts USING btree (status);


--
-- Name: cloud_ro_mounts_thread_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX cloud_ro_mounts_thread_idx ON public.cloud_ro_mounts USING btree (thread_id);


--
-- Name: cloud_ro_mounts_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cloud_ro_mounts_user_idx ON public.cloud_ro_mounts USING btree (user_id);


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
-- Name: idx_automations_expert_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_automations_expert_id ON public.automations USING btree (expert_id) WHERE (expert_id IS NOT NULL);


--
-- Name: idx_automations_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_automations_owner ON public.automations USING btree (owner_id);


--
-- Name: idx_automations_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_automations_project ON public.automations USING btree (project_id) WHERE (project_id IS NOT NULL);


--
-- Name: idx_canvas_origin_sessions_active_identity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canvas_origin_sessions_active_identity ON public.canvas_origin_sessions USING btree (origin_generation, thread_id, canvas_id) WHERE (revoked_at IS NULL);


--
-- Name: idx_canvas_origin_sessions_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canvas_origin_sessions_expires ON public.canvas_origin_sessions USING btree (expires_at);


--
-- Name: idx_canvas_origin_sessions_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canvas_origin_sessions_parent ON public.canvas_origin_sessions USING btree (parent_srw_session_id) WHERE ((revoked_at IS NULL) AND (parent_srw_session_id IS NOT NULL));


--
-- Name: idx_canvas_origin_sessions_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canvas_origin_sessions_user_active ON public.canvas_origin_sessions USING btree (user_id) WHERE (revoked_at IS NULL);


--
-- Name: idx_canvas_view_attachments_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canvas_view_attachments_active ON public.canvas_view_attachments USING btree (thread_id, canvas_id, user_id) WHERE (closed_at IS NULL);


--
-- Name: idx_canvas_view_attachments_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canvas_view_attachments_expires ON public.canvas_view_attachments USING btree (expires_at);


--
-- Name: idx_canvas_view_attachments_origin_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canvas_view_attachments_origin_session ON public.canvas_view_attachments USING btree (origin_session_id) WHERE ((closed_at IS NULL) AND (origin_session_id IS NOT NULL));


--
-- Name: idx_canvas_view_bootstraps_exchange_token; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_canvas_view_bootstraps_exchange_token ON public.canvas_view_bootstraps USING btree (exchange_token_hash) WHERE (exchange_token_hash IS NOT NULL);


--
-- Name: idx_canvas_view_bootstraps_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canvas_view_bootstraps_expires ON public.canvas_view_bootstraps USING btree (expires_at);


--
-- Name: idx_canvas_view_bootstraps_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canvas_view_bootstraps_pending ON public.canvas_view_bootstraps USING btree (attachment_id, expires_at) WHERE (consumed_at IS NULL);


--
-- Name: idx_config_override_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_config_override_lookup ON public.config_overrides USING btree (family, kind, name);


--
-- Name: idx_contact_addresses_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_addresses_contact ON public.contact_addresses USING btree (contact_id);


--
-- Name: idx_contacts_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contacts_owner ON public.contacts USING btree (owner_user_id);


--
-- Name: idx_datasource_project_reconcile_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_datasource_project_reconcile_due ON public.datasource_project_reconcile_queue USING btree (next_attempt_at, updated_at);


--
-- Name: idx_datasources_auto_attach_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_datasources_auto_attach_owner ON public.datasources USING btree (created_by) WHERE ((job_id IS NULL) AND (auto_attach = true));


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
-- Name: idx_job_change_records_loop_iteration; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_change_records_loop_iteration ON public.job_change_records USING btree (loop_id, iteration DESC) WHERE (loop_id IS NOT NULL);


--
-- Name: idx_job_change_records_project_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_change_records_project_created ON public.job_change_records USING btree (project_id, created_at DESC);


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
-- Name: idx_jobs_wake_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_wake_pending ON public.jobs USING btree (updated_at) WHERE (wake_on_complete AND (created_by_thread_id IS NOT NULL) AND (wake_state = ANY (ARRAY['none'::text, 'pending'::text, 'sending'::text])));


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
-- Name: idx_project_contacts_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_contacts_contact ON public.project_contacts USING btree (contact_id);


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
-- Name: idx_session_wake_events_claim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_wake_events_claim ON public.session_wake_events USING btree (created_at) WHERE (state = ANY (ARRAY['pending'::text, 'sending'::text]));


--
-- Name: idx_session_wake_events_debounce; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_wake_events_debounce ON public.session_wake_events USING btree (thread_id, source, sent_at) WHERE (state = 'sent'::text);


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
-- Name: idx_user_expert_defaults_expert; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_expert_defaults_expert ON public.user_expert_defaults USING btree (expert_id);


--
-- Name: idx_users_keycloak_sub; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_keycloak_sub ON public.users USING btree (keycloak_sub);


--
-- Name: jobs_lease_expiry_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX jobs_lease_expiry_idx ON public.jobs USING btree (lease_expires_at) WHERE ((status)::text = 'processing'::text);


--
-- Name: resource_intervals_materializer_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_intervals_materializer_idx ON public.resource_intervals USING btree (materialized_through, last_confirmed_at) WHERE (materialized_through < COALESCE(ended_at, last_confirmed_at));


--
-- Name: resource_intervals_open_lifecycle_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX resource_intervals_open_lifecycle_uq ON public.resource_intervals USING btree (source_lifecycle_id) WHERE (ended_at IS NULL);


--
-- Name: resource_intervals_open_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX resource_intervals_open_uq ON public.resource_intervals USING btree (source_cluster, source_kind, source_uid) WHERE (ended_at IS NULL);


--
-- Name: resource_intervals_project_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_intervals_project_time_idx ON public.resource_intervals USING btree (project_id, started_at, ended_at) WHERE (project_id IS NOT NULL);


--
-- Name: resource_intervals_user_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_intervals_user_time_idx ON public.resource_intervals USING btree (user_id, started_at, ended_at) WHERE (user_id IS NOT NULL);


--
-- Name: resource_inventory_coverage_gaps_open_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX resource_inventory_coverage_gaps_open_uq ON public.resource_inventory_coverage_gaps USING btree (scope_epoch_id, gap_start, reason) WHERE (resolution = 'unresolved'::text);


--
-- Name: resource_inventory_coverage_gaps_range_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_coverage_gaps_range_idx ON public.resource_inventory_coverage_gaps USING btree (scope_epoch_id, gap_start, gap_end);


--
-- Name: resource_inventory_scope_epochs_active_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX resource_inventory_scope_epochs_active_uq ON public.resource_inventory_scope_epochs USING btree (scope_id) WHERE (retired_at IS NULL);


--
-- Name: resource_inventory_scope_epochs_rollup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_scope_epochs_rollup_idx ON public.resource_inventory_scope_epochs USING btree (required_from, retired_at) WHERE (required_for_rollup = true);


--
-- Name: resource_inventory_scopes_identity_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX resource_inventory_scopes_identity_uq ON public.resource_inventory_scopes USING btree (collector_id, source_cluster, api_resource, namespace) NULLS NOT DISTINCT;


--
-- Name: resource_inventory_snapshots_controller_seq_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX resource_inventory_snapshots_controller_seq_uq ON public.resource_inventory_snapshots USING btree (scope_epoch_id, controller_epoch, sequence) WHERE ((controller_epoch IS NOT NULL) AND (sequence IS NOT NULL));


--
-- Name: resource_inventory_snapshots_scope_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_snapshots_scope_time_idx ON public.resource_inventory_snapshots USING btree (scope_epoch_id, collection_completed_at DESC);


--
-- Name: resource_publication_plans_interval_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_publication_plans_interval_idx ON public.resource_publication_plans USING btree (source_interval_id, period_start);


--
-- Name: resource_publication_plans_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_publication_plans_pending_idx ON public.resource_publication_plans USING btree (created_at, id) WHERE (state = 'planned'::text);


--
-- Name: schema_migrations_dirty_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX schema_migrations_dirty_idx ON public.schema_migrations USING btree (filename) WHERE (success = false);


--
-- Name: uq_canvases_origin_generation; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_canvases_origin_generation ON public.canvases USING btree (origin_generation) WHERE (origin_generation IS NOT NULL);


--
-- Name: uq_config_override; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_config_override ON public.config_overrides USING btree (COALESCE(family, ''::character varying), kind, name);


--
-- Name: uq_contact_primary_per_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_contact_primary_per_channel ON public.contact_addresses USING btree (contact_id, channel) WHERE is_primary;


--
-- Name: uq_datasource_name_type_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_datasource_name_type_owner ON public.datasources USING btree (name, type, COALESCE(created_by, '00000000-0000-0000-0000-000000000000'::uuid));


--
-- Name: uq_docker_workspace_active_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_docker_workspace_active_owner ON public.docker_workspace_leases USING btree (owner_kind, owner_id) WHERE ((owner_id IS NOT NULL) AND (status = ANY (ARRAY['ready'::text, 'releasing'::text])));


--
-- Name: uq_docker_workspace_lease_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_docker_workspace_lease_id ON public.docker_workspace_leases USING btree (lease_id) WHERE (lease_id IS NOT NULL);


--
-- Name: uq_experts_managed_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_experts_managed_key ON public.experts USING btree (managed_key) WHERE (managed_key IS NOT NULL);


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
-- Name: uq_project_knowledge_repo; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_project_knowledge_repo ON public.project_repositories USING btree (project_id) WHERE ((role)::text = 'knowledge'::text);


--
-- Name: uq_session_wake_events_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_session_wake_events_pending ON public.session_wake_events USING btree (thread_id, source, dedup_key) WHERE (state = 'pending'::text);


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
-- Name: usage_daily_v2_dims_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX usage_daily_v2_dims_uq ON public.usage_daily_v2 USING btree (day, user_id, project_id, category, resource, unit, measurement_basis, resource_class, attribution_scope, cost_domain, measurement_algorithm) NULLS NOT DISTINCT;


--
-- Name: usage_daily_v2_project_day_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_daily_v2_project_day_idx ON public.usage_daily_v2 USING btree (project_id, day) WHERE (project_id IS NOT NULL);


--
-- Name: usage_daily_v2_user_day_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_daily_v2_user_day_idx ON public.usage_daily_v2 USING btree (user_id, day) WHERE (user_id IS NOT NULL);


--
-- Name: usage_rate_card_rates_lookup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_rate_card_rates_lookup_idx ON public.usage_rate_card_rates USING btree (rate_card_id, category, resource, unit, effective_from DESC);


--
-- Name: usage_rate_card_versions_v2_select_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_rate_card_versions_v2_select_idx ON public.usage_rate_card_versions_v2 USING btree (card_id, pricing_basis, provider_effective_from DESC);


--
-- Name: usage_rates_v2_lookup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usage_rates_v2_lookup_idx ON public.usage_rates_v2 USING btree (cost_domain, measurement_basis, category, resource_class, resource, unit, effective_from DESC);


--
-- Name: workspace_intervals_open_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX workspace_intervals_open_uq ON public.workspace_intervals USING btree (owner_kind, owner_id) WHERE (ended_at IS NULL);


--
-- Name: workspace_intervals_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX workspace_intervals_pending_idx ON public.workspace_intervals USING btree (ended_at) WHERE ((ended_at IS NOT NULL) AND (materialized_at IS NULL));


--
-- Name: project_datasources datasource_project_policy_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER datasource_project_policy_change AFTER INSERT OR DELETE OR UPDATE ON public.project_datasources FOR EACH ROW EXECUTE FUNCTION public.reconcile_datasource_project_policy_change();


--
-- Name: infra_usage_day_state infra_usage_day_state_one_way_seal; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER infra_usage_day_state_one_way_seal BEFORE INSERT OR DELETE OR UPDATE ON public.infra_usage_day_state FOR EACH ROW EXECUTE FUNCTION public.protect_infra_usage_day_state_mutation();


--
-- Name: resource_intervals resource_intervals_immutable_revision; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_intervals_immutable_revision BEFORE DELETE OR UPDATE ON public.resource_intervals FOR EACH ROW EXECUTE FUNCTION public.protect_resource_interval_revision_mutation();


--
-- Name: resource_intervals resource_intervals_scope_identity; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_intervals_scope_identity BEFORE INSERT OR UPDATE OF inventory_scope_id, source_cluster, namespace, last_seen_snapshot_id ON public.resource_intervals FOR EACH ROW EXECUTE FUNCTION public.validate_resource_interval_scope_identity();


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_complete_snapshot; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_scope_epochs_complete_snapshot BEFORE INSERT OR UPDATE OF last_complete_snapshot_id ON public.resource_inventory_scope_epochs FOR EACH ROW EXECUTE FUNCTION public.validate_inventory_epoch_last_complete_snapshot();


--
-- Name: resource_inventory_snapshot_items resource_inventory_snapshot_items_staging_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_snapshot_items_staging_only BEFORE INSERT OR DELETE OR UPDATE ON public.resource_inventory_snapshot_items FOR EACH ROW EXECUTE FUNCTION public.protect_resource_inventory_snapshot_item_mutation();


--
-- Name: resource_inventory_snapshots resource_inventory_snapshots_seal_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_snapshots_seal_only BEFORE INSERT OR UPDATE ON public.resource_inventory_snapshots FOR EACH ROW EXECUTE FUNCTION public.protect_resource_inventory_snapshot_mutation();


--
-- Name: resource_publication_plan_events resource_publication_plan_events_frozen; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_publication_plan_events_frozen BEFORE INSERT OR DELETE OR UPDATE ON public.resource_publication_plan_events FOR EACH ROW EXECUTE FUNCTION public.protect_resource_publication_plan_event_mutation();


--
-- Name: resource_publication_plan_events resource_publication_plan_events_manifest_complete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER resource_publication_plan_events_manifest_complete AFTER INSERT ON public.resource_publication_plan_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_resource_publication_plan_manifest();


--
-- Name: resource_publication_plans resource_publication_plans_frozen_intent; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_publication_plans_frozen_intent BEFORE DELETE OR UPDATE ON public.resource_publication_plans FOR EACH ROW EXECUTE FUNCTION public.protect_resource_publication_plan_mutation();


--
-- Name: resource_publication_plans resource_publication_plans_manifest_complete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER resource_publication_plans_manifest_complete AFTER INSERT ON public.resource_publication_plans DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_resource_publication_plan_manifest();


--
-- Name: thread_permission_requests thread_permission_notify_trigger; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER thread_permission_notify_trigger AFTER UPDATE ON public.thread_permission_requests FOR EACH ROW EXECUTE FUNCTION public.notify_thread_permission_update();


--
-- Name: canvas_origin_sessions trg_canvas_origin_session_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_canvas_origin_session_change AFTER UPDATE OF revoked_at, expires_at ON public.canvas_origin_sessions FOR EACH ROW EXECUTE FUNCTION public.notify_canvas_origin_session_change();


--
-- Name: srw_sessions trg_canvas_revoke_bff_session; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_canvas_revoke_bff_session BEFORE DELETE ON public.srw_sessions FOR EACH ROW EXECUTE FUNCTION public.revoke_canvas_sessions_for_bff_session();


--
-- Name: canvases trg_canvas_revoke_retired_origin; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_canvas_revoke_retired_origin AFTER UPDATE OF origin_generation ON public.canvases FOR EACH ROW EXECUTE FUNCTION public.revoke_canvas_sessions_for_retired_origin();


--
-- Name: users trg_canvas_revoke_user_admission; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_canvas_revoke_user_admission AFTER UPDATE OF is_approved ON public.users FOR EACH ROW EXECUTE FUNCTION public.revoke_canvas_sessions_for_user_admission();


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
-- Name: usage_rate_card_versions_v2 usage_rate_card_versions_v2_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER usage_rate_card_versions_v2_immutable BEFORE DELETE OR UPDATE ON public.usage_rate_card_versions_v2 FOR EACH ROW EXECUTE FUNCTION public.protect_usage_rate_card_version_mutation();


--
-- Name: usage_rate_card_versions_v2 usage_rate_card_versions_v2_manifest_complete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER usage_rate_card_versions_v2_manifest_complete AFTER INSERT ON public.usage_rate_card_versions_v2 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_usage_rate_card_component_count();


--
-- Name: usage_rate_components_v2 usage_rate_components_v2_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER usage_rate_components_v2_immutable BEFORE DELETE OR UPDATE ON public.usage_rate_components_v2 FOR EACH ROW EXECUTE FUNCTION public.reject_usage_rate_component_mutation();


--
-- Name: usage_rate_components_v2 usage_rate_components_v2_manifest_complete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER usage_rate_components_v2_manifest_complete AFTER INSERT ON public.usage_rate_components_v2 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_usage_rate_card_component_count();


--
-- Name: usage_rates_v2 usage_rates_v2_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER usage_rates_v2_immutable BEFORE DELETE OR UPDATE ON public.usage_rates_v2 FOR EACH ROW EXECUTE FUNCTION public.protect_usage_rates_v2_mutation();


--
-- Name: agents agents_current_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_current_job_id_fkey FOREIGN KEY (current_job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: application_expert_defaults application_expert_defaults_expert_type_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.application_expert_defaults
    ADD CONSTRAINT application_expert_defaults_expert_type_fkey FOREIGN KEY (expert_id, expert_type) REFERENCES public.experts(id, expert_type) ON DELETE RESTRICT;


--
-- Name: application_expert_defaults application_expert_defaults_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.application_expert_defaults
    ADD CONSTRAINT application_expert_defaults_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: auth_tokens auth_tokens_superseded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT auth_tokens_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES public.auth_tokens(id) ON DELETE SET NULL;


--
-- Name: automations automations_expert_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automations
    ADD CONSTRAINT automations_expert_id_fkey FOREIGN KEY (expert_id) REFERENCES public.experts(id) ON DELETE RESTRICT;


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
-- Name: bench_runs bench_runs_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bench_runs
    ADD CONSTRAINT bench_runs_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: canvas_origin_sessions canvas_origin_sessions_parent_srw_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_origin_sessions
    ADD CONSTRAINT canvas_origin_sessions_parent_srw_session_id_fkey FOREIGN KEY (parent_srw_session_id) REFERENCES public.srw_sessions(id) ON DELETE SET NULL;


--
-- Name: canvas_origin_sessions canvas_origin_sessions_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_origin_sessions
    ADD CONSTRAINT canvas_origin_sessions_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: canvas_origin_sessions canvas_origin_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_origin_sessions
    ADD CONSTRAINT canvas_origin_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: canvas_view_attachments canvas_view_attachments_origin_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_view_attachments
    ADD CONSTRAINT canvas_view_attachments_origin_session_id_fkey FOREIGN KEY (origin_session_id) REFERENCES public.canvas_origin_sessions(id) ON DELETE SET NULL;


--
-- Name: canvas_view_attachments canvas_view_attachments_parent_srw_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_view_attachments
    ADD CONSTRAINT canvas_view_attachments_parent_srw_session_id_fkey FOREIGN KEY (parent_srw_session_id) REFERENCES public.srw_sessions(id) ON DELETE SET NULL;


--
-- Name: canvas_view_attachments canvas_view_attachments_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_view_attachments
    ADD CONSTRAINT canvas_view_attachments_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: canvas_view_attachments canvas_view_attachments_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_view_attachments
    ADD CONSTRAINT canvas_view_attachments_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: canvas_view_bootstraps canvas_view_bootstraps_attachment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_view_bootstraps
    ADD CONSTRAINT canvas_view_bootstraps_attachment_id_fkey FOREIGN KEY (attachment_id) REFERENCES public.canvas_view_attachments(id) ON DELETE CASCADE;


--
-- Name: canvas_view_bootstraps canvas_view_bootstraps_consumed_origin_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_view_bootstraps
    ADD CONSTRAINT canvas_view_bootstraps_consumed_origin_session_id_fkey FOREIGN KEY (consumed_origin_session_id) REFERENCES public.canvas_origin_sessions(id) ON DELETE CASCADE;


--
-- Name: canvases canvases_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvases
    ADD CONSTRAINT canvases_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: capability_grants capability_grants_granted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.capability_grants
    ADD CONSTRAINT capability_grants_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: contact_addresses contact_addresses_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_addresses
    ADD CONSTRAINT contact_addresses_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.contacts(id) ON DELETE CASCADE;


--
-- Name: contact_addresses contact_addresses_owner_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_addresses
    ADD CONSTRAINT contact_addresses_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: contacts contacts_owner_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT contacts_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES public.users(id) ON DELETE CASCADE;


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
-- Name: expert_default_audit expert_default_audit_actor_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expert_default_audit
    ADD CONSTRAINT expert_default_audit_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: expert_default_audit expert_default_audit_target_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expert_default_audit
    ADD CONSTRAINT expert_default_audit_target_project_id_fkey FOREIGN KEY (target_project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: expert_default_audit expert_default_audit_target_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expert_default_audit
    ADD CONSTRAINT expert_default_audit_target_user_id_fkey FOREIGN KEY (target_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


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
-- Name: canvas_snapshots fk_canvas_snapshots_canvas; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_snapshots
    ADD CONSTRAINT fk_canvas_snapshots_canvas FOREIGN KEY (thread_id, canvas_id) REFERENCES public.canvases(thread_id, canvas_id) ON DELETE CASCADE;


--
-- Name: jobs fk_jobs_assigned_agent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT fk_jobs_assigned_agent FOREIGN KEY (assigned_agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: job_change_records job_change_records_loop_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_change_records
    ADD CONSTRAINT job_change_records_loop_id_fkey FOREIGN KEY (loop_id) REFERENCES public.project_loops(id) ON DELETE SET NULL;


--
-- Name: job_change_records job_change_records_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_change_records
    ADD CONSTRAINT job_change_records_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


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
-- Name: jobs jobs_created_by_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_created_by_thread_id_fkey FOREIGN KEY (created_by_thread_id) REFERENCES public.threads(id) ON DELETE SET NULL;


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
-- Name: project_contacts project_contacts_added_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_contacts
    ADD CONSTRAINT project_contacts_added_by_fkey FOREIGN KEY (added_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: project_contacts project_contacts_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_contacts
    ADD CONSTRAINT project_contacts_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.contacts(id) ON DELETE CASCADE;


--
-- Name: project_contacts project_contacts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_contacts
    ADD CONSTRAINT project_contacts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


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
-- Name: resource_intervals resource_intervals_inventory_scope_cluster_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_intervals
    ADD CONSTRAINT resource_intervals_inventory_scope_cluster_fkey FOREIGN KEY (inventory_scope_id, source_cluster) REFERENCES public.resource_inventory_scopes(id, source_cluster) ON DELETE RESTRICT;


--
-- Name: resource_intervals resource_intervals_last_seen_snapshot_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_intervals
    ADD CONSTRAINT resource_intervals_last_seen_snapshot_fkey FOREIGN KEY (last_seen_snapshot_id, inventory_scope_id) REFERENCES public.resource_inventory_snapshots(id, inventory_scope_id) ON DELETE RESTRICT;


--
-- Name: resource_intervals resource_intervals_source_lifecycle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_intervals
    ADD CONSTRAINT resource_intervals_source_lifecycle_id_fkey FOREIGN KEY (source_lifecycle_id) REFERENCES public.resource_lifecycle_heads(source_lifecycle_id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_coverage_gaps resource_inventory_coverage_gaps_scope_epoch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_coverage_gaps
    ADD CONSTRAINT resource_inventory_coverage_gaps_scope_epoch_id_fkey FOREIGN KEY (scope_epoch_id) REFERENCES public.resource_inventory_scope_epochs(id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_last_snapshot_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scope_epochs
    ADD CONSTRAINT resource_inventory_scope_epochs_last_snapshot_fkey FOREIGN KEY (last_complete_snapshot_id, id) REFERENCES public.resource_inventory_snapshots(id, scope_epoch_id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_scope_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scope_epochs
    ADD CONSTRAINT resource_inventory_scope_epochs_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES public.resource_inventory_scopes(id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_snapshot_items resource_inventory_snapshot_items_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_snapshot_items
    ADD CONSTRAINT resource_inventory_snapshot_items_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES public.resource_inventory_snapshots(id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_snapshots resource_inventory_snapshots_epoch_scope_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_snapshots
    ADD CONSTRAINT resource_inventory_snapshots_epoch_scope_fkey FOREIGN KEY (scope_epoch_id, inventory_scope_id) REFERENCES public.resource_inventory_scope_epochs(id, scope_id) ON DELETE RESTRICT;


--
-- Name: resource_lifecycle_heads resource_lifecycle_heads_current_interval_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_lifecycle_heads
    ADD CONSTRAINT resource_lifecycle_heads_current_interval_fkey FOREIGN KEY (current_interval_id, source_lifecycle_id) REFERENCES public.resource_intervals(id, source_lifecycle_id) ON DELETE RESTRICT;


--
-- Name: resource_publication_plan_events resource_publication_plan_events_canonical_rate_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_publication_plan_events
    ADD CONSTRAINT resource_publication_plan_events_canonical_rate_version_id_fkey FOREIGN KEY (canonical_rate_version_id) REFERENCES public.usage_rates_v2(id) ON DELETE RESTRICT;


--
-- Name: resource_publication_plan_events resource_publication_plan_events_plan_kind_time_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_publication_plan_events
    ADD CONSTRAINT resource_publication_plan_events_plan_kind_time_fkey FOREIGN KEY (plan_id, event_kind, ts) REFERENCES public.resource_publication_plans(id, plan_kind, period_start) ON DELETE RESTRICT;


--
-- Name: resource_publication_plans resource_publication_plans_interval_revision_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_publication_plans
    ADD CONSTRAINT resource_publication_plans_interval_revision_fkey FOREIGN KEY (source_interval_id, source_revision) REFERENCES public.resource_intervals(id, source_revision) ON DELETE RESTRICT;


--
-- Name: session_wake_events session_wake_events_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_wake_events
    ADD CONSTRAINT session_wake_events_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


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
-- Name: usage_rate_card_rates usage_rate_card_rates_rate_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rate_card_rates
    ADD CONSTRAINT usage_rate_card_rates_rate_card_id_fkey FOREIGN KEY (rate_card_id) REFERENCES public.usage_rate_cards(id) ON DELETE CASCADE;


--
-- Name: usage_rate_card_versions_v2 usage_rate_card_versions_v2_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rate_card_versions_v2
    ADD CONSTRAINT usage_rate_card_versions_v2_card_id_fkey FOREIGN KEY (card_id) REFERENCES public.usage_rate_cards(id) ON DELETE RESTRICT;


--
-- Name: usage_rate_components_v2 usage_rate_components_v2_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rate_components_v2
    ADD CONSTRAINT usage_rate_components_v2_version_id_fkey FOREIGN KEY (version_id) REFERENCES public.usage_rate_card_versions_v2(id) ON DELETE RESTRICT;


--
-- Name: user_api_keys user_api_keys_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_api_keys
    ADD CONSTRAINT user_api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: user_expert_defaults user_expert_defaults_expert_type_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_expert_defaults
    ADD CONSTRAINT user_expert_defaults_expert_type_fkey FOREIGN KEY (expert_id, expert_type) REFERENCES public.experts(id, expert_type) ON DELETE CASCADE;


--
-- Name: user_expert_defaults user_expert_defaults_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_expert_defaults
    ADD CONSTRAINT user_expert_defaults_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


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
