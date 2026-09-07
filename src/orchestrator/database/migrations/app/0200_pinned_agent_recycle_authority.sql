-- migration:     0200_pinned_agent_recycle_authority.sql
-- description:   Add exact Kubernetes namespace/finalizer coordinates and
--                append-only persistent-agent Pod recycle handoffs.
-- depends-on:    0199_vm_remote_operation_leases.sql
-- expected:      < 5s. Two nullable legacy columns, two empty authority
--                ledgers, and row triggers; no historical scan or rewrite.
-- locks:         Brief ACCESS EXCLUSIVE locks on the two 0185 pinned-agent
--                ledgers while nullable columns and triggers are installed.
-- transactional: yes
--
-- Pinned agent Kubernetes authority follow-on.
--
-- 0185 deliberately records a Pod/PVC name before the first Kubernetes
-- effect.  Namespace is part of that immutable API key, and a persistent-pod
-- recycle needs a durable edge from the published predecessor to the one
-- permitted successor.  Keep 0185 as the canonical lifecycle contract and
-- add only those missing coordinates here.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.thread_agent_workspace_claims
    ADD COLUMN IF NOT EXISTS namespace varchar(253),
    ADD COLUMN IF NOT EXISTS protection_protocol varchar(32);

ALTER TABLE public.thread_agent_pod_provision_intents
    ADD COLUMN IF NOT EXISTS namespace varchar(253),
    ADD COLUMN IF NOT EXISTS protection_protocol varchar(32);

ALTER TABLE public.thread_agent_workspace_claims
    DROP CONSTRAINT IF EXISTS thread_agent_workspace_claim_namespace,
    ADD CONSTRAINT thread_agent_workspace_claim_namespace CHECK (
        (namespace IS NULL AND protection_protocol IS NULL)
        OR (
            namespace IS NOT NULL
            AND length(namespace) BETWEEN 1 AND 63
            AND namespace ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
            AND protection_protocol = 'finalizer_v1'
        )
    ) NOT VALID;

ALTER TABLE public.thread_agent_pod_provision_intents
    DROP CONSTRAINT IF EXISTS thread_agent_pod_provision_namespace,
    ADD CONSTRAINT thread_agent_pod_provision_namespace CHECK (
        (namespace IS NULL AND protection_protocol IS NULL)
        OR (
            namespace IS NOT NULL
            AND length(namespace) BETWEEN 1 AND 63
            AND namespace ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
            AND protection_protocol = 'finalizer_v1'
        )
    ) NOT VALID;

