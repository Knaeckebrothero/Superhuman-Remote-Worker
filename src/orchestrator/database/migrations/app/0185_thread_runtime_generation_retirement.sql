-- migration:     0185_thread_runtime_generation_retirement.sql
-- description:   Give every persistent thread an immutable runtime
--                generation and a durable, exact pinned-retirement marker.
-- depends-on:    0184_thread_ended_transition_fence.sql
-- maintenance-gate: pinned-runtime-authority-v1
-- expected:      Bounded scans/updates of threads, live protected readers,
--                and pending pinned controls; two outcome tables; multiple
--                lifecycle/immutability triggers and validated constraints.
--                The transaction intentionally aborts with SQLSTATE 23514
--                when a drained authority cannot be reconstructed exactly.
-- locks:         ACCESS EXCLUSIVE while columns/constraints are installed.
--                Requires a pinned-session maintenance window: stop every
--                pre-0185 create/Resume/prepare/End/status writer and drain
--                live pinned authorities before applying this migration.
-- transactional: yes
-- rollout:       Stop old mutating replicas, satisfy the explicit authority
--                gates below, migrate, deploy exact-generation writers, then
--                reopen pinned admission. Trigger-owned generation rotation
--                and attach-token minting are rollback defenses only; they do
--                not make a mixed old/new mutating fleet supported.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.threads
    ADD COLUMN IF NOT EXISTS runtime_generation uuid;

UPDATE public.threads
   SET runtime_generation = public.uuid_generate_v4()
 WHERE runtime_generation IS NULL;

ALTER TABLE public.threads
    ALTER COLUMN runtime_generation SET DEFAULT public.uuid_generate_v4(),
    -- The maintenance gate guarantees a drained fleet; this bounded scan is
    -- the final authority invariant before exact-generation writers start.
    -- squawk-ignore adding-not-nullable-field
    ALTER COLUMN runtime_generation SET NOT NULL,
    ADD COLUMN IF NOT EXISTS runtime_retirement_token uuid,
    ADD COLUMN IF NOT EXISTS runtime_retirement_permanent boolean,
    ADD COLUMN IF NOT EXISTS runtime_retirement_started_at timestamptz,
    ADD COLUMN IF NOT EXISTS runtime_retirement_authorized_at timestamptz,
    ADD COLUMN IF NOT EXISTS runtime_retirement_context jsonb,
    ADD COLUMN IF NOT EXISTS runtime_retirement_stage_receipt jsonb,
    ADD COLUMN IF NOT EXISTS runtime_retirement_local_quiescence jsonb,
    ADD COLUMN IF NOT EXISTS runtime_retirement_external_cleanup jsonb,
    ADD COLUMN IF NOT EXISTS runtime_authority_exposed boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS runtime_attach_token uuid,
    ADD COLUMN IF NOT EXISTS runtime_attach_abort_receipt jsonb;

ALTER TABLE public.cloud_ro_mounts
    ADD COLUMN IF NOT EXISTS runtime_generation uuid,
    ADD COLUMN IF NOT EXISTS engage_attempt uuid;

ALTER TABLE public.thread_control_requests
    ADD COLUMN IF NOT EXISTS runtime_generation uuid;

-- Exact destructive recovery cannot safely guess a Pod UID or VM/rootdisk
-- tuple from a partial historical blob. Stop the rollout instead of shipping
-- rows whose permanent retirement can only wedge or broaden deletion.
DO $$
DECLARE
    ro_authority_row record;
    parsed_grant jsonb;
BEGIN
    IF EXISTS (
        SELECT 1
          FROM public.threads
         WHERE execution_lane = 'pinned'
           AND (agent_id IS NOT NULL OR runtime_attach_token IS NOT NULL)
    ) THEN
        RAISE EXCEPTION
            '0185 requires every pinned runtime binding to be drained'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_runtime_attach_migration_authority';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.agents AS agent
          JOIN public.threads AS thread ON thread.id = agent.thread_id
         WHERE thread.execution_lane = 'pinned'
    ) THEN
        RAISE EXCEPTION
            '0185 requires every inverse pinned agent binding to be drained'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'agents_thread_migration_authority';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.threads AS thread
          JOIN public.docker_workspace_leases AS lease
            ON lease.owner_kind = 'thread'
           AND lease.owner_id = thread.id
           AND lease.status IN ('ready', 'releasing')
         WHERE thread.execution_lane = 'pinned'
         GROUP BY thread.id, thread.metadata
        HAVING count(*) <> 1
            OR bool_or(
                jsonb_typeof(thread.metadata) IS DISTINCT FROM 'object'
                OR jsonb_typeof(thread.metadata->'workspace_container')
                    IS DISTINCT FROM 'object'
                OR jsonb_typeof(thread.metadata->'_workspace_binding')
                    IS DISTINCT FROM 'object'
                OR thread.metadata->'workspace_container'->>'provisioner'
                    IS DISTINCT FROM 'docker'
                OR thread.metadata->'workspace_container'
                       ->>'_docker_workspace_lease_id'
                    IS DISTINCT FROM lease.lease_id::text
                OR thread.metadata->'workspace_container'->>'status'
                    IS DISTINCT FROM lease.status
                OR thread.metadata->'workspace_container'->>'host'
                    IS DISTINCT FROM lease.host
                OR thread.metadata->'workspace_container'->>'port'
                    IS DISTINCT FROM lease.port::text
                OR thread.metadata->'_workspace_binding'->>'kind'
                    IS DISTINCT FROM 'remote'
                OR right(
                       COALESCE(
                           thread.metadata->'_workspace_binding'->>'backing_id', ''
                       ),
                       length(lease.lease_id::text) + 1
                   ) IS DISTINCT FROM ':' || lease.lease_id::text
            )
    ) THEN
        RAISE EXCEPTION
            '0185 requires one reciprocal Docker inventory lease per pinned owner'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'docker_workspace_lease_migration_authority';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.threads
         WHERE execution_lane = 'pinned'
           AND status IN ('active', 'awaiting_user')
           AND (agent_id IS NULL OR runtime_attach_token IS NULL)
    ) THEN
        RAISE EXCEPTION
            '0185 requires reciprocal agent/attach authority for live pinned rows'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_live_runtime_migration_authority';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.threads
         WHERE execution_lane = 'pinned'
           AND status <> 'ended'
           AND metadata ? 'agent_pod'
           AND metadata->'agent_pod' IS NOT NULL
           AND metadata->'agent_pod' NOT IN ('null'::jsonb, '{}'::jsonb)
           AND (agent_id IS NULL OR runtime_attach_token IS NULL)
    ) THEN
        RAISE EXCEPTION
            '0185 requires reciprocal authority for every live agent Pod'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_live_agent_pod_migration_authority';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.threads
         WHERE execution_lane = 'pinned'
           AND status = 'ended'
           AND (
               agent_id IS NOT NULL
               OR control_admission_agent_id IS NOT NULL
               OR runtime_attach_token IS NOT NULL
               OR (
                   metadata ? 'agent_pod'
                   AND metadata->'agent_pod' IS NOT NULL
                   AND metadata->'agent_pod'
                       NOT IN ('null'::jsonb, '{}'::jsonb)
               )
           )
    ) THEN
        RAISE EXCEPTION
            '0185 requires ended pinned rows to be fully detached before rollout'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_ended_migration_authority';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.threads
         WHERE execution_lane = 'pinned'
           AND metadata ? 'agent_pod'
           AND metadata->'agent_pod' IS NOT NULL
           AND metadata->'agent_pod' NOT IN ('null'::jsonb, '{}'::jsonb)
           AND (
               jsonb_typeof(metadata->'agent_pod') IS DISTINCT FROM 'object'
               OR NULLIF(metadata->'agent_pod'->>'pod_name', '') IS NULL
               OR NULLIF(metadata->'agent_pod'->>'pod_uid', '') IS NULL
           )
    ) THEN
        RAISE EXCEPTION
            '0185 requires complete pinned agent_pod name/UID authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_agent_pod_migration_authority';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.threads AS thread
          JOIN public.agents AS agent ON agent.id = thread.agent_id
         WHERE thread.execution_lane = 'pinned'
           AND thread.metadata ? 'agent_pod'
           AND thread.metadata->'agent_pod' IS NOT NULL
           AND thread.metadata->'agent_pod'
               NOT IN ('null'::jsonb, '{}'::jsonb)
           AND (
               thread.metadata->'agent_pod'->>'pod_name'
                   IS DISTINCT FROM agent.hostname
               OR thread.metadata->'agent_pod'->>'pod_uid'
                   IS DISTINCT FROM agent.pod_uid
           )
    ) THEN
        RAISE EXCEPTION
            '0185 requires one exact pinned agent Pod identity'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_agent_pod_migration_identity';
    END IF;
    IF EXISTS (
        WITH authority AS (
            SELECT thread.id,
                   thread.metadata->'workspace_container' AS workspace,
                   thread.metadata->'_workspace_binding' AS binding,
                   (
                       COALESCE(
                           thread.metadata->'workspace_container'->>'status', ''
                       ) NOT IN ('', 'deleted')
                       OR thread.metadata->'workspace_container'
                              ->>'_runtime_incarnation' IS NOT NULL
                       OR thread.metadata->'workspace_container'->>'pod_ip'
                              IS NOT NULL
                       OR thread.metadata->'workspace_container'->>'pod_name'
                              IS NOT NULL
                       OR thread.metadata->'workspace_container'->>'host'
                              IS NOT NULL
                       OR thread.metadata->'workspace_container'->>'port'
                              IS NOT NULL
                       OR thread.metadata->'workspace_container'->>'ide_host'
                              IS NOT NULL
                       OR thread.metadata->'workspace_container'->>'ide_port'
                              IS NOT NULL
                       OR thread.metadata->'workspace_container'
                              ->>'_canvas_workspace_generation' IS NOT NULL
                       OR thread.metadata->'workspace_container'
                              ->>'_docker_workspace_lease_id' IS NOT NULL
                   ) AS workspace_evidence,
                   (
                       thread.metadata ? '_workspace_binding'
                       AND thread.metadata->'_workspace_binding' IS NOT NULL
                       AND thread.metadata->'_workspace_binding'
                           NOT IN ('null'::jsonb, '{}'::jsonb)
                   ) AS binding_evidence
              FROM public.threads AS thread
             WHERE thread.execution_lane = 'pinned'
        )
        SELECT 1
          FROM authority
         WHERE (
             (workspace IS NOT NULL AND workspace <> 'null'::jsonb
              AND jsonb_typeof(workspace) IS DISTINCT FROM 'object')
             OR (binding IS NOT NULL AND binding <> 'null'::jsonb
                 AND jsonb_typeof(binding) IS DISTINCT FROM 'object')
             OR (
                 (workspace_evidence OR binding_evidence)
                 AND NOT (
                     -- Logical object-store backing: no Pod/lease actuator.
                     (
                         NOT workspace_evidence
                         AND binding->>'kind' = 'virtual'
                         AND binding->>'backing_id' ~ '^rclone:[0-9a-f]{64}$'
                         AND binding->>'generation'
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                     )
                     OR
                     -- Kubernetes: immutable Pod UID plus exact Pod/PVC UID
                     -- embedded in the remote backing.
                     (
                         binding->>'kind' = 'remote'
                         AND binding->>'backing_id'
                             ~* '^k8s-(pvc|pod):[^:]+:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                         AND workspace->>'_runtime_incarnation'
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                         AND binding->>'generation'
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                         AND (
                             binding->>'backing_id' NOT LIKE 'k8s-pod:%'
                             OR split_part(binding->>'backing_id', ':', 3)
                                  = workspace->>'_runtime_incarnation'
                         )
                         AND (
                             workspace->>'_canvas_workspace_generation' IS NULL
                             OR workspace->>'_canvas_workspace_generation'
                                  = binding->>'generation'
                         )
                     )
                     OR
                     -- Static Docker workspace: the durable inventory lease,
                     -- not a Kubernetes runtime incarnation, is authority.
                     (
                         workspace->>'provisioner' = 'docker'
                         AND binding->>'kind' = 'remote'
                         AND binding->>'generation'
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                         AND workspace->>'_docker_workspace_lease_id'
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                         AND right(
                             binding->>'backing_id',
                             length(workspace->>'_docker_workspace_lease_id') + 1
                         ) = ':' || (
                             workspace->>'_docker_workspace_lease_id'
                         )
                         AND EXISTS (
                             SELECT 1
                               FROM public.docker_workspace_leases AS lease
                              WHERE lease.owner_kind = 'thread'
                                AND lease.owner_id = authority.id
                                AND lease.lease_id::text
                                    = workspace->>'_docker_workspace_lease_id'
                                AND lease.status IN ('ready', 'releasing')
                         )
                     )
                 )
             )
         )
    ) THEN
        RAISE EXCEPTION
            '0185 requires complete pinned sandbox teardown authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_workspace_migration_authority';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.cloud_ro_mounts AS ro
          JOIN public.threads AS thread ON thread.id = ro.thread_id
         WHERE thread.execution_lane = 'pinned'
           AND ro.status IN ('engaging', 'active', 'revoking')
           AND ro.backend IS DISTINCT FROM 'nextcloud'
    ) THEN
        RAISE EXCEPTION
            '0185 cannot recover a live non-Nextcloud protected reader'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_migration_backend';
    END IF;
    -- A live Nextcloud grant is recoverable only through its exact group.
    -- Validate the opaque JSON without letting one malformed historical row
    -- abort with an implementation-specific json cast error: operators need
    -- the stable authority constraint name to identify and drain the row.
    FOR ro_authority_row IN
        SELECT ro.id, ro.grant_handle, ro.reader_id,
               thread.id AS thread_id
          FROM public.cloud_ro_mounts AS ro
          JOIN public.threads AS thread ON thread.id = ro.thread_id
         WHERE thread.execution_lane = 'pinned'
           AND ro.status IN ('engaging', 'active', 'revoking')
    LOOP
        BEGIN
            parsed_grant := ro_authority_row.grant_handle::jsonb;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION
                '0185 cannot recover malformed live Nextcloud reader %',
                ro_authority_row.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'cloud_ro_mounts_migration_grant_identity';
        END;
        IF jsonb_typeof(parsed_grant) IS DISTINCT FROM 'object'
           OR parsed_grant->>'group_id'
                IS DISTINCT FROM 'srw-rog-' || left(
                    ro_authority_row.thread_id::text, 16
                )
           OR parsed_grant->>'reader_id'
                IS DISTINCT FROM ro_authority_row.reader_id THEN
            RAISE EXCEPTION
                '0185 cannot recover malformed live Nextcloud reader %',
                ro_authority_row.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'cloud_ro_mounts_migration_grant_identity';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1
          FROM public.threads
         WHERE execution_lane = 'pinned'
           AND metadata ? 'vm'
           AND metadata->'vm' IS NOT NULL
           AND metadata->'vm' <> 'null'::jsonb
           AND (
               jsonb_typeof(metadata->'vm') IS DISTINCT FROM 'object'
               OR (
                   (
                       COALESCE(metadata->'vm'->>'status', '')
                           NOT IN ('', 'deleted')
                       OR metadata->'vm'->>'provision_generation' IS NOT NULL
                       OR metadata->'vm'->>'identity_provision_generation' IS NOT NULL
                       OR metadata->'vm'->>'vm_uid' IS NOT NULL
                       OR metadata->'vm'->>'_runtime_incarnation' IS NOT NULL
                       OR metadata->'vm'->>'rootdisk_pvc_uid' IS NOT NULL
                       OR metadata->'vm'->>'ssh_host' IS NOT NULL
                       OR metadata->'vm'->>'ssh_port' IS NOT NULL
                       OR metadata->'vm'->>'_canvas_workspace_generation' IS NOT NULL
                   )
                   AND (
                       NULLIF(metadata->'vm'->>'provision_generation', '') IS NULL
                       OR NULLIF(metadata->'vm'->>'vm_uid', '') IS NULL
                       OR NULLIF(metadata->'vm'->>'rootdisk_pvc_uid', '') IS NULL
                       OR metadata->'vm'->>'provision_generation'
                              !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                       OR metadata->'vm'->>'vm_uid'
                              !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                       OR metadata->'vm'->>'rootdisk_pvc_uid'
                              !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                       OR metadata->'vm'->'identity_authenticated'
                              IS DISTINCT FROM 'true'::jsonb
                       OR metadata->'vm'->>'identity_provision_generation'
                              IS DISTINCT FROM
                                  metadata->'vm'->>'provision_generation'
                   )
               )
           )
    ) THEN
        RAISE EXCEPTION
            '0185 requires complete pinned VM generation/UID/rootdisk authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_vm_migration_authority';
    END IF;
END;
$$;

-- A rolling upgrade cannot reconstruct every historical attach after its
-- reciprocal row was released.  Preserve the observable evidence
-- conservatively; the trigger below makes the bit monotonic for all future
-- same-generation writes, including old bind SQL that knows nothing about
-- this column.
UPDATE public.threads
   SET runtime_authority_exposed = true
 WHERE execution_lane = 'pinned'
   AND (
        agent_id IS NOT NULL
        OR runtime_attach_token IS NOT NULL
        OR status IN ('active', 'awaiting_user')
        OR (
            metadata ? 'agent_pod'
            AND metadata->'agent_pod' IS NOT NULL
            AND metadata->'agent_pod' NOT IN ('null'::jsonb, '{}'::jsonb)
        )
   );

-- Existing live readers predate the exact engage-attempt columns.  Their
-- remote grant handle is already durable, so assigning the joined thread life
-- and one fresh attempt makes exact retirement/reconciliation possible without
-- broad grant deletion.
UPDATE public.cloud_ro_mounts AS ro
   SET runtime_generation = COALESCE(ro.runtime_generation, thread.runtime_generation),
       engage_attempt = COALESCE(ro.engage_attempt, public.uuid_generate_v4())
  FROM public.threads AS thread
 WHERE ro.thread_id = thread.id
   AND ro.status IN ('engaging', 'active', 'revoking')
   AND (ro.runtime_generation IS NULL OR ro.engage_attempt IS NULL);

-- Pending controls remain durable obligations across an End/Resume. Attribute
-- every pre-0185 row to the life currently carrying it; the lifecycle trigger
-- below transfers still-pending rows atomically on the sole DB-owned generation
-- rotation edge. Stateless consumption remains lease-token fenced, but its
-- durable intent attribution is no longer silently NULL/cross-generation.
UPDATE public.thread_control_requests AS request
   SET runtime_generation = thread.runtime_generation
  FROM public.threads AS thread
 WHERE request.thread_id = thread.id
   AND thread.execution_lane IN ('pinned', 'stateless')
   AND request.outcome IS NULL
   AND request.runtime_generation IS NULL;

CREATE OR REPLACE FUNCTION public.capture_pinned_control_runtime_generation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    lane text;
    generation uuid;
    retirement_token uuid;
BEGIN
    SELECT execution_lane, runtime_generation, runtime_retirement_token
      INTO lane, generation, retirement_token
      FROM public.threads
     WHERE id = NEW.thread_id
     FOR SHARE;
    IF lane IN ('pinned', 'stateless') THEN
        IF lane = 'pinned' AND retirement_token IS NOT NULL THEN
            RAISE EXCEPTION
                'thread % runtime retirement closes control admission', NEW.thread_id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_control_runtime_retirement';
        END IF;
        IF NEW.runtime_generation IS NULL THEN
            NEW.runtime_generation := generation;
        ELSIF NEW.runtime_generation IS DISTINCT FROM generation THEN
            RAISE EXCEPTION
                'thread % control runtime generation changed', NEW.thread_id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_control_runtime_generation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS thread_control_capture_runtime_generation
    ON public.thread_control_requests;
CREATE TRIGGER thread_control_capture_runtime_generation
BEFORE INSERT ON public.thread_control_requests
FOR EACH ROW
EXECUTE FUNCTION public.capture_pinned_control_runtime_generation();

