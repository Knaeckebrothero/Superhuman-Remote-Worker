-- migration:     0175_job_workspace_contract_dispatch_fence.sql
-- description:   Fence mixed-version job dispatch on a server-owned workspace
--                contract/claim marker and refuse pre-contract Officer jobs.
-- depends-on:    0174_persistent_input_deliveries.sql
-- expected:      < 1s. Function and three trigger catalog writes only; no row
--                rewrite or table scan.
-- locks:         SHARE ROW EXCLUSIVE briefly while the triggers are installed.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE FUNCTION public.enforce_job_workspace_contract_dispatch()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    contract JSONB;
    marker JSONB;
    assigned_backend TEXT;
    configured_backend TEXT;
    requested_backend TEXT;
    legacy_hint TEXT;
    marker_expiry TIMESTAMPTZ;
    marker_queue_leased_until TIMESTAMPTZ;
    marker_queue_lease_token BIGINT;
    queue_lease_token BIGINT;
    queue_leased_by TEXT;
    queue_leased_until TIMESTAMPTZ;
    is_dispatch_claim BOOLEAN := FALSE;
    is_pinned_claim BOOLEAN := FALSE;
    is_stateless_claim BOOLEAN := FALSE;
BEGIN
    contract := COALESCE(NEW.context, '{}'::jsonb)->'_workspace_contract';
    configured_backend := CASE lower(COALESCE(
        NEW.config_override->'workspace'->>'backend', 'sandbox'
    ))
        WHEN 'container' THEN 'sandbox'
        WHEN 'remote' THEN 'vm'
        ELSE lower(COALESCE(
            NEW.config_override->'workspace'->>'backend', 'sandbox'
        ))
    END;

    -- Pre-0175 replicas know neither the collision-loud Officer boundary nor
    -- the dispatch contract. Refuse their Officer INSERT before a durable
    -- ticket claim/job pair can commit. Ordinary legacy rows remain readable
    -- and are admitted below only by a contract-aware claimant.
    IF TG_OP = 'INSERT'
       AND NEW.origin = 'officer'
       AND (
           jsonb_typeof(contract) IS DISTINCT FROM 'object'
           OR contract->>'version' IS DISTINCT FROM '1'
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'officer_job_requires_workspace_contract',
            MESSAGE = 'Officer job creation requires workspace-contract authority',
            HINT = 'Retry from a workspace-contract-aware orchestrator replica.';
    END IF;

    IF contract IS NOT NULL THEN
        IF jsonb_typeof(contract) IS DISTINCT FROM 'object'
           OR contract->>'version' IS DISTINCT FROM '1' THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'job_workspace_contract_shape',
                MESSAGE = 'Job workspace contract is malformed';
        END IF;
        assigned_backend := contract->>'assigned_backend';
        requested_backend := contract->>'requested_backend';
        IF assigned_backend NOT IN ('sandbox', 'vm', 'virtual', 'none')
           OR configured_backend IS DISTINCT FROM assigned_backend
           OR NULLIF(btrim(contract->>'assignment_source'), '') IS NULL
           OR (
               requested_backend IS NOT NULL
               AND requested_backend NOT IN ('sandbox', 'vm', 'virtual', 'none')
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'job_workspace_contract_consistency',
                MESSAGE = 'Job workspace contract disagrees with configuration';
        END IF;
    ELSE
        assigned_backend := configured_backend;
    END IF;

    -- Contract/config writes need the consistency checks above, but only a
    -- status/assignment/lease writer can claim dispatch authority. Separate
    -- triggers pass this mode explicitly so a context-only callback cannot be
    -- mistaken for a stateless processing->processing batch rotation.
    IF TG_OP = 'INSERT' OR COALESCE(TG_ARGV[0], 'contract') <> 'dispatch' THEN
        RETURN NEW;
    END IF;

    is_pinned_claim := NEW.status = 'processing'
            AND NEW.assigned_agent_id IS NOT NULL
            AND (
                OLD.assigned_agent_id IS DISTINCT FROM NEW.assigned_agent_id
                OR (
                    OLD.status IN ('created', 'paused', 'failed')
                    AND OLD.status IS DISTINCT FROM NEW.status
                )
            );
    -- Stateless batches deliberately keep assigned_agent_id/lease_expires_at
    -- NULL and may rotate processing->processing. Every UPDATE statement made
    -- by their claim CAS is therefore a dispatch boundary, even when the row's
    -- visible status does not change. The exact run_queue lease below proves
    -- whether a current claimant authored it.
    is_stateless_claim := NEW.status = 'processing'
        AND NEW.execution_lane = 'stateless'
        AND NEW.assigned_agent_id IS NULL;
    is_dispatch_claim := is_pinned_claim OR is_stateless_claim;
    IF NOT is_dispatch_claim THEN
        RETURN NEW;
    END IF;

    -- A contract-aware claimant writes this marker in the same UPDATE as
    -- status, agent and lease. An old replica therefore fails before agent
    -- network I/O. The lease timestamp prevents a marker left by a previous
    -- recovery from authorizing a later claim, even to the same agent.
    marker := COALESCE(NEW.context, '{}'::jsonb)
        ->'_workspace_dispatch_authority';
    IF jsonb_typeof(marker) IS DISTINCT FROM 'object'
       OR marker->>'version' IS DISTINCT FROM '1'
       OR marker->>'assigned_backend' IS DISTINCT FROM assigned_backend
       OR (
           contract IS NULL
           AND marker->>'contract_version' IS DISTINCT FROM '0'
       )
       OR (
           contract IS NOT NULL
           AND marker->>'contract_version' IS DISTINCT FROM '1'
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'job_workspace_dispatch_requires_authority',
            MESSAGE = 'Job dispatch claim lacks workspace-contract authority',
            HINT = 'Retry from a workspace-contract-aware orchestrator replica.';
    END IF;

    IF is_stateless_claim THEN
        BEGIN
            marker_queue_lease_token := NULLIF(
                marker->>'queue_lease_token', ''
            )::bigint;
            marker_queue_leased_until := NULLIF(
                marker->>'queue_leased_until', ''
            )::timestamptz;
        EXCEPTION
            WHEN invalid_text_representation
                OR numeric_value_out_of_range
                OR datetime_field_overflow THEN
                marker_queue_lease_token := NULL;
                marker_queue_leased_until := NULL;
        END;
        SELECT queue.lease_token, queue.leased_by, queue.leased_until
          INTO queue_lease_token, queue_leased_by, queue_leased_until
          FROM public.run_queue AS queue
         WHERE queue.unit_id = NEW.id
           AND queue.unit_kind = 'worker_batch'
           AND queue.state = 'leased';
        IF marker->>'dispatch_kind' IS DISTINCT FROM 'stateless'
           OR marker->>'worker_pod' IS DISTINCT FROM queue_leased_by
           OR marker_queue_lease_token IS DISTINCT FROM queue_lease_token
           OR marker_queue_leased_until IS DISTINCT FROM queue_leased_until THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'job_workspace_dispatch_requires_authority',
                MESSAGE = 'Stateless job claim lacks workspace-contract authority',
                HINT = 'Retry from a workspace-contract-aware worker replica.';
        END IF;
    ELSE
        BEGIN
            marker_expiry := NULLIF(
                marker->>'lease_expires_at', ''
            )::timestamptz;
        EXCEPTION
            WHEN invalid_text_representation OR datetime_field_overflow THEN
                marker_expiry := NULL;
        END;
        IF marker->>'dispatch_kind' IS DISTINCT FROM 'pinned'
           OR marker->>'agent_id' IS DISTINCT FROM NEW.assigned_agent_id::text
           OR marker_expiry IS DISTINCT FROM NEW.lease_expires_at THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'job_workspace_dispatch_requires_authority',
                MESSAGE = 'Pinned job claim lacks workspace-contract authority',
                HINT = 'Retry from a workspace-contract-aware orchestrator replica.';
        END IF;
    END IF;

    -- Compatibility rows use configuration as the conservative assignment.
    -- A historical request hint may corroborate it, never contradict it.
    IF contract IS NULL THEN
        IF configured_backend NOT IN ('sandbox', 'vm', 'virtual', 'none') THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'legacy_job_workspace_ambiguous',
                MESSAGE = 'Legacy job workspace assignment is unknown';
        END IF;
        IF COALESCE(NEW.context, '{}'::jsonb) ? 'workspace_backend' THEN
            legacy_hint := CASE lower(NEW.context->>'workspace_backend')
                WHEN 'container' THEN 'sandbox'
                WHEN 'remote' THEN 'vm'
                ELSE lower(NEW.context->>'workspace_backend')
            END;
            IF legacy_hint NOT IN ('sandbox', 'vm', 'virtual', 'none')
               OR legacy_hint IS DISTINCT FROM configured_backend THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'legacy_job_workspace_ambiguous',
                    MESSAGE = 'Legacy job workspace evidence is contradictory';
            END IF;
        END IF;
        IF COALESCE(NEW.context->'vm'->>'requested', 'false') = 'true'
           AND configured_backend <> 'vm' THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'legacy_job_workspace_ambiguous',
                MESSAGE = 'Legacy job workspace evidence is contradictory';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_job_workspace_contract_insert
BEFORE INSERT ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.enforce_job_workspace_contract_dispatch();

CREATE TRIGGER trg_job_workspace_contract_consistency
BEFORE UPDATE OF context, config_override ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.enforce_job_workspace_contract_dispatch('contract');

CREATE TRIGGER trg_job_workspace_contract_dispatch
BEFORE UPDATE OF status, assigned_agent_id, lease_expires_at ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.enforce_job_workspace_contract_dispatch('dispatch');

COMMENT ON FUNCTION public.enforce_job_workspace_contract_dispatch() IS
    'Rolling-upgrade fence for workspace-tier truth. Pre-contract Officer '
    'INSERTs and pre-0175 pinned/stateless dispatch claims fail before durable '
    'admission or worker delivery; current claims bind contract to the exact '
    'agent lease or run-queue lease atomically.';

COMMIT;
