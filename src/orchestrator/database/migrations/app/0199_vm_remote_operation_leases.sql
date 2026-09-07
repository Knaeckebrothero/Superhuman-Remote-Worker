-- migration:     0199_vm_remote_operation_leases.sql
-- description:   Persist exact VM remote-I/O leases and fence lifecycle
--                replacement while an admitted operation is in flight.
-- depends-on:    0198_non_pinned_workspace_lifecycle_authority.sql
-- expected:      < 5s. Two empty/singleton authority tables plus four owner
--                triggers; no historical scan or owner-row rewrite.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on jobs and threads for
--                trigger installation; ACCESS EXCLUSIVE only on new objects.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE SEQUENCE public.vm_remote_operation_claim_seq;

-- A migration applying is not permission to begin remote I/O.  The protocol
-- ships dark and is activated only by a v1-aware replica after every serving
-- replica has converged.  Activation is deliberately monotonic: once external
-- effects depend on v1 receipts, rolling back to a pre-v1 image is unsafe.
CREATE TABLE public.vm_remote_operation_protocol_gate (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    protocol_version INTEGER NOT NULL CHECK (protocol_version > 0),
    activated_at TIMESTAMPTZ,
    activated_by TEXT,
    CONSTRAINT vm_remote_operation_protocol_activation_shape CHECK (
        (activated_at IS NULL AND activated_by IS NULL)
        OR (
            activated_at IS NOT NULL
            AND length(activated_by) BETWEEN 1 AND 256
            AND activated_by = btrim(activated_by)
        )
    )
);

INSERT INTO public.vm_remote_operation_protocol_gate (
    singleton, protocol_version, activated_at, activated_by
) VALUES (TRUE, 1, NULL, NULL);

CREATE TABLE public.vm_remote_operation_leases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_kind TEXT NOT NULL,
    owner_id UUID NOT NULL,
    operation_kind TEXT NOT NULL,
    protocol_version INTEGER NOT NULL,
    workspace_tier TEXT NOT NULL,
    workspace_contract_digest TEXT NOT NULL,
    workspace_generation UUID NOT NULL,
    vm_uid TEXT NOT NULL,
    launcher_pod_uid UUID NOT NULL,
    ssh_host TEXT NOT NULL,
    ssh_port INTEGER NOT NULL,
    ssh_host_key_fingerprint TEXT NOT NULL,
    claim_token BIGINT NOT NULL DEFAULT nextval(
        'public.vm_remote_operation_claim_seq'
    ),
    claimed_by TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_renewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ,
    result_kind TEXT,
    CONSTRAINT vm_remote_operation_owner_kind_check
        CHECK (owner_kind IN ('job', 'thread')),
    CONSTRAINT vm_remote_operation_kind_check
        CHECK (operation_kind IN (
            'cloud_stage', 'ide_settings', 'ide_profile',
            'snapshot_capture', 'thread_upload', 'thread_delete'
        )),
    CONSTRAINT vm_remote_operation_protocol_version_check
        CHECK (protocol_version = 1),
    CONSTRAINT vm_remote_operation_workspace_contract_check
        CHECK (
            workspace_tier = 'vm'
            AND workspace_contract_digest ~ '^[0-9a-f]{64}$'
        ),
    CONSTRAINT vm_remote_operation_identity_check
        CHECK (
            length(vm_uid) BETWEEN 1 AND 256
            AND vm_uid = btrim(vm_uid)
            AND vm_uid !~ '[[:space:]]'
            AND length(ssh_host) BETWEEN 1 AND 512
            AND ssh_host = btrim(ssh_host)
            AND ssh_host !~ '[[:space:]]'
            AND ssh_port BETWEEN 1 AND 65535
            AND ssh_host_key_fingerprint ~ '^SHA256:[A-Za-z0-9+/]{43}$'
        ),
    CONSTRAINT vm_remote_operation_claim_check
        CHECK (
            claim_token > 0
            AND length(claimed_by) BETWEEN 1 AND 256
            AND lease_expires_at > claimed_at
            AND attempts > 0
        ),
    CONSTRAINT vm_remote_operation_result_check
        CHECK (
            (settled_at IS NULL AND result_kind IS NULL)
            OR (
                settled_at IS NOT NULL
                AND result_kind IN ('succeeded', 'failed', 'replaced', 'abandoned')
            )
        )
);