-- A Kubernetes Pod can be accepted server-side while the create response is
-- lost.  The live ``metadata.agent_pod`` tuple deliberately remains
-- all-or-nothing (name + immutable UID), so persist the chosen name and an
-- attempt label before the first create effect.  No bootstrap credential is
-- stored here.  Planned attempts independently fence thread deletion and are
-- resolved only by exact UID publication or attempt-labelled absence.
CREATE TABLE IF NOT EXISTS public.thread_agent_workspace_claims (
    claim_id uuid PRIMARY KEY,
    -- Like the Pod-name fence below, a permanent delete leaves a retained
    -- PVC-name tombstone for restart-safe garbage collection.
    thread_id uuid NOT NULL UNIQUE,
    created_runtime_generation uuid NOT NULL,
    create_attempt uuid NOT NULL,
    provisioner varchar(16) NOT NULL
        CHECK (provisioner IN ('agent', 'persistent')),
    pvc_name varchar(253) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'ready', 'revoking', 'fenced', 'reclaimed')),
    pvc_uid text,
    created_at timestamptz NOT NULL DEFAULT now(),
    fenced_at timestamptz,
    gc_after timestamptz,
    resolved_at timestamptz,
    CHECK (pvc_name <> ''),
    CHECK (
        (status = 'planned' AND pvc_uid IS NULL
            AND fenced_at IS NULL AND gc_after IS NULL
            AND resolved_at IS NULL)
        OR (status = 'ready' AND NULLIF(pvc_uid, '') IS NOT NULL
            AND fenced_at IS NULL AND gc_after IS NULL
            AND resolved_at IS NOT NULL)
        OR (status = 'revoking' AND fenced_at IS NULL
            AND gc_after IS NULL AND resolved_at IS NULL)
        OR (status = 'fenced' AND NULLIF(pvc_uid, '') IS NOT NULL
            AND fenced_at IS NOT NULL
            AND gc_after >= fenced_at + interval '10 minutes'
            AND resolved_at IS NULL)
        OR (status = 'reclaimed' AND fenced_at IS NOT NULL
            AND gc_after IS NOT NULL AND resolved_at IS NOT NULL)
    )
);

CREATE OR REPLACE FUNCTION public.enforce_thread_agent_workspace_claim()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    thread_row public.threads%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'reclaimed' THEN
            RAISE EXCEPTION
                'live agent workspace claim % cannot be deleted', OLD.claim_id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_workspace_claim_authority';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.claim_id IS DISTINCT FROM OLD.claim_id
           OR NEW.thread_id IS DISTINCT FROM OLD.thread_id
           OR NEW.created_runtime_generation
              IS DISTINCT FROM OLD.created_runtime_generation
           OR NEW.create_attempt IS DISTINCT FROM OLD.create_attempt
           OR NEW.provisioner IS DISTINCT FROM OLD.provisioner
           OR NEW.pvc_name IS DISTINCT FROM OLD.pvc_name
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NOT (
                (OLD.status = 'planned' AND NEW.status = 'ready'
                    AND NULLIF(NEW.pvc_uid, '') IS NOT NULL
                    AND NEW.fenced_at IS NULL AND NEW.gc_after IS NULL
                    AND NEW.resolved_at IS NOT NULL)
                OR (OLD.status IN ('planned', 'ready')
                    AND NEW.status = 'revoking'
                    AND NEW.pvc_uid IS NOT DISTINCT FROM OLD.pvc_uid
                    AND NEW.fenced_at IS NULL AND NEW.gc_after IS NULL
                    AND NEW.resolved_at IS NULL)
                OR (OLD.status = 'revoking' AND NEW.status = 'fenced'
                    AND NULLIF(NEW.pvc_uid, '') IS NOT NULL
                    AND NEW.fenced_at IS NOT NULL
                    AND NEW.fenced_at IS NOT DISTINCT FROM transaction_timestamp()
                    AND NEW.gc_after >= NEW.fenced_at + interval '10 minutes'
                    AND NEW.resolved_at IS NULL)
                OR (OLD.status = 'fenced'
                    AND NEW.status = 'reclaimed'
                    AND NEW.pvc_uid IS NOT DISTINCT FROM OLD.pvc_uid
                    AND NEW.fenced_at IS NOT DISTINCT FROM OLD.fenced_at
                    AND NEW.gc_after IS NOT DISTINCT FROM OLD.gc_after
                    AND OLD.gc_after <= transaction_timestamp()
                    AND NEW.resolved_at
                        IS NOT DISTINCT FROM transaction_timestamp())
           ) THEN
            RAISE EXCEPTION
                'agent workspace claim % transition is not exact', OLD.claim_id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_workspace_claim_authority';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status <> 'planned' THEN
        RAISE EXCEPTION
            'agent workspace claim % must begin planned', NEW.claim_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_workspace_claim_authority';
    END IF;
    SELECT * INTO thread_row FROM public.threads
     WHERE id = NEW.thread_id FOR KEY SHARE;
    IF thread_row.id IS NULL
       OR thread_row.execution_lane <> 'pinned'
       OR thread_row.runtime_generation
          IS DISTINCT FROM NEW.created_runtime_generation
       OR thread_row.runtime_retirement_token IS NOT NULL
       OR thread_row.status NOT IN ('created', 'active', 'awaiting_user', 'suspended')
       OR thread_row.agent_id IS NOT NULL
       OR thread_row.control_admission_agent_id IS NOT NULL
       OR thread_row.runtime_attach_token IS NOT NULL THEN
        RAISE EXCEPTION
            'agent workspace claim % lacks open pinned authority', NEW.claim_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_workspace_claim_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS thread_agent_workspace_claim_authority
    ON public.thread_agent_workspace_claims;
CREATE TRIGGER thread_agent_workspace_claim_authority
BEFORE INSERT OR UPDATE OR DELETE
ON public.thread_agent_workspace_claims
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_agent_workspace_claim();

