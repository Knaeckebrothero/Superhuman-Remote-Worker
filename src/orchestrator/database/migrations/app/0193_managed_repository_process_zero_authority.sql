-- migration:     0193_managed_repository_process_zero_authority.sql
-- description:   Persist exact workspace process-zero observations outside
--                caller-writable job and thread JSON.
-- depends-on:    0192_stateless_input_delivery_validate.sql
-- expected:      < 1s. One empty table plus two JSON guard triggers; no
--                historical scan or row rewrite.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on jobs and threads for
--                trigger installation; ACCESS EXCLUSIVE only on new objects.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE public.managed_repository_process_zero_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_kind TEXT NOT NULL,
    owner_id UUID NOT NULL,
    scope TEXT NOT NULL,
    provisioner TEXT NOT NULL,
    runtime_incarnation TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT managed_repository_process_zero_identity_unique
        UNIQUE (owner_kind, owner_id, scope, runtime_incarnation),
    CONSTRAINT managed_repository_process_zero_owner_kind_check
        CHECK (owner_kind IN ('job', 'thread')),
    CONSTRAINT managed_repository_process_zero_scope_check
        CHECK (scope IN (
            'workspace_container',
            'vm',
            'ide',
            'ide_local',
            'stateless_workspace',
            'docker_workspace'
        )),
    CONSTRAINT managed_repository_process_zero_provisioner_check
        CHECK (
            (scope = 'workspace_container' AND provisioner = 'k8s')
            OR (scope = 'vm' AND provisioner = 'vm')
            OR (scope = 'ide' AND provisioner = 'k8s')
            OR (scope = 'ide_local' AND provisioner = 'docker')
            OR (scope = 'stateless_workspace' AND provisioner = 'k8s')
            OR (scope = 'docker_workspace' AND provisioner = 'docker')
        ),
    CONSTRAINT managed_repository_process_zero_runtime_check
        CHECK (
            (
                scope <> 'ide_local'
                AND runtime_incarnation ~
                    '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
            )
            OR (
                scope = 'ide_local'
                AND runtime_incarnation ~ '^[0-9a-f]{64}$'
            )
        )
);

COMMENT ON TABLE public.managed_repository_process_zero_receipts IS
    'Server-owned exact-runtime evidence that managed repository ssh-agent processes reached zero before destructive workspace teardown.';