-- Remote operations share one guest filesystem.  A cloud-stage tar, IDE
-- profile capture, upload, and delete cannot safely overlap merely because
-- their audit kinds differ (an unlink can otherwise race an open SFTP write).
CREATE UNIQUE INDEX vm_remote_operation_one_active_owner
    ON public.vm_remote_operation_leases (
        owner_kind, owner_id
    ) WHERE settled_at IS NULL;

CREATE INDEX vm_remote_operation_expiry
    ON public.vm_remote_operation_leases (lease_expires_at, id)
    WHERE settled_at IS NULL;

COMMENT ON TABLE public.vm_remote_operation_leases IS
    'Server-owned exact VM and selected-workspace-contract receipts. A renewable claim spans every remote byte and blocks lifecycle or tier replacement until settlement or database-time expiry.';
COMMENT ON TABLE public.vm_remote_operation_protocol_gate IS
    'Default-dark, monotonic rollout boundary for orchestrator-originated VM SSH/SFTP. Activate v1 only after every serving replica and workspace NetworkPolicy carries the v1 capability; never roll back to a pre-v1 image after activation.';

CREATE FUNCTION public.prevent_vm_remote_operation_protocol_rollback()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
        RAISE EXCEPTION USING
            ERRCODE = '55006',
            CONSTRAINT = 'vm_remote_operation_protocol_is_forward_only',
            MESSAGE = 'VM remote-operation protocol authority is forward-only';
    END IF;
    IF NEW.protocol_version IS DISTINCT FROM OLD.protocol_version
       OR (
            OLD.activated_at IS NOT NULL
            AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD)
       )
       OR (
            OLD.activated_at IS NULL
            AND NEW.activated_at IS NULL
            AND NEW.activated_by IS NOT NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55006',
            CONSTRAINT = 'vm_remote_operation_protocol_is_forward_only',
            MESSAGE = 'VM remote-operation protocol authority is forward-only';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_vm_remote_operation_protocol_forward_only
BEFORE UPDATE OR DELETE ON public.vm_remote_operation_protocol_gate
FOR EACH ROW
EXECUTE FUNCTION public.prevent_vm_remote_operation_protocol_rollback();

CREATE TRIGGER trg_vm_remote_operation_protocol_no_truncate
BEFORE TRUNCATE ON public.vm_remote_operation_protocol_gate
FOR EACH STATEMENT
EXECUTE FUNCTION public.prevent_vm_remote_operation_protocol_rollback();

CREATE FUNCTION public.vm_remote_identity_envelope(value JSONB)
RETURNS JSONB LANGUAGE SQL IMMUTABLE AS $$
    SELECT jsonb_build_object(
        'status', value #> '{vm,status}',
        'provision_generation', value #> '{vm,provision_generation}',
        'vm_uid', value #> '{vm,vm_uid}',
        'active_pod_uid', value #> '{vm,active_pod_uid}',
        'ssh_host', value #> '{vm,ssh_host}',
        'ssh_port', value #> '{vm,ssh_port}',
        'ssh_host_key_fingerprint', value #> '{vm,ssh_host_key_fingerprint}',
        'ssh_registration_id', value #> '{vm,ssh_registration_id}',
        'identity_authenticated', value #> '{vm,identity_authenticated}',
        'identity_provision_generation',
            value #> '{vm,identity_provision_generation}'
    );
$$;

-- This is intentionally a conservative, coordinate-free owner envelope. The
-- application derives the compact digest with the canonical shared workspace
-- resolver while holding the owner lock. The trigger separately prevents an
-- old/direct writer from changing any input to that resolver while a receipt
-- is live, including a VM -> sandbox transition that leaves stale vm residue.
CREATE FUNCTION public.vm_remote_workspace_contract_envelope(
    value JSONB,
    config_override JSONB
)
RETURNS JSONB LANGUAGE SQL IMMUTABLE AS $$
    SELECT jsonb_build_object(
        'contract', value -> '_workspace_contract',
        'legacy_workspace_backend', value -> 'workspace_backend',
        'vm_requested', value #> '{vm,requested}',
        'workspace_config', CASE
            WHEN jsonb_typeof(config_override -> 'workspace') = 'object'
            THEN (config_override -> 'workspace') - 'remote'
            ELSE NULL
        END,
        'non_object_config', CASE
            WHEN jsonb_typeof(config_override) = 'object' THEN NULL
            ELSE config_override
        END
    );
$$;

CREATE FUNCTION public.prevent_active_vm_remote_operation_rebind()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    owner_kind_value TEXT;
    old_state JSONB;
    new_state JSONB;
    old_config_override JSONB;
    new_config_override JSONB;
    new_status TEXT;
    terminal_transition BOOLEAN;
BEGIN
    owner_kind_value := CASE WHEN TG_TABLE_NAME = 'jobs' THEN 'job' ELSE 'thread' END;
    old_state := CASE WHEN TG_TABLE_NAME = 'jobs'
        THEN COALESCE(to_jsonb(OLD) -> 'context', '{}'::JSONB)
        ELSE COALESCE(to_jsonb(OLD) -> 'metadata', '{}'::JSONB)
    END;
    old_config_override := CASE WHEN TG_TABLE_NAME = 'jobs'
        THEN COALESCE(to_jsonb(OLD) -> 'config_override', '{}'::JSONB)
        ELSE COALESCE(old_state -> 'config_override', '{}'::JSONB)
    END;
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1
              FROM public.vm_remote_operation_leases AS lease
             WHERE lease.owner_kind = owner_kind_value
               AND lease.owner_id = OLD.id
               AND lease.settled_at IS NULL
               AND lease.lease_expires_at > now()
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '55006',
                CONSTRAINT = 'active_vm_remote_operation_rebind',
                MESSAGE = 'VM owner is leased by an active remote operation';
        END IF;
        RETURN OLD;
    END IF;
    new_state := CASE WHEN TG_TABLE_NAME = 'jobs'
        THEN COALESCE(to_jsonb(NEW) -> 'context', '{}'::JSONB)
        ELSE COALESCE(to_jsonb(NEW) -> 'metadata', '{}'::JSONB)
    END;
    new_config_override := CASE WHEN TG_TABLE_NAME = 'jobs'
        THEN COALESCE(to_jsonb(NEW) -> 'config_override', '{}'::JSONB)
        ELSE COALESCE(new_state -> 'config_override', '{}'::JSONB)
    END;
    new_status := COALESCE(to_jsonb(NEW) ->> 'status', '');
    terminal_transition := CASE WHEN owner_kind_value = 'job'
        THEN new_status IN ('completed', 'failed', 'cancelled')
        ELSE new_status = 'ended'
    END;
    IF public.vm_remote_identity_envelope(old_state)
           IS NOT DISTINCT FROM public.vm_remote_identity_envelope(new_state)
       AND public.vm_remote_workspace_contract_envelope(
               old_state, old_config_override
           ) IS NOT DISTINCT FROM public.vm_remote_workspace_contract_envelope(
               new_state, new_config_override
           )
       AND NOT terminal_transition THEN
        RETURN NEW;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM public.vm_remote_operation_leases AS lease
         WHERE lease.owner_kind = owner_kind_value
           AND lease.owner_id = OLD.id
           AND lease.settled_at IS NULL
           AND lease.lease_expires_at > now()
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55006',
            CONSTRAINT = 'active_vm_remote_operation_rebind',
            MESSAGE = 'VM lifecycle identity is leased by an active remote operation';
    END IF;
    RETURN NEW;
END;
$$;

LOCK TABLE public.jobs IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE public.threads IN SHARE ROW EXCLUSIVE MODE;

CREATE TRIGGER trg_jobs_prevent_active_vm_remote_operation_rebind
BEFORE UPDATE OF context, config_override, status ON public.jobs
FOR EACH ROW
EXECUTE FUNCTION public.prevent_active_vm_remote_operation_rebind();

CREATE TRIGGER trg_threads_prevent_active_vm_remote_operation_rebind
BEFORE UPDATE OF metadata, status ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.prevent_active_vm_remote_operation_rebind();

-- Run before the existing process-zero / retirement deletion guards. A live
-- remote operation is the earliest owner-delete veto; later guards still
-- require their own cleanup authority after this lease settles.
CREATE TRIGGER trg_jobs_00_prevent_active_vm_remote_operation_delete
BEFORE DELETE ON public.jobs
FOR EACH ROW
EXECUTE FUNCTION public.prevent_active_vm_remote_operation_rebind();

CREATE TRIGGER trg_threads_00_prevent_active_vm_remote_operation_delete
BEFORE DELETE ON public.threads
FOR EACH ROW
EXECUTE FUNCTION public.prevent_active_vm_remote_operation_rebind();

COMMIT;
