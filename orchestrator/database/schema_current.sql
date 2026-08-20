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
-- Name: append_agent_metering_binding_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.append_agent_metering_binding_event() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    INSERT INTO public.agent_metering_binding_events (
        agent_id, revision, agent_present, pod_uid, hostname,
        identity_state, attribution_scope, owner_kind, owner_id,
        user_id, project_id, reason_code, transition_source, effective_at
    ) VALUES (
        NEW.agent_id, NEW.revision, NEW.agent_present, NEW.pod_uid, NEW.hostname,
        NEW.identity_state, NEW.attribution_scope, NEW.owner_kind, NEW.owner_id,
        NEW.user_id, NEW.project_id, NEW.reason_code, NEW.transition_source,
        NEW.effective_at
    );
    RETURN NULL;
END;
$$;


--
-- Name: audit_officer_ticket_claim_job_delete(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.audit_officer_ticket_claim_job_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE public.officer_ticket_claims
       SET job_deleted_at = COALESCE(job_deleted_at, statement_timestamp()),
           job_status_at_delete = COALESCE(job_status_at_delete, OLD.status),
           deletion_reason = COALESCE(
               deletion_reason,
               'database_delete_compatibility_trigger'
           )
     WHERE job_id = OLD.id;
    RETURN OLD;
END
$$;


--
-- Name: FUNCTION audit_officer_ticket_claim_job_delete(); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.audit_officer_ticket_claim_job_delete() IS '0162 rolling-upgrade backstop: any claimed job DELETE records status/time before the operational row disappears.';


--
-- Name: close_compute_intervals_at_epoch_retirement(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.close_compute_intervals_at_epoch_retirement() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF OLD.retired_at IS NULL AND NEW.retired_at IS NOT NULL THEN
        PERFORM activation.activation_key
        FROM public.compute_metering_epoch_authorities AS authority
        JOIN public.compute_metering_activation AS activation
          ON activation.activation_key = authority.activation_key
        WHERE authority.inventory_scope_epoch_id = OLD.id
        ORDER BY activation.activation_key
        FOR SHARE OF activation;

        IF EXISTS (
            SELECT 1
            FROM public.resource_intervals AS interval
            WHERE interval.compute_scope_epoch_id = OLD.id
              AND interval.ended_at IS NULL
              AND (interval.started_at > NEW.retired_at
                   OR interval.last_seen_at > NEW.retired_at
                   OR interval.last_confirmed_at > NEW.retired_at
                   OR interval.materialized_through > NEW.retired_at)
        ) THEN
            RAISE EXCEPTION
                'compute interval evidence leads epoch retirement clock'
                USING ERRCODE = '55000';
        END IF;

        UPDATE public.resource_intervals AS interval
        SET ended_at = NEW.retired_at,
            end_time_source = 'inventory-epoch-retired',
            end_uncertainty_us = 0,
            end_reason = 'inventory-epoch-retired',
            updated_at = statement_timestamp()
        WHERE interval.compute_scope_epoch_id = OLD.id
          AND interval.ended_at IS NULL;

        UPDATE public.resource_lifecycle_heads AS head
        SET current_interval_id = NULL,
            updated_at = statement_timestamp()
        WHERE head.current_interval_id IN (
            SELECT interval.id
            FROM public.resource_intervals AS interval
            WHERE interval.compute_scope_epoch_id = OLD.id
              AND interval.ended_at = NEW.retired_at
              AND interval.end_reason = 'inventory-epoch-retired'
        );
    ELSIF OLD.retired_at IS NOT NULL
          AND NEW.retired_at IS DISTINCT FROM OLD.retired_at THEN
        RAISE EXCEPTION 'inventory epoch retirement is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: converge_agent_metering_binding(uuid, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.converge_agent_metering_binding(target_agent_id uuid, requested_transition_source text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $_$
DECLARE
    agent_row          RECORD;
    current_row        RECORD;
    job_row            RECORD;
    thread_row         RECORD;
    agent_found        BOOLEAN;
    initial_pod_uid    TEXT;
    normalized_pod_uid TEXT;
    normalized_host    TEXT;
    duplicate_count    BIGINT;
    next_present       BOOLEAN;
    next_identity      TEXT;
    next_scope         TEXT;
    next_owner_kind    TEXT;
    next_owner_id      UUID;
    next_user_id       UUID;
    next_project_id    UUID;
    next_reason        TEXT;
    transition_at      TIMESTAMPTZ;
BEGIN
    IF requested_transition_source IS NULL
       OR requested_transition_source !~ '^[a-z0-9][a-z0-9._-]{0,63}$' THEN
        RAISE EXCEPTION 'agent metering transition source is invalid'
            USING ERRCODE = '22023';
    END IF;

    -- Every convergence path uses Pod identity then agent identity lock order.
    -- Re-read after either wait so a job/thread trigger cannot overwrite a
    -- newer registration transition with the agent row it saw before waiting.
    SELECT agent.id, agent.pod_uid, agent.hostname,
           agent.current_job_id, agent.thread_id
    INTO agent_row
    FROM public.agents AS agent
    WHERE agent.id = target_agent_id;
    agent_found := FOUND;
    IF agent_found THEN
        initial_pod_uid := NULLIF(btrim(agent_row.pod_uid), '');
        IF initial_pod_uid IS NOT NULL
           AND length(initial_pod_uid) <= 256 THEN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'srw-agent-metering-pod:' || initial_pod_uid,
                    0
                )
            );
        END IF;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'srw-agent-metering-agent:' || target_agent_id::TEXT,
            0
        )
    );
    SELECT agent.id, agent.pod_uid, agent.hostname,
           agent.current_job_id, agent.thread_id
    INTO agent_row
    FROM public.agents AS agent
    WHERE agent.id = target_agent_id;
    agent_found := FOUND;

    IF NOT agent_found THEN
        SELECT * INTO current_row
        FROM public.agent_metering_pod_identity_state
        WHERE agent_id = target_agent_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RETURN;
        END IF;
        normalized_pod_uid := current_row.pod_uid;
        normalized_host := current_row.hostname;
        next_present := FALSE;
        next_identity := 'missing';
        next_scope := 'unknown';
        next_owner_kind := NULL;
        next_owner_id := NULL;
        next_user_id := NULL;
        next_project_id := NULL;
        next_reason := 'agent-row-deleted';
    ELSE
        normalized_pod_uid := NULLIF(btrim(agent_row.pod_uid), '');
        IF normalized_pod_uid IS NOT NULL
           AND length(normalized_pod_uid) > 256 THEN
            normalized_pod_uid := NULL;
        END IF;
        normalized_host := NULLIF(btrim(agent_row.hostname), '');
        IF normalized_host IS NOT NULL AND length(normalized_host) > 255 THEN
            normalized_host := NULL;
        END IF;
        next_present := TRUE;
        next_owner_kind := NULL;
        next_owner_id := NULL;
        next_user_id := NULL;
        next_project_id := NULL;

        IF normalized_pod_uid IS NULL THEN
            next_identity := 'missing';
            next_scope := 'unknown';
            next_reason := 'missing-pod-uid';
        ELSE
            -- The caller already holds the observed Pod-identity lock. Agent
            -- row triggers additionally pre-lock both old and new UIDs, so
            -- simultaneous re-registrations cannot form a peer-state cycle.
            SELECT count(*) INTO duplicate_count
            FROM public.agents AS peer
            WHERE NULLIF(btrim(peer.pod_uid), '') = normalized_pod_uid;

            IF duplicate_count > 1 THEN
                next_identity := 'duplicate';
                next_scope := 'unknown';
                next_reason := 'duplicate-pod-uid';
            ELSIF agent_row.current_job_id IS NOT NULL
                  AND agent_row.thread_id IS NOT NULL THEN
                next_identity := 'valid';
                next_scope := 'unknown';
                next_reason := 'dual-owner-conflict';
            ELSIF agent_row.current_job_id IS NOT NULL THEN
                SELECT job.id, job.user_id, job.project_id,
                       job.status, job.assigned_agent_id
                INTO job_row
                FROM public.jobs AS job
                WHERE job.id = agent_row.current_job_id;
                IF FOUND
                   AND job_row.status = 'processing'
                   AND job_row.assigned_agent_id = target_agent_id
                   AND job_row.user_id IS NOT NULL THEN
                    next_identity := 'valid';
                    next_scope := 'customer';
                    next_owner_kind := 'job';
                    next_owner_id := job_row.id;
                    next_user_id := job_row.user_id;
                    next_project_id := job_row.project_id;
                    next_reason := 'job-mutual-binding';
                ELSE
                    next_identity := 'valid';
                    next_scope := 'unknown';
                    next_reason := 'job-binding-conflict';
                END IF;
            ELSIF agent_row.thread_id IS NOT NULL THEN
                SELECT thread.id, thread.user_id, thread.project_id,
                       thread.status, thread.agent_id
                INTO thread_row
                FROM public.threads AS thread
                WHERE thread.id = agent_row.thread_id;
                IF FOUND
                   AND thread_row.status IN ('active', 'awaiting_user')
                   AND thread_row.agent_id = target_agent_id
                   AND thread_row.user_id IS NOT NULL THEN
                    next_identity := 'valid';
                    next_scope := 'customer';
                    next_owner_kind := 'thread';
                    next_owner_id := thread_row.id;
                    next_user_id := thread_row.user_id;
                    next_project_id := thread_row.project_id;
                    next_reason := 'thread-mutual-binding';
                ELSE
                    next_identity := 'valid';
                    next_scope := 'unknown';
                    next_reason := 'thread-binding-conflict';
                END IF;
            ELSE
                next_identity := 'valid';
                next_scope := 'shared-platform';
                next_reason := 'unbound-agent';
            END IF;
        END IF;
    END IF;

    SELECT * INTO current_row
    FROM public.agent_metering_pod_identity_state
    WHERE agent_id = target_agent_id
    FOR UPDATE;

    IF NOT FOUND THEN
        transition_at := clock_timestamp();
        INSERT INTO public.agent_metering_pod_identity_state (
            agent_id, agent_present, pod_uid, hostname, identity_state,
            attribution_scope, owner_kind, owner_id, user_id, project_id,
            reason_code, transition_source, revision, effective_at
        ) VALUES (
            target_agent_id, next_present, normalized_pod_uid, normalized_host,
            next_identity, next_scope, next_owner_kind, next_owner_id,
            next_user_id, next_project_id, next_reason,
            requested_transition_source, 1, transition_at
        );
        RETURN;
    END IF;

    IF current_row.agent_present IS NOT DISTINCT FROM next_present
       AND current_row.pod_uid IS NOT DISTINCT FROM normalized_pod_uid
       AND current_row.hostname IS NOT DISTINCT FROM normalized_host
       AND current_row.identity_state IS NOT DISTINCT FROM next_identity
       AND current_row.attribution_scope IS NOT DISTINCT FROM next_scope
       AND current_row.owner_kind IS NOT DISTINCT FROM next_owner_kind
       AND current_row.owner_id IS NOT DISTINCT FROM next_owner_id
       AND current_row.user_id IS NOT DISTINCT FROM next_user_id
       AND current_row.project_id IS NOT DISTINCT FROM next_project_id
       AND current_row.reason_code IS NOT DISTINCT FROM next_reason THEN
        RETURN;
    END IF;

    -- statement_timestamp() is fixed before any advisory/row-lock wait. A
    -- concurrent statement may therefore resume after a newer revision while
    -- still carrying an older timestamp. Sample the wall clock only after the
    -- current head is locked and clamp it to the durable head so revisions can
    -- never move effective_at or updated_at backwards.
    transition_at := GREATEST(clock_timestamp(), current_row.effective_at);
    UPDATE public.agent_metering_pod_identity_state
    SET agent_present = next_present,
        pod_uid = normalized_pod_uid,
        hostname = normalized_host,
        identity_state = next_identity,
        attribution_scope = next_scope,
        owner_kind = next_owner_kind,
        owner_id = next_owner_id,
        user_id = next_user_id,
        project_id = next_project_id,
        reason_code = next_reason,
        transition_source = requested_transition_source,
        revision = current_row.revision + 1,
        effective_at = transition_at,
        updated_at = transition_at
    WHERE agent_id = target_agent_id;
END;
$_$;


--
-- Name: converge_agent_metering_from_agent_row(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.converge_agent_metering_from_agent_row() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    old_pod_uid TEXT;
    new_pod_uid TEXT;
    locked_uid  TEXT;
    peer_id     UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        old_pod_uid := NULLIF(btrim(OLD.pod_uid), '');
    ELSE
        new_pod_uid := NULLIF(btrim(NEW.pod_uid), '');
        IF TG_OP = 'UPDATE' THEN
            old_pod_uid := NULLIF(btrim(OLD.pod_uid), '');
        END IF;
    END IF;

    -- A row update is already visible to its own trigger but not to a
    -- concurrent peer. Lock both sides in lexical order before touching any
    -- metering head; this prevents old-UID peer convergence deadlocks.
    FOR locked_uid IN
        SELECT candidate.uid
        FROM (
            SELECT old_pod_uid AS uid
            UNION
            SELECT new_pod_uid AS uid
        ) AS candidate
        WHERE candidate.uid IS NOT NULL AND length(candidate.uid) <= 256
        ORDER BY candidate.uid
    LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                'srw-agent-metering-pod:' || locked_uid,
                0
            )
        );
    END LOOP;

    IF TG_OP = 'DELETE' THEN
        PERFORM public.converge_agent_metering_binding(
            OLD.id, 'agents-delete'
        );
        -- Do not row-lock peers here. The outer DELETE already owns OLD's
        -- tuple lock, while a concurrent peer mutation owns its own tuple
        -- lock before its AFTER trigger can wait on the Pod advisory lock.
        -- Waiting for that peer row would invert those locks and deadlock;
        -- SKIP LOCKED, conversely, can leave the survivor permanently marked
        -- duplicate. The Pod advisory lock serializes changes for this UID,
        -- and a plain MVCC read lets this transaction converge committed
        -- peers without waiting. A concurrent peer mutation runs its own
        -- trigger after this transaction releases the UID lock and is the
        -- final-state repair.
        FOR peer_id IN
            SELECT agent.id FROM public.agents AS agent
            WHERE old_pod_uid IS NOT NULL
              AND NULLIF(btrim(agent.pod_uid), '') = old_pod_uid
            ORDER BY agent.id
        LOOP
            PERFORM public.converge_agent_metering_binding(
                peer_id, 'agents-delete-peer'
            );
        END LOOP;
        RETURN OLD;
    END IF;

    PERFORM public.converge_agent_metering_binding(
        NEW.id,
        CASE WHEN TG_OP = 'INSERT' THEN 'agents-insert' ELSE 'agents-update' END
    );
    -- Peer discovery intentionally remains a non-locking MVCC read; see the
    -- DELETE path above for the row-lock/advisory-lock ordering contract.
    FOR peer_id IN
        SELECT agent.id FROM public.agents AS agent
        WHERE agent.id <> NEW.id
          AND ((new_pod_uid IS NOT NULL
                AND NULLIF(btrim(agent.pod_uid), '') = new_pod_uid)
               OR (old_pod_uid IS NOT NULL
                AND NULLIF(btrim(agent.pod_uid), '') = old_pod_uid))
        ORDER BY agent.id
    LOOP
        PERFORM public.converge_agent_metering_binding(
            peer_id, 'agents-identity-peer'
        );
    END LOOP;
    RETURN NEW;
END;
$$;