CREATE TABLE IF NOT EXISTS public.thread_agent_pod_provision_intents (
    attempt_id uuid PRIMARY KEY,
    -- No FK by design: permanent thread deletion leaves an exact-name fence
    -- as a durable cleanup work item until its API-server horizon expires.
    thread_id uuid NOT NULL,
    runtime_generation uuid NOT NULL,
    provisioner varchar(16) NOT NULL
        CHECK (provisioner IN ('agent', 'persistent')),
    workspace_claim_id uuid
        REFERENCES public.thread_agent_workspace_claims(claim_id),
    pod_name varchar(253) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'published', 'revoking', 'fenced', 'retired')),
    pod_uid text,
    created_at timestamptz NOT NULL DEFAULT now(),
    fenced_at timestamptz,
    gc_after timestamptz,
    resolved_at timestamptz,
    CHECK (pod_name <> ''),
    CHECK (
        (status IN ('planned', 'revoking') AND pod_uid IS NULL
            AND fenced_at IS NULL AND gc_after IS NULL
            AND resolved_at IS NULL)
        OR (status = 'published' AND NULLIF(pod_uid, '') IS NOT NULL
            AND fenced_at IS NULL AND gc_after IS NULL
            AND resolved_at IS NOT NULL)
        OR (status = 'fenced' AND NULLIF(pod_uid, '') IS NOT NULL
            AND fenced_at IS NOT NULL
            AND gc_after >= fenced_at + interval '10 minutes'
            AND resolved_at IS NULL)
        OR (status = 'retired' AND fenced_at IS NOT NULL
            AND gc_after IS NOT NULL AND resolved_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_agent_pod_provision_planned
    ON public.thread_agent_pod_provision_intents(thread_id)
    WHERE status IN ('planned', 'revoking', 'fenced');

CREATE INDEX IF NOT EXISTS idx_thread_agent_pod_provision_generation
    ON public.thread_agent_pod_provision_intents
       (thread_id, runtime_generation, created_at DESC);

CREATE OR REPLACE FUNCTION public.enforce_thread_agent_pod_provision_intent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    thread_row public.threads%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('planned', 'revoking', 'fenced') THEN
            RAISE EXCEPTION
                'unresolved agent Pod provision intent % cannot be deleted', OLD.attempt_id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_pod_provision_intent_authority';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
           OR NEW.thread_id IS DISTINCT FROM OLD.thread_id
           OR NEW.runtime_generation IS DISTINCT FROM OLD.runtime_generation
           OR NEW.provisioner IS DISTINCT FROM OLD.provisioner
           OR NEW.workspace_claim_id IS DISTINCT FROM OLD.workspace_claim_id
           OR NEW.pod_name IS DISTINCT FROM OLD.pod_name
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NOT (
                (OLD.status = 'planned'
                    AND NEW.status = 'published'
                    AND NULLIF(NEW.pod_uid, '') IS NOT NULL
                    AND NEW.fenced_at IS NULL
                    AND NEW.gc_after IS NULL
                    AND NEW.resolved_at IS NOT NULL)
                OR (OLD.status = 'planned'
                    AND NEW.status = 'revoking'
                    AND NEW.pod_uid IS NULL
                    AND NEW.fenced_at IS NULL
                    AND NEW.gc_after IS NULL
                    AND NEW.resolved_at IS NULL)
                OR (OLD.status = 'revoking'
                    AND NEW.status = 'fenced'
                    AND NULLIF(NEW.pod_uid, '') IS NOT NULL
                    AND NEW.fenced_at IS NOT NULL
                    AND NEW.fenced_at IS NOT DISTINCT FROM transaction_timestamp()
                    AND NEW.gc_after >= NEW.fenced_at + interval '10 minutes'
                    AND NEW.resolved_at IS NULL)
                OR (OLD.status = 'fenced'
                    AND NEW.status = 'retired'
                    AND NEW.pod_uid IS NOT DISTINCT FROM OLD.pod_uid
                    AND NEW.fenced_at IS NOT DISTINCT FROM OLD.fenced_at
                    AND NEW.gc_after IS NOT DISTINCT FROM OLD.gc_after
                    AND OLD.gc_after <= transaction_timestamp()
                    AND NEW.resolved_at
                        IS NOT DISTINCT FROM transaction_timestamp())
           ) THEN
            RAISE EXCEPTION
                'agent Pod provision intent % transition is not exact', OLD.attempt_id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_agent_pod_provision_intent_authority';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status <> 'planned' THEN
        RAISE EXCEPTION
            'agent Pod provision intent % must begin planned', NEW.attempt_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_pod_provision_intent_authority';
    END IF;

    SELECT * INTO thread_row
      FROM public.threads
     WHERE id = NEW.thread_id
     FOR KEY SHARE;
    IF NOT FOUND
       OR thread_row.execution_lane <> 'pinned'
       OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR thread_row.runtime_retirement_token IS NOT NULL
       OR thread_row.status NOT IN ('created', 'active', 'awaiting_user', 'suspended')
       OR thread_row.agent_id IS NOT NULL
       OR thread_row.control_admission_agent_id IS NOT NULL
       OR thread_row.runtime_attach_token IS NOT NULL
       OR (
            thread_row.metadata ? 'agent_pod'
            AND thread_row.metadata->'agent_pod' IS NOT NULL
            AND thread_row.metadata->'agent_pod'
                NOT IN ('null'::jsonb, '{}'::jsonb)
       )
       OR (
            NEW.workspace_claim_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM public.thread_agent_workspace_claims claim
                 WHERE claim.claim_id = NEW.workspace_claim_id
                   AND claim.thread_id = NEW.thread_id
                   AND claim.provisioner = NEW.provisioner
                   AND claim.status IN ('planned', 'ready')
            )
       ) THEN
        RAISE EXCEPTION
            'agent Pod provision intent % lacks open pinned authority', NEW.attempt_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_agent_pod_provision_intent_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS thread_agent_pod_provision_intent_authority
    ON public.thread_agent_pod_provision_intents;
CREATE TRIGGER thread_agent_pod_provision_intent_authority
BEFORE INSERT OR UPDATE OR DELETE
ON public.thread_agent_pod_provision_intents
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_agent_pod_provision_intent();

-- Pinned Kubernetes workspaces create several independent API objects.  A
-- client timeout after any one CREATE is not absence proof: the apiserver may
-- still commit that object after End observes a 404.  Persist every name before
-- the first effect, label all original objects with this attempt, and retain
-- same-name inert fences through the bounded apiserver request horizon.  No FK
-- is intentional; permanent thread deletion must not erase the cleanup work.
CREATE TABLE IF NOT EXISTS public.thread_workspace_provision_intents (
    attempt_id uuid PRIMARY KEY,
    thread_id uuid NOT NULL,
    runtime_generation uuid NOT NULL,
    created_agent_id uuid,
    created_attach_token uuid,
    namespace varchar(253) NOT NULL,
    pod_name varchar(253) NOT NULL,
    pvc_name varchar(253),
    seed_configmap_name varchar(253),
    service_name varchar(253),
    network_tier varchar(32) NOT NULL,
    manifest_fingerprint varchar(64) NOT NULL,
    previous_binding jsonb NOT NULL DEFAULT '{}'::jsonb,
    retained_binding_generation uuid,
    retained_pvc_uid text,
    retained_service_uid text,
    status varchar(16) NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'published', 'revoking', 'fenced', 'retired')),
    pod_uid text,
    pvc_uid text,
    seed_configmap_uid text,
    service_uid text,
    fence_pod_uid text,
    fence_pvc_uid text,
    fence_configmap_uid text,
    fence_service_uid text,
    created_at timestamptz NOT NULL DEFAULT now(),
    fenced_at timestamptz,
    gc_after timestamptz,
    resolved_at timestamptz,
    CHECK ((created_agent_id IS NULL) = (created_attach_token IS NULL)),
    CHECK (namespace <> '' AND pod_name <> '' AND network_tier <> ''),
    CHECK (manifest_fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK (jsonb_typeof(previous_binding) = 'object'),
    CHECK ((pvc_name IS NULL) = (service_name IS NULL)),
    CHECK (pod_uid IS NULL OR pod_name IS NOT NULL),
    CHECK (pvc_uid IS NULL OR pvc_name IS NOT NULL),
    CHECK (seed_configmap_uid IS NULL OR seed_configmap_name IS NOT NULL),
    CHECK (service_uid IS NULL OR service_name IS NOT NULL),
    CHECK (
        retained_pvc_uid IS NULL
        OR (pvc_name IS NOT NULL AND retained_binding_generation IS NOT NULL)
    ),
    CHECK (retained_service_uid IS NULL OR service_name IS NOT NULL),
    CHECK (
        (status IN ('planned', 'revoking')
            AND fence_pod_uid IS NULL AND fence_pvc_uid IS NULL
            AND fence_configmap_uid IS NULL AND fence_service_uid IS NULL
            AND fenced_at IS NULL AND gc_after IS NULL AND resolved_at IS NULL)
        OR (status = 'published'
            AND NULLIF(pod_uid, '') IS NOT NULL
            AND (pvc_name IS NULL OR NULLIF(pvc_uid, '') IS NOT NULL)
            AND (seed_configmap_name IS NULL
                 OR NULLIF(seed_configmap_uid, '') IS NOT NULL)
            AND (service_name IS NULL OR NULLIF(service_uid, '') IS NOT NULL)
            AND fence_pod_uid IS NULL AND fence_pvc_uid IS NULL
            AND fence_configmap_uid IS NULL AND fence_service_uid IS NULL
            AND fenced_at IS NULL AND gc_after IS NULL
            AND resolved_at IS NOT NULL)
        OR (status = 'fenced'
            AND NULLIF(fence_pod_uid, '') IS NOT NULL
            AND (pvc_name IS NULL OR retained_pvc_uid IS NOT NULL
                 OR NULLIF(fence_pvc_uid, '') IS NOT NULL)
            AND (seed_configmap_name IS NULL
                 OR NULLIF(fence_configmap_uid, '') IS NOT NULL)
            AND (service_name IS NULL OR retained_service_uid IS NOT NULL
                 OR NULLIF(fence_service_uid, '') IS NOT NULL)
            AND fenced_at IS NOT NULL
            AND gc_after >= fenced_at + interval '10 minutes'
            AND resolved_at IS NULL)
        OR (status = 'retired'
            AND fenced_at IS NOT NULL AND gc_after IS NOT NULL
            AND resolved_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_workspace_provision_unresolved
    ON public.thread_workspace_provision_intents(thread_id)
    WHERE status IN ('planned', 'revoking', 'fenced');

CREATE INDEX IF NOT EXISTS idx_thread_workspace_provision_generation
    ON public.thread_workspace_provision_intents
       (thread_id, runtime_generation, created_at DESC);

CREATE OR REPLACE FUNCTION public.enforce_thread_workspace_provision_intent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    thread_row public.threads%ROWTYPE;
    inverse_count integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'workspace provision intent % is append-only', OLD.attempt_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_workspace_provision_intent_authority';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
           OR NEW.thread_id IS DISTINCT FROM OLD.thread_id
           OR NEW.runtime_generation IS DISTINCT FROM OLD.runtime_generation
           OR NEW.created_agent_id IS DISTINCT FROM OLD.created_agent_id
           OR NEW.created_attach_token IS DISTINCT FROM OLD.created_attach_token
           OR NEW.namespace IS DISTINCT FROM OLD.namespace
           OR NEW.pod_name IS DISTINCT FROM OLD.pod_name
           OR NEW.pvc_name IS DISTINCT FROM OLD.pvc_name
           OR NEW.seed_configmap_name IS DISTINCT FROM OLD.seed_configmap_name
           OR NEW.service_name IS DISTINCT FROM OLD.service_name
           OR NEW.network_tier IS DISTINCT FROM OLD.network_tier
           OR NEW.manifest_fingerprint IS DISTINCT FROM OLD.manifest_fingerprint
           OR NEW.previous_binding IS DISTINCT FROM OLD.previous_binding
           OR NEW.retained_binding_generation
                IS DISTINCT FROM OLD.retained_binding_generation
           OR NEW.retained_pvc_uid IS DISTINCT FROM OLD.retained_pvc_uid
           OR NEW.retained_service_uid IS DISTINCT FROM OLD.retained_service_uid
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NOT (
                (OLD.status = 'planned' AND NEW.status = 'planned'
                    AND (OLD.pod_uid IS NULL
                         OR NEW.pod_uid IS NOT DISTINCT FROM OLD.pod_uid)
                    AND (OLD.pvc_uid IS NULL
                         OR NEW.pvc_uid IS NOT DISTINCT FROM OLD.pvc_uid)
                    AND (OLD.seed_configmap_uid IS NULL
                         OR NEW.seed_configmap_uid
                            IS NOT DISTINCT FROM OLD.seed_configmap_uid)
                    AND (OLD.service_uid IS NULL
                         OR NEW.service_uid IS NOT DISTINCT FROM OLD.service_uid)
                    AND NEW.fence_pod_uid IS NULL
                    AND NEW.fence_pvc_uid IS NULL
                    AND NEW.fence_configmap_uid IS NULL
                    AND NEW.fence_service_uid IS NULL
                    AND NEW.fenced_at IS NULL AND NEW.gc_after IS NULL
                    AND NEW.resolved_at IS NULL)
                OR (OLD.status = 'planned' AND NEW.status = 'published'
                    AND NULLIF(NEW.pod_uid, '') IS NOT NULL
                    AND (NEW.pvc_name IS NULL OR NULLIF(NEW.pvc_uid, '') IS NOT NULL)
                    AND (NEW.seed_configmap_name IS NULL
                         OR NULLIF(NEW.seed_configmap_uid, '') IS NOT NULL)
                    AND (NEW.service_name IS NULL
                         OR NULLIF(NEW.service_uid, '') IS NOT NULL)
                    AND NEW.fence_pod_uid IS NULL
                    AND NEW.fence_pvc_uid IS NULL
                    AND NEW.fence_configmap_uid IS NULL
                    AND NEW.fence_service_uid IS NULL
                    AND NEW.fenced_at IS NULL AND NEW.gc_after IS NULL
                    AND NEW.resolved_at
                        IS NOT DISTINCT FROM transaction_timestamp())
                OR (OLD.status = 'planned' AND NEW.status = 'revoking'
                    AND NEW.pod_uid IS NOT DISTINCT FROM OLD.pod_uid
                    AND NEW.pvc_uid IS NOT DISTINCT FROM OLD.pvc_uid
                    AND NEW.seed_configmap_uid
                        IS NOT DISTINCT FROM OLD.seed_configmap_uid
                    AND NEW.service_uid IS NOT DISTINCT FROM OLD.service_uid
                    AND NEW.fence_pod_uid IS NULL
                    AND NEW.fence_pvc_uid IS NULL
                    AND NEW.fence_configmap_uid IS NULL
                    AND NEW.fence_service_uid IS NULL
                    AND NEW.fenced_at IS NULL AND NEW.gc_after IS NULL
                    AND NEW.resolved_at IS NULL)
                OR (OLD.status = 'revoking' AND NEW.status = 'fenced'
                    AND NEW.pod_uid IS NOT DISTINCT FROM OLD.pod_uid
                    AND NEW.pvc_uid IS NOT DISTINCT FROM OLD.pvc_uid
                    AND NEW.seed_configmap_uid
                        IS NOT DISTINCT FROM OLD.seed_configmap_uid
                    AND NEW.service_uid IS NOT DISTINCT FROM OLD.service_uid
                    AND NULLIF(NEW.fence_pod_uid, '') IS NOT NULL
                    AND (NEW.pvc_name IS NULL OR NEW.retained_pvc_uid IS NOT NULL
                         OR NULLIF(NEW.fence_pvc_uid, '') IS NOT NULL)
                    AND (NEW.seed_configmap_name IS NULL
                         OR NULLIF(NEW.fence_configmap_uid, '') IS NOT NULL)
                    AND (NEW.service_name IS NULL
                         OR NEW.retained_service_uid IS NOT NULL
                         OR NULLIF(NEW.fence_service_uid, '') IS NOT NULL)
                    AND NEW.fenced_at
                        IS NOT DISTINCT FROM transaction_timestamp()
                    AND NEW.gc_after >= NEW.fenced_at + interval '10 minutes'
                    AND NEW.resolved_at IS NULL)
                OR (OLD.status = 'fenced' AND NEW.status = 'fenced'
                    AND NEW.pod_uid IS NOT DISTINCT FROM OLD.pod_uid
                    AND NEW.pvc_uid IS NOT DISTINCT FROM OLD.pvc_uid
                    AND NEW.seed_configmap_uid
                        IS NOT DISTINCT FROM OLD.seed_configmap_uid
                    AND NEW.service_uid IS NOT DISTINCT FROM OLD.service_uid
                    AND (OLD.fence_pod_uid IS NULL
                         OR NEW.fence_pod_uid IS NOT DISTINCT FROM OLD.fence_pod_uid)
                    AND (OLD.fence_pvc_uid IS NULL
                         OR NEW.fence_pvc_uid IS NOT DISTINCT FROM OLD.fence_pvc_uid)
                    AND (OLD.fence_configmap_uid IS NULL
                         OR NEW.fence_configmap_uid
                            IS NOT DISTINCT FROM OLD.fence_configmap_uid)
                    AND (OLD.fence_service_uid IS NULL
                         OR NEW.fence_service_uid
                            IS NOT DISTINCT FROM OLD.fence_service_uid)
                    AND (
                        NEW.fence_pod_uid IS DISTINCT FROM OLD.fence_pod_uid
                        OR NEW.fence_pvc_uid IS DISTINCT FROM OLD.fence_pvc_uid
                        OR NEW.fence_configmap_uid
                            IS DISTINCT FROM OLD.fence_configmap_uid
                        OR NEW.fence_service_uid
                            IS DISTINCT FROM OLD.fence_service_uid
                    )
                    AND NEW.fenced_at
                        IS NOT DISTINCT FROM transaction_timestamp()
                    AND NEW.gc_after >= NEW.fenced_at + interval '10 minutes'
                    AND NEW.resolved_at IS NULL
                    AND EXISTS (
                        SELECT 1 FROM public.threads thread
                         WHERE thread.id = OLD.thread_id
                           AND thread.runtime_generation = OLD.runtime_generation
                           AND thread.runtime_retirement_token IS NOT NULL
                           AND thread.runtime_retirement_permanent = true
                           AND thread.runtime_retirement_authorized_at IS NOT NULL
                           AND thread.runtime_retirement_context
                                ->'workspace_provision_intent'->>'attempt_id'
                               = OLD.attempt_id::text
                    ))
                OR (OLD.status = 'fenced' AND NEW.status = 'retired'
                    AND NEW.pod_uid IS NOT DISTINCT FROM OLD.pod_uid
                    AND NEW.pvc_uid IS NOT DISTINCT FROM OLD.pvc_uid
                    AND NEW.seed_configmap_uid
                        IS NOT DISTINCT FROM OLD.seed_configmap_uid
                    AND NEW.service_uid IS NOT DISTINCT FROM OLD.service_uid
                    AND NEW.fence_pod_uid IS NOT DISTINCT FROM OLD.fence_pod_uid
                    AND NEW.fence_pvc_uid IS NOT DISTINCT FROM OLD.fence_pvc_uid
                    AND NEW.fence_configmap_uid
                        IS NOT DISTINCT FROM OLD.fence_configmap_uid
                    AND NEW.fence_service_uid
                        IS NOT DISTINCT FROM OLD.fence_service_uid
                    AND NEW.fenced_at IS NOT DISTINCT FROM OLD.fenced_at
                    AND NEW.gc_after IS NOT DISTINCT FROM OLD.gc_after
                    AND OLD.gc_after <= transaction_timestamp()
                    AND NEW.resolved_at
                        IS NOT DISTINCT FROM transaction_timestamp())
           ) THEN
            RAISE EXCEPTION
                'workspace provision intent % transition is not exact', OLD.attempt_id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'thread_workspace_provision_intent_authority';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status <> 'planned' THEN
        RAISE EXCEPTION
            'workspace provision intent % must begin planned', NEW.attempt_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_workspace_provision_intent_authority';
    END IF;
    IF NEW.pod_uid IS NOT NULL
       OR NEW.pvc_uid IS NOT NULL
       OR NEW.seed_configmap_uid IS NOT NULL
       OR NEW.service_uid IS NOT NULL THEN
        RAISE EXCEPTION
            'workspace provision intent % cannot pre-publish a resource UID',
            NEW.attempt_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_workspace_provision_intent_authority';
    END IF;
    SELECT * INTO thread_row
      FROM public.threads
     WHERE id = NEW.thread_id
     FOR KEY SHARE;
    SELECT count(*) INTO inverse_count
      FROM public.agents
     WHERE thread_id = NEW.thread_id;
    IF thread_row.id IS NULL
       OR thread_row.execution_lane <> 'pinned'
       OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR thread_row.runtime_retirement_token IS NOT NULL
       OR thread_row.status NOT IN ('created', 'active', 'awaiting_user', 'suspended')
       OR thread_row.agent_id IS DISTINCT FROM NEW.created_agent_id
       OR thread_row.runtime_attach_token IS DISTINCT FROM NEW.created_attach_token
       OR (
            thread_row.control_admission_agent_id IS NOT NULL
            AND thread_row.control_admission_agent_id
                IS DISTINCT FROM NEW.created_agent_id
       )
       OR (NEW.created_agent_id IS NULL AND inverse_count <> 0)
       OR (NEW.created_agent_id IS NOT NULL AND (
            inverse_count <> 1
            OR NOT EXISTS (
                SELECT 1 FROM public.agents agent
                 WHERE agent.id = NEW.created_agent_id
                   AND agent.thread_id = NEW.thread_id
            )
       )) THEN
        RAISE EXCEPTION
            'workspace provision intent % lacks open pinned authority', NEW.attempt_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_workspace_provision_intent_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS thread_workspace_provision_intent_authority
    ON public.thread_workspace_provision_intents;
CREATE TRIGGER thread_workspace_provision_intent_authority
BEFORE INSERT OR UPDATE OR DELETE
ON public.thread_workspace_provision_intents
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_workspace_provision_intent();

-- Append-only exact settlement readback. The thread row deliberately clears
-- its retirement token/owner on settlement (and permanent End deletes it), so
-- an agent whose final 200 is lost needs one non-secret proof that *its exact*
-- T/G/process attempt committed. No foreign key: permanent deletion must not
-- erase the proof before the old process can observe it and exit.
CREATE TABLE IF NOT EXISTS public.thread_runtime_retirement_outcomes (
    thread_id uuid NOT NULL,
    runtime_generation uuid NOT NULL,
    retirement_token uuid NOT NULL,
    agent_id uuid,
    runtime_attach_token uuid,
    disposition varchar(16) NOT NULL
        CHECK (disposition IN ('ended', 'suspended')),
    permanent boolean NOT NULL,
    outcome varchar(16) NOT NULL
        CHECK (outcome IN ('settled', 'deleted')),
    settled_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, runtime_generation, retirement_token)
);

CREATE INDEX IF NOT EXISTS idx_thread_runtime_retirement_outcomes_process
    ON public.thread_runtime_retirement_outcomes
       (thread_id, agent_id, runtime_attach_token, settled_at DESC);

CREATE TABLE IF NOT EXISTS public.thread_runtime_attach_abort_outcomes (
    thread_id uuid NOT NULL,
    runtime_generation uuid NOT NULL,
    runtime_attach_token uuid NOT NULL,
    agent_id uuid NOT NULL,
    agent_pod_uid text NOT NULL,
    successor_generation uuid NOT NULL,
    release_kind varchar(32) NOT NULL
        CHECK (release_kind IN ('server_pre_delivery', 'process_zero')),
    quiescence_protocol varchar(40) NOT NULL,
    workspace_generation uuid,
    workspace_runtime_incarnation uuid,
    released_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, runtime_generation, runtime_attach_token)
);

-- These rows are authority proofs, not mutable bookkeeping.  A prior soft
-- settlement is accepted by the permanent DELETE fence below, and an attach
-- abort outcome owns restart-safe successor recovery.  Prevent an old or
-- compromised writer from rewriting/deleting either proof after publication.
CREATE OR REPLACE FUNCTION public.enforce_thread_runtime_outcome_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = '23514',
              CONSTRAINT = 'thread_runtime_outcomes_append_only';
END;
$$;

DROP TRIGGER IF EXISTS thread_runtime_retirement_outcomes_append_only
    ON public.thread_runtime_retirement_outcomes;
CREATE TRIGGER thread_runtime_retirement_outcomes_append_only
BEFORE UPDATE OR DELETE ON public.thread_runtime_retirement_outcomes
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_runtime_outcome_append_only();

DROP TRIGGER IF EXISTS thread_runtime_attach_abort_outcomes_append_only
    ON public.thread_runtime_attach_abort_outcomes;
CREATE TRIGGER thread_runtime_attach_abort_outcomes_append_only
BEFORE UPDATE OR DELETE ON public.thread_runtime_attach_abort_outcomes
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_runtime_outcome_append_only();

-- Canonical receipt expected from the row-locked external-cleanup CAS.  The
-- captured JSON is embedded verbatim so an old/direct writer cannot replace
-- one resource tuple with a different absent-looking shape.  NULL means the
-- immutable retirement context is malformed or mixes physical tiers.
CREATE OR REPLACE FUNCTION public.pinned_retirement_external_cleanup_expected(
    retirement_context jsonb,
    runtime_generation uuid,
    retirement_token uuid
)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    workspace jsonb;
    binding jsonb;
    agent_claim jsonb;
    workspace_intent jsonb;
    vm jsonb;
    protected_ro jsonb;
    backend text;
    workspace_evidence boolean := false;
    binding_evidence boolean := false;
    vm_evidence boolean := false;
    ro_live boolean := false;
    workspace_protocol text;
BEGIN
    IF jsonb_typeof(retirement_context) IS DISTINCT FROM 'object'
       OR COALESCE(jsonb_typeof(retirement_context->'workspace_container'), 'null')
          NOT IN ('object', 'null')
       OR COALESCE(jsonb_typeof(retirement_context->'workspace_binding'), 'null')
          NOT IN ('object', 'null')
       OR COALESCE(jsonb_typeof(retirement_context->'agent_workspace_claim'), 'null')
          NOT IN ('object', 'null')
       OR COALESCE(
            jsonb_typeof(retirement_context->'workspace_provision_intent'),
            'null'
          ) NOT IN ('object', 'null')
       OR COALESCE(jsonb_typeof(retirement_context->'vm'), 'null')
          NOT IN ('object', 'null')
       OR COALESCE(jsonb_typeof(retirement_context->'protected_ro'), 'null')
          NOT IN ('object', 'null') THEN
        RETURN NULL;
    END IF;

    workspace := CASE
        WHEN jsonb_typeof(retirement_context->'workspace_container') = 'object'
        THEN retirement_context->'workspace_container'
        ELSE '{}'::jsonb
    END;
    binding := CASE
        WHEN jsonb_typeof(retirement_context->'workspace_binding') = 'object'
        THEN retirement_context->'workspace_binding'
        ELSE '{}'::jsonb
    END;
    agent_claim := CASE
        WHEN jsonb_typeof(retirement_context->'agent_workspace_claim') = 'object'
        THEN retirement_context->'agent_workspace_claim'
        ELSE '{}'::jsonb
    END;
    workspace_intent := CASE
        WHEN jsonb_typeof(retirement_context->'workspace_provision_intent') = 'object'
        THEN retirement_context->'workspace_provision_intent'
        ELSE '{}'::jsonb
    END;
    vm := CASE
        WHEN jsonb_typeof(retirement_context->'vm') = 'object'
        THEN retirement_context->'vm'
        ELSE '{}'::jsonb
    END;
    protected_ro := CASE
        WHEN jsonb_typeof(retirement_context->'protected_ro') = 'object'
        THEN retirement_context->'protected_ro'
        ELSE '{}'::jsonb
    END;
    backend := COALESCE(retirement_context->>'workspace_backend', '');
    workspace_evidence := (
        COALESCE(workspace->>'status', '') NOT IN ('', 'deleted')
        OR workspace->>'_runtime_incarnation' IS NOT NULL
        OR workspace->>'_docker_workspace_lease_id' IS NOT NULL
        OR workspace->>'pod_ip' IS NOT NULL
        OR workspace->>'pod_name' IS NOT NULL
        OR workspace->>'host' IS NOT NULL
        OR workspace->>'port' IS NOT NULL
        OR workspace->>'ide_host' IS NOT NULL
        OR workspace->>'ide_port' IS NOT NULL
        OR workspace->>'_canvas_workspace_generation' IS NOT NULL
    );
    binding_evidence := binding <> '{}'::jsonb;
    vm_evidence := (
        COALESCE(vm->>'status', '') NOT IN ('', 'deleted')
        OR vm->>'provision_generation' IS NOT NULL
        OR vm->>'identity_provision_generation' IS NOT NULL
        OR vm->>'vm_uid' IS NOT NULL
        OR vm->>'_runtime_incarnation' IS NOT NULL
        OR vm->>'rootdisk_pvc_uid' IS NOT NULL
        OR vm->>'ssh_host' IS NOT NULL
        OR vm->>'ssh_port' IS NOT NULL
        OR vm->>'_canvas_workspace_generation' IS NOT NULL
    );
    ro_live := COALESCE(protected_ro->>'status', '') IN (
        'engaging', 'active', 'revoking'
    );

    IF agent_claim <> '{}'::jsonb AND (
        COALESCE(agent_claim->>'claim_id', '') !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        OR COALESCE(agent_claim->>'thread_id', '') !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        OR agent_claim->>'thread_id' IS DISTINCT FROM retirement_context->>'thread_id'
        OR COALESCE(agent_claim->>'created_runtime_generation', '') !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        OR COALESCE(agent_claim->>'create_attempt', '') !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        OR COALESCE(agent_claim->>'provisioner', '') NOT IN ('agent', 'persistent')
        OR NULLIF(agent_claim->>'pvc_name', '') IS NULL
        OR COALESCE(agent_claim->>'status', '') NOT IN ('planned', 'ready')
        OR (
            (agent_claim->>'status' = 'ready')
            IS DISTINCT FROM (NULLIF(agent_claim->>'pvc_uid', '') IS NOT NULL)
        )
    ) THEN
        RETURN NULL;
    END IF;

    IF workspace_intent <> '{}'::jsonb AND (
        COALESCE(workspace_intent->>'attempt_id', '') !~
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        OR workspace_intent->>'thread_id'
            IS DISTINCT FROM retirement_context->>'thread_id'
        OR workspace_intent->>'runtime_generation'
            IS DISTINCT FROM runtime_generation::text
        OR NULLIF(workspace_intent->>'namespace', '') IS NULL
        OR NULLIF(workspace_intent->>'pod_name', '') IS NULL
        OR NULLIF(workspace_intent->>'network_tier', '') IS NULL
        OR COALESCE(workspace_intent->>'manifest_fingerprint', '')
            !~ '^[0-9a-f]{64}$'
        OR COALESCE(workspace_intent->>'status', '')
            NOT IN ('planned', 'fenced', 'retired')
        OR jsonb_typeof(workspace_intent->'previous_binding')
            IS DISTINCT FROM 'object'
        OR workspace_intent->'previous_binding'
            IS DISTINCT FROM binding
        OR (
            NULLIF(workspace_intent->>'created_agent_id', '') IS NULL
        ) IS DISTINCT FROM (
            NULLIF(workspace_intent->>'created_attach_token', '') IS NULL
        )
        OR (
            NULLIF(workspace_intent->>'pvc_name', '') IS NULL
        ) IS DISTINCT FROM (
            NULLIF(workspace_intent->>'service_name', '') IS NULL
        )
    ) THEN
        RETURN NULL;
    END IF;

    IF workspace_intent <> '{}'::jsonb THEN
        IF vm_evidence OR backend NOT IN ('sandbox', 'virtual', 'none') THEN
            RETURN NULL;
        END IF;
        workspace_protocol := 'workspace_provision_fence_v1';
    ELSIF backend = 'sandbox' THEN
        IF vm_evidence OR (binding_evidence AND binding->>'kind' = 'virtual') THEN
            RETURN NULL;
        END IF;
        workspace_protocol := CASE
            WHEN workspace_evidence OR binding_evidence
            THEN 'sandbox_actuator_zero_v1'
            ELSE 'external_none_v1'
        END;
    ELSIF backend IN ('vm', 'remote') THEN
        IF workspace_evidence OR binding_evidence THEN
            RETURN NULL;
        END IF;
        workspace_protocol := CASE
            WHEN vm_evidence THEN 'workspace_actuator_zero_v1'
            ELSE 'external_none_v1'
        END;
    ELSIF backend = 'virtual' THEN
        IF workspace_evidence OR vm_evidence
           OR (binding_evidence AND (
               binding->>'kind' IS DISTINCT FROM 'virtual'
               OR COALESCE(binding->>'backing_id', '')
                    !~ '^rclone:[0-9a-f]{64}$'
           )) THEN
            RETURN NULL;
        END IF;
        workspace_protocol := CASE
            WHEN binding_evidence THEN 'virtual_backing_zero_v1'
            ELSE 'external_none_v1'
        END;
    ELSIF backend = 'none' THEN
        IF workspace_evidence OR binding_evidence OR vm_evidence THEN
            RETURN NULL;
        END IF;
        workspace_protocol := 'external_none_v1';
    ELSE
        RETURN NULL;
    END IF;

    IF ro_live AND (
        NULLIF(protected_ro->>'id', '') IS NULL
        OR NULLIF(protected_ro->>'runtime_generation', '') IS NULL
        OR NULLIF(protected_ro->>'engage_attempt', '') IS NULL
        OR NULLIF(protected_ro->>'grant_handle', '') IS NULL
    ) THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'version', 1,
        'runtime_generation', runtime_generation::text,
        'retirement_token', retirement_token::text,
        'cleanup_actor', 'orchestrator',
        'workspace_cleanup_protocol', workspace_protocol,
        'agent_workspace_cleanup_protocol', CASE
            WHEN agent_claim <> '{}'::jsonb
            THEN 'k8s_pvc_name_tombstone_v1' ELSE NULL
        END,
        'protected_reader_cleanup_protocol', CASE
            WHEN ro_live THEN 'protected_reader_zero_v1' ELSE NULL
        END,
        'captured_resources', jsonb_build_object(
            'workspace_backend', backend,
            'workspace_container', retirement_context->'workspace_container',
            'workspace_binding', retirement_context->'workspace_binding',
            'agent_workspace_claim', retirement_context->'agent_workspace_claim',
            'workspace_provision_intent',
                retirement_context->'workspace_provision_intent',
            'vm', retirement_context->'vm',
            'protected_ro', retirement_context->'protected_ro'
        )
    );