-- Historical 0185 rows can exist when this follow-on is deployed.  They are
-- intentionally left NULL: guessing the current Helm namespace could fence the
-- wrong API key after a namespace move.  A live object may be grandfathered
-- only through the append-only receipt below after the server has matched its
-- exact labels/UID and observed our finalizer in one explicitly configured
-- namespace.  Every new row must still be fully protected from birth.
CREATE TABLE IF NOT EXISTS public.thread_agent_k8s_authority_adoptions (
    attempt_id uuid PRIMARY KEY
        REFERENCES public.thread_agent_pod_provision_intents(attempt_id)
        ON DELETE RESTRICT,
    thread_id uuid NOT NULL,
    runtime_generation uuid NOT NULL,
    provisioner varchar(16) NOT NULL
        CHECK (provisioner IN ('agent', 'persistent')),
    workspace_claim_id uuid UNIQUE
        REFERENCES public.thread_agent_workspace_claims(claim_id)
        ON DELETE RESTRICT,
    namespace varchar(253) NOT NULL,
    pod_name varchar(253) NOT NULL,
    pod_uid text NOT NULL,
    pod_resource_version text NOT NULL,
    pvc_name varchar(253),
    pvc_uid text,
    pvc_resource_version text,
    protection_finalizer varchar(128) NOT NULL,
    evidence_protocol varchar(48) NOT NULL,
    observed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    CHECK (pod_name <> '' AND pod_uid <> '' AND pod_resource_version <> ''),
    CHECK (
        length(namespace) BETWEEN 1 AND 63
        AND namespace ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
    ),
    CHECK (protection_finalizer = 'srw.io/pinned-authority-protection'),
    CHECK (evidence_protocol = 'exact_live_finalizer_v1'),
    CHECK (observed_at <= created_at),
    CHECK (
        (workspace_claim_id IS NULL
            AND pvc_name IS NULL
            AND pvc_uid IS NULL
            AND pvc_resource_version IS NULL)
        OR
        (workspace_claim_id IS NOT NULL
            AND NULLIF(pvc_name, '') IS NOT NULL
            AND NULLIF(pvc_uid, '') IS NOT NULL
            AND NULLIF(pvc_resource_version, '') IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_thread_agent_k8s_authority_adoptions_thread
    ON public.thread_agent_k8s_authority_adoptions(thread_id, runtime_generation);

CREATE OR REPLACE FUNCTION public.enforce_thread_agent_k8s_authority_adoption()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    thread_row public.threads%ROWTYPE;
    intent_row public.thread_agent_pod_provision_intents%ROWTYPE;
    claim_row public.thread_agent_workspace_claims%ROWTYPE;
    marker jsonb;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'pinned agent Kubernetes adoption is append-only'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_k8s_authority_adoption';
    END IF;

    SELECT * INTO thread_row FROM public.threads
     WHERE id = NEW.thread_id FOR KEY SHARE;
    SELECT * INTO intent_row
      FROM public.thread_agent_pod_provision_intents
     WHERE attempt_id = NEW.attempt_id FOR KEY SHARE;
    IF NEW.workspace_claim_id IS NOT NULL THEN
        SELECT * INTO claim_row
          FROM public.thread_agent_workspace_claims
         WHERE claim_id = NEW.workspace_claim_id FOR KEY SHARE;
    END IF;
    marker := COALESCE(thread_row.metadata->'agent_pod', '{}'::jsonb);

    IF thread_row.id IS NULL
       OR thread_row.execution_lane <> 'pinned'
       OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR thread_row.runtime_retirement_token IS NOT NULL
       OR thread_row.status NOT IN ('created', 'active', 'awaiting_user', 'suspended')
       OR intent_row.attempt_id IS NULL
       OR intent_row.thread_id IS DISTINCT FROM NEW.thread_id
       OR intent_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR intent_row.provisioner IS DISTINCT FROM NEW.provisioner
       OR intent_row.workspace_claim_id IS DISTINCT FROM NEW.workspace_claim_id
       OR intent_row.pod_name IS DISTINCT FROM NEW.pod_name
       OR intent_row.namespace IS NOT NULL
       OR intent_row.protection_protocol IS NOT NULL
       OR intent_row.status NOT IN ('planned', 'published')
       OR (
            intent_row.status = 'published'
            AND intent_row.pod_uid IS DISTINCT FROM NEW.pod_uid
       )
       OR (
            intent_row.status = 'planned'
            AND (
                intent_row.pod_uid IS NOT NULL
                OR marker NOT IN ('null'::jsonb, '{}'::jsonb)
                OR thread_row.agent_id IS NOT NULL
                OR thread_row.control_admission_agent_id IS NOT NULL
                OR thread_row.runtime_attach_token IS NOT NULL
            )
       )
       OR (
            intent_row.status = 'published'
            AND (
                marker->>'pod_name' IS DISTINCT FROM NEW.pod_name
                OR marker->>'pod_uid' IS DISTINCT FROM NEW.pod_uid
                OR marker->>'provision_attempt'
                    IS DISTINCT FROM NEW.attempt_id::text
                OR marker->>'runtime_generation'
                    IS DISTINCT FROM NEW.runtime_generation::text
                OR NULLIF(marker->>'namespace', '') IS NOT NULL
                OR NULLIF(marker->>'protection_protocol', '') IS NOT NULL
            )
       )
       OR (
            NEW.workspace_claim_id IS NULL
            AND intent_row.workspace_claim_id IS NOT NULL
       )
       OR (
            NEW.workspace_claim_id IS NOT NULL
            AND (
                claim_row.claim_id IS NULL
                OR claim_row.thread_id IS DISTINCT FROM NEW.thread_id
                OR claim_row.created_runtime_generation
                    IS DISTINCT FROM NEW.runtime_generation
                OR claim_row.provisioner IS DISTINCT FROM NEW.provisioner
                OR claim_row.pvc_name IS DISTINCT FROM NEW.pvc_name
                OR claim_row.namespace IS NOT NULL
                OR claim_row.protection_protocol IS NOT NULL
                OR claim_row.status NOT IN ('planned', 'ready')
                OR (
                    claim_row.status = 'ready'
                    AND claim_row.pvc_uid IS DISTINCT FROM NEW.pvc_uid
                )
                OR (
                    claim_row.status = 'planned'
                    AND claim_row.pvc_uid IS NOT NULL
                )
            )
       ) THEN
        RAISE EXCEPTION 'pinned agent Kubernetes adoption lacks legacy authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_k8s_authority_adoption';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS thread_agent_k8s_authority_adoption
    ON public.thread_agent_k8s_authority_adoptions;
CREATE TRIGGER thread_agent_k8s_authority_adoption
BEFORE INSERT OR UPDATE OR DELETE
ON public.thread_agent_k8s_authority_adoptions
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_agent_k8s_authority_adoption();

CREATE OR REPLACE FUNCTION public.pinned_agent_claim_adoption_matches(
    candidate public.thread_agent_workspace_claims
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
          FROM public.thread_agent_k8s_authority_adoptions adoption
         WHERE adoption.workspace_claim_id = candidate.claim_id
           AND adoption.thread_id = candidate.thread_id
           AND adoption.runtime_generation = candidate.created_runtime_generation
           AND adoption.provisioner = candidate.provisioner
           AND adoption.namespace = candidate.namespace
           AND adoption.pvc_name = candidate.pvc_name
           AND adoption.pvc_uid = candidate.pvc_uid
           AND candidate.protection_protocol = 'finalizer_v1'
    )
$$;

CREATE OR REPLACE FUNCTION public.pinned_agent_intent_adoption_matches(
    candidate public.thread_agent_pod_provision_intents
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
          FROM public.thread_agent_k8s_authority_adoptions adoption
         WHERE adoption.attempt_id = candidate.attempt_id
           AND adoption.thread_id = candidate.thread_id
           AND adoption.runtime_generation = candidate.runtime_generation
           AND adoption.provisioner = candidate.provisioner
           AND adoption.workspace_claim_id
                IS NOT DISTINCT FROM candidate.workspace_claim_id
           AND adoption.namespace = candidate.namespace
           AND adoption.pod_name = candidate.pod_name
           AND adoption.pod_uid = candidate.pod_uid
           AND candidate.protection_protocol = 'finalizer_v1'
    )
$$;

-- 0185's original transition triggers reject a same-status UPDATE.  Retain
-- those exact functions for every ordinary write, but let the coordinate-only
-- legacy edge reach the receipt-aware trigger below.  Planned->ready/published
-- adoption remains an ordinary 0185 transition and is checked by both.
DROP TRIGGER IF EXISTS thread_agent_workspace_claim_authority
    ON public.thread_agent_workspace_claims;
DROP TRIGGER IF EXISTS thread_agent_workspace_claim_authority_update
    ON public.thread_agent_workspace_claims;
CREATE TRIGGER thread_agent_workspace_claim_authority
BEFORE INSERT OR DELETE
ON public.thread_agent_workspace_claims
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_agent_workspace_claim();
CREATE TRIGGER thread_agent_workspace_claim_authority_update
BEFORE UPDATE
ON public.thread_agent_workspace_claims
FOR EACH ROW
WHEN (NOT (
    OLD.namespace IS NULL
    AND OLD.protection_protocol IS NULL
    AND NEW.namespace IS NOT NULL
    AND NEW.protection_protocol = 'finalizer_v1'
    AND NEW.claim_id IS NOT DISTINCT FROM OLD.claim_id
    AND NEW.thread_id IS NOT DISTINCT FROM OLD.thread_id
    AND NEW.created_runtime_generation
        IS NOT DISTINCT FROM OLD.created_runtime_generation
    AND NEW.create_attempt IS NOT DISTINCT FROM OLD.create_attempt
    AND NEW.provisioner IS NOT DISTINCT FROM OLD.provisioner
    AND NEW.pvc_name IS NOT DISTINCT FROM OLD.pvc_name
    AND NEW.status IS NOT DISTINCT FROM OLD.status
    AND NEW.pvc_uid IS NOT DISTINCT FROM OLD.pvc_uid
    AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
    AND NEW.fenced_at IS NOT DISTINCT FROM OLD.fenced_at
    AND NEW.gc_after IS NOT DISTINCT FROM OLD.gc_after
    AND NEW.resolved_at IS NOT DISTINCT FROM OLD.resolved_at
    AND public.pinned_agent_claim_adoption_matches(NEW)
))
EXECUTE FUNCTION public.enforce_thread_agent_workspace_claim();

DROP TRIGGER IF EXISTS thread_agent_pod_provision_intent_authority
    ON public.thread_agent_pod_provision_intents;
DROP TRIGGER IF EXISTS thread_agent_pod_provision_intent_authority_update
    ON public.thread_agent_pod_provision_intents;
CREATE TRIGGER thread_agent_pod_provision_intent_authority
BEFORE INSERT OR DELETE
ON public.thread_agent_pod_provision_intents
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_agent_pod_provision_intent();
CREATE TRIGGER thread_agent_pod_provision_intent_authority_update
BEFORE UPDATE
ON public.thread_agent_pod_provision_intents
FOR EACH ROW
WHEN (NOT (
    OLD.namespace IS NULL
    AND OLD.protection_protocol IS NULL
    AND NEW.namespace IS NOT NULL
    AND NEW.protection_protocol = 'finalizer_v1'
    AND NEW.attempt_id IS NOT DISTINCT FROM OLD.attempt_id
    AND NEW.thread_id IS NOT DISTINCT FROM OLD.thread_id
    AND NEW.runtime_generation IS NOT DISTINCT FROM OLD.runtime_generation
    AND NEW.provisioner IS NOT DISTINCT FROM OLD.provisioner
    AND NEW.workspace_claim_id IS NOT DISTINCT FROM OLD.workspace_claim_id
    AND NEW.pod_name IS NOT DISTINCT FROM OLD.pod_name
    AND NEW.status IS NOT DISTINCT FROM OLD.status
    AND NEW.pod_uid IS NOT DISTINCT FROM OLD.pod_uid
    AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
    AND NEW.fenced_at IS NOT DISTINCT FROM OLD.fenced_at
    AND NEW.gc_after IS NOT DISTINCT FROM OLD.gc_after
    AND NEW.resolved_at IS NOT DISTINCT FROM OLD.resolved_at
    AND public.pinned_agent_intent_adoption_matches(NEW)
))
EXECUTE FUNCTION public.enforce_thread_agent_pod_provision_intent();

CREATE OR REPLACE FUNCTION public.enforce_pinned_agent_k8s_coordinates()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    claim_row public.thread_agent_workspace_claims%ROWTYPE;
    adoption_allowed boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.namespace IS NULL
           OR NEW.protection_protocol IS DISTINCT FROM 'finalizer_v1' THEN
            RAISE EXCEPTION
                'new pinned agent Kubernetes authority requires namespace/finalizer_v1'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'pinned_agent_k8s_coordinates';
        END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.namespace IS DISTINCT FROM OLD.namespace
           OR NEW.protection_protocol IS DISTINCT FROM OLD.protection_protocol THEN
            IF TG_TABLE_NAME = 'thread_agent_workspace_claims' THEN
                adoption_allowed := public.pinned_agent_claim_adoption_matches(NEW);
            ELSIF TG_TABLE_NAME = 'thread_agent_pod_provision_intents' THEN
                adoption_allowed := public.pinned_agent_intent_adoption_matches(NEW);
            END IF;
            IF NOT (
                OLD.namespace IS NULL
                AND OLD.protection_protocol IS NULL
                AND NEW.namespace IS NOT NULL
                AND NEW.protection_protocol = 'finalizer_v1'
                AND adoption_allowed
            ) THEN
                RAISE EXCEPTION
                    'pinned agent Kubernetes coordinates are immutable'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'pinned_agent_k8s_coordinates';
            END IF;
        END IF;
    END IF;

    IF TG_TABLE_NAME = 'thread_agent_pod_provision_intents'
       AND to_jsonb(NEW)->>'workspace_claim_id' IS NOT NULL THEN
        SELECT * INTO claim_row
          FROM public.thread_agent_workspace_claims
         WHERE claim_id = (to_jsonb(NEW)->>'workspace_claim_id')::uuid;
        IF NOT FOUND
           OR claim_row.namespace IS DISTINCT FROM NEW.namespace
           OR claim_row.protection_protocol
                IS DISTINCT FROM NEW.protection_protocol THEN
            RAISE EXCEPTION
                'pinned agent Pod and workspace claim coordinates differ'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'pinned_agent_k8s_coordinates';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS zz_thread_agent_workspace_claim_coordinates
    ON public.thread_agent_workspace_claims;
CREATE TRIGGER zz_thread_agent_workspace_claim_coordinates
BEFORE INSERT OR UPDATE
ON public.thread_agent_workspace_claims
FOR EACH ROW
EXECUTE FUNCTION public.enforce_pinned_agent_k8s_coordinates();

CREATE OR REPLACE FUNCTION public.validate_thread_agent_k8s_authority_adoption()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    thread_row public.threads%ROWTYPE;
    intent_row public.thread_agent_pod_provision_intents%ROWTYPE;
    claim_row public.thread_agent_workspace_claims%ROWTYPE;
    marker jsonb;
BEGIN
    SELECT * INTO thread_row FROM public.threads WHERE id = NEW.thread_id;
    SELECT * INTO intent_row
      FROM public.thread_agent_pod_provision_intents
     WHERE attempt_id = NEW.attempt_id;
    IF NEW.workspace_claim_id IS NOT NULL THEN
        SELECT * INTO claim_row
          FROM public.thread_agent_workspace_claims
         WHERE claim_id = NEW.workspace_claim_id;
    END IF;
    marker := COALESCE(thread_row.metadata->'agent_pod', '{}'::jsonb);

    IF thread_row.id IS NULL
       OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR thread_row.runtime_retirement_token IS NOT NULL
       OR intent_row.attempt_id IS NULL
       OR intent_row.status <> 'published'
       OR intent_row.pod_uid IS DISTINCT FROM NEW.pod_uid
       OR intent_row.namespace IS DISTINCT FROM NEW.namespace
       OR intent_row.protection_protocol <> 'finalizer_v1'
       OR marker->>'pod_name' IS DISTINCT FROM NEW.pod_name
       OR marker->>'pod_uid' IS DISTINCT FROM NEW.pod_uid
       OR marker->>'provision_attempt' IS DISTINCT FROM NEW.attempt_id::text
       OR marker->>'runtime_generation'
            IS DISTINCT FROM NEW.runtime_generation::text
       OR marker->>'namespace' IS DISTINCT FROM NEW.namespace
       OR marker->>'protection_protocol' <> 'finalizer_v1'
       OR (
            NEW.workspace_claim_id IS NOT NULL
            AND (
                claim_row.claim_id IS NULL
                OR claim_row.status <> 'ready'
                OR claim_row.pvc_uid IS DISTINCT FROM NEW.pvc_uid
                OR claim_row.namespace IS DISTINCT FROM NEW.namespace
                OR claim_row.protection_protocol <> 'finalizer_v1'
            )
       ) THEN
        RAISE EXCEPTION 'pinned agent Kubernetes adoption is not reciprocal'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_k8s_authority_adoption_reciprocity';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS thread_agent_k8s_authority_adoption_reciprocity
    ON public.thread_agent_k8s_authority_adoptions;
CREATE CONSTRAINT TRIGGER thread_agent_k8s_authority_adoption_reciprocity
AFTER INSERT
ON public.thread_agent_k8s_authority_adoptions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION public.validate_thread_agent_k8s_authority_adoption();

DROP TRIGGER IF EXISTS zz_thread_agent_pod_provision_coordinates
    ON public.thread_agent_pod_provision_intents;
CREATE TRIGGER zz_thread_agent_pod_provision_coordinates
BEFORE INSERT OR UPDATE
ON public.thread_agent_pod_provision_intents
FOR EACH ROW
EXECUTE FUNCTION public.enforce_pinned_agent_k8s_coordinates();

-- A warm-pool Pod predates the thread that will eventually reserve it, so it
-- cannot carry thread authority from birth.  Persist the exact intended
-- thread/agent/Pod tuple before installing our finalizer, then publish the
-- observed finalizer and reciprocal binding in separate, fenced transitions.
-- This is deliberately independent from the 0185 create-intent table: no Pod
-- is created here, and a crashed attach must release an already-existing pool
-- Pod rather than manufacture a same-name create fence.
CREATE TABLE IF NOT EXISTS public.thread_agent_warm_binding_protections (
    protection_id uuid PRIMARY KEY,
    -- No foreign keys by design.  A settled End may delete the thread/agent,
    -- while this exact external-effect receipt remains useful for audit and
    -- restart-safe finalizer cleanup.
    thread_id uuid NOT NULL,
    runtime_generation uuid NOT NULL,
    runtime_attach_token uuid NOT NULL,
    agent_id uuid NOT NULL,
    source varchar(24) NOT NULL
        CHECK (source IN ('attach', 'legacy_binding')),
    provisioner varchar(16) NOT NULL
        CHECK (provisioner IN ('agent', 'persistent')),
    namespace varchar(253) NOT NULL,
    pod_name varchar(253) NOT NULL,
    pod_uid text NOT NULL,
    discovered_resource_version text NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'planned'
        CHECK (status IN (
            'planned', 'protecting', 'protected', 'bound', 'releasing',
            'released', 'aborted'
        )),
    lease_expires_at timestamptz NOT NULL,
    -- A fresh token/horizon is claimed in Postgres immediately before the
    -- first Kubernetes mutation.  The immutable horizon is what lets a
    -- reconciler fence a worker that was paused after its claim.
    effect_token uuid,
    effect_started_at timestamptz,
    effect_expires_at timestamptz,
    protection_resource_version text,
    evidence_protocol varchar(48),
    protected_at timestamptz,
    bound_at timestamptz,
    release_started_at timestamptz,
    release_outcome varchar(32)
        CHECK (release_outcome IN (
            'exact_live_unprotected_v1', 'exact_absent_v1',
            'exact_replacement_v1'
        )),
    abort_fence_protocol varchar(48)
        CHECK (abort_fence_protocol IN (
            'unclaimed_plan_v1', 'exact_rv_annotation_fence_v1',
            'exact_object_gone_v1'
        )),
    abort_fence_resource_version text,
    abort_fence_value text,
    released_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (agent_id, runtime_attach_token),
    CHECK (
        length(namespace) BETWEEN 1 AND 63
        AND namespace ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
    ),
    CHECK (pod_name <> '' AND pod_uid <> ''),
    CHECK (discovered_resource_version <> ''),
    CHECK (lease_expires_at > created_at),
    CHECK (
        (status = 'planned'
            AND effect_token IS NULL AND effect_started_at IS NULL
            AND effect_expires_at IS NULL
            AND protection_resource_version IS NULL
            AND evidence_protocol IS NULL AND protected_at IS NULL
            AND bound_at IS NULL AND release_started_at IS NULL
            AND abort_fence_protocol IS NULL
            AND abort_fence_resource_version IS NULL
            AND abort_fence_value IS NULL
            AND release_outcome IS NULL AND released_at IS NULL)
        OR (status = 'protecting'
            AND effect_token IS NOT NULL AND effect_started_at IS NOT NULL
            AND effect_expires_at > effect_started_at
            AND effect_expires_at
                <= effect_started_at + interval '180 seconds'
            AND protection_resource_version IS NULL
            AND evidence_protocol IS NULL AND protected_at IS NULL
            AND bound_at IS NULL AND release_started_at IS NULL
            AND abort_fence_protocol IS NULL
            AND abort_fence_resource_version IS NULL
            AND abort_fence_value IS NULL
            AND release_outcome IS NULL AND released_at IS NULL)
        OR (status = 'protected'
            AND effect_token IS NOT NULL AND effect_started_at IS NOT NULL
            AND effect_expires_at IS NOT NULL
            AND NULLIF(protection_resource_version, '') IS NOT NULL
            AND evidence_protocol = 'exact_live_finalizer_v1'
            AND protected_at IS NOT NULL AND bound_at IS NULL
            AND release_started_at IS NULL
            AND abort_fence_protocol IS NULL
            AND abort_fence_resource_version IS NULL
            AND abort_fence_value IS NULL
            AND release_outcome IS NULL AND released_at IS NULL)
        OR (status = 'bound'
            AND effect_token IS NOT NULL AND effect_started_at IS NOT NULL
            AND effect_expires_at IS NOT NULL
            AND NULLIF(protection_resource_version, '') IS NOT NULL
            AND evidence_protocol = 'exact_live_finalizer_v1'
            AND protected_at IS NOT NULL AND bound_at IS NOT NULL
            AND release_started_at IS NULL
            AND abort_fence_protocol IS NULL
            AND abort_fence_resource_version IS NULL
            AND abort_fence_value IS NULL
            AND release_outcome IS NULL AND released_at IS NULL)
        OR (status = 'releasing'
            AND effect_token IS NOT NULL AND effect_started_at IS NOT NULL
            AND effect_expires_at IS NOT NULL
            AND NULLIF(protection_resource_version, '') IS NOT NULL
            AND evidence_protocol = 'exact_live_finalizer_v1'
            AND protected_at IS NOT NULL
            AND release_started_at IS NOT NULL
            AND abort_fence_protocol IS NULL
            AND abort_fence_resource_version IS NULL
            AND abort_fence_value IS NULL
            AND release_outcome IS NULL AND released_at IS NULL)
        OR (status = 'released'
            AND effect_token IS NOT NULL AND effect_started_at IS NOT NULL
            AND effect_expires_at IS NOT NULL
            AND NULLIF(protection_resource_version, '') IS NOT NULL
            AND evidence_protocol = 'exact_live_finalizer_v1'
            AND protected_at IS NOT NULL
            AND release_started_at IS NOT NULL
            AND abort_fence_protocol IS NULL
            AND abort_fence_resource_version IS NULL
            AND abort_fence_value IS NULL
            AND release_outcome IS NOT NULL AND released_at IS NOT NULL)
        OR (status = 'aborted'
            AND protection_resource_version IS NULL
            AND evidence_protocol IS NULL AND protected_at IS NULL
            AND bound_at IS NULL AND release_started_at IS NULL
            AND (
                (effect_token IS NULL AND effect_started_at IS NULL
                    AND effect_expires_at IS NULL
                    AND abort_fence_protocol = 'unclaimed_plan_v1'
                    AND abort_fence_resource_version IS NULL
                    AND abort_fence_value IS NULL)
                OR
                (effect_token IS NOT NULL AND effect_started_at IS NOT NULL
                    AND effect_expires_at IS NOT NULL
                    AND (
                        (release_outcome = 'exact_live_unprotected_v1'
                            AND abort_fence_protocol
                                = 'exact_rv_annotation_fence_v1'
                            AND NULLIF(abort_fence_resource_version, '')
                                IS NOT NULL
                            AND abort_fence_value
                                = protection_id::text || ':'
                                    || effect_token::text)
                        OR
                        (release_outcome IN (
                                'exact_absent_v1', 'exact_replacement_v1'
                            )
                            AND abort_fence_protocol
                                = 'exact_object_gone_v1'
                            AND abort_fence_resource_version IS NULL
                            AND abort_fence_value IS NULL)
                    ))
            )
            AND release_outcome IS NOT NULL AND released_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_thread_agent_warm_binding_reconcile
    ON public.thread_agent_warm_binding_protections(
        status, lease_expires_at, effect_expires_at
    )
    WHERE status IN ('planned', 'protecting', 'protected', 'releasing');

CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_agent_warm_binding_thread_active
    ON public.thread_agent_warm_binding_protections(
        thread_id, runtime_generation
    )
    WHERE status IN ('planned', 'protecting', 'protected', 'bound', 'releasing');

CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_agent_warm_binding_agent_active
    ON public.thread_agent_warm_binding_protections(agent_id)
    WHERE status IN ('planned', 'protecting', 'protected', 'bound', 'releasing');

CREATE OR REPLACE FUNCTION public.enforce_thread_agent_warm_binding_protection()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    thread_row public.threads%ROWTYPE;
    agent_row public.agents%ROWTYPE;
    marker jsonb;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'warm binding protection is durable authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_warm_binding_authority';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF NEW.protection_id IS DISTINCT FROM OLD.protection_id
           OR NEW.thread_id IS DISTINCT FROM OLD.thread_id
           OR NEW.runtime_generation IS DISTINCT FROM OLD.runtime_generation
           OR NEW.runtime_attach_token IS DISTINCT FROM OLD.runtime_attach_token
           OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
           OR NEW.source IS DISTINCT FROM OLD.source
           OR NEW.provisioner IS DISTINCT FROM OLD.provisioner
           OR NEW.namespace IS DISTINCT FROM OLD.namespace
           OR NEW.pod_name IS DISTINCT FROM OLD.pod_name
           OR NEW.pod_uid IS DISTINCT FROM OLD.pod_uid
           OR NEW.discovered_resource_version
                IS DISTINCT FROM OLD.discovered_resource_version
           OR NEW.lease_expires_at IS DISTINCT FROM OLD.lease_expires_at
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NOT (
                -- This CAS is the only grant to mutate Kubernetes.  It may
                -- race an expired-plan abort: exactly one transition wins.
                (OLD.status = 'planned' AND NEW.status = 'protecting'
                    AND NEW.effect_token IS NOT NULL
                    AND NEW.effect_started_at
                        IS NOT DISTINCT FROM transaction_timestamp()
                    AND NEW.effect_expires_at > NEW.effect_started_at
                    AND NEW.effect_expires_at
                        <= NEW.effect_started_at + interval '180 seconds'
                    AND NEW.protection_resource_version IS NULL
                    AND NEW.evidence_protocol IS NULL
                    AND NEW.protected_at IS NULL AND NEW.bound_at IS NULL
                    AND NEW.release_started_at IS NULL
                    AND NEW.release_outcome IS NULL
                    AND NEW.abort_fence_protocol IS NULL
                    AND NEW.abort_fence_resource_version IS NULL
                    AND NEW.abort_fence_value IS NULL
                    AND NEW.released_at IS NULL)
                OR (OLD.status = 'planned' AND NEW.status = 'aborted'
                    AND transaction_timestamp() >= OLD.lease_expires_at
                    AND NEW.effect_token IS NULL
                    AND NEW.effect_started_at IS NULL
                    AND NEW.effect_expires_at IS NULL
                    AND NEW.protection_resource_version IS NULL
                    AND NEW.evidence_protocol IS NULL
                    AND NEW.protected_at IS NULL AND NEW.bound_at IS NULL
                    AND NEW.release_started_at IS NULL
                    AND NEW.release_outcome IN (
                        'exact_absent_v1', 'exact_replacement_v1',
                        'exact_live_unprotected_v1'
                    )
                    AND NEW.abort_fence_protocol = 'unclaimed_plan_v1'
                    AND NEW.abort_fence_resource_version IS NULL
                    AND NEW.abort_fence_value IS NULL
                    AND NEW.released_at
                        IS NOT DISTINCT FROM transaction_timestamp())
                OR (OLD.status = 'protecting' AND NEW.status = 'protected'
                    AND NEW.effect_token IS NOT DISTINCT FROM OLD.effect_token
                    AND NEW.effect_started_at
                        IS NOT DISTINCT FROM OLD.effect_started_at
                    AND NEW.effect_expires_at
                        IS NOT DISTINCT FROM OLD.effect_expires_at
                    AND NULLIF(NEW.protection_resource_version, '') IS NOT NULL
                    AND NEW.evidence_protocol = 'exact_live_finalizer_v1'
                    AND NEW.protected_at
                        IS NOT DISTINCT FROM transaction_timestamp()
                    AND NEW.bound_at IS NULL
                    AND NEW.release_started_at IS NULL
                    AND NEW.release_outcome IS NULL
                    AND NEW.abort_fence_protocol IS NULL
                    AND NEW.abort_fence_resource_version IS NULL
                    AND NEW.abort_fence_value IS NULL
                    AND NEW.released_at IS NULL)
                OR (OLD.status = 'protecting' AND NEW.status = 'aborted'
                    AND transaction_timestamp() >= OLD.effect_expires_at
                    AND NEW.effect_token IS NOT DISTINCT FROM OLD.effect_token
                    AND NEW.effect_started_at
                        IS NOT DISTINCT FROM OLD.effect_started_at
                    AND NEW.effect_expires_at
                        IS NOT DISTINCT FROM OLD.effect_expires_at
                    AND NEW.protection_resource_version IS NULL
                    AND NEW.evidence_protocol IS NULL
                    AND NEW.protected_at IS NULL AND NEW.bound_at IS NULL
                    AND NEW.release_started_at IS NULL
                    AND (
                        (NEW.release_outcome = 'exact_live_unprotected_v1'
                            AND NEW.abort_fence_protocol
                                = 'exact_rv_annotation_fence_v1'
                            AND NULLIF(NEW.abort_fence_resource_version, '')
                                IS NOT NULL
                            AND NEW.abort_fence_value
                                = NEW.protection_id::text || ':'
                                    || NEW.effect_token::text)
                        OR
                        (NEW.release_outcome IN (
                                'exact_absent_v1', 'exact_replacement_v1'
                            )
                            AND NEW.abort_fence_protocol
                                = 'exact_object_gone_v1'
                            AND NEW.abort_fence_resource_version IS NULL
                            AND NEW.abort_fence_value IS NULL)
                    )
                    AND NEW.released_at
                        IS NOT DISTINCT FROM transaction_timestamp())
                OR (OLD.status = 'protected' AND NEW.status = 'bound'
                    AND NEW.effect_token IS NOT DISTINCT FROM OLD.effect_token
                    AND NEW.effect_started_at
                        IS NOT DISTINCT FROM OLD.effect_started_at
                    AND NEW.effect_expires_at
                        IS NOT DISTINCT FROM OLD.effect_expires_at
                    AND NEW.protection_resource_version
                        IS NOT DISTINCT FROM OLD.protection_resource_version
                    AND NEW.evidence_protocol
                        IS NOT DISTINCT FROM OLD.evidence_protocol
                    AND NEW.protected_at IS NOT DISTINCT FROM OLD.protected_at
                    AND NEW.bound_at
                        IS NOT DISTINCT FROM transaction_timestamp()
                    AND NEW.release_started_at IS NULL
                    AND NEW.release_outcome IS NULL
                    AND NEW.abort_fence_protocol IS NULL
                    AND NEW.abort_fence_resource_version IS NULL
                    AND NEW.abort_fence_value IS NULL
                    AND NEW.released_at IS NULL)
                OR (OLD.status IN ('protected', 'bound')
                    AND NEW.status = 'releasing'
                    AND NEW.effect_token IS NOT DISTINCT FROM OLD.effect_token
                    AND NEW.effect_started_at
                        IS NOT DISTINCT FROM OLD.effect_started_at
                    AND NEW.effect_expires_at
                        IS NOT DISTINCT FROM OLD.effect_expires_at
                    AND NEW.protection_resource_version
                        IS NOT DISTINCT FROM OLD.protection_resource_version
                    AND NEW.evidence_protocol
                        IS NOT DISTINCT FROM OLD.evidence_protocol
                    AND NEW.protected_at IS NOT DISTINCT FROM OLD.protected_at
                    AND NEW.bound_at IS NOT DISTINCT FROM OLD.bound_at
                    AND NEW.release_started_at
                        IS NOT DISTINCT FROM transaction_timestamp()
                    AND NEW.release_outcome IS NULL
                    AND NEW.abort_fence_protocol IS NULL
                    AND NEW.abort_fence_resource_version IS NULL
                    AND NEW.abort_fence_value IS NULL
                    AND NEW.released_at IS NULL)
                OR (OLD.status = 'releasing' AND NEW.status = 'released'
                    AND NEW.effect_token IS NOT DISTINCT FROM OLD.effect_token
                    AND NEW.effect_started_at
                        IS NOT DISTINCT FROM OLD.effect_started_at
                    AND NEW.effect_expires_at
                        IS NOT DISTINCT FROM OLD.effect_expires_at
                    AND NEW.protection_resource_version
                        IS NOT DISTINCT FROM OLD.protection_resource_version
                    AND NEW.evidence_protocol
                        IS NOT DISTINCT FROM OLD.evidence_protocol
                    AND NEW.protected_at IS NOT DISTINCT FROM OLD.protected_at
                    AND NEW.bound_at IS NOT DISTINCT FROM OLD.bound_at
                    AND NEW.release_started_at
                        IS NOT DISTINCT FROM OLD.release_started_at
                    AND NEW.release_outcome IN (
                        'exact_absent_v1', 'exact_replacement_v1',
                        'exact_live_unprotected_v1'
                    )
                    AND NEW.abort_fence_protocol IS NULL
                    AND NEW.abort_fence_resource_version IS NULL
                    AND NEW.abort_fence_value IS NULL
                    AND NEW.released_at
                        IS NOT DISTINCT FROM transaction_timestamp())
           ) THEN
            RAISE EXCEPTION 'warm binding protection transition is not exact'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_warm_binding_authority';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status <> 'planned' THEN
        RAISE EXCEPTION 'warm binding protection must begin planned'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_warm_binding_authority';
    END IF;
    SELECT * INTO thread_row FROM public.threads
     WHERE id = NEW.thread_id FOR KEY SHARE;
    SELECT * INTO agent_row FROM public.agents
     WHERE id = NEW.agent_id FOR KEY SHARE;
    marker := COALESCE(thread_row.metadata->'agent_pod', '{}'::jsonb);
    IF thread_row.id IS NULL
       OR thread_row.execution_lane <> 'pinned'
       OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR thread_row.runtime_retirement_token IS NOT NULL
       OR thread_row.status NOT IN ('created', 'active', 'awaiting_user', 'suspended')
       OR agent_row.id IS NULL
       OR agent_row.hostname IS DISTINCT FROM NEW.pod_name
       OR agent_row.pod_uid IS DISTINCT FROM NEW.pod_uid
       OR agent_row.current_job_id IS NOT NULL
       OR marker NOT IN ('null'::jsonb, '{}'::jsonb)
       OR (
            NEW.source = 'attach'
            AND (
                thread_row.agent_id IS NOT NULL
                OR thread_row.control_admission_agent_id IS NOT NULL
                OR thread_row.runtime_attach_token IS NOT NULL
                OR agent_row.thread_id IS NOT NULL
                OR agent_row.status::text <> 'ready'
            )
       )
       OR (
            NEW.source = 'legacy_binding'
            AND (
                thread_row.agent_id IS DISTINCT FROM NEW.agent_id
                OR thread_row.runtime_attach_token
                    IS DISTINCT FROM NEW.runtime_attach_token
                OR agent_row.thread_id IS DISTINCT FROM NEW.thread_id
                OR agent_row.status::text <> 'session'
            )
       ) THEN
        RAISE EXCEPTION 'warm binding protection lacks exact open authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_warm_binding_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS thread_agent_warm_binding_authority
    ON public.thread_agent_warm_binding_protections;
CREATE TRIGGER thread_agent_warm_binding_authority
BEFORE INSERT OR UPDATE OR DELETE
ON public.thread_agent_warm_binding_protections
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_agent_warm_binding_protection();

CREATE OR REPLACE FUNCTION public.validate_thread_agent_warm_binding_protection()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    thread_row public.threads%ROWTYPE;
    agent_row public.agents%ROWTYPE;
    marker jsonb;
BEGIN
    SELECT * INTO thread_row FROM public.threads WHERE id = NEW.thread_id;
    SELECT * INTO agent_row FROM public.agents WHERE id = NEW.agent_id;
    marker := COALESCE(thread_row.metadata->'agent_pod', '{}'::jsonb);

    IF NEW.status IN ('planned', 'protecting', 'protected')
       AND NEW.source = 'attach' THEN
        IF thread_row.id IS NULL
           OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
           OR thread_row.runtime_retirement_token IS NOT NULL
           OR thread_row.agent_id IS NOT NULL
           OR thread_row.runtime_attach_token IS NOT NULL
           OR agent_row.id IS NULL
           OR agent_row.thread_id IS NOT NULL
           OR agent_row.current_job_id IS NOT NULL
           OR agent_row.status::text <> 'draining' THEN
            RAISE EXCEPTION 'warm attach plan is not reciprocal'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_warm_binding_reciprocity';
        END IF;
    ELSIF NEW.status IN ('planned', 'protecting', 'protected')
          AND NEW.source = 'legacy_binding' THEN
        IF thread_row.id IS NULL
           OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
           OR thread_row.runtime_retirement_token IS NOT NULL
           OR thread_row.agent_id IS DISTINCT FROM NEW.agent_id
           OR thread_row.runtime_attach_token
                IS DISTINCT FROM NEW.runtime_attach_token
           OR agent_row.id IS NULL
           OR agent_row.thread_id IS DISTINCT FROM NEW.thread_id
           OR agent_row.status::text <> 'session' THEN
            RAISE EXCEPTION 'legacy warm binding plan is not reciprocal'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_warm_binding_reciprocity';
        END IF;
    ELSIF NEW.status = 'bound' THEN
        IF thread_row.id IS NULL
           OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
           OR thread_row.agent_id IS DISTINCT FROM NEW.agent_id
           OR thread_row.runtime_attach_token
                IS DISTINCT FROM NEW.runtime_attach_token
           OR agent_row.id IS NULL
           OR agent_row.thread_id IS DISTINCT FROM NEW.thread_id
           OR agent_row.status::text <> 'session'
           OR marker->>'warm_binding_protection'
                IS DISTINCT FROM NEW.protection_id::text
           OR marker->>'pod_name' IS DISTINCT FROM NEW.pod_name
           OR marker->>'pod_uid' IS DISTINCT FROM NEW.pod_uid
           OR marker->>'runtime_generation'
                IS DISTINCT FROM NEW.runtime_generation::text
           OR marker->>'namespace' IS DISTINCT FROM NEW.namespace
           OR marker->>'protection_protocol' <> 'finalizer_v1' THEN
            RAISE EXCEPTION 'warm binding publication is not reciprocal'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_warm_binding_reciprocity';
        END IF;
    ELSIF NEW.status = 'releasing' THEN
        IF thread_row.id IS NULL
           OR thread_row.agent_id IS NOT DISTINCT FROM NEW.agent_id
           OR agent_row.id IS NULL
           OR agent_row.thread_id IS NOT NULL
           OR agent_row.status::text <> 'draining' THEN
            RAISE EXCEPTION 'warm binding release is not fenced'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_warm_binding_reciprocity';
        END IF;
    ELSIF NEW.status IN ('released', 'aborted') THEN
        IF thread_row.id IS NOT NULL
           AND thread_row.agent_id IS NOT DISTINCT FROM NEW.agent_id THEN
            RAISE EXCEPTION 'released warm Pod remains thread authority'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_warm_binding_reciprocity';
        END IF;
        IF agent_row.id IS NOT NULL AND (
            agent_row.thread_id IS NOT NULL
            OR agent_row.status::text NOT IN ('ready', 'offline')
        ) THEN
            RAISE EXCEPTION 'released warm Pod remains reserved'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_warm_binding_reciprocity';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS thread_agent_warm_binding_reciprocity
    ON public.thread_agent_warm_binding_protections;
CREATE CONSTRAINT TRIGGER thread_agent_warm_binding_reciprocity
AFTER INSERT OR UPDATE
ON public.thread_agent_warm_binding_protections
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION public.validate_thread_agent_warm_binding_protection();

-- A mixed/raw writer may not bypass the planned->protected edge. Dedicated
-- Pods remain governed by their published 0185 create intent; every other
-- same-generation pinned bind must publish the exact warm protection marker.
CREATE OR REPLACE FUNCTION public.enforce_pinned_warm_binding_publication()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    marker jsonb;
    warm_authorized boolean := false;
    dedicated_authorized boolean := false;
BEGIN
    IF OLD.execution_lane <> 'pinned'
       OR NEW.runtime_generation IS DISTINCT FROM OLD.runtime_generation THEN
        RETURN NEW;
    END IF;
    marker := COALESCE(NEW.metadata->'agent_pod', '{}'::jsonb);
    IF NEW.agent_id IS NOT NULL AND marker NOT IN ('null'::jsonb, '{}'::jsonb) THEN
        warm_authorized := EXISTS (
            SELECT 1 FROM public.thread_agent_warm_binding_protections warm
             WHERE warm.protection_id::text = marker->>'warm_binding_protection'
               AND warm.thread_id = NEW.id
               AND warm.runtime_generation = NEW.runtime_generation
               AND warm.runtime_attach_token = NEW.runtime_attach_token
               AND warm.agent_id = NEW.agent_id
               AND warm.status IN ('protected', 'bound')
               AND warm.namespace = marker->>'namespace'
               AND warm.pod_name = marker->>'pod_name'
               AND warm.pod_uid = marker->>'pod_uid'
               AND marker->>'protection_protocol' = 'finalizer_v1'
        );
        dedicated_authorized := EXISTS (
            SELECT 1 FROM public.thread_agent_pod_provision_intents intent
             WHERE intent.attempt_id::text = marker->>'provision_attempt'
               AND intent.thread_id = NEW.id
               AND intent.runtime_generation = NEW.runtime_generation
               AND intent.status = 'published'
               AND intent.pod_name = marker->>'pod_name'
               AND intent.pod_uid = marker->>'pod_uid'
               AND intent.namespace = marker->>'namespace'
               AND intent.protection_protocol = 'finalizer_v1'
        );
    END IF;
    IF (
        OLD.agent_id IS NULL AND NEW.agent_id IS NOT NULL
        OR COALESCE(OLD.metadata->'agent_pod', '{}'::jsonb)
             IN ('null'::jsonb, '{}'::jsonb)
           AND marker NOT IN ('null'::jsonb, '{}'::jsonb)
           AND NEW.agent_id IS NOT NULL
    ) AND NOT (warm_authorized OR dedicated_authorized) THEN
        RAISE EXCEPTION 'pinned binding lacks protected Kubernetes authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_pinned_warm_binding_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS zzz_threads_pinned_warm_binding_authority
    ON public.threads;
CREATE TRIGGER zzz_threads_pinned_warm_binding_authority
BEFORE UPDATE ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.enforce_pinned_warm_binding_publication();

CREATE OR REPLACE FUNCTION public.enforce_pinned_warm_agent_reservation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.thread_agent_warm_binding_protections warm
         WHERE warm.agent_id = NEW.id
           AND warm.status IN ('planned', 'protecting', 'protected', 'releasing')
    ) AND (
        NEW.thread_id IS NOT NULL
        OR NEW.current_job_id IS NOT NULL
        OR NEW.status::text <> 'draining'
    ) THEN
        RAISE EXCEPTION 'warm Pod has unresolved protection authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'agents_pinned_warm_binding_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS zzz_agents_pinned_warm_binding_authority
    ON public.agents;
CREATE TRIGGER zzz_agents_pinned_warm_binding_authority
BEFORE UPDATE ON public.agents
FOR EACH ROW
EXECUTE FUNCTION public.enforce_pinned_warm_agent_reservation();

CREATE OR REPLACE FUNCTION public.enforce_pinned_warm_create_exclusion()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.thread_agent_warm_binding_protections warm
         WHERE warm.thread_id = NEW.thread_id
           AND warm.runtime_generation = NEW.runtime_generation
           AND warm.status IN ('planned', 'protecting', 'protected', 'releasing')
    ) THEN
        RAISE EXCEPTION 'pinned Pod create races warm binding protection'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_warm_binding_create_exclusion';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS zzz_thread_agent_warm_binding_create_exclusion
    ON public.thread_agent_pod_provision_intents;
CREATE TRIGGER zzz_thread_agent_warm_binding_create_exclusion
BEFORE INSERT ON public.thread_agent_pod_provision_intents
FOR EACH ROW
EXECUTE FUNCTION public.enforce_pinned_warm_create_exclusion();

CREATE TABLE IF NOT EXISTS public.thread_agent_pod_recycle_handoffs (
    thread_id uuid NOT NULL,
    runtime_generation uuid NOT NULL,
    recycle_generation uuid NOT NULL,
    predecessor_attempt_id uuid NOT NULL UNIQUE
        REFERENCES public.thread_agent_pod_provision_intents(attempt_id)
        ON DELETE RESTRICT,
    predecessor_pod_uid text NOT NULL,
    successor_attempt_id uuid NOT NULL UNIQUE,
    workspace_claim_id uuid NOT NULL
        REFERENCES public.thread_agent_workspace_claims(claim_id)
        ON DELETE RESTRICT,
    namespace varchar(253) NOT NULL,
    pod_name varchar(253) NOT NULL,
    process_zero_protocol varchar(48) NOT NULL,
    process_zero_observed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (thread_id, runtime_generation, recycle_generation),
    CONSTRAINT thread_agent_pod_recycle_successor_fk
        FOREIGN KEY (successor_attempt_id)
        REFERENCES public.thread_agent_pod_provision_intents(attempt_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (predecessor_attempt_id <> successor_attempt_id),
    CHECK (predecessor_pod_uid <> '' AND pod_name <> ''),
    CHECK (
        length(namespace) BETWEEN 1 AND 63
        AND namespace ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
    ),
    CHECK (process_zero_protocol = 'finalized_exact_terminal_v1'),
    CHECK (process_zero_observed_at <= created_at)
);

CREATE INDEX IF NOT EXISTS idx_thread_agent_pod_recycle_handoff_successor
    ON public.thread_agent_pod_recycle_handoffs(successor_attempt_id);

CREATE OR REPLACE FUNCTION public.enforce_thread_agent_pod_recycle_handoff()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    thread_row public.threads%ROWTYPE;
    predecessor public.thread_agent_pod_provision_intents%ROWTYPE;
    claim_row public.thread_agent_workspace_claims%ROWTYPE;
    marker jsonb;
    recycle jsonb;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'pinned agent Pod recycle handoff is append-only'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_pod_recycle_handoff_authority';
    END IF;

    SELECT * INTO thread_row FROM public.threads
     WHERE id = NEW.thread_id FOR KEY SHARE;
    SELECT * INTO predecessor
      FROM public.thread_agent_pod_provision_intents
     WHERE attempt_id = NEW.predecessor_attempt_id FOR KEY SHARE;
    SELECT * INTO claim_row FROM public.thread_agent_workspace_claims
     WHERE claim_id = NEW.workspace_claim_id FOR KEY SHARE;
    marker := COALESCE(thread_row.metadata->'agent_pod', '{}'::jsonb);
    recycle := COALESCE(marker->'recycle', '{}'::jsonb);

    IF thread_row.id IS NULL
       OR thread_row.execution_lane <> 'pinned'
       OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR thread_row.runtime_retirement_token IS NOT NULL
       OR thread_row.status NOT IN ('created', 'active', 'awaiting_user', 'suspended')
       OR thread_row.agent_id IS NOT NULL
       OR thread_row.control_admission_agent_id IS NOT NULL
       OR thread_row.runtime_attach_token IS NOT NULL
       OR predecessor.attempt_id IS NULL
       OR predecessor.thread_id IS DISTINCT FROM NEW.thread_id
       OR predecessor.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR predecessor.provisioner <> 'persistent'
       OR predecessor.workspace_claim_id IS DISTINCT FROM NEW.workspace_claim_id
       OR predecessor.namespace IS DISTINCT FROM NEW.namespace
       OR predecessor.protection_protocol <> 'finalizer_v1'
       OR predecessor.pod_name IS DISTINCT FROM NEW.pod_name
       OR predecessor.status <> 'published'
       OR predecessor.pod_uid IS DISTINCT FROM NEW.predecessor_pod_uid
       OR claim_row.claim_id IS NULL
       OR claim_row.thread_id IS DISTINCT FROM NEW.thread_id
       OR claim_row.provisioner <> 'persistent'
       OR claim_row.namespace IS DISTINCT FROM NEW.namespace
       OR claim_row.protection_protocol <> 'finalizer_v1'
       OR claim_row.status <> 'ready'
       OR NULLIF(claim_row.pvc_uid, '') IS NULL
       OR marker->>'pod_name' IS DISTINCT FROM NEW.pod_name
       OR marker->>'pod_uid' IS DISTINCT FROM NEW.predecessor_pod_uid
       OR marker->>'provision_attempt'
            IS DISTINCT FROM NEW.predecessor_attempt_id::text
       OR marker->>'runtime_generation'
            IS DISTINCT FROM NEW.runtime_generation::text
       OR marker->>'namespace' IS DISTINCT FROM NEW.namespace
       OR marker->>'protection_protocol' <> 'finalizer_v1'
       OR recycle->>'generation' IS DISTINCT FROM NEW.recycle_generation::text
       OR recycle->>'phase' <> 'fencing_old_authority'
       OR NULLIF(recycle->>'old_pod_terminal_at', '') IS NULL THEN
        RAISE EXCEPTION 'pinned agent Pod recycle handoff lacks exact authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_pod_recycle_handoff_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS thread_agent_pod_recycle_handoff_authority
    ON public.thread_agent_pod_recycle_handoffs;
CREATE TRIGGER thread_agent_pod_recycle_handoff_authority
BEFORE INSERT OR UPDATE OR DELETE
ON public.thread_agent_pod_recycle_handoffs
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_agent_pod_recycle_handoff();

-- A second same-generation Pod intent is legal only as the exact successor
-- named by a handoff inserted earlier in the same transaction.  The deferred
-- FK prevents committing a handoff without that successor.
CREATE OR REPLACE FUNCTION public.enforce_thread_agent_pod_recycle_successor()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    prior_count integer;
    handoff public.thread_agent_pod_recycle_handoffs%ROWTYPE;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RETURN NEW;
    END IF;
    SELECT count(*) INTO prior_count
      FROM public.thread_agent_pod_provision_intents prior
     WHERE prior.thread_id = NEW.thread_id
       AND prior.runtime_generation = NEW.runtime_generation
       AND prior.status = 'published'
       AND NOT EXISTS (
           SELECT 1 FROM public.thread_agent_pod_recycle_handoffs retired
            WHERE retired.predecessor_attempt_id = prior.attempt_id
       );
    IF prior_count = 0 THEN
        RETURN NEW;
    END IF;
    SELECT * INTO handoff
      FROM public.thread_agent_pod_recycle_handoffs
     WHERE successor_attempt_id = NEW.attempt_id;
    IF prior_count <> 1
       OR handoff.successor_attempt_id IS NULL
       OR handoff.thread_id IS DISTINCT FROM NEW.thread_id
       OR handoff.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR handoff.workspace_claim_id IS DISTINCT FROM NEW.workspace_claim_id
       OR handoff.namespace IS DISTINCT FROM NEW.namespace
       OR handoff.pod_name IS DISTINCT FROM NEW.pod_name
       OR NEW.provisioner <> 'persistent'
       OR NEW.protection_protocol <> 'finalizer_v1' THEN
        RAISE EXCEPTION 'published pinned Pod authority lacks exact recycle handoff'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_pod_recycle_successor_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS zzy_thread_agent_pod_recycle_successor_authority
    ON public.thread_agent_pod_provision_intents;
CREATE TRIGGER zzy_thread_agent_pod_recycle_successor_authority
BEFORE INSERT
ON public.thread_agent_pod_provision_intents
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_agent_pod_recycle_successor();

CREATE OR REPLACE FUNCTION public.validate_thread_agent_pod_recycle_handoff()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    successor public.thread_agent_pod_provision_intents%ROWTYPE;
    thread_row public.threads%ROWTYPE;
    marker jsonb;
    recycle jsonb;
BEGIN
    SELECT * INTO successor
      FROM public.thread_agent_pod_provision_intents
     WHERE attempt_id = NEW.successor_attempt_id;
    SELECT * INTO thread_row FROM public.threads WHERE id = NEW.thread_id;
    marker := COALESCE(thread_row.metadata->'agent_pod', '{}'::jsonb);
    recycle := COALESCE(marker->'recycle', '{}'::jsonb);
    IF successor.attempt_id IS NULL
       OR successor.thread_id IS DISTINCT FROM NEW.thread_id
       OR successor.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR successor.provisioner <> 'persistent'
       OR successor.workspace_claim_id IS DISTINCT FROM NEW.workspace_claim_id
       OR successor.namespace IS DISTINCT FROM NEW.namespace
       OR successor.protection_protocol <> 'finalizer_v1'
       OR successor.pod_name IS DISTINCT FROM NEW.pod_name
       OR successor.status NOT IN ('planned', 'published')
       OR thread_row.id IS NULL
       OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR marker->>'pod_name' IS DISTINCT FROM NEW.pod_name
       OR recycle->>'generation' IS DISTINCT FROM NEW.recycle_generation::text
       OR recycle->>'successor_attempt'
            IS DISTINCT FROM NEW.successor_attempt_id::text THEN
        RAISE EXCEPTION 'pinned agent Pod recycle handoff is not reciprocal'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_pod_recycle_handoff_reciprocity';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS thread_agent_pod_recycle_handoff_reciprocity
    ON public.thread_agent_pod_recycle_handoffs;
CREATE CONSTRAINT TRIGGER thread_agent_pod_recycle_handoff_reciprocity
AFTER INSERT
ON public.thread_agent_pod_recycle_handoffs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION public.validate_thread_agent_pod_recycle_handoff();

COMMIT;