--
-- Name: converge_agent_metering_from_job_row(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.converge_agent_metering_from_job_row() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    target_job_id UUID;
    old_agent_id  UUID;
    new_agent_id  UUID;
    locked_uid    TEXT;
    peer_id       UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target_job_id := OLD.id;
        old_agent_id := OLD.assigned_agent_id;
    ELSIF TG_OP = 'INSERT' THEN
        target_job_id := NEW.id;
        new_agent_id := NEW.assigned_agent_id;
    ELSE
        target_job_id := NEW.id;
        old_agent_id := OLD.assigned_agent_id;
        new_agent_id := NEW.assigned_agent_id;
    END IF;

    -- One owner transition may converge more than one inconsistent/transitional
    -- agent row. Advisory locks live until transaction end, so acquiring them
    -- indirectly in agent UUID order can conflict with the lexical old/new UID
    -- order used by an agent-row trigger. Prelock the complete visible UID set
    -- lexically before touching any metering head. Concurrent agent mutations
    -- include their old UID in the same lock protocol and perform the final
    -- repair after this transaction when their uncommitted row was not visible.
    FOR locked_uid IN
        SELECT candidate.uid
        FROM (
            SELECT DISTINCT NULLIF(btrim(agent.pod_uid), '') AS uid
            FROM public.agents AS agent
            WHERE agent.current_job_id = target_job_id
               OR agent.id = old_agent_id
               OR agent.id = new_agent_id
        ) AS candidate
        WHERE candidate.uid IS NOT NULL AND length(candidate.uid) <= 256
        ORDER BY candidate.uid
    LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                'srw-agent-metering-pod:' || locked_uid,
                0
            )
        );
    END LOOP;

    FOR peer_id IN
        SELECT agent.id FROM public.agents AS agent
        WHERE agent.current_job_id = target_job_id
           OR agent.id = old_agent_id
           OR agent.id = new_agent_id
        ORDER BY agent.id
    LOOP
        PERFORM public.converge_agent_metering_binding(
            peer_id,
            CASE WHEN TG_OP = 'INSERT' THEN 'jobs-insert'
                 WHEN TG_OP = 'DELETE' THEN 'jobs-delete'
                 ELSE 'jobs-update' END
        );
    END LOOP;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: converge_agent_metering_from_thread_row(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.converge_agent_metering_from_thread_row() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    target_thread_id UUID;
    old_agent_id     UUID;
    new_agent_id     UUID;
    locked_uid       TEXT;
    peer_id          UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target_thread_id := OLD.id;
        old_agent_id := OLD.agent_id;
    ELSIF TG_OP = 'INSERT' THEN
        target_thread_id := NEW.id;
        new_agent_id := NEW.agent_id;
    ELSE
        target_thread_id := NEW.id;
        old_agent_id := OLD.agent_id;
        new_agent_id := NEW.agent_id;
    END IF;

    -- Match the job-trigger lock contract: collect every currently visible Pod
    -- identity first and acquire the transaction locks in one lexical order.
    FOR locked_uid IN
        SELECT candidate.uid
        FROM (
            SELECT DISTINCT NULLIF(btrim(agent.pod_uid), '') AS uid
            FROM public.agents AS agent
            WHERE agent.thread_id = target_thread_id
               OR agent.id = old_agent_id
               OR agent.id = new_agent_id
        ) AS candidate
        WHERE candidate.uid IS NOT NULL AND length(candidate.uid) <= 256
        ORDER BY candidate.uid
    LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                'srw-agent-metering-pod:' || locked_uid,
                0
            )
        );
    END LOOP;

    FOR peer_id IN
        SELECT agent.id FROM public.agents AS agent
        WHERE agent.thread_id = target_thread_id
           OR agent.id = old_agent_id
           OR agent.id = new_agent_id
        ORDER BY agent.id
    LOOP
        PERFORM public.converge_agent_metering_binding(
            peer_id,
            CASE WHEN TG_OP = 'INSERT' THEN 'threads-insert'
                 WHEN TG_OP = 'DELETE' THEN 'threads-delete'
                 ELSE 'threads-update' END
        );
    END LOOP;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: enforce_inventory_epoch_required_boundary(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_inventory_epoch_required_boundary() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    durable_state TEXT;
    durable_cutover TIMESTAMPTZ;
BEGIN
    SELECT cutover_state, cutover_at
    INTO durable_state, durable_cutover
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;

    IF TG_OP = 'UPDATE'
       AND OLD.required_for_rollup
       AND OLD.required_from IS NOT NULL
       AND OLD.required_from = durable_cutover
       AND (NEW.required_for_rollup IS DISTINCT FROM OLD.required_for_rollup
            OR NEW.required_from IS DISTINCT FROM OLD.required_from) THEN
        RAISE EXCEPTION 'initial cutover inventory boundary is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.required_from IS NULL
       OR NEW.required_from = date_trunc('day', NEW.required_from, 'UTC') THEN
        RETURN NEW;
    END IF;

    IF durable_state NOT IN ('preparing', 'active')
       OR durable_cutover IS NULL
       OR NEW.required_from IS DISTINCT FROM durable_cutover THEN
        RAISE EXCEPTION
            'inventory requirement must begin at UTC midnight or durable cutover'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: enforce_legacy_workspace_cutover_barrier(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_legacy_workspace_cutover_barrier() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    current_state TEXT;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.ended_at IS NOT NULL
           AND NEW.ended_at IS DISTINCT FROM OLD.ended_at THEN
            RAISE EXCEPTION 'closed legacy workspace end is immutable'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    SELECT cutover_state INTO current_state
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;

    IF current_state IS NULL OR current_state <> 'disabled' THEN
        RAISE EXCEPTION 'legacy workspace inserts are disabled by metering cutover'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: enforce_officer_runtime_agent_binding(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_officer_runtime_agent_binding() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.caller_kind = 'officer'
       AND OLD.agent_id IS NOT NULL
       AND NEW.agent_id IS DISTINCT FROM OLD.agent_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'runtime_actor_grants_agent_provenance',
            MESSAGE = 'Officer runtime grant agent provenance is immutable';
    END IF;
    IF NEW.caller_kind = 'officer'
       AND NEW.agent_id IS NULL
       AND NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'runtime_actor_grants_officer_agent_binding',
            MESSAGE = 'Officer runtime grants require an authoritative agent binding',
            HINT = 'Drain pre-0171 orchestrator replicas before serving Officer attach or refresh traffic.';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.caller_kind = 'officer'
       AND OLD.refresh_rotation_required
       AND NEW.refresh_rotation_required
       AND NEW.last_refreshed_at IS DISTINCT FROM OLD.last_refreshed_at THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'runtime_actor_grants_recovery_rotation',
            MESSAGE = 'Recovered Officer runtime grants require refresh rotation',
            HINT = 'Drain pre-0171 orchestrator replicas before serving Officer refresh traffic.';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.caller_kind = 'officer'
       AND OLD.refresh_handoff_ciphertext IS NOT NULL
       AND NEW.last_refreshed_at IS DISTINCT FROM OLD.last_refreshed_at
       AND NEW.last_maintenance_at IS NOT DISTINCT FROM OLD.last_maintenance_at THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'runtime_actor_grants_rotation_acknowledgement',
            MESSAGE = 'Officer refresh handoffs require acknowledged rotation semantics',
            HINT = 'Drain pre-0171 orchestrator replicas before serving Officer refresh traffic.';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: FUNCTION enforce_officer_runtime_agent_binding(); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.enforce_officer_runtime_agent_binding() IS '0171 mixed-version fence: old replicas fail safely instead of minting or refreshing an unbound Officer authority, rewriting agent provenance, or bypassing acknowledged recovery rotation.';


--
-- Name: enforce_officer_ticket_claim_job_integrity(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_officer_ticket_claim_job_integrity() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    durable_claim public.officer_ticket_claims%ROWTYPE;
    admission     JSONB;
    generation    TIMESTAMPTZ;
    incarnation   INTEGER;
    lineage_size  INTEGER;
    claim_exists  BOOLEAN;
BEGIN
    SELECT *
      INTO durable_claim
      FROM public.officer_ticket_claims
     WHERE job_id = NEW.id;
    claim_exists := FOUND;

    IF NOT (COALESCE(NEW.context, '{}'::jsonb) ? 'ticket_note_id') THEN
        IF claim_exists THEN
            RAISE EXCEPTION 'claimed job % cannot remove its server-owned ticket/admission provenance', NEW.id
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'officer_ticket_claim_job_integrity';
        END IF;
        RETURN NEW;
    END IF;

    IF NOT claim_exists THEN
        RAISE EXCEPTION 'ticket-bearing job % has no durable Officer claim; retry after rolling upgrade', NEW.id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'officer_ticket_claim_job_integrity',
                  HINT = 'Old replicas cannot dispatch ticket work after migration 0162; use the post-locked claim+job admission path.';
    END IF;

    admission := COALESCE(NEW.context, '{}'::jsonb)->'officer_admission';

    -- These rows are a quarantine boundary, not recovered admission
    -- authority. Preserve the observable job identity while allowing ordinary
    -- context merges against genuine stamp-less/partial historical rows. Any
    -- admission-looking JSON on such a job remains non-authoritative because
    -- the immutable ledger source/generation shape, not jobs.context, governs
    -- eligibility. Only a new post-locked claim can consume a post-cutover
    -- ready_at.
    IF durable_claim.source = 'legacy_unversioned' THEN
        IF NEW.project_id IS NULL
           OR durable_claim.project_id IS DISTINCT FROM NEW.project_id
           OR durable_claim.ticket_note_id
                  IS DISTINCT FROM NEW.context->>'ticket_note_id'
           OR (
               durable_claim.officer_thread_id IS NOT NULL
               AND durable_claim.officer_thread_id
                      IS DISTINCT FROM NEW.created_by_thread_id
           )
           OR durable_claim.officer_slot
                  IS DISTINCT FROM NEW.context->>'officer_slot'
           OR durable_claim.work_category
                  IS DISTINCT FROM NEW.context->>'work_category' THEN
            RAISE EXCEPTION 'legacy ticket-bearing job % does not match its durable cutover barrier', NEW.id
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'officer_ticket_claim_job_integrity';
        END IF;
        RETURN NEW;
    END IF;

    IF jsonb_typeof(admission) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'ticket-bearing job % has no server Officer admission provenance', NEW.id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'officer_ticket_claim_job_integrity';
    END IF;
    BEGIN
        generation := (admission->>'ticket_ready_at')::timestamptz;
        incarnation := (admission->>'incarnation')::integer;
        lineage_size := (admission->>'lineage_size')::integer;
    EXCEPTION
        WHEN invalid_text_representation
           OR invalid_datetime_format
           OR datetime_field_overflow
           OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'ticket-bearing job % has invalid server Officer admission provenance', NEW.id
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'officer_ticket_claim_job_integrity';
    END;

    IF generation IS NULL OR NOT isfinite(generation)
       OR incarnation IS NULL OR incarnation < 0
       OR lineage_size IS NULL
       OR lineage_size IS DISTINCT FROM incarnation + 1 THEN
        RAISE EXCEPTION 'ticket-bearing job % has missing or invalid Officer generation/incarnation/lineage provenance', NEW.id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'officer_ticket_claim_job_integrity';
    END IF;

    IF NEW.project_id IS NULL
       OR durable_claim.project_id IS DISTINCT FROM NEW.project_id
       OR durable_claim.ticket_note_id
              IS DISTINCT FROM NEW.context->>'ticket_note_id'
       OR durable_claim.ready_generation_at IS DISTINCT FROM generation
       OR (
           durable_claim.source = 'backfill'
           AND admission ? 'ticket_claim_source'
       )
       OR (
           durable_claim.source <> 'backfill'
           AND durable_claim.source
                  IS DISTINCT FROM admission->>'ticket_claim_source'
       )
       OR durable_claim.officer_thread_id
              IS DISTINCT FROM NEW.created_by_thread_id
       OR durable_claim.officer_thread_id::text
              IS DISTINCT FROM admission->>'thread_id'
       OR durable_claim.project_id::text
              IS DISTINCT FROM admission->>'project_id'
       OR durable_claim.officer_incarnation IS DISTINCT FROM incarnation
       OR durable_claim.officer_slot
              IS DISTINCT FROM NEW.context->>'officer_slot'
       OR durable_claim.officer_slot
              IS DISTINCT FROM admission->>'slot'
       OR durable_claim.work_category
              IS DISTINCT FROM NEW.context->>'work_category'
       OR durable_claim.work_category
              IS DISTINCT FROM admission->>'category'
       OR durable_claim.admission_config_fingerprint
              IS DISTINCT FROM admission->>'config_fingerprint'
       OR durable_claim.admission_lineage_size IS DISTINCT FROM lineage_size THEN
        RAISE EXCEPTION 'ticket-bearing job % does not match its durable Officer claim', NEW.id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'officer_ticket_claim_job_integrity';
    END IF;

    RETURN NEW;
END
$$;


--
-- Name: FUNCTION enforce_officer_ticket_claim_job_integrity(); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.enforce_officer_ticket_claim_job_integrity() IS '0162 rolling-upgrade backstop: a ticket-bearing jobs row must match a durable claim already visible in the same transaction; legacy_unversioned rows remain non-authoritative cutover barriers.';


--
-- Name: enforce_resource_interval_compute_epoch_authority(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_resource_interval_compute_epoch_authority() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    required_key         TEXT;
    activation_state     TEXT;
    activation_boundary  TIMESTAMPTZ;
    epoch_retired_at     TIMESTAMPTZ;
    authority_boundary   TIMESTAMPTZ;
BEGIN
    IF NEW.source_kind = 'pod'
       AND NEW.category = 'compute'
       AND NEW.resource = 'agent_pod' THEN
        required_key := 'agent_pod';
    ELSIF NEW.source_kind = 'pod'
          AND NEW.category = 'compute'
          AND NEW.resource = 'workspace_pod'
          AND NEW.details->>'product_class' = 'ide-session' THEN
        required_key := 'ide_workspace_pod';
    ELSIF NEW.source_kind = 'vmi'
          AND NEW.category = 'compute'
          AND NEW.resource = 'workspace_vm' THEN
        required_key := 'workspace_vm';
    ELSE
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE'
       AND NEW.compute_scope_epoch_id IS DISTINCT FROM OLD.compute_scope_epoch_id THEN
        RAISE EXCEPTION 'compute interval epoch binding is immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT epoch.retired_at
    INTO epoch_retired_at
    FROM public.resource_inventory_scope_epochs AS epoch
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = epoch.scope_id
    WHERE epoch.id = NEW.compute_scope_epoch_id
      AND epoch.scope_id = NEW.inventory_scope_id
      AND scope.source_cluster = NEW.source_cluster
    FOR SHARE OF epoch;

    SELECT activation.state, activation.activated_at
    INTO activation_state, activation_boundary
    FROM public.compute_metering_activation AS activation
    WHERE activation.activation_key = required_key
    FOR SHARE;

    SELECT authority.effective_from
    INTO authority_boundary
    FROM public.compute_metering_epoch_authorities AS authority
    WHERE authority.activation_key = required_key
      AND authority.inventory_scope_id = NEW.inventory_scope_id
      AND authority.inventory_scope_epoch_id = NEW.compute_scope_epoch_id;

    IF activation_state IS DISTINCT FROM 'active'
       OR activation_boundary IS NULL
       OR statement_timestamp() < activation_boundary
       OR authority_boundary IS NULL
       OR statement_timestamp() < authority_boundary
       OR NEW.started_at < GREATEST(activation_boundary, authority_boundary) THEN
        RAISE EXCEPTION
            'compute product class % lacks bound exact epoch authority',
            required_key
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' AND epoch_retired_at IS NOT NULL THEN
        RAISE EXCEPTION 'compute interval exact epoch is retired'
            USING ERRCODE = '55000';
    END IF;
    IF epoch_retired_at IS NOT NULL
       AND (NEW.last_seen_at > epoch_retired_at
            OR NEW.last_confirmed_at > epoch_retired_at
            OR NEW.materialized_through > epoch_retired_at
            OR (NEW.ended_at IS NOT NULL
                AND NEW.ended_at > epoch_retired_at)) THEN
        RAISE EXCEPTION 'compute interval mutation exceeds epoch retirement'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: enforce_resource_interval_storage_activation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_resource_interval_storage_activation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    global_state     TEXT;
    global_boundary  TIMESTAMPTZ;
    source_state     TEXT;
    source_boundary  TIMESTAMPTZ;
    effective_boundary TIMESTAMPTZ;
BEGIN
    IF NEW.measurement_basis NOT IN (
        'claim-requested', 'volume-provisioned'
    ) THEN
        RETURN NEW;
    END IF;

    SELECT global_activation.state, global_activation.activated_at,
           source_activation.state, source_activation.activated_at
    INTO global_state, global_boundary, source_state, source_boundary
    FROM public.resource_inventory_scopes AS scope
    JOIN public.storage_metering_source_requirements AS requirement
      ON requirement.inventory_scope_id = scope.id
     AND requirement.measurement_basis = NEW.measurement_basis
     AND requirement.collector_id = scope.collector_id
     AND requirement.source_cluster = scope.source_cluster
     AND requirement.requirement_role = 'quantity'
    JOIN public.storage_metering_source_activations AS source_activation
      ON source_activation.measurement_basis = requirement.measurement_basis
     AND source_activation.collector_id = requirement.collector_id
     AND source_activation.source_cluster = requirement.source_cluster
    JOIN public.storage_metering_activation AS global_activation
      ON global_activation.measurement_basis = requirement.measurement_basis
    WHERE scope.id = NEW.inventory_scope_id
    FOR SHARE OF source_activation, global_activation;

    effective_boundary := GREATEST(global_boundary, source_boundary);
    IF global_state IS DISTINCT FROM 'active'
       OR source_state IS DISTINCT FROM 'active'
       OR effective_boundary IS NULL
       OR statement_timestamp() < effective_boundary
       OR NEW.started_at < effective_boundary THEN
        RAISE EXCEPTION
            'storage interval source is not active at its clamped boundary'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: enforce_resource_inventory_item_fence(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_resource_inventory_item_fence() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    target_snapshot_id UUID;
    fence_ok BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    target_snapshot_id := NEW.snapshot_id;

    SELECT TRUE INTO fence_ok
    FROM public.resource_inventory_snapshots snapshot
    JOIN public.resource_inventory_scope_epochs epoch
      ON epoch.id = snapshot.scope_epoch_id
    JOIN public.resource_inventory_ingest_tickets ticket
      ON ticket.id = snapshot.ingest_ticket_id
     AND ticket.scope_epoch_id = snapshot.scope_epoch_id
    JOIN public.infra_metering_control control ON control.singleton = TRUE
    WHERE snapshot.id = target_snapshot_id
      AND snapshot.manifest_state = 'staging'
      AND epoch.retired_at IS NULL
      AND snapshot.leader_generation = control.leader_generation
      AND ticket.leader_generation = control.leader_generation
      AND ticket.bound_snapshot_id = snapshot.id
      AND ticket.consumed_at IS NULL
      AND ticket.expires_at > statement_timestamp();

    IF fence_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'snapshot item ingestion fence failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: enforce_resource_inventory_snapshot_fence(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_resource_inventory_snapshot_fence() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    current_generation BIGINT;
    epoch_retired_at TIMESTAMPTZ;
    ticket_generation BIGINT;
    ticket_expires_at TIMESTAMPTZ;
    ticket_bound_snapshot_id UUID;
    ticket_consumed_at TIMESTAMPTZ;
    ticket_max_items INTEGER;
    ticket_max_bytes BIGINT;
    ticket_staged_bytes BIGINT;
    actual_staged_bytes BIGINT;
BEGIN
    IF TG_OP = 'UPDATE'
       AND (
            (OLD.manifest_state = 'sealed'
                AND NEW.manifest_state = 'items-expired')
            OR (OLD.manifest_state = 'staging'
                AND NEW.manifest_state = 'staging-expired')
       ) THEN
        RETURN NEW;
    END IF;

    SELECT leader_generation INTO current_generation
    FROM public.infra_metering_control
    WHERE singleton = TRUE;

    SELECT retired_at INTO epoch_retired_at
    FROM public.resource_inventory_scope_epochs
    WHERE id = NEW.scope_epoch_id;

    SELECT leader_generation, expires_at, bound_snapshot_id, consumed_at,
           max_snapshot_items, max_snapshot_bytes, staged_bytes
    INTO ticket_generation, ticket_expires_at,
         ticket_bound_snapshot_id, ticket_consumed_at,
         ticket_max_items, ticket_max_bytes, ticket_staged_bytes
    FROM public.resource_inventory_ingest_tickets
    WHERE id = NEW.ingest_ticket_id
      AND scope_epoch_id = NEW.scope_epoch_id;

    IF current_generation IS NULL
       OR epoch_retired_at IS NOT NULL
       OR NEW.ingest_ticket_id IS NULL
       OR ticket_generation IS NULL
       OR NEW.leader_generation <> current_generation
       OR ticket_generation <> current_generation
       OR ticket_consumed_at IS NOT NULL
       OR ticket_expires_at <= statement_timestamp()
       OR (ticket_bound_snapshot_id IS NOT NULL
           AND ticket_bound_snapshot_id <> NEW.id) THEN
        RAISE EXCEPTION
            'snapshot ingestion ticket/generation/scope epoch fence failed'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'UPDATE'
       AND OLD.manifest_state = 'staging'
       AND NEW.manifest_state = 'sealed' THEN
        SELECT COALESCE(sum(
            public.resource_inventory_snapshot_item_size_bytes(
                item.source_kind, item.source_uid, item.revision_hash,
                item.normalized_item, item.item_error
            )
        ), 0)
        INTO actual_staged_bytes
        FROM public.resource_inventory_snapshot_items item
        WHERE item.snapshot_id = NEW.id;

        IF NEW.leader_generation IS DISTINCT FROM OLD.leader_generation
            OR NEW.ingest_ticket_id IS DISTINCT FROM OLD.ingest_ticket_id
            OR ticket_bound_snapshot_id IS DISTINCT FROM NEW.id
            OR jsonb_typeof(NEW.reconciliation_summary)
                IS DISTINCT FROM 'object'
            OR NEW.item_count > ticket_max_items
            OR ticket_staged_bytes <> actual_staged_bytes
            OR actual_staged_bytes > ticket_max_bytes THEN
            RAISE EXCEPTION 'snapshot fence identity/bounds failed at seal'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: lock_inventory_epoch_boundary_statement(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.lock_inventory_epoch_boundary_statement() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    control_exists BOOLEAN;
BEGIN
    SELECT TRUE INTO control_exists
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;

    IF control_exists IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'infra metering control row is missing'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$;


--
-- Name: lock_legacy_workspace_insert_statement(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.lock_legacy_workspace_insert_statement() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    control_exists BOOLEAN;
BEGIN
    SELECT TRUE INTO control_exists
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;

    IF control_exists IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'infra metering control row is missing'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$;


--
-- Name: mirror_legacy_message_delivery_intent(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.mirror_legacy_message_delivery_intent() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.direction = 'outbound'
       AND NEW.status <> 'rate_limited'
       AND NEW.effective_audience = 'legacy_human' THEN
        INSERT INTO public.message_delivery_intents (
            routing_generation, job_id, project_id, user_id, bucket,
            effective_audience, state, reserved_at, accepted_at, metadata
        )
        SELECT NEW.routing_generation,
               NEW.job_id,
               j.project_id,
               NEW.user_id,
               'human',
               'legacy_human',
               CASE WHEN NEW.status IN ('sent', 'delivered')
                    THEN 'accepted' ELSE 'failed' END,
               NEW.created_at,
               CASE WHEN NEW.status IN ('sent', 'delivered')
                    THEN NEW.created_at ELSE NULL END,
               jsonb_build_object('legacy_replica', true, 'message_id', NEW.id)
          FROM (SELECT 1) AS one
          LEFT JOIN public.jobs j ON j.id = NEW.job_id
        ON CONFLICT (routing_generation, bucket) DO NOTHING;
    END IF;
    RETURN NEW;
END;
$$;


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
-- Name: notify_thread_control_request(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.notify_thread_control_request() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify(
        'thread_control_requests',
        json_build_object(
            'id', NEW.id,
            'thread_id', NEW.thread_id,
            'request_seq', NEW.request_seq
        )::text
    );
    RETURN NEW;
END;
$$;


--
-- Name: notify_thread_interrupt_request(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.notify_thread_interrupt_request() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify(
        'thread_interrupt_requests',
        json_build_object(
            'id', NEW.id,
            'thread_id', NEW.thread_id,
            'lease_token', NEW.accepted_lease_token,
            'turn_id', NEW.target_turn_id
        )::text
    );
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
-- Name: protect_agent_metering_binding_event_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_agent_metering_binding_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    RAISE EXCEPTION 'agent metering binding events are append-only'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_agent_metering_identity_state_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_agent_metering_identity_state_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'agent metering identity state cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.revision <> 1 THEN
            RAISE EXCEPTION 'agent metering identity state must begin at revision 1'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.revision <> OLD.revision + 1
       OR NEW.effective_at < OLD.effective_at
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'agent metering identity state transition is invalid'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_compute_epoch_promotion_request(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_compute_epoch_promotion_request() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'compute epoch promotion requests are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.promoted_at IS DISTINCT FROM statement_timestamp()
       OR NEW.created_at IS DISTINCT FROM statement_timestamp() THEN
        RAISE EXCEPTION 'compute epoch promotion must use the database clock'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_compute_metering_activation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_compute_metering_activation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    requirement_count BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'compute activation rows cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'disabled' OR NEW.activated_at IS NOT NULL THEN
            RAISE EXCEPTION 'compute activation rows must begin disabled'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.activation_key IS DISTINCT FROM OLD.activation_key
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'compute activation identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'disabled' AND NEW.state = 'shadow'
       AND NEW.activated_at IS NULL THEN
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF OLD.state = 'shadow' AND NEW.state = 'active'
       AND NEW.activated_at IS NOT NULL
       AND NEW.activated_at = date_trunc('day', NEW.activated_at, 'UTC')
       AND NEW.activated_at > statement_timestamp() THEN
        SELECT count(*)
        INTO requirement_count
        FROM public.compute_metering_scope_requirements AS requirement
        WHERE requirement.activation_key = OLD.activation_key;
        IF requirement_count = 0 OR EXISTS (
            SELECT 1
            FROM public.compute_metering_scope_requirements AS requirement
            LEFT JOIN public.compute_metering_epoch_authorities AS authority
              ON authority.activation_key = requirement.activation_key
             AND authority.inventory_scope_id = requirement.inventory_scope_id
             AND authority.inventory_scope_epoch_id =
                 requirement.inventory_scope_epoch_id
             AND authority.authority_sequence = 1
             AND authority.effective_from = requirement.required_from
            LEFT JOIN public.resource_inventory_scope_epochs AS epoch
              ON epoch.id = requirement.inventory_scope_epoch_id
             AND epoch.scope_id = requirement.inventory_scope_id
            WHERE requirement.activation_key = OLD.activation_key
              AND (requirement.required_from IS DISTINCT FROM NEW.activated_at
                   OR authority.id IS NULL
                   OR epoch.id IS NULL
                   OR epoch.retired_at IS NOT NULL
                   OR NOT epoch.required_for_rollup
                   OR epoch.required_from IS NULL
                   OR epoch.required_from > requirement.required_from)
        ) THEN
            RAISE EXCEPTION
                'compute activation requires audited exact epoch authority'
                USING ERRCODE = '55000';
        END IF;
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF NEW.state IS NOT DISTINCT FROM OLD.state
       AND NEW.activated_at IS NOT DISTINCT FROM OLD.activated_at
       AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'compute activation permits only disabled -> shadow -> future active'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_compute_metering_epoch_authority(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_compute_metering_epoch_authority() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    activation_state       TEXT;
    activation_boundary    TIMESTAMPTZ;
    request_key            TEXT;
    request_kind           TEXT;
    request_collector      TEXT;
    request_cluster        TEXT;
    request_promoted_at    TIMESTAMPTZ;
    current_generation     BIGINT;
    scope_resource         TEXT;
    scope_namespace        TEXT;
    epoch_recovery_from    UUID;
    epoch_retired_at       TIMESTAMPTZ;
    previous_epoch_id      UUID;
    previous_sequence      BIGINT;
    previous_retired_at    TIMESTAMPTZ;
    snapshot_item_count    BIGINT;
    snapshot_generation    BIGINT;
    snapshot_is_proof      BOOLEAN;
    shadow_count           BIGINT;
    missing_shadow_count   BIGINT;
    orphan_shadow_count    BIGINT;
    lineage_reaches_prior  BOOLEAN;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'compute epoch authorities are append-only'
            USING ERRCODE = '55000';
    END IF;

    SELECT epoch.recovery_from_epoch_id, epoch.retired_at,
           scope.api_resource, scope.namespace
    INTO epoch_recovery_from, epoch_retired_at,
         scope_resource, scope_namespace
    FROM public.resource_inventory_scope_epochs AS epoch
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = epoch.scope_id
     AND scope.collector_id = NEW.collector_id
     AND scope.source_cluster = NEW.source_cluster
    WHERE epoch.id = NEW.inventory_scope_epoch_id
      AND epoch.scope_id = NEW.inventory_scope_id
    FOR SHARE OF epoch;

    SELECT activation.state, activation.activated_at
    INTO activation_state, activation_boundary
    FROM public.compute_metering_activation AS activation
    WHERE activation.activation_key = NEW.activation_key
    FOR SHARE;

    SELECT request.activation_key, request.request_kind,
           request.collector_id, request.source_cluster, request.promoted_at
    INTO request_key, request_kind, request_collector, request_cluster,
         request_promoted_at
    FROM public.compute_metering_epoch_promotion_requests AS request
    WHERE request.id = NEW.promotion_request_id
    FOR SHARE;

    SELECT control.leader_generation
    INTO current_generation
    FROM public.infra_metering_control AS control
    WHERE control.singleton = TRUE
    FOR SHARE;

    IF epoch_retired_at IS NOT NULL
       OR request_key IS DISTINCT FROM NEW.activation_key
       OR request_collector IS DISTINCT FROM NEW.collector_id
       OR request_cluster IS DISTINCT FROM NEW.source_cluster
       OR scope_namespace IS NULL
       OR NOT (
            (NEW.activation_key IN ('agent_pod', 'ide_workspace_pod')
             AND NEW.collector_id = 'kubernetes-pods'
             AND scope_resource = 'core/v1/pods')
            OR (NEW.activation_key = 'workspace_vm'
                AND NEW.collector_id = 'kubevirt-vmis'
                AND scope_resource =
                    'kubevirt.io/v1/virtualmachineinstances')
       ) THEN
        RAISE EXCEPTION 'compute epoch authority identity is invalid'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.authority_sequence = 1 THEN
        IF activation_state IS DISTINCT FROM 'shadow'
           OR request_kind IS DISTINCT FROM 'initial-activation'
           OR epoch_recovery_from IS NOT NULL
           OR NOT EXISTS (
                SELECT 1
                FROM public.compute_metering_scope_requirements AS requirement
                WHERE requirement.activation_key = NEW.activation_key
                  AND requirement.collector_id = NEW.collector_id
                  AND requirement.source_cluster = NEW.source_cluster
                  AND requirement.inventory_scope_id = NEW.inventory_scope_id
                  AND requirement.inventory_scope_epoch_id =
                      NEW.inventory_scope_epoch_id
                  AND requirement.required_from = NEW.effective_from
           ) THEN
            RAISE EXCEPTION 'initial compute epoch authority is invalid'
                USING ERRCODE = '55000';
        END IF;
    ELSE
        SELECT prior.inventory_scope_epoch_id, prior.authority_sequence,
               epoch.retired_at
        INTO previous_epoch_id, previous_sequence, previous_retired_at
        FROM public.compute_metering_epoch_authorities AS prior
        JOIN public.resource_inventory_scope_epochs AS epoch
          ON epoch.id = prior.inventory_scope_epoch_id
         AND epoch.scope_id = prior.inventory_scope_id
        WHERE prior.id = NEW.previous_authority_id
          AND prior.activation_key = NEW.activation_key
          AND prior.inventory_scope_id = NEW.inventory_scope_id
        FOR SHARE OF epoch;

        WITH RECURSIVE lineage AS (
            SELECT epoch.id, epoch.scope_id, epoch.recovery_from_epoch_id,
                   ARRAY[epoch.id]::UUID[] AS path, 1 AS depth
            FROM public.resource_inventory_scope_epochs AS epoch
            WHERE epoch.id = NEW.inventory_scope_epoch_id
              AND epoch.scope_id = NEW.inventory_scope_id
            UNION ALL
            SELECT predecessor.id, predecessor.scope_id,
                   predecessor.recovery_from_epoch_id,
                   lineage.path || predecessor.id,
                   lineage.depth + 1
            FROM lineage
            JOIN public.resource_inventory_scope_epochs AS predecessor
              ON predecessor.id = lineage.recovery_from_epoch_id
             AND predecessor.scope_id = NEW.inventory_scope_id
            WHERE lineage.depth < 10000
              AND NOT predecessor.id = ANY(lineage.path)
        )
        SELECT EXISTS (
            SELECT 1 FROM lineage WHERE id = previous_epoch_id
        ) INTO lineage_reaches_prior;

        IF activation_state IS DISTINCT FROM 'active'
           OR activation_boundary IS NULL
           OR statement_timestamp() < activation_boundary
           OR request_kind IS DISTINCT FROM 'recovery-rollover'
           OR previous_sequence IS NULL
           OR NEW.authority_sequence <> previous_sequence + 1
           OR epoch_recovery_from IS DISTINCT FROM NEW.predecessor_epoch_id
           OR previous_retired_at IS NULL
           OR previous_retired_at > NEW.effective_from
           OR lineage_reaches_prior IS DISTINCT FROM TRUE
           OR NEW.effective_from IS DISTINCT FROM request_promoted_at THEN
            RAISE EXCEPTION 'compute recovery epoch lineage is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    SELECT snapshot.item_count, snapshot.leader_generation,
           snapshot.complete
           AND snapshot.manifest_state = 'sealed'
           AND epoch.last_complete_snapshot_id = snapshot.id
    INTO snapshot_item_count, snapshot_generation, snapshot_is_proof
    FROM public.resource_inventory_snapshots AS snapshot
    JOIN public.resource_inventory_scope_epochs AS epoch
      ON epoch.id = snapshot.scope_epoch_id
     AND epoch.scope_id = snapshot.inventory_scope_id
    WHERE snapshot.id = NEW.proof_snapshot_id
      AND snapshot.scope_epoch_id = NEW.inventory_scope_epoch_id
      AND snapshot.inventory_scope_id = NEW.inventory_scope_id
    FOR SHARE OF epoch;

    SELECT count(*)
    INTO shadow_count
    FROM public.compute_shadow_observations AS observation
    WHERE observation.snapshot_id = NEW.proof_snapshot_id
      AND observation.inventory_scope_id = NEW.inventory_scope_id
      AND observation.activation_key = NEW.activation_key;

    SELECT count(*)
    INTO missing_shadow_count
    FROM public.resource_inventory_snapshot_items AS item
    WHERE item.snapshot_id = NEW.proof_snapshot_id
      AND NOT EXISTS (
          SELECT 1
          FROM public.compute_shadow_observations AS observation
          WHERE observation.snapshot_id = item.snapshot_id
            AND observation.activation_key = NEW.activation_key
            AND observation.source_kind = item.source_kind
            AND observation.source_uid = item.source_uid
      );

    SELECT count(*)
    INTO orphan_shadow_count
    FROM public.compute_shadow_observations AS observation
    WHERE observation.snapshot_id = NEW.proof_snapshot_id
      AND observation.activation_key = NEW.activation_key
      AND NOT EXISTS (
          SELECT 1
          FROM public.resource_inventory_snapshot_items AS item
          WHERE item.snapshot_id = observation.snapshot_id
            AND item.source_kind = observation.source_kind
            AND item.source_uid = observation.source_uid
      );

    IF snapshot_is_proof IS DISTINCT FROM TRUE
       OR snapshot_generation IS DISTINCT FROM NEW.proof_generation
       OR current_generation IS DISTINCT FROM NEW.proof_generation
       OR shadow_count IS DISTINCT FROM snapshot_item_count
       OR missing_shadow_count <> 0
       OR orphan_shadow_count <> 0 THEN
        RAISE EXCEPTION
            'compute epoch authority requires an exact item-for-item proof'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_compute_metering_scope_requirement(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_compute_metering_scope_requirement() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    activation_state TEXT;
    scope_resource   TEXT;
    scope_namespace  TEXT;
    epoch_is_current BOOLEAN;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'compute scope requirements are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT activation.state, scope.api_resource, scope.namespace,
           epoch.retired_at IS NULL
    INTO activation_state, scope_resource, scope_namespace, epoch_is_current
    FROM public.compute_metering_activation AS activation
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = NEW.inventory_scope_id
     AND scope.collector_id = NEW.collector_id
     AND scope.source_cluster = NEW.source_cluster
    JOIN public.resource_inventory_scope_epochs AS epoch
      ON epoch.id = NEW.inventory_scope_epoch_id
     AND epoch.scope_id = scope.id
    WHERE activation.activation_key = NEW.activation_key
    FOR SHARE OF activation, epoch;

    IF activation_state IS DISTINCT FROM 'shadow' THEN
        RAISE EXCEPTION
            'compute scope requirements can be added only while shadow'
            USING ERRCODE = '55000';
    END IF;
    IF epoch_is_current IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            'compute scope requirement must name the exact current epoch'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.required_from <= statement_timestamp() THEN
        RAISE EXCEPTION 'compute scope requirement boundary must be future'
            USING ERRCODE = '55000';
    END IF;
    IF NOT (
        (NEW.activation_key IN ('agent_pod', 'ide_workspace_pod')
         AND NEW.collector_id = 'kubernetes-pods'
         AND scope_resource = 'core/v1/pods'
         AND scope_namespace IS NOT NULL)
        OR (NEW.activation_key = 'workspace_vm'
            AND NEW.collector_id = 'kubevirt-vmis'
            AND scope_resource =
                'kubevirt.io/v1/virtualmachineinstances'
            AND scope_namespace IS NOT NULL)
    ) THEN
        RAISE EXCEPTION 'compute scope requirement does not match its class'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_compute_shadow_observation_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_compute_shadow_observation_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    snapshot_state   TEXT;
    activation_state TEXT;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'compute shadow observations are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT snapshot.manifest_state
    INTO snapshot_state
    FROM public.resource_inventory_snapshots AS snapshot
    JOIN public.resource_inventory_ingest_tickets AS ticket
      ON ticket.id = snapshot.ingest_ticket_id
    JOIN public.infra_metering_control AS control ON control.singleton = TRUE
    WHERE snapshot.id = NEW.snapshot_id
      AND snapshot.inventory_scope_id = NEW.inventory_scope_id
      AND snapshot.leader_generation = control.leader_generation
      AND ticket.bound_snapshot_id = snapshot.id
      AND ticket.consumed_at IS NULL
      AND ticket.expires_at > statement_timestamp();

    SELECT activation.state
    INTO activation_state
    FROM public.compute_metering_activation AS activation
    WHERE activation.activation_key = NEW.activation_key
    FOR SHARE;

    IF snapshot_state IS DISTINCT FROM 'staging'
       OR activation_state NOT IN ('shadow', 'active') THEN
        RAISE EXCEPTION 'compute shadow observation fence failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_infra_metering_cutover_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_infra_metering_cutover_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'infra metering cutover control cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.cutover_request_id IS NOT NULL AND (
        NEW.cutover_request_id IS DISTINCT FROM OLD.cutover_request_id
        OR NEW.cutover_actor_id IS DISTINCT FROM OLD.cutover_actor_id
        OR NEW.cutover_reason IS DISTINCT FROM OLD.cutover_reason
        OR NEW.cutover_requested_at IS DISTINCT FROM OLD.cutover_requested_at
        OR NEW.barrier_committed_at IS DISTINCT FROM OLD.barrier_committed_at
        OR NEW.cutover_at IS DISTINCT FROM OLD.cutover_at
    ) THEN
        RAISE EXCEPTION 'infrastructure metering cutover identity/barrier is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.cutover_state = 'disabled' THEN
        IF NEW.cutover_state = 'disabled' THEN
            IF NEW.cutover_phase <> 'disabled' THEN
                RAISE EXCEPTION 'disabled metering cutover phase is immutable'
                    USING ERRCODE = '55000';
            END IF;
        ELSIF NEW.cutover_state = 'preparing' THEN
            IF NEW.cutover_phase <> 'legacy-draining'
               OR NEW.cutover_request_id IS NULL
               OR NEW.cutover_at IS NULL THEN
                RAISE EXCEPTION 'cutover must enter preparing at legacy-draining'
                    USING ERRCODE = '55000';
            END IF;
        ELSE
            RAISE EXCEPTION 'cutover advances disabled to preparing only'
                USING ERRCODE = '55000';
        END IF;
    ELSIF OLD.cutover_state = 'preparing' THEN
        IF NEW.cutover_state = 'preparing' THEN
            IF NOT (
                NEW.cutover_phase = OLD.cutover_phase
                OR (OLD.cutover_phase = 'legacy-draining'
                    AND NEW.cutover_phase = 'ready-to-activate')
            ) THEN
                RAISE EXCEPTION 'preparing cutover phase cannot move backwards'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.legacy_drained_at IS NOT NULL
               AND NEW.legacy_drained_at IS DISTINCT FROM OLD.legacy_drained_at THEN
                RAISE EXCEPTION 'legacy drain completion is immutable'
                    USING ERRCODE = '55000';
            END IF;
        ELSIF NEW.cutover_state = 'active' THEN
            IF OLD.cutover_phase <> 'ready-to-activate'
               OR NEW.cutover_phase <> 'active'
               OR NEW.activated_at IS NULL THEN
                RAISE EXCEPTION 'cutover activates only after durable legacy drain'
                    USING ERRCODE = '55000';
            END IF;
        ELSE
            RAISE EXCEPTION 'preparing cutover cannot be disabled or replaced'
                USING ERRCODE = '55000';
        END IF;
    ELSE
        IF NEW.cutover_state IS DISTINCT FROM OLD.cutover_state
           OR NEW.cutover_phase IS DISTINCT FROM OLD.cutover_phase
           OR NEW.legacy_drained_at IS DISTINCT FROM OLD.legacy_drained_at
           OR NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
            RAISE EXCEPTION 'active infrastructure metering cutover is irreversible'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;


--
-- Name: protect_infra_metering_generation_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_infra_metering_generation_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'infra metering control cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.singleton IS DISTINCT FROM OLD.singleton
       OR NEW.leader_generation < OLD.leader_generation
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'infra metering generation is monotonic'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_infra_metering_legacy_drain_completion(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_infra_metering_legacy_drain_completion() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF OLD.legacy_drained_at IS NOT NULL
       AND NEW.legacy_drained_at IS DISTINCT FROM OLD.legacy_drained_at THEN
        RAISE EXCEPTION 'legacy drain completion is immutable'
            USING ERRCODE = '55000';
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
        IF NEW.state <> 'open' OR NEW.coverage_sequence <> 0 THEN
            RAISE EXCEPTION
                'infrastructure usage day state must begin open at sequence zero'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'infrastructure usage day state cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.day IS DISTINCT FROM OLD.day
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'infrastructure usage day identity/time is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'sealed' THEN
        IF NEW.state <> 'sealed'
           OR NEW.sealed_at IS DISTINCT FROM OLD.sealed_at
           OR NEW.coverage_sequence <> OLD.coverage_sequence + 1
           OR NEW.coverage_revision IS NULL
           OR NEW.coverage_revision = ''
           OR NEW.coverage_revision IS NOT DISTINCT FROM OLD.coverage_revision
           OR NOT (OLD.unknown_ranges <@ NEW.unknown_ranges)
           OR jsonb_array_length(NEW.unknown_ranges)
                < jsonb_array_length(OLD.unknown_ranges)
           OR EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.unknown_ranges) AS item(value)
                GROUP BY item.value
                HAVING count(*) > 1
           )
           OR NOT (
                (OLD.coverage_status = 'complete'
                    AND NEW.coverage_status = 'partial'
                    AND jsonb_array_length(NEW.unknown_ranges) > 0)
                OR
                (OLD.coverage_status = 'partial'
                    AND NEW.coverage_status = 'partial'
                    AND jsonb_array_length(NEW.unknown_ranges)
                        > jsonb_array_length(OLD.unknown_ranges))
           ) THEN
            RAISE EXCEPTION
                'sealed infrastructure day may only gain fail-closed unknown ranges'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF (OLD.state = 'open' AND NEW.state NOT IN ('open', 'sealing'))
       OR (OLD.state = 'sealing' AND NEW.state NOT IN ('sealing', 'sealed')) THEN
        RAISE EXCEPTION
            'infrastructure usage day state advances open to sealing to sealed'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.state = 'sealed' THEN
        IF OLD.state <> 'sealing' OR NEW.coverage_sequence NOT IN (0, 1) THEN
            RAISE EXCEPTION 'initial infrastructure day seal is invalid'
                USING ERRCODE = '55000';
        END IF;
        NEW.coverage_sequence := 1;
    ELSIF NEW.coverage_sequence <> 0 THEN
        RAISE EXCEPTION 'unsealed infrastructure day has a coverage revision'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_infrastructure_storage_resource_mapping(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_infrastructure_storage_resource_mapping() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    RAISE EXCEPTION 'storage resource mappings are append-only'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_inventory_epoch_recovery_identity(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_inventory_epoch_recovery_identity() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'inventory epochs cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.scope_id IS DISTINCT FROM OLD.scope_id
       OR NEW.epoch_number IS DISTINCT FROM OLD.epoch_number
       OR NEW.coverage_mode IS DISTINCT FROM OLD.coverage_mode
       OR NEW.capture_epoch IS DISTINCT FROM OLD.capture_epoch
       OR NEW.recovery_from_epoch_id IS DISTINCT FROM OLD.recovery_from_epoch_id
       OR NEW.require_after_recovery IS DISTINCT FROM OLD.require_after_recovery
    THEN
        RAISE EXCEPTION 'inventory epoch recovery identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_legacy_workspace_cutover_plan_event_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_legacy_workspace_cutover_plan_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    parent_state TEXT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'legacy workspace cutover plan events are immutable'
            USING ERRCODE = '55000';
    END IF;
    SELECT state INTO parent_state
    FROM public.legacy_workspace_cutover_plans
    WHERE id = NEW.plan_id
    FOR SHARE;
    IF parent_state IS DISTINCT FROM 'planned' THEN
        RAISE EXCEPTION 'legacy workspace cutover plan no longer accepts events'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_legacy_workspace_cutover_plan_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_legacy_workspace_cutover_plan_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'legacy workspace cutover plans cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF (to_jsonb(NEW)
            - 'state' - 'attempt_count' - 'last_attempt_generation'
            - 'last_attempt_at' - 'sanitized_error' - 'published_at')
       <> (to_jsonb(OLD)
            - 'state' - 'attempt_count' - 'last_attempt_generation'
            - 'last_attempt_at' - 'sanitized_error' - 'published_at') THEN
        RAISE EXCEPTION 'legacy workspace cutover plan intent is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state <> 'planned'
       OR NEW.state NOT IN ('planned', 'published', 'conflict')
       OR NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'legacy workspace cutover plan terminal/retry state is invalid'
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
DECLARE
    snapshot_end_link BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'resource interval revisions are retained and cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    snapshot_end_link := OLD.ended_at IS NOT NULL
        AND OLD.end_time_source = 'app-db-received'
        AND OLD.end_reason IN ('not-applicable', 'terminal-or-unscheduled')
        AND OLD.last_seen_at <= OLD.ended_at
        AND NEW.last_seen_at = GREATEST(OLD.last_seen_at, OLD.ended_at)
        AND NEW.last_seen_snapshot_id IS NOT NULL
        AND NEW.last_seen_snapshot_id
            IS DISTINCT FROM OLD.last_seen_snapshot_id;

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
           OR NEW.last_confirmed_at IS DISTINCT FROM OLD.last_confirmed_at
           OR (NOT snapshot_end_link AND (
                NEW.last_seen_at IS DISTINCT FROM OLD.last_seen_at
                OR NEW.last_seen_snapshot_id
                    IS DISTINCT FROM OLD.last_seen_snapshot_id))
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
-- Name: protect_resource_inventory_ingest_ticket_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_resource_inventory_ingest_ticket_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    current_generation BIGINT;
    epoch_retired_at TIMESTAMPTZ;
    snapshot_ticket_id UUID;
    actual_staged_bytes BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.bound_snapshot_id IS NULL
           AND OLD.bound_at IS NULL
           AND OLD.consumed_at IS NULL
           AND OLD.expires_at <= statement_timestamp() THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'only expired unbound inventory ingest tickets may be deleted'
            USING ERRCODE = '55000';
    END IF;

    SELECT leader_generation INTO current_generation
    FROM public.infra_metering_control WHERE singleton = TRUE;
    SELECT retired_at INTO epoch_retired_at
    FROM public.resource_inventory_scope_epochs WHERE id = NEW.scope_epoch_id;

    IF current_generation IS NULL
       OR epoch_retired_at IS NOT NULL
       OR NEW.leader_generation <> current_generation THEN
        RAISE EXCEPTION 'inventory ingest ticket generation/scope fence failed'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.bound_snapshot_id IS NOT NULL
           OR NEW.bound_at IS NOT NULL
           OR NEW.consumed_at IS NOT NULL
           OR NEW.staged_bytes <> 0
           OR NEW.expires_at <= statement_timestamp() THEN
            RAISE EXCEPTION 'new inventory ingest ticket must be live and unbound'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF (to_jsonb(NEW)
            - 'bound_snapshot_id' - 'bound_at' - 'consumed_at' - 'staged_bytes')
       <> (to_jsonb(OLD)
            - 'bound_snapshot_id' - 'bound_at' - 'consumed_at' - 'staged_bytes') THEN
        RAISE EXCEPTION 'inventory ingest ticket request identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.bound_snapshot_id IS NULL
       AND OLD.bound_at IS NULL
       AND OLD.consumed_at IS NULL
       AND NEW.bound_snapshot_id IS NOT NULL
       AND NEW.bound_at IS NOT NULL
       AND NEW.consumed_at IS NULL
       AND NEW.staged_bytes = OLD.staged_bytes
       AND NEW.expires_at > statement_timestamp() THEN
        SELECT ingest_ticket_id INTO snapshot_ticket_id
        FROM public.resource_inventory_snapshots
        WHERE id = NEW.bound_snapshot_id
          AND scope_epoch_id = NEW.scope_epoch_id;
        IF snapshot_ticket_id = NEW.id THEN
            RETURN NEW;
        END IF;
    END IF;

    IF OLD.bound_snapshot_id IS NOT NULL
       AND NEW.bound_snapshot_id = OLD.bound_snapshot_id
       AND NEW.bound_at = OLD.bound_at
       AND OLD.consumed_at IS NULL
       AND NEW.consumed_at IS NULL
       AND NEW.staged_bytes >= OLD.staged_bytes
       AND NEW.staged_bytes <= NEW.max_snapshot_bytes
       AND NEW.expires_at > statement_timestamp() THEN
        SELECT COALESCE(sum(
            public.resource_inventory_snapshot_item_size_bytes(
                item.source_kind, item.source_uid, item.revision_hash,
                item.normalized_item, item.item_error
            )
        ), 0)
        INTO actual_staged_bytes
        FROM public.resource_inventory_snapshot_items item
        WHERE item.snapshot_id = NEW.bound_snapshot_id;
        IF actual_staged_bytes = NEW.staged_bytes THEN
            RETURN NEW;
        END IF;
    END IF;

    IF OLD.bound_snapshot_id IS NOT NULL
       AND NEW.bound_snapshot_id = OLD.bound_snapshot_id
       AND NEW.bound_at = OLD.bound_at
       AND NEW.staged_bytes = OLD.staged_bytes
       AND OLD.consumed_at IS NULL
       AND NEW.consumed_at IS NOT NULL
       AND NEW.consumed_at <= NEW.expires_at
       AND NEW.expires_at > statement_timestamp() THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'inventory ingest ticket transition is invalid'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_resource_inventory_shadow_comparison_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_resource_inventory_shadow_comparison_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    snapshot_is_staging BOOLEAN;
    snapshot_state TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT snapshot.manifest_state
        INTO snapshot_state
        FROM public.resource_inventory_snapshots snapshot
        WHERE snapshot.id = OLD.snapshot_id
          AND snapshot.inventory_scope_id = OLD.inventory_scope_id
        FOR SHARE;
        IF snapshot_state IN ('items-expired', 'staging-expired')
           AND OLD.comparison_at
                <= statement_timestamp() - INTERVAL '7 days'
           AND OLD.created_at
                <= statement_timestamp() - INTERVAL '7 days' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'shadow comparisons require an expired manifest and seven-day floor'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'inventory shadow comparison rows are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT snapshot.manifest_state = 'staging'
    INTO snapshot_is_staging
    FROM public.resource_inventory_snapshots snapshot
    JOIN public.resource_inventory_ingest_tickets ticket
      ON ticket.id = snapshot.ingest_ticket_id
    JOIN public.infra_metering_control control ON control.singleton = TRUE
    WHERE snapshot.id = NEW.snapshot_id
      AND snapshot.inventory_scope_id = NEW.inventory_scope_id
      AND snapshot.leader_generation = control.leader_generation
      AND ticket.bound_snapshot_id = snapshot.id
      AND ticket.consumed_at IS NULL
      AND ticket.expires_at > statement_timestamp();

    IF snapshot_is_staging IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'shadow comparison snapshot fence failed'
            USING ERRCODE = '55000';
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
        IF old_state IN ('items-expired', 'staging-expired') THEN
            RETURN OLD;
        END IF;
    END IF;

    RAISE EXCEPTION
        'inventory snapshot items are immutable outside an expiry terminal'
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
       AND OLD.sealed_at <= statement_timestamp() - INTERVAL '7 days'
       AND (to_jsonb(NEW) - 'manifest_state' - 'items_expired_at')
           = (to_jsonb(OLD) - 'manifest_state' - 'items_expired_at') THEN
        RETURN NEW;
    END IF;

    IF OLD.manifest_state = 'staging'
       AND NEW.manifest_state = 'staging-expired'
       AND NOT OLD.complete
       AND NOT NEW.complete
       AND NEW.sealed_at IS NULL
       AND NEW.items_expired_at IS NOT NULL
       AND NEW.items_expired_at <= statement_timestamp()
       AND OLD.created_at <= statement_timestamp() - INTERVAL '24 hours'
       AND (
            OLD.ingest_ticket_id IS NULL
            OR EXISTS (
                SELECT 1
                FROM public.resource_inventory_ingest_tickets ticket
                WHERE ticket.id = OLD.ingest_ticket_id
                  AND ticket.scope_epoch_id = OLD.scope_epoch_id
                  AND ticket.bound_snapshot_id = OLD.id
                  AND ticket.expires_at <= statement_timestamp()
            )
       )
       AND (to_jsonb(NEW) - 'manifest_state' - 'items_expired_at')
           = (to_jsonb(OLD) - 'manifest_state' - 'items_expired_at') THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'snapshot metadata may only be finalized once or enter an expiry terminal'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_resource_inventory_transport_nonce_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_resource_inventory_transport_nonce_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    current_generation BIGINT;
    epoch_retired_at TIMESTAMPTZ;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'inventory transport nonce claims are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.expires_at > statement_timestamp() THEN
            RAISE EXCEPTION 'live inventory transport nonce cannot be deleted'
                USING ERRCODE = '55000';
        END IF;
        RETURN OLD;
    END IF;

    SELECT leader_generation INTO current_generation
    FROM public.infra_metering_control WHERE singleton = TRUE;
    SELECT retired_at INTO epoch_retired_at
    FROM public.resource_inventory_scope_epochs WHERE id = NEW.scope_epoch_id;
    IF current_generation IS NULL
       OR epoch_retired_at IS NOT NULL
       OR NEW.leader_generation <> current_generation
       OR NEW.received_at > statement_timestamp()
       OR NEW.expires_at <= statement_timestamp() THEN
        RAISE EXCEPTION 'inventory transport nonce generation/scope fence failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_resource_inventory_watch_event_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_resource_inventory_watch_event_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    session_row public.resource_inventory_watch_sessions%ROWTYPE;
    current_generation BIGINT;
    epoch_scope_id UUID;
    epoch_retired_at TIMESTAMPTZ;
    epoch_resource_version TEXT;
    postcondition_ok BOOLEAN;
    deletion_allowed BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT (session.consumed_at IS NOT NULL
                    OR session.expires_at <= statement_timestamp())
               AND COALESCE(session.consumed_at, session.expires_at)
                    <= statement_timestamp() - INTERVAL '7 days'
        INTO deletion_allowed
        FROM public.resource_inventory_watch_sessions session
        WHERE session.id = OLD.watch_session_id
          AND session.scope_epoch_id = OLD.scope_epoch_id
        FOR SHARE;
        IF deletion_allowed IS TRUE THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'watch events require a seven-day terminal session floor'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'inventory watch event rows are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT * INTO session_row
    FROM public.resource_inventory_watch_sessions
    WHERE id = NEW.watch_session_id
      AND scope_epoch_id = NEW.scope_epoch_id
    FOR UPDATE;
    SELECT leader_generation INTO current_generation
    FROM public.infra_metering_control WHERE singleton = TRUE;
    SELECT scope_id, retired_at, last_resource_version
    INTO epoch_scope_id, epoch_retired_at, epoch_resource_version
    FROM public.resource_inventory_scope_epochs
    WHERE id = NEW.scope_epoch_id;

    IF session_row.id IS NULL
       OR session_row.consumed_at IS NOT NULL
       OR session_row.expires_at <= statement_timestamp()
       OR epoch_retired_at IS NOT NULL
       OR current_generation IS NULL
       OR session_row.leader_generation <> current_generation
       OR NEW.ordinal <> session_row.committed_events + 1
       OR NEW.expected_resource_version
            IS DISTINCT FROM session_row.last_resource_version
       OR NEW.expected_resource_version
            IS DISTINCT FROM epoch_resource_version
       OR NEW.event_bytes > session_row.max_bytes
                              - session_row.committed_bytes
       OR NEW.received_at > statement_timestamp() THEN
        RAISE EXCEPTION 'inventory watch event cursor/session fence failed'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.mutation_action IN ('confirm', 'open', 'revise') THEN
        SELECT TRUE INTO postcondition_ok
        FROM public.resource_intervals interval
        WHERE interval.id = NEW.affected_interval_id
          AND interval.inventory_scope_id = epoch_scope_id
          AND interval.source_kind = NEW.source_kind
          AND interval.source_uid = NEW.source_uid
          AND interval.source_revision = NEW.revision_hash
          AND interval.ended_at IS NULL
          AND interval.last_seen_at >= NEW.received_at
          AND interval.last_confirmed_at >= NEW.received_at;
    ELSIF NEW.mutation_action = 'presence-invalid'
          AND NEW.affected_interval_id IS NOT NULL THEN
        SELECT TRUE INTO postcondition_ok
        FROM public.resource_intervals interval
        WHERE interval.id = NEW.affected_interval_id
          AND interval.inventory_scope_id = epoch_scope_id
          AND interval.source_kind = NEW.source_kind
          AND interval.source_uid = NEW.source_uid
          AND interval.ended_at IS NULL
          AND interval.last_seen_at >= NEW.received_at;
    ELSIF NEW.mutation_action = 'close' THEN
        SELECT TRUE INTO postcondition_ok
        FROM public.resource_intervals interval
        WHERE interval.id = NEW.affected_interval_id
          AND interval.inventory_scope_id = epoch_scope_id
          AND interval.source_kind = NEW.source_kind
          AND interval.source_uid = NEW.source_uid
          AND interval.ended_at = NEW.received_at;
    ELSIF NEW.mutation_action IN ('already-absent', 'not-applicable') THEN
        SELECT NOT EXISTS (
            SELECT 1 FROM public.resource_intervals interval
            WHERE interval.inventory_scope_id = epoch_scope_id
              AND interval.source_kind = NEW.source_kind
              AND interval.source_uid = NEW.source_uid
              AND interval.ended_at IS NULL
        ) INTO postcondition_ok;
    ELSIF NEW.mutation_action = 'history-gap' THEN
        SELECT TRUE INTO postcondition_ok
        FROM public.resource_inventory_coverage_gaps gap
        WHERE gap.id = NEW.coverage_gap_id
          AND gap.scope_epoch_id = NEW.scope_epoch_id
          AND gap.resolution = 'unresolved'
          AND gap.gap_start <= NEW.received_at;
    ELSE
        -- BOOKMARK and an invalid item without an existing interval have no
        -- object mutation, by design.
        postcondition_ok := TRUE;
    END IF;

    IF postcondition_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'inventory watch event interval/gap postcondition failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_resource_inventory_watch_session_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_resource_inventory_watch_session_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    current_generation BIGINT;
    epoch_retired_at TIMESTAMPTZ;
    epoch_resource_version TEXT;
    committed_event public.resource_inventory_watch_events%ROWTYPE;
    hit_limit BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF (OLD.consumed_at IS NOT NULL
                OR OLD.expires_at <= statement_timestamp())
           AND COALESCE(OLD.consumed_at, OLD.expires_at)
                <= statement_timestamp() - INTERVAL '7 days'
           AND NOT EXISTS (
                SELECT 1
                FROM public.resource_inventory_watch_events event
                WHERE event.watch_session_id = OLD.id
           ) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'watch sessions require a seven-day terminal floor and no events'
            USING ERRCODE = '55000';
    END IF;

    SELECT leader_generation INTO current_generation
    FROM public.infra_metering_control WHERE singleton = TRUE;
    SELECT retired_at, last_resource_version
    INTO epoch_retired_at, epoch_resource_version
    FROM public.resource_inventory_scope_epochs
    WHERE id = NEW.scope_epoch_id;

    IF current_generation IS NULL
       OR epoch_retired_at IS NOT NULL
       OR NEW.leader_generation <> current_generation THEN
        RAISE EXCEPTION 'inventory watch session generation/scope fence failed'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.starting_resource_version IS DISTINCT FROM epoch_resource_version
           OR NEW.last_resource_version IS DISTINCT FROM epoch_resource_version
           OR NEW.committed_events <> 0
           OR NEW.committed_bytes <> 0
           OR NEW.consumed_at IS NOT NULL
           OR NEW.termination_reason IS NOT NULL
           OR NEW.expires_at <= statement_timestamp() THEN
            RAISE EXCEPTION 'new watch session must bind the committed cursor'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.consumed_at IS NOT NULL
       OR (to_jsonb(NEW)
            - 'last_resource_version' - 'committed_events'
            - 'committed_bytes' - 'termination_reason'
            - 'consumed_at' - 'updated_at')
          <> (to_jsonb(OLD)
            - 'last_resource_version' - 'committed_events'
            - 'committed_bytes' - 'termination_reason'
            - 'consumed_at' - 'updated_at')
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'inventory watch session identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    -- A clean shutdown consumes the grant without inventing an event/cursor.
    IF NEW.committed_events = OLD.committed_events
       AND NEW.committed_bytes = OLD.committed_bytes
       AND NEW.last_resource_version = OLD.last_resource_version
       AND NEW.termination_reason = 'completed'
       AND NEW.consumed_at IS NOT NULL
       AND NEW.consumed_at <= NEW.expires_at
       AND NEW.expires_at > statement_timestamp() THEN
        RETURN NEW;
    END IF;

    SELECT event.* INTO committed_event
    FROM public.resource_inventory_watch_events event
    WHERE event.watch_session_id = NEW.id
      AND event.ordinal = NEW.committed_events;

    hit_limit := NEW.committed_events = NEW.max_events
                 OR NEW.committed_bytes = NEW.max_bytes;
    IF committed_event.id IS NOT NULL
       AND NEW.expires_at > statement_timestamp()
       AND NEW.committed_events = OLD.committed_events + 1
       AND NEW.committed_bytes = OLD.committed_bytes
                                     + committed_event.event_bytes
       AND committed_event.expected_resource_version
             = OLD.last_resource_version
       AND NEW.last_resource_version = COALESCE(
            committed_event.resource_version, OLD.last_resource_version
       )
       AND (
            (committed_event.event_type = 'history-lost'
                AND NEW.last_resource_version = OLD.last_resource_version
                AND NEW.termination_reason = 'history-lost'
                AND NEW.consumed_at IS NOT NULL)
            OR (committed_event.event_type <> 'history-lost'
                AND hit_limit
                AND NEW.termination_reason = 'limit-reached'
                AND NEW.consumed_at IS NOT NULL)
            OR (committed_event.event_type <> 'history-lost'
                AND NOT hit_limit
                AND NEW.termination_reason IS NULL
                AND NEW.consumed_at IS NULL)
       )
       AND (NEW.consumed_at IS NULL OR NEW.consumed_at <= NEW.expires_at) THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'inventory watch session transition is invalid'
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
-- Name: protect_storage_asset_coverage_gap_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_storage_asset_coverage_gap_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    asset_state TEXT;
    lifecycle_id UUID;
    first_seen  TIMESTAMPTZ;
    last_seen   TIMESTAMPTZ;
    evidence_ok BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'storage asset coverage gaps cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        SELECT asset.lifecycle_state, asset.first_observed_at,
               asset.last_observed_at
        INTO asset_state, first_seen, last_seen
        FROM public.storage_volume_assets AS asset
        WHERE asset.id = NEW.asset_id
        FOR UPDATE;
        IF asset_state IS NULL OR asset_state = 'destroyed'
           OR NEW.gap_start < first_seen
           OR NEW.gap_start < last_seen
           OR NEW.resolution <> 'unresolved' THEN
            RAISE EXCEPTION 'storage asset gap cannot open for this lifecycle'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.resolution <> 'unresolved'
       OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.asset_id IS DISTINCT FROM OLD.asset_id
       OR NEW.scope_epoch_id IS DISTINCT FROM OLD.scope_epoch_id
       OR NEW.gap_start IS DISTINCT FROM OLD.gap_start
       OR NEW.reason_code IS DISTINCT FROM OLD.reason_code
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.resolution NOT IN ('reobserved', 'destroyed-confirmed')
       OR NEW.gap_end IS NULL
       OR NEW.resolved_at IS NULL
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'storage asset gap resolution is immutable or invalid'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.resolution = 'destroyed-confirmed' THEN
        SELECT TRUE INTO evidence_ok
        FROM public.storage_backend_assertions AS assertion
        WHERE assertion.id = NEW.resolution_assertion_id
          AND assertion.asset_id = OLD.asset_id
          AND assertion.effective_at = NEW.gap_end;
        IF evidence_ok IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'storage asset gap destruction evidence is invalid'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_storage_backend_assertion_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_storage_backend_assertion_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    prior       public.storage_backend_assertions%ROWTYPE;
    asset_state TEXT;
    lifecycle_id UUID;
    first_seen  TIMESTAMPTZ;
    last_seen   TIMESTAMPTZ;
    open_start  TIMESTAMPTZ;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'storage backend assertions are append-only'
            USING ERRCODE = '55000';
    END IF;

    SELECT * INTO prior
    FROM public.storage_backend_assertions AS assertion
    WHERE assertion.idempotency_key = NEW.idempotency_key;
    IF FOUND THEN
        IF prior.asset_id IS DISTINCT FROM NEW.asset_id
           OR prior.assertion_kind IS DISTINCT FROM NEW.assertion_kind
           OR prior.request_hash IS DISTINCT FROM NEW.request_hash
           OR prior.effective_at IS DISTINCT FROM NEW.effective_at
           OR prior.evidence_kind IS DISTINCT FROM NEW.evidence_kind
           OR prior.evidence_digest IS DISTINCT FROM NEW.evidence_digest
           OR prior.actor_kind IS DISTINCT FROM NEW.actor_kind
           OR prior.actor_id IS DISTINCT FROM NEW.actor_id
           OR prior.reason_code IS DISTINCT FROM NEW.reason_code THEN
            RAISE EXCEPTION
                'storage assertion idempotency replay changed immutable intent'
                USING ERRCODE = '23505';
        END IF;
        RETURN NEW;
    END IF;

    SELECT asset.lifecycle_state, asset.source_lifecycle_id,
           asset.first_observed_at, asset.last_observed_at
    INTO asset_state, lifecycle_id, first_seen, last_seen
    FROM public.storage_volume_assets AS asset
    WHERE asset.id = NEW.asset_id
    FOR UPDATE;
    IF asset_state IS NULL OR asset_state <> 'backend-unverified'
       OR NEW.effective_at < first_seen
       OR NEW.effective_at < last_seen
       OR NEW.effective_at > statement_timestamp() THEN
        RAISE EXCEPTION 'storage destruction assertion is outside the lifecycle'
            USING ERRCODE = '55000';
    END IF;

    SELECT gap.gap_start INTO open_start
    FROM public.storage_asset_coverage_gaps AS gap
    WHERE gap.asset_id = NEW.asset_id AND gap.resolution = 'unresolved'
    FOR UPDATE;
    IF open_start IS NULL THEN
        RAISE EXCEPTION 'storage destruction requires an unresolved backend gap'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.effective_at < open_start THEN
        RAISE EXCEPTION 'storage destruction predates the backend-unknown gap'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.storage_volume_incarnations AS incarnation
        WHERE incarnation.asset_id = NEW.asset_id
          AND incarnation.detached_at IS NULL
    ) OR EXISTS (
        SELECT 1 FROM public.resource_intervals AS interval
        WHERE interval.source_lifecycle_id = lifecycle_id
          AND interval.ended_at IS NULL
    ) THEN
        RAISE EXCEPTION 'storage destruction requires a detached closed lifecycle'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_storage_identity_key_state(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_storage_identity_key_state() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'storage identity key state is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_storage_metering_activation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_storage_metering_activation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'storage activation rows cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'disabled' OR NEW.activated_at IS NOT NULL THEN
            RAISE EXCEPTION 'storage activation rows must begin disabled'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.measurement_basis IS DISTINCT FROM OLD.measurement_basis
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'storage activation identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'disabled' AND NEW.state = 'shadow'
       AND NEW.activated_at IS NULL THEN
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF OLD.state = 'shadow' AND NEW.state = 'active'
       AND NEW.activated_at IS NOT NULL
       AND NEW.activated_at = date_trunc('day', NEW.activated_at, 'UTC')
       AND NEW.activated_at > statement_timestamp() THEN
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF NEW.state IS NOT DISTINCT FROM OLD.state
       AND NEW.activated_at IS NOT DISTINCT FROM OLD.activated_at
       AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'storage activation permits only disabled -> shadow -> future active'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_storage_metering_source_activation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_storage_metering_source_activation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    quantity_count     BIGINT;
    attribution_count  BIGINT;
    global_state       TEXT;
    global_boundary    TIMESTAMPTZ;
    claim_state        TEXT;
    claim_boundary     TIMESTAMPTZ;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'storage source activation rows cannot be deleted'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'disabled' OR NEW.activated_at IS NOT NULL THEN
            RAISE EXCEPTION 'storage source activation rows must begin disabled'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.measurement_basis IS DISTINCT FROM OLD.measurement_basis
       OR NEW.collector_id IS DISTINCT FROM OLD.collector_id
       OR NEW.source_cluster IS DISTINCT FROM OLD.source_cluster
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'storage source activation identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'disabled' AND NEW.state = 'shadow'
       AND NEW.activated_at IS NULL THEN
        SELECT
            count(*) FILTER (WHERE requirement_role = 'quantity'),
            count(*) FILTER (WHERE requirement_role = 'attribution')
        INTO quantity_count, attribution_count
        FROM public.storage_metering_source_requirements AS requirement
        WHERE requirement.measurement_basis = OLD.measurement_basis
          AND requirement.collector_id = OLD.collector_id
          AND requirement.source_cluster = OLD.source_cluster;

        IF quantity_count = 0
           OR (OLD.measurement_basis = 'claim-requested'
               AND attribution_count <> 0)
           OR (OLD.measurement_basis = 'volume-provisioned'
               AND attribution_count = 0) THEN
            RAISE EXCEPTION
                'storage source activation requires an exact scope set'
                USING ERRCODE = '55000';
        END IF;
        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF OLD.state = 'shadow' AND NEW.state = 'active'
       AND NEW.activated_at IS NOT NULL
       AND NEW.activated_at = date_trunc('day', NEW.activated_at, 'UTC')
       AND NEW.activated_at > statement_timestamp() THEN
        SELECT activation.state, activation.activated_at
        INTO global_state, global_boundary
        FROM public.storage_metering_activation AS activation
        WHERE activation.measurement_basis = OLD.measurement_basis
        FOR SHARE;
        IF global_state IS DISTINCT FROM 'active'
           OR global_boundary IS NULL
           OR global_boundary > NEW.activated_at THEN
            RAISE EXCEPTION
                'global storage basis must have an equal or earlier activation boundary'
                USING ERRCODE = '55000';
        END IF;

        IF OLD.measurement_basis = 'volume-provisioned' THEN
            SELECT activation.state, activation.activated_at
            INTO claim_state, claim_boundary
            FROM public.storage_metering_source_activations AS activation
            WHERE activation.measurement_basis = 'claim-requested'
              AND activation.collector_id = OLD.collector_id
              AND activation.source_cluster = OLD.source_cluster
            FOR SHARE;
            IF claim_state IS DISTINCT FROM 'active'
               OR claim_boundary IS NULL
               OR claim_boundary > NEW.activated_at THEN
                RAISE EXCEPTION
                    'matching claim source must activate before volume source'
                    USING ERRCODE = '55000';
            END IF;

            IF EXISTS (
                (SELECT requirement.inventory_scope_id
                 FROM public.storage_metering_source_requirements AS requirement
                 WHERE requirement.measurement_basis = 'volume-provisioned'
                   AND requirement.collector_id = OLD.collector_id
                   AND requirement.source_cluster = OLD.source_cluster
                   AND requirement.requirement_role = 'attribution')
                EXCEPT
                (SELECT requirement.inventory_scope_id
                 FROM public.storage_metering_source_requirements AS requirement
                 WHERE requirement.measurement_basis = 'claim-requested'
                   AND requirement.collector_id = OLD.collector_id
                   AND requirement.source_cluster = OLD.source_cluster
                   AND requirement.requirement_role = 'quantity')
            ) OR EXISTS (
                (SELECT requirement.inventory_scope_id
                 FROM public.storage_metering_source_requirements AS requirement
                 WHERE requirement.measurement_basis = 'claim-requested'
                   AND requirement.collector_id = OLD.collector_id
                   AND requirement.source_cluster = OLD.source_cluster
                   AND requirement.requirement_role = 'quantity')
                EXCEPT
                (SELECT requirement.inventory_scope_id
                 FROM public.storage_metering_source_requirements AS requirement
                 WHERE requirement.measurement_basis = 'volume-provisioned'
                   AND requirement.collector_id = OLD.collector_id
                   AND requirement.source_cluster = OLD.source_cluster
                   AND requirement.requirement_role = 'attribution')
            ) THEN
                RAISE EXCEPTION
                    'volume attribution requirements must exactly match claim quantity requirements'
                    USING ERRCODE = '55000';
            END IF;
        END IF;

        -- The runtime proves freshness and item identity before promotion.
        -- This trigger independently requires that every frozen input was
        -- promoted in the same transaction (or at an earlier boundary).
        PERFORM epoch.id
        FROM public.storage_metering_source_requirements AS requirement
        JOIN public.resource_inventory_scope_epochs AS epoch
          ON epoch.scope_id = requirement.inventory_scope_id
         AND epoch.retired_at IS NULL
        WHERE requirement.measurement_basis = OLD.measurement_basis
          AND requirement.collector_id = OLD.collector_id
          AND requirement.source_cluster = OLD.source_cluster
        ORDER BY requirement.requirement_role,
                 requirement.inventory_scope_id
        FOR SHARE OF epoch;

        IF EXISTS (
            SELECT 1
            FROM public.storage_metering_source_requirements AS requirement
            LEFT JOIN public.resource_inventory_scope_epochs AS epoch
              ON epoch.scope_id = requirement.inventory_scope_id
             AND epoch.retired_at IS NULL
            WHERE requirement.measurement_basis = OLD.measurement_basis
              AND requirement.collector_id = OLD.collector_id
              AND requirement.source_cluster = OLD.source_cluster
              AND (epoch.id IS NULL
                   OR NOT epoch.required_for_rollup
                   OR epoch.required_from IS NULL
                   OR epoch.required_from > NEW.activated_at)
        ) THEN
            RAISE EXCEPTION
                'storage source activation requires every exact scope to be promoted'
                USING ERRCODE = '55000';
        END IF;

        NEW.updated_at := statement_timestamp();
        RETURN NEW;
    END IF;

    IF NEW.state IS NOT DISTINCT FROM OLD.state
       AND NEW.activated_at IS NOT DISTINCT FROM OLD.activated_at
       AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'storage source activation permits only disabled -> shadow -> future active'
        USING ERRCODE = '55000';
END;
$$;


--
-- Name: protect_storage_metering_source_requirement(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_storage_metering_source_requirement() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    activation_state TEXT;
    api_resource     TEXT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'storage source requirements are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT activation.state, scope.api_resource
    INTO activation_state, api_resource
    FROM public.storage_metering_source_activations AS activation
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = NEW.inventory_scope_id
     AND scope.collector_id = activation.collector_id
     AND scope.source_cluster = activation.source_cluster
    WHERE activation.measurement_basis = NEW.measurement_basis
      AND activation.collector_id = NEW.collector_id
      AND activation.source_cluster = NEW.source_cluster
    FOR SHARE OF activation;

    IF activation_state IS DISTINCT FROM 'disabled' THEN
        RAISE EXCEPTION
            'storage source requirements can be added only while disabled'
            USING ERRCODE = '55000';
    END IF;

    IF NOT (
        (NEW.measurement_basis = 'claim-requested'
            AND NEW.requirement_role = 'quantity'
            AND api_resource = 'core/v1/persistentvolumeclaims')
        OR (NEW.measurement_basis = 'volume-provisioned'
            AND NEW.requirement_role = 'quantity'
            AND api_resource = 'core/v1/persistentvolumes')
        OR (NEW.measurement_basis = 'volume-provisioned'
            AND NEW.requirement_role = 'attribution'
            AND api_resource = 'core/v1/persistentvolumeclaims')
    ) THEN
        RAISE EXCEPTION 'storage source requirement role/resource mismatch'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_storage_shadow_observation_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_storage_shadow_observation_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    snapshot_state TEXT;
    global_state   TEXT;
    source_state   TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT snapshot.manifest_state INTO snapshot_state
        FROM public.resource_inventory_snapshots AS snapshot
        WHERE snapshot.id = OLD.snapshot_id
          AND snapshot.inventory_scope_id = OLD.inventory_scope_id
        FOR SHARE;
        IF snapshot_state IN ('items-expired', 'staging-expired')
           AND OLD.observed_at <= statement_timestamp() - INTERVAL '7 days'
           AND OLD.created_at <= statement_timestamp() - INTERVAL '7 days' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'storage shadow deletion requires an expired manifest and seven-day floor'
            USING ERRCODE = '55000';
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'storage shadow observations are immutable'
            USING ERRCODE = '55000';
    END IF;

    SELECT snapshot.manifest_state,
           global_activation.state, source_activation.state
    INTO snapshot_state, global_state, source_state
    FROM public.resource_inventory_snapshots AS snapshot
    JOIN public.resource_inventory_ingest_tickets AS ticket
      ON ticket.id = snapshot.ingest_ticket_id
    JOIN public.infra_metering_control AS control ON control.singleton = TRUE
    JOIN public.resource_inventory_scopes AS scope
      ON scope.id = snapshot.inventory_scope_id
    JOIN public.storage_metering_source_requirements AS requirement
      ON requirement.inventory_scope_id = scope.id
     AND requirement.measurement_basis = NEW.measurement_basis
     AND requirement.collector_id = scope.collector_id
     AND requirement.source_cluster = scope.source_cluster
     AND requirement.requirement_role = 'quantity'
    JOIN public.storage_metering_source_activations AS source_activation
      ON source_activation.measurement_basis = requirement.measurement_basis
     AND source_activation.collector_id = requirement.collector_id
     AND source_activation.source_cluster = requirement.source_cluster
    JOIN public.storage_metering_activation AS global_activation
      ON global_activation.measurement_basis = requirement.measurement_basis
    WHERE snapshot.id = NEW.snapshot_id
      AND snapshot.inventory_scope_id = NEW.inventory_scope_id
      AND snapshot.leader_generation = control.leader_generation
      AND ticket.bound_snapshot_id = snapshot.id
      AND ticket.consumed_at IS NULL
      AND ticket.expires_at > statement_timestamp()
    FOR SHARE OF source_activation, global_activation;

    IF snapshot_state IS DISTINCT FROM 'staging'
       OR global_state NOT IN ('shadow', 'active')
       OR source_state NOT IN ('shadow', 'active') THEN
        RAISE EXCEPTION 'storage shadow observation source fence failed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_storage_volume_asset_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_storage_volume_asset_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    transition_ok BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'storage volume assets cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.source_cluster IS DISTINCT FROM OLD.source_cluster
       OR NEW.asset_digest IS DISTINCT FROM OLD.asset_digest
       OR NEW.identity_key_version IS DISTINCT FROM OLD.identity_key_version
       OR NEW.identity_scheme IS DISTINCT FROM OLD.identity_scheme
       OR NEW.csi_driver IS DISTINCT FROM OLD.csi_driver
       OR NEW.source_lifecycle_id IS DISTINCT FROM OLD.source_lifecycle_id
       OR NEW.first_observed_at IS DISTINCT FROM OLD.first_observed_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'storage volume asset identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.last_observed_at < OLD.last_observed_at
       OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'storage volume asset cursors are monotonic'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.lifecycle_state = 'destroyed' THEN
        IF NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'destroyed storage volume assets are immutable'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.lifecycle_state = 'backend-unverified' THEN
        SELECT TRUE INTO transition_ok
        FROM public.storage_asset_coverage_gaps AS gap
        WHERE gap.asset_id = OLD.id AND gap.resolution = 'unresolved';
    ELSIF NEW.lifecycle_state = 'visible'
          AND OLD.lifecycle_state = 'backend-unverified' THEN
        SELECT TRUE INTO transition_ok
        FROM public.storage_asset_coverage_gaps AS gap
        WHERE gap.asset_id = OLD.id AND gap.resolution = 'reobserved'
        ORDER BY gap.gap_end DESC LIMIT 1;
    ELSIF NEW.lifecycle_state = 'destroyed' THEN
        SELECT TRUE INTO transition_ok
        FROM public.storage_backend_assertions AS assertion
        WHERE assertion.id = NEW.destruction_assertion_id
          AND assertion.asset_id = OLD.id
          AND assertion.effective_at = NEW.destroyed_at;
    ELSE
        transition_ok := NEW.lifecycle_state = OLD.lifecycle_state;
    END IF;

    IF transition_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'storage volume asset transition lacks durable evidence'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: protect_storage_volume_incarnation_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_storage_volume_incarnation_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'storage volume incarnations cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.asset_id IS DISTINCT FROM OLD.asset_id
       OR NEW.inventory_scope_id IS DISTINCT FROM OLD.inventory_scope_id
       OR NEW.source_cluster IS DISTINCT FROM OLD.source_cluster
       OR NEW.pv_uid IS DISTINCT FROM OLD.pv_uid
       OR NEW.pv_name IS DISTINCT FROM OLD.pv_name
       OR NEW.first_observed_at IS DISTINCT FROM OLD.first_observed_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'storage volume incarnation identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.last_observed_at < OLD.last_observed_at
       OR NEW.updated_at < OLD.updated_at
       OR (OLD.backend_deletion_finalizer_observed
           AND NOT NEW.backend_deletion_finalizer_observed)
       OR (OLD.detached_at IS NOT NULL AND NEW IS DISTINCT FROM OLD)
       OR (OLD.detached_at IS NULL AND NEW.detached_at IS NOT NULL
           AND NEW.detached_at < OLD.last_observed_at) THEN
        RAISE EXCEPTION 'storage volume incarnation lifecycle is not monotonic'
            USING ERRCODE = '55000';
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
-- Name: protect_usage_rate_v2_referenced_range(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.protect_usage_rate_v2_referenced_range() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    blocking_plan UUID;
BEGIN
    IF OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL THEN
        SELECT plan.id
        INTO blocking_plan
        FROM public.resource_publication_plan_events AS event
        JOIN public.resource_publication_plans AS plan
          ON plan.id = event.plan_id
        WHERE event.canonical_rate_version_id = OLD.id
          AND plan.state IN ('planned', 'published', 'conflict')
          AND plan.period_end > NEW.effective_to
        ORDER BY plan.period_end DESC, plan.id
        LIMIT 1;

        IF blocking_plan IS NOT NULL THEN
            RAISE EXCEPTION
                'usage rate % cannot close before retained publication plan % ends',
                OLD.id, blocking_plan
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
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
-- Name: record_compute_authority_confirmation_gap(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.record_compute_authority_confirmation_gap() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    authority_gap_start TIMESTAMPTZ;
BEGIN
    IF NEW.authority_sequence = 1 THEN
        authority_gap_start := NEW.effective_from;
    ELSE
        SELECT epoch.retired_at
        INTO authority_gap_start
        FROM public.resource_inventory_scope_epochs AS epoch
        WHERE epoch.id = NEW.predecessor_epoch_id
          AND epoch.scope_id = NEW.inventory_scope_id
        FOR SHARE;

        IF authority_gap_start IS NULL THEN
            RAISE EXCEPTION
                'compute authority confirmation gap requires predecessor retirement'
                USING ERRCODE = '55000';
        END IF;
    END IF;

    INSERT INTO public.resource_inventory_coverage_gaps (
        scope_epoch_id, gap_start, reason, resolution_details
    ) VALUES (
        NEW.inventory_scope_epoch_id,
        authority_gap_start,
        'compute-authority-awaiting-confirmation:' || NEW.activation_key,
        pg_catalog.jsonb_build_object(
            'code', 'compute-authority-awaiting-confirmation',
            'activation_key', NEW.activation_key,
            'authority_id', NEW.id,
            'previous_authority_id', NEW.previous_authority_id,
            'promotion_request_id', NEW.promotion_request_id,
            'authority_effective_from', NEW.effective_from
        )
    );

    RETURN NEW;
END;
$$;


--
-- Name: FUNCTION record_compute_authority_confirmation_gap(); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.record_compute_authority_confirmation_gap() IS 'Opens a fail-closed coverage gap until a post-authority complete LIST confirms exact interval binding.';


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
-- Name: resource_inventory_snapshot_item_size_bytes(text, text, text, jsonb, jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.resource_inventory_snapshot_item_size_bytes(source_kind text, source_uid text, revision_hash text, normalized_item jsonb, item_error jsonb) RETURNS bigint
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog'
    AS $$
    SELECT 64::BIGINT
         + octet_length(source_kind)::BIGINT
         + octet_length(source_uid)::BIGINT
         + COALESCE(octet_length(revision_hash), 0)::BIGINT
         + octet_length(normalized_item::TEXT)::BIGINT
         + COALESCE(octet_length(item_error::TEXT), 0)::BIGINT
$$;


--
-- Name: FUNCTION resource_inventory_snapshot_item_size_bytes(source_kind text, source_uid text, revision_hash text, normalized_item jsonb, item_error jsonb); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.resource_inventory_snapshot_item_size_bytes(source_kind text, source_uid text, revision_hash text, normalized_item jsonb, item_error jsonb) IS 'Deterministic logical payload bytes for snapshot bounds; never physical TOAST size.';


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
-- Name: revoke_runtime_actor_grants_on_agent_delete(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.revoke_runtime_actor_grants_on_agent_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    -- Preserve the UUID snapshot while making the deleted runtime's access
    -- and refresh authority unusable even to a pre-0171 application replica.
    UPDATE public.runtime_actor_grants
       SET revoked_at = COALESCE(revoked_at, statement_timestamp())
     WHERE caller_kind = 'officer'
       AND agent_id = OLD.id
       AND revoked_at IS NULL;
    RETURN OLD;
END;
$$;


--
-- Name: FUNCTION revoke_runtime_actor_grants_on_agent_delete(); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.revoke_runtime_actor_grants_on_agent_delete() IS 'Revokes live Officer authority before operational agent deletion while retaining the immutable agent UUID snapshot for grant audit provenance.';


--
-- Name: serialize_resource_interval_statement_with_cutover(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.serialize_resource_interval_statement_with_cutover() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    control_exists BOOLEAN;
BEGIN
    SELECT TRUE INTO control_exists
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;
    IF control_exists IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'infra metering control row is missing'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$;


--
-- Name: serialize_resource_lifecycle_head_with_cutover(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.serialize_resource_lifecycle_head_with_cutover() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    control_exists BOOLEAN;
BEGIN
    SELECT TRUE INTO control_exists
    FROM public.infra_metering_control
    WHERE singleton = TRUE
    FOR SHARE;

    IF control_exists IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'infra metering control row is missing'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$;


--
-- Name: settle_job_wakes_before_thread_delete(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.settle_job_wakes_before_thread_delete() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
    AS $$
BEGIN
    UPDATE public.jobs
    SET wake_state = 'undeliverable',
        wake_claimed_at = NULL,
        updated_at = CURRENT_TIMESTAMP
    WHERE wake_on_complete
      AND created_by_thread_id = OLD.id
      AND wake_state IN ('none', 'pending', 'sending');
    RETURN OLD;
END;
$$;


--
-- Name: FUNCTION settle_job_wakes_before_thread_delete(); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.settle_job_wakes_before_thread_delete() IS 'Atomically retires open completion wakes before jobs.creator FK ON DELETE SET NULL. The trigger is the rolling-version guard for old/raw thread deletes; PostgresDB.delete_thread performs the same update explicitly.';


--
-- Name: transition_storage_asset_for_destruction(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.transition_storage_asset_for_destruction() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    UPDATE public.storage_asset_coverage_gaps
    SET gap_end = NEW.effective_at,
        resolution = 'destroyed-confirmed',
        resolution_assertion_id = NEW.id,
        resolved_at = statement_timestamp(),
        updated_at = statement_timestamp()
    WHERE asset_id = NEW.asset_id AND resolution = 'unresolved';

    UPDATE public.storage_volume_incarnations
    SET detached_at = GREATEST(last_observed_at, NEW.effective_at),
        detach_reason = 'backend-destroyed',
        updated_at = statement_timestamp()
    WHERE asset_id = NEW.asset_id AND detached_at IS NULL;

    UPDATE public.storage_volume_assets
    SET lifecycle_state = 'destroyed',
        destroyed_at = NEW.effective_at,
        destruction_assertion_id = NEW.id,
        updated_at = statement_timestamp()
    WHERE id = NEW.asset_id;
    RETURN NULL;
END;
$$;


--
-- Name: transition_storage_asset_for_gap(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.transition_storage_asset_for_gap() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE public.storage_volume_assets
        SET lifecycle_state = 'backend-unverified',
            backend_unverified_at = NEW.gap_start,
            updated_at = statement_timestamp()
        WHERE id = NEW.asset_id;
    ELSIF NEW.resolution = 'reobserved' THEN
        UPDATE public.storage_volume_assets
        SET lifecycle_state = 'visible',
            backend_unverified_at = NULL,
            last_observed_at = GREATEST(last_observed_at, NEW.gap_end),
            updated_at = statement_timestamp()
        WHERE id = NEW.asset_id;
    END IF;
    RETURN NULL;
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
-- Name: validate_inventory_watch_terminal_interval_evidence(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.validate_inventory_watch_terminal_interval_evidence() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    evidence_ok BOOLEAN;
BEGIN
    IF NEW.mutation_action <> 'not-applicable'
       OR NEW.affected_interval_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT TRUE INTO evidence_ok
    FROM public.resource_inventory_scope_epochs epoch
    JOIN public.resource_intervals interval
      ON interval.inventory_scope_id = epoch.scope_id
    WHERE epoch.id = NEW.scope_epoch_id
      AND interval.id = NEW.affected_interval_id
      AND interval.source_kind = NEW.source_kind
      AND interval.source_uid = NEW.source_uid
      AND interval.ended_at = NEW.received_at
      AND interval.end_time_source = 'app-db-received'
      AND interval.end_reason IN ('not-applicable', 'terminal-or-unscheduled');

    IF evidence_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            'not-applicable WATCH evidence does not match its terminal interval'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: validate_legacy_workspace_cutover_plan_manifest(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.validate_legacy_workspace_cutover_plan_manifest() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    target_plan UUID;
    expected_count INTEGER;
    actual_count INTEGER;
    min_ordinal INTEGER;
    max_ordinal INTEGER;
BEGIN
    target_plan := COALESCE(
        (to_jsonb(NEW) ->> 'id')::UUID,
        (to_jsonb(NEW) ->> 'plan_id')::UUID
    );
    SELECT expected_event_count INTO expected_count
    FROM public.legacy_workspace_cutover_plans WHERE id = target_plan;
    SELECT count(*), min(ordinal), max(ordinal)
    INTO actual_count, min_ordinal, max_ordinal
    FROM public.legacy_workspace_cutover_plan_events
    WHERE plan_id = target_plan;
    IF expected_count IS NULL
       OR actual_count <> expected_count
       OR min_ordinal <> 0
       OR max_ordinal <> expected_count - 1 THEN
        RAISE EXCEPTION 'legacy workspace cutover plan manifest is incomplete'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
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
-- Name: validate_resource_interval_snapshot_end_evidence(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.validate_resource_interval_snapshot_end_evidence() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog'
    AS $$
DECLARE
    evidence_ok BOOLEAN;
    boundary_already_linked BOOLEAN;
BEGIN
    IF OLD.ended_at IS NULL
       OR (NEW.last_seen_at IS NOT DISTINCT FROM OLD.last_seen_at
           AND NEW.last_seen_snapshot_id
                IS NOT DISTINCT FROM OLD.last_seen_snapshot_id) THEN
        RETURN NEW;
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM public.resource_inventory_snapshots snapshot
        WHERE snapshot.id = OLD.last_seen_snapshot_id
          AND snapshot.inventory_scope_id = OLD.inventory_scope_id
          AND snapshot.complete = TRUE
          AND snapshot.manifest_state IN ('sealed', 'items-expired')
          AND snapshot.received_at = OLD.ended_at
    ) INTO boundary_already_linked;

    IF boundary_already_linked THEN
        RAISE EXCEPTION
            'closed interval already has immutable terminal snapshot evidence'
            USING ERRCODE = '55000';
    END IF;

    SELECT TRUE INTO evidence_ok
    FROM public.resource_inventory_snapshots snapshot
    JOIN public.resource_inventory_snapshot_items item
      ON item.snapshot_id = snapshot.id
     AND item.source_kind = OLD.source_kind
     AND item.source_uid = OLD.source_uid
     AND item.valid_for_metering = TRUE
    WHERE OLD.end_time_source = 'app-db-received'
      AND OLD.end_reason IN ('not-applicable', 'terminal-or-unscheduled')
      AND OLD.last_seen_at <= OLD.ended_at
      AND NEW.last_seen_at = GREATEST(OLD.last_seen_at, OLD.ended_at)
      AND NEW.last_seen_snapshot_id = snapshot.id
      AND NEW.last_seen_snapshot_id
            IS DISTINCT FROM OLD.last_seen_snapshot_id
      AND snapshot.inventory_scope_id = OLD.inventory_scope_id
      AND snapshot.complete = TRUE
      AND snapshot.manifest_state IN ('sealed', 'items-expired')
      AND snapshot.received_at = OLD.ended_at;

    IF evidence_ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            'closed interval snapshot evidence does not match its terminal boundary'
            USING ERRCODE = '55000';
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
-- Name: agent_metering_binding_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_metering_binding_events (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    agent_id uuid NOT NULL,
    revision bigint NOT NULL,
    agent_present boolean NOT NULL,
    pod_uid text,
    hostname text,
    identity_state text NOT NULL,
    attribution_scope text NOT NULL,
    owner_kind text,
    owner_id uuid,
    user_id uuid,
    project_id uuid,
    reason_code text NOT NULL,
    transition_source text NOT NULL,
    effective_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_metering_binding_events_attribution_check CHECK (((attribution_scope = ANY (ARRAY['customer'::text, 'shared-platform'::text, 'unknown'::text])) AND (((attribution_scope = 'customer'::text) AND agent_present AND (identity_state = 'valid'::text) AND (owner_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (owner_id IS NOT NULL) AND (user_id IS NOT NULL)) OR ((attribution_scope = 'shared-platform'::text) AND agent_present AND (identity_state = 'valid'::text) AND (owner_kind IS NULL) AND (owner_id IS NULL) AND (user_id IS NULL) AND (project_id IS NULL)) OR ((attribution_scope = 'unknown'::text) AND (owner_kind IS NULL) AND (owner_id IS NULL) AND (user_id IS NULL) AND (project_id IS NULL))))),
    CONSTRAINT agent_metering_binding_events_identity_check CHECK (((revision > 0) AND ((pod_uid IS NULL) OR ((pod_uid <> ''::text) AND (length(pod_uid) <= 256))) AND ((hostname IS NULL) OR ((hostname <> ''::text) AND (length(hostname) <= 255))) AND (identity_state = ANY (ARRAY['valid'::text, 'missing'::text, 'duplicate'::text])) AND (reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text) AND (transition_source ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text)))
);


--
-- Name: TABLE agent_metering_binding_events; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_metering_binding_events IS 'Append-only revisions of mutually validated agent Pod identity and job/thread attribution.';


--
-- Name: agent_metering_pod_identity_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_metering_pod_identity_state (
    agent_id uuid NOT NULL,
    agent_present boolean NOT NULL,
    pod_uid text,
    hostname text,
    identity_state text NOT NULL,
    attribution_scope text NOT NULL,
    owner_kind text,
    owner_id uuid,
    user_id uuid,
    project_id uuid,
    reason_code text NOT NULL,
    transition_source text NOT NULL,
    revision bigint NOT NULL,
    effective_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_metering_pod_identity_state_attribution_check CHECK (((attribution_scope = ANY (ARRAY['customer'::text, 'shared-platform'::text, 'unknown'::text])) AND (((attribution_scope = 'customer'::text) AND agent_present AND (identity_state = 'valid'::text) AND (owner_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (owner_id IS NOT NULL) AND (user_id IS NOT NULL)) OR ((attribution_scope = 'shared-platform'::text) AND agent_present AND (identity_state = 'valid'::text) AND (owner_kind IS NULL) AND (owner_id IS NULL) AND (user_id IS NULL) AND (project_id IS NULL)) OR ((attribution_scope = 'unknown'::text) AND (owner_kind IS NULL) AND (owner_id IS NULL) AND (user_id IS NULL) AND (project_id IS NULL))))),
    CONSTRAINT agent_metering_pod_identity_state_identity_check CHECK (((revision > 0) AND ((pod_uid IS NULL) OR ((pod_uid <> ''::text) AND (length(pod_uid) <= 256))) AND ((hostname IS NULL) OR ((hostname <> ''::text) AND (length(hostname) <= 255))) AND (identity_state = ANY (ARRAY['valid'::text, 'missing'::text, 'duplicate'::text])) AND (reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text) AND (transition_source ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text) AND ((agent_present AND (identity_state = ANY (ARRAY['valid'::text, 'duplicate'::text])) AND (pod_uid IS NOT NULL)) OR (agent_present AND (identity_state = 'missing'::text) AND (pod_uid IS NULL)) OR ((NOT agent_present) AND (identity_state = 'missing'::text)))))
);


--
-- Name: TABLE agent_metering_pod_identity_state; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_metering_pod_identity_state IS 'Current converged agent Pod identity and attribution head, including missing/duplicate ambiguity and deletion tombstones.';


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
-- Name: canvas_editor_awareness; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.canvas_editor_awareness (
    thread_id uuid NOT NULL,
    canvas_id character varying(64) DEFAULT 'main'::character varying NOT NULL,
    editing_session_id character varying(128) NOT NULL,
    sender_id uuid DEFAULT gen_random_uuid() NOT NULL,
    state character varying(16) NOT NULL,
    client_seq bigint NOT NULL,
    path text NOT NULL,
    presentation_revision bigint NOT NULL,
    source_version character varying(71) NOT NULL,
    refreshed_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_canvas_editor_awareness_client_seq CHECK ((client_seq > 0)),
    CONSTRAINT ck_canvas_editor_awareness_expiry CHECK (((((state)::text = 'editing'::text) AND (expires_at > refreshed_at)) OR (((state)::text = 'idle'::text) AND (expires_at = refreshed_at)))),
    CONSTRAINT ck_canvas_editor_awareness_main CHECK (((canvas_id)::text = 'main'::text)),
    CONSTRAINT ck_canvas_editor_awareness_path CHECK (((char_length(path) >= 1) AND (char_length(path) <= 4096))),
    CONSTRAINT ck_canvas_editor_awareness_revision CHECK ((presentation_revision > 0)),
    CONSTRAINT ck_canvas_editor_awareness_session_id CHECK (((editing_session_id)::text ~ '^[A-Za-z0-9_-]{8,128}$'::text)),
    CONSTRAINT ck_canvas_editor_awareness_source_version CHECK (((source_version)::text ~ '^sha256:[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_canvas_editor_awareness_state CHECK (((state)::text = ANY ((ARRAY['editing'::character varying, 'idle'::character varying])::text[])))
);


--
-- Name: TABLE canvas_editor_awareness; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.canvas_editor_awareness IS 'Owner-authenticated Canvas editor courtesy leases. Per-editor rows keep tabs independent; idle tombstones and client_seq reject reordered stale renewals. This is UX state only, never authorization or execution lease.';


--
-- Name: COLUMN canvas_editor_awareness.sender_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvas_editor_awareness.sender_id IS 'Server-minted stable public fan-out identity for this editor row.';


--
-- Name: COLUMN canvas_editor_awareness.client_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvas_editor_awareness.client_seq IS 'Client-monotonic sequence. Lower values never mutate the row; equal values are idempotent only when the complete state and Canvas identity match.';


--
-- Name: COLUMN canvas_editor_awareness.expires_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.canvas_editor_awareness.expires_at IS 'Database-clock editing deadline. Idle tombstones set expires_at equal to refreshed_at and remain briefly so delayed lower-sequence renewals lose.';


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
-- Name: completion_effects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.completion_effects (
    producer_kind text NOT NULL,
    producer_id uuid NOT NULL,
    scope_id uuid,
    effect_name text NOT NULL,
    effect_group text NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 5 NOT NULL,
    run_after timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    intent_at timestamp with time zone,
    complete_by timestamp with time zone,
    completed_at timestamp with time zone,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_code text,
    claimed_by uuid
);


--
-- Name: TABLE completion_effects; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.completion_effects IS 'One stable-name progress row per completion effect. Polymorphic by producer_kind and deliberately has no foreign key or state-driven partial index. job_completion producers use the command finalizer states; session_turn producers use pending, done, or dead and are age-pruned from created_at. Retention is explicit for both kinds.';


--
-- Name: COLUMN completion_effects.claimed_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.completion_effects.claimed_by IS 'Independent session-effect drain claim identity. NULL means unclaimed; a session drain may complete or release only the UUID it claimed.';


--
-- Name: completion_finalizer_leases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.completion_finalizer_leases (
    lease_name text NOT NULL,
    leader_id text NOT NULL,
    elected_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT completion_finalizer_leader_id_nonempty CHECK ((btrim(leader_id) <> ''::text)),
    CONSTRAINT completion_finalizer_lease_expiry_order CHECK ((expires_at > elected_at)),
    CONSTRAINT completion_finalizer_lease_name_nonempty CHECK ((btrim(lease_name) <> ''::text))
);


--
-- Name: TABLE completion_finalizer_leases; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.completion_finalizer_leases IS 'Observable expiring leader leases for completion finalizer drains. Election inserts a named row, renewal fences on leader_id plus elected_at, and failover reaps only an expired row.';


--
-- Name: compute_metering_activation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compute_metering_activation (
    activation_key text NOT NULL,
    state text DEFAULT 'disabled'::text NOT NULL,
    activated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT compute_metering_activation_key_check CHECK ((activation_key = ANY (ARRAY['agent_pod'::text, 'ide_workspace_pod'::text, 'workspace_vm'::text]))),
    CONSTRAINT compute_metering_activation_state_check CHECK ((((state = ANY (ARRAY['disabled'::text, 'shadow'::text])) AND (activated_at IS NULL)) OR ((state = 'active'::text) AND (activated_at IS NOT NULL) AND (activated_at = date_trunc('day'::text, activated_at, 'UTC'::text)))))
);


--
-- Name: TABLE compute_metering_activation; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.compute_metering_activation IS 'Forward-only activation boundaries for agent Pods, IDE workspace Pods, and VMI compute.';


--
-- Name: compute_metering_epoch_authorities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compute_metering_epoch_authorities (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    activation_key text NOT NULL,
    collector_id text NOT NULL,
    source_cluster text NOT NULL,
    inventory_scope_id uuid NOT NULL,
    inventory_scope_epoch_id uuid NOT NULL,
    previous_authority_id uuid,
    predecessor_epoch_id uuid,
    authority_sequence bigint NOT NULL,
    effective_from timestamp with time zone NOT NULL,
    proof_snapshot_id uuid NOT NULL,
    proof_generation bigint NOT NULL,
    promotion_request_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT statement_timestamp() NOT NULL,
    CONSTRAINT compute_epoch_authorities_sequence_check CHECK (((authority_sequence > 0) AND (proof_generation > 0) AND (((authority_sequence = 1) AND (previous_authority_id IS NULL) AND (predecessor_epoch_id IS NULL)) OR ((authority_sequence > 1) AND (previous_authority_id IS NOT NULL) AND (predecessor_epoch_id IS NOT NULL)))))
);


--
-- Name: TABLE compute_metering_epoch_authorities; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.compute_metering_epoch_authorities IS 'Append-only per-class exact-epoch authority; effective end is the bound inventory epoch retired_at and gaps are never inherited.';


--
-- Name: compute_metering_epoch_promotion_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compute_metering_epoch_promotion_requests (
    id uuid NOT NULL,
    activation_key text NOT NULL,
    request_kind text NOT NULL,
    collector_id text NOT NULL,
    source_cluster text NOT NULL,
    request_digest text NOT NULL,
    actor_id uuid NOT NULL,
    audit_reason text NOT NULL,
    promoted_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT statement_timestamp() NOT NULL,
    CONSTRAINT compute_epoch_promotion_requests_identity_check CHECK (((collector_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'::text) AND (source_cluster ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$'::text) AND (request_digest ~ '^[0-9a-f]{64}$'::text) AND ((length(btrim(audit_reason)) >= 1) AND (length(btrim(audit_reason)) <= 2048)) AND (promoted_at = created_at))),
    CONSTRAINT compute_epoch_promotion_requests_kind_check CHECK ((request_kind = ANY (ARRAY['initial-activation'::text, 'recovery-rollover'::text])))
);


--
-- Name: TABLE compute_metering_epoch_promotion_requests; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.compute_metering_epoch_promotion_requests IS 'Immutable fleet-admin idempotency and audit ledger for initial and recovery compute epoch promotion.';


--
-- Name: compute_metering_scope_requirements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compute_metering_scope_requirements (
    activation_key text NOT NULL,
    collector_id text NOT NULL,
    source_cluster text NOT NULL,
    inventory_scope_id uuid NOT NULL,
    required_from timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT statement_timestamp() NOT NULL,
    inventory_scope_epoch_id uuid NOT NULL,
    CONSTRAINT compute_metering_scope_requirements_boundary_check CHECK ((required_from = date_trunc('day'::text, required_from, 'UTC'::text)))
);


--
-- Name: TABLE compute_metering_scope_requirements; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.compute_metering_scope_requirements IS 'Immutable exact inventory scopes and per-class boundary proven when one Slice 3 compute class is activated.';


--
-- Name: COLUMN compute_metering_scope_requirements.inventory_scope_epoch_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.compute_metering_scope_requirements.inventory_scope_epoch_id IS 'Exact immutable inventory epoch whose class-specific shadow proof authorized this scope; epoch rollover fails closed in v1.';


--
-- Name: compute_shadow_observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compute_shadow_observations (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    activation_key text NOT NULL,
    snapshot_id uuid NOT NULL,
    inventory_scope_id uuid NOT NULL,
    source_kind text NOT NULL,
    source_uid text NOT NULL,
    resource text NOT NULL,
    product_class text NOT NULL,
    cpu_millicores bigint,
    memory_bytes bigint,
    attribution_scope text NOT NULL,
    owner_kind text,
    owner_id uuid,
    user_id uuid,
    project_id uuid,
    disposition text NOT NULL,
    reason_code text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT compute_shadow_observations_attribution_check CHECK (((attribution_scope = ANY (ARRAY['customer'::text, 'shared-platform'::text, 'unknown'::text])) AND (((attribution_scope = 'customer'::text) AND (owner_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (owner_id IS NOT NULL) AND (user_id IS NOT NULL)) OR ((attribution_scope = 'shared-platform'::text) AND (owner_kind = 'platform'::text) AND (owner_id IS NULL) AND (user_id IS NULL) AND (project_id IS NULL)) OR ((attribution_scope = 'unknown'::text) AND (owner_kind IS NULL) AND (owner_id IS NULL) AND (user_id IS NULL) AND (project_id IS NULL))) AND (disposition = ANY (ARRAY['eligible-unpriced'::text, 'not-applicable'::text, 'invalid'::text, 'identity-ambiguous'::text])))),
    CONSTRAINT compute_shadow_observations_capacity_check CHECK ((((cpu_millicores IS NULL) OR (cpu_millicores >= 0)) AND ((memory_bytes IS NULL) OR (memory_bytes >= 0)) AND (((disposition = 'eligible-unpriced'::text) AND (cpu_millicores IS NOT NULL) AND (memory_bytes IS NOT NULL)) OR (disposition <> 'eligible-unpriced'::text)))),
    CONSTRAINT compute_shadow_observations_identity_check CHECK (((source_uid <> ''::text) AND (length(source_uid) <= 256) AND (resource <> ''::text) AND (length(resource) <= 128) AND (product_class ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text) AND (reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text) AND (((activation_key = 'agent_pod'::text) AND (source_kind = 'pod'::text) AND (resource = 'agent_pod'::text)) OR ((activation_key = 'ide_workspace_pod'::text) AND (source_kind = 'pod'::text) AND (resource = 'workspace_pod'::text) AND (product_class = 'ide-session'::text)) OR ((activation_key = 'workspace_vm'::text) AND (source_kind = 'vmi'::text) AND (resource = 'workspace_vm'::text)))))
);


--
-- Name: TABLE compute_shadow_observations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.compute_shadow_observations IS 'Immutable non-publishable per-item compute shadow classifications; no ledger relationship.';


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
-- Name: datasource_tombstones; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.datasource_tombstones (
    id uuid NOT NULL,
    name text NOT NULL,
    deleted_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_by uuid
);


--
-- Name: TABLE datasource_tombstones; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.datasource_tombstones IS 'Names of deleted connectors, kept so drifted session config can label a dangling datasource_id instead of showing a bare uuid.';


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
    cutover_phase text DEFAULT 'disabled'::text NOT NULL,
    cutover_request_id uuid,
    cutover_actor_id uuid,
    cutover_reason text,
    cutover_requested_at timestamp with time zone,
    barrier_committed_at timestamp with time zone,
    legacy_drained_at timestamp with time zone,
    activated_at timestamp with time zone,
    cutover_error jsonb,
    CONSTRAINT infra_metering_control_cutover_check CHECK ((((cutover_state = 'disabled'::text) AND (cutover_at IS NULL)) OR ((cutover_state = ANY (ARRAY['preparing'::text, 'active'::text])) AND (cutover_at IS NOT NULL)))),
    CONSTRAINT infra_metering_control_cutover_error_check CHECK (((cutover_error IS NULL) OR (jsonb_typeof(cutover_error) = 'object'::text))),
    CONSTRAINT infra_metering_control_cutover_phase_check CHECK ((((cutover_state = 'disabled'::text) AND (cutover_phase = 'disabled'::text) AND (cutover_at IS NULL) AND (cutover_request_id IS NULL) AND (cutover_actor_id IS NULL) AND (cutover_reason IS NULL) AND (cutover_requested_at IS NULL) AND (barrier_committed_at IS NULL) AND (legacy_drained_at IS NULL) AND (activated_at IS NULL) AND (cutover_error IS NULL)) OR ((cutover_state = 'preparing'::text) AND (cutover_phase = ANY (ARRAY['legacy-draining'::text, 'ready-to-activate'::text])) AND (cutover_at IS NOT NULL) AND (cutover_request_id IS NOT NULL) AND (cutover_actor_id IS NOT NULL) AND (cutover_reason IS NOT NULL) AND (cutover_reason = btrim(cutover_reason)) AND ((char_length(cutover_reason) >= 1) AND (char_length(cutover_reason) <= 1024)) AND (cutover_reason !~ '[[:cntrl:]]'::text) AND (cutover_requested_at IS NOT NULL) AND (barrier_committed_at IS NOT NULL) AND (cutover_requested_at = cutover_at) AND (barrier_committed_at = cutover_at) AND (((cutover_phase = 'legacy-draining'::text) AND (legacy_drained_at IS NULL)) OR ((cutover_phase = 'ready-to-activate'::text) AND (legacy_drained_at IS NOT NULL) AND (legacy_drained_at >= cutover_at))) AND (activated_at IS NULL)) OR ((cutover_state = 'active'::text) AND (cutover_phase = 'active'::text) AND (cutover_at IS NOT NULL) AND (cutover_request_id IS NOT NULL) AND (cutover_actor_id IS NOT NULL) AND (cutover_reason IS NOT NULL) AND (cutover_reason = btrim(cutover_reason)) AND ((char_length(cutover_reason) >= 1) AND (char_length(cutover_reason) <= 1024)) AND (cutover_reason !~ '[[:cntrl:]]'::text) AND (cutover_requested_at IS NOT NULL) AND (barrier_committed_at IS NOT NULL) AND (cutover_requested_at = cutover_at) AND (barrier_committed_at = cutover_at) AND (legacy_drained_at IS NOT NULL) AND (legacy_drained_at >= cutover_at) AND (activated_at IS NOT NULL) AND (activated_at >= legacy_drained_at)))),
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
    coverage_sequence bigint DEFAULT 0 NOT NULL,
    CONSTRAINT infra_usage_day_state_coverage_sequence_check CHECK ((((state = 'sealed'::text) AND (coverage_sequence > 0)) OR ((state <> 'sealed'::text) AND (coverage_sequence = 0)))),
    CONSTRAINT infra_usage_day_state_shape_check CHECK (((state = ANY (ARRAY['open'::text, 'sealing'::text, 'sealed'::text])) AND ((coverage_status IS NULL) OR (coverage_status = ANY (ARRAY['complete'::text, 'partial'::text]))) AND ((coverage_status IS NULL) = (coverage_revision IS NULL)) AND ((coverage_revision IS NULL) OR (coverage_revision <> ''::text)) AND (jsonb_typeof(unknown_ranges) = 'array'::text) AND (((state = 'sealed'::text) AND (coverage_status IS NOT NULL) AND (coverage_revision IS NOT NULL) AND (sealed_at IS NOT NULL)) OR ((state <> 'sealed'::text) AND (sealed_at IS NULL)))))
);


--
-- Name: infrastructure_storage_resource_mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.infrastructure_storage_resource_mappings (
    source_cluster text NOT NULL,
    storage_class_name text,
    csi_driver text,
    volume_mode text NOT NULL,
    resource text NOT NULL,
    mapping_version text NOT NULL,
    rule_fingerprint character(64) NOT NULL,
    registered_at timestamp with time zone DEFAULT statement_timestamp() NOT NULL,
    CONSTRAINT infrastructure_storage_resource_mappings_output_check CHECK (((resource ~ '^block_volume_[a-z0-9_]+$'::text) AND (length(resource) <= 128) AND (mapping_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'::text) AND (rule_fingerprint ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT infrastructure_storage_resource_mappings_selector_check CHECK (((source_cluster ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$'::text) AND ((storage_class_name IS NULL) OR ((storage_class_name ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$'::text) AND (length(storage_class_name) <= 253) AND (storage_class_name <> ALL (ARRAY['unknown'::text, 'unmapped'::text, 'any'::text])))) AND ((csi_driver IS NULL) OR ((csi_driver ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$'::text) AND (length(csi_driver) <= 253) AND (csi_driver <> ALL (ARRAY['unknown'::text, 'unmapped'::text, 'any'::text])))) AND (volume_mode = ANY (ARRAY['filesystem'::text, 'block'::text]))))
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
-- Name: job_completion_commands; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_completion_commands (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    job_id uuid NOT NULL,
    report_seq bigint NOT NULL,
    client_report_id uuid NOT NULL,
    payload jsonb NOT NULL,
    payload_digest text NOT NULL,
    reported_at timestamp with time zone DEFAULT now() NOT NULL,
    accepted_lease_token bigint,
    accepted_agent_id uuid,
    origin text DEFAULT 'agent'::text NOT NULL,
    requested_by text NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 5 NOT NULL,
    run_after timestamp with time zone DEFAULT now() NOT NULL,
    lease_expires_at timestamp with time zone,
    deadline_at timestamp with time zone NOT NULL,
    finalizing_by text,
    code_version text NOT NULL,
    outcome jsonb,
    finalized_at timestamp with time zone,
    error_code text,
    accepted_job_status text,
    status_reorder_enabled boolean DEFAULT false NOT NULL,
    CONSTRAINT job_completion_accepted_status_nonempty CHECK (((accepted_job_status IS NULL) OR (btrim(accepted_job_status) <> ''::text))),
    CONSTRAINT job_completion_fence_exactly_one CHECK ((((origin = 'operator'::text) AND (accepted_lease_token IS NULL) AND (accepted_agent_id IS NULL)) OR ((origin <> 'operator'::text) AND (((accepted_lease_token IS NOT NULL) AND (accepted_agent_id IS NULL)) OR ((accepted_lease_token IS NULL) AND (accepted_agent_id IS NOT NULL)))))),
    CONSTRAINT job_completion_state_value CHECK ((state = ANY (ARRAY['pending'::text, 'finalizing'::text, 'done'::text, 'parked'::text, 'superseded'::text, 'force_resolved'::text]))),
    CONSTRAINT job_completion_terminal_shape CHECK ((((state = ANY (ARRAY['pending'::text, 'finalizing'::text])) AND (outcome IS NULL) AND (finalized_at IS NULL) AND (error_code IS NULL)) OR ((state = 'done'::text) AND (outcome IS NOT NULL) AND (finalized_at IS NOT NULL) AND (error_code IS NULL)) OR ((state = 'force_resolved'::text) AND (outcome IS NOT NULL) AND (finalized_at IS NOT NULL)) OR ((state = 'superseded'::text) AND (outcome IS NOT NULL) AND (finalized_at IS NOT NULL) AND (error_code IS NOT NULL) AND (btrim(error_code) <> ''::text) AND (finalizing_by IS NULL) AND (lease_expires_at IS NULL)) OR ((state = 'parked'::text) AND (error_code IS NOT NULL) AND (outcome IS NULL) AND (finalized_at IS NULL))))
);


--
-- Name: TABLE job_completion_commands; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.job_completion_commands IS 'Durable, commit-ordered completion reports for both pinned and stateless job lanes. Agent-origin reports are fenced by exactly one lane-specific credential; operator-origin terminal paths carry neither.';


--
-- Name: COLUMN job_completion_commands.accepted_job_status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_completion_commands.accepted_job_status IS 'jobs.status observed while admission held the jobs row lock. Nullable only for legacy commands: a completed late_callback_guard S1 journal row is the sole accepted backfill proof; an unproven NULL fails closed to whole-command supersession rather than guessing from current job state.';


--
-- Name: COLUMN job_completion_commands.status_reorder_enabled; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_completion_commands.status_reorder_enabled IS 'Status-reorder policy captured at fresh admission. False preserves the legacy status-first order; exact retries and resumed finalization use this stored decision rather than the process-global rollout flag.';


--
-- Name: job_completion_sweep_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_completion_sweep_actions (
    job_id uuid NOT NULL,
    attempt bigint NOT NULL,
    command_id uuid NOT NULL,
    command_attempt integer NOT NULL,
    route text NOT NULL,
    source text NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    claimed_by text,
    claimed_at timestamp with time zone,
    claim_expires_at timestamp with time zone,
    result jsonb,
    error_code text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT job_completion_sweep_action_shape CHECK ((((state = 'pending'::text) AND (claimed_by IS NULL) AND (claimed_at IS NULL) AND (claim_expires_at IS NULL) AND (result IS NULL) AND (error_code IS NULL) AND (completed_at IS NULL)) OR ((state = 'claimed'::text) AND (claimed_by IS NOT NULL) AND (btrim(claimed_by) <> ''::text) AND (claimed_at IS NOT NULL) AND (claim_expires_at IS NOT NULL) AND (claim_expires_at > claimed_at) AND (result IS NULL) AND (error_code IS NULL) AND (completed_at IS NULL)) OR ((state = 'done'::text) AND (claimed_by IS NULL) AND (claim_expires_at IS NULL) AND (claimed_at IS NOT NULL) AND (completed_at IS NOT NULL) AND (completed_at >= claimed_at) AND ((result IS NOT NULL) OR (error_code IS NOT NULL))))),
    CONSTRAINT job_completion_sweep_attempt_positive CHECK ((attempt > 0)),
    CONSTRAINT job_completion_sweep_command_attempt_nonnegative CHECK ((command_attempt >= 0)),
    CONSTRAINT job_completion_sweep_error_nonempty CHECK (((error_code IS NULL) OR (btrim(error_code) <> ''::text))),
    CONSTRAINT job_completion_sweep_result_object CHECK (((result IS NULL) OR (jsonb_typeof(result) = 'object'::text))),
    CONSTRAINT job_completion_sweep_route_value CHECK ((route = ANY (ARRAY['resume_finalizer'::text, 'park_alert'::text, 'alert_only'::text]))),
    CONSTRAINT job_completion_sweep_source_nonempty CHECK ((btrim(source) <> ''::text)),
    CONSTRAINT job_completion_sweep_state_value CHECK ((state = ANY (ARRAY['pending'::text, 'claimed'::text, 'done'::text])))
);


--
-- Name: TABLE job_completion_sweep_actions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.job_completion_sweep_actions IS 'Durable class-1 rescue actions for unfinished completion commands. One job-local attempt is allocated under the jobs-row lock; the action claim has a visibility lease, and UNIQUE(command_id, command_attempt) lets a pending action change route without a second reap firing.';


--
-- Name: COLUMN job_completion_sweep_actions.command_attempt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_completion_sweep_actions.command_attempt IS 'Finalizer attempt observed when this reap action was allocated. Together with command_id it deduplicates competing rescuers for that exact attempt.';


--
-- Name: COLUMN job_completion_sweep_actions.route; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_completion_sweep_actions.route IS 'Actionable route from job_completion_sweep_exclusions: resume_finalizer, park_alert, or alert_only. stand_down never creates an action row.';


--
-- Name: COLUMN job_completion_sweep_actions.source; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_completion_sweep_actions.source IS 'Class-1 rescuer that first materialized the action (orphan, job lease, stale-agent, pause redispatch, or registration recovery).';


--
-- Name: COLUMN job_completion_sweep_actions.claim_expires_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_completion_sweep_actions.claim_expires_at IS 'Visibility deadline for the action claimant. An expired claimed row is eligible for takeover; claimed_by alone is never an ownership fence.';


--
-- Name: job_completion_sweep_exclusions; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.job_completion_sweep_exclusions AS
 WITH authoritative AS (
         SELECT command_1.id,
            command_1.job_id,
            command_1.report_seq,
            command_1.client_report_id,
            command_1.payload,
            command_1.payload_digest,
            command_1.reported_at,
            command_1.accepted_lease_token,
            command_1.accepted_agent_id,
            command_1.origin,
            command_1.requested_by,
            command_1.state,
            command_1.attempts,
            command_1.max_attempts,
            command_1.run_after,
            command_1.lease_expires_at,
            command_1.deadline_at,
            command_1.finalizing_by,
            command_1.code_version,
            command_1.outcome,
            command_1.finalized_at,
            command_1.error_code,
            row_number() OVER (PARTITION BY command_1.job_id ORDER BY command_1.report_seq) AS command_order
           FROM public.job_completion_commands command_1
          WHERE (command_1.state = ANY (ARRAY['pending'::text, 'finalizing'::text, 'parked'::text]))
        )
 SELECT command.job_id,
    command.id AS command_id,
    command.report_seq,
    command.state AS command_state,
    command.attempts AS command_attempts,
    command.max_attempts,
    command.run_after,
    command.lease_expires_at,
    command.deadline_at,
        CASE
            WHEN (command.state = 'parked'::text) THEN 'alert_only'::text
            WHEN ((command.state = 'finalizing'::text) AND (command.lease_expires_at > now())) THEN 'stand_down'::text
            WHEN ((command.deadline_at <= now()) OR (command.attempts >= command.max_attempts)) THEN 'park_alert'::text
            ELSE 'resume_finalizer'::text
        END AS route
   FROM authoritative command
  WHERE (command.command_order = 1);


--
-- Name: VIEW job_completion_sweep_exclusions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.job_completion_sweep_exclusions IS 'Single source of truth for class-1 completion rescue routing. One row is the oldest pending, finalizing, or parked command per job: parked alerts only; live finalizer leases stand down; non-live deadline/retry-cap rows park and alert; all others resume from durable effect progress.';


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
-- Name: job_message_routes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_message_routes (
    route_id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    project_id uuid,
    thread_id character varying(64) NOT NULL,
    originating_message_id uuid,
    policy_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    state text NOT NULL,
    blocking boolean DEFAULT false NOT NULL,
    officer_thread_id uuid,
    officer_incarnation integer,
    officer_deadline timestamp with time zone,
    user_delivery_at timestamp with time zone,
    resolved_by_kind text,
    resolved_by_id text,
    resolved_at timestamp with time zone,
    total_deadline timestamp with time zone,
    transitions jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    routing_generation uuid DEFAULT gen_random_uuid() NOT NULL,
    effective_audience text DEFAULT 'legacy_human'::text NOT NULL,
    CONSTRAINT job_message_routes_effective_audience_check CHECK ((effective_audience = ANY (ARRAY['legacy_human'::text, 'human'::text, 'officer'::text, 'officer_and_user'::text, 'explicit_recipient'::text]))),
    CONSTRAINT job_message_routes_policy_is_object CHECK ((jsonb_typeof(policy_snapshot) = 'object'::text)),
    CONSTRAINT job_message_routes_state_check CHECK ((state = ANY (ARRAY['pending_officer'::text, 'pending_both'::text, 'user_direct'::text, 'escalated_to_user'::text, 'resolved_by_officer'::text, 'resolved_by_user'::text, 'timed_out'::text, 'delivery_failed'::text, 'closed'::text]))),
    CONSTRAINT job_message_routes_transitions_is_array CHECK ((jsonb_typeof(transitions) = 'array'::text))
);


--
-- Name: TABLE job_message_routes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.job_message_routes IS 'Control state for officer-aware worker messages (officer_message_routing.md §3). message_log keeps the canonical thread; this ledger carries the per-message policy snapshot, delivery state, officer SLA + total blocking deadlines, and the CAS surface the reply lanes and the leader-gated reconciler resolve through exactly once.';


--
-- Name: COLUMN job_message_routes.thread_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_message_routes.thread_id IS 'Job message thread key (message_log.thread_id shape). One canonical thread — the route never forks the conversation.';


--
-- Name: COLUMN job_message_routes.policy_snapshot; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_message_routes.policy_snapshot IS 'Routing policy frozen at send time. Changing the project setting later never retargets a waiting question.';


--
-- Name: COLUMN job_message_routes.state; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_message_routes.state IS 'pending_officer/pending_both/user_direct -> resolved_by_officer | resolved_by_user | escalated_to_user | timed_out; pre-delivery states may pass through delivery_failed. Any open state -> closed when the job reaches a terminal status (auto-close; see the transitions audit for the stamp). CAS-only transitions.';


--
-- Name: COLUMN job_message_routes.transitions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_message_routes.transitions IS 'Append-only [{at, from, to, actor_kind, actor_id, officer_incarnation, note}] — actor identity on every transition (spec §7).';


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
    execution_lane text DEFAULT 'pinned'::text NOT NULL,
    completion_seq_hwm bigint DEFAULT 0 NOT NULL,
    completion_sweep_attempt_hwm bigint DEFAULT 0 NOT NULL,
    CONSTRAINT jobs_diff_status_check CHECK (((diff_status IS NULL) OR (diff_status = ANY (ARRAY['pending'::text, 'accepted'::text, 'rejected'::text])))),
    CONSTRAINT jobs_runner_kind_check CHECK ((runner_kind = ANY (ARRAY['user'::text, 'lifecycle'::text, 'service'::text]))),
    CONSTRAINT jobs_wake_state_known CHECK ((wake_state = ANY (ARRAY['none'::text, 'pending'::text, 'sending'::text, 'sent'::text, 'dead'::text, 'undeliverable'::text]))),
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

COMMENT ON COLUMN public.jobs.wake_state IS 'Wake outbox state: none|pending|sending|sent|dead|undeliverable. dead is retry exhaustion; undeliverable means the exact creating thread was hard-deleted before the wake could settle. Claimed by an atomic UPDATE ... FOR UPDATE SKIP LOCKED before the non-idempotent send.';


--
-- Name: COLUMN jobs.wake_notified_status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.wake_notified_status IS 'Terminal status last delivered to the creating session. Second half of the (job_id, terminal_status) dedup key — a later, different terminal status (pending_review → completed via approve) is a legitimate second wake.';


--
-- Name: COLUMN jobs.failed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.failed_at IS 'When the job entered ''failed'', set by update_job_status on the transition. Use this, NEVER updated_at, to date a failure: the update_jobs_updated_at trigger fires on FK cascades from gc_offline_agents, which rewrites updated_at to exactly 24h after the assigned agent''s last heartbeat. NULL on rows that failed before migration 0072 — the time is genuinely unknown, not zero. Design: docs/superpowers/specs/2026-07-28-transient-infra-failure-handling-design.md.';


--
-- Name: COLUMN jobs.execution_lane; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.execution_lane IS 'Which execution plane owns this job: ''pinned'' (registered-agent dispatch and jobs-row lease recovery, the default) or ''stateless'' (worker_batch run_queue claim and reaper). App-validated by design; exactly one plane may dispatch or recover a job. See docs/features/stateless_agents.md §5.4.4.';


--
-- Name: COLUMN jobs.completion_seq_hwm; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.completion_seq_hwm IS 'Highest commit-ordered job_completion_commands.report_seq allocated for this job. Admission increments it while holding the jobs row lock; never allocate completion order from an IDENTITY/sequence.';


--
-- Name: COLUMN jobs.completion_sweep_attempt_hwm; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.completion_sweep_attempt_hwm IS 'Highest job_completion_sweep_actions.attempt allocated for this job. Routing increments it while holding the jobs row lock; the resulting (job_id, attempt) pair is the reap-action dedup key.';


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
-- Name: knowledge_materialization_intents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_materialization_intents (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    project_id uuid NOT NULL,
    note_id text NOT NULL,
    content text NOT NULL,
    content_hash text NOT NULL,
    job_id uuid,
    canonical_state text DEFAULT 'pending_sync'::text NOT NULL,
    projection_state text DEFAULT 'pending'::text NOT NULL,
    retry_state text DEFAULT 'retryable'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    attempt_token uuid,
    lease_expires_at timestamp with time zone,
    last_attempted_at timestamp with time zone,
    next_retry_at timestamp with time zone,
    last_error_class text,
    last_error text,
    repo text,
    branch text,
    path text,
    operation text,
    canonical_at timestamp with time zone,
    projected_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT knowledge_materialization_intents_attempts_check CHECK ((attempts >= 0)),
    CONSTRAINT knowledge_materialization_intents_canonical_state_check CHECK ((canonical_state = ANY (ARRAY['pending_sync'::text, 'canonical'::text, 'failed'::text, 'superseded'::text]))),
    CONSTRAINT knowledge_materialization_intents_projection_state_check CHECK ((projection_state = ANY (ARRAY['pending'::text, 'synced'::text, 'projection_only'::text, 'failed'::text]))),
    CONSTRAINT knowledge_materialization_intents_retry_state_check CHECK ((retry_state = ANY (ARRAY['none'::text, 'retryable'::text, 'permanent'::text])))
);


--
-- Name: TABLE knowledge_materialization_intents; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.knowledge_materialization_intents IS 'BP-08 durable canonical-file and projection convergence ledger. The file in the project KB repository is authoritative; pending/failed rows are ineligible for backlog dispatch.';


--
-- Name: legacy_workspace_cutover_plan_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.legacy_workspace_cutover_plan_events (
    plan_id uuid NOT NULL,
    ordinal integer NOT NULL,
    source text NOT NULL,
    source_id text NOT NULL,
    unit text NOT NULL,
    ts timestamp with time zone NOT NULL,
    row_hash text NOT NULL,
    event_payload jsonb NOT NULL,
    CONSTRAINT legacy_workspace_cutover_plan_events_shape_check CHECK (((ordinal = ANY (ARRAY[0, 1])) AND (source = 'orchestrator'::text) AND (source_id <> ''::text) AND (unit = ANY (ARRAY['vcpu-hour'::text, 'gib-hour'::text])) AND (row_hash ~ '^[0-9a-f]{64}$'::text) AND (jsonb_typeof(event_payload) = 'object'::text)))
);


--
-- Name: legacy_workspace_cutover_plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.legacy_workspace_cutover_plans (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    workspace_interval_id bigint NOT NULL,
    cutover_request_id uuid NOT NULL,
    expected_event_count integer DEFAULT 2 NOT NULL,
    payload_schema_version integer DEFAULT 1 NOT NULL,
    hash_algorithm text DEFAULT 'sha256'::text NOT NULL,
    event_set_hash text NOT NULL,
    creator_generation bigint NOT NULL,
    state text DEFAULT 'planned'::text NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_attempt_generation bigint,
    last_attempt_at timestamp with time zone,
    sanitized_error jsonb,
    published_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT legacy_workspace_cutover_plans_shape_check CHECK (((expected_event_count = 2) AND (payload_schema_version = 1) AND (hash_algorithm = 'sha256'::text) AND (event_set_hash ~ '^[0-9a-f]{64}$'::text) AND (creator_generation > 0) AND (state = ANY (ARRAY['planned'::text, 'published'::text, 'conflict'::text])) AND (attempt_count >= 0) AND (((attempt_count = 0) AND (last_attempt_generation IS NULL) AND (last_attempt_at IS NULL)) OR ((attempt_count > 0) AND (last_attempt_generation IS NOT NULL) AND (last_attempt_generation > 0) AND (last_attempt_at IS NOT NULL))) AND ((sanitized_error IS NULL) OR (jsonb_typeof(sanitized_error) = 'object'::text)) AND (((state = 'published'::text) AND (published_at IS NOT NULL)) OR ((state <> 'published'::text) AND (published_at IS NULL)))))
);


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
-- Name: message_delivery_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.message_delivery_attempts (
    attempt_id bigint NOT NULL,
    intent_id uuid NOT NULL,
    attempt_number integer NOT NULL,
    state text DEFAULT 'attempted'::text NOT NULL,
    attempted_at timestamp with time zone DEFAULT now() NOT NULL,
    settled_at timestamp with time zone,
    failure_class text,
    detail text,
    CONSTRAINT message_delivery_attempts_attempt_number_check CHECK ((attempt_number > 0)),
    CONSTRAINT message_delivery_attempts_state_check CHECK ((state = ANY (ARRAY['attempted'::text, 'accepted'::text, 'failed'::text])))
);


--
-- Name: message_delivery_attempts_attempt_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.message_delivery_attempts_attempt_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: message_delivery_attempts_attempt_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.message_delivery_attempts_attempt_id_seq OWNED BY public.message_delivery_attempts.attempt_id;


--
-- Name: message_delivery_intents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.message_delivery_intents (
    intent_id uuid DEFAULT gen_random_uuid() NOT NULL,
    routing_generation uuid NOT NULL,
    route_id uuid,
    job_id uuid,
    project_id uuid,
    user_id uuid,
    bucket text NOT NULL,
    effective_audience text NOT NULL,
    state text DEFAULT 'reserved'::text NOT NULL,
    reserved_at timestamp with time zone DEFAULT now() NOT NULL,
    last_attempted_at timestamp with time zone,
    accepted_at timestamp with time zone,
    last_failed_at timestamp with time zone,
    failure_class text,
    attempt_count integer DEFAULT 0 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT message_delivery_intents_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT message_delivery_intents_bucket_check CHECK ((bucket = ANY (ARRAY['human'::text, 'officer_internal'::text]))),
    CONSTRAINT message_delivery_intents_effective_audience_check CHECK ((effective_audience = ANY (ARRAY['legacy_human'::text, 'human'::text, 'officer'::text, 'officer_and_user'::text, 'explicit_recipient'::text]))),
    CONSTRAINT message_delivery_intents_metadata_check CHECK ((jsonb_typeof(metadata) = 'object'::text)),
    CONSTRAINT message_delivery_intents_state_check CHECK ((state = ANY (ARRAY['reserved'::text, 'attempted'::text, 'accepted'::text, 'failed'::text])))
);


--
-- Name: TABLE message_delivery_intents; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.message_delivery_intents IS 'OC-07 durable quota reservation and effective-audience identity. One row per routing generation and bucket; quota is reserved before any non-idempotent delivery and retries reuse this identity.';


--
-- Name: COLUMN message_delivery_intents.effective_audience; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_delivery_intents.effective_audience IS 'Server-resolved durable audience. Quota meaning is never reconstructed from message_log.direction.';


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
    read_at timestamp with time zone,
    routing_generation uuid DEFAULT gen_random_uuid() NOT NULL,
    effective_audience text DEFAULT 'legacy_human'::text NOT NULL,
    CONSTRAINT message_log_effective_audience_check CHECK ((effective_audience = ANY (ARRAY['legacy_human'::text, 'human'::text, 'officer'::text, 'officer_and_user'::text, 'explicit_recipient'::text])))
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
-- Name: officer_floor_wake_episodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.officer_floor_wake_episodes (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    project_id uuid NOT NULL,
    officer_incarnation uuid NOT NULL,
    pool text NOT NULL,
    dedup_key text NOT NULL,
    wake_event_id bigint,
    state text DEFAULT 'retryable'::text NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_attempted_at timestamp with time zone,
    last_queued_at timestamp with time zone,
    delivered_at timestamp with time zone,
    failure_class text,
    last_error text,
    next_retry_at timestamp with time zone,
    resolved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT officer_floor_wake_episodes_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT officer_floor_wake_episodes_state_check CHECK ((state = ANY (ARRAY['retryable'::text, 'queued'::text, 'delivered'::text, 'permanent_failed'::text, 'superseded'::text])))
);


--
-- Name: TABLE officer_floor_wake_episodes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.officer_floor_wake_episodes IS 'BP-10 durable backlog-floor wake policy outcomes. Policy debounce starts at last_queued_at; transient retry timing is next_retry_at.';


--
-- Name: officer_ticket_claims; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.officer_ticket_claims (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    ticket_note_id text NOT NULL,
    ready_generation_at timestamp with time zone,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL,
    source text NOT NULL,
    officer_thread_id uuid,
    officer_incarnation integer,
    officer_slot text,
    work_category text,
    admission_config_fingerprint text,
    admission_lineage_size integer,
    job_id uuid NOT NULL,
    job_deleted_at timestamp with time zone,
    job_status_at_delete text,
    deletion_actor_user_id uuid,
    deletion_reason text,
    CONSTRAINT officer_ticket_claim_authority_shape CHECK ((((source = 'legacy_unversioned'::text) AND (ready_generation_at IS NULL) AND (officer_incarnation IS NULL) AND (admission_config_fingerprint IS NULL) AND (admission_lineage_size IS NULL)) OR ((source <> 'legacy_unversioned'::text) AND (ready_generation_at IS NOT NULL) AND (officer_thread_id IS NOT NULL) AND (officer_incarnation IS NOT NULL) AND (admission_config_fingerprint IS NOT NULL) AND (admission_lineage_size IS NOT NULL)))),
    CONSTRAINT officer_ticket_claim_fingerprint_valid CHECK (((admission_config_fingerprint IS NULL) OR (admission_config_fingerprint ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT officer_ticket_claim_generation_finite CHECK (((ready_generation_at IS NULL) OR isfinite(ready_generation_at))),
    CONSTRAINT officer_ticket_claim_incarnation_valid CHECK (((officer_incarnation IS NULL) OR (officer_incarnation >= 0))),
    CONSTRAINT officer_ticket_claim_lineage_size_valid CHECK (((admission_lineage_size IS NULL) OR (admission_lineage_size = (officer_incarnation + 1)))),
    CONSTRAINT officer_ticket_claim_source_nonempty CHECK ((btrim(source) <> ''::text)),
    CONSTRAINT officer_ticket_claim_ticket_nonempty CHECK ((btrim(ticket_note_id) <> ''::text))
);


--
-- Name: TABLE officer_ticket_claims; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.officer_ticket_claims IS 'Durable Officer backlog claim ledger. Claim identities survive job/thread deletion; job_deleted_at is audit only and never re-arms a ticket (BP-05).';


--
-- Name: COLUMN officer_ticket_claims.ready_generation_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.officer_ticket_claims.ready_generation_at IS 'The server-resolved Officer ready_at generation consumed by this claim. NULL only for source=legacy_unversioned: claimed_at is then the database cutover barrier and no historical generation is guessed.';


--
-- Name: COLUMN officer_ticket_claims.claimed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.officer_ticket_claims.claimed_at IS 'Claim time. For source=legacy_unversioned this is the server cutover timestamp and the ticket must be explicitly re-readied strictly later.';


--
-- Name: COLUMN officer_ticket_claims.job_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.officer_ticket_claims.job_id IS 'Durable job identity without a jobs FK so physical deletion cannot erase or null claim history.';


--
-- Name: COLUMN officer_ticket_claims.job_status_at_delete; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.officer_ticket_claims.job_status_at_delete IS 'Status observed under the jobs row lock immediately before deletion. A non-terminal value remains a later-generation admission blocker.';


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
-- Name: project_officers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_officers (
    project_id uuid NOT NULL,
    thread_id uuid,
    config_override jsonb DEFAULT '{}'::jsonb NOT NULL,
    communication_policy jsonb DEFAULT '{"worker_messages": "officer_first", "officer_response_minutes": 15}'::jsonb NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    incarnations jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_officer_communication_is_object CHECK ((jsonb_typeof(communication_policy) = 'object'::text)),
    CONSTRAINT project_officer_config_is_object CHECK ((jsonb_typeof(config_override) = 'object'::text)),
    CONSTRAINT project_officer_incarnations_is_array CHECK ((jsonb_typeof(incarnations) = 'array'::text)),
    CONSTRAINT project_officer_state_is_object CHECK ((jsonb_typeof(state) = 'object'::text))
);


--
-- Name: TABLE project_officers; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.project_officers IS 'The officer''s durable post — one row per project, always present. thread_id IS NULL = vacant; commission links a thread, decommission harvests its state back onto the row (officer_post.md §2). The thread stays the runtime projection; this row is the durable record.';


--
-- Name: COLUMN project_officers.thread_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_officers.thread_id IS 'Current incarnation (threads.id), NULL while the post is vacant. Deliberately not unique so a future legion can share one commander.';


--
-- Name: COLUMN project_officers.communication_policy; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_officers.communication_policy IS 'Legate-owned worker-message routing policy (officer_message_routing). Row-only: resolved server-side per message, never mirrored into thread metadata, not writable by the officer runtime. Defaults to officer_first since 0163; effective policy is still user_direct while the post is vacant.';


--
-- Name: COLUMN project_officers.state; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_officers.state IS 'Harvested metadata.officer_state from the last decommission (digest ring, page counters, sitrep fingerprints) plus the while-vacant ledger. Empty while commissioned — the live copy stays on the thread.';


--
-- Name: COLUMN project_officers.incarnations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_officers.incarnations IS 'Append-only history: [{thread_id, commissioned_at, decommissioned_at, reason}]. The index into old officer threads, which remain readable as ended sessions.';


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
    CONSTRAINT valid_project_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'archived'::character varying])::text[])))
);


--
-- Name: COLUMN projects.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.projects.status IS 'Project lifecycle: active|archived, enforced by valid_project_status since 0169. Archiving is an explicit owner action and hides the project from the default GET /api/projects list (?status= opts it back in); deletion is a hard row delete, never a status. The column remains NULLABLE on purpose — a CHECK passes on NULL, and the API fails toward showing an unclassifiable row rather than hiding it.';


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
    compute_scope_epoch_id uuid,
    CONSTRAINT resource_intervals_attribution_check CHECK ((((attribution_scope = 'customer'::text) AND (owner_kind IS NOT NULL) AND (owner_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (owner_id IS NOT NULL) AND (owner_id <> ''::text) AND (user_id IS NOT NULL) AND (attribution_quality = ANY (ARRAY['exact'::text, 'derived'::text]))) OR ((attribution_scope = 'shared-platform'::text) AND (user_id IS NULL) AND (project_id IS NULL) AND (attribution_quality = ANY (ARRAY['exact'::text, 'derived'::text]))) OR ((attribution_scope = 'unknown'::text) AND (user_id IS NULL) AND (project_id IS NULL) AND (attribution_quality = ANY (ARRAY['ambiguous'::text, 'unknown'::text]))))),
    CONSTRAINT resource_intervals_capacity_check CHECK (((revision_no > 0) AND ((cpu_millicores IS NULL) OR (cpu_millicores >= 0)) AND ((memory_bytes IS NULL) OR (memory_bytes >= 0)) AND ((storage_bytes IS NULL) OR (storage_bytes >= 0)) AND (((category = 'compute'::text) AND (cpu_millicores IS NOT NULL) AND (memory_bytes IS NOT NULL) AND (storage_bytes IS NULL)) OR ((category = 'storage'::text) AND (storage_bytes IS NOT NULL) AND (cpu_millicores IS NULL) AND (memory_bytes IS NULL))))),
    CONSTRAINT resource_intervals_compute_scope_epoch_shape_check CHECK ((((((source_kind = 'pod'::text) AND (category = 'compute'::text) AND (resource = 'agent_pod'::text)) OR ((source_kind = 'pod'::text) AND (category = 'compute'::text) AND (resource = 'workspace_pod'::text) AND (COALESCE((details ->> 'product_class'::text), ''::text) = 'ide-session'::text)) OR ((source_kind = 'vmi'::text) AND (category = 'compute'::text) AND (resource = 'workspace_vm'::text))) AND (compute_scope_epoch_id IS NOT NULL)) OR ((NOT (((source_kind = 'pod'::text) AND (category = 'compute'::text) AND (resource = 'agent_pod'::text)) OR ((source_kind = 'pod'::text) AND (category = 'compute'::text) AND (resource = 'workspace_pod'::text) AND (COALESCE((details ->> 'product_class'::text), ''::text) = 'ide-session'::text)) OR ((source_kind = 'vmi'::text) AND (category = 'compute'::text) AND (resource = 'workspace_vm'::text)))) AND (compute_scope_epoch_id IS NULL)))),
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
-- Name: COLUMN resource_intervals.compute_scope_epoch_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.resource_intervals.compute_scope_epoch_id IS 'Exact promoted Slice 3 inventory epoch authorizing this immutable compute interval revision; NULL for every other resource class.';


--
-- Name: CONSTRAINT resource_intervals_compute_scope_epoch_shape_check ON resource_intervals; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON CONSTRAINT resource_intervals_compute_scope_epoch_shape_check ON public.resource_intervals IS 'Requires exact promoted compute epoch binding only for agent Pods, IDE Pods, and workspace VMIs; NULL product classes cannot bypass the shape.';


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
-- Name: resource_inventory_ingest_tickets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_inventory_ingest_tickets (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    nonce_hash text NOT NULL,
    scope_epoch_id uuid NOT NULL,
    leader_generation bigint NOT NULL,
    request_digest text NOT NULL,
    max_snapshot_items integer NOT NULL,
    max_snapshot_bytes bigint NOT NULL,
    staged_bytes bigint DEFAULT 0 NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    bound_snapshot_id uuid,
    bound_at timestamp with time zone,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_inventory_ingest_tickets_generation_check CHECK (((leader_generation > 0) AND (max_snapshot_items > 0) AND (max_snapshot_bytes > 0) AND (staged_bytes >= 0) AND (staged_bytes <= max_snapshot_bytes))),
    CONSTRAINT resource_inventory_ingest_tickets_hash_check CHECK (((nonce_hash ~ '^[0-9a-f]{64}$'::text) AND (request_digest ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT resource_inventory_ingest_tickets_state_check CHECK ((((bound_snapshot_id IS NULL) AND (bound_at IS NULL) AND (consumed_at IS NULL)) OR ((bound_snapshot_id IS NOT NULL) AND (bound_at IS NOT NULL)))),
    CONSTRAINT resource_inventory_ingest_tickets_time_check CHECK (((expires_at > created_at) AND ((bound_at IS NULL) OR (bound_at >= created_at)) AND ((consumed_at IS NULL) OR (consumed_at >= bound_at)) AND ((consumed_at IS NULL) OR (consumed_at <= expires_at))))
);


--
-- Name: TABLE resource_inventory_ingest_tickets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_inventory_ingest_tickets IS 'Hashed one-time collector tickets bound to one scope epoch, generation, request digest, and snapshot.';


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
    recovery_from_epoch_id uuid,
    require_after_recovery boolean DEFAULT false NOT NULL,
    CONSTRAINT resource_inventory_scope_epochs_health_check CHECK (((coverage_mode <> ''::text) AND (snapshot_health <> ''::text) AND (continuity_health <> ''::text) AND (item_health <> ''::text) AND (backend_health <> ''::text) AND (publication_health <> ''::text) AND (leader_generation >= 0) AND (consecutive_failures >= 0) AND ((last_sequence IS NULL) OR (last_sequence >= 0)) AND ((last_item_count IS NULL) OR (last_item_count >= 0)) AND ((sanitized_error IS NULL) OR (jsonb_typeof(sanitized_error) = 'object'::text)))),
    CONSTRAINT resource_inventory_scope_epochs_number_check CHECK ((epoch_number > 0)),
    CONSTRAINT resource_inventory_scope_epochs_recovery_shape_check CHECK ((((recovery_from_epoch_id IS NULL) AND (NOT require_after_recovery)) OR ((recovery_from_epoch_id IS NOT NULL) AND (recovery_from_epoch_id <> id)))),
    CONSTRAINT resource_inventory_scope_epochs_requirement_check CHECK (((required_for_rollup AND (required_from IS NOT NULL) AND (reliable_from IS NOT NULL) AND (required_from >= reliable_from)) OR ((NOT required_for_rollup) AND (required_from IS NULL)))),
    CONSTRAINT resource_inventory_scope_epochs_retirement_check CHECK (((retired_at IS NULL) OR (((reliable_from IS NULL) OR (retired_at >= reliable_from)) AND ((continuous_since IS NULL) OR (retired_at >= continuous_since)) AND ((complete_through IS NULL) OR (retired_at >= complete_through)))))
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
-- Name: resource_inventory_shadow_comparisons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_inventory_shadow_comparisons (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    snapshot_id uuid NOT NULL,
    inventory_scope_id uuid NOT NULL,
    source_uid text NOT NULL,
    owner_kind text,
    owner_id uuid,
    owner_trusted boolean DEFAULT false NOT NULL,
    legacy_interval_id bigint,
    legacy_cpu_millicores bigint,
    legacy_memory_bytes bigint,
    legacy_started_at timestamp with time zone,
    observed_cpu_millicores bigint,
    observed_memory_bytes bigint,
    observed_started_at timestamp with time zone,
    observed_start_time_source text,
    observed_start_uncertainty_us bigint,
    start_delta_us bigint,
    status text NOT NULL,
    reason_code text NOT NULL,
    explained boolean DEFAULT false NOT NULL,
    comparison_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_inventory_shadow_comparisons_capacity_check CHECK (((((legacy_interval_id IS NULL) AND (legacy_cpu_millicores IS NULL) AND (legacy_memory_bytes IS NULL)) OR ((legacy_interval_id IS NOT NULL) AND (legacy_cpu_millicores IS NOT NULL) AND (legacy_memory_bytes IS NOT NULL) AND (legacy_cpu_millicores >= 0) AND (legacy_memory_bytes >= 0))) AND ((observed_cpu_millicores IS NULL) OR (observed_cpu_millicores >= 0)) AND ((observed_memory_bytes IS NULL) OR (observed_memory_bytes >= 0)))),
    CONSTRAINT resource_inventory_shadow_comparisons_lifetime_check CHECK (((((observed_started_at IS NULL) AND (observed_start_time_source IS NULL) AND (observed_start_uncertainty_us IS NULL)) OR ((observed_started_at IS NOT NULL) AND (observed_start_time_source IS NOT NULL) AND (observed_start_time_source <> ''::text) AND (observed_start_uncertainty_us IS NOT NULL) AND (observed_start_uncertainty_us >= 0))) AND ((start_delta_us IS NULL) OR ((legacy_started_at IS NOT NULL) AND (observed_started_at IS NOT NULL) AND (start_delta_us = ((EXTRACT(epoch FROM (observed_started_at - legacy_started_at)) * (1000000)::numeric))::bigint))))),
    CONSTRAINT resource_inventory_shadow_comparisons_owner_check CHECK (((owner_trusted AND (owner_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (owner_id IS NOT NULL)) OR ((NOT owner_trusted) AND (owner_kind IS NULL) AND (owner_id IS NULL)))),
    CONSTRAINT resource_inventory_shadow_comparisons_status_check CHECK (((status = ANY (ARRAY['matched'::text, 'capacity-mismatch'::text, 'owner-mismatch'::text, 'legacy-missing'::text, 'invalid-observation'::text, 'not-applicable'::text, 'lifetime-mismatch'::text])) AND (reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text) AND ((status <> 'matched'::text) OR (start_delta_us IS NULL) OR (start_delta_us = 0)) AND ((status <> 'lifetime-mismatch'::text) OR (((NOT explained) AND (reason_code = ANY (ARRAY['start-semantics'::text, 'start-evidence-missing'::text])) AND ((start_delta_us IS NULL) OR (start_delta_us <> 0))) OR (explained AND (reason_code = 'bounded-start-semantics'::text) AND (start_delta_us > 0) AND (observed_start_time_source = 'app-db-received'::text) AND (observed_start_uncertainty_us IS NOT NULL) AND (start_delta_us <= observed_start_uncertainty_us)))))),
    CONSTRAINT resource_inventory_shadow_comparisons_uid_check CHECK ((source_uid <> ''::text))
);


--
-- Name: TABLE resource_inventory_shadow_comparisons; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_inventory_shadow_comparisons IS 'Per-snapshot workspace shadow comparisons, immutable until the manifest and diagnostic horizons expire; reason_code contains no free-form customer data.';


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
    ingest_ticket_id uuid,
    reconciliation_summary jsonb,
    CONSTRAINT resource_inventory_snapshots_manifest_state_check CHECK ((((manifest_state = 'staging'::text) AND (sealed_at IS NULL) AND (items_expired_at IS NULL)) OR ((manifest_state = 'sealed'::text) AND (sealed_at IS NOT NULL) AND (items_expired_at IS NULL)) OR ((manifest_state = 'items-expired'::text) AND (sealed_at IS NOT NULL) AND (items_expired_at IS NOT NULL) AND (items_expired_at >= sealed_at)) OR ((manifest_state = 'staging-expired'::text) AND (NOT complete) AND (sealed_at IS NULL) AND (items_expired_at IS NOT NULL) AND (items_expired_at >= created_at)))),
    CONSTRAINT resource_inventory_snapshots_reconciliation_summary_check CHECK ((((ingest_ticket_id IS NULL) AND (reconciliation_summary IS NULL)) OR ((manifest_state = ANY (ARRAY['staging'::text, 'staging-expired'::text])) AND (reconciliation_summary IS NULL)) OR ((manifest_state = ANY (ARRAY['sealed'::text, 'items-expired'::text])) AND (jsonb_typeof(reconciliation_summary) = 'object'::text)))),
    CONSTRAINT resource_inventory_snapshots_shape_check CHECK (((leader_generation >= 0) AND (item_count >= 0) AND ((sequence IS NULL) OR (sequence >= 0)) AND ((item_digest IS NULL) OR (item_digest ~ '^[0-9a-f]{64}$'::text)) AND (jsonb_typeof(fatal_errors) = 'array'::text) AND (jsonb_typeof(item_errors) = 'array'::text) AND ((NOT complete) OR (jsonb_array_length(fatal_errors) = 0)) AND ((manifest_state <> 'sealed'::text) OR (NOT complete) OR (item_digest IS NOT NULL)))),
    CONSTRAINT resource_inventory_snapshots_time_check CHECK (((collection_completed_at >= collection_started_at) AND ((sealed_at IS NULL) OR (sealed_at >= received_at))))
);


--
-- Name: resource_inventory_transport_nonces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_inventory_transport_nonces (
    collector_id text NOT NULL,
    request_nonce uuid NOT NULL,
    request_kind text NOT NULL,
    request_digest text NOT NULL,
    scope_epoch_id uuid NOT NULL,
    leader_generation bigint NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT resource_inventory_transport_nonces_identity_check CHECK (((collector_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'::text) AND (request_kind ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text) AND (request_digest ~ '^[0-9a-f]{64}$'::text) AND (leader_generation > 0))),
    CONSTRAINT resource_inventory_transport_nonces_time_check CHECK ((expires_at > received_at))
);


--
-- Name: TABLE resource_inventory_transport_nonces; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_inventory_transport_nonces IS 'Immutable HMAC request nonce claims retained through a replay window; expired rows are removed in bounded batches.';


--
-- Name: resource_inventory_watch_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_inventory_watch_events (
    watch_session_id uuid NOT NULL,
    id uuid NOT NULL,
    scope_epoch_id uuid NOT NULL,
    ordinal integer NOT NULL,
    request_digest text NOT NULL,
    event_type text NOT NULL,
    expected_resource_version text NOT NULL,
    resource_version text,
    source_kind text,
    source_uid text,
    revision_hash text,
    normalized_item jsonb,
    valid_for_metering boolean,
    item_error jsonb,
    mutation_action text NOT NULL,
    affected_interval_id uuid,
    coverage_gap_id uuid,
    event_bytes bigint NOT NULL,
    collector_observed_at timestamp with time zone,
    received_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_inventory_watch_events_common_check CHECK (((ordinal > 0) AND ((event_bytes > 0) OR ((event_type = 'history-lost'::text) AND (event_bytes = 0))) AND (expected_resource_version <> ''::text) AND (expected_resource_version <> '0'::text) AND ((resource_version IS NULL) OR ((resource_version <> ''::text) AND (resource_version <> '0'::text))) AND ((revision_hash IS NULL) OR (revision_hash ~ '^[0-9a-f]{64}$'::text)) AND ((normalized_item IS NULL) OR (jsonb_typeof(normalized_item) = 'object'::text)) AND ((item_error IS NULL) OR (jsonb_typeof(item_error) = 'object'::text)))),
    CONSTRAINT resource_inventory_watch_events_digest_check CHECK ((request_digest ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT resource_inventory_watch_events_interval_action_check CHECK ((((mutation_action = ANY (ARRAY['confirm'::text, 'open'::text, 'revise'::text, 'close'::text])) AND (affected_interval_id IS NOT NULL)) OR (mutation_action = 'presence-invalid'::text) OR (mutation_action = 'not-applicable'::text) OR ((mutation_action = ANY (ARRAY['already-absent'::text, 'bookmark'::text, 'history-gap'::text])) AND (affected_interval_id IS NULL)))),
    CONSTRAINT resource_inventory_watch_events_shape_check CHECK ((((event_type = ANY (ARRAY['added'::text, 'modified'::text])) AND (resource_version IS NOT NULL) AND (source_kind = ANY (ARRAY['pod'::text, 'vmi'::text, 'pvc'::text, 'volume'::text])) AND (source_uid IS NOT NULL) AND (source_uid <> ''::text) AND (normalized_item IS NOT NULL) AND (valid_for_metering IS NOT NULL) AND ((valid_for_metering AND (revision_hash IS NOT NULL) AND (item_error IS NULL) AND (mutation_action = ANY (ARRAY['confirm'::text, 'open'::text, 'revise'::text, 'not-applicable'::text, 'close'::text, 'already-absent'::text]))) OR ((NOT valid_for_metering) AND (item_error IS NOT NULL) AND (mutation_action = ANY (ARRAY['presence-invalid'::text, 'close'::text, 'already-absent'::text])))) AND (coverage_gap_id IS NULL)) OR ((event_type = 'deleted'::text) AND (resource_version IS NOT NULL) AND (source_kind = ANY (ARRAY['pod'::text, 'vmi'::text, 'pvc'::text, 'volume'::text])) AND (source_uid IS NOT NULL) AND (source_uid <> ''::text) AND (revision_hash IS NULL) AND (normalized_item IS NULL) AND (valid_for_metering IS NULL) AND (item_error IS NULL) AND (mutation_action = ANY (ARRAY['close'::text, 'already-absent'::text])) AND (coverage_gap_id IS NULL)) OR ((event_type = 'bookmark'::text) AND (resource_version IS NOT NULL) AND (source_kind IS NULL) AND (source_uid IS NULL) AND (revision_hash IS NULL) AND (normalized_item IS NULL) AND (valid_for_metering IS NULL) AND (item_error IS NULL) AND (mutation_action = 'bookmark'::text) AND (affected_interval_id IS NULL) AND (coverage_gap_id IS NULL)) OR ((event_type = 'history-lost'::text) AND (resource_version IS NULL) AND (source_kind IS NULL) AND (source_uid IS NULL) AND (revision_hash IS NULL) AND (normalized_item IS NULL) AND (valid_for_metering IS NULL) AND (item_error IS NULL) AND (mutation_action = 'history-gap'::text) AND (affected_interval_id IS NULL) AND (coverage_gap_id IS NOT NULL))))
);


--
-- Name: TABLE resource_inventory_watch_events; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_inventory_watch_events IS 'One-event receipts coupling a UID mutation, BOOKMARK, or history gap to one opaque cursor CAS; immutable until their terminal session passes retention.';


--
-- Name: resource_inventory_watch_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_inventory_watch_sessions (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    nonce_hash text NOT NULL,
    scope_epoch_id uuid NOT NULL,
    leader_generation bigint NOT NULL,
    request_digest text NOT NULL,
    starting_resource_version text NOT NULL,
    last_resource_version text NOT NULL,
    max_events integer NOT NULL,
    max_bytes bigint NOT NULL,
    committed_events integer DEFAULT 0 NOT NULL,
    committed_bytes bigint DEFAULT 0 NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    termination_reason text,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_inventory_watch_sessions_bounds_check CHECK (((leader_generation > 0) AND (max_events > 0) AND (max_bytes > 0) AND (committed_events >= 0) AND (committed_events <= max_events) AND (committed_bytes >= 0) AND (committed_bytes <= max_bytes))),
    CONSTRAINT resource_inventory_watch_sessions_cursor_check CHECK (((starting_resource_version <> ''::text) AND (starting_resource_version <> '0'::text) AND (last_resource_version <> ''::text) AND (last_resource_version <> '0'::text))),
    CONSTRAINT resource_inventory_watch_sessions_hash_check CHECK (((nonce_hash ~ '^[0-9a-f]{64}$'::text) AND (request_digest ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT resource_inventory_watch_sessions_state_check CHECK ((((consumed_at IS NULL) AND (termination_reason IS NULL)) OR ((consumed_at IS NOT NULL) AND (termination_reason = ANY (ARRAY['completed'::text, 'limit-reached'::text, 'history-lost'::text]))))),
    CONSTRAINT resource_inventory_watch_sessions_time_check CHECK (((expires_at > created_at) AND ((consumed_at IS NULL) OR (consumed_at >= created_at)) AND ((consumed_at IS NULL) OR (consumed_at <= expires_at))))
);


--
-- Name: TABLE resource_inventory_watch_sessions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_inventory_watch_sessions IS 'Hashed bounded WATCH grants bound to one scope epoch, leader generation, starting cursor, event count, bytes, and expiry; terminal diagnostics may be pruned child-first after the hard floor.';


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
-- Name: run_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.run_queue (
    unit_id uuid NOT NULL,
    unit_kind text NOT NULL,
    dedup_key text,
    state text DEFAULT 'queued'::text NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    fair_key text,
    run_after timestamp with time zone DEFAULT now() NOT NULL,
    attempts_since_completion integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 5 NOT NULL,
    lease_token bigint DEFAULT 0 NOT NULL,
    leased_by text,
    leased_until timestamp with time zone,
    input_seq bigint,
    consumed_seq bigint,
    queued_at timestamp with time zone DEFAULT now() NOT NULL,
    enqueue_ord bigint NOT NULL,
    last_leased_by text,
    control_input_seq bigint DEFAULT 0 NOT NULL,
    control_consumed_seq bigint DEFAULT 0 NOT NULL,
    interrupt_admission_lease_token bigint,
    interrupt_admission_turn_id integer,
    CONSTRAINT run_queue_interrupt_admission_shape CHECK ((((interrupt_admission_lease_token IS NULL) AND (interrupt_admission_turn_id IS NULL)) OR ((interrupt_admission_lease_token IS NOT NULL) AND (interrupt_admission_turn_id IS NOT NULL) AND (unit_kind = 'session_turn'::text) AND (state = 'leased'::text) AND (interrupt_admission_lease_token = lease_token) AND (interrupt_admission_lease_token > 0) AND (interrupt_admission_turn_id > 0))))
);


--
-- Name: TABLE run_queue; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.run_queue IS 'Work queue + recorded lease for stateless execution (docs/features/stateless_agents.md §5.1/§5.2). One DURABLE row per unit (thread, job, or bg task): rows are never deleted while the unit lives, so lease_token stays monotonic — delete-and-reinsert would reset it and break fencing. State machine: queued -> leased -> {done | queued | parked}; states are app-validated by design (no CHECK), values: queued, leased, done, parked. All writes go through src/shared/run_queue/.';


--
-- Name: COLUMN run_queue.unit_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.unit_id IS 'Polymorphic unit id: thread_id (session_turn), job_id (worker_batch), or a fresh task id (bg_task). Deliberately NO foreign key — the queue outlives and predates its referents across kinds.';


--
-- Name: COLUMN run_queue.unit_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.unit_kind IS 'session_turn | worker_batch | bg_task (app-validated). Claims filter on kind so bg work never starves interactive claims.';


--
-- Name: COLUMN run_queue.dedup_key; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.dedup_key IS 'Collapse key for collapsible bg work (e.g. cloud_push:<thread>). Dedup is QUEUED-ONLY (partial unique index below): one pending and one running instance may coexist; a signal arriving mid-run must produce a new pending row, never be swallowed (§5.1).';


--
-- Name: COLUMN run_queue.fair_key; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.fair_key IS 'Per-user fairness dimension for session_turn claims (user id). The column ships with S1; the per-key round-robin CTE follows (§5.3.7).';


--
-- Name: COLUMN run_queue.run_after; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.run_after IS 'Not claimable before this instant: scheduling + retry backoff. Reset by completion; pushed out by error release and reaper steals.';


--
-- Name: COLUMN run_queue.attempts_since_completion; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.attempts_since_completion IS 'Claims since the last successful completion (incremented at claim, not at release). Reset to 0 only by complete_unit and unpark_unit. The reaper parks the unit when it reaches max_attempts.';


--
-- Name: COLUMN run_queue.lease_token; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.lease_token IS 'Kleppmann fencing token: MONOTONIC per unit, bumped on EVERY claim and EVERY reaper steal, NEVER reset by enqueue/complete/release. Every persist transaction fences on it (fence_lease, FOR SHARE); a zombie writer holding a stale token is rejected at persist time.';


--
-- Name: COLUMN run_queue.leased_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.leased_by IS 'Pod name. Diagnostics only — never correctness; ownership is proven by lease_token alone.';


--
-- Name: COLUMN run_queue.input_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.input_seq IS 'Newest thread_messages.seq enqueued for this unit (input watermark). Input arriving during a leased turn bumps ONLY this column — flipping a leased row''s state would break the lease.';


--
-- Name: COLUMN run_queue.consumed_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.consumed_seq IS 'Newest seq a COMPLETED turn has answered (consumed watermark). Completion re-queues when input_seq is ahead; every claim compares the two watermarks BEFORE invoking the LLM (skip-if-answered) so a steal landing between final persist and completion cannot double-answer.';


--
-- Name: COLUMN run_queue.queued_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.queued_at IS 'Fairness position: reset on completion, voluntary release and steal, so claim order (priority DESC, queued_at) round-robins within a priority class instead of letting the oldest unit win every cycle.';


--
-- Name: COLUMN run_queue.enqueue_ord; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.enqueue_ord IS 'Insertion-order tiebreak: final ORDER BY key of the claim so equal-timestamp claims are deterministic FIFO. Never reused, never meaningful beyond ordering.';


--
-- Name: COLUMN run_queue.last_leased_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.last_leased_by IS 'Pod that most recently claimed this unit (set by the claim, never cleared on completion — that is the point). Feeds the affinity grace in the general claim: for affinity_grace_seconds after a unit is queued, only this pod (or any pod, once the grace lapses) may claim it, so the holder of the warm in-process session wins its own re-claims instead of racing cold pods. Soft optimization ONLY — correctness never depends on it, and the grace is bounded so a dead holder delays a unit by at most that window. Cleared by the reaper on a steal.';


--
-- Name: COLUMN run_queue.control_input_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.control_input_seq IS 'Newest control request_seq admitted for this stateless unit. Admission advances it without disturbing a live lease; completion requeues while it is ahead of control_consumed_seq.';


--
-- Name: COLUMN run_queue.control_consumed_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.control_consumed_seq IS 'Newest contiguous control request_seq whose owner-written journal result is durable and whose request row has been terminalized under the current lease fence.';


--
-- Name: COLUMN run_queue.interrupt_admission_lease_token; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.interrupt_admission_lease_token IS 'NULL closes stateless interrupt admission. While open, this is the exact current session_turn lease token and never transfers to a successor. Admission and closure serialize on the run_queue row.';


--
-- Name: COLUMN run_queue.interrupt_admission_turn_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_queue.interrupt_admission_turn_id IS 'Concrete active turn accepted by the exact interrupt lease window. NULL means closed. This is deliberately not a queue watermark: an interrupt for one turn must never wake or cancel a later turn.';


--
-- Name: run_queue_enqueue_ord_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.run_queue ALTER COLUMN enqueue_ord ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.run_queue_enqueue_ord_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: runtime_actor_access_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.runtime_actor_access_tokens (
    token_hash bytea NOT NULL,
    grant_id uuid NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone
);


--
-- Name: TABLE runtime_actor_access_tokens; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.runtime_actor_access_tokens IS 'Short-lived opaque access credentials for runtime actor authorization.';


--
-- Name: runtime_actor_bootstraps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.runtime_actor_bootstraps (
    token_hash bytea NOT NULL,
    thread_id uuid,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone
);


--
-- Name: TABLE runtime_actor_bootstraps; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.runtime_actor_bootstraps IS 'Short-lived per-pod bootstrap credentials used only by dedicated session registration; never shared with stateless workers.';


--
-- Name: COLUMN runtime_actor_bootstraps.thread_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runtime_actor_bootstraps.thread_id IS 'Session this bootstrap may be exchanged for. NULL means pod-scoped: the holder is a warm pool agent and the thread is resolved at attach time from the durable agents.thread_id binding, never from the caller.';


--
-- Name: runtime_actor_grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.runtime_actor_grants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    refresh_token_hash bytea NOT NULL,
    caller_kind text NOT NULL,
    user_id uuid,
    project_id uuid,
    project_role text,
    thread_id uuid,
    officer_incarnation integer,
    refresh_expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_refreshed_at timestamp with time zone,
    agent_id uuid,
    credential_generation bigint DEFAULT 1 NOT NULL,
    previous_refresh_token_hash bytea,
    previous_refresh_valid_until timestamp with time zone,
    refresh_handoff_ciphertext text,
    refresh_handoff_acknowledged_at timestamp with time zone,
    last_maintenance_at timestamp with time zone,
    refresh_rotation_required boolean DEFAULT false NOT NULL,
    CONSTRAINT runtime_actor_grants_generation_check CHECK ((credential_generation > 0)),
    CONSTRAINT runtime_actor_grants_incarnation_check CHECK (((officer_incarnation IS NULL) OR (officer_incarnation >= 0))),
    CONSTRAINT runtime_actor_grants_kind_check CHECK ((caller_kind = ANY (ARRAY['worker'::text, 'human'::text, 'conference'::text, 'officer'::text]))),
    CONSTRAINT runtime_actor_grants_officer_shape_check CHECK (((caller_kind <> 'officer'::text) OR ((project_id IS NOT NULL) AND (thread_id IS NOT NULL) AND (officer_incarnation IS NOT NULL)))),
    CONSTRAINT runtime_actor_grants_previous_refresh_shape_check CHECK ((((previous_refresh_token_hash IS NULL) = (previous_refresh_valid_until IS NULL)) AND ((previous_refresh_token_hash IS NULL) = (refresh_handoff_ciphertext IS NULL)) AND ((refresh_handoff_acknowledged_at IS NULL) OR (previous_refresh_token_hash IS NOT NULL)))),
    CONSTRAINT runtime_actor_grants_role_check CHECK (((project_role IS NULL) OR (project_role = ANY (ARRAY['admin'::text, 'owner'::text, 'editor'::text, 'viewer'::text]))))
);


--
-- Name: TABLE runtime_actor_grants; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.runtime_actor_grants IS 'Server-derived runtime identities. Opaque refresh credentials are hashed; every authorization re-checks current post/membership state.';


--
-- Name: COLUMN runtime_actor_grants.agent_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runtime_actor_grants.agent_id IS 'Immutable authoritative persistent-agent UUID snapshot for Officer grants. It intentionally has no agents FK so revoked-grant audit provenance survives agent deletion. NULL is only a pre-0171 grant awaiting unambiguous current-incarnation adoption.';


--
-- Name: COLUMN runtime_actor_grants.credential_generation; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runtime_actor_grants.credential_generation IS 'Server-owned refresh rotation generation. It is not exposed in model schemas or audit payloads.';


--
-- Name: COLUMN runtime_actor_grants.previous_refresh_token_hash; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runtime_actor_grants.previous_refresh_token_hash IS 'Predecessor refresh digest used only to re-deliver one encrypted, unacknowledged rotation or during its bounded acknowledged overlap.';


--
-- Name: COLUMN runtime_actor_grants.refresh_handoff_ciphertext; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runtime_actor_grants.refresh_handoff_ciphertext IS 'APP_ENCRYPTION_KEY-protected current refresh bearer retained only for idempotent ambiguous-response recovery; plaintext is never persisted.';


--
-- Name: COLUMN runtime_actor_grants.refresh_handoff_acknowledged_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runtime_actor_grants.refresh_handoff_acknowledged_at IS 'First presentation of the rotated current bearer. This acknowledgement starts the bounded predecessor overlap.';


--
-- Name: COLUMN runtime_actor_grants.last_maintenance_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runtime_actor_grants.last_maintenance_at IS 'Last server watchdog or credential-bearing maintenance of this grant.';


--
-- Name: COLUMN runtime_actor_grants.refresh_rotation_required; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runtime_actor_grants.refresh_rotation_required IS 'Server-only handoff fence set when the watchdog recovers an expired current Officer grant. The next refresh rotates the bearer and clears it.';


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
-- Name: storage_asset_coverage_gaps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_asset_coverage_gaps (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    asset_id uuid NOT NULL,
    scope_epoch_id uuid NOT NULL,
    gap_start timestamp with time zone NOT NULL,
    gap_end timestamp with time zone,
    reason_code text NOT NULL,
    resolution text DEFAULT 'unresolved'::text NOT NULL,
    resolution_assertion_id uuid,
    resolved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT storage_asset_coverage_gaps_range_check CHECK (((gap_end IS NULL) OR (gap_end >= gap_start))),
    CONSTRAINT storage_asset_coverage_gaps_reason_check CHECK ((reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text)),
    CONSTRAINT storage_asset_coverage_gaps_resolution_check CHECK ((((resolution = 'unresolved'::text) AND (gap_end IS NULL) AND (resolution_assertion_id IS NULL) AND (resolved_at IS NULL)) OR ((resolution = 'reobserved'::text) AND (gap_end IS NOT NULL) AND (resolution_assertion_id IS NULL) AND (resolved_at IS NOT NULL)) OR ((resolution = 'destroyed-confirmed'::text) AND (gap_end IS NOT NULL) AND (resolution_assertion_id IS NOT NULL) AND (resolved_at IS NOT NULL))))
);


--
-- Name: TABLE storage_asset_coverage_gaps; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.storage_asset_coverage_gaps IS 'Per-asset backend-unverified ranges, separate from collector-wide coverage gaps.';


--
-- Name: storage_backend_assertions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_backend_assertions (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    idempotency_key uuid NOT NULL,
    asset_id uuid NOT NULL,
    assertion_kind text DEFAULT 'backend-destroyed'::text NOT NULL,
    request_hash text NOT NULL,
    effective_at timestamp with time zone NOT NULL,
    evidence_kind text NOT NULL,
    evidence_digest text NOT NULL,
    actor_kind text NOT NULL,
    actor_id uuid,
    reason_code text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT storage_backend_assertions_shape_check CHECK (((assertion_kind = 'backend-destroyed'::text) AND (request_hash ~ '^[0-9a-f]{64}$'::text) AND (evidence_digest ~ '^[0-9a-f]{64}$'::text) AND (evidence_kind = ANY (ARRAY['csi-confirmed'::text, 'provider-confirmed'::text, 'delete-finalizer-confirmed'::text, 'operator-attested'::text])) AND (((actor_kind = 'user'::text) AND (actor_id IS NOT NULL)) OR ((actor_kind = 'service'::text) AND (actor_id IS NULL))) AND (reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text)))
);


--
-- Name: TABLE storage_backend_assertions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.storage_backend_assertions IS 'Append-only idempotent evidence that a physical backend was destroyed.';


--
-- Name: storage_identity_key_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_identity_key_state (
    singleton boolean DEFAULT true NOT NULL,
    key_version text NOT NULL,
    key_fingerprint text NOT NULL,
    algorithm text DEFAULT 'hmac-sha256-v1'::text NOT NULL,
    registered_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT storage_identity_key_state_shape_check CHECK (((key_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'::text) AND (key_fingerprint ~ '^[0-9a-f]{64}$'::text) AND (algorithm = 'hmac-sha256-v1'::text))),
    CONSTRAINT storage_identity_key_state_singleton_check CHECK (singleton)
);


--
-- Name: TABLE storage_identity_key_state; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.storage_identity_key_state IS 'Fingerprint and version of the dedicated stable volume-identity HMAC key; key material and raw CSI handles are never persisted.';


--
-- Name: storage_metering_activation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_metering_activation (
    measurement_basis text NOT NULL,
    state text DEFAULT 'disabled'::text NOT NULL,
    activated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT storage_metering_activation_basis_check CHECK ((measurement_basis = ANY (ARRAY['claim-requested'::text, 'volume-provisioned'::text]))),
    CONSTRAINT storage_metering_activation_state_check CHECK ((((state = ANY (ARRAY['disabled'::text, 'shadow'::text])) AND (activated_at IS NULL)) OR ((state = 'active'::text) AND (activated_at IS NOT NULL) AND (activated_at = date_trunc('day'::text, activated_at, 'UTC'::text)))))
);


--
-- Name: TABLE storage_metering_activation; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.storage_metering_activation IS 'Forward-only per-basis shadow/activation boundary; storage intervals are database-clamped to activated_at.';


--
-- Name: storage_metering_source_activations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_metering_source_activations (
    measurement_basis text NOT NULL,
    collector_id text NOT NULL,
    source_cluster text NOT NULL,
    state text DEFAULT 'disabled'::text NOT NULL,
    activated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT statement_timestamp() NOT NULL,
    updated_at timestamp with time zone DEFAULT statement_timestamp() NOT NULL,
    CONSTRAINT storage_metering_source_activations_identity_check CHECK (((measurement_basis = ANY (ARRAY['claim-requested'::text, 'volume-provisioned'::text])) AND (collector_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'::text) AND (source_cluster ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$'::text))),
    CONSTRAINT storage_metering_source_activations_state_check CHECK ((((state = ANY (ARRAY['disabled'::text, 'shadow'::text])) AND (activated_at IS NULL)) OR ((state = 'active'::text) AND (activated_at IS NOT NULL) AND (activated_at = date_trunc('day'::text, activated_at, 'UTC'::text)))))
);


--
-- Name: TABLE storage_metering_source_activations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.storage_metering_source_activations IS 'Forward-only storage boundary for one measurement basis, collector, and source cluster; the per-basis activation remains the global master.';


--
-- Name: storage_metering_source_requirements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_metering_source_requirements (
    measurement_basis text NOT NULL,
    collector_id text NOT NULL,
    source_cluster text NOT NULL,
    inventory_scope_id uuid NOT NULL,
    requirement_role text NOT NULL,
    created_at timestamp with time zone DEFAULT statement_timestamp() NOT NULL,
    CONSTRAINT storage_metering_source_requirements_role_check CHECK ((requirement_role = ANY (ARRAY['quantity'::text, 'attribution'::text])))
);


--
-- Name: TABLE storage_metering_source_requirements; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.storage_metering_source_requirements IS 'Immutable exact quantity/attribution inventory-scope set frozen when a storage source enters shadow.';


--
-- Name: storage_shadow_observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_shadow_observations (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    snapshot_id uuid NOT NULL,
    inventory_scope_id uuid NOT NULL,
    source_kind text NOT NULL,
    source_uid text NOT NULL,
    measurement_basis text NOT NULL,
    asset_id uuid,
    storage_bytes bigint,
    resource text NOT NULL,
    mapping_version text,
    mapping_fingerprint character(64),
    attribution_scope text NOT NULL,
    owner_kind text,
    owner_id text,
    disposition text NOT NULL,
    reason_code text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT storage_shadow_observations_identity_check CHECK (((source_uid <> ''::text) AND (length(source_uid) <= 256) AND (resource <> ''::text) AND (length(resource) <= 255) AND (reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'::text) AND (((mapping_version IS NULL) AND (mapping_fingerprint IS NULL)) OR ((source_kind = 'volume'::text) AND (resource ~ '^block_volume_[a-z0-9_]+$'::text) AND (mapping_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'::text) AND (mapping_fingerprint ~ '^[0-9a-f]{64}$'::text))) AND (((source_kind = 'pvc'::text) AND (measurement_basis = 'claim-requested'::text) AND (asset_id IS NULL)) OR ((source_kind = 'volume'::text) AND (measurement_basis = 'volume-provisioned'::text))))),
    CONSTRAINT storage_shadow_observations_shape_check CHECK (((attribution_scope = ANY (ARRAY['customer'::text, 'shared-platform'::text, 'unknown'::text])) AND (((attribution_scope = 'customer'::text) AND (owner_kind = ANY (ARRAY['job'::text, 'thread'::text])) AND (owner_id IS NOT NULL) AND (owner_id <> ''::text)) OR ((attribution_scope = 'shared-platform'::text) AND (owner_kind = 'platform'::text) AND (owner_id IS NULL)) OR ((attribution_scope = 'unknown'::text) AND (owner_kind IS NULL) AND (owner_id IS NULL))) AND (disposition = ANY (ARRAY['eligible-unpriced'::text, 'not-applicable'::text, 'invalid'::text, 'identity-ambiguous'::text, 'backend-unverified'::text])) AND ((storage_bytes IS NULL) OR (storage_bytes >= 0)) AND ((disposition <> 'eligible-unpriced'::text) OR (storage_bytes IS NOT NULL)) AND ((source_kind <> 'volume'::text) OR (disposition = ANY (ARRAY['invalid'::text, 'identity-ambiguous'::text])) OR (asset_id IS NOT NULL))))
);


--
-- Name: TABLE storage_shadow_observations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.storage_shadow_observations IS 'Non-publishable storage classifications with no interval or publication-plan relationship.';


--
-- Name: storage_volume_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_volume_assets (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    source_cluster text NOT NULL,
    asset_digest text NOT NULL,
    identity_key_version text NOT NULL,
    identity_scheme text NOT NULL,
    csi_driver text,
    source_lifecycle_id uuid NOT NULL,
    lifecycle_state text DEFAULT 'visible'::text NOT NULL,
    first_observed_at timestamp with time zone NOT NULL,
    last_observed_at timestamp with time zone NOT NULL,
    backend_unverified_at timestamp with time zone,
    destroyed_at timestamp with time zone,
    destruction_assertion_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT storage_volume_assets_identity_check CHECK (((source_cluster <> ''::text) AND (length(source_cluster) <= 255) AND (asset_digest ~ '^[0-9a-f]{64}$'::text) AND (((identity_scheme = 'csi-hmac-sha256-v1'::text) AND (csi_driver ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$'::text)) OR ((identity_scheme = 'pv-uid-v1'::text) AND (csi_driver IS NULL))))),
    CONSTRAINT storage_volume_assets_state_check CHECK ((((lifecycle_state = 'visible'::text) AND (backend_unverified_at IS NULL) AND (destroyed_at IS NULL) AND (destruction_assertion_id IS NULL)) OR ((lifecycle_state = 'backend-unverified'::text) AND (backend_unverified_at IS NOT NULL) AND (destroyed_at IS NULL) AND (destruction_assertion_id IS NULL)) OR ((lifecycle_state = 'destroyed'::text) AND (destroyed_at IS NOT NULL) AND (destruction_assertion_id IS NOT NULL)))),
    CONSTRAINT storage_volume_assets_time_check CHECK (((last_observed_at >= first_observed_at) AND ((backend_unverified_at IS NULL) OR (backend_unverified_at >= first_observed_at)) AND ((destroyed_at IS NULL) OR (destroyed_at >= first_observed_at))))
);


--
-- Name: TABLE storage_volume_assets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.storage_volume_assets IS 'Physical storage lifecycle keyed only by an opaque HMAC digest; PV UID belongs to an incarnation, not the billable asset.';


--
-- Name: storage_volume_incarnations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_volume_incarnations (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    asset_id uuid NOT NULL,
    inventory_scope_id uuid NOT NULL,
    source_cluster text NOT NULL,
    pv_uid text NOT NULL,
    pv_name text NOT NULL,
    storage_class_name text,
    reclaim_policy text NOT NULL,
    backend_deletion_finalizer_observed boolean DEFAULT false NOT NULL,
    volume_mode text NOT NULL,
    capacity_bytes bigint NOT NULL,
    bound_claim_uid text,
    source_resource_version text,
    first_observed_at timestamp with time zone NOT NULL,
    last_observed_at timestamp with time zone NOT NULL,
    detached_at timestamp with time zone,
    detach_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT storage_volume_incarnations_identity_check CHECK (((source_cluster <> ''::text) AND (pv_uid <> ''::text) AND (pv_name <> ''::text) AND (length(pv_uid) <= 256) AND (length(pv_name) <= 253) AND ((storage_class_name IS NULL) OR ((storage_class_name <> ''::text) AND (length(storage_class_name) <= 253))) AND ((bound_claim_uid IS NULL) OR ((bound_claim_uid <> ''::text) AND (length(bound_claim_uid) <= 256))) AND ((source_resource_version IS NULL) OR ((source_resource_version <> ''::text) AND (length(source_resource_version) <= 255))))),
    CONSTRAINT storage_volume_incarnations_shape_check CHECK (((reclaim_policy = ANY (ARRAY['delete'::text, 'retain'::text, 'recycle'::text, 'unknown'::text])) AND (volume_mode = ANY (ARRAY['filesystem'::text, 'block'::text, 'unknown'::text])) AND (capacity_bytes >= 0) AND (last_observed_at >= first_observed_at) AND (((detached_at IS NULL) AND (detach_reason IS NULL)) OR ((detached_at IS NOT NULL) AND (detach_reason = ANY (ARRAY['pv-deleted'::text, 'reimported'::text, 'backend-destroyed'::text])) AND (detached_at >= last_observed_at)))))
);


--
-- Name: TABLE storage_volume_incarnations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.storage_volume_incarnations IS 'Kubernetes PV incarnations attached to one durable physical-volume asset; contains no CSI handle or attributes.';


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
-- Name: thread_client_presence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_client_presence (
    thread_id uuid NOT NULL,
    refreshed_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT thread_client_presence_expiry_order CHECK ((expires_at > refreshed_at))
);


--
-- Name: TABLE thread_client_presence; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_client_presence IS 'Owner-gated SSE attestation that at least one browser is attached to a stateless session. One TTL row per thread deliberately collapses tabs: reload and reconnect renew the same row, and disconnect never deletes it. This is cooperative UX state only, never authorization, queue ownership, a fencing token, or a worker/finalizer liveness signal.';


--
-- Name: COLUMN thread_client_presence.refreshed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_client_presence.refreshed_at IS 'Database-clock time of the latest successful SSE establishment or renewal.';


--
-- Name: COLUMN thread_client_presence.expires_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_client_presence.expires_at IS 'Database-clock presence deadline. Absence means no row or expires_at at or before clock_timestamp(); rows are retained and overwritten so cardinality remains bounded by threads.';


--
-- Name: thread_cloud_citation_anchors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_cloud_citation_anchors (
    thread_id uuid NOT NULL,
    workspace_path text NOT NULL,
    anchor jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT thread_cloud_anchor_object CHECK ((jsonb_typeof(anchor) = 'object'::text)),
    CONSTRAINT thread_cloud_anchor_path_nonempty CHECK ((btrim(workspace_path) <> ''::text))
);


--
-- Name: TABLE thread_cloud_citation_anchors; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_cloud_citation_anchors IS 'Latest cloud provenance/version anchor per logical workspace path. A WebDAV read commits this metadata before returning; later claimants hydrate it before citation registration.';


--
-- Name: thread_cloud_sync_generations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_cloud_sync_generations (
    thread_id uuid NOT NULL,
    mount_id text NOT NULL,
    required_generation bigint DEFAULT 0 NOT NULL,
    acknowledged_generation bigint DEFAULT 0 NOT NULL,
    required_lease_token bigint DEFAULT 0 NOT NULL,
    workspace_generation text NOT NULL,
    sync_scope_sha256 character(64) NOT NULL,
    required_at timestamp with time zone DEFAULT now() NOT NULL,
    acknowledged_at timestamp with time zone,
    baseline_manifest jsonb DEFAULT '{}'::jsonb NOT NULL,
    baseline_sha256 character(64) DEFAULT '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'::bpchar NOT NULL,
    CONSTRAINT thread_cloud_sync_baseline_digest_shape CHECK ((baseline_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT thread_cloud_sync_baseline_manifest_shape CHECK (((jsonb_typeof(baseline_manifest) = 'object'::text) AND (octet_length((baseline_manifest)::text) <= 4194304))),
    CONSTRAINT thread_cloud_sync_generation_shape CHECK (((required_generation >= 0) AND (acknowledged_generation >= 0) AND (acknowledged_generation <= required_generation) AND (required_lease_token >= 0) AND ((required_generation = 0) OR (required_lease_token > 0)))),
    CONSTRAINT thread_cloud_sync_mount_id_nonempty CHECK ((mount_id <> ''::text)),
    CONSTRAINT thread_cloud_sync_scope_digest_shape CHECK ((sync_scope_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT thread_cloud_sync_workspace_generation_nonempty CHECK ((workspace_generation <> ''::text))
);


--
-- Name: TABLE thread_cloud_sync_generations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_cloud_sync_generations IS 'Queue-fenced required generation per thread cloud mount. Before a stateless owner starts push(N), it increments required_generation in the same statement that proves its live run_queue lease. The remote cloud marker is the resource-side commit acknowledgement; the DB acknowledgement is an observable mirror, not a substitute for reading that marker on the next claim.';


--
-- Name: COLUMN thread_cloud_sync_generations.mount_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_cloud_sync_generations.mount_id IS 'Stable logical cloud destination key derived from non-secret source/path identity; never the replace-on-edit thread_mounts row UUID. Legacy session folders use the reserved value legacy-session.';


--
-- Name: COLUMN thread_cloud_sync_generations.required_generation; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_cloud_sync_generations.required_generation IS 'Highest push generation a successor must observe committed on the cloud resource before it may pull. Monotonic for the lifetime of the thread.';


--
-- Name: COLUMN thread_cloud_sync_generations.acknowledged_generation; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_cloud_sync_generations.acknowledged_generation IS 'Highest resource marker verified or written by a live lease owner. This may lag after marker-write/DB-ack crash; successors reconcile it from the resource. It never authorizes pull by itself.';


--
-- Name: COLUMN thread_cloud_sync_generations.required_lease_token; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_cloud_sync_generations.required_lease_token IS 'run_queue lease token that reserved required_generation. Diagnostic and recovery evidence; current ownership is always re-proved against run_queue rather than inferred from this value.';


--
-- Name: COLUMN thread_cloud_sync_generations.workspace_generation; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_cloud_sync_generations.workspace_generation IS 'Authoritative orchestrator workspace-binding generation whose durable workspace bytes are being pushed. A pending row may not be recovered against a different runtime incarnation.';


--
-- Name: COLUMN thread_cloud_sync_generations.sync_scope_sha256; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_cloud_sync_generations.sync_scope_sha256 IS 'SHA-256 over the non-secret cloud destination descriptor (thread, mount identity/path/backend/WebDAV URL) plus workspace generation. It binds the counter to one exact source/destination pair without persisting credentials.';


--
-- Name: COLUMN thread_cloud_sync_generations.baseline_manifest; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_cloud_sync_generations.baseline_manifest IS 'Queue-fenced turn-start baseline keyed by mount-relative path. Each entry contains the SHA-256 of durable workspace bytes and the WebDAV ETag observed after pull. A successor compares against this baseline and replays only locally changed/new/deleted paths; it never force-PUTs untouched files over concurrent cloud edits.';


--
-- Name: COLUMN thread_cloud_sync_generations.baseline_sha256; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_cloud_sync_generations.baseline_sha256 IS 'SHA-256 of the canonical compact JSON baseline. The resource commit marker binds this digest so marker-write/DB-ack recovery can acknowledge without replaying the already committed delta.';


--
-- Name: thread_control_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_control_requests (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    thread_id uuid NOT NULL,
    request_seq bigint NOT NULL,
    client_request_id uuid NOT NULL,
    verb text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    requested_by text NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    accepted_agent_id uuid,
    outcome text,
    result jsonb,
    applied_at timestamp with time zone,
    applied_lease_token bigint,
    applied_agent_id uuid,
    journal_epoch integer,
    journal_seq bigint,
    acknowledged_at timestamp with time zone,
    error_code text,
    CONSTRAINT thread_control_outcome_value CHECK (((outcome IS NULL) OR (outcome = ANY (ARRAY['applied'::text, 'rejected'::text])))),
    CONSTRAINT thread_control_payload_object CHECK ((jsonb_typeof(payload) = 'object'::text)),
    CONSTRAINT thread_control_terminal_shape CHECK ((((outcome IS NULL) AND (result IS NULL) AND (applied_at IS NULL) AND (applied_lease_token IS NULL) AND (applied_agent_id IS NULL) AND (journal_epoch IS NULL) AND (journal_seq IS NULL) AND (acknowledged_at IS NULL) AND (error_code IS NULL)) OR ((outcome IS NOT NULL) AND (result IS NOT NULL) AND (applied_at IS NOT NULL) AND (((applied_lease_token IS NOT NULL) AND (applied_agent_id IS NULL)) OR ((applied_lease_token IS NULL) AND (applied_agent_id IS NOT NULL))) AND (journal_epoch IS NOT NULL) AND (journal_seq IS NOT NULL) AND (acknowledged_at IS NOT NULL))))
);


--
-- Name: TABLE thread_control_requests; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_control_requests IS 'Durable session control inbox shared by pinned and stateless lanes. The orchestrator admits an owner-authorized request; only the exact serving owner applies it and writes its journal result. outcome remains NULL until the result event is durably committed and terminalization proves the current lease token or exact pinned agent binding.';


--
-- Name: COLUMN thread_control_requests.accepted_agent_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_control_requests.accepted_agent_id IS 'Exact pinned journal-writer credential. Captured under the admission row lock and transferable only to a reciprocal successor before any receipt; NULL for stateless requests. Hostname, IP and pod name are not ownership credentials.';


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
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    control_request_id uuid,
    interrupt_request_id uuid,
    permission_request_id uuid
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
-- Name: COLUMN thread_events.control_request_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_events.control_request_id IS 'Durable receipt link for an owner-written control result frame. A pending request with this event already committed is validated and finalized without emitting a duplicate frame; the current owner then converges its in-memory scalar from the validated result.';


--
-- Name: COLUMN thread_events.interrupt_request_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_events.interrupt_request_id IS 'Durable result link for one exact-lease interrupt request. A committed receipt lets the same owner recover finalization without emitting a duplicate journal frame; it never transfers application authority to a successor lease.';


--
-- Name: COLUMN thread_events.permission_request_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_events.permission_request_id IS 'Durable permission.resolved receipt link for one exact-lease permission request retired after proven owner loss. The partial unique index added by 0148 permits at most one linked receipt per request.';


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
-- Name: thread_interrupt_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_interrupt_requests (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    thread_id uuid NOT NULL,
    client_request_id uuid NOT NULL,
    target_turn_id integer NOT NULL,
    accepted_lease_token bigint NOT NULL,
    accepted_leased_by text NOT NULL,
    requested_by text NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    outcome text,
    result jsonb,
    applied_mode text,
    applied_at timestamp with time zone,
    applied_lease_token bigint,
    journal_epoch integer,
    journal_seq bigint,
    acknowledged_at timestamp with time zone,
    error_code text,
    CONSTRAINT thread_interrupt_exact_lease_receipt CHECK (((applied_lease_token IS NULL) OR (applied_lease_token = accepted_lease_token))),
    CONSTRAINT thread_interrupt_lease_token_positive CHECK ((accepted_lease_token > 0)),
    CONSTRAINT thread_interrupt_leased_by_nonempty CHECK ((btrim(accepted_leased_by) <> ''::text)),
    CONSTRAINT thread_interrupt_mode_value CHECK (((applied_mode IS NULL) OR (applied_mode = ANY (ARRAY['hard'::text, 'graceful'::text])))),
    CONSTRAINT thread_interrupt_outcome_value CHECK (((outcome IS NULL) OR (outcome = ANY (ARRAY['applied'::text, 'rejected'::text])))),
    CONSTRAINT thread_interrupt_result_object CHECK (((result IS NULL) OR (jsonb_typeof(result) = 'object'::text))),
    CONSTRAINT thread_interrupt_target_turn_positive CHECK ((target_turn_id > 0)),
    CONSTRAINT thread_interrupt_terminal_shape CHECK ((((outcome IS NULL) AND (result IS NULL) AND (applied_mode IS NULL) AND (applied_at IS NULL) AND (applied_lease_token IS NULL) AND (journal_epoch IS NULL) AND (journal_seq IS NULL) AND (acknowledged_at IS NULL) AND (error_code IS NULL)) OR ((outcome = 'applied'::text) AND (result IS NOT NULL) AND (applied_mode IS NOT NULL) AND (applied_at IS NOT NULL) AND (applied_lease_token = accepted_lease_token) AND (journal_epoch IS NOT NULL) AND (journal_seq IS NOT NULL) AND (acknowledged_at IS NOT NULL) AND (error_code IS NULL)) OR ((outcome = 'rejected'::text) AND (result IS NOT NULL) AND (applied_mode IS NULL) AND (applied_at IS NOT NULL) AND (applied_lease_token = accepted_lease_token) AND (journal_epoch IS NOT NULL) AND (journal_seq IS NOT NULL) AND (acknowledged_at IS NOT NULL) AND (error_code IS NOT NULL) AND (btrim(error_code) <> ''::text))))
);


--
-- Name: TABLE thread_interrupt_requests; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_interrupt_requests IS 'Durable stateless interrupt inbox. Each request is admitted only for the exact run_queue lease and concrete active turn captured on the row. The lease owner applies the idempotent signal and writes the journal result with its in-process sequence allocator; a successor never applies it. outcome remains NULL until that receipt is durable and owner-fenced.';


--
-- Name: COLUMN thread_interrupt_requests.client_request_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_interrupt_requests.client_request_id IS 'Browser-generated idempotency and acknowledgement correlation key. It is unique per thread; concurrent tabs keep distinct rows and receipts.';


--
-- Name: COLUMN thread_interrupt_requests.accepted_lease_token; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_interrupt_requests.accepted_lease_token IS 'Immutable exact stateless owner credential captured while the matching run_queue admission window is locked. No later lease may adopt it.';


--
-- Name: COLUMN thread_interrupt_requests.accepted_leased_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_interrupt_requests.accepted_leased_by IS 'Pod identity captured for diagnostics only. Correctness is fenced by accepted_lease_token; hostname or pod name is never an owner credential.';


--
-- Name: COLUMN thread_interrupt_requests.applied_lease_token; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_interrupt_requests.applied_lease_token IS 'Lease that wrote the durable result frame. The table constraint requires it to equal accepted_lease_token for every terminal result.';


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
    seq bigint NOT NULL,
    rewound_at timestamp with time zone,
    turn_execution_id uuid
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
-- Name: COLUMN thread_messages.rewound_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_messages.rewound_at IS 'Set when a session rewind supersedes this row (seq >= the rewind''s from_seq). Live conversation readers filter rewound_at IS NULL; the row itself is never deleted. See docs/features/session_rewind.md.';


--
-- Name: COLUMN thread_messages.turn_execution_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_messages.turn_execution_id IS 'Identity minted on the exact accepted turn-boundary message inside the stateless claim''s fenced final-transcript transaction. It is reused by an idempotent reconcile and keys session_turn completion effects; it is NULL for pinned turns and messages that are not a finalized boundary.';


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
    accepted_lease_token bigint,
    CONSTRAINT thread_permission_accepted_lease_positive CHECK (((accepted_lease_token IS NULL) OR (accepted_lease_token > 0))),
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
-- Name: COLUMN thread_permission_requests.accepted_lease_token; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.thread_permission_requests.accepted_lease_token IS 'Immutable exact stateless session_turn lease captured while admission holds the threads -> run_queue locks. NULL identifies pinned or legacy rows and is never guessed by a generic expiry sweep. For rolling compatibility, a NULL row may be expired only at a proven writer-exclusive stateless owner-loss or terminal boundary.';


--
-- Name: thread_rewinds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_rewinds (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    thread_id uuid NOT NULL,
    from_seq bigint NOT NULL,
    mode text NOT NULL,
    actor text,
    swept_count integer DEFAULT 0 NOT NULL,
    abandoned_sha text,
    restored_to_sha text,
    restore_commit_sha text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT thread_rewinds_mode_check CHECK ((mode = ANY (ARRAY['both'::text, 'conversation'::text, 'code'::text])))
);


--
-- Name: TABLE thread_rewinds; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_rewinds IS 'One row per session rewind: the audit trail, un-tombstone metadata, and the workspace SHAs of the forward-restore. Append-only.';


--
-- Name: thread_session_runtime_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_session_runtime_state (
    thread_id uuid NOT NULL,
    memory_extraction_turn integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT thread_session_memory_cursor_nonnegative CHECK ((memory_extraction_turn >= 0))
);


--
-- Name: TABLE thread_session_runtime_state; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_session_runtime_state IS 'Small durable cursors for persistent-session work that is otherwise process-local. memory_extraction_turn prevents a successor claim from repeating interval extraction already claimed by its predecessor.';


--
-- Name: thread_session_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_session_tasks (
    thread_id uuid NOT NULL,
    task_number integer NOT NULL,
    description text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    priority text DEFAULT 'medium'::text NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT thread_session_tasks_completion_shape CHECK ((((status = 'completed'::text) AND (completed_at IS NOT NULL)) OR ((status <> 'completed'::text) AND (completed_at IS NULL)))),
    CONSTRAINT thread_session_tasks_description_nonempty CHECK ((btrim(description) <> ''::text)),
    CONSTRAINT thread_session_tasks_number_positive CHECK ((task_number > 0)),
    CONSTRAINT thread_session_tasks_priority_value CHECK ((priority = ANY (ARRAY['high'::text, 'medium'::text, 'low'::text]))),
    CONSTRAINT thread_session_tasks_status_value CHECK ((status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'completed'::text])))
);


--
-- Name: TABLE thread_session_tasks; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_session_tasks IS 'Authoritative persistent-session checklist keyed by thread. The runtime hydrates it on every attach; task_number is rendered as task_<N> and is allocated while the parent thread row is locked.';


--
-- Name: thread_turn_commits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_turn_commits (
    thread_id uuid NOT NULL,
    seq bigint NOT NULL,
    commit_sha text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE thread_turn_commits; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.thread_turn_commits IS 'Workspace state after transcript seq <= N: written right after each per-turn auto-commit / compaction checkpoint commit succeeds. The restore target for a rewind to seq S is the row with the largest seq < S. seq 0 = the pre-first-message workspace.';


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
    execution_lane text DEFAULT 'pinned'::text NOT NULL,
    events_seq_hwm bigint DEFAULT 0 NOT NULL,
    control_seq_hwm bigint DEFAULT 0 NOT NULL,
    control_admission_agent_id uuid,
    narration_mode text,
    CONSTRAINT valid_narration_mode CHECK ((narration_mode = ANY (ARRAY['silent'::text, 'verbose'::text, 'auto'::text]))),
    CONSTRAINT valid_permission_mode CHECK (((permission_mode)::text = ANY ((ARRAY['supervised'::character varying, 'auto_accept'::character varying, 'autonomous'::character varying])::text[]))),
    CONSTRAINT valid_thread_status CHECK (((status)::text = ANY ((ARRAY['created'::character varying, 'active'::character varying, 'idle'::character varying, 'awaiting_user'::character varying, 'suspended'::character varying, 'ended'::character varying])::text[])))
);


--
-- Name: COLUMN threads.events_epoch; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.events_epoch IS 'Current event-log writer generation (client-visible). Bumped only deliberately: rewind, a reaper/steal takeover, or an attach that finds the previous session life terminal (terminal thread status, a terminal lifecycle frame in the epoch, or the epoch wholly beyond retention). Clean reattaches REUSE the epoch so cached client cursors stay valid; an older-epoch cursor triggers authoritative re-sync (gone_beyond_horizon). See docs/features/stateless_agents.md §5.3.2.';


--
-- Name: COLUMN threads.awaiting_user_since; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.awaiting_user_since IS 'Set by the agent when it reaches a natural pause untethered. The attention_sleep_sweeper suspends the workspace when this exceeds headless_attention_sleep_minutes. Cleared on reattach.';


--
-- Name: COLUMN threads.extend_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.extend_count IS 'Number of magic-link extend-window clicks since this awaiting_user session began. Capped at 4 (= 4h ceiling at default 60min/extend).';


--
-- Name: COLUMN threads.execution_lane; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.execution_lane IS 'Which execution plane serves this thread: ''pinned'' (registered-agent pod, the default) or ''stateless'' (run_queue claim by any pod). App-validated by design — no CHECK. See docs/features/stateless_agents.md §5.4.4.';


--
-- Name: COLUMN threads.events_seq_hwm; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.events_seq_hwm IS 'Highest seq ever allocated in the CURRENT events_epoch. Survives retention pruning of the thread_events rows themselves; reset to 0 atomically on every epoch bump. Maintained by the agent journal writer''s fenced flush (GREATEST over the batch in the same statement) and pre-incremented by the system-frame allocator (src/shared/event_journal). Attach seeds its in-process counter from GREATEST(events_seq_hwm, MAX(seq) of the epoch). See docs/features/stateless_agents.md §5.3.2.';


--
-- Name: COLUMN threads.control_seq_hwm; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.control_seq_hwm IS 'Highest commit-ordered thread_control_requests.request_seq allocated for this thread. Admission increments it while holding the threads row lock; never allocate request order from an IDENTITY/sequence.';


--
-- Name: COLUMN threads.control_admission_agent_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.control_admission_agent_id IS 'Exact pinned-owner capability and teardown fence. NULL is closed. An inbox-capable reciprocal owner writes its own agent UUID after attach and clears it before its final drain. A stale credential never transfers to a different owner generation.';


--
-- Name: COLUMN threads.narration_mode; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.threads.narration_mode IS 'Durable interactive narration mode (silent | verbose | auto). NULL means the thread still inherits its resolved config; creation and the serving control owner materialize an explicit value.';


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
    infra_coverage_revision text,
    CONSTRAINT usage_rollup_day_state_infra_revision_check CHECK (((infra_coverage_revision IS NULL) OR (infra_coverage_revision <> ''::text))),
    CONSTRAINT usage_rollup_day_state_shape_check CHECK (((applied_audit_revision > 0) AND (coverage_status = ANY (ARRAY['complete'::text, 'partial'::text, 'unavailable'::text])) AND (jsonb_typeof(unknown_ranges) = 'array'::text)))
);


--
-- Name: COLUMN usage_rollup_day_state.infra_coverage_revision; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_rollup_day_state.infra_coverage_revision IS 'Exact infra_usage_day_state.coverage_revision consumed by this full-day rollup; NULL only for legacy-only/pre-cutover days.';


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
-- Name: message_delivery_attempts attempt_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_delivery_attempts ALTER COLUMN attempt_id SET DEFAULT nextval('public.message_delivery_attempts_attempt_id_seq'::regclass);


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
-- Name: agent_metering_binding_events agent_metering_binding_events_agent_revision_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_metering_binding_events
    ADD CONSTRAINT agent_metering_binding_events_agent_revision_uq UNIQUE (agent_id, revision);


--
-- Name: agent_metering_binding_events agent_metering_binding_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_metering_binding_events
    ADD CONSTRAINT agent_metering_binding_events_pkey PRIMARY KEY (id);


--
-- Name: agent_metering_pod_identity_state agent_metering_pod_identity_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_metering_pod_identity_state
    ADD CONSTRAINT agent_metering_pod_identity_state_pkey PRIMARY KEY (agent_id);


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
-- Name: completion_effects completion_effects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.completion_effects
    ADD CONSTRAINT completion_effects_pkey PRIMARY KEY (producer_kind, producer_id, effect_name);


--
-- Name: completion_finalizer_leases completion_finalizer_leases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.completion_finalizer_leases
    ADD CONSTRAINT completion_finalizer_leases_pkey PRIMARY KEY (lease_name);


--
-- Name: compute_metering_epoch_authorities compute_epoch_authorities_epoch_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_epoch_authorities_epoch_uq UNIQUE (activation_key, inventory_scope_id, inventory_scope_epoch_id);


--
-- Name: compute_metering_epoch_authorities compute_epoch_authorities_id_scope_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_epoch_authorities_id_scope_uq UNIQUE (id, activation_key, inventory_scope_id);


--
-- Name: compute_metering_epoch_authorities compute_epoch_authorities_request_scope_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_epoch_authorities_request_scope_uq UNIQUE (promotion_request_id, inventory_scope_id);


--
-- Name: compute_metering_epoch_authorities compute_epoch_authorities_sequence_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_epoch_authorities_sequence_uq UNIQUE (activation_key, inventory_scope_id, authority_sequence);


--
-- Name: compute_metering_activation compute_metering_activation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_activation
    ADD CONSTRAINT compute_metering_activation_pkey PRIMARY KEY (activation_key);


--
-- Name: compute_metering_epoch_authorities compute_metering_epoch_authorities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_metering_epoch_authorities_pkey PRIMARY KEY (id);


--
-- Name: compute_metering_epoch_promotion_requests compute_metering_epoch_promotion_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_promotion_requests
    ADD CONSTRAINT compute_metering_epoch_promotion_requests_pkey PRIMARY KEY (id);


--
-- Name: compute_metering_scope_requirements compute_metering_scope_requirements_epoch_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_scope_requirements
    ADD CONSTRAINT compute_metering_scope_requirements_epoch_uq UNIQUE (activation_key, inventory_scope_epoch_id);


--
-- Name: compute_metering_scope_requirements compute_metering_scope_requirements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_scope_requirements
    ADD CONSTRAINT compute_metering_scope_requirements_pkey PRIMARY KEY (activation_key, inventory_scope_id);


--
-- Name: compute_shadow_observations compute_shadow_observations_item_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_shadow_observations
    ADD CONSTRAINT compute_shadow_observations_item_uq UNIQUE (snapshot_id, activation_key, source_kind, source_uid);


--
-- Name: compute_shadow_observations compute_shadow_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_shadow_observations
    ADD CONSTRAINT compute_shadow_observations_pkey PRIMARY KEY (id);


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
-- Name: datasource_tombstones datasource_tombstones_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.datasource_tombstones
    ADD CONSTRAINT datasource_tombstones_pkey PRIMARY KEY (id);


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
-- Name: infra_metering_control infra_metering_control_cutover_request_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.infra_metering_control
    ADD CONSTRAINT infra_metering_control_cutover_request_uq UNIQUE (cutover_request_id);


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
-- Name: infrastructure_storage_resource_mappings infrastructure_storage_resource_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.infrastructure_storage_resource_mappings
    ADD CONSTRAINT infrastructure_storage_resource_mappings_pkey PRIMARY KEY (rule_fingerprint);


--
-- Name: infrastructure_storage_resource_mappings infrastructure_storage_resource_mappings_selector_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.infrastructure_storage_resource_mappings
    ADD CONSTRAINT infrastructure_storage_resource_mappings_selector_uq UNIQUE NULLS NOT DISTINCT (source_cluster, storage_class_name, csi_driver, volume_mode);


--
-- Name: job_change_records job_change_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_change_records
    ADD CONSTRAINT job_change_records_pkey PRIMARY KEY (job_id);


--
-- Name: job_completion_commands job_completion_commands_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_completion_commands
    ADD CONSTRAINT job_completion_commands_pkey PRIMARY KEY (id);


--
-- Name: job_completion_sweep_actions job_completion_sweep_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_completion_sweep_actions
    ADD CONSTRAINT job_completion_sweep_actions_pkey PRIMARY KEY (job_id, attempt);


--
-- Name: job_datasources job_datasources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_datasources
    ADD CONSTRAINT job_datasources_pkey PRIMARY KEY (job_id, datasource_id);


--
-- Name: job_message_routes job_message_routes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_message_routes
    ADD CONSTRAINT job_message_routes_pkey PRIMARY KEY (route_id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: knowledge_materialization_intents knowledge_materialization_intents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_materialization_intents
    ADD CONSTRAINT knowledge_materialization_intents_pkey PRIMARY KEY (id);


--
-- Name: legacy_workspace_cutover_plan_events legacy_workspace_cutover_plan_even_source_source_id_unit_ts_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legacy_workspace_cutover_plan_events
    ADD CONSTRAINT legacy_workspace_cutover_plan_even_source_source_id_unit_ts_key UNIQUE (source, source_id, unit, ts);


--
-- Name: legacy_workspace_cutover_plan_events legacy_workspace_cutover_plan_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legacy_workspace_cutover_plan_events
    ADD CONSTRAINT legacy_workspace_cutover_plan_events_pkey PRIMARY KEY (plan_id, ordinal);


--
-- Name: legacy_workspace_cutover_plans legacy_workspace_cutover_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legacy_workspace_cutover_plans
    ADD CONSTRAINT legacy_workspace_cutover_plans_pkey PRIMARY KEY (id);


--
-- Name: legacy_workspace_cutover_plans legacy_workspace_cutover_plans_workspace_interval_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legacy_workspace_cutover_plans
    ADD CONSTRAINT legacy_workspace_cutover_plans_workspace_interval_id_key UNIQUE (workspace_interval_id);


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
-- Name: message_delivery_attempts message_delivery_attempts_intent_id_attempt_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_delivery_attempts
    ADD CONSTRAINT message_delivery_attempts_intent_id_attempt_number_key UNIQUE (intent_id, attempt_number);


--
-- Name: message_delivery_attempts message_delivery_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_delivery_attempts
    ADD CONSTRAINT message_delivery_attempts_pkey PRIMARY KEY (attempt_id);


--
-- Name: message_delivery_intents message_delivery_intents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_delivery_intents
    ADD CONSTRAINT message_delivery_intents_pkey PRIMARY KEY (intent_id);


--
-- Name: message_delivery_intents message_delivery_intents_routing_generation_bucket_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_delivery_intents
    ADD CONSTRAINT message_delivery_intents_routing_generation_bucket_key UNIQUE (routing_generation, bucket);


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
-- Name: officer_floor_wake_episodes officer_floor_wake_episodes_dedup_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.officer_floor_wake_episodes
    ADD CONSTRAINT officer_floor_wake_episodes_dedup_key_key UNIQUE (dedup_key);


--
-- Name: officer_floor_wake_episodes officer_floor_wake_episodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.officer_floor_wake_episodes
    ADD CONSTRAINT officer_floor_wake_episodes_pkey PRIMARY KEY (id);


--
-- Name: officer_ticket_claims officer_ticket_claims_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.officer_ticket_claims
    ADD CONSTRAINT officer_ticket_claims_pkey PRIMARY KEY (id);


--
-- Name: canvas_editor_awareness pk_canvas_editor_awareness; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_editor_awareness
    ADD CONSTRAINT pk_canvas_editor_awareness PRIMARY KEY (thread_id, canvas_id, editing_session_id);


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
-- Name: project_officers project_officers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_officers
    ADD CONSTRAINT project_officers_pkey PRIMARY KEY (project_id);


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
-- Name: resource_inventory_ingest_tickets resource_inventory_ingest_tickets_id_scope_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_ingest_tickets
    ADD CONSTRAINT resource_inventory_ingest_tickets_id_scope_uq UNIQUE (id, scope_epoch_id);


--
-- Name: resource_inventory_ingest_tickets resource_inventory_ingest_tickets_nonce_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_ingest_tickets
    ADD CONSTRAINT resource_inventory_ingest_tickets_nonce_hash_key UNIQUE (nonce_hash);


--
-- Name: resource_inventory_ingest_tickets resource_inventory_ingest_tickets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_ingest_tickets
    ADD CONSTRAINT resource_inventory_ingest_tickets_pkey PRIMARY KEY (id);


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
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_recovery_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scope_epochs
    ADD CONSTRAINT resource_inventory_scope_epochs_recovery_uq UNIQUE (recovery_from_epoch_id);


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
-- Name: resource_inventory_scopes resource_inventory_scopes_source_identity_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scopes
    ADD CONSTRAINT resource_inventory_scopes_source_identity_uq UNIQUE (id, collector_id, source_cluster);


--
-- Name: resource_inventory_shadow_comparisons resource_inventory_shadow_comparisons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_shadow_comparisons
    ADD CONSTRAINT resource_inventory_shadow_comparisons_pkey PRIMARY KEY (id);


--
-- Name: resource_inventory_shadow_comparisons resource_inventory_shadow_comparisons_snapshot_uid_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_shadow_comparisons
    ADD CONSTRAINT resource_inventory_shadow_comparisons_snapshot_uid_uq UNIQUE (snapshot_id, source_uid);


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
-- Name: resource_inventory_snapshots resource_inventory_snapshots_ingest_ticket_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_snapshots
    ADD CONSTRAINT resource_inventory_snapshots_ingest_ticket_uq UNIQUE (ingest_ticket_id);


--
-- Name: resource_inventory_snapshots resource_inventory_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_snapshots
    ADD CONSTRAINT resource_inventory_snapshots_pkey PRIMARY KEY (id);


--
-- Name: resource_inventory_transport_nonces resource_inventory_transport_nonces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_transport_nonces
    ADD CONSTRAINT resource_inventory_transport_nonces_pkey PRIMARY KEY (collector_id, request_nonce);


--
-- Name: resource_inventory_watch_events resource_inventory_watch_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_watch_events
    ADD CONSTRAINT resource_inventory_watch_events_pkey PRIMARY KEY (watch_session_id, id);


--
-- Name: resource_inventory_watch_events resource_inventory_watch_events_session_ordinal_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_watch_events
    ADD CONSTRAINT resource_inventory_watch_events_session_ordinal_uq UNIQUE (watch_session_id, ordinal);


--
-- Name: resource_inventory_watch_sessions resource_inventory_watch_sessions_id_epoch_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_watch_sessions
    ADD CONSTRAINT resource_inventory_watch_sessions_id_epoch_uq UNIQUE (id, scope_epoch_id);


--
-- Name: resource_inventory_watch_sessions resource_inventory_watch_sessions_nonce_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_watch_sessions
    ADD CONSTRAINT resource_inventory_watch_sessions_nonce_hash_key UNIQUE (nonce_hash);


--
-- Name: resource_inventory_watch_sessions resource_inventory_watch_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_watch_sessions
    ADD CONSTRAINT resource_inventory_watch_sessions_pkey PRIMARY KEY (id);


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
-- Name: run_queue run_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_queue
    ADD CONSTRAINT run_queue_pkey PRIMARY KEY (unit_id);


--
-- Name: runtime_actor_access_tokens runtime_actor_access_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_actor_access_tokens
    ADD CONSTRAINT runtime_actor_access_tokens_pkey PRIMARY KEY (token_hash);


--
-- Name: runtime_actor_bootstraps runtime_actor_bootstraps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_actor_bootstraps
    ADD CONSTRAINT runtime_actor_bootstraps_pkey PRIMARY KEY (token_hash);


--
-- Name: runtime_actor_grants runtime_actor_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_actor_grants
    ADD CONSTRAINT runtime_actor_grants_pkey PRIMARY KEY (id);


--
-- Name: runtime_actor_grants runtime_actor_grants_refresh_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_actor_grants
    ADD CONSTRAINT runtime_actor_grants_refresh_token_hash_key UNIQUE (refresh_token_hash);


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
-- Name: storage_asset_coverage_gaps storage_asset_coverage_gaps_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_asset_coverage_gaps
    ADD CONSTRAINT storage_asset_coverage_gaps_no_overlap EXCLUDE USING gist (asset_id WITH =, tstzrange(gap_start, gap_end, '[)'::text) WITH &&);


--
-- Name: storage_asset_coverage_gaps storage_asset_coverage_gaps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_asset_coverage_gaps
    ADD CONSTRAINT storage_asset_coverage_gaps_pkey PRIMARY KEY (id);


--
-- Name: storage_backend_assertions storage_backend_assertions_asset_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_backend_assertions
    ADD CONSTRAINT storage_backend_assertions_asset_uq UNIQUE (asset_id);


--
-- Name: storage_backend_assertions storage_backend_assertions_idempotency_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_backend_assertions
    ADD CONSTRAINT storage_backend_assertions_idempotency_uq UNIQUE (idempotency_key);


--
-- Name: storage_backend_assertions storage_backend_assertions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_backend_assertions
    ADD CONSTRAINT storage_backend_assertions_pkey PRIMARY KEY (id);


--
-- Name: storage_identity_key_state storage_identity_key_state_key_fingerprint_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_identity_key_state
    ADD CONSTRAINT storage_identity_key_state_key_fingerprint_key UNIQUE (key_fingerprint);


--
-- Name: storage_identity_key_state storage_identity_key_state_key_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_identity_key_state
    ADD CONSTRAINT storage_identity_key_state_key_version_key UNIQUE (key_version);


--
-- Name: storage_identity_key_state storage_identity_key_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_identity_key_state
    ADD CONSTRAINT storage_identity_key_state_pkey PRIMARY KEY (singleton);


--
-- Name: storage_metering_activation storage_metering_activation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_metering_activation
    ADD CONSTRAINT storage_metering_activation_pkey PRIMARY KEY (measurement_basis);


--
-- Name: storage_metering_source_activations storage_metering_source_activations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_metering_source_activations
    ADD CONSTRAINT storage_metering_source_activations_pkey PRIMARY KEY (measurement_basis, collector_id, source_cluster);


--
-- Name: storage_metering_source_requirements storage_metering_source_requirements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_metering_source_requirements
    ADD CONSTRAINT storage_metering_source_requirements_pkey PRIMARY KEY (measurement_basis, collector_id, source_cluster, inventory_scope_id, requirement_role);


--
-- Name: storage_shadow_observations storage_shadow_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_shadow_observations
    ADD CONSTRAINT storage_shadow_observations_pkey PRIMARY KEY (id);


--
-- Name: storage_shadow_observations storage_shadow_observations_snapshot_identity_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_shadow_observations
    ADD CONSTRAINT storage_shadow_observations_snapshot_identity_uq UNIQUE (snapshot_id, source_kind, source_uid);


--
-- Name: storage_volume_assets storage_volume_assets_destruction_assertion_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_assets
    ADD CONSTRAINT storage_volume_assets_destruction_assertion_id_key UNIQUE (destruction_assertion_id);


--
-- Name: storage_volume_assets storage_volume_assets_identity_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_assets
    ADD CONSTRAINT storage_volume_assets_identity_uq UNIQUE (source_cluster, asset_digest);


--
-- Name: storage_volume_assets storage_volume_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_assets
    ADD CONSTRAINT storage_volume_assets_pkey PRIMARY KEY (id);


--
-- Name: storage_volume_assets storage_volume_assets_source_lifecycle_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_assets
    ADD CONSTRAINT storage_volume_assets_source_lifecycle_id_key UNIQUE (source_lifecycle_id);


--
-- Name: storage_volume_incarnations storage_volume_incarnations_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_incarnations
    ADD CONSTRAINT storage_volume_incarnations_no_overlap EXCLUDE USING gist (asset_id WITH =, tstzrange(first_observed_at, detached_at, '[)'::text) WITH &&);


--
-- Name: storage_volume_incarnations storage_volume_incarnations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_incarnations
    ADD CONSTRAINT storage_volume_incarnations_pkey PRIMARY KEY (id);


--
-- Name: storage_volume_incarnations storage_volume_incarnations_pv_uid_uq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_incarnations
    ADD CONSTRAINT storage_volume_incarnations_pv_uid_uq UNIQUE (source_cluster, pv_uid);


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
-- Name: thread_client_presence thread_client_presence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_client_presence
    ADD CONSTRAINT thread_client_presence_pkey PRIMARY KEY (thread_id);


--
-- Name: thread_cloud_citation_anchors thread_cloud_citation_anchors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_cloud_citation_anchors
    ADD CONSTRAINT thread_cloud_citation_anchors_pkey PRIMARY KEY (thread_id, workspace_path);


--
-- Name: thread_cloud_sync_generations thread_cloud_sync_generations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_cloud_sync_generations
    ADD CONSTRAINT thread_cloud_sync_generations_pkey PRIMARY KEY (thread_id, mount_id);


--
-- Name: thread_control_requests thread_control_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_control_requests
    ADD CONSTRAINT thread_control_requests_pkey PRIMARY KEY (id);


--
-- Name: thread_events thread_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_events
    ADD CONSTRAINT thread_events_pkey PRIMARY KEY (id);


--
-- Name: thread_interrupt_requests thread_interrupt_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_interrupt_requests
    ADD CONSTRAINT thread_interrupt_requests_pkey PRIMARY KEY (id);


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
-- Name: thread_rewinds thread_rewinds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_rewinds
    ADD CONSTRAINT thread_rewinds_pkey PRIMARY KEY (id);


--
-- Name: thread_session_runtime_state thread_session_runtime_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_session_runtime_state
    ADD CONSTRAINT thread_session_runtime_state_pkey PRIMARY KEY (thread_id);


--
-- Name: thread_session_tasks thread_session_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_session_tasks
    ADD CONSTRAINT thread_session_tasks_pkey PRIMARY KEY (thread_id, task_number);


--
-- Name: thread_turn_commits thread_turn_commits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_turn_commits
    ADD CONSTRAINT thread_turn_commits_pkey PRIMARY KEY (thread_id, seq);


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
-- Name: canvas_editor_awareness uq_canvas_editor_awareness_sender; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_editor_awareness
    ADD CONSTRAINT uq_canvas_editor_awareness_sender UNIQUE (sender_id);


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
-- Name: job_completion_commands uq_job_completion_client; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_completion_commands
    ADD CONSTRAINT uq_job_completion_client UNIQUE (job_id, client_report_id);


--
-- Name: job_completion_commands uq_job_completion_seq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_completion_commands
    ADD CONSTRAINT uq_job_completion_seq UNIQUE (job_id, report_seq);


--
-- Name: job_completion_sweep_actions uq_job_completion_sweep_command_attempt; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_completion_sweep_actions
    ADD CONSTRAINT uq_job_completion_sweep_command_attempt UNIQUE (command_id, command_attempt);


--
-- Name: models uq_model_provider_v2; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.models
    ADD CONSTRAINT uq_model_provider_v2 UNIQUE (provider_kind, provider_ref, model_id);


--
-- Name: officer_ticket_claims uq_officer_ticket_claim_generation; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.officer_ticket_claims
    ADD CONSTRAINT uq_officer_ticket_claim_generation UNIQUE (project_id, ticket_note_id, ready_generation_at);


--
-- Name: officer_ticket_claims uq_officer_ticket_claim_job; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.officer_ticket_claims
    ADD CONSTRAINT uq_officer_ticket_claim_job UNIQUE (job_id);


--
-- Name: thread_control_requests uq_thread_control_client_request; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_control_requests
    ADD CONSTRAINT uq_thread_control_client_request UNIQUE (thread_id, client_request_id);


--
-- Name: thread_control_requests uq_thread_control_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_control_requests
    ADD CONSTRAINT uq_thread_control_identity UNIQUE (id, thread_id);


--
-- Name: thread_control_requests uq_thread_control_request_seq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_control_requests
    ADD CONSTRAINT uq_thread_control_request_seq UNIQUE (thread_id, request_seq);


--
-- Name: thread_interrupt_requests uq_thread_interrupt_client_request; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_interrupt_requests
    ADD CONSTRAINT uq_thread_interrupt_client_request UNIQUE (thread_id, client_request_id);


--
-- Name: thread_interrupt_requests uq_thread_interrupt_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_interrupt_requests
    ADD CONSTRAINT uq_thread_interrupt_identity UNIQUE (id, thread_id);


--
-- Name: thread_mounts uq_thread_mount_path; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_mounts
    ADD CONSTRAINT uq_thread_mount_path UNIQUE (thread_id, target_path);


--
-- Name: thread_permission_requests uq_thread_permission_request_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_permission_requests
    ADD CONSTRAINT uq_thread_permission_request_identity UNIQUE (id, thread_id);


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
-- Name: agent_metering_binding_events_owner_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_metering_binding_events_owner_time_idx ON public.agent_metering_binding_events USING btree (owner_kind, owner_id, effective_at, id) WHERE (attribution_scope = 'customer'::text);


--
-- Name: agent_metering_binding_events_pod_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_metering_binding_events_pod_time_idx ON public.agent_metering_binding_events USING btree (pod_uid, effective_at, id) WHERE (pod_uid IS NOT NULL);


--
-- Name: agent_metering_pod_identity_state_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_metering_pod_identity_state_owner_idx ON public.agent_metering_pod_identity_state USING btree (owner_kind, owner_id) WHERE (attribution_scope = 'customer'::text);


--
-- Name: agent_metering_pod_identity_state_pod_uid_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_metering_pod_identity_state_pod_uid_idx ON public.agent_metering_pod_identity_state USING btree (pod_uid, agent_id) WHERE (agent_present AND (pod_uid IS NOT NULL));


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
-- Name: compute_epoch_authorities_current_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compute_epoch_authorities_current_idx ON public.compute_metering_epoch_authorities USING btree (activation_key, inventory_scope_id, authority_sequence DESC);


--
-- Name: compute_epoch_authorities_epoch_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compute_epoch_authorities_epoch_idx ON public.compute_metering_epoch_authorities USING btree (inventory_scope_epoch_id, activation_key, effective_from);


--
-- Name: compute_epoch_promotion_requests_activation_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compute_epoch_promotion_requests_activation_idx ON public.compute_metering_epoch_promotion_requests USING btree (activation_key, promoted_at DESC, id);


--
-- Name: compute_metering_scope_requirements_epoch_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compute_metering_scope_requirements_epoch_idx ON public.compute_metering_scope_requirements USING btree (inventory_scope_epoch_id, activation_key, required_from);


--
-- Name: compute_metering_scope_requirements_scope_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compute_metering_scope_requirements_scope_idx ON public.compute_metering_scope_requirements USING btree (inventory_scope_id, activation_key, required_from);


--
-- Name: compute_shadow_observations_latest_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compute_shadow_observations_latest_idx ON public.compute_shadow_observations USING btree (activation_key, inventory_scope_id, source_kind, source_uid, observed_at DESC);


--
-- Name: compute_shadow_observations_retention_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compute_shadow_observations_retention_idx ON public.compute_shadow_observations USING btree (observed_at, id);


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
-- Name: idx_canvas_editor_awareness_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_canvas_editor_awareness_expires_at ON public.canvas_editor_awareness USING btree (expires_at);


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
-- Name: idx_completion_effects_session_drain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_completion_effects_session_drain ON public.completion_effects USING btree (producer_kind, state, run_after, created_at);


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
-- Name: idx_job_completion_drain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_completion_drain ON public.job_completion_commands USING btree (run_after) WHERE (state = ANY (ARRAY['pending'::text, 'finalizing'::text]));


--
-- Name: idx_job_completion_sweep_actions_claim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_completion_sweep_actions_claim ON public.job_completion_sweep_actions USING btree (state, claim_expires_at, created_at) WHERE (state = ANY (ARRAY['pending'::text, 'claimed'::text]));


--
-- Name: idx_job_datasources_ds; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_datasources_ds ON public.job_datasources USING btree (datasource_id);


--
-- Name: idx_job_message_routes_job_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_message_routes_job_thread ON public.job_message_routes USING btree (job_id, thread_id, created_at DESC);


--
-- Name: idx_job_message_routes_officer_sla; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_message_routes_officer_sla ON public.job_message_routes USING btree (officer_deadline) WHERE ((state = 'pending_officer'::text) AND blocking);


--
-- Name: idx_job_message_routes_open_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_message_routes_open_project ON public.job_message_routes USING btree (project_id, created_at) WHERE (state = ANY (ARRAY['pending_officer'::text, 'pending_both'::text, 'escalated_to_user'::text, 'delivery_failed'::text]));


--
-- Name: idx_job_message_routes_total_deadline; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_message_routes_total_deadline ON public.job_message_routes USING btree (total_deadline) WHERE (blocking AND (state = ANY (ARRAY['pending_officer'::text, 'pending_both'::text, 'user_direct'::text, 'escalated_to_user'::text, 'delivery_failed'::text])));


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
-- Name: idx_knowledge_materialization_project_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_materialization_project_recent ON public.knowledge_materialization_intents USING btree (project_id, updated_at DESC);


--
-- Name: idx_knowledge_materialization_retry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_materialization_retry ON public.knowledge_materialization_intents USING btree (next_retry_at, lease_expires_at, created_at) WHERE ((canonical_state = 'pending_sync'::text) AND (retry_state = 'retryable'::text));


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
-- Name: idx_message_delivery_human_job_reserved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_message_delivery_human_job_reserved ON public.message_delivery_intents USING btree (job_id, reserved_at DESC) WHERE (bucket = 'human'::text);


--
-- Name: idx_message_delivery_human_user_reserved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_message_delivery_human_user_reserved ON public.message_delivery_intents USING btree (user_id, reserved_at DESC) WHERE (bucket = 'human'::text);


--
-- Name: idx_message_delivery_internal_job_reserved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_message_delivery_internal_job_reserved ON public.message_delivery_intents USING btree (job_id, reserved_at DESC) WHERE (bucket = 'officer_internal'::text);


--
-- Name: idx_message_delivery_internal_project_reserved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_message_delivery_internal_project_reserved ON public.message_delivery_intents USING btree (project_id, reserved_at DESC) WHERE (bucket = 'officer_internal'::text);


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
-- Name: idx_officer_floor_wake_project_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_officer_floor_wake_project_recent ON public.officer_floor_wake_episodes USING btree (project_id, created_at DESC);


--
-- Name: idx_officer_floor_wake_retry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_officer_floor_wake_retry ON public.officer_floor_wake_episodes USING btree (next_retry_at, created_at) WHERE ((state = 'retryable'::text) AND (resolved_at IS NULL));


--
-- Name: idx_officer_ticket_claims_lineage_slot_claimed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_officer_ticket_claims_lineage_slot_claimed ON public.officer_ticket_claims USING btree (officer_thread_id, officer_slot, claimed_at DESC) WHERE (officer_thread_id IS NOT NULL);


--
-- Name: idx_officer_ticket_claims_project_ticket; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_officer_ticket_claims_project_ticket ON public.officer_ticket_claims USING btree (project_id, ticket_note_id, ready_generation_at DESC, claimed_at DESC);


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
-- Name: idx_run_queue_affinity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_run_queue_affinity ON public.run_queue USING btree (last_leased_by, queued_at) WHERE (state = 'queued'::text);


--
-- Name: idx_run_queue_claim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_run_queue_claim ON public.run_queue USING btree (unit_kind, priority DESC, queued_at, enqueue_ord) WHERE (state = 'queued'::text);


--
-- Name: idx_run_queue_dedup; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_run_queue_dedup ON public.run_queue USING btree (unit_kind, dedup_key) WHERE ((dedup_key IS NOT NULL) AND (state = 'queued'::text));


--
-- Name: idx_run_queue_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_run_queue_expiry ON public.run_queue USING btree (leased_until) WHERE (state = 'leased'::text);


--
-- Name: idx_runtime_actor_access_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_runtime_actor_access_expiry ON public.runtime_actor_access_tokens USING btree (expires_at);


--
-- Name: idx_runtime_actor_bootstrap_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_runtime_actor_bootstrap_expiry ON public.runtime_actor_bootstraps USING btree (expires_at);


--
-- Name: idx_runtime_actor_grants_officer_binding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_runtime_actor_grants_officer_binding ON public.runtime_actor_grants USING btree (project_id, thread_id, officer_incarnation, created_at DESC, id DESC) WHERE ((caller_kind = 'officer'::text) AND (revoked_at IS NULL));


--
-- Name: idx_runtime_actor_grants_refresh_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_runtime_actor_grants_refresh_expiry ON public.runtime_actor_grants USING btree (refresh_expires_at) WHERE (revoked_at IS NULL);


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
-- Name: idx_thread_client_presence_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_client_presence_expires_at ON public.thread_client_presence USING btree (expires_at);


--
-- Name: idx_thread_cloud_sync_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_cloud_sync_pending ON public.thread_cloud_sync_generations USING btree (thread_id, required_generation) WHERE (acknowledged_generation < required_generation);


--
-- Name: idx_thread_control_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_control_pending ON public.thread_control_requests USING btree (thread_id, request_seq) WHERE (outcome IS NULL);


--
-- Name: idx_thread_events_control_request; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_thread_events_control_request ON public.thread_events USING btree (control_request_id) WHERE (control_request_id IS NOT NULL);


--
-- Name: idx_thread_events_interrupt_request; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_thread_events_interrupt_request ON public.thread_events USING btree (interrupt_request_id) WHERE (interrupt_request_id IS NOT NULL);


--
-- Name: idx_thread_events_permission_request; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_thread_events_permission_request ON public.thread_events USING btree (permission_request_id) WHERE (permission_request_id IS NOT NULL);


--
-- Name: idx_thread_events_thread_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_events_thread_created ON public.thread_events USING btree (thread_id, created_at);


--
-- Name: idx_thread_events_thread_epoch_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_thread_events_thread_epoch_seq ON public.thread_events USING btree (thread_id, epoch, seq);


--
-- Name: idx_thread_interrupt_pending_exact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_interrupt_pending_exact ON public.thread_interrupt_requests USING btree (thread_id, accepted_lease_token, target_turn_id, requested_at, id) WHERE (outcome IS NULL);


--
-- Name: idx_thread_messages_thread_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_messages_thread_seq ON public.thread_messages USING btree (thread_id, seq);


--
-- Name: idx_thread_messages_thread_seq_live; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_messages_thread_seq_live ON public.thread_messages USING btree (thread_id, seq) WHERE (rewound_at IS NULL);


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
-- Name: idx_thread_rewinds_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_rewinds_thread ON public.thread_rewinds USING btree (thread_id, created_at DESC);


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
-- Name: infrastructure_storage_resource_mappings_resource_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX infrastructure_storage_resource_mappings_resource_idx ON public.infrastructure_storage_resource_mappings USING btree (resource, source_cluster);


--
-- Name: jobs_lease_expiry_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX jobs_lease_expiry_idx ON public.jobs USING btree (lease_expires_at) WHERE ((status)::text = 'processing'::text);


--
-- Name: jobs_verification_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX jobs_verification_uniq ON public.jobs USING btree (parent_job_id, ((context ->> 'verification_round'::text))) WHERE (((context ->> 'verification_target'::text) IS NOT NULL) AND jsonb_exists(context, 'verification_round'::text));


--
-- Name: legacy_workspace_cutover_plans_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX legacy_workspace_cutover_plans_pending_idx ON public.legacy_workspace_cutover_plans USING btree (created_at, id) WHERE (state = 'planned'::text);


--
-- Name: resource_intervals_compute_scope_epoch_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_intervals_compute_scope_epoch_idx ON public.resource_intervals USING btree (compute_scope_epoch_id, started_at, id) WHERE (compute_scope_epoch_id IS NOT NULL);


--
-- Name: resource_intervals_materializer_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_intervals_materializer_idx ON public.resource_intervals USING btree (materialized_through, last_confirmed_at) WHERE (materialized_through < COALESCE(ended_at, last_confirmed_at));


--
-- Name: resource_intervals_open_lifecycle_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX resource_intervals_open_lifecycle_uq ON public.resource_intervals USING btree (source_lifecycle_id) WHERE (ended_at IS NULL);


--
-- Name: resource_intervals_open_scope_identity_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_intervals_open_scope_identity_idx ON public.resource_intervals USING btree (inventory_scope_id, source_kind, source_uid) WHERE (ended_at IS NULL);


--
-- Name: resource_intervals_open_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX resource_intervals_open_uq ON public.resource_intervals USING btree (source_cluster, source_kind, source_uid) WHERE (ended_at IS NULL);


--
-- Name: resource_intervals_overlap_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_intervals_overlap_idx ON public.resource_intervals USING gist (tstzrange(started_at, ended_at, '[)'::text));


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
-- Name: resource_inventory_ingest_tickets_expiry_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_ingest_tickets_expiry_idx ON public.resource_inventory_ingest_tickets USING btree (expires_at, id) WHERE (consumed_at IS NULL);


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
-- Name: resource_inventory_shadow_comparisons_latest_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_shadow_comparisons_latest_idx ON public.resource_inventory_shadow_comparisons USING btree (inventory_scope_id, source_uid, comparison_at DESC);


--
-- Name: resource_inventory_shadow_comparisons_retention_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_shadow_comparisons_retention_idx ON public.resource_inventory_shadow_comparisons USING btree (comparison_at, id);


--
-- Name: resource_inventory_shadow_comparisons_unresolved_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_shadow_comparisons_unresolved_idx ON public.resource_inventory_shadow_comparisons USING btree (inventory_scope_id, comparison_at DESC, snapshot_id) WHERE (explained = false);


--
-- Name: resource_inventory_snapshots_complete_received_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_snapshots_complete_received_idx ON public.resource_inventory_snapshots USING btree (scope_epoch_id, received_at, id) WHERE ((complete IS TRUE) AND (manifest_state = ANY (ARRAY['sealed'::text, 'items-expired'::text])));


--
-- Name: resource_inventory_snapshots_controller_seq_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX resource_inventory_snapshots_controller_seq_uq ON public.resource_inventory_snapshots USING btree (scope_epoch_id, controller_epoch, sequence) WHERE ((controller_epoch IS NOT NULL) AND (sequence IS NOT NULL));


--
-- Name: resource_inventory_snapshots_scope_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_snapshots_scope_time_idx ON public.resource_inventory_snapshots USING btree (scope_epoch_id, collection_completed_at DESC);


--
-- Name: resource_inventory_snapshots_sealed_retention_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_snapshots_sealed_retention_idx ON public.resource_inventory_snapshots USING btree (sealed_at, id) WHERE (manifest_state = 'sealed'::text);


--
-- Name: resource_inventory_snapshots_staging_retention_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_snapshots_staging_retention_idx ON public.resource_inventory_snapshots USING btree (created_at, id) WHERE (manifest_state = 'staging'::text);


--
-- Name: resource_inventory_transport_nonces_expiry_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_transport_nonces_expiry_idx ON public.resource_inventory_transport_nonces USING btree (expires_at, collector_id, request_nonce);


--
-- Name: resource_inventory_watch_events_gap_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_watch_events_gap_idx ON public.resource_inventory_watch_events USING btree (coverage_gap_id) WHERE (coverage_gap_id IS NOT NULL);


--
-- Name: resource_inventory_watch_events_invalid_received_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_watch_events_invalid_received_idx ON public.resource_inventory_watch_events USING btree (scope_epoch_id, received_at, id) WHERE ((valid_for_metering IS FALSE) AND (mutation_action = 'presence-invalid'::text));


--
-- Name: resource_inventory_watch_events_scope_uid_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_watch_events_scope_uid_idx ON public.resource_inventory_watch_events USING btree (scope_epoch_id, source_kind, source_uid, received_at DESC) WHERE (source_uid IS NOT NULL);


--
-- Name: resource_inventory_watch_sessions_live_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_watch_sessions_live_idx ON public.resource_inventory_watch_sessions USING btree (expires_at, id) WHERE (consumed_at IS NULL);


--
-- Name: resource_inventory_watch_sessions_live_scope_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_watch_sessions_live_scope_idx ON public.resource_inventory_watch_sessions USING btree (scope_epoch_id, created_at, id) WHERE (consumed_at IS NULL);


--
-- Name: resource_inventory_watch_sessions_retention_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_inventory_watch_sessions_retention_idx ON public.resource_inventory_watch_sessions USING btree (COALESCE(consumed_at, expires_at), id);


--
-- Name: resource_publication_plan_events_rate_reference_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_publication_plan_events_rate_reference_idx ON public.resource_publication_plan_events USING btree (canonical_rate_version_id, plan_id) WHERE (canonical_rate_version_id IS NOT NULL);


--
-- Name: resource_publication_plans_interval_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_publication_plans_interval_idx ON public.resource_publication_plans USING btree (source_interval_id, period_start);


--
-- Name: resource_publication_plans_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_publication_plans_pending_idx ON public.resource_publication_plans USING btree (created_at, id) WHERE (state = 'planned'::text);


--
-- Name: resource_publication_plans_period_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_publication_plans_period_idx ON public.resource_publication_plans USING gist (tstzrange(period_start, period_end, '[)'::text));


--
-- Name: schema_migrations_dirty_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX schema_migrations_dirty_idx ON public.schema_migrations USING btree (filename) WHERE (success = false);


--
-- Name: storage_asset_coverage_gaps_open_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX storage_asset_coverage_gaps_open_uq ON public.storage_asset_coverage_gaps USING btree (asset_id) WHERE (resolution = 'unresolved'::text);


--
-- Name: storage_asset_coverage_gaps_range_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX storage_asset_coverage_gaps_range_idx ON public.storage_asset_coverage_gaps USING btree (asset_id, gap_start, gap_end, id);


--
-- Name: storage_metering_source_requirements_scope_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX storage_metering_source_requirements_scope_idx ON public.storage_metering_source_requirements USING btree (inventory_scope_id, measurement_basis, requirement_role);


--
-- Name: storage_shadow_observations_latest_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX storage_shadow_observations_latest_idx ON public.storage_shadow_observations USING btree (inventory_scope_id, source_kind, source_uid, observed_at DESC);


--
-- Name: storage_shadow_observations_retention_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX storage_shadow_observations_retention_idx ON public.storage_shadow_observations USING btree (observed_at, id);


--
-- Name: storage_volume_assets_state_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX storage_volume_assets_state_idx ON public.storage_volume_assets USING btree (lifecycle_state, source_cluster, id);


--
-- Name: storage_volume_incarnations_active_asset_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX storage_volume_incarnations_active_asset_uq ON public.storage_volume_incarnations USING btree (asset_id) WHERE (detached_at IS NULL);


--
-- Name: storage_volume_incarnations_asset_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX storage_volume_incarnations_asset_time_idx ON public.storage_volume_incarnations USING btree (asset_id, first_observed_at, detached_at, id);


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
-- Name: uq_jobs_active_ticket_claim; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_jobs_active_ticket_claim ON public.jobs USING btree (project_id, ((context ->> 'ticket_note_id'::text))) WHERE ((context ? 'ticket_note_id'::text) AND ((status)::text <> ALL ((ARRAY['completed'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])));


--
-- Name: uq_knowledge_materialization_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_knowledge_materialization_unresolved ON public.knowledge_materialization_intents USING btree (project_id, note_id, content_hash) WHERE (canonical_state = ANY (ARRAY['pending_sync'::text, 'failed'::text]));


--
-- Name: uq_llm_endpoint_label_system; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_llm_endpoint_label_system ON public.llm_endpoints USING btree (label) WHERE (user_id IS NULL);


--
-- Name: uq_llm_endpoint_label_user; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_llm_endpoint_label_user ON public.llm_endpoints USING btree (user_id, label) WHERE (user_id IS NOT NULL);


--
-- Name: uq_officer_floor_wake_active_episode; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_officer_floor_wake_active_episode ON public.officer_floor_wake_episodes USING btree (project_id, officer_incarnation, pool) WHERE (resolved_at IS NULL);


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
-- Name: uq_runtime_actor_grants_live_officer_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_runtime_actor_grants_live_officer_agent ON public.runtime_actor_grants USING btree (agent_id) WHERE ((caller_kind = 'officer'::text) AND (revoked_at IS NULL) AND (agent_id IS NOT NULL));


--
-- Name: uq_runtime_actor_grants_previous_refresh_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_runtime_actor_grants_previous_refresh_hash ON public.runtime_actor_grants USING btree (previous_refresh_token_hash) WHERE (previous_refresh_token_hash IS NOT NULL);


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
-- Name: agents agent_metering_agents_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_agents_delete AFTER DELETE ON public.agents FOR EACH ROW EXECUTE FUNCTION public.converge_agent_metering_from_agent_row();


--
-- Name: agents agent_metering_agents_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_agents_insert AFTER INSERT ON public.agents FOR EACH ROW EXECUTE FUNCTION public.converge_agent_metering_from_agent_row();


--
-- Name: agents agent_metering_agents_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_agents_update AFTER UPDATE OF pod_uid, hostname, current_job_id, thread_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.converge_agent_metering_from_agent_row();


--
-- Name: agent_metering_binding_events agent_metering_binding_events_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_binding_events_append_only BEFORE DELETE OR UPDATE ON public.agent_metering_binding_events FOR EACH ROW EXECUTE FUNCTION public.protect_agent_metering_binding_event_mutation();


--
-- Name: jobs agent_metering_jobs_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_jobs_delete AFTER DELETE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.converge_agent_metering_from_job_row();


--
-- Name: jobs agent_metering_jobs_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_jobs_insert AFTER INSERT ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.converge_agent_metering_from_job_row();


--
-- Name: jobs agent_metering_jobs_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_jobs_update AFTER UPDATE OF status, assigned_agent_id, user_id, project_id ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.converge_agent_metering_from_job_row();


--
-- Name: agent_metering_pod_identity_state agent_metering_pod_identity_state_journal; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_pod_identity_state_journal AFTER INSERT OR UPDATE ON public.agent_metering_pod_identity_state FOR EACH ROW EXECUTE FUNCTION public.append_agent_metering_binding_event();


--
-- Name: agent_metering_pod_identity_state agent_metering_pod_identity_state_one_way; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_pod_identity_state_one_way BEFORE INSERT OR DELETE OR UPDATE ON public.agent_metering_pod_identity_state FOR EACH ROW EXECUTE FUNCTION public.protect_agent_metering_identity_state_mutation();


--
-- Name: threads agent_metering_threads_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_threads_delete AFTER DELETE ON public.threads FOR EACH ROW EXECUTE FUNCTION public.converge_agent_metering_from_thread_row();


--
-- Name: threads agent_metering_threads_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_threads_insert AFTER INSERT ON public.threads FOR EACH ROW EXECUTE FUNCTION public.converge_agent_metering_from_thread_row();


--
-- Name: threads agent_metering_threads_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_metering_threads_update AFTER UPDATE OF status, agent_id, user_id, project_id ON public.threads FOR EACH ROW EXECUTE FUNCTION public.converge_agent_metering_from_thread_row();


--
-- Name: compute_metering_epoch_authorities compute_epoch_authority_confirmation_gap; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER compute_epoch_authority_confirmation_gap AFTER INSERT ON public.compute_metering_epoch_authorities FOR EACH ROW EXECUTE FUNCTION public.record_compute_authority_confirmation_gap();


--
-- Name: compute_metering_epoch_promotion_requests compute_epoch_promotion_requests_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER compute_epoch_promotion_requests_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.compute_metering_epoch_promotion_requests FOR EACH ROW EXECUTE FUNCTION public.protect_compute_epoch_promotion_request();


--
-- Name: compute_metering_activation compute_metering_activation_one_way; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER compute_metering_activation_one_way BEFORE INSERT OR DELETE OR UPDATE ON public.compute_metering_activation FOR EACH ROW EXECUTE FUNCTION public.protect_compute_metering_activation();


--
-- Name: compute_metering_epoch_authorities compute_metering_epoch_authorities_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER compute_metering_epoch_authorities_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.compute_metering_epoch_authorities FOR EACH ROW EXECUTE FUNCTION public.protect_compute_metering_epoch_authority();


--
-- Name: compute_metering_scope_requirements compute_metering_scope_requirements_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER compute_metering_scope_requirements_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.compute_metering_scope_requirements FOR EACH ROW EXECUTE FUNCTION public.protect_compute_metering_scope_requirement();


--
-- Name: compute_shadow_observations compute_shadow_observations_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER compute_shadow_observations_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.compute_shadow_observations FOR EACH ROW EXECUTE FUNCTION public.protect_compute_shadow_observation_mutation();


--
-- Name: project_datasources datasource_project_policy_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER datasource_project_policy_change AFTER INSERT OR DELETE OR UPDATE ON public.project_datasources FOR EACH ROW EXECUTE FUNCTION public.reconcile_datasource_project_policy_change();


--
-- Name: infra_metering_control infra_metering_control_cutover_one_way; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER infra_metering_control_cutover_one_way BEFORE DELETE OR UPDATE ON public.infra_metering_control FOR EACH ROW EXECUTE FUNCTION public.protect_infra_metering_cutover_mutation();


--
-- Name: infra_metering_control infra_metering_control_legacy_drain_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER infra_metering_control_legacy_drain_immutable BEFORE UPDATE OF legacy_drained_at ON public.infra_metering_control FOR EACH ROW EXECUTE FUNCTION public.protect_infra_metering_legacy_drain_completion();


--
-- Name: infra_metering_control infra_metering_control_monotonic_generation; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER infra_metering_control_monotonic_generation BEFORE DELETE OR UPDATE ON public.infra_metering_control FOR EACH ROW EXECUTE FUNCTION public.protect_infra_metering_generation_mutation();


--
-- Name: infra_usage_day_state infra_usage_day_state_one_way_seal; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER infra_usage_day_state_one_way_seal BEFORE INSERT OR DELETE OR UPDATE ON public.infra_usage_day_state FOR EACH ROW EXECUTE FUNCTION public.protect_infra_usage_day_state_mutation();


--
-- Name: infrastructure_storage_resource_mappings infrastructure_storage_resource_mappings_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER infrastructure_storage_resource_mappings_append_only BEFORE DELETE OR UPDATE ON public.infrastructure_storage_resource_mappings FOR EACH ROW EXECUTE FUNCTION public.protect_infrastructure_storage_resource_mapping();


--
-- Name: legacy_workspace_cutover_plan_events legacy_workspace_cutover_plan_event_manifest_complete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER legacy_workspace_cutover_plan_event_manifest_complete AFTER INSERT ON public.legacy_workspace_cutover_plan_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_legacy_workspace_cutover_plan_manifest();


--
-- Name: legacy_workspace_cutover_plan_events legacy_workspace_cutover_plan_events_frozen; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER legacy_workspace_cutover_plan_events_frozen BEFORE INSERT OR DELETE OR UPDATE ON public.legacy_workspace_cutover_plan_events FOR EACH ROW EXECUTE FUNCTION public.protect_legacy_workspace_cutover_plan_event_mutation();


--
-- Name: legacy_workspace_cutover_plans legacy_workspace_cutover_plan_manifest_complete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER legacy_workspace_cutover_plan_manifest_complete AFTER INSERT OR UPDATE ON public.legacy_workspace_cutover_plans DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_legacy_workspace_cutover_plan_manifest();


--
-- Name: legacy_workspace_cutover_plans legacy_workspace_cutover_plans_frozen; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER legacy_workspace_cutover_plans_frozen BEFORE DELETE OR UPDATE ON public.legacy_workspace_cutover_plans FOR EACH ROW EXECUTE FUNCTION public.protect_legacy_workspace_cutover_plan_mutation();


--
-- Name: message_log mirror_legacy_message_delivery_intent; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER mirror_legacy_message_delivery_intent AFTER INSERT ON public.message_log FOR EACH ROW EXECUTE FUNCTION public.mirror_legacy_message_delivery_intent();


--
-- Name: jobs officer_ticket_claim_job_delete_audit; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER officer_ticket_claim_job_delete_audit BEFORE DELETE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.audit_officer_ticket_claim_job_delete();


--
-- Name: jobs officer_ticket_claim_job_integrity; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER officer_ticket_claim_job_integrity AFTER INSERT OR UPDATE OF id, context, project_id, created_by_thread_id ON public.jobs DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION public.enforce_officer_ticket_claim_job_integrity();


--
-- Name: resource_intervals resource_intervals_compute_epoch_authority_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_intervals_compute_epoch_authority_guard BEFORE INSERT OR UPDATE ON public.resource_intervals FOR EACH ROW EXECUTE FUNCTION public.enforce_resource_interval_compute_epoch_authority();


--
-- Name: resource_intervals resource_intervals_cutover_serialization; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_intervals_cutover_serialization BEFORE INSERT OR UPDATE ON public.resource_intervals FOR EACH STATEMENT EXECUTE FUNCTION public.serialize_resource_interval_statement_with_cutover();


--
-- Name: resource_intervals resource_intervals_immutable_revision; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_intervals_immutable_revision BEFORE DELETE OR UPDATE ON public.resource_intervals FOR EACH ROW EXECUTE FUNCTION public.protect_resource_interval_revision_mutation();


--
-- Name: resource_intervals resource_intervals_scope_identity; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_intervals_scope_identity BEFORE INSERT OR UPDATE OF inventory_scope_id, source_cluster, namespace, last_seen_snapshot_id ON public.resource_intervals FOR EACH ROW EXECUTE FUNCTION public.validate_resource_interval_scope_identity();


--
-- Name: resource_intervals resource_intervals_snapshot_end_single_boundary_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_intervals_snapshot_end_single_boundary_guard BEFORE UPDATE OF last_seen_at, last_seen_snapshot_id ON public.resource_intervals FOR EACH ROW EXECUTE FUNCTION public.validate_resource_interval_snapshot_end_evidence();


--
-- Name: resource_intervals resource_intervals_storage_activation_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_intervals_storage_activation_guard BEFORE INSERT ON public.resource_intervals FOR EACH ROW EXECUTE FUNCTION public.enforce_resource_interval_storage_activation();


--
-- Name: resource_inventory_scope_epochs resource_inventory_epochs_compute_retirement; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_epochs_compute_retirement BEFORE UPDATE OF retired_at ON public.resource_inventory_scope_epochs FOR EACH ROW EXECUTE FUNCTION public.close_compute_intervals_at_epoch_retirement();


--
-- Name: resource_inventory_scope_epochs resource_inventory_epochs_recovery_identity_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_epochs_recovery_identity_immutable BEFORE DELETE OR UPDATE ON public.resource_inventory_scope_epochs FOR EACH ROW EXECUTE FUNCTION public.protect_inventory_epoch_recovery_identity();


--
-- Name: resource_inventory_ingest_tickets resource_inventory_ingest_tickets_one_way; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_ingest_tickets_one_way BEFORE INSERT OR DELETE OR UPDATE ON public.resource_inventory_ingest_tickets FOR EACH ROW EXECUTE FUNCTION public.protect_resource_inventory_ingest_ticket_mutation();


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_boundary_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_scope_epochs_boundary_insert BEFORE INSERT ON public.resource_inventory_scope_epochs FOR EACH ROW EXECUTE FUNCTION public.enforce_inventory_epoch_required_boundary();


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_boundary_insert_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_scope_epochs_boundary_insert_lock BEFORE INSERT ON public.resource_inventory_scope_epochs FOR EACH STATEMENT EXECUTE FUNCTION public.lock_inventory_epoch_boundary_statement();


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_boundary_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_scope_epochs_boundary_update BEFORE UPDATE OF required_for_rollup, required_from ON public.resource_inventory_scope_epochs FOR EACH ROW EXECUTE FUNCTION public.enforce_inventory_epoch_required_boundary();


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_boundary_update_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_scope_epochs_boundary_update_lock BEFORE UPDATE OF required_for_rollup, required_from ON public.resource_inventory_scope_epochs FOR EACH STATEMENT EXECUTE FUNCTION public.lock_inventory_epoch_boundary_statement();


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_complete_snapshot; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_scope_epochs_complete_snapshot BEFORE INSERT OR UPDATE OF last_complete_snapshot_id ON public.resource_inventory_scope_epochs FOR EACH ROW EXECUTE FUNCTION public.validate_inventory_epoch_last_complete_snapshot();


--
-- Name: resource_inventory_shadow_comparisons resource_inventory_shadow_comparisons_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_shadow_comparisons_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.resource_inventory_shadow_comparisons FOR EACH ROW EXECUTE FUNCTION public.protect_resource_inventory_shadow_comparison_mutation();


--
-- Name: resource_inventory_snapshot_items resource_inventory_snapshot_items_generation_fence; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_snapshot_items_generation_fence BEFORE INSERT OR UPDATE ON public.resource_inventory_snapshot_items FOR EACH ROW EXECUTE FUNCTION public.enforce_resource_inventory_item_fence();


--
-- Name: resource_inventory_snapshot_items resource_inventory_snapshot_items_staging_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_snapshot_items_staging_only BEFORE INSERT OR DELETE OR UPDATE ON public.resource_inventory_snapshot_items FOR EACH ROW EXECUTE FUNCTION public.protect_resource_inventory_snapshot_item_mutation();


--
-- Name: resource_inventory_snapshots resource_inventory_snapshots_generation_fence; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_snapshots_generation_fence BEFORE INSERT OR UPDATE ON public.resource_inventory_snapshots FOR EACH ROW EXECUTE FUNCTION public.enforce_resource_inventory_snapshot_fence();


--
-- Name: resource_inventory_snapshots resource_inventory_snapshots_seal_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_snapshots_seal_only BEFORE INSERT OR UPDATE ON public.resource_inventory_snapshots FOR EACH ROW EXECUTE FUNCTION public.protect_resource_inventory_snapshot_mutation();


--
-- Name: resource_inventory_transport_nonces resource_inventory_transport_nonces_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_transport_nonces_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.resource_inventory_transport_nonces FOR EACH ROW EXECUTE FUNCTION public.protect_resource_inventory_transport_nonce_mutation();


--
-- Name: resource_inventory_watch_events resource_inventory_watch_events_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_watch_events_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.resource_inventory_watch_events FOR EACH ROW EXECUTE FUNCTION public.protect_resource_inventory_watch_event_mutation();


--
-- Name: resource_inventory_watch_events resource_inventory_watch_events_terminal_evidence_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_watch_events_terminal_evidence_guard BEFORE INSERT ON public.resource_inventory_watch_events FOR EACH ROW EXECUTE FUNCTION public.validate_inventory_watch_terminal_interval_evidence();


--
-- Name: resource_inventory_watch_sessions resource_inventory_watch_sessions_one_way; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_inventory_watch_sessions_one_way BEFORE INSERT OR DELETE OR UPDATE ON public.resource_inventory_watch_sessions FOR EACH ROW EXECUTE FUNCTION public.protect_resource_inventory_watch_session_mutation();


--
-- Name: resource_lifecycle_heads resource_lifecycle_heads_cutover_serialization; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER resource_lifecycle_heads_cutover_serialization BEFORE INSERT OR UPDATE ON public.resource_lifecycle_heads FOR EACH STATEMENT EXECUTE FUNCTION public.serialize_resource_lifecycle_head_with_cutover();


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
-- Name: threads settle_job_wakes_before_thread_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER settle_job_wakes_before_thread_delete BEFORE DELETE ON public.threads FOR EACH ROW EXECUTE FUNCTION public.settle_job_wakes_before_thread_delete();


--
-- Name: storage_asset_coverage_gaps storage_asset_coverage_gaps_lifecycle_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_asset_coverage_gaps_lifecycle_guard BEFORE INSERT OR DELETE OR UPDATE ON public.storage_asset_coverage_gaps FOR EACH ROW EXECUTE FUNCTION public.protect_storage_asset_coverage_gap_mutation();


--
-- Name: storage_asset_coverage_gaps storage_asset_coverage_gaps_transition; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_asset_coverage_gaps_transition AFTER INSERT OR UPDATE OF resolution ON public.storage_asset_coverage_gaps FOR EACH ROW EXECUTE FUNCTION public.transition_storage_asset_for_gap();


--
-- Name: storage_backend_assertions storage_backend_assertions_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_backend_assertions_append_only BEFORE INSERT OR DELETE OR UPDATE ON public.storage_backend_assertions FOR EACH ROW EXECUTE FUNCTION public.protect_storage_backend_assertion_mutation();


--
-- Name: storage_backend_assertions storage_backend_assertions_transition; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_backend_assertions_transition AFTER INSERT ON public.storage_backend_assertions FOR EACH ROW EXECUTE FUNCTION public.transition_storage_asset_for_destruction();


--
-- Name: storage_identity_key_state storage_identity_key_state_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_identity_key_state_immutable BEFORE DELETE OR UPDATE ON public.storage_identity_key_state FOR EACH ROW EXECUTE FUNCTION public.protect_storage_identity_key_state();


--
-- Name: storage_metering_activation storage_metering_activation_one_way; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_metering_activation_one_way BEFORE INSERT OR DELETE OR UPDATE ON public.storage_metering_activation FOR EACH ROW EXECUTE FUNCTION public.protect_storage_metering_activation();


--
-- Name: storage_metering_source_activations storage_metering_source_activations_one_way; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_metering_source_activations_one_way BEFORE INSERT OR DELETE OR UPDATE ON public.storage_metering_source_activations FOR EACH ROW EXECUTE FUNCTION public.protect_storage_metering_source_activation();


--
-- Name: storage_metering_source_requirements storage_metering_source_requirements_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_metering_source_requirements_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.storage_metering_source_requirements FOR EACH ROW EXECUTE FUNCTION public.protect_storage_metering_source_requirement();


--
-- Name: storage_shadow_observations storage_shadow_observations_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_shadow_observations_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.storage_shadow_observations FOR EACH ROW EXECUTE FUNCTION public.protect_storage_shadow_observation_mutation();


--
-- Name: storage_volume_assets storage_volume_assets_lifecycle_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_volume_assets_lifecycle_guard BEFORE DELETE OR UPDATE ON public.storage_volume_assets FOR EACH ROW EXECUTE FUNCTION public.protect_storage_volume_asset_mutation();


--
-- Name: storage_volume_incarnations storage_volume_incarnations_lifecycle_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER storage_volume_incarnations_lifecycle_guard BEFORE DELETE OR UPDATE ON public.storage_volume_incarnations FOR EACH ROW EXECUTE FUNCTION public.protect_storage_volume_incarnation_mutation();


--
-- Name: thread_control_requests thread_control_request_notify_trigger; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER thread_control_request_notify_trigger AFTER INSERT ON public.thread_control_requests FOR EACH ROW EXECUTE FUNCTION public.notify_thread_control_request();


--
-- Name: thread_interrupt_requests thread_interrupt_request_notify_trigger; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER thread_interrupt_request_notify_trigger AFTER INSERT ON public.thread_interrupt_requests FOR EACH ROW EXECUTE FUNCTION public.notify_thread_interrupt_request();


--
-- Name: thread_permission_requests thread_permission_notify_trigger; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER thread_permission_notify_trigger AFTER UPDATE ON public.thread_permission_requests FOR EACH ROW EXECUTE FUNCTION public.notify_thread_permission_update();


--
-- Name: agents trg_agents_revoke_runtime_actor_grants; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agents_revoke_runtime_actor_grants BEFORE DELETE ON public.agents FOR EACH ROW EXECUTE FUNCTION public.revoke_runtime_actor_grants_on_agent_delete();


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
-- Name: runtime_actor_grants trg_runtime_actor_grants_officer_agent_binding; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_runtime_actor_grants_officer_agent_binding BEFORE INSERT OR UPDATE ON public.runtime_actor_grants FOR EACH ROW EXECUTE FUNCTION public.enforce_officer_runtime_agent_binding();


--
-- Name: datasources update_datasources_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_datasources_updated_at BEFORE UPDATE ON public.datasources FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: job_message_routes update_job_message_routes_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_job_message_routes_updated_at BEFORE UPDATE ON public.job_message_routes FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: jobs update_jobs_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_jobs_updated_at BEFORE UPDATE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: project_api_keys update_project_api_keys_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_project_api_keys_updated_at BEFORE UPDATE ON public.project_api_keys FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: project_officers update_project_officers_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_project_officers_updated_at BEFORE UPDATE ON public.project_officers FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


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
-- Name: usage_rates_v2 usage_rates_v2_referenced_range_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER usage_rates_v2_referenced_range_guard BEFORE UPDATE OF effective_to ON public.usage_rates_v2 FOR EACH ROW EXECUTE FUNCTION public.protect_usage_rate_v2_referenced_range();


--
-- Name: workspace_intervals workspace_intervals_cutover_insert_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER workspace_intervals_cutover_insert_lock BEFORE INSERT ON public.workspace_intervals FOR EACH STATEMENT EXECUTE FUNCTION public.lock_legacy_workspace_insert_statement();


--
-- Name: workspace_intervals workspace_intervals_cutover_open_barrier; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER workspace_intervals_cutover_open_barrier BEFORE INSERT OR UPDATE ON public.workspace_intervals FOR EACH ROW EXECUTE FUNCTION public.enforce_legacy_workspace_cutover_barrier();


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
-- Name: compute_metering_epoch_authorities compute_epoch_authorities_epoch_scope_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_epoch_authorities_epoch_scope_fkey FOREIGN KEY (inventory_scope_epoch_id, inventory_scope_id) REFERENCES public.resource_inventory_scope_epochs(id, scope_id) ON DELETE RESTRICT;


--
-- Name: compute_metering_epoch_authorities compute_epoch_authorities_predecessor_scope_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_epoch_authorities_predecessor_scope_fkey FOREIGN KEY (predecessor_epoch_id, inventory_scope_id) REFERENCES public.resource_inventory_scope_epochs(id, scope_id) ON DELETE RESTRICT;


--
-- Name: compute_metering_epoch_authorities compute_epoch_authorities_previous_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_epoch_authorities_previous_fkey FOREIGN KEY (previous_authority_id, activation_key, inventory_scope_id) REFERENCES public.compute_metering_epoch_authorities(id, activation_key, inventory_scope_id) ON DELETE RESTRICT;


--
-- Name: compute_metering_epoch_authorities compute_epoch_authorities_proof_epoch_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_epoch_authorities_proof_epoch_fkey FOREIGN KEY (proof_snapshot_id, inventory_scope_epoch_id) REFERENCES public.resource_inventory_snapshots(id, scope_epoch_id) ON DELETE RESTRICT;


--
-- Name: compute_metering_epoch_authorities compute_epoch_authorities_proof_scope_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_epoch_authorities_proof_scope_fkey FOREIGN KEY (proof_snapshot_id, inventory_scope_id) REFERENCES public.resource_inventory_snapshots(id, inventory_scope_id) ON DELETE RESTRICT;


--
-- Name: compute_metering_epoch_authorities compute_epoch_authorities_scope_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_epoch_authorities_scope_fkey FOREIGN KEY (inventory_scope_id, collector_id, source_cluster) REFERENCES public.resource_inventory_scopes(id, collector_id, source_cluster) ON DELETE RESTRICT;


--
-- Name: compute_metering_epoch_authorities compute_metering_epoch_authorities_activation_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_metering_epoch_authorities_activation_key_fkey FOREIGN KEY (activation_key) REFERENCES public.compute_metering_activation(activation_key) ON DELETE RESTRICT;


--
-- Name: compute_metering_epoch_authorities compute_metering_epoch_authorities_promotion_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_authorities
    ADD CONSTRAINT compute_metering_epoch_authorities_promotion_request_id_fkey FOREIGN KEY (promotion_request_id) REFERENCES public.compute_metering_epoch_promotion_requests(id) ON DELETE RESTRICT;


--
-- Name: compute_metering_epoch_promotion_requests compute_metering_epoch_promotion_requests_activation_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_epoch_promotion_requests
    ADD CONSTRAINT compute_metering_epoch_promotion_requests_activation_key_fkey FOREIGN KEY (activation_key) REFERENCES public.compute_metering_activation(activation_key) ON DELETE RESTRICT;


--
-- Name: compute_metering_scope_requirements compute_metering_scope_requirements_activation_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_scope_requirements
    ADD CONSTRAINT compute_metering_scope_requirements_activation_fkey FOREIGN KEY (activation_key) REFERENCES public.compute_metering_activation(activation_key) ON DELETE RESTRICT;


--
-- Name: compute_metering_scope_requirements compute_metering_scope_requirements_epoch_scope_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_scope_requirements
    ADD CONSTRAINT compute_metering_scope_requirements_epoch_scope_fkey FOREIGN KEY (inventory_scope_epoch_id, inventory_scope_id) REFERENCES public.resource_inventory_scope_epochs(id, scope_id) ON DELETE RESTRICT;


--
-- Name: compute_metering_scope_requirements compute_metering_scope_requirements_scope_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_metering_scope_requirements
    ADD CONSTRAINT compute_metering_scope_requirements_scope_fkey FOREIGN KEY (inventory_scope_id, collector_id, source_cluster) REFERENCES public.resource_inventory_scopes(id, collector_id, source_cluster) ON DELETE RESTRICT;


--
-- Name: compute_shadow_observations compute_shadow_observations_activation_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_shadow_observations
    ADD CONSTRAINT compute_shadow_observations_activation_key_fkey FOREIGN KEY (activation_key) REFERENCES public.compute_metering_activation(activation_key) ON DELETE RESTRICT;


--
-- Name: compute_shadow_observations compute_shadow_observations_snapshot_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compute_shadow_observations
    ADD CONSTRAINT compute_shadow_observations_snapshot_fkey FOREIGN KEY (snapshot_id, inventory_scope_id) REFERENCES public.resource_inventory_snapshots(id, inventory_scope_id) ON DELETE RESTRICT;


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
-- Name: canvas_editor_awareness fk_canvas_editor_awareness_canvas; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_editor_awareness
    ADD CONSTRAINT fk_canvas_editor_awareness_canvas FOREIGN KEY (thread_id, canvas_id) REFERENCES public.canvases(thread_id, canvas_id) ON DELETE CASCADE;


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
-- Name: job_completion_commands job_completion_commands_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_completion_commands
    ADD CONSTRAINT job_completion_commands_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: job_completion_sweep_actions job_completion_sweep_actions_command_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_completion_sweep_actions
    ADD CONSTRAINT job_completion_sweep_actions_command_id_fkey FOREIGN KEY (command_id) REFERENCES public.job_completion_commands(id) ON DELETE CASCADE;


--
-- Name: job_completion_sweep_actions job_completion_sweep_actions_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_completion_sweep_actions
    ADD CONSTRAINT job_completion_sweep_actions_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


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
-- Name: job_message_routes job_message_routes_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_message_routes
    ADD CONSTRAINT job_message_routes_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: job_message_routes job_message_routes_officer_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_message_routes
    ADD CONSTRAINT job_message_routes_officer_thread_id_fkey FOREIGN KEY (officer_thread_id) REFERENCES public.threads(id) ON DELETE SET NULL;


--
-- Name: job_message_routes job_message_routes_originating_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_message_routes
    ADD CONSTRAINT job_message_routes_originating_message_id_fkey FOREIGN KEY (originating_message_id) REFERENCES public.message_log(id) ON DELETE SET NULL;


--
-- Name: job_message_routes job_message_routes_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_message_routes
    ADD CONSTRAINT job_message_routes_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


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
-- Name: knowledge_materialization_intents knowledge_materialization_intents_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_materialization_intents
    ADD CONSTRAINT knowledge_materialization_intents_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: legacy_workspace_cutover_plan_events legacy_workspace_cutover_plan_events_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legacy_workspace_cutover_plan_events
    ADD CONSTRAINT legacy_workspace_cutover_plan_events_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.legacy_workspace_cutover_plans(id) ON DELETE RESTRICT;


--
-- Name: legacy_workspace_cutover_plans legacy_workspace_cutover_plans_workspace_interval_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legacy_workspace_cutover_plans
    ADD CONSTRAINT legacy_workspace_cutover_plans_workspace_interval_id_fkey FOREIGN KEY (workspace_interval_id) REFERENCES public.workspace_intervals(id) ON DELETE RESTRICT;


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
-- Name: message_delivery_attempts message_delivery_attempts_intent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_delivery_attempts
    ADD CONSTRAINT message_delivery_attempts_intent_id_fkey FOREIGN KEY (intent_id) REFERENCES public.message_delivery_intents(intent_id) ON DELETE CASCADE;


--
-- Name: message_delivery_intents message_delivery_intents_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_delivery_intents
    ADD CONSTRAINT message_delivery_intents_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: message_delivery_intents message_delivery_intents_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_delivery_intents
    ADD CONSTRAINT message_delivery_intents_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: message_delivery_intents message_delivery_intents_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_delivery_intents
    ADD CONSTRAINT message_delivery_intents_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


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
-- Name: officer_floor_wake_episodes officer_floor_wake_episodes_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.officer_floor_wake_episodes
    ADD CONSTRAINT officer_floor_wake_episodes_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: officer_floor_wake_episodes officer_floor_wake_episodes_wake_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.officer_floor_wake_episodes
    ADD CONSTRAINT officer_floor_wake_episodes_wake_event_id_fkey FOREIGN KEY (wake_event_id) REFERENCES public.session_wake_events(id) ON DELETE SET NULL;


--
-- Name: officer_ticket_claims officer_ticket_claims_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.officer_ticket_claims
    ADD CONSTRAINT officer_ticket_claims_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


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
-- Name: project_officers project_officers_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_officers
    ADD CONSTRAINT project_officers_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_officers project_officers_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_officers
    ADD CONSTRAINT project_officers_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE SET NULL;


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
-- Name: resource_intervals resource_intervals_compute_scope_epoch_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_intervals
    ADD CONSTRAINT resource_intervals_compute_scope_epoch_fkey FOREIGN KEY (compute_scope_epoch_id, inventory_scope_id) REFERENCES public.resource_inventory_scope_epochs(id, scope_id) ON DELETE RESTRICT;


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
-- Name: resource_inventory_ingest_tickets resource_inventory_ingest_tickets_scope_epoch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_ingest_tickets
    ADD CONSTRAINT resource_inventory_ingest_tickets_scope_epoch_id_fkey FOREIGN KEY (scope_epoch_id) REFERENCES public.resource_inventory_scope_epochs(id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_ingest_tickets resource_inventory_ingest_tickets_snapshot_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_ingest_tickets
    ADD CONSTRAINT resource_inventory_ingest_tickets_snapshot_fkey FOREIGN KEY (bound_snapshot_id, scope_epoch_id) REFERENCES public.resource_inventory_snapshots(id, scope_epoch_id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_last_snapshot_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scope_epochs
    ADD CONSTRAINT resource_inventory_scope_epochs_last_snapshot_fkey FOREIGN KEY (last_complete_snapshot_id, id) REFERENCES public.resource_inventory_snapshots(id, scope_epoch_id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_recovery_from_epoch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scope_epochs
    ADD CONSTRAINT resource_inventory_scope_epochs_recovery_from_epoch_id_fkey FOREIGN KEY (recovery_from_epoch_id) REFERENCES public.resource_inventory_scope_epochs(id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_scope_epochs resource_inventory_scope_epochs_scope_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_scope_epochs
    ADD CONSTRAINT resource_inventory_scope_epochs_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES public.resource_inventory_scopes(id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_shadow_comparisons resource_inventory_shadow_comparisons_snapshot_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_shadow_comparisons
    ADD CONSTRAINT resource_inventory_shadow_comparisons_snapshot_fkey FOREIGN KEY (snapshot_id, inventory_scope_id) REFERENCES public.resource_inventory_snapshots(id, inventory_scope_id) ON DELETE RESTRICT;


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
-- Name: resource_inventory_snapshots resource_inventory_snapshots_ingest_ticket_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_snapshots
    ADD CONSTRAINT resource_inventory_snapshots_ingest_ticket_fkey FOREIGN KEY (ingest_ticket_id, scope_epoch_id) REFERENCES public.resource_inventory_ingest_tickets(id, scope_epoch_id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_transport_nonces resource_inventory_transport_nonces_scope_epoch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_transport_nonces
    ADD CONSTRAINT resource_inventory_transport_nonces_scope_epoch_id_fkey FOREIGN KEY (scope_epoch_id) REFERENCES public.resource_inventory_scope_epochs(id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_watch_events resource_inventory_watch_events_affected_interval_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_watch_events
    ADD CONSTRAINT resource_inventory_watch_events_affected_interval_id_fkey FOREIGN KEY (affected_interval_id) REFERENCES public.resource_intervals(id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_watch_events resource_inventory_watch_events_coverage_gap_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_watch_events
    ADD CONSTRAINT resource_inventory_watch_events_coverage_gap_id_fkey FOREIGN KEY (coverage_gap_id) REFERENCES public.resource_inventory_coverage_gaps(id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_watch_events resource_inventory_watch_events_session_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_watch_events
    ADD CONSTRAINT resource_inventory_watch_events_session_fkey FOREIGN KEY (watch_session_id, scope_epoch_id) REFERENCES public.resource_inventory_watch_sessions(id, scope_epoch_id) ON DELETE RESTRICT;


--
-- Name: resource_inventory_watch_sessions resource_inventory_watch_sessions_scope_epoch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_inventory_watch_sessions
    ADD CONSTRAINT resource_inventory_watch_sessions_scope_epoch_id_fkey FOREIGN KEY (scope_epoch_id) REFERENCES public.resource_inventory_scope_epochs(id) ON DELETE RESTRICT;


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
-- Name: runtime_actor_access_tokens runtime_actor_access_tokens_grant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_actor_access_tokens
    ADD CONSTRAINT runtime_actor_access_tokens_grant_id_fkey FOREIGN KEY (grant_id) REFERENCES public.runtime_actor_grants(id) ON DELETE CASCADE;


--
-- Name: runtime_actor_bootstraps runtime_actor_bootstraps_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_actor_bootstraps
    ADD CONSTRAINT runtime_actor_bootstraps_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: runtime_actor_grants runtime_actor_grants_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_actor_grants
    ADD CONSTRAINT runtime_actor_grants_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: runtime_actor_grants runtime_actor_grants_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_actor_grants
    ADD CONSTRAINT runtime_actor_grants_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: runtime_actor_grants runtime_actor_grants_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runtime_actor_grants
    ADD CONSTRAINT runtime_actor_grants_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


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
-- Name: storage_asset_coverage_gaps storage_asset_coverage_gaps_assertion_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_asset_coverage_gaps
    ADD CONSTRAINT storage_asset_coverage_gaps_assertion_fkey FOREIGN KEY (resolution_assertion_id) REFERENCES public.storage_backend_assertions(id) ON DELETE RESTRICT;


--
-- Name: storage_asset_coverage_gaps storage_asset_coverage_gaps_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_asset_coverage_gaps
    ADD CONSTRAINT storage_asset_coverage_gaps_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.storage_volume_assets(id) ON DELETE RESTRICT;


--
-- Name: storage_asset_coverage_gaps storage_asset_coverage_gaps_scope_epoch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_asset_coverage_gaps
    ADD CONSTRAINT storage_asset_coverage_gaps_scope_epoch_id_fkey FOREIGN KEY (scope_epoch_id) REFERENCES public.resource_inventory_scope_epochs(id) ON DELETE RESTRICT;


--
-- Name: storage_backend_assertions storage_backend_assertions_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_backend_assertions
    ADD CONSTRAINT storage_backend_assertions_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.storage_volume_assets(id) ON DELETE RESTRICT;


--
-- Name: storage_metering_source_activations storage_metering_source_activations_measurement_basis_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_metering_source_activations
    ADD CONSTRAINT storage_metering_source_activations_measurement_basis_fkey FOREIGN KEY (measurement_basis) REFERENCES public.storage_metering_activation(measurement_basis) ON DELETE RESTRICT;


--
-- Name: storage_metering_source_requirements storage_metering_source_requirements_activation_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_metering_source_requirements
    ADD CONSTRAINT storage_metering_source_requirements_activation_fkey FOREIGN KEY (measurement_basis, collector_id, source_cluster) REFERENCES public.storage_metering_source_activations(measurement_basis, collector_id, source_cluster) ON DELETE RESTRICT;


--
-- Name: storage_metering_source_requirements storage_metering_source_requirements_scope_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_metering_source_requirements
    ADD CONSTRAINT storage_metering_source_requirements_scope_fkey FOREIGN KEY (inventory_scope_id, collector_id, source_cluster) REFERENCES public.resource_inventory_scopes(id, collector_id, source_cluster) ON DELETE RESTRICT;


--
-- Name: storage_shadow_observations storage_shadow_observations_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_shadow_observations
    ADD CONSTRAINT storage_shadow_observations_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.storage_volume_assets(id) ON DELETE RESTRICT;


--
-- Name: storage_shadow_observations storage_shadow_observations_snapshot_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_shadow_observations
    ADD CONSTRAINT storage_shadow_observations_snapshot_fkey FOREIGN KEY (snapshot_id, inventory_scope_id) REFERENCES public.resource_inventory_snapshots(id, inventory_scope_id) ON DELETE RESTRICT;


--
-- Name: storage_volume_assets storage_volume_assets_destruction_assertion_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_assets
    ADD CONSTRAINT storage_volume_assets_destruction_assertion_fkey FOREIGN KEY (destruction_assertion_id) REFERENCES public.storage_backend_assertions(id) ON DELETE RESTRICT;


--
-- Name: storage_volume_assets storage_volume_assets_identity_key_version_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_assets
    ADD CONSTRAINT storage_volume_assets_identity_key_version_fkey FOREIGN KEY (identity_key_version) REFERENCES public.storage_identity_key_state(key_version) ON DELETE RESTRICT;


--
-- Name: storage_volume_assets storage_volume_assets_source_lifecycle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_assets
    ADD CONSTRAINT storage_volume_assets_source_lifecycle_id_fkey FOREIGN KEY (source_lifecycle_id) REFERENCES public.resource_lifecycle_heads(source_lifecycle_id) ON DELETE RESTRICT;


--
-- Name: storage_volume_incarnations storage_volume_incarnations_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_incarnations
    ADD CONSTRAINT storage_volume_incarnations_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.storage_volume_assets(id) ON DELETE RESTRICT;


--
-- Name: storage_volume_incarnations storage_volume_incarnations_scope_cluster_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_volume_incarnations
    ADD CONSTRAINT storage_volume_incarnations_scope_cluster_fkey FOREIGN KEY (inventory_scope_id, source_cluster) REFERENCES public.resource_inventory_scopes(id, source_cluster) ON DELETE RESTRICT;


--
-- Name: sudo_approval_requests sudo_approval_requests_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sudo_approval_requests
    ADD CONSTRAINT sudo_approval_requests_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: thread_client_presence thread_client_presence_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_client_presence
    ADD CONSTRAINT thread_client_presence_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: thread_cloud_citation_anchors thread_cloud_citation_anchors_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_cloud_citation_anchors
    ADD CONSTRAINT thread_cloud_citation_anchors_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: thread_cloud_sync_generations thread_cloud_sync_generations_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_cloud_sync_generations
    ADD CONSTRAINT thread_cloud_sync_generations_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: thread_control_requests thread_control_requests_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_control_requests
    ADD CONSTRAINT thread_control_requests_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: thread_events thread_events_control_request_thread_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_events
    ADD CONSTRAINT thread_events_control_request_thread_fkey FOREIGN KEY (control_request_id, thread_id) REFERENCES public.thread_control_requests(id, thread_id);


--
-- Name: thread_events thread_events_interrupt_request_thread_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_events
    ADD CONSTRAINT thread_events_interrupt_request_thread_fkey FOREIGN KEY (interrupt_request_id, thread_id) REFERENCES public.thread_interrupt_requests(id, thread_id);


--
-- Name: thread_events thread_events_permission_request_thread_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_events
    ADD CONSTRAINT thread_events_permission_request_thread_fkey FOREIGN KEY (permission_request_id, thread_id) REFERENCES public.thread_permission_requests(id, thread_id);


--
-- Name: thread_events thread_events_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_events
    ADD CONSTRAINT thread_events_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: thread_interrupt_requests thread_interrupt_requests_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_interrupt_requests
    ADD CONSTRAINT thread_interrupt_requests_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


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
-- Name: thread_session_runtime_state thread_session_runtime_state_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_session_runtime_state
    ADD CONSTRAINT thread_session_runtime_state_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


--
-- Name: thread_session_tasks thread_session_tasks_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_session_tasks
    ADD CONSTRAINT thread_session_tasks_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;


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