END;
$$;

-- Migrations and the runtime intentionally use the same owning SRW role. Keep
-- the canonicalizer unavailable to unrelated PUBLIC roles; it is a database
-- belt for stale/unaware writers, not a cryptographic attestation against a
-- malicious process holding the owner credential. The exact orchestrator
-- actuator remains the external-effect trust root.
REVOKE ALL ON FUNCTION
    public.pinned_retirement_external_cleanup_expected(jsonb, uuid, uuid)
    FROM PUBLIC;

-- Current external absence is deliberately separate from the immutable
-- actuator receipt.  The receipt proves which captured tuple the trusted
-- orchestrator retired; this predicate proves that no replacement/live tuple
-- is present when a terminal outcome is published or the thread is deleted.
CREATE OR REPLACE FUNCTION public.pinned_retirement_external_resources_absent(
    subject_thread_id uuid,
    thread_metadata jsonb
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE((
        jsonb_typeof(thread_metadata) = 'object'
        AND COALESCE(
              jsonb_typeof(thread_metadata->'workspace_container'), 'null'
            ) IN ('object', 'null')
        AND COALESCE(
              jsonb_typeof(thread_metadata->'_workspace_binding'), 'null'
            ) IN ('object', 'null')
        AND COALESCE(jsonb_typeof(thread_metadata->'vm'), 'null')
              IN ('object', 'null')
        AND (
              NOT (thread_metadata ? 'agent_pod')
              OR thread_metadata->'agent_pod' IS NULL
              OR thread_metadata->'agent_pod' = 'null'::jsonb
              OR thread_metadata->'agent_pod' = '{}'::jsonb
        )
        AND COALESCE(
              thread_metadata->'workspace_container'->>'status', ''
            ) IN ('', 'deleted')
        AND thread_metadata->'workspace_container'->>'_runtime_incarnation'
            IS NULL
        AND thread_metadata->'workspace_container'->>'_docker_workspace_lease_id'
            IS NULL
        AND thread_metadata->'workspace_container'->>'pod_ip' IS NULL
        AND thread_metadata->'workspace_container'->>'pod_name' IS NULL
        AND thread_metadata->'workspace_container'->>'host' IS NULL
        AND thread_metadata->'workspace_container'->>'port' IS NULL
        AND thread_metadata->'workspace_container'->>'ide_host' IS NULL
        AND thread_metadata->'workspace_container'->>'ide_port' IS NULL
        AND thread_metadata->'workspace_container'->>'_canvas_workspace_generation'
            IS NULL
        AND (
              NOT (thread_metadata ? '_workspace_binding')
              OR thread_metadata->'_workspace_binding' IS NULL
              OR thread_metadata->'_workspace_binding' = 'null'::jsonb
              OR thread_metadata->'_workspace_binding' = '{}'::jsonb
        )
        AND COALESCE(thread_metadata->'vm'->>'status', '') IN ('', 'deleted')
        AND thread_metadata->'vm'->>'provision_generation' IS NULL
        AND thread_metadata->'vm'->>'identity_provision_generation' IS NULL
        AND thread_metadata->'vm'->>'vm_uid' IS NULL
        AND thread_metadata->'vm'->>'_runtime_incarnation' IS NULL
        AND thread_metadata->'vm'->>'rootdisk_pvc_uid' IS NULL
        AND thread_metadata->'vm'->>'ssh_host' IS NULL
        AND thread_metadata->'vm'->>'ssh_port' IS NULL
        AND thread_metadata->'vm'->>'_canvas_workspace_generation' IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM public.cloud_ro_mounts ro
             WHERE ro.thread_id = subject_thread_id
               AND ro.status IN ('engaging', 'active', 'revoking')
        )
        AND NOT EXISTS (
            SELECT 1 FROM public.docker_workspace_leases lease
             WHERE lease.owner_kind = 'thread'
               AND lease.owner_id = subject_thread_id
               AND lease.status IN ('ready', 'releasing')
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_agent_pod_provision_intents intent
             WHERE intent.thread_id = subject_thread_id
               AND intent.status IN ('planned', 'revoking')
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_agent_workspace_claims claim
             WHERE claim.thread_id = subject_thread_id
               AND claim.status IN ('planned', 'ready', 'revoking')
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_workspace_provision_intents intent
             WHERE intent.thread_id = subject_thread_id
               AND intent.status IN ('planned', 'revoking')
        )
    ), false);
$$;

REVOKE ALL ON FUNCTION
    public.pinned_retirement_external_resources_absent(uuid, jsonb)
    FROM PUBLIC;

-- One immutable workspace create may be in flight when retirement closes
-- admission.  Soft settlement and permanent deletion may trust that attempt
-- only after every submitted resource name is occupied by its retained inert
-- fence (or the post-request-horizon GC has retired those exact fence UIDs).
-- Keeping this comparison in one function prevents the outcome INSERT, status
-- transition, external-receipt UPDATE, and DELETE belts from drifting apart.
CREATE OR REPLACE FUNCTION
    public.pinned_retirement_workspace_provision_intent_retired(
        subject_thread_id uuid,
        runtime_generation uuid,
        captured_intent jsonb,
        require_all_resource_fences boolean
    )
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(CASE
        WHEN captured_intent IS NULL
             OR captured_intent IN ('null'::jsonb, '{}'::jsonb) THEN
            NOT EXISTS (
                SELECT 1
                  FROM public.thread_workspace_provision_intents intent
                 WHERE intent.thread_id = subject_thread_id
                   AND intent.status IN ('planned', 'revoking', 'fenced')
            )
        WHEN jsonb_typeof(captured_intent) = 'object'
             AND COALESCE(captured_intent->>'attempt_id', '') ~
                '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             AND captured_intent->>'thread_id' = subject_thread_id::text
             AND captured_intent->>'runtime_generation' = runtime_generation::text
             AND NULLIF(captured_intent->>'namespace', '') IS NOT NULL
             AND NULLIF(captured_intent->>'pod_name', '') IS NOT NULL
             AND NULLIF(captured_intent->>'network_tier', '') IS NOT NULL
             AND COALESCE(captured_intent->>'manifest_fingerprint', '') ~
                '^[0-9a-f]{64}$'
             AND COALESCE(captured_intent->>'status', '')
                IN ('planned', 'fenced', 'retired')
             AND jsonb_typeof(captured_intent->'previous_binding') = 'object'
             AND (
                 NULLIF(captured_intent->>'created_agent_id', '') IS NULL
             ) = (
                 NULLIF(captured_intent->>'created_attach_token', '') IS NULL
             )
             AND (
                 NULLIF(captured_intent->>'pvc_name', '') IS NULL
             ) = (
                 NULLIF(captured_intent->>'service_name', '') IS NULL
             ) THEN
            EXISTS (
                SELECT 1
                  FROM public.thread_workspace_provision_intents intent
                 WHERE intent.attempt_id::text = captured_intent->>'attempt_id'
                   AND intent.thread_id = subject_thread_id
                   AND intent.runtime_generation = runtime_generation
                   AND intent.status IN ('fenced', 'retired')
                   AND (
                       NOT require_all_resource_fences
                       OR intent.status = 'retired'
                       OR (
                           NULLIF(intent.fence_pod_uid, '') IS NOT NULL
                           AND (
                               intent.pvc_name IS NULL
                               OR NULLIF(intent.fence_pvc_uid, '') IS NOT NULL
                           )
                           AND (
                               intent.seed_configmap_name IS NULL
                               OR NULLIF(intent.fence_configmap_uid, '') IS NOT NULL
                           )
                           AND (
                               intent.service_name IS NULL
                               OR NULLIF(intent.fence_service_uid, '') IS NOT NULL
                           )
                       )
                   )
                   AND COALESCE(intent.created_agent_id::text, '') =
                       COALESCE(captured_intent->>'created_agent_id', '')
                   AND COALESCE(intent.created_attach_token::text, '') =
                       COALESCE(captured_intent->>'created_attach_token', '')
                   AND intent.namespace = captured_intent->>'namespace'
                   AND intent.pod_name = captured_intent->>'pod_name'
                   AND COALESCE(intent.pvc_name, '') =
                       COALESCE(captured_intent->>'pvc_name', '')
                   AND COALESCE(intent.seed_configmap_name, '') =
                       COALESCE(captured_intent->>'seed_configmap_name', '')
                   AND COALESCE(intent.service_name, '') =
                       COALESCE(captured_intent->>'service_name', '')
                   AND intent.network_tier = captured_intent->>'network_tier'
                   AND intent.manifest_fingerprint =
                       captured_intent->>'manifest_fingerprint'
                   AND intent.previous_binding IS NOT DISTINCT FROM
                       captured_intent->'previous_binding'
                   AND COALESCE(intent.retained_binding_generation::text, '') =
                       COALESCE(
                           captured_intent->>'retained_binding_generation', ''
                       )
                   AND COALESCE(intent.retained_pvc_uid, '') =
                       COALESCE(captured_intent->>'retained_pvc_uid', '')
                   AND COALESCE(intent.retained_service_uid, '') =
                       COALESCE(captured_intent->>'retained_service_uid', '')
                   AND COALESCE(intent.pod_uid, '') =
                       COALESCE(captured_intent->>'pod_uid', '')
                   AND COALESCE(intent.pvc_uid, '') =
                       COALESCE(captured_intent->>'pvc_uid', '')
                   AND COALESCE(intent.seed_configmap_uid, '') =
                       COALESCE(captured_intent->>'seed_configmap_uid', '')
                   AND COALESCE(intent.service_uid, '') =
                       COALESCE(captured_intent->>'service_uid', '')
                   AND NOT EXISTS (
                       SELECT 1
                         FROM public.thread_workspace_provision_intents other
                        WHERE other.thread_id = subject_thread_id
                          AND other.attempt_id <> intent.attempt_id
                          AND other.status IN ('planned', 'revoking', 'fenced')
                   )
            )
        ELSE false
    END, false);
$$;

REVOKE ALL ON FUNCTION
    public.pinned_retirement_workspace_provision_intent_retired(
        uuid, uuid, jsonb, boolean
    )
    FROM PUBLIC;

-- Outcome rows are durable authority, not a free-form audit log.  UPDATE and
-- DELETE are blocked above; INSERT is accepted only while the exact row-locked
-- lifecycle transition that owns the proof is still visible.  The application
-- intentionally inserts a soft/permanent outcome before clearing/deleting its
-- thread row in the same transaction, and inserts attach-abort after the exact
-- G1 -> G2 rotation receipt is installed.
CREATE OR REPLACE FUNCTION public.enforce_thread_runtime_retirement_outcome_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    thread_row public.threads%ROWTYPE;
    retirement_context jsonb;
    runtime_exposed boolean := false;
    prior_soft_settlement boolean := false;
BEGIN
    SELECT t.* INTO thread_row
      FROM public.threads t
     WHERE t.id = NEW.thread_id
     FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'retirement outcome has no live transition owner'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_runtime_outcome_insert_authority';
    END IF;
    retirement_context := thread_row.runtime_retirement_context;
    runtime_exposed := COALESCE((
        thread_row.runtime_authority_exposed
        OR thread_row.agent_id IS NOT NULL
        OR thread_row.runtime_attach_token IS NOT NULL
        OR retirement_context->>'runtime_authority_exposed' = 'true'
        OR NULLIF(retirement_context->>'agent_id', '') IS NOT NULL
        OR NULLIF(retirement_context->>'runtime_attach_token', '') IS NOT NULL
        OR (
            retirement_context->'agent' IS NOT NULL
            AND retirement_context->'agent' NOT IN ('null'::jsonb, '{}'::jsonb)
        )
        OR (
            retirement_context->'agent_pod' IS NOT NULL
            AND retirement_context->'agent_pod'
                NOT IN ('null'::jsonb, '{}'::jsonb)
        )
        OR (
            retirement_context->'agent_pod_provision_intent' IS NOT NULL
            AND retirement_context->'agent_pod_provision_intent'
                NOT IN ('null'::jsonb, '{}'::jsonb)
        )
    ), false);
    prior_soft_settlement := (
        NEW.permanent
        AND thread_row.status = 'ended'
        AND thread_row.agent_id IS NULL
        AND thread_row.control_admission_agent_id IS NULL
        AND thread_row.runtime_attach_token IS NULL
        AND (
            NOT (retirement_context ? 'agent')
            OR retirement_context->'agent' IN ('null'::jsonb, '{}'::jsonb)
        )
        AND (
            NOT (retirement_context ? 'agent_pod')
            OR retirement_context->'agent_pod' IN ('null'::jsonb, '{}'::jsonb)
        )
        AND (
            NOT (retirement_context ? 'agent_pod_provision_intent')
            OR retirement_context->'agent_pod_provision_intent'
                IN ('null'::jsonb, '{}'::jsonb)
        )
        AND EXISTS (
            SELECT 1
              FROM public.thread_runtime_retirement_outcomes prior
             WHERE prior.thread_id = NEW.thread_id
               AND prior.runtime_generation = NEW.runtime_generation
               AND prior.disposition = 'ended'
               AND prior.permanent = false
               AND prior.outcome = 'settled'
        )
    );

    IF thread_row.execution_lane <> 'pinned'
       OR thread_row.runtime_generation IS DISTINCT FROM NEW.runtime_generation
       OR thread_row.runtime_retirement_token IS DISTINCT FROM NEW.retirement_token
       OR thread_row.runtime_retirement_authorized_at IS NULL
       OR thread_row.runtime_retirement_permanent IS DISTINCT FROM NEW.permanent
       OR jsonb_typeof(retirement_context) IS DISTINCT FROM 'object'
       OR retirement_context->>'generation'
          IS DISTINCT FROM NEW.runtime_generation::text
       OR retirement_context->>'settle_status' IS DISTINCT FROM NEW.disposition
       OR COALESCE(retirement_context->>'agent_id', '')
          IS DISTINCT FROM COALESCE(NEW.agent_id::text, '')
       OR COALESCE(retirement_context->>'runtime_attach_token', '')
          IS DISTINCT FROM COALESCE(NEW.runtime_attach_token::text, '')
       OR COALESCE(thread_row.agent_id::text, '')
          IS DISTINCT FROM COALESCE(NEW.agent_id::text, '')
       OR COALESCE(thread_row.runtime_attach_token::text, '')
          IS DISTINCT FROM COALESCE(NEW.runtime_attach_token::text, '')
       OR NOT public.pinned_retirement_workspace_provision_intent_retired(
            NEW.thread_id,
            NEW.runtime_generation,
            retirement_context->'workspace_provision_intent',
            NEW.permanent
       )
       OR (NEW.permanent AND (
            NEW.disposition <> 'ended'
            OR NEW.outcome <> 'deleted'
            OR thread_row.runtime_retirement_external_cleanup IS DISTINCT FROM
               public.pinned_retirement_external_cleanup_expected(
                   retirement_context,
                   NEW.runtime_generation,
                   NEW.retirement_token
               )
            OR NOT public.pinned_retirement_external_resources_absent(
                NEW.thread_id, thread_row.metadata
            )
            OR EXISTS (
                SELECT 1 FROM public.agents a
                 WHERE a.thread_id = NEW.thread_id
            )
            OR (runtime_exposed
                AND NOT prior_soft_settlement
                AND thread_row.runtime_retirement_local_quiescence IS NULL)
       ))
       OR (NOT NEW.permanent AND (
            NEW.outcome <> 'settled'
            OR thread_row.status NOT IN (
                'created', 'active', 'awaiting_user', 'suspended'
            )
            OR (runtime_exposed
                AND thread_row.runtime_retirement_local_quiescence IS NULL)
            OR (COALESCE(retirement_context->>'protected_cloud', 'false') = 'true'
                AND thread_row.runtime_retirement_stage_receipt IS NULL)
       )) THEN
        RAISE EXCEPTION 'retirement outcome lacks exact transition authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_runtime_outcome_insert_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS thread_runtime_retirement_outcomes_insert_authority
    ON public.thread_runtime_retirement_outcomes;
CREATE TRIGGER thread_runtime_retirement_outcomes_insert_authority
BEFORE INSERT ON public.thread_runtime_retirement_outcomes
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_runtime_retirement_outcome_insert();

CREATE OR REPLACE FUNCTION public.enforce_thread_runtime_attach_abort_outcome_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    thread_row public.threads%ROWTYPE;
    receipt jsonb;
BEGIN
    SELECT t.* INTO thread_row
      FROM public.threads t
     WHERE t.id = NEW.thread_id
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'attach-abort outcome has no successor owner'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_runtime_outcome_insert_authority';
    END IF;
    receipt := thread_row.runtime_attach_abort_receipt;
    IF thread_row.execution_lane <> 'pinned'
       OR thread_row.status <> 'created'
       OR thread_row.runtime_generation IS DISTINCT FROM NEW.successor_generation
       OR thread_row.runtime_retirement_token IS NOT NULL
       OR thread_row.agent_id IS NOT NULL
       OR thread_row.control_admission_agent_id IS NOT NULL
       OR thread_row.runtime_attach_token IS NOT NULL
       OR thread_row.runtime_authority_exposed
       OR jsonb_typeof(receipt) IS DISTINCT FROM 'object'
       OR receipt->>'version' IS DISTINCT FROM '1'
       OR receipt->>'runtime_generation'
          IS DISTINCT FROM NEW.runtime_generation::text
       OR receipt->>'successor_generation'
          IS DISTINCT FROM NEW.successor_generation::text
       OR receipt->>'agent_id' IS DISTINCT FROM NEW.agent_id::text
       OR receipt->>'runtime_attach_token'
          IS DISTINCT FROM NEW.runtime_attach_token::text
       OR receipt->>'agent_pod_uid' IS DISTINCT FROM NEW.agent_pod_uid
       OR receipt->>'release_kind' IS DISTINCT FROM NEW.release_kind
       OR receipt->>'quiescence_protocol'
          IS DISTINCT FROM NEW.quiescence_protocol
       OR COALESCE(receipt->>'workspace_generation', '')
          IS DISTINCT FROM COALESCE(NEW.workspace_generation::text, '')
       OR COALESCE(receipt->>'workspace_runtime_incarnation', '')
          IS DISTINCT FROM COALESCE(
              NEW.workspace_runtime_incarnation::text, ''
          )
       OR (
            NEW.release_kind = 'server_pre_delivery'
            AND NEW.quiescence_protocol <> 'pre_delivery_no_payload_v1'
       )
       OR (
            NEW.release_kind = 'process_zero'
            AND NEW.quiescence_protocol NOT IN (
                'agent_attach_not_started_v1',
                'agent_runtime_zero_v1',
                'workspace_process_zero_v1'
            )
       ) THEN
        RAISE EXCEPTION 'attach-abort outcome lacks exact transition authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'thread_runtime_outcome_insert_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS thread_runtime_attach_abort_outcomes_insert_authority
    ON public.thread_runtime_attach_abort_outcomes;
CREATE TRIGGER thread_runtime_attach_abort_outcomes_insert_authority
BEFORE INSERT ON public.thread_runtime_attach_abort_outcomes
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_runtime_attach_abort_outcome_insert();

ALTER TABLE public.threads
    DROP CONSTRAINT IF EXISTS threads_runtime_retirement_shape;
ALTER TABLE public.threads
    ADD CONSTRAINT threads_runtime_retirement_shape CHECK (
        (
            runtime_retirement_token IS NULL
            AND runtime_retirement_permanent IS NULL
            AND runtime_retirement_started_at IS NULL
            AND runtime_retirement_authorized_at IS NULL
            AND runtime_retirement_context IS NULL
        )
        OR
        (
            runtime_retirement_token IS NOT NULL
            AND runtime_retirement_permanent IS NOT NULL
            AND runtime_retirement_started_at IS NOT NULL
            AND jsonb_typeof(runtime_retirement_context) = 'object'
        )
    ) NOT VALID;
