-- migration:     0190_managed_repository_terminal_thread_delete_validate.sql
-- description:   Require a completed permanent retirement marker when a
--                receipt-bound terminal thread no longer carries its UID.
-- depends-on:    0189_managed_repository_terminal_thread_delete.sql
-- expected:      < 1s. Replaces one trigger function; no row scan or rewrite.
-- locks:         Function-catalog lock only; installed row triggers remain.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE OR REPLACE FUNCTION public.enforce_managed_repository_process_zero_transition()
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
        IF runtime_id IS NULL
           AND source_kind = 'thread'
           AND OLD.status::TEXT = 'ended'
           AND old_state->'_stateless_workspace_retirement_pending' = 'true'::JSONB
           AND old_state #> '{_stateless_claim_retirement,permanent}' = 'true'::JSONB
           AND old_state #> '{_stateless_claim_retirement,claimant_quiesced}' = 'true'::JSONB
           AND (
               old_state #> '{_stateless_claim_retirement,resident_cleanup_required}'
                   = 'false'::JSONB
               OR old_state #> '{_stateless_claim_retirement,residents_retired}'
                   = 'true'::JSONB
           )
           AND (
               old_state #> '{_stateless_claim_retirement,shell_retirement_required}'
                   = 'false'::JSONB
               OR old_state #> '{_stateless_claim_retirement,remote_retired}'
                   = 'true'::JSONB
           )
           AND old_workspace->>'status' IN ('deleted', 'released')
        THEN
            runtime_id := NULLIF(
                old_state #>> '{_stateless_claim_retirement,runtime_incarnation}',
                ''
            );
        END IF;
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
            OR (
                runtime_id IS NOT NULL
                AND new_workspace->>'_runtime_incarnation'
                    IS DISTINCT FROM runtime_id
            )
            OR (
                runtime_id IS NULL
                AND old_workspace <> '{}'::JSONB
                AND (
                    new_workspace = '{}'::JSONB
                    OR new_workspace->>'provisioner'
                        IS DISTINCT FROM old_workspace->>'provisioner'
                )
            )
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
            OR (
                runtime_id IS NOT NULL
                AND new_workspace->>'_docker_workspace_lease_id'
                    IS DISTINCT FROM runtime_id
            )
            OR (
                runtime_id IS NULL
                AND old_workspace <> '{}'::JSONB
                AND (
                    new_workspace = '{}'::JSONB
                    OR new_workspace->>'provisioner'
                        IS DISTINCT FROM old_workspace->>'provisioner'
                )
            )
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
        OR (
            runtime_id IS NOT NULL
            AND new_vm->>'provision_generation' IS DISTINCT FROM runtime_id
        )
        OR (
            runtime_id IS NULL
            AND old_vm <> '{}'::JSONB
            AND new_vm = '{}'::JSONB
        )
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
            OR (
                runtime_id IS NOT NULL
                AND new_ide->>'_runtime_incarnation'
                    IS DISTINCT FROM runtime_id
            )
            OR (
                old_ide->>'container_id' IS NOT NULL
                AND new_ide->>'container_id'
                    IS DISTINCT FROM old_ide->>'container_id'
            )
            OR (
                runtime_id IS NULL
                AND old_ide->>'container_id' IS NULL
                AND old_ide <> '{}'::JSONB
                AND new_ide = '{}'::JSONB
            )
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

COMMIT;


