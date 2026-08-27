-- migration:     0174_persistent_input_deliveries.sql
-- description:   Separate durable persistent-session input execution from
--                transcript storage and fence wake settlement on provider
--                admission.
-- depends-on:    0172_jobs_origin.sql (the runner applies transactional 0174
--                before the independent non-transactional 0173 index pass)
-- expected:      < 1s. New empty ledger, nullable jobs column, and small
--                trigger catalog changes; no historical-row rewrite.
-- locks:         ACCESS EXCLUSIVE briefly for jobs catalog change and trigger
--                installation. No long-lived row locks.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE public.thread_input_deliveries (
    delivery_id UUID PRIMARY KEY,
    thread_id UUID NOT NULL REFERENCES public.threads(id) ON DELETE CASCADE,
    message_id UUID NOT NULL UNIQUE
        REFERENCES public.thread_messages(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'persisted'
        CHECK (state IN (
            'persisted', 'owned', 'queued', 'admitted', 'settled', 'deferred'
        )),
    claim_generation BIGINT NOT NULL DEFAULT 0
        CHECK (claim_generation >= 0),
    -- Provenance snapshots, deliberately not FKs: agent rows are GC state and
    -- delivery audit must retain the exact predecessor identity afterwards.
    owner_agent_id UUID,
    owner_pod_uid TEXT,
    owner_runtime_generation UUID,
    admitted_turn_number BIGINT,
    deferred_reason TEXT,
    persisted_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    owned_at TIMESTAMPTZ,
    queued_at TIMESTAMPTZ,
    admitted_at TIMESTAMPTZ,
    settled_at TIMESTAMPTZ,
    deferred_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT thread_input_deliveries_owner_shape CHECK (
        (owner_agent_id IS NULL AND owner_pod_uid IS NULL
             AND owner_runtime_generation IS NULL)
        OR
        (owner_agent_id IS NOT NULL AND owner_pod_uid IS NOT NULL
             AND owner_runtime_generation IS NOT NULL)
    ),
    CONSTRAINT thread_input_deliveries_claim_shape CHECK (
        (claim_generation = 0 AND owner_agent_id IS NULL)
        OR
        (claim_generation > 0 AND owner_agent_id IS NOT NULL)
    ),
    CONSTRAINT thread_input_deliveries_admission_shape CHECK (
        (state NOT IN ('admitted', 'settled') AND admitted_at IS NULL
             AND admitted_turn_number IS NULL)
        OR
        (state IN ('admitted', 'settled') AND admitted_at IS NOT NULL
             AND admitted_turn_number IS NOT NULL)
    ),
    CONSTRAINT thread_input_deliveries_settlement_shape CHECK (
        (state = 'settled') = (settled_at IS NOT NULL)
    )
);

CREATE INDEX idx_thread_input_deliveries_reclaim
    ON public.thread_input_deliveries (thread_id, persisted_at, delivery_id)
    WHERE state IN ('persisted', 'owned', 'queued', 'deferred');

ALTER TABLE public.jobs
    ADD COLUMN wake_delivery_id UUID,
    ADD COLUMN wake_delivery_claim_attempt INTEGER
        CHECK (wake_delivery_claim_attempt IS NULL
               OR wake_delivery_claim_attempt >= 0);

CREATE FUNCTION public.require_executed_persistent_wake()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    delivery UUID;
    expected_delivery UUID;
    claim_attempt INTEGER;
    is_officer BOOLEAN := FALSE;
BEGIN
    IF TG_TABLE_NAME = 'session_wake_events' THEN
        -- An old replica increments attempts and begins HTTP delivery without
        -- establishing the execution ledger identity. Reject that claim before
        -- network I/O. The marker is tied to this exact attempt, so a later old
        -- replica cannot reuse a marker left by a prior new claim.
        IF NEW.state = 'sending' AND NEW.attempts > OLD.attempts THEN
            BEGIN
                delivery := NULLIF(NEW.payload->>'_delivery_id', '')::uuid;
                claim_attempt := NULLIF(
                    NEW.payload->>'_delivery_claim_attempt', ''
                )::integer;
            EXCEPTION
                WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                    delivery := NULL;
                    claim_attempt := NULL;
            END;
            IF delivery IS NULL OR claim_attempt IS DISTINCT FROM NEW.attempts THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'persistent_wake_requires_delivery_claim',
                    MESSAGE = 'Persistent wake claim lacks execution-ledger authority',
                    HINT = 'Retry the claim from an input-ledger-aware replica.';
            END IF;
        END IF;

        IF NEW.state <> 'sent' OR OLD.state = 'sent' THEN
            RETURN NEW;
        END IF;
        BEGIN
            delivery := NULLIF(NEW.payload->>'_delivery_id', '')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            delivery := NULL;
        END;
    ELSE
        IF NEW.wake_state = 'sending'
           AND NEW.wake_attempts > OLD.wake_attempts THEN
            expected_delivery := md5(
                'ada612a0-95c7-5e7e-83c3-8c37613455de:job:'
                || NEW.id::text || ':' || COALESCE(NEW.status, '')
            )::uuid;
            IF NEW.wake_delivery_id IS DISTINCT FROM expected_delivery
               OR NEW.wake_delivery_claim_attempt
                    IS DISTINCT FROM NEW.wake_attempts THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'persistent_wake_requires_delivery_claim',
                    MESSAGE = 'Persistent wake claim lacks execution-ledger authority',
                    HINT = 'Retry the claim from an input-ledger-aware replica.';
            END IF;
        END IF;

        IF NEW.wake_state <> 'sent' OR OLD.wake_state = 'sent' THEN
            RETURN NEW;
        END IF;

        -- Officer-created job wakes are converted into session_wake_events;
        -- this jobs-row transition retires only the conversion trigger, not
        -- the actual wake. Preserve that established two-outbox contract.
        IF NEW.created_by_thread_id IS NOT NULL THEN
            SELECT COALESCE(
                (thread.metadata #>> '{config_override,officer,enabled}')::boolean,
                FALSE
            )
              INTO is_officer
              FROM public.threads AS thread
             WHERE thread.id = NEW.created_by_thread_id;
        END IF;
        IF is_officer THEN
            RETURN NEW;
        END IF;
        delivery := NEW.wake_delivery_id;
    END IF;

    IF delivery IS NULL OR NOT EXISTS (
        SELECT 1
          FROM public.thread_input_deliveries input
         WHERE input.delivery_id = delivery
           AND input.state IN ('admitted', 'settled')
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'persistent_wake_requires_execution_admission',
            MESSAGE = 'Persistent wake delivery has not reached provider admission',
            HINT = 'Keep the wake retryable until its durable input delivery is admitted.';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_session_wake_requires_execution_admission
BEFORE UPDATE OF state ON public.session_wake_events
FOR EACH ROW EXECUTE FUNCTION public.require_executed_persistent_wake();

CREATE TRIGGER trg_job_wake_requires_execution_admission
BEFORE UPDATE OF wake_state ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.require_executed_persistent_wake();

COMMENT ON TABLE public.thread_input_deliveries IS
    'Server-owned persistent input execution ledger. A transcript row proves '
    'persistence only; admitted/settled are the wake-delivery boundary.';
COMMENT ON COLUMN public.thread_input_deliveries.owner_runtime_generation IS
    'Process generation inside one pod. A container restart may reclaim RAM-queued '
    'work even when the Kubernetes pod UID and agent row are unchanged.';
COMMENT ON COLUMN public.thread_input_deliveries.claim_generation IS
    'Monotonic CAS fence. A predecessor cannot defer or settle a successor claim.';
COMMENT ON COLUMN public.jobs.wake_delivery_id IS
    'Server-owned identity linking a non-Officer completion wake to its persistent '
    'input execution ledger. Not caller- or model-authored.';
COMMENT ON COLUMN public.jobs.wake_delivery_claim_attempt IS
    '0174 rolling-upgrade fence. Must equal wake_attempts on each sending claim '
    'so an old replica fails before performing a non-idempotent HTTP send.';
COMMENT ON FUNCTION public.require_executed_persistent_wake() IS
    'Rolling-upgrade and settlement fence: pre-0174 replicas cannot claim a '
    'persistent wake for HTTP delivery, and no replica can stamp it sent before '
    'the durable input reaches provider admission.';

COMMIT;