ALTER TABLE public.threads
    -- This maintenance-gated migration runs with old writers drained and must
    -- commit its authority constraints atomically before admission reopens.
    -- squawk-ignore constraint-missing-not-valid
    VALIDATE CONSTRAINT threads_runtime_retirement_shape;

ALTER TABLE public.threads
    DROP CONSTRAINT IF EXISTS threads_runtime_retirement_stage_receipt_shape;
ALTER TABLE public.threads
    ADD CONSTRAINT threads_runtime_retirement_stage_receipt_shape CHECK (
        runtime_retirement_stage_receipt IS NULL
        OR (
            runtime_retirement_token IS NOT NULL
            AND jsonb_typeof(runtime_retirement_stage_receipt) = 'object'
        )
    ) NOT VALID;
ALTER TABLE public.threads
    -- squawk-ignore constraint-missing-not-valid
    VALIDATE CONSTRAINT threads_runtime_retirement_stage_receipt_shape;

ALTER TABLE public.threads
    DROP CONSTRAINT IF EXISTS threads_runtime_retirement_local_quiescence_shape;
ALTER TABLE public.threads
    ADD CONSTRAINT threads_runtime_retirement_local_quiescence_shape CHECK (
        runtime_retirement_local_quiescence IS NULL
        OR (
            runtime_retirement_token IS NOT NULL
            AND jsonb_typeof(runtime_retirement_local_quiescence) = 'object'
        )
    ) NOT VALID;
ALTER TABLE public.threads
    -- squawk-ignore constraint-missing-not-valid
    VALIDATE CONSTRAINT threads_runtime_retirement_local_quiescence_shape;

ALTER TABLE public.threads
    DROP CONSTRAINT IF EXISTS threads_runtime_retirement_external_cleanup_shape;
ALTER TABLE public.threads
    ADD CONSTRAINT threads_runtime_retirement_external_cleanup_shape CHECK (
        runtime_retirement_external_cleanup IS NULL
        OR (
            runtime_retirement_token IS NOT NULL
            AND runtime_retirement_permanent = true
            AND jsonb_typeof(runtime_retirement_external_cleanup) = 'object'
        )
    ) NOT VALID;
ALTER TABLE public.threads
    -- squawk-ignore constraint-missing-not-valid
    VALIDATE CONSTRAINT threads_runtime_retirement_external_cleanup_shape;

CREATE OR REPLACE FUNCTION public.enforce_thread_ended_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    attach_abort boolean := false;
    preflight_control_reopen boolean := false;
    settlement_runtime_exposed boolean := false;