CREATE FUNCTION public.managed_repository_process_zero_receipt_exists(
    requested_owner_kind TEXT,
    requested_owner_id UUID,
    requested_scope TEXT,
    requested_provisioner TEXT,
    requested_runtime TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    IF requested_runtime IS NULL
       OR (
           requested_scope = 'ide_local'
           AND requested_runtime !~ '^[0-9a-f]{64}$'
       )
       OR (
           requested_scope <> 'ide_local'
           AND requested_runtime !~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       ) THEN
        RETURN FALSE;
    END IF;
    RETURN EXISTS (
        SELECT 1
          FROM public.managed_repository_process_zero_receipts AS receipt
         WHERE receipt.owner_kind = requested_owner_kind
           AND receipt.owner_id = requested_owner_id
           AND receipt.scope = requested_scope
           AND receipt.provisioner = requested_provisioner
           AND receipt.runtime_incarnation = requested_runtime
    );
END;
$$;

CREATE FUNCTION public.enforce_managed_repository_process_zero_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    source_kind TEXT;
    source_id UUID;
    old_state JSONB;
    new_state JSONB;
    old_workspace JSONB;
    new_workspace JSONB;
    old_vm JSONB;
    new_vm JSONB;
    old_ide JSONB;
    new_ide JSONB;
    parent_state JSONB := '{}'::JSONB;
    parent_workspace JSONB := '{}'::JSONB;
    parent_vm JSONB := '{}'::JSONB;
    parent_ide JSONB := '{}'::JSONB;
    parent_id UUID;
    runtime_id TEXT;
    receipt_ok BOOLEAN;
    declared_inherited BOOLEAN := FALSE;
    inherited_scope BOOLEAN := FALSE;
    destructive_transition BOOLEAN;
BEGIN
    IF TG_TABLE_NAME = 'jobs' THEN
        source_kind := 'job';
        source_id := OLD.id;
        parent_id := OLD.parent_job_id;
        old_state := COALESCE(OLD.context, '{}'::JSONB);
        new_state := CASE WHEN TG_OP = 'DELETE'
                          THEN '{}'::JSONB
                          ELSE COALESCE(NEW.context, '{}'::JSONB) END;
        -- Inherited subjobs carry a diagnostic copy of their parent's runtime,
        -- but never own that compute namespace.  The pre-0175 writer did not
        -- stamp a workspace contract, so absence is accepted only with the
        -- relational edge plus the exact parent runtime below.  A present,
        -- contradictory contract always fails closed.
        declared_inherited := parent_id IS NOT NULL
            AND old_state->>'inherits_parent_workspace' = 'true'
            AND (
                NOT (old_state ? '_workspace_contract')
                OR old_state #>> '{_workspace_contract,assignment_source}'
                    = 'parent_inheritance'
            );
        IF declared_inherited THEN
            SELECT COALESCE(parent.context, '{}'::JSONB)
              INTO parent_state
              FROM public.jobs AS parent
             WHERE parent.id = parent_id;
            parent_state := COALESCE(parent_state, '{}'::JSONB);
            parent_workspace := COALESCE(
                parent_state->'workspace_container', '{}'::JSONB
            );
            parent_vm := COALESCE(parent_state->'vm', '{}'::JSONB);
            parent_ide := COALESCE(parent_state->'ide_session', '{}'::JSONB);
        END IF;
    ELSE
        source_kind := 'thread';
        source_id := OLD.id;
        old_state := COALESCE(OLD.metadata, '{}'::JSONB);
        new_state := CASE WHEN TG_OP = 'DELETE'
                          THEN '{}'::JSONB
                          ELSE COALESCE(NEW.metadata, '{}'::JSONB) END;
    END IF;

    old_workspace := COALESCE(old_state->'workspace_container', '{}'::JSONB);
    new_workspace := COALESCE(new_state->'workspace_container', '{}'::JSONB);
    inherited_scope := declared_inherited
        AND old_workspace <> '{}'::JSONB
        AND old_workspace->>'provisioner' = parent_workspace->>'provisioner'
        AND (
            (
                old_workspace->>'_runtime_incarnation' IS NOT NULL
                AND old_workspace->>'_runtime_incarnation'
                    = parent_workspace->>'_runtime_incarnation'
            )
            OR (
                old_workspace->>'_docker_workspace_lease_id' IS NOT NULL
                AND old_workspace->>'_docker_workspace_lease_id'
                    = parent_workspace->>'_docker_workspace_lease_id'
            )
            OR (
                old_workspace->>'_runtime_incarnation' IS NULL
                AND old_workspace->>'_docker_workspace_lease_id' IS NULL
                AND old_workspace = parent_workspace
            )
            OR public.managed_repository_process_zero_receipt_exists(
                'job',
                parent_id,
                CASE
                    WHEN old_workspace->>'provisioner' = 'docker'
                    THEN 'docker_workspace'
                    ELSE 'workspace_container'
                END,
                old_workspace->>'provisioner',
                COALESCE(
                    old_workspace->>'_docker_workspace_lease_id',
                    old_workspace->>'_runtime_incarnation'
                )
            )
        );
    IF inherited_scope THEN
        old_workspace := '{}'::JSONB;
        new_workspace := '{}'::JSONB;
    END IF;
    IF old_workspace->>'provisioner' = 'k8s' THEN
        runtime_id := old_workspace->>'_runtime_incarnation';
        receipt_ok := runtime_id IS NOT NULL AND (
            public.managed_repository_process_zero_receipt_exists(
                source_kind, source_id, 'workspace_container', 'k8s', runtime_id
            )
            OR (
                source_kind = 'thread'
                AND public.managed_repository_process_zero_receipt_exists(
                    source_kind,
                    source_id,
                    'stateless_workspace',
                    'k8s',
                    runtime_id
                )
            )
        );
        IF (receipt_ok OR old_workspace->>'status' = 'retiring_process_zero')
           AND new_workspace->>'_runtime_incarnation' = runtime_id
           AND COALESCE(new_workspace->>'status', '') NOT IN (
               'retiring_process_zero', 'deleted', 'suspended', 'released',
               'quarantined'
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_workspace_retirement_is_absorbing',
                MESSAGE = 'A process-zero workspace runtime may not be reactivated';
        END IF;
        destructive_transition := (
            TG_OP = 'DELETE'
            OR new_workspace->>'_runtime_incarnation' IS DISTINCT FROM runtime_id
            OR (
                new_workspace->>'status' IN (
                    'deleted', 'suspended', 'released', 'quarantined'
                )
                AND new_workspace->>'status'
                    IS DISTINCT FROM old_workspace->>'status'
            )
        );
        IF destructive_transition AND runtime_id IS NULL
           AND old_workspace <> '{}'::JSONB THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_workspace_runtime_identity_required',
                MESSAGE = 'Workspace runtime identity is required before destructive teardown';
        ELSIF destructive_transition AND runtime_id IS NOT NULL THEN
            IF NOT receipt_ok THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'managed_repository_workspace_process_zero_required',
                    MESSAGE = 'Workspace process-zero authority is required';
            END IF;
        END IF;
    ELSIF old_workspace->>'provisioner' = 'docker' THEN
        runtime_id := old_workspace->>'_docker_workspace_lease_id';
        receipt_ok := runtime_id IS NOT NULL
            AND public.managed_repository_process_zero_receipt_exists(
                source_kind,
                source_id,
                'docker_workspace',
                'docker',
                runtime_id
            );
        IF (receipt_ok OR old_workspace->>'status' = 'releasing')
           AND new_workspace->>'_docker_workspace_lease_id' = runtime_id
           AND COALESCE(new_workspace->>'status', '') NOT IN (
               'releasing', 'released', 'quarantined'
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_docker_retirement_is_absorbing',
                MESSAGE = 'A process-zero Docker lease may not be reactivated';
        END IF;
        destructive_transition := (
            TG_OP = 'DELETE'
            OR new_workspace->>'_docker_workspace_lease_id'
                IS DISTINCT FROM runtime_id
            OR (
                new_workspace->>'status' = 'released'
                AND new_workspace->>'status' IS DISTINCT FROM old_workspace->>'status'
            )
            OR (
                new_workspace->>'quarantine_reason' =
                    'container_recreation_required_process_zero'
                AND new_workspace->>'quarantine_reason'
                    IS DISTINCT FROM old_workspace->>'quarantine_reason'
            )
        );
        IF destructive_transition AND runtime_id IS NULL
           AND old_workspace <> '{}'::JSONB THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_docker_runtime_identity_required',
                MESSAGE = 'Docker workspace lease identity is required before destructive teardown';
        ELSIF destructive_transition AND runtime_id IS NOT NULL
           AND NOT receipt_ok THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_docker_process_zero_required',
                MESSAGE = 'Docker workspace process-zero authority is required';
        END IF;
    ELSIF old_workspace <> '{}'::JSONB
       -- A status-only placeholder has no process, endpoint, lease, or
       -- repository authority to retire.  Sanitized untrusted creation paths
       -- legitimately move that placeholder between pending/ready states.
       AND old_workspace - 'status' <> '{}'::JSONB
       AND (
           TG_OP = 'DELETE'
           OR new_workspace IS DISTINCT FROM old_workspace
       )
       AND NOT (
           -- A pre-0175 persistent thread can lack a provisioner stamp while
           -- still carrying its historical credential-bearing repository
           -- URL.  Managed-authority adoption must be able to perform the
           -- exact authority-reducing URL scrub before the replacement agent
           -- binds.  Migration 0176 independently requires the matching
           -- active write authority; this exception permits only removal of
           -- userinfo (and its transient pending marker), never a workspace
           -- runtime mutation.
           TG_OP = 'UPDATE'
           AND source_kind = 'thread'
           AND (
               old_workspace
                   - 'git_remote_url'
                   - '_managed_repository_authority_pending'
           ) = (
               new_workspace
                   - 'git_remote_url'
                   - '_managed_repository_authority_pending'
           )
           AND new_workspace->>'git_remote_url' IS NOT NULL
           AND NOT public.managed_repository_url_has_userinfo(
               new_workspace->>'git_remote_url'
           )
           AND (
               public.managed_repository_url_has_userinfo(
                   old_workspace->>'git_remote_url'
               )
               OR (
                   old_workspace->'_managed_repository_authority_pending'
                       = 'true'::JSONB
                   AND NOT (
                       new_workspace
                           ? '_managed_repository_authority_pending'
                   )
                   AND old_workspace->>'git_remote_url'
                       IS NOT DISTINCT FROM
                       new_workspace->>'git_remote_url'
               )
           )
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_workspace_provisioner_required',
            MESSAGE = 'Workspace provisioner authority is required before mutation or teardown';
    END IF;

    old_vm := COALESCE(old_state->'vm', '{}'::JSONB);
    new_vm := COALESCE(new_state->'vm', '{}'::JSONB);
    inherited_scope := declared_inherited
        AND old_vm <> '{}'::JSONB
        AND (
            (
                old_vm->>'provision_generation' IS NOT NULL
                AND (
                    old_vm->>'provision_generation'
                        = parent_vm->>'provision_generation'
                    OR public.managed_repository_process_zero_receipt_exists(
                        'job', parent_id, 'vm', 'vm',
                        old_vm->>'provision_generation'
                    )
                )
            )
            OR (
                old_vm->>'provision_generation' IS NULL
                AND old_vm = parent_vm
            )
        );
    IF inherited_scope THEN
        old_vm := '{}'::JSONB;
        new_vm := '{}'::JSONB;
    END IF;
    runtime_id := old_vm->>'provision_generation';
    receipt_ok := runtime_id IS NOT NULL
        AND public.managed_repository_process_zero_receipt_exists(
            source_kind, source_id, 'vm', 'vm', runtime_id
        );
    IF (receipt_ok OR old_vm->>'status' = 'retiring_process_zero')
       AND new_vm->>'provision_generation' = runtime_id
       AND COALESCE(new_vm->>'status', '') NOT IN (
           'retiring_process_zero', 'aborted', 'deleted', 'suspended', 'released'
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_vm_retirement_is_absorbing',
            MESSAGE = 'A process-zero VM runtime may not be reactivated';
    END IF;
    destructive_transition := (
        TG_OP = 'DELETE'
        OR new_vm->>'provision_generation' IS DISTINCT FROM runtime_id
        OR (
            new_vm->>'status' IN (
                'aborted', 'deleted', 'suspended', 'released'
            )
            AND new_vm->>'status' IS DISTINCT FROM old_vm->>'status'
        )
    );
    IF destructive_transition AND runtime_id IS NULL
       AND old_vm <> '{}'::JSONB
    THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_vm_runtime_identity_required',
            MESSAGE = 'VM runtime identity is required before destructive teardown';
    ELSIF destructive_transition AND runtime_id IS NOT NULL
       AND NOT receipt_ok THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_vm_process_zero_required',
            MESSAGE = 'VM process-zero authority is required';
    END IF;

    IF source_kind = 'job' THEN
        old_ide := COALESCE(old_state->'ide_session', '{}'::JSONB);
        new_ide := COALESCE(new_state->'ide_session', '{}'::JSONB);
        inherited_scope := declared_inherited
            AND old_ide <> '{}'::JSONB
            AND (
                (
                    old_ide->>'_runtime_incarnation' IS NOT NULL
                    AND old_ide->>'_runtime_incarnation'
                        = parent_ide->>'_runtime_incarnation'
                )
                OR (
                    old_ide->>'container_id' IS NOT NULL
                    AND old_ide->>'container_id' = parent_ide->>'container_id'
                )
            );
        IF inherited_scope THEN
            old_ide := '{}'::JSONB;
            new_ide := '{}'::JSONB;
        END IF;
        runtime_id := old_ide->>'_runtime_incarnation';
        receipt_ok := (
            runtime_id IS NOT NULL
            AND public.managed_repository_process_zero_receipt_exists(
                source_kind, source_id, 'ide', 'k8s', runtime_id
            )
        ) OR (
            runtime_id IS NULL
            AND old_ide->>'container_id' IS NOT NULL
            AND public.managed_repository_process_zero_receipt_exists(
                source_kind,
                source_id,
                'ide_local',
                'docker',
                old_ide->>'container_id'
            )
        );
        IF (receipt_ok OR old_ide->>'status' IN (
               'cleanup_pending', 'retiring_process_zero'
           ))
           AND COALESCE(
               new_ide->>'_runtime_incarnation', new_ide->>'container_id'
           ) = COALESCE(
               runtime_id, old_ide->>'container_id'
           )
           AND COALESCE(new_ide->>'status', '') NOT IN (
               'cleanup_pending', 'retiring_process_zero', 'expired', 'deleted'
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_ide_retirement_is_absorbing',
                MESSAGE = 'A process-zero IDE runtime may not be reactivated';
        END IF;
        destructive_transition := (
            TG_OP = 'DELETE'
            OR new_ide->>'_runtime_incarnation' IS DISTINCT FROM runtime_id
            OR new_ide->>'container_id'
                IS DISTINCT FROM old_ide->>'container_id'
            OR (
                new_ide->>'status' IN ('expired', 'deleted')
                AND new_ide->>'status' IS DISTINCT FROM old_ide->>'status'
            )
        );
        IF destructive_transition AND runtime_id IS NULL
           AND old_ide->>'container_id' IS NOT NULL THEN
            runtime_id := old_ide->>'container_id';
            IF NOT receipt_ok THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'managed_repository_ide_local_process_zero_required',
                    MESSAGE = 'Local IDE process-zero authority is required';
            END IF;
        ELSIF destructive_transition AND runtime_id IS NULL
           AND old_ide <> '{}'::JSONB THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_ide_runtime_identity_required',
                MESSAGE = 'IDE runtime identity is required before destructive teardown';
        ELSIF destructive_transition AND runtime_id IS NOT NULL
           AND NOT receipt_ok THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_ide_process_zero_required',
                MESSAGE = 'IDE process-zero authority is required';
        END IF;
    END IF;

    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE FUNCTION public.enforce_docker_workspace_reuse_process_zero()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF (
        NEW.status = 'released'
        AND NEW.status IS DISTINCT FROM OLD.status
    ) OR (
        NEW.status = 'quarantined'
        AND NEW.quarantine_reason =
            'container_recreation_required_process_zero'
        AND (
            NEW.status IS DISTINCT FROM OLD.status
            OR NEW.quarantine_reason IS DISTINCT FROM OLD.quarantine_reason
        )
    ) THEN
        IF OLD.owner_kind IS NULL
           OR OLD.owner_id IS NULL
           OR OLD.lease_id IS NULL
           OR NOT public.managed_repository_process_zero_receipt_exists(
               OLD.owner_kind,
               OLD.owner_id,
               'docker_workspace',
               'docker',
               OLD.lease_id::TEXT
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'docker_workspace_reuse_requires_process_zero',
                MESSAGE = 'Docker workspace reuse requires process-zero authority';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.reject_managed_repository_process_zero_json()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'jobs' THEN
        IF NEW.context ? '_managed_repository_process_zero'
           AND (
               TG_OP = 'INSERT'
               OR NOT (OLD.context ? '_managed_repository_process_zero')
               OR OLD.context->'_managed_repository_process_zero'
                  IS DISTINCT FROM
                  NEW.context->'_managed_repository_process_zero'
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_process_zero_is_server_owned',
                MESSAGE = 'Managed repository process-zero evidence is server-owned';
        END IF;
    ELSIF (
        NEW.metadata ? '_managed_repository_process_zero'
        AND (
            TG_OP = 'INSERT'
            OR NOT (OLD.metadata ? '_managed_repository_process_zero')
            OR OLD.metadata->'_managed_repository_process_zero'
               IS DISTINCT FROM
               NEW.metadata->'_managed_repository_process_zero'
        )
    ) OR (
        NEW.metadata ? '_stateless_workspace_process_zero_observation'
        AND (
            TG_OP = 'INSERT'
            OR NOT (OLD.metadata ? '_stateless_workspace_process_zero_observation')
            OR OLD.metadata->'_stateless_workspace_process_zero_observation'
               IS DISTINCT FROM
               NEW.metadata->'_stateless_workspace_process_zero_observation'
        )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'managed_repository_process_zero_is_server_owned',
            MESSAGE = 'Managed repository process-zero evidence is server-owned';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_jobs_reject_managed_repository_process_zero_json
BEFORE INSERT OR UPDATE OF context ON public.jobs
FOR EACH ROW
EXECUTE FUNCTION public.reject_managed_repository_process_zero_json();

CREATE TRIGGER trg_threads_reject_managed_repository_process_zero_json
BEFORE INSERT OR UPDATE OF metadata ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.reject_managed_repository_process_zero_json();

CREATE TRIGGER trg_jobs_enforce_managed_repository_process_zero
BEFORE UPDATE OF context OR DELETE ON public.jobs
FOR EACH ROW
EXECUTE FUNCTION public.enforce_managed_repository_process_zero_transition();

CREATE TRIGGER trg_threads_enforce_managed_repository_process_zero
BEFORE UPDATE OF metadata OR DELETE ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.enforce_managed_repository_process_zero_transition();

CREATE TRIGGER trg_docker_workspace_reuse_requires_process_zero
BEFORE UPDATE OF status, owner_kind, owner_id, lease_id, quarantine_reason
ON public.docker_workspace_leases
FOR EACH ROW
EXECUTE FUNCTION public.enforce_docker_workspace_reuse_process_zero();

COMMIT;