BEGIN
    attach_abort := (
        OLD.status = 'created'
        AND NEW.status = 'created'
        AND OLD.runtime_retirement_token IS NULL
        AND NEW.runtime_retirement_token IS NULL
        AND OLD.agent_id IS NOT NULL
        AND OLD.runtime_attach_token IS NOT NULL
        AND NEW.agent_id IS NULL
        AND NEW.control_admission_agent_id IS NULL
        AND NEW.runtime_attach_token IS NULL
        AND OLD.runtime_authority_exposed
        AND NOT NEW.runtime_authority_exposed
        AND NEW.runtime_generation IS DISTINCT FROM OLD.runtime_generation
        AND jsonb_typeof(NEW.runtime_attach_abort_receipt) = 'object'
        AND COALESCE(NEW.runtime_attach_abort_receipt->>'version', '') = '1'
        AND NEW.runtime_attach_abort_receipt->>'runtime_generation'
            = OLD.runtime_generation::text
        AND NEW.runtime_attach_abort_receipt->>'successor_generation'
            = NEW.runtime_generation::text
        AND NEW.runtime_attach_abort_receipt->>'agent_id' = OLD.agent_id::text
        AND NEW.runtime_attach_abort_receipt->>'runtime_attach_token'
            = OLD.runtime_attach_token::text
        AND NULLIF(NEW.runtime_attach_abort_receipt->>'agent_pod_uid', '')
            IS NOT NULL
        AND NEW.runtime_attach_abort_receipt->>'release_kind'
            IN ('server_pre_delivery', 'process_zero')
    );

    preflight_control_reopen := (
        OLD.runtime_retirement_token IS NOT NULL
        AND NEW.runtime_retirement_token IS NULL
        AND NEW.status IS NOT DISTINCT FROM OLD.status
        AND OLD.runtime_retirement_authorized_at IS NULL
        AND OLD.runtime_retirement_stage_receipt IS NULL
        AND OLD.runtime_retirement_local_quiescence IS NULL
        AND OLD.runtime_retirement_external_cleanup IS NULL
        AND OLD.agent_id IS NOT NULL
        AND OLD.runtime_attach_token IS NOT NULL
        AND OLD.control_admission_agent_id IS NULL
        AND NEW.control_admission_agent_id = OLD.agent_id
        AND NEW.agent_id IS NOT DISTINCT FROM OLD.agent_id
        AND NEW.runtime_attach_token IS NOT DISTINCT FROM OLD.runtime_attach_token
        AND NEW.runtime_generation IS NOT DISTINCT FROM OLD.runtime_generation
        AND OLD.runtime_retirement_context->>'initiator' = 'agent'
        AND OLD.runtime_retirement_context->>'control_admission_reopen_agent_id'
            = OLD.agent_id::text
        AND OLD.runtime_retirement_context->>'generation'
            = OLD.runtime_generation::text
        AND OLD.runtime_retirement_context->>'runtime_attach_token'
            = OLD.runtime_attach_token::text
        AND EXISTS (
            SELECT 1 FROM public.agents AS a
             WHERE a.id = OLD.agent_id
               AND a.thread_id = OLD.id
               AND a.status = 'session'
        )
    );

    -- Runtime generations are immutable inside one session life. The sole
    -- rotation edge is an explicit/legacy Resume. Assign here instead of
    -- trusting the writer so an old `SET status='created'` is generation-safe.
    IF OLD.status = 'ended' AND NEW.status = 'created' THEN
        IF OLD.runtime_retirement_token IS NOT NULL
           OR NEW.runtime_retirement_token IS NOT NULL THEN
            RAISE EXCEPTION
                'ended thread % has unfinished runtime retirement', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_pending';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM public.thread_agent_pod_provision_intents intent
             WHERE intent.thread_id = OLD.id
               AND intent.status IN ('planned', 'revoking', 'fenced')
        ) OR EXISTS (
            SELECT 1
              FROM public.thread_workspace_provision_intents intent
             WHERE intent.thread_id = OLD.id
               AND intent.status IN ('planned', 'revoking', 'fenced')
        ) OR EXISTS (
            SELECT 1
              FROM public.thread_agent_workspace_claims claim
             WHERE claim.thread_id = OLD.id
               AND claim.status IN ('planned', 'revoking', 'fenced')
        ) THEN
            RAISE EXCEPTION
                'ended thread % retains a Kubernetes create fence', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_resume_create_fence_authority';
        END IF;
        IF COALESCE(OLD.metadata->'agent_pod', 'null'::jsonb)
              NOT IN ('null'::jsonb, '{}'::jsonb) THEN
            IF NOT EXISTS (
                SELECT 1
                  FROM public.thread_runtime_retirement_outcomes outcome
                 WHERE outcome.thread_id = OLD.id
                   AND outcome.runtime_generation = OLD.runtime_generation
                   AND outcome.disposition = 'ended'
                   AND outcome.permanent = false
                   AND outcome.outcome = 'settled'
            ) THEN
                RAISE EXCEPTION
                    'ended thread % retains unretired agent Pod authority', OLD.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'threads_resume_agent_pod_authority';
            END IF;
            NEW.metadata := COALESCE(NEW.metadata, '{}'::jsonb) - 'agent_pod';
        END IF;
        -- Resume is the sole creator of G2, never an implicit carry-over of
        -- any G1 actor. This also makes a legacy status-only writer safe after
        -- the maintenance migration: it cannot retain or introduce pointers
        -- while the trigger owns generation rotation.
        NEW.agent_id := NULL;
        NEW.control_admission_agent_id := NULL;
        NEW.runtime_attach_token := NULL;
        NEW.metadata := COALESCE(NEW.metadata, '{}'::jsonb) - 'agent_pod';
        NEW.runtime_generation := public.uuid_generate_v4();
        NEW.runtime_authority_exposed := false;
        IF OLD.execution_lane IN ('pinned', 'stateless') THEN
            UPDATE public.thread_control_requests
               SET runtime_generation = NEW.runtime_generation
             WHERE thread_id = OLD.id
               AND outcome IS NULL
               AND (
                   runtime_generation IS NULL
                   OR runtime_generation IS NOT DISTINCT FROM OLD.runtime_generation
               );
        END IF;
    ELSIF OLD.runtime_retirement_token IS NOT NULL
          AND OLD.runtime_retirement_permanent = false
          AND NEW.runtime_retirement_token IS NULL
          AND NEW.status = 'suspended' THEN
        -- Suspension is a settled, preparable successor state.  Force the
        -- rotation here even for old/direct SQL so no caller can reopen it
        -- under the retired generation (or choose its own generation).
        NEW.runtime_generation := public.uuid_generate_v4();
        NEW.runtime_authority_exposed := false;
        IF OLD.execution_lane = 'pinned' THEN
            UPDATE public.thread_control_requests
               SET runtime_generation = NEW.runtime_generation
             WHERE thread_id = OLD.id
               AND outcome IS NULL
               AND runtime_generation IS NOT DISTINCT FROM OLD.runtime_generation;
        END IF;
    ELSIF attach_abort THEN
        -- An attach that never admitted input/provider work may be rolled
        -- back only by an exact proof-bearing transaction. Rotation (rather
        -- than clearing ownership inside G1) makes every delayed A request
        -- fail against the next attach even when the same pool agent recurs.
        NULL;
    ELSIF OLD.runtime_generation IS DISTINCT FROM NEW.runtime_generation THEN
        RAISE EXCEPTION
            'thread % runtime generation is immutable within one session life', OLD.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_runtime_generation_immutable';
    END IF;

    IF NEW.runtime_attach_abort_receipt IS DISTINCT FROM
       OLD.runtime_attach_abort_receipt AND NOT attach_abort THEN
        RAISE EXCEPTION
            'thread % attach-abort receipt lacks exact rotation shape', OLD.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_runtime_attach_abort_receipt';
    END IF;

    -- A per-generation authority exposure is monotonic.  New orchestrators
    -- set it explicitly before delivery; this trigger also catches old bind
    -- SQL so a rolling writer cannot create an unrecorded exact process.
    IF NEW.runtime_generation IS NOT DISTINCT FROM OLD.runtime_generation THEN
        IF OLD.execution_lane = 'pinned'
           AND OLD.agent_id IS NULL
           AND NEW.agent_id IS NOT NULL
           AND NEW.runtime_attach_token IS NULL THEN
            -- Mixed-version old bind SQL has no attach-token field. Mint the
            -- exact process attempt at the database edge so the new
            -- orchestrator can fence/retire it even though the old actor never
            -- learns or echoes this credential.
            NEW.runtime_attach_token := public.uuid_generate_v4();
        END IF;
        IF OLD.execution_lane = 'pinned'
           AND (NEW.agent_id IS NULL) <> (NEW.runtime_attach_token IS NULL) THEN
            RAISE EXCEPTION
                'thread % agent/attach authority must be reciprocal', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_attach_authority_shape';
        END IF;
        IF NEW.metadata ? 'agent_pod'
           AND NEW.metadata->'agent_pod' IS NOT NULL
           AND NEW.metadata->'agent_pod' NOT IN ('null'::jsonb, '{}'::jsonb)
           AND (
               jsonb_typeof(NEW.metadata->'agent_pod') IS DISTINCT FROM 'object'
               OR NULLIF(NEW.metadata->'agent_pod'->>'pod_name', '') IS NULL
               OR NULLIF(NEW.metadata->'agent_pod'->>'pod_uid', '') IS NULL
           ) THEN
            RAISE EXCEPTION
                'thread % agent Pod authority is incomplete', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_agent_pod_authority_shape';
        END IF;
        IF NEW.agent_id IS NOT NULL
           AND NEW.metadata ? 'agent_pod'
           AND NEW.metadata->'agent_pod' IS NOT NULL
           AND NEW.metadata->'agent_pod' NOT IN ('null'::jsonb, '{}'::jsonb)
           AND EXISTS (
               SELECT 1
                FROM public.agents AS agent
                WHERE agent.id = NEW.agent_id
                  AND (
                      NEW.metadata->'agent_pod'->>'pod_name'
                           IS DISTINCT FROM agent.hostname
                      OR NEW.metadata->'agent_pod'->>'pod_uid'
                           IS DISTINCT FROM agent.pod_uid
                  )
           ) THEN
            RAISE EXCEPTION
                'thread % publishes conflicting agent Pod identities', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_agent_pod_authority_identity';
        END IF;
        IF NEW.agent_id IS NOT NULL
           OR NEW.runtime_attach_token IS NOT NULL
           OR (
               NEW.metadata ? 'agent_pod'
               AND NEW.metadata->'agent_pod' IS NOT NULL
               AND NEW.metadata->'agent_pod'
                   NOT IN ('null'::jsonb, '{}'::jsonb)
           ) THEN
            NEW.runtime_authority_exposed := true;
        ELSIF OLD.runtime_authority_exposed
              AND NOT NEW.runtime_authority_exposed THEN
            RAISE EXCEPTION
                'thread % runtime exposure history is immutable', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_authority_exposed_immutable';
        END IF;
    END IF;

    IF OLD.status = 'ended' AND NEW.status NOT IN ('ended', 'created') THEN
        RAISE EXCEPTION
            'ended thread % may only take the resume-shaped created edge', OLD.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_ended_transition_fence';
    END IF;
    IF OLD.status = 'ended'
       AND NEW.status = 'ended'
       AND NEW.runtime_generation IS NOT DISTINCT FROM OLD.runtime_generation
       AND (
           NEW.agent_id IS NOT NULL
           OR NEW.control_admission_agent_id IS NOT NULL
           OR NEW.runtime_attach_token IS NOT NULL
           OR (
               NEW.metadata ? 'agent_pod'
               AND NEW.metadata->'agent_pod' IS NOT NULL
               AND NEW.metadata->'agent_pod'
                   NOT IN ('null'::jsonb, '{}'::jsonb)
           )
       ) THEN
        RAISE EXCEPTION
            'ended thread % cannot publish runtime authority before Resume', OLD.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_ended_runtime_authority';
    END IF;

    -- A pending pinned retirement closes every runtime/status admission path.
    -- Only its exact settlement may change lifecycle status: live -> ended
    -- while atomically clearing the four-column marker. A non-force abort may
    -- clear the marker without changing status.
    IF OLD.runtime_retirement_token IS NOT NULL THEN
        IF NEW.runtime_retirement_token IS NOT NULL AND (
            NEW.runtime_retirement_token IS DISTINCT FROM OLD.runtime_retirement_token
            OR NEW.runtime_retirement_permanent IS DISTINCT FROM OLD.runtime_retirement_permanent
            OR NEW.runtime_retirement_started_at IS DISTINCT FROM OLD.runtime_retirement_started_at
            OR NEW.runtime_retirement_context IS DISTINCT FROM OLD.runtime_retirement_context
        ) THEN
            RAISE EXCEPTION
                'thread % runtime retirement identity is immutable', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_immutable';
        END IF;
        IF OLD.runtime_retirement_stage_receipt IS NOT NULL
           AND NEW.runtime_retirement_stage_receipt IS DISTINCT FROM
               OLD.runtime_retirement_stage_receipt
           AND NOT (
               NEW.status IN ('ended', 'suspended')
               AND OLD.runtime_retirement_permanent = false
               AND NEW.runtime_retirement_token IS NULL
           ) THEN
            RAISE EXCEPTION
                'thread % runtime retirement stage receipt is immutable', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_stage_receipt_immutable';
        END IF;
        IF OLD.runtime_retirement_local_quiescence IS NOT NULL
           AND NEW.runtime_retirement_local_quiescence IS DISTINCT FROM
               OLD.runtime_retirement_local_quiescence
           AND NOT (
               NEW.status IN ('ended', 'suspended')
               AND OLD.runtime_retirement_permanent = false
               AND NEW.runtime_retirement_token IS NULL
           ) THEN
            RAISE EXCEPTION
                'thread % runtime local-quiescence receipt is immutable', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_local_quiescence_immutable';
        END IF;
        IF OLD.runtime_retirement_external_cleanup IS NOT NULL
           AND NEW.runtime_retirement_external_cleanup IS DISTINCT FROM
               OLD.runtime_retirement_external_cleanup THEN
            RAISE EXCEPTION
                'thread % runtime external-cleanup receipt is immutable', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_external_cleanup_immutable';
        END IF;
        IF NEW.runtime_retirement_external_cleanup IS NOT NULL AND (
            NEW.runtime_retirement_permanent IS DISTINCT FROM true
            OR NEW.runtime_retirement_authorized_at IS NULL
            OR NEW.runtime_retirement_external_cleanup IS DISTINCT FROM
               public.pinned_retirement_external_cleanup_expected(
                   NEW.runtime_retirement_context,
                   NEW.runtime_generation,
                   NEW.runtime_retirement_token
               )
            OR NOT public.pinned_retirement_workspace_provision_intent_retired(
                NEW.id,
                NEW.runtime_generation,
                NEW.runtime_retirement_context->'workspace_provision_intent',
                true
            )
            OR jsonb_typeof(NEW.metadata) IS DISTINCT FROM 'object'
            OR COALESCE(
                   jsonb_typeof(NEW.metadata->'workspace_container'), 'null'
               ) NOT IN ('object', 'null')
            OR COALESCE(
                   jsonb_typeof(NEW.metadata->'_workspace_binding'), 'null'
               ) NOT IN ('object', 'null')
            OR COALESCE(
                   NEW.metadata->'workspace_container'->>'status', ''
               ) NOT IN ('', 'deleted')
            OR NEW.metadata->'workspace_container'->>'_runtime_incarnation'
               IS NOT NULL
            OR NEW.metadata->'workspace_container'->>'_docker_workspace_lease_id'
               IS NOT NULL
            OR NEW.metadata->'workspace_container'->>'pod_ip' IS NOT NULL
            OR NEW.metadata->'workspace_container'->>'pod_name' IS NOT NULL
            OR NEW.metadata->'workspace_container'->>'host' IS NOT NULL
            OR NEW.metadata->'workspace_container'->>'port' IS NOT NULL
            OR NEW.metadata->'workspace_container'->>'ide_host' IS NOT NULL
            OR NEW.metadata->'workspace_container'->>'ide_port' IS NOT NULL
            OR NEW.metadata->'workspace_container'->>'_canvas_workspace_generation'
               IS NOT NULL
            OR COALESCE(
                   NEW.metadata->'_workspace_binding', '{}'::jsonb
               ) <> '{}'::jsonb
            OR COALESCE(jsonb_typeof(NEW.metadata->'vm'), 'null')
               NOT IN ('object', 'null')
            OR COALESCE(NEW.metadata->'vm'->>'status', '') NOT IN ('', 'deleted')
            OR NEW.metadata->'vm'->>'provision_generation' IS NOT NULL
            OR NEW.metadata->'vm'->>'identity_provision_generation' IS NOT NULL
            OR NEW.metadata->'vm'->>'vm_uid' IS NOT NULL
            OR NEW.metadata->'vm'->>'_runtime_incarnation' IS NOT NULL
            OR NEW.metadata->'vm'->>'rootdisk_pvc_uid' IS NOT NULL
            OR NEW.metadata->'vm'->>'ssh_host' IS NOT NULL
            OR NEW.metadata->'vm'->>'ssh_port' IS NOT NULL
            OR NEW.metadata->'vm'->>'_canvas_workspace_generation' IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM public.cloud_ro_mounts ro
                 WHERE ro.thread_id = NEW.id
                   AND ro.status IN ('engaging', 'active', 'revoking')
            )
            OR EXISTS (
                SELECT 1 FROM public.docker_workspace_leases lease
                 WHERE lease.owner_kind = 'thread'
                   AND lease.owner_id = NEW.id
                   AND lease.status IN ('ready', 'releasing')
            )
            OR EXISTS (
                SELECT 1
                  FROM public.thread_workspace_provision_intents intent
                 WHERE intent.thread_id = NEW.id
                   AND intent.status IN ('planned', 'revoking')
            )
        ) THEN
            RAISE EXCEPTION
                'thread % runtime external-cleanup receipt is malformed', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_external_cleanup_identity';
        END IF;
        IF NEW.runtime_retirement_token IS NOT NULL
           AND NEW.runtime_retirement_local_quiescence IS NOT NULL
           AND (
               jsonb_typeof(NEW.runtime_retirement_context) IS DISTINCT FROM 'object'
               OR COALESCE(
                    jsonb_typeof(
                        NEW.runtime_retirement_context->'workspace_container'
                    ), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(
                        NEW.runtime_retirement_context->'workspace_binding'
                    ), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(NEW.runtime_retirement_context->'vm'), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(NEW.runtime_retirement_context->'agent'), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(NEW.runtime_retirement_context->'agent_pod'), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(
                        NEW.runtime_retirement_context
                            ->'agent_pod_provision_intent'
                    ), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(
                        NEW.runtime_retirement_context
                            ->'workspace_provision_intent'
                    ), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(NEW.runtime_retirement_context->'protected_ro'), 'null'
                  ) NOT IN ('object', 'null')
               OR (
                    NEW.runtime_retirement_context->'agent' IS NOT NULL
                    AND NEW.runtime_retirement_context->'agent'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                    AND (
                        NULLIF(
                            NEW.runtime_retirement_context->'agent'->>'hostname', ''
                        ) IS NULL
                        OR NULLIF(
                            NEW.runtime_retirement_context->'agent'->>'pod_uid', ''
                        ) IS NULL
                    )
               )
               OR (
                    NEW.runtime_retirement_context->'agent_pod' IS NOT NULL
                    AND NEW.runtime_retirement_context->'agent_pod'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                    AND (
                        NULLIF(
                            NEW.runtime_retirement_context->'agent_pod'->>'pod_name', ''
                        ) IS NULL
                        OR NULLIF(
                            NEW.runtime_retirement_context->'agent_pod'->>'pod_uid', ''
                        ) IS NULL
                    )
               )
               OR (
                    NEW.runtime_retirement_context
                        ->'agent_pod_provision_intent' IS NOT NULL
                    AND NEW.runtime_retirement_context
                        ->'agent_pod_provision_intent'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                    AND (
                        NULLIF(
                            NEW.runtime_retirement_context
                                ->'agent_pod_provision_intent'->>'attempt_id', ''
                        ) IS NULL
                        OR NULLIF(
                            NEW.runtime_retirement_context
                                ->'agent_pod_provision_intent'->>'pod_name', ''
                        ) IS NULL
                        OR NEW.runtime_retirement_context
                            ->'agent_pod_provision_intent'->>'runtime_generation'
                           IS DISTINCT FROM NEW.runtime_generation::text
                        OR NEW.runtime_retirement_context
                            ->'agent_pod_provision_intent'->>'provisioner'
                           NOT IN ('agent', 'persistent')
                        OR NEW.runtime_retirement_context
                            ->'agent_pod_provision_intent'->>'status' <> 'planned'
                    )
               )
               OR COALESCE(
                   NEW.runtime_retirement_local_quiescence->>'version', ''
               ) <> '1'
               OR NEW.runtime_retirement_local_quiescence->>'runtime_generation'
                  IS DISTINCT FROM NEW.runtime_generation::text
               OR NEW.runtime_retirement_local_quiescence->>'retirement_token'
                  IS DISTINCT FROM NEW.runtime_retirement_token::text
               OR NEW.runtime_retirement_local_quiescence->>'agent_id'
                  IS DISTINCT FROM NEW.runtime_retirement_context->>'agent_id'
               OR NEW.runtime_retirement_local_quiescence->>'runtime_attach_token'
                  IS DISTINCT FROM
                     NEW.runtime_retirement_context->>'runtime_attach_token'
               OR (
                    NEW.runtime_retirement_context->'agent_pod' IS NOT NULL
                    AND NEW.runtime_retirement_context->'agent_pod'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                    AND NEW.runtime_retirement_context->>'agent_id' IS NULL
                    AND NEW.runtime_retirement_context->>'runtime_attach_token'
                        IS NULL
                    AND (
                        NEW.runtime_retirement_local_quiescence
                            ->>'quiescence_actor' <> 'orchestrator'
                        OR NEW.runtime_retirement_local_quiescence
                            ->>'quiescence_protocol' <> 'agent_runtime_zero_v1'
                        OR NEW.runtime_retirement_local_quiescence
                            ->>'agent_pod_name' IS DISTINCT FROM
                           NEW.runtime_retirement_context
                            ->'agent_pod'->>'pod_name'
                        OR NEW.runtime_retirement_local_quiescence
                            ->>'agent_pod_uid' IS DISTINCT FROM
                           NEW.runtime_retirement_context
                            ->'agent_pod'->>'pod_uid'
                    )
               )
               OR (
                    NEW.runtime_retirement_context
                        ->'agent_pod_provision_intent' IS NOT NULL
                    AND NEW.runtime_retirement_context
                        ->'agent_pod_provision_intent'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                    AND (
                        NEW.runtime_retirement_context->>'agent_id' IS NOT NULL
                        OR NEW.runtime_retirement_context
                            ->>'runtime_attach_token' IS NOT NULL
                        OR NEW.runtime_retirement_local_quiescence
                            ->>'quiescence_actor' <> 'orchestrator'
                        OR NEW.runtime_retirement_local_quiescence
                            ->>'quiescence_protocol' <> 'agent_runtime_zero_v1'
                        OR NEW.runtime_retirement_local_quiescence
                            ->>'agent_pod_provision_attempt' IS DISTINCT FROM
                           NEW.runtime_retirement_context
                            ->'agent_pod_provision_intent'->>'attempt_id'
                        OR NEW.runtime_retirement_local_quiescence
                            ->>'agent_pod_name' IS DISTINCT FROM
                           NEW.runtime_retirement_context
                            ->'agent_pod_provision_intent'->>'pod_name'
                        OR NULLIF(
                            NEW.runtime_retirement_local_quiescence
                                ->>'agent_pod_uid', ''
                        ) IS NULL
                        OR NEW.runtime_retirement_local_quiescence
                            ->>'agent_pod_fence_protocol'
                           <> 'k8s_name_tombstone_v1'
                        OR NOT EXISTS (
                            SELECT 1
                              FROM public.thread_agent_pod_provision_intents intent
                             WHERE intent.attempt_id::text =
                                    NEW.runtime_retirement_context
                                        ->'agent_pod_provision_intent'
                                        ->>'attempt_id'
                               AND intent.thread_id = NEW.id
                               AND intent.runtime_generation = NEW.runtime_generation
                               AND intent.pod_name = NEW.runtime_retirement_context
                                    ->'agent_pod_provision_intent'->>'pod_name'
                               AND intent.status = 'fenced'
                               AND intent.pod_uid = NEW.runtime_retirement_local_quiescence
                                    ->>'agent_pod_uid'
                        )
                    )
               )
               OR NEW.runtime_retirement_local_quiescence->>'settle_status'
                  IS DISTINCT FROM
                     NEW.runtime_retirement_context->>'settle_status'
               OR (CASE
                    WHEN NEW.runtime_retirement_context
                            ->'workspace_provision_intent'
                         NOT IN ('null'::jsonb, '{}'::jsonb)
                    THEN NEW.runtime_retirement_local_quiescence
                            ->>'quiescence_protocol'
                         = 'agent_runtime_zero_v1'
                         AND NEW.runtime_retirement_local_quiescence
                            ->>'workspace_generation' IS NULL
                         AND NEW.runtime_retirement_local_quiescence
                            ->>'workspace_runtime_incarnation' IS NULL
                    WHEN NEW.runtime_retirement_context->>'workspace_backend' = 'sandbox'
                         AND OLD.runtime_retirement_permanent = true
                         AND NEW.runtime_retirement_local_quiescence->>'quiescence_actor'
                             = 'orchestrator'
                         AND NEW.runtime_retirement_local_quiescence->>'quiescence_protocol'
                             = 'sandbox_actuator_zero_v1'
                         AND (
                             COALESCE(
                                 NEW.runtime_retirement_context->'workspace_binding',
                                 '{}'::jsonb
                             ) <> '{}'::jsonb
                             OR COALESCE(
                                 NEW.runtime_retirement_context->'workspace_container'->>'status',
                                 ''
                             ) NOT IN ('', 'deleted')
                             OR NEW.runtime_retirement_context->'workspace_container'->>'_runtime_incarnation'
                                 IS NOT NULL
                             OR NEW.runtime_retirement_context->'workspace_container'->>'pod_ip'
                                 IS NOT NULL
                             OR NEW.runtime_retirement_context->'workspace_container'->>'pod_name'
                                 IS NOT NULL
                             OR NEW.runtime_retirement_context->'workspace_container'->>'host'
                                 IS NOT NULL
                             OR NEW.runtime_retirement_context->'workspace_container'->>'port'
                                 IS NOT NULL
                             OR NEW.runtime_retirement_context->'workspace_container'->>'ide_host'
                                 IS NOT NULL
                             OR NEW.runtime_retirement_context->'workspace_container'->>'ide_port'
                                 IS NOT NULL
                             OR NEW.runtime_retirement_context->'workspace_container'->>'_canvas_workspace_generation'
                                 IS NOT NULL
                         )
                    THEN true
                    WHEN NEW.runtime_retirement_context->>'workspace_backend' = 'sandbox'
                         AND NULLIF(
                             NEW.runtime_retirement_context->'workspace_binding'->>'generation',
                             ''
                         ) IS NOT NULL
                         AND NULLIF(
                             COALESCE(
                                 NEW.runtime_retirement_context->'workspace_container'->>'_runtime_incarnation',
                                 NEW.runtime_retirement_context->'workspace_container'->>'_docker_workspace_lease_id'
                             ),
                             ''
                         ) IS NOT NULL
                    THEN NEW.runtime_retirement_local_quiescence->>'quiescence_protocol'
                         = 'workspace_process_zero_v1'
                    WHEN NEW.runtime_retirement_context->>'workspace_backend' = 'sandbox'
                         AND NULLIF(
                             NEW.runtime_retirement_context->'workspace_binding'->>'generation',
                             ''
                         ) IS NULL
                         AND NULLIF(
                             COALESCE(
                                 NEW.runtime_retirement_context->'workspace_container'->>'_runtime_incarnation',
                                 NEW.runtime_retirement_context->'workspace_container'->>'_docker_workspace_lease_id'
                             ),
                             ''
                         ) IS NULL
                    THEN NEW.runtime_retirement_local_quiescence->>'quiescence_protocol'
                         = 'agent_runtime_zero_v1'
                         AND NEW.runtime_retirement_local_quiescence->>'workspace_generation'
                             IS NULL
                         AND NEW.runtime_retirement_local_quiescence->>'workspace_runtime_incarnation'
                             IS NULL
                    WHEN NEW.runtime_retirement_context->>'workspace_backend'
                         IN ('virtual', 'none')
                    THEN NEW.runtime_retirement_local_quiescence->>'quiescence_protocol'
                         = 'agent_runtime_zero_v1'
                         AND NEW.runtime_retirement_local_quiescence->>'workspace_generation'
                             IS NULL
                         AND NEW.runtime_retirement_local_quiescence->>'workspace_runtime_incarnation'
                             IS NULL
                    WHEN NEW.runtime_retirement_context->>'workspace_backend'
                         IN ('vm', 'remote')
                    THEN NEW.runtime_retirement_local_quiescence->>'quiescence_protocol'
                         = 'workspace_actuator_zero_v1'
                         AND NULLIF(
                             NEW.runtime_retirement_context->'vm'->>'provision_generation',
                             ''
                         ) IS NOT NULL
                         AND NULLIF(
                             NEW.runtime_retirement_context->'vm'->>'vm_uid',
                             ''
                         ) IS NOT NULL
                    ELSE false
                  END) IS NOT TRUE
               OR COALESCE(
                      NEW.runtime_retirement_local_quiescence->>'quiescence_actor',
                      ''
                  ) NOT IN ('agent', 'orchestrator')
               OR NEW.runtime_retirement_local_quiescence->>'workspace_generation'
                  IS DISTINCT FROM CASE
                    WHEN NEW.runtime_retirement_context
                            ->'workspace_provision_intent'
                         NOT IN ('null'::jsonb, '{}'::jsonb)
                    THEN NULL
                    WHEN NEW.runtime_retirement_context->>'workspace_backend' = 'sandbox'
                    THEN NEW.runtime_retirement_context->'workspace_binding'->>'generation'
                    WHEN NEW.runtime_retirement_context->>'workspace_backend' IN ('vm', 'remote')
                    THEN NEW.runtime_retirement_context->'vm'->>'provision_generation'
                    ELSE NULL
                  END
               OR NEW.runtime_retirement_local_quiescence->>'workspace_runtime_incarnation'
                  IS DISTINCT FROM CASE
                    WHEN NEW.runtime_retirement_context
                            ->'workspace_provision_intent'
                         NOT IN ('null'::jsonb, '{}'::jsonb)
                    THEN NULL
                    WHEN NEW.runtime_retirement_context->>'workspace_backend' = 'sandbox'
                    THEN COALESCE(
                        NEW.runtime_retirement_context->'workspace_container'->>'_runtime_incarnation',
                        NEW.runtime_retirement_context->'workspace_container'->>'_docker_workspace_lease_id'
                    )
                    WHEN NEW.runtime_retirement_context->>'workspace_backend' IN ('vm', 'remote')
                    THEN NEW.runtime_retirement_context->'vm'->>'vm_uid'
                    ELSE NULL
                  END
           ) THEN
            RAISE EXCEPTION
                'thread % runtime local-quiescence receipt is malformed', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_local_quiescence_identity';
        END IF;
        IF OLD.runtime_retirement_authorized_at IS NOT NULL
           AND NEW.runtime_retirement_authorized_at IS DISTINCT FROM
               OLD.runtime_retirement_authorized_at
           AND NEW.runtime_retirement_token IS NOT NULL THEN
            RAISE EXCEPTION
                'thread % runtime retirement authorization is immutable', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_authorized_immutable';
        END IF;
        -- The only same-status marker-clear edge is an exact soft abort.  It
        -- may reopen admission, but it may not smuggle an ownership or
        -- generation replacement into the same UPDATE.  Begin retirement
        -- deliberately leaves these identities in place, so an abort is a
        -- marker-only operation.
        IF NEW.runtime_retirement_token IS NULL
           AND NEW.status IS NOT DISTINCT FROM OLD.status
           AND (
               NEW.runtime_generation IS DISTINCT FROM OLD.runtime_generation
               OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
               OR (
                   NEW.control_admission_agent_id IS DISTINCT FROM
                       OLD.control_admission_agent_id
                   AND NOT preflight_control_reopen
               )
               OR NEW.runtime_attach_token IS DISTINCT FROM OLD.runtime_attach_token
           ) THEN
            RAISE EXCEPTION
                'thread % runtime ownership cannot change while aborting retirement', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_ownership';
        END IF;
        IF NEW.runtime_retirement_token IS NULL
           AND NEW.status IS NOT DISTINCT FROM OLD.status
           AND (
               OLD.runtime_retirement_authorized_at IS NOT NULL
               OR
               OLD.runtime_retirement_stage_receipt IS NOT NULL
               OR OLD.runtime_retirement_local_quiescence IS NOT NULL
               OR OLD.runtime_retirement_external_cleanup IS NOT NULL
           ) THEN
            RAISE EXCEPTION
                'thread % retirement is authorized/published and cannot be aborted', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_stage_receipt_pending';
        END IF;
        IF NEW.runtime_retirement_token IS NOT NULL AND (
            NEW.agent_id IS DISTINCT FROM OLD.agent_id
            OR NEW.control_admission_agent_id IS DISTINCT FROM OLD.control_admission_agent_id
            OR NEW.runtime_attach_token IS DISTINCT FROM OLD.runtime_attach_token
        ) THEN
            RAISE EXCEPTION
                'thread % runtime ownership cannot change during retirement', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_ownership';
        END IF;
        -- ``permanent`` is intent, not authorization. A still-hidden
        -- preflight may be cleared for either mode; once authorized, the
        -- append-only authorization check above makes every mode irrevocable.
        IF NEW.status IS DISTINCT FROM OLD.status THEN
            IF jsonb_typeof(OLD.runtime_retirement_context) IS DISTINCT FROM 'object'
               OR COALESCE(
                    jsonb_typeof(
                        OLD.runtime_retirement_context->'workspace_container'
                    ), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(
                        OLD.runtime_retirement_context->'workspace_binding'
                    ), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(OLD.runtime_retirement_context->'vm'), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(OLD.runtime_retirement_context->'agent'), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(OLD.runtime_retirement_context->'agent_pod'), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(
                        OLD.runtime_retirement_context
                            ->'agent_pod_provision_intent'
                    ), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(
                        OLD.runtime_retirement_context
                            ->'workspace_provision_intent'
                    ), 'null'
                  ) NOT IN ('object', 'null')
               OR COALESCE(
                    jsonb_typeof(OLD.runtime_retirement_context->'protected_ro'), 'null'
                  ) NOT IN ('object', 'null')
               OR (
                    OLD.runtime_retirement_context->'agent' IS NOT NULL
                    AND OLD.runtime_retirement_context->'agent'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                    AND (
                        NULLIF(
                            OLD.runtime_retirement_context->'agent'->>'hostname', ''
                        ) IS NULL
                        OR NULLIF(
                            OLD.runtime_retirement_context->'agent'->>'pod_uid', ''
                        ) IS NULL
                    )
               )
               OR (
                    OLD.runtime_retirement_context->'agent_pod' IS NOT NULL
                    AND OLD.runtime_retirement_context->'agent_pod'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                    AND (
                        NULLIF(
                            OLD.runtime_retirement_context->'agent_pod'->>'pod_name', ''
                        ) IS NULL
                        OR NULLIF(
                            OLD.runtime_retirement_context->'agent_pod'->>'pod_uid', ''
                        ) IS NULL
                    )
               )
               OR (
                    OLD.runtime_retirement_context
                        ->'agent_pod_provision_intent' IS NOT NULL
                    AND OLD.runtime_retirement_context
                        ->'agent_pod_provision_intent'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                    AND (
                        NULLIF(
                            OLD.runtime_retirement_context
                                ->'agent_pod_provision_intent'->>'attempt_id', ''
                        ) IS NULL
                        OR NULLIF(
                            OLD.runtime_retirement_context
                                ->'agent_pod_provision_intent'->>'pod_name', ''
                        ) IS NULL
                        OR OLD.runtime_retirement_context
                            ->'agent_pod_provision_intent'->>'runtime_generation'
                           IS DISTINCT FROM OLD.runtime_generation::text
                        OR OLD.runtime_retirement_context
                            ->'agent_pod_provision_intent'->>'provisioner'
                           NOT IN ('agent', 'persistent')
                        OR OLD.runtime_retirement_context
                            ->'agent_pod_provision_intent'->>'status' <> 'planned'
                    )
               )
               OR (
                    OLD.metadata ? 'agent_pod'
                    AND OLD.metadata->'agent_pod' IS NOT NULL
                    AND OLD.metadata->'agent_pod' <> 'null'::jsonb
                    AND (
                        jsonb_typeof(OLD.metadata->'agent_pod')
                            IS DISTINCT FROM 'object'
                        OR OLD.metadata->'agent_pod' = '{}'::jsonb
                        OR NULLIF(
                            OLD.metadata->'agent_pod'->>'pod_name', ''
                        ) IS NULL
                        OR NULLIF(
                            OLD.metadata->'agent_pod'->>'pod_uid', ''
                        ) IS NULL
                        OR OLD.runtime_retirement_context->'agent_pod'
                            IS DISTINCT FROM OLD.metadata->'agent_pod'
                    )
               )
               OR (
                    NEW.metadata ? 'agent_pod'
                    AND NEW.metadata->'agent_pod' IS NOT NULL
                    AND NEW.metadata->'agent_pod'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                    AND (
                        jsonb_typeof(NEW.metadata->'agent_pod')
                            IS DISTINCT FROM 'object'
                        OR OLD.runtime_retirement_context->'agent_pod'
                            IS DISTINCT FROM NEW.metadata->'agent_pod'
                    )
               ) THEN
                RAISE EXCEPTION
                    'thread % retirement context is malformed', OLD.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'threads_runtime_retirement_context_shape';
            END IF;
            settlement_runtime_exposed := COALESCE((
                OLD.runtime_authority_exposed
                OR NEW.runtime_authority_exposed
                OR OLD.agent_id IS NOT NULL
                OR OLD.runtime_attach_token IS NOT NULL
                OR OLD.runtime_retirement_context->>'runtime_authority_exposed'
                    = 'true'
                OR NULLIF(
                    OLD.runtime_retirement_context->>'agent_id', ''
                ) IS NOT NULL
                OR NULLIF(
                    OLD.runtime_retirement_context->>'runtime_attach_token', ''
                ) IS NOT NULL
                OR (
                    OLD.runtime_retirement_context->'agent' IS NOT NULL
                    AND OLD.runtime_retirement_context->'agent' <> 'null'::jsonb
                    AND OLD.runtime_retirement_context->'agent' <> '{}'::jsonb
                )
                OR (
                    OLD.runtime_retirement_context->'agent_pod' IS NOT NULL
                    AND OLD.runtime_retirement_context->'agent_pod' <> 'null'::jsonb
                    AND OLD.runtime_retirement_context->'agent_pod' <> '{}'::jsonb
                )
                OR (
                    OLD.runtime_retirement_context
                        ->'agent_pod_provision_intent' IS NOT NULL
                    AND OLD.runtime_retirement_context
                        ->'agent_pod_provision_intent'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                )
                OR (
                    OLD.metadata ? 'agent_pod'
                    AND OLD.metadata->'agent_pod' IS NOT NULL
                    AND OLD.metadata->'agent_pod'
                        NOT IN ('null'::jsonb, '{}'::jsonb)
                )
            ), false);
            IF NOT (
                NEW.status IN ('ended', 'suspended')
                AND OLD.runtime_retirement_permanent = false
                AND OLD.runtime_retirement_authorized_at IS NOT NULL
                AND NEW.runtime_retirement_token IS NULL
                AND NEW.runtime_retirement_permanent IS NULL
                AND NEW.runtime_retirement_started_at IS NULL
                AND NEW.runtime_retirement_authorized_at IS NULL
                AND NEW.runtime_retirement_context IS NULL
                AND NEW.runtime_retirement_stage_receipt IS NULL
                AND NEW.runtime_retirement_local_quiescence IS NULL
                AND NEW.runtime_retirement_external_cleanup IS NULL
                AND NEW.agent_id IS NULL
                AND NEW.control_admission_agent_id IS NULL
                AND NEW.runtime_attach_token IS NULL
                AND (
                    (NEW.status = 'ended' AND NEW.ended_at IS NOT NULL)
                    OR (NEW.status = 'suspended' AND NEW.ended_at IS NULL)
                )
            ) THEN
                RAISE EXCEPTION
                    'thread % runtime retirement is still in progress', OLD.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'threads_runtime_retirement_pending';
            END IF;
            IF COALESCE(
                   OLD.runtime_retirement_context->>'settle_status',
                   ''
               ) IS DISTINCT FROM NEW.status THEN
                RAISE EXCEPTION
                    'thread % retirement disposition does not match settlement', OLD.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'threads_runtime_retirement_disposition';
            END IF;
            IF settlement_runtime_exposed
               AND OLD.runtime_retirement_local_quiescence IS NULL THEN
                RAISE EXCEPTION
                    'thread % exposed runtime has no local-quiescence receipt', OLD.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'threads_runtime_retirement_local_quiescence_pending';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.thread_agent_pod_provision_intents intent
                 WHERE intent.thread_id = OLD.id
                   AND intent.status IN ('planned', 'revoking')
            ) THEN
                RAISE EXCEPTION
                    'thread % agent Pod provision intent is unresolved', OLD.id
                    USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_runtime_retirement_local_quiescence_pending';
            END IF;
            IF NOT public.pinned_retirement_workspace_provision_intent_retired(
                OLD.id,
                OLD.runtime_generation,
                OLD.runtime_retirement_context->'workspace_provision_intent',
                false
            ) THEN
                RAISE EXCEPTION
                    'thread % workspace provision intent is unresolved', OLD.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'threads_runtime_retirement_external_cleanup_pending';
            END IF;
            IF OLD.runtime_retirement_context->'agent_workspace_claim'
                   NOT IN ('null'::jsonb, '{}'::jsonb) THEN
                IF jsonb_typeof(
                       OLD.runtime_retirement_context->'agent_workspace_claim'
                   ) IS DISTINCT FROM 'object'
                   OR NOT EXISTS (
                        SELECT 1
                          FROM public.thread_agent_workspace_claims claim
                         WHERE claim.thread_id = OLD.id
                           AND claim.claim_id::text = OLD.runtime_retirement_context
                                ->'agent_workspace_claim'->>'claim_id'
                           AND claim.created_runtime_generation::text =
                               OLD.runtime_retirement_context
                                ->'agent_workspace_claim'
                                ->>'created_runtime_generation'
                           AND claim.create_attempt::text =
                               OLD.runtime_retirement_context
                                ->'agent_workspace_claim'->>'create_attempt'
                           AND claim.provisioner = OLD.runtime_retirement_context
                                ->'agent_workspace_claim'->>'provisioner'
                           AND claim.pvc_name = OLD.runtime_retirement_context
                                ->'agent_workspace_claim'->>'pvc_name'
                           AND claim.status = 'ready'
                           AND NULLIF(claim.pvc_uid, '') IS NOT NULL
                           AND (
                                OLD.runtime_retirement_context
                                  ->'agent_workspace_claim'->>'status' = 'planned'
                                OR (
                                    OLD.runtime_retirement_context
                                      ->'agent_workspace_claim'->>'status' = 'ready'
                                    AND claim.pvc_uid = OLD.runtime_retirement_context
                                      ->'agent_workspace_claim'->>'pvc_uid'
                                )
                           )
                   ) THEN
                    RAISE EXCEPTION
                        'thread % agent workspace claim is unresolved', OLD.id
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'threads_runtime_retirement_external_cleanup_pending';
                END IF;
            ELSIF EXISTS (
                SELECT 1
                  FROM public.thread_agent_workspace_claims claim
                 WHERE claim.thread_id = OLD.id
                   AND claim.status <> 'reclaimed'
            ) THEN
                RAISE EXCEPTION
                    'thread % has uncaptured agent workspace authority', OLD.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'threads_runtime_retirement_external_cleanup_pending';
            END IF;
            -- The receipt above retires this exact G/process. Do not carry the
            -- old Pod tuple into the ended/suspended successor life, where it
            -- would be misattributed as fresh process exposure.
            NEW.metadata := COALESCE(NEW.metadata, '{}'::jsonb) - 'agent_pod';
            IF COALESCE(
                   OLD.runtime_retirement_context->>'protected_cloud',
                   'false'
               ) = 'true' THEN
                IF OLD.runtime_retirement_stage_receipt IS NULL THEN
                    RAISE EXCEPTION
                        'protected thread % retirement has no final staging receipt', OLD.id
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'threads_runtime_retirement_stage_receipt_pending';
                END IF;
                -- A staging receipt may authorize cleanup after the mutable
                -- mount row and workspace have gone. Bind it to the exact
                -- source captured by Begin; a matching epoch/attempt alone
                -- must never relabel A's bytes as a successor source B.
                IF jsonb_typeof(OLD.runtime_retirement_stage_receipt)
                       IS DISTINCT FROM 'object'
                   OR OLD.runtime_retirement_stage_receipt->>'version'
                       IS DISTINCT FROM '1'
                   OR COALESCE(
                        OLD.runtime_retirement_stage_receipt->>'kind', ''
                      ) NOT IN ('uploaded', 'unchanged', 'empty', 'never_engaged')
                   OR OLD.runtime_retirement_stage_receipt->>'runtime_generation'
                       IS DISTINCT FROM OLD.runtime_generation::text
                   OR OLD.runtime_retirement_stage_receipt->>'retirement_token'
                       IS DISTINCT FROM OLD.runtime_retirement_token::text
                   OR (
                        OLD.runtime_retirement_context->'protected_ro' IS NULL
                        OR OLD.runtime_retirement_context->'protected_ro'
                            = 'null'::jsonb
                      ) AND (
                        NULLIF(
                            OLD.runtime_retirement_stage_receipt
                                ->>'source_binding_sha256', ''
                        ) IS NOT NULL
                        OR OLD.runtime_retirement_stage_receipt->>'mount_id'
                            IS NOT NULL
                        OR OLD.runtime_retirement_stage_receipt->>'engage_attempt'
                            IS NOT NULL
                      )
                   OR (
                        OLD.runtime_retirement_context->'protected_ro' IS NOT NULL
                        AND OLD.runtime_retirement_context->'protected_ro'
                            <> 'null'::jsonb
                      ) AND (
                        jsonb_typeof(
                            OLD.runtime_retirement_context->'protected_ro'
                        ) IS DISTINCT FROM 'object'
                        OR COALESCE(
                            OLD.runtime_retirement_context->'protected_ro'
                                ->>'source_binding_sha256', ''
                           ) !~ '^[0-9a-f]{64}$'
                        OR OLD.runtime_retirement_stage_receipt
                                ->>'source_binding_sha256'
                            IS DISTINCT FROM OLD.runtime_retirement_context
                                ->'protected_ro'->>'source_binding_sha256'
                        OR OLD.runtime_retirement_stage_receipt->>'mount_id'
                            IS DISTINCT FROM OLD.runtime_retirement_context
                                ->'protected_ro'->>'id'
                        OR OLD.runtime_retirement_stage_receipt->>'engage_attempt'
                            IS DISTINCT FROM OLD.runtime_retirement_context
                                ->'protected_ro'->>'engage_attempt'
                        OR (
                            COALESCE(
                                OLD.runtime_retirement_stage_receipt->>'kind', ''
                            ) IN ('uploaded', 'unchanged')
                            AND (
                                jsonb_typeof(
                                    OLD.runtime_retirement_stage_receipt
                                        ->'staged_summary'
                                ) IS DISTINCT FROM 'object'
                                OR OLD.runtime_retirement_stage_receipt
                                        ->'staged_summary'
                                        ->>'source_binding_sha256'
                                    IS DISTINCT FROM OLD.runtime_retirement_context
                                        ->'protected_ro'
                                        ->>'source_binding_sha256'
                                OR OLD.runtime_retirement_stage_receipt
                                        ->'staged_summary'->'source_binding'
                                    IS DISTINCT FROM OLD.runtime_retirement_context
                                        ->'protected_ro'->'source_binding'
                            )
                        )
                        OR (
                            COALESCE(
                                OLD.runtime_retirement_stage_receipt->>'kind', ''
                            ) IN ('empty', 'never_engaged')
                            AND OLD.runtime_retirement_stage_receipt
                                    ->'staged_summary'
                                IS DISTINCT FROM 'null'::jsonb
                        )
                      ) THEN
                    RAISE EXCEPTION
                        'protected thread % staging receipt source is malformed', OLD.id
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'threads_runtime_retirement_stage_receipt_source';
                END IF;
            END IF;
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS threads_ended_transition_fence ON public.threads;
CREATE TRIGGER threads_ended_transition_fence
BEFORE UPDATE OF status, runtime_generation,
                 runtime_retirement_token, runtime_retirement_permanent,
                 runtime_retirement_started_at, runtime_retirement_authorized_at,
                 runtime_retirement_context,
                 runtime_retirement_stage_receipt,
                 runtime_retirement_local_quiescence,
                 runtime_retirement_external_cleanup,
                 runtime_authority_exposed,
                 agent_id, control_admission_agent_id, runtime_attach_token,
                 runtime_attach_abort_receipt, metadata
ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.enforce_thread_ended_transition();

-- ``agents.thread_id`` intentionally has no FK, so it can otherwise publish
-- inverse-only process authority that the thread lifecycle never sees. New
-- bindings are valid only after the thread side has installed the exact agent
-- and attach token in the same transaction (thread -> agent lock order).
CREATE OR REPLACE FUNCTION public.enforce_pinned_agent_thread_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_lane text;
    target_agent uuid;
    target_attach uuid;
    target_retirement uuid;
    target_status text;
BEGIN
    IF NEW.thread_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT execution_lane, agent_id, runtime_attach_token,
           runtime_retirement_token, status::text
      INTO target_lane, target_agent, target_attach,
           target_retirement, target_status
      FROM public.threads
     WHERE id = NEW.thread_id
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'agent % references missing thread %', NEW.id, NEW.thread_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'agents_thread_authority';
    END IF;
    IF target_lane = 'pinned'
       AND (
           target_agent IS DISTINCT FROM NEW.id
           OR target_attach IS NULL
           OR target_retirement IS NOT NULL
           OR target_status NOT IN ('created','active','awaiting_user','suspended')
       ) THEN
        RAISE EXCEPTION
            'agent % lacks reciprocal pinned thread authority for %', NEW.id, NEW.thread_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'agents_thread_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS agents_thread_authority_fence ON public.agents;
CREATE TRIGGER agents_thread_authority_fence
BEFORE INSERT OR UPDATE OF thread_id ON public.agents
FOR EACH ROW
EXECUTE FUNCTION public.enforce_pinned_agent_thread_authority();

CREATE OR REPLACE FUNCTION public.enforce_pinned_thread_agent_reciprocity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.execution_lane = 'pinned' AND (
        (
            NEW.agent_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM public.agents AS agent
                 WHERE agent.id = NEW.agent_id
                   AND agent.thread_id = NEW.id
            )
        )
        OR (
            NEW.agent_id IS NULL
            AND EXISTS (
                SELECT 1 FROM public.agents AS agent
                 WHERE agent.thread_id = NEW.id
            )
        )
    ) THEN
        RAISE EXCEPTION 'thread % has nonreciprocal agent authority', NEW.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_agent_reciprocity';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS threads_agent_reciprocity_fence ON public.threads;
CREATE CONSTRAINT TRIGGER threads_agent_reciprocity_fence
AFTER INSERT OR UPDATE ON public.threads
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION public.enforce_pinned_thread_agent_reciprocity();

CREATE OR REPLACE FUNCTION public.enforce_agent_thread_reciprocity_final()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    new_thread_id uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        new_thread_id := NULL;
    ELSE
        new_thread_id := NEW.thread_id;
    END IF;
    IF OLD.thread_id IS DISTINCT FROM new_thread_id
       AND EXISTS (
           SELECT 1 FROM public.threads AS thread
            WHERE thread.id = OLD.thread_id
              AND thread.execution_lane = 'pinned'
              AND thread.agent_id = OLD.id
       ) THEN
        RAISE EXCEPTION 'agent % left reciprocal pinned thread %', OLD.id, OLD.thread_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'agents_thread_reciprocity';
    END IF;
    IF new_thread_id IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM public.threads AS thread
            WHERE thread.id = new_thread_id
              AND thread.execution_lane = 'pinned'
              AND (
                  thread.agent_id IS DISTINCT FROM NEW.id
                  OR thread.runtime_attach_token IS NULL
              )
       ) THEN
        RAISE EXCEPTION 'agent % entered nonreciprocal pinned thread %', NEW.id, new_thread_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'agents_thread_reciprocity';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS agents_thread_reciprocity_fence ON public.agents;
CREATE CONSTRAINT TRIGGER agents_thread_reciprocity_fence
AFTER UPDATE OR DELETE ON public.agents
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION public.enforce_agent_thread_reciprocity_final();

-- DELETE does not invoke the UPDATE fence above. Keep the destructive pinned
-- edge inside the same database authority: an exposed/runtime-owned life may
-- disappear only under its exact authorized permanent retirement and a
-- tier-correct physical-zero receipt. A prior successful soft End is also a
-- durable zero proof for the same unchanged generation; Resume rotates G, so
-- that proof cannot authorize deletion of a successor. Permanent deletion
-- intentionally discards protected review and therefore does not require a
-- cloud staging receipt.
CREATE OR REPLACE FUNCTION public.enforce_pinned_thread_delete_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    retirement_context jsonb := OLD.runtime_retirement_context;
    local_quiescence jsonb := OLD.runtime_retirement_local_quiescence;
    external_cleanup jsonb := OLD.runtime_retirement_external_cleanup;
    runtime_exposed boolean := COALESCE((
        OLD.runtime_authority_exposed
        OR OLD.agent_id IS NOT NULL
        OR OLD.runtime_attach_token IS NOT NULL
        OR OLD.runtime_retirement_context->>'runtime_authority_exposed' = 'true'
        OR NULLIF(OLD.runtime_retirement_context->>'agent_id', '') IS NOT NULL
        OR NULLIF(
            OLD.runtime_retirement_context->>'runtime_attach_token', ''
        ) IS NOT NULL
        OR (
            OLD.runtime_retirement_context->'agent' IS NOT NULL
            AND OLD.runtime_retirement_context->'agent' <> 'null'::jsonb
            AND OLD.runtime_retirement_context->'agent' <> '{}'::jsonb
        )
        OR (
            OLD.runtime_retirement_context->'agent_pod' IS NOT NULL
            AND OLD.runtime_retirement_context->'agent_pod' <> 'null'::jsonb
            AND OLD.runtime_retirement_context->'agent_pod' <> '{}'::jsonb
        )
        OR (
            OLD.runtime_retirement_context->'agent_pod_provision_intent'
                IS NOT NULL
            AND OLD.runtime_retirement_context->'agent_pod_provision_intent'
                NOT IN ('null'::jsonb, '{}'::jsonb)
        )
    ), false);
    prior_soft_settlement boolean := false;
    external_resources_absent boolean := false;
    legacy_authority_absent boolean := false;
    workspace_backend text;
    expected_protocol text;
    expected_workspace_generation text := '';
    expected_workspace_runtime text := '';
BEGIN
    IF OLD.execution_lane <> 'pinned' THEN
        RETURN OLD;
    END IF;

    -- ``agents.thread_id`` has no FK back to threads. A direct DELETE must
    -- never orphan inverse-only process authority. The exact application
    -- retirement transaction clears the reciprocal row first; its deferred
    -- agent constraint then validates the transaction's final state.
    IF EXISTS (
        SELECT 1 FROM public.agents agent WHERE agent.thread_id = OLD.id
    ) THEN
        RAISE EXCEPTION
            'pinned thread % delete retains inverse agent authority', OLD.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_pinned_delete_authority';
    END IF;

    external_resources_absent := COALESCE((
        jsonb_typeof(OLD.metadata) = 'object'
        AND COALESCE(
              jsonb_typeof(OLD.metadata->'workspace_container'), 'null'
            ) IN ('object', 'null')
        AND COALESCE(
              jsonb_typeof(OLD.metadata->'_workspace_binding'), 'null'
            ) IN ('object', 'null')
        AND COALESCE(jsonb_typeof(OLD.metadata->'vm'), 'null')
              IN ('object', 'null')
        AND (
              NOT (OLD.metadata ? 'agent_pod')
              OR OLD.metadata->'agent_pod' IS NULL
              OR OLD.metadata->'agent_pod' = 'null'::jsonb
              OR OLD.metadata->'agent_pod' = '{}'::jsonb
        )
        AND COALESCE(
              OLD.metadata->'workspace_container'->>'status', ''
            ) IN ('', 'deleted')
        AND OLD.metadata->'workspace_container'->>'_runtime_incarnation' IS NULL
        AND OLD.metadata->'workspace_container'->>'_docker_workspace_lease_id'
            IS NULL
        AND OLD.metadata->'workspace_container'->>'pod_ip' IS NULL
        AND OLD.metadata->'workspace_container'->>'pod_name' IS NULL
        AND OLD.metadata->'workspace_container'->>'host' IS NULL
        AND OLD.metadata->'workspace_container'->>'port' IS NULL
        AND OLD.metadata->'workspace_container'->>'ide_host' IS NULL
        AND OLD.metadata->'workspace_container'->>'ide_port' IS NULL
        AND OLD.metadata->'workspace_container'->>'_canvas_workspace_generation'
            IS NULL
        AND (
              NOT (OLD.metadata ? '_workspace_binding')
              OR OLD.metadata->'_workspace_binding' IS NULL
              OR OLD.metadata->'_workspace_binding' = 'null'::jsonb
              OR OLD.metadata->'_workspace_binding' = '{}'::jsonb
        )
        AND COALESCE(OLD.metadata->'vm'->>'status', '') IN ('', 'deleted')
        AND OLD.metadata->'vm'->>'provision_generation' IS NULL
        AND OLD.metadata->'vm'->>'identity_provision_generation' IS NULL
        AND OLD.metadata->'vm'->>'vm_uid' IS NULL
        AND OLD.metadata->'vm'->>'_runtime_incarnation' IS NULL
        AND OLD.metadata->'vm'->>'rootdisk_pvc_uid' IS NULL
        AND OLD.metadata->'vm'->>'ssh_host' IS NULL
        AND OLD.metadata->'vm'->>'ssh_port' IS NULL
        AND OLD.metadata->'vm'->>'_canvas_workspace_generation' IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM public.cloud_ro_mounts ro
             WHERE ro.thread_id = OLD.id
               AND ro.status IN ('engaging', 'active', 'revoking')
        )
        AND NOT EXISTS (
            SELECT 1 FROM public.docker_workspace_leases lease
             WHERE lease.owner_kind = 'thread'
               AND lease.owner_id = OLD.id
               AND lease.status IN ('ready', 'releasing')
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_agent_pod_provision_intents intent
             WHERE intent.thread_id = OLD.id
               AND intent.status IN ('planned', 'revoking')
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_agent_workspace_claims claim
             WHERE claim.thread_id = OLD.id
               AND claim.status IN ('planned', 'ready', 'revoking')
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_workspace_provision_intents intent
             WHERE intent.thread_id = OLD.id
               AND intent.status IN ('planned', 'revoking')
        )
    ), false);
    legacy_authority_absent := (
        external_resources_absent
        AND public.pinned_retirement_workspace_provision_intent_retired(
            OLD.id,
            OLD.runtime_generation,
            NULL,
            false
        )
        -- The markerless exception is only for true predecessor tombstones.
        -- Any append-only authority outcome or provision record proves that
        -- this row has participated in the exact lifecycle and must take the
        -- authorized permanent path (including its external-cleanup receipt).
        -- Otherwise a direct writer could erase retained metadata after a
        -- soft settlement and relabel that modern row as a legacy tombstone.
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_runtime_retirement_outcomes outcome
             WHERE outcome.thread_id = OLD.id
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_runtime_attach_abort_outcomes outcome
             WHERE outcome.thread_id = OLD.id
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_agent_pod_provision_intents intent
             WHERE intent.thread_id = OLD.id
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_agent_workspace_claims claim
             WHERE claim.thread_id = OLD.id
        )
        AND NOT EXISTS (
            SELECT 1
              FROM public.thread_workspace_provision_intents intent
             WHERE intent.thread_id = OLD.id
        )
    );

    -- Historical ownerless ended rows that provably never exposed runtime
    -- authority remain removable during a mixed-version rollout.
    IF OLD.status = 'ended'
       AND NOT runtime_exposed
       AND OLD.agent_id IS NULL
       AND OLD.control_admission_agent_id IS NULL
       AND OLD.runtime_attach_token IS NULL
       AND OLD.runtime_retirement_token IS NULL THEN
        IF NOT legacy_authority_absent THEN
            RAISE EXCEPTION
                'pinned thread % delete retains physical runtime authority', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_pinned_delete_authority';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.runtime_retirement_token IS NULL
       OR OLD.runtime_retirement_permanent IS DISTINCT FROM true
       OR OLD.runtime_retirement_authorized_at IS NULL
       OR jsonb_typeof(retirement_context) IS DISTINCT FROM 'object'
       OR COALESCE(
            jsonb_typeof(retirement_context->'workspace_container'), 'null'
          ) NOT IN ('object', 'null')
       OR COALESCE(
            jsonb_typeof(retirement_context->'workspace_binding'), 'null'
          ) NOT IN ('object', 'null')
       OR COALESCE(
            jsonb_typeof(retirement_context->'agent_workspace_claim'), 'null'
          ) NOT IN ('object', 'null')
       OR COALESCE(jsonb_typeof(retirement_context->'vm'), 'null')
          NOT IN ('object', 'null')
       OR COALESCE(jsonb_typeof(retirement_context->'agent'), 'null')
          NOT IN ('object', 'null')
       OR COALESCE(jsonb_typeof(retirement_context->'agent_pod'), 'null')
          NOT IN ('object', 'null')
       OR COALESCE(
            jsonb_typeof(
                retirement_context->'agent_pod_provision_intent'
            ), 'null'
          ) NOT IN ('object', 'null')
       OR COALESCE(
            jsonb_typeof(
                retirement_context->'workspace_provision_intent'
            ), 'null'
          ) NOT IN ('object', 'null')
       OR COALESCE(jsonb_typeof(retirement_context->'protected_ro'), 'null')
          NOT IN ('object', 'null')
       OR (
            retirement_context->'agent' IS NOT NULL
            AND retirement_context->'agent' NOT IN ('null'::jsonb, '{}'::jsonb)
            AND (
                NULLIF(retirement_context->'agent'->>'hostname', '') IS NULL
                OR NULLIF(retirement_context->'agent'->>'pod_uid', '') IS NULL
            )
       )
       OR (
            retirement_context->'agent_pod' IS NOT NULL
            AND retirement_context->'agent_pod'
                NOT IN ('null'::jsonb, '{}'::jsonb)
            AND (
                NULLIF(retirement_context->'agent_pod'->>'pod_name', '') IS NULL
                OR NULLIF(retirement_context->'agent_pod'->>'pod_uid', '') IS NULL
            )
       )
       OR (
            retirement_context->'agent_pod_provision_intent' IS NOT NULL
            AND retirement_context->'agent_pod_provision_intent'
                NOT IN ('null'::jsonb, '{}'::jsonb)
            AND (
                NULLIF(
                    retirement_context->'agent_pod_provision_intent'
                        ->>'attempt_id', ''
                ) IS NULL
                OR NULLIF(
                    retirement_context->'agent_pod_provision_intent'
                        ->>'pod_name', ''
                ) IS NULL
                OR retirement_context->'agent_pod_provision_intent'
                    ->>'runtime_generation'
                   IS DISTINCT FROM OLD.runtime_generation::text
                OR retirement_context->'agent_pod_provision_intent'
                    ->>'provisioner' NOT IN ('agent', 'persistent')
                OR retirement_context->'agent_pod_provision_intent'
                    ->>'status' <> 'planned'
            )
       )
       OR retirement_context->>'settle_status' IS DISTINCT FROM 'ended'
       OR retirement_context->>'generation'
          IS DISTINCT FROM OLD.runtime_generation::text
       OR COALESCE(retirement_context->>'agent_id', '')
          IS DISTINCT FROM COALESCE(OLD.agent_id::text, '')
       OR COALESCE(retirement_context->>'runtime_attach_token', '')
          IS DISTINCT FROM COALESCE(OLD.runtime_attach_token::text, '')
       THEN
        RAISE EXCEPTION
            'pinned thread % delete lacks exact permanent retirement authority', OLD.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_pinned_delete_authority';
    END IF;

    prior_soft_settlement := (
        OLD.status = 'ended'
        AND OLD.agent_id IS NULL
        AND OLD.control_admission_agent_id IS NULL
        AND OLD.runtime_attach_token IS NULL
        AND (
            NOT (retirement_context ? 'agent')
            OR retirement_context->'agent' IN ('null'::jsonb, '{}'::jsonb)
        )
        AND (
            NOT (retirement_context ? 'agent_pod')
            OR retirement_context->'agent_pod' IN ('null'::jsonb, '{}'::jsonb)
        )
        AND (
            NOT (retirement_context ? 'agent_pod_provision_intent')
            OR retirement_context->'agent_pod_provision_intent'
                IN ('null'::jsonb, '{}'::jsonb)
        )
        AND EXISTS (
            SELECT 1
              FROM public.thread_runtime_retirement_outcomes outcome
             WHERE outcome.thread_id = OLD.id
               AND outcome.runtime_generation = OLD.runtime_generation
               AND outcome.disposition = 'ended'
               AND outcome.permanent = false
               AND outcome.outcome = 'settled'
        )
    );

    IF NOT public.pinned_retirement_workspace_provision_intent_retired(
        OLD.id,
        OLD.runtime_generation,
        retirement_context->'workspace_provision_intent',
        true
    ) THEN
        RAISE EXCEPTION
            'pinned thread % delete retains workspace provision authority', OLD.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_pinned_delete_authority';
    END IF;

    IF external_cleanup IS DISTINCT FROM
       public.pinned_retirement_external_cleanup_expected(
           retirement_context,
           OLD.runtime_generation,
           OLD.runtime_retirement_token
       ) THEN
        RAISE EXCEPTION
            'pinned thread % delete lacks exact external-cleanup receipt', OLD.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_pinned_delete_authority';
    END IF;

    -- Process-zero and external cleanup are separate proofs.  A local receipt
    -- or prior same-generation soft settlement can prove that writers stopped,
    -- but neither may orphan a Pod, VM/rootdisk, protected reader, or retained
    -- workspace backing.  The orchestrator exact-clears those identities only
    -- after their UID/generation-fenced actuators report absence.
    IF NOT external_resources_absent THEN
        RAISE EXCEPTION
            'pinned thread % delete retains external runtime resources', OLD.id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'threads_pinned_delete_authority';
    ELSIF runtime_exposed AND NOT prior_soft_settlement THEN
        workspace_backend := COALESCE(retirement_context->>'workspace_backend', '');
        IF retirement_context->'workspace_provision_intent'
              NOT IN ('null'::jsonb, '{}'::jsonb) THEN
            expected_protocol := 'agent_runtime_zero_v1';
            expected_workspace_generation := '';
            expected_workspace_runtime := '';
        ELSIF workspace_backend = 'sandbox' THEN
            expected_workspace_generation := COALESCE(
                retirement_context->'workspace_binding'->>'generation', ''
            );
            expected_workspace_runtime := COALESCE(
                retirement_context->'workspace_container'->>'_runtime_incarnation',
                retirement_context->'workspace_container'->>'_docker_workspace_lease_id',
                ''
            );
            IF local_quiescence->>'quiescence_actor' = 'orchestrator'
               AND local_quiescence->>'quiescence_protocol'
                   = 'sandbox_actuator_zero_v1'
               AND (
                    COALESCE(
                        retirement_context->'workspace_binding', '{}'::jsonb
                    ) <> '{}'::jsonb
                    OR COALESCE(
                        retirement_context->'workspace_container'->>'status', ''
                    ) NOT IN ('', 'deleted')
                    OR retirement_context->'workspace_container'->>'_runtime_incarnation'
                        IS NOT NULL
                    OR retirement_context->'workspace_container'->>'pod_ip'
                        IS NOT NULL
                    OR retirement_context->'workspace_container'->>'pod_name'
                        IS NOT NULL
                    OR retirement_context->'workspace_container'->>'host'
                        IS NOT NULL
                    OR retirement_context->'workspace_container'->>'port'
                        IS NOT NULL
                    OR retirement_context->'workspace_container'->>'ide_host'
                        IS NOT NULL
                    OR retirement_context->'workspace_container'->>'ide_port'
                        IS NOT NULL
                    OR retirement_context->'workspace_container'->>'_canvas_workspace_generation'
                        IS NOT NULL
               ) THEN
                expected_protocol := 'sandbox_actuator_zero_v1';
            ELSIF expected_workspace_generation <> ''
               AND expected_workspace_runtime <> '' THEN
                expected_protocol := 'workspace_process_zero_v1';
            ELSIF expected_workspace_generation = ''
                  AND expected_workspace_runtime = '' THEN
                expected_protocol := 'agent_runtime_zero_v1';
            END IF;
        ELSIF workspace_backend IN ('virtual', 'none') THEN
            expected_protocol := 'agent_runtime_zero_v1';
        ELSIF workspace_backend IN ('vm', 'remote') THEN
            expected_protocol := 'workspace_actuator_zero_v1';
            expected_workspace_generation := COALESCE(
                retirement_context->'vm'->>'provision_generation', ''
            );
            expected_workspace_runtime := COALESCE(
                retirement_context->'vm'->>'vm_uid', ''
            );
            IF expected_workspace_generation = ''
               OR expected_workspace_runtime = '' THEN
                expected_protocol := NULL;
            END IF;
        END IF;

        IF jsonb_typeof(local_quiescence) IS DISTINCT FROM 'object'
           OR local_quiescence->>'version' IS DISTINCT FROM '1'
           OR local_quiescence->>'runtime_generation'
              IS DISTINCT FROM OLD.runtime_generation::text
           OR local_quiescence->>'retirement_token'
              IS DISTINCT FROM OLD.runtime_retirement_token::text
           OR COALESCE(local_quiescence->>'agent_id', '')
              IS DISTINCT FROM COALESCE(retirement_context->>'agent_id', '')
           OR COALESCE(local_quiescence->>'runtime_attach_token', '')
              IS DISTINCT FROM COALESCE(
                    retirement_context->>'runtime_attach_token', ''
                 )
           OR (
                retirement_context->'agent_pod' IS NOT NULL
                AND retirement_context->'agent_pod'
                    NOT IN ('null'::jsonb, '{}'::jsonb)
                AND retirement_context->>'agent_id' IS NULL
                AND retirement_context->>'runtime_attach_token' IS NULL
                AND (
                    local_quiescence->>'quiescence_actor' <> 'orchestrator'
                    OR local_quiescence->>'quiescence_protocol'
                        <> 'agent_runtime_zero_v1'
                    OR local_quiescence->>'agent_pod_name' IS DISTINCT FROM
                       retirement_context->'agent_pod'->>'pod_name'
                    OR local_quiescence->>'agent_pod_uid' IS DISTINCT FROM
                       retirement_context->'agent_pod'->>'pod_uid'
                )
           )
           OR (
                retirement_context->'agent_pod_provision_intent' IS NOT NULL
                AND retirement_context->'agent_pod_provision_intent'
                    NOT IN ('null'::jsonb, '{}'::jsonb)
                AND (
                    retirement_context->>'agent_id' IS NOT NULL
                    OR retirement_context->>'runtime_attach_token' IS NOT NULL
                    OR local_quiescence->>'quiescence_actor' <> 'orchestrator'
                    OR local_quiescence->>'quiescence_protocol'
                        <> 'agent_runtime_zero_v1'
                    OR local_quiescence->>'agent_pod_provision_attempt'
                       IS DISTINCT FROM retirement_context
                        ->'agent_pod_provision_intent'->>'attempt_id'
                    OR local_quiescence->>'agent_pod_name'
                       IS DISTINCT FROM retirement_context
                        ->'agent_pod_provision_intent'->>'pod_name'
                    OR NULLIF(local_quiescence->>'agent_pod_uid', '') IS NULL
                    OR local_quiescence->>'agent_pod_fence_protocol'
                       <> 'k8s_name_tombstone_v1'
                    OR NOT EXISTS (
                        SELECT 1
                          FROM public.thread_agent_pod_provision_intents intent
                         WHERE intent.attempt_id::text = retirement_context
                                ->'agent_pod_provision_intent'->>'attempt_id'
                           AND intent.thread_id = OLD.id
                           AND intent.runtime_generation = OLD.runtime_generation
                           AND intent.pod_name = retirement_context
                                ->'agent_pod_provision_intent'->>'pod_name'
                           AND intent.status = 'fenced'
                           AND intent.pod_uid = local_quiescence
                                ->>'agent_pod_uid'
                    )
                )
           )
           OR local_quiescence->>'settle_status' IS DISTINCT FROM 'ended'
           OR expected_protocol IS NULL
           OR local_quiescence->>'quiescence_protocol'
              IS DISTINCT FROM expected_protocol
           OR COALESCE(local_quiescence->>'quiescence_actor', '')
              NOT IN ('agent', 'orchestrator')
           OR (
                expected_protocol = 'sandbox_actuator_zero_v1'
                AND local_quiescence->>'quiescence_actor' <> 'orchestrator'
           )
           OR COALESCE(local_quiescence->>'workspace_generation', '')
              IS DISTINCT FROM expected_workspace_generation
           OR COALESCE(
                local_quiescence->>'workspace_runtime_incarnation', ''
              ) IS DISTINCT FROM expected_workspace_runtime THEN
            RAISE EXCEPTION
                'pinned thread % delete lacks exact physical quiescence', OLD.id
                USING ERRCODE = '23514',
                      CONSTRAINT = 'threads_pinned_delete_authority';
        END IF;
    END IF;

    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS threads_pinned_delete_authority ON public.threads;
CREATE TRIGGER threads_pinned_delete_authority
BEFORE DELETE ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.enforce_pinned_thread_delete_authority();

COMMENT ON COLUMN public.threads.runtime_generation IS
    'Immutable authority for one pinned/stateless session runtime life. The database rotates it on the sole ended->created Resume edge.';
COMMENT ON COLUMN public.threads.runtime_retirement_token IS
    'Non-NULL closes pinned runtime admission while exact external cleanup is retryable.';
COMMENT ON COLUMN public.threads.runtime_retirement_context IS
    'Immutable identities captured when pinned retirement closes admission; cleanup must target these identities, never names alone.';
COMMENT ON COLUMN public.threads.runtime_retirement_authorized_at IS
    'Append-once preflight authorization for the pending retirement. Exact agents do not begin local teardown and reconcilers do not actuate before it is set.';
COMMENT ON COLUMN public.threads.runtime_retirement_stage_receipt IS
    'Append-once exact staging publication receipt for retrying a soft retirement after workspace/reader cleanup; cleared only by settlement.';
COMMENT ON COLUMN public.threads.runtime_retirement_local_quiescence IS
    'Append-once exact agent acknowledgement that shell, overlay, mounts, and ordinary event writers are quiesced for the pending runtime retirement.';
COMMENT ON COLUMN public.threads.runtime_authority_exposed IS
    'Monotonic within one runtime_generation: true once any pinned process authority was delivered; reset only by the database generation-rotation edge.';
COMMENT ON COLUMN public.threads.runtime_attach_token IS
    'Exact physical pinned-process attach identity; register/bind rotates it and every maintenance or credential request must match it with runtime_generation.';
COMMENT ON COLUMN public.threads.runtime_attach_abort_receipt IS
    'Last exact pre-input attach-abort rotation receipt. Internal authority only; browser thread payloads must redact it.';
COMMENT ON COLUMN public.cloud_ro_mounts.runtime_generation IS
    'Pinned session generation that minted this reader grant/staging authority.';
COMMENT ON COLUMN public.cloud_ro_mounts.engage_attempt IS
    'Exact protected-engage attempt identity; stale cleanup must match it.';
COMMENT ON COLUMN public.thread_control_requests.runtime_generation IS
    'Thread runtime generation captured by control admission. Pinned consumers must match it before applying or journalling the request.';
COMMENT ON TABLE public.thread_runtime_retirement_outcomes IS
    'Append-only exact pinned retirement settlement proof used only for lost-final-response reconciliation; contains no successor coordinates or credentials.';
COMMENT ON TABLE public.thread_runtime_attach_abort_outcomes IS
    'Append-only exact failed-attach rotation proof; lets a lost response converge without clearing or inspecting a successor binding.';
COMMENT ON FUNCTION public.enforce_thread_ended_transition() IS
    'Mixed-version lifecycle fence: ended resumes rotate generation; pending retirement blocks Resume/runtime status mutation.';
COMMENT ON FUNCTION public.enforce_pinned_thread_delete_authority() IS
    'Direct-writer fence: exposed or live pinned rows require exact authorized permanent retirement and physical quiescence before DELETE.';

COMMIT;
