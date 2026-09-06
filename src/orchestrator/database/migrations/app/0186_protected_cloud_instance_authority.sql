-- migration:     0186_protected_cloud_instance_authority.sql
-- description:   Persist immutable main-cloud installation identities and
--                exact protected-reader/effect authority.
-- depends-on:    0185_thread_runtime_generation_retirement.sql
-- maintenance-gate: pinned-runtime-authority-v1
-- expected:      New authority/effect tables; nullable UUID/source columns on
--                cloud resource rows; FK/check validation scans of projects,
--                threads, thread_mounts and cloud_ro_mounts; one live-reader
--                drain scan and value-preserving canonical-shape UPDATE.
-- locks:         Brief ACCESS EXCLUSIVE while columns and constraints are
--                installed, plus row locks on any retained live reader rows.
--                Reuses the 0185 drained-writer cutover window.
-- transactional: yes
-- rollout:       Live legacy protected readers must be revoked and drained.
--                Old provider-only project/thread rows remain nullable legacy
--                records; protected effects never adopt or route through them.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE IF NOT EXISTS public.main_cloud_backend_instances (
    id                         uuid PRIMARY KEY,
    backend_id                 text NOT NULL,
    routing                    jsonb NOT NULL,
    routing_sha256             text NOT NULL,
    installation_proof_sha256  text NOT NULL,
    secret_refs                jsonb NOT NULL,
    secret_revision            bigint NOT NULL DEFAULT 1,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT main_cloud_backend_instances_backend
        CHECK (backend_id IN ('nextcloud', 'opencloud')),
    CONSTRAINT main_cloud_backend_instances_routing_shape
        CHECK (jsonb_typeof(routing) = 'object'),
    CONSTRAINT main_cloud_backend_instances_routing_sha
        CHECK (routing_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT main_cloud_backend_instances_proof_sha
        CHECK (installation_proof_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT main_cloud_backend_instances_secret_shape
        CHECK (jsonb_typeof(secret_refs) = 'object'),
    CONSTRAINT main_cloud_backend_instances_secret_revision
        CHECK (secret_revision > 0),
    CONSTRAINT main_cloud_backend_instances_id_backend
        UNIQUE (id, backend_id),
    CONSTRAINT main_cloud_backend_instances_routing_identity
        UNIQUE (backend_id, routing_sha256, installation_proof_sha256)
);

CREATE TABLE IF NOT EXISTS public.main_cloud_active_backend (
    singleton                   boolean PRIMARY KEY DEFAULT true,
    backend_instance_id         uuid NOT NULL,
    backend_id                  text NOT NULL,
    activation_revision         bigint NOT NULL DEFAULT 1,
    activated_at                timestamptz NOT NULL DEFAULT now(),
    activated_by                text,
    CONSTRAINT main_cloud_active_backend_singleton CHECK (singleton),
    CONSTRAINT main_cloud_active_backend_revision
        CHECK (activation_revision > 0),
    CONSTRAINT main_cloud_active_backend_instance_fk
        FOREIGN KEY (backend_instance_id, backend_id)
        REFERENCES public.main_cloud_backend_instances (id, backend_id)
        ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION public.enforce_main_cloud_backend_instance_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'main-cloud backend instance history is retained'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'main_cloud_backend_instances_retained';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.backend_id IS DISTINCT FROM OLD.backend_id
       OR NEW.routing IS DISTINCT FROM OLD.routing
       OR NEW.routing_sha256 IS DISTINCT FROM OLD.routing_sha256
       OR NEW.installation_proof_sha256
            IS DISTINCT FROM OLD.installation_proof_sha256
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'main-cloud backend routing authority is immutable'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'main_cloud_backend_instances_immutable';
    END IF;
    IF NEW.secret_refs IS DISTINCT FROM OLD.secret_refs THEN
        IF NEW.secret_revision IS DISTINCT FROM OLD.secret_revision + 1 THEN
            RAISE EXCEPTION 'main-cloud secret rotation lacks next revision'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'main_cloud_backend_instances_secret_cas';
        END IF;
    ELSIF NEW.secret_revision IS DISTINCT FROM OLD.secret_revision THEN
        RAISE EXCEPTION 'main-cloud secret revision changed without references'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'main_cloud_backend_instances_secret_cas';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS main_cloud_backend_instances_history
    ON public.main_cloud_backend_instances;
CREATE TRIGGER main_cloud_backend_instances_history
BEFORE UPDATE OR DELETE ON public.main_cloud_backend_instances
FOR EACH ROW
EXECUTE FUNCTION public.enforce_main_cloud_backend_instance_history();

CREATE OR REPLACE FUNCTION public.enforce_main_cloud_active_backend_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'main-cloud active authority cannot be deleted'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'main_cloud_active_backend_retained';
    END IF;
    IF NEW.singleton IS DISTINCT FROM true
       OR NEW.activation_revision IS DISTINCT FROM OLD.activation_revision + 1
       OR (
           NEW.backend_instance_id IS NOT DISTINCT FROM OLD.backend_instance_id
           AND NEW.backend_id IS NOT DISTINCT FROM OLD.backend_id
       ) THEN
        RAISE EXCEPTION 'main-cloud activation lacks exact successor CAS'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'main_cloud_active_backend_cas';
    END IF;
    NEW.activated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS main_cloud_active_backend_history
    ON public.main_cloud_active_backend;
CREATE TRIGGER main_cloud_active_backend_history
BEFORE UPDATE OR DELETE ON public.main_cloud_active_backend
FOR EACH ROW
EXECUTE FUNCTION public.enforce_main_cloud_active_backend_history();

ALTER TABLE public.projects
    ADD COLUMN IF NOT EXISTS main_cloud_backend_instance_id uuid;
ALTER TABLE public.threads
    ADD COLUMN IF NOT EXISTS main_cloud_backend_instance_id uuid;
ALTER TABLE public.thread_mounts
    ADD COLUMN IF NOT EXISTS backend_instance_id uuid;
ALTER TABLE public.cloud_ro_mounts
    ADD COLUMN IF NOT EXISTS backend_instance_id uuid,
    ADD COLUMN IF NOT EXISTS grant_group_id text,
    ADD COLUMN IF NOT EXISTS grant_handle_sha256 text,
    ADD COLUMN IF NOT EXISTS source_binding jsonb,
    ADD COLUMN IF NOT EXISTS source_binding_sha256 text,
    ADD COLUMN IF NOT EXISTS selected_mount_id uuid,
    ADD COLUMN IF NOT EXISTS failure_code text,
    ADD COLUMN IF NOT EXISTS revocation_started_at timestamptz,
    ADD COLUMN IF NOT EXISTS remote_absence_verified_at timestamptz;

ALTER TABLE public.projects
    DROP CONSTRAINT IF EXISTS projects_main_cloud_backend_instance_fk,
    ADD CONSTRAINT projects_main_cloud_backend_instance_fk
        FOREIGN KEY (main_cloud_backend_instance_id, main_cloud_backend)
        REFERENCES public.main_cloud_backend_instances (id, backend_id)
        ON DELETE RESTRICT NOT VALID;
ALTER TABLE public.threads
    DROP CONSTRAINT IF EXISTS threads_main_cloud_backend_instance_fk,
    ADD CONSTRAINT threads_main_cloud_backend_instance_fk
        FOREIGN KEY (main_cloud_backend_instance_id, main_cloud_backend)
        REFERENCES public.main_cloud_backend_instances (id, backend_id)
        ON DELETE RESTRICT NOT VALID;
ALTER TABLE public.thread_mounts
    DROP CONSTRAINT IF EXISTS thread_mounts_backend_instance_fk,
    ADD CONSTRAINT thread_mounts_backend_instance_fk
        FOREIGN KEY (backend_instance_id, backend_id)
        REFERENCES public.main_cloud_backend_instances (id, backend_id)
        ON DELETE RESTRICT NOT VALID;
ALTER TABLE public.cloud_ro_mounts
    DROP CONSTRAINT IF EXISTS cloud_ro_mounts_backend_instance_fk,
    ADD CONSTRAINT cloud_ro_mounts_backend_instance_fk
        FOREIGN KEY (backend_instance_id, backend)
        REFERENCES public.main_cloud_backend_instances (id, backend_id)
        ON DELETE RESTRICT NOT VALID,
    DROP CONSTRAINT IF EXISTS cloud_ro_mounts_status_shape,
    ADD CONSTRAINT cloud_ro_mounts_status_shape
        CHECK (status IN ('engaging', 'active', 'revoking', 'revoked'))
        NOT VALID,
    DROP CONSTRAINT IF EXISTS cloud_ro_mounts_live_authority_shape,
    ADD CONSTRAINT cloud_ro_mounts_live_authority_shape CHECK (
        status = 'revoked'
        OR (
            backend = 'nextcloud'
            AND backend_instance_id IS NOT NULL
            AND runtime_generation IS NOT NULL
            AND engage_attempt IS NOT NULL
            AND NULLIF(reader_id, '') IS NOT NULL
            AND NULLIF(grant_group_id, '') IS NOT NULL
            AND NULLIF(grant_handle, '') IS NOT NULL
            AND grant_handle_sha256 ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(source_binding) = 'object'
            AND source_binding_sha256 ~ '^[0-9a-f]{64}$'
            AND selected_mount_id IS NOT NULL
            AND NULLIF(credentials, '') IS NOT NULL
            AND NULLIF(webdav_url, '') IS NOT NULL
            AND auth_kind = 'basic'
        )
    ) NOT VALID;

CREATE OR REPLACE FUNCTION public.protected_cloud_try_jsonb(value text)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
BEGIN
    RETURN value::jsonb;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION public.enforce_cloud_ro_mount_authority_shape()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    modern_authority boolean;
    source_handle jsonb;
    source_ref uuid;
    source_target text;
    source_native text;
    source_mountpoint text;
    source_canonical text;
    grant_canonical text;
    selected record;
    selected_handle jsonb;
    validate_selected boolean;
BEGIN
    modern_authority := (
        NEW.backend_instance_id IS NOT NULL
        OR NEW.grant_group_id IS NOT NULL
        OR NEW.grant_handle_sha256 IS NOT NULL
        OR NEW.source_binding IS NOT NULL
        OR NEW.source_binding_sha256 IS NOT NULL
        OR NEW.selected_mount_id IS NOT NULL
    );
    IF NEW.status IN ('engaging', 'active', 'revoking') THEN
        modern_authority := true;
    END IF;
    IF NOT modern_authority THEN
        -- Drained, revoked rows from before this authority version remain
        -- reject-only history. They can neither engage nor receive effects.
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.status <> 'engaging' THEN
        RAISE EXCEPTION 'protected reader must begin as an engaging intent'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_authority_shape';
    END IF;
    IF NEW.backend <> 'nextcloud'
       OR NEW.backend_instance_id IS NULL
       OR NEW.runtime_generation IS NULL
       OR NEW.engage_attempt IS NULL
       OR NEW.selected_mount_id IS NULL
       OR NEW.reader_id IS DISTINCT FROM
            'srw-reader-a-' || replace(NEW.engage_attempt::text, '-', '')
       OR NEW.grant_group_id IS DISTINCT FROM
            'srw-rog-a-' || replace(NEW.engage_attempt::text, '-', '')
       OR NEW.auth_kind IS DISTINCT FROM 'basic'
       OR NULLIF(NEW.webdav_url, '') IS NULL
       OR NEW.grant_handle_sha256 !~ '^[0-9a-f]{64}$'
       OR NEW.source_binding_sha256 !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(NEW.source_binding) IS DISTINCT FROM 'object'
       OR NEW.source_binding->>'version' IS DISTINCT FROM '1'
       OR NEW.source_binding->>'backend' IS DISTINCT FROM 'nextcloud'
       OR NEW.source_binding->>'backend_instance_id'
            IS DISTINCT FROM NEW.backend_instance_id::text
       OR NEW.source_binding->>'mount_kind' IS DISTINCT FROM 'project'
       OR NEW.source_binding->>'source_kind' IS DISTINCT FROM 'project_folder'
       OR jsonb_typeof(NEW.source_binding->'handle') IS DISTINCT FROM 'object'
       OR (
            NEW.status IN ('engaging', 'active', 'revoking')
            AND NULLIF(NEW.credentials, '') IS NULL
       )
       OR (
            NEW.status = 'active'
            AND jsonb_typeof(NEW.etag_baseline) IS DISTINCT FROM 'object'
       ) THEN
        RAISE EXCEPTION 'protected reader authority is malformed'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_authority_shape';
    END IF;

    BEGIN
        source_ref := (NEW.source_binding->>'source_ref')::uuid;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'protected reader source reference is malformed'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_authority_shape';
    END;
    source_handle := NEW.source_binding->'handle';
    source_target := NEW.source_binding->>'target_path';
    source_native := source_handle->>'native_id';
    source_mountpoint := source_handle->>'mountpoint';
    IF source_ref = '00000000-0000-0000-0000-000000000000'::uuid
       OR NEW.source_binding->>'source_ref' IS DISTINCT FROM source_ref::text
       OR NULLIF(source_target, '') IS NULL
       OR left(source_target, 1) = '/'
       OR position(E'\\' in source_target) > 0
       OR position('//' in source_target) > 0
       OR EXISTS (
            SELECT 1
              FROM unnest(string_to_array(source_target, '/')) AS segment(value)
             WHERE value IN ('', '.', '..')
       )
       OR COALESCE(source_native, '') !~ '^[1-9][0-9]*$'
       OR NULLIF(source_mountpoint, '') IS NULL
       OR source_mountpoint IN ('.', '..')
       OR position('/' in source_mountpoint) > 0
       OR position(E'\\' in source_mountpoint) > 0
       OR NEW.source_binding IS DISTINCT FROM pg_catalog.jsonb_build_object(
            'version', 1,
            'backend', 'nextcloud',
            'backend_instance_id', NEW.backend_instance_id::text,
            'mount_kind', 'project',
            'source_kind', 'project_folder',
            'source_ref', source_ref::text,
            'target_path', source_target,
            'handle', pg_catalog.jsonb_build_object(
                'native_id', source_native,
                'mountpoint', source_mountpoint
            )
       ) THEN
        RAISE EXCEPTION 'protected reader source binding is not canonical'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_authority_shape';
    END IF;

    source_canonical :=
        '{"backend":' || pg_catalog.to_jsonb('nextcloud'::text)::text ||
        ',"backend_instance_id":' ||
            pg_catalog.to_jsonb(NEW.backend_instance_id::text)::text ||
        ',"handle":{"mountpoint":' ||
            pg_catalog.to_jsonb(source_mountpoint)::text ||
        ',"native_id":' || pg_catalog.to_jsonb(source_native)::text || '}' ||
        ',"mount_kind":' || pg_catalog.to_jsonb('project'::text)::text ||
        ',"source_kind":' ||
            pg_catalog.to_jsonb('project_folder'::text)::text ||
        ',"source_ref":' || pg_catalog.to_jsonb(source_ref::text)::text ||
        ',"target_path":' || pg_catalog.to_jsonb(source_target)::text ||
        ',"version":1}';
    IF NEW.source_binding_sha256 IS DISTINCT FROM pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(source_canonical, 'UTF8')),
        'hex'
    ) THEN
        RAISE EXCEPTION 'protected reader source digest does not match'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_authority_shape';
    END IF;

    grant_canonical :=
        '{"backend":' || pg_catalog.to_jsonb('nextcloud'::text)::text ||
        ',"backend_instance_id":' ||
            pg_catalog.to_jsonb(NEW.backend_instance_id::text)::text ||
        ',"engage_attempt":' ||
            pg_catalog.to_jsonb(NEW.engage_attempt::text)::text ||
        ',"folder_id":' || pg_catalog.to_jsonb(source_native)::text ||
        ',"group_id":' || pg_catalog.to_jsonb(NEW.grant_group_id)::text ||
        ',"mountpoint":' || pg_catalog.to_jsonb(source_mountpoint)::text ||
        ',"reader_id":' || pg_catalog.to_jsonb(NEW.reader_id)::text ||
        ',"source_sha256":' ||
            pg_catalog.to_jsonb(NEW.source_binding_sha256)::text ||
        ',"version":1}';
    IF NEW.grant_handle IS DISTINCT FROM grant_canonical
       OR NEW.grant_handle_sha256 IS DISTINCT FROM pg_catalog.encode(
            pg_catalog.sha256(pg_catalog.convert_to(grant_canonical, 'UTF8')),
            'hex'
       ) THEN
        RAISE EXCEPTION 'protected reader grant handle does not match its attempt'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_authority_shape';
    END IF;

    validate_selected := TG_OP = 'INSERT';
    IF TG_OP = 'UPDATE' THEN
        validate_selected := (
            OLD.status = 'revoked'
            OR NEW.selected_mount_id IS DISTINCT FROM OLD.selected_mount_id
        );
    END IF;
    IF NEW.status = 'engaging' AND validate_selected THEN
        SELECT mount.mount_kind, mount.target_path, mount.source_kind,
               mount.source_ref, mount.backend_id,
               mount.backend_instance_id, mount.cloud_handle
          INTO selected
          FROM public.thread_mounts AS mount
         WHERE mount.id = NEW.selected_mount_id
           AND mount.thread_id = NEW.thread_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'protected reader selected mount no longer exists'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'cloud_ro_mounts_selected_source';
        END IF;
        selected_handle := public.protected_cloud_try_jsonb(selected.cloud_handle);
        IF selected.mount_kind IS DISTINCT FROM 'project'
           OR selected.target_path IS DISTINCT FROM source_target
           OR selected.source_kind IS DISTINCT FROM 'project_folder'
           OR selected.source_ref IS DISTINCT FROM source_ref
           OR selected.backend_id IS DISTINCT FROM 'nextcloud'
           OR selected.backend_instance_id IS DISTINCT FROM NEW.backend_instance_id
           OR selected_handle IS DISTINCT FROM pg_catalog.jsonb_build_object(
                'backend', 'nextcloud',
                'native_id', source_native,
                'vendor_meta', pg_catalog.jsonb_build_object(
                    'mountpoint', source_mountpoint
                )
           ) THEN
            RAISE EXCEPTION 'protected reader selected source changed'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'cloud_ro_mounts_selected_source';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS cloud_ro_mounts_authority_shape
    ON public.cloud_ro_mounts;
CREATE TRIGGER cloud_ro_mounts_authority_shape
BEFORE INSERT OR UPDATE ON public.cloud_ro_mounts
FOR EACH ROW
EXECUTE FUNCTION public.enforce_cloud_ro_mount_authority_shape();

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM public.cloud_ro_mounts
         WHERE status IN ('engaging', 'active', 'revoking')
           AND (
               backend_instance_id IS NULL
               OR runtime_generation IS NULL
               OR engage_attempt IS NULL
               OR grant_group_id IS NULL
               OR grant_handle_sha256 IS NULL
               OR source_binding IS NULL
               OR source_binding_sha256 IS NULL
           )
    ) THEN
        RAISE EXCEPTION
            '0186 requires every live legacy protected reader to be drained'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_instance_migration_authority';
    END IF;
    -- Invoke the canonical shape trigger against every retained live row.
    -- The assignment is intentionally value-preserving; a malformed row
    -- aborts this transaction and no migration ledger entry is recorded.
    UPDATE public.cloud_ro_mounts
       SET status = status
     WHERE status IN ('engaging', 'active', 'revoking');
END;
$$;

-- 0186 follows the drained 0185 authority cutover in the same startup. These
-- bounded validations must complete before the protected-cloud writer opens.
ALTER TABLE public.projects
    -- squawk-ignore constraint-missing-not-valid
    VALIDATE CONSTRAINT projects_main_cloud_backend_instance_fk;
ALTER TABLE public.threads
    -- squawk-ignore constraint-missing-not-valid
    VALIDATE CONSTRAINT threads_main_cloud_backend_instance_fk;
ALTER TABLE public.thread_mounts
    -- squawk-ignore constraint-missing-not-valid
    VALIDATE CONSTRAINT thread_mounts_backend_instance_fk;
ALTER TABLE public.cloud_ro_mounts
    -- squawk-ignore constraint-missing-not-valid
    VALIDATE CONSTRAINT cloud_ro_mounts_backend_instance_fk,
    -- squawk-ignore constraint-missing-not-valid
    VALIDATE CONSTRAINT cloud_ro_mounts_status_shape,
    -- squawk-ignore constraint-missing-not-valid
    VALIDATE CONSTRAINT cloud_ro_mounts_live_authority_shape;

-- One unresolved grant authority per remote principal. Attempt-scoped names
-- make these global uniqueness belts, not merely per-thread conventions. The
-- table is empty after the required drain, so a transactional build cannot
-- block a live reader writer and keeps the invariant atomic with the schema.
-- squawk-ignore require-concurrent-index-creation
CREATE UNIQUE INDEX IF NOT EXISTS cloud_ro_mounts_live_reader_authority_idx
    ON public.cloud_ro_mounts (reader_id)
    WHERE status IN ('engaging', 'active', 'revoking');
-- squawk-ignore require-concurrent-index-creation
CREATE UNIQUE INDEX IF NOT EXISTS cloud_ro_mounts_live_group_authority_idx
    ON public.cloud_ro_mounts (grant_group_id)
    WHERE status IN ('engaging', 'active', 'revoking');

CREATE TABLE IF NOT EXISTS public.cloud_ro_effect_intents (
    id                        uuid PRIMARY KEY DEFAULT public.uuid_generate_v4(),
    thread_id                 uuid NOT NULL,
    runtime_generation        uuid NOT NULL,
    engage_attempt            uuid NOT NULL,
    backend_instance_id       uuid NOT NULL,
    backend_id                text NOT NULL,
    config_sha256             text NOT NULL,
    request_authority_sha256  text NOT NULL,
    fence_intent              jsonb NOT NULL,
    status                    text NOT NULL DEFAULT 'planned',
    horizon                   jsonb,
    dispatch_closed_at        timestamptz,
    safe_after                timestamptz,
    created_at                timestamptz NOT NULL DEFAULT now(),
    closed_at                 timestamptz,
    CONSTRAINT cloud_ro_effect_intents_backend_fk
        FOREIGN KEY (backend_instance_id, backend_id)
        REFERENCES public.main_cloud_backend_instances (id, backend_id)
        ON DELETE RESTRICT,
    CONSTRAINT cloud_ro_effect_intents_backend
        CHECK (backend_id = 'nextcloud'),
    CONSTRAINT cloud_ro_effect_intents_request_sha
        CHECK (request_authority_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT cloud_ro_effect_intents_config_sha
        CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT cloud_ro_effect_intents_fence_shape
        CHECK (jsonb_typeof(fence_intent) = 'object'),
    CONSTRAINT cloud_ro_effect_intents_status
        CHECK (status IN ('planned', 'closed')),
    CONSTRAINT cloud_ro_effect_intents_close_shape CHECK (
        (
            status = 'planned'
            AND horizon IS NULL
            AND dispatch_closed_at IS NULL
            AND safe_after IS NULL
            AND closed_at IS NULL
        ) OR (
            status = 'closed'
            AND jsonb_typeof(horizon) = 'object'
            AND dispatch_closed_at IS NOT NULL
            AND safe_after IS NOT NULL
            AND closed_at IS NOT NULL
            AND safe_after >= dispatch_closed_at
        )
    ),
    CONSTRAINT cloud_ro_effect_intents_attempt_request
        UNIQUE (engage_attempt, request_authority_sha256)
);

CREATE INDEX IF NOT EXISTS cloud_ro_effect_intents_attempt_status_idx
    ON public.cloud_ro_effect_intents (engage_attempt, status, safe_after);

CREATE OR REPLACE FUNCTION public.enforce_cloud_ro_effect_intent_insert_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    capability jsonb;
    request jsonb;
    request_canonical text;
    db_before timestamptz;
    db_after timestamptz;
    fresh_until timestamptz;
    dispatched_at timestamptz;
    effect_not_after timestamptz;
    queue_bound integer;
    handler_bound integer;
    skew_bound integer;
    safety_margin integer;
    capability_max_age integer;
    exact_authority uuid;
BEGIN
    capability := NEW.fence_intent->'capability';
    request := NEW.fence_intent->'request';
    IF NEW.status <> 'planned'
       OR NEW.horizon IS NOT NULL
       OR NEW.dispatch_closed_at IS NOT NULL
       OR NEW.safe_after IS NOT NULL
       OR NEW.closed_at IS NOT NULL
       OR NEW.backend_id <> 'nextcloud'
       OR jsonb_typeof(NEW.fence_intent) IS DISTINCT FROM 'object'
       OR NEW.fence_intent IS DISTINCT FROM pg_catalog.jsonb_build_object(
            'version', NEW.fence_intent->'version',
            'capability', capability,
            'capability_signature', NEW.fence_intent->'capability_signature',
            'request', request,
            'request_signature', NEW.fence_intent->'request_signature',
            'db_before', NEW.fence_intent->'db_before',
            'db_after', NEW.fence_intent->'db_after',
            'fresh_until', NEW.fence_intent->'fresh_until',
            'db_dispatched_at', NEW.fence_intent->'db_dispatched_at'
       )
       OR NEW.fence_intent->>'version' IS DISTINCT FROM '1'
       OR jsonb_typeof(capability) IS DISTINCT FROM 'object'
       OR capability IS DISTINCT FROM pg_catalog.jsonb_build_object(
            'version', capability->'version',
            'backend_instance_id', capability->'backend_instance_id',
            'config_sha256', capability->'config_sha256',
            'queue_bound_seconds', capability->'queue_bound_seconds',
            'handler_bound_seconds', capability->'handler_bound_seconds',
            'clock_skew_bound_seconds', capability->'clock_skew_bound_seconds',
            'safety_margin_seconds', capability->'safety_margin_seconds',
            'capability_max_age_seconds', capability->'capability_max_age_seconds',
            'server_time', capability->'server_time'
       )
       OR capability->>'version' IS DISTINCT FROM '1'
       OR capability->>'backend_instance_id'
            IS DISTINCT FROM NEW.backend_instance_id::text
       OR capability->>'config_sha256' IS DISTINCT FROM NEW.config_sha256
       OR jsonb_typeof(request) IS DISTINCT FROM 'object'
       OR request IS DISTINCT FROM pg_catalog.jsonb_build_object(
            'version', request->'version',
            'backend_instance_id', request->'backend_instance_id',
            'config_sha256', request->'config_sha256',
            'engage_attempt', request->'engage_attempt',
            'method', request->'method',
            'path', request->'path',
            'body_sha256', request->'body_sha256',
            'effect_not_after', request->'effect_not_after'
       )
       OR request->>'version' IS DISTINCT FROM '1'
       OR request->>'backend_instance_id'
            IS DISTINCT FROM NEW.backend_instance_id::text
       OR request->>'config_sha256' IS DISTINCT FROM NEW.config_sha256
       OR request->>'engage_attempt' IS DISTINCT FROM NEW.engage_attempt::text
       OR COALESCE(request->>'method', '') NOT IN ('POST', 'PUT')
       OR COALESCE(request->>'path', '') !~ '^/'
       OR position('//' in COALESCE(request->>'path', '')) > 0
       OR position(E'\\' in COALESCE(request->>'path', '')) > 0
       OR position('?' in COALESCE(request->>'path', '')) > 0
       OR position('#' in COALESCE(request->>'path', '')) > 0
       OR COALESCE(request->>'body_sha256', '') !~ '^[0-9a-f]{64}$'
       OR COALESCE(NEW.fence_intent->>'capability_signature', '')
            !~ '^[0-9a-f]{64}$'
       OR COALESCE(NEW.fence_intent->>'request_signature', '')
            !~ '^[0-9a-f]{64}$'
       OR NEW.config_sha256 !~ '^[0-9a-f]{64}$'
       OR NEW.request_authority_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'protected effect intent shape is malformed'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_effect_intents_insert_authority';
    END IF;

    BEGIN
        queue_bound := (capability->>'queue_bound_seconds')::integer;
        handler_bound := (capability->>'handler_bound_seconds')::integer;
        skew_bound := (capability->>'clock_skew_bound_seconds')::integer;
        safety_margin := (capability->>'safety_margin_seconds')::integer;
        capability_max_age := (
            capability->>'capability_max_age_seconds'
        )::integer;
        db_before := (NEW.fence_intent->>'db_before')::timestamptz;
        db_after := (NEW.fence_intent->>'db_after')::timestamptz;
        fresh_until := (NEW.fence_intent->>'fresh_until')::timestamptz;
        dispatched_at := (NEW.fence_intent->>'db_dispatched_at')::timestamptz;
        effect_not_after := (request->>'effect_not_after')::timestamptz;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'protected effect timing authority is malformed'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_effect_intents_insert_authority';
    END;
    IF queue_bound < 1 OR queue_bound > 86400
       OR handler_bound < 1 OR handler_bound > 86400
       OR skew_bound < 1 OR skew_bound > 86400
       OR safety_margin < 1 OR safety_margin > 86400
       OR capability_max_age < 1 OR capability_max_age > 86400
       OR db_after < db_before
       OR fresh_until < db_after
       OR dispatched_at < db_after
       OR dispatched_at > fresh_until
       OR effect_not_after <= dispatched_at
       OR effect_not_after > dispatched_at
            + pg_catalog.make_interval(secs => queue_bound) THEN
        RAISE EXCEPTION 'protected effect timing authority is inconsistent'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_effect_intents_insert_authority';
    END IF;

    request_canonical :=
        '{"backend_instance_id":' ||
            pg_catalog.to_jsonb(NEW.backend_instance_id::text)::text ||
        ',"body_sha256":' ||
            pg_catalog.to_jsonb(request->>'body_sha256')::text ||
        ',"config_sha256":' || pg_catalog.to_jsonb(NEW.config_sha256)::text ||
        ',"effect_not_after":' ||
            pg_catalog.to_jsonb(request->>'effect_not_after')::text ||
        ',"engage_attempt":' ||
            pg_catalog.to_jsonb(NEW.engage_attempt::text)::text ||
        ',"method":' || pg_catalog.to_jsonb(request->>'method')::text ||
        ',"path":' || pg_catalog.to_jsonb(request->>'path')::text ||
        ',"version":1}';
    IF NEW.request_authority_sha256 IS DISTINCT FROM pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(request_canonical, 'UTF8')),
        'hex'
    ) THEN
        RAISE EXCEPTION 'protected effect request digest does not match'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_effect_intents_insert_authority';
    END IF;

    SELECT ro.id
      INTO exact_authority
      FROM public.cloud_ro_mounts AS ro
      JOIN public.threads AS thread ON thread.id = ro.thread_id
      JOIN public.main_cloud_backend_instances AS backend
        ON backend.id = ro.backend_instance_id
       AND backend.backend_id = ro.backend
     WHERE ro.thread_id = NEW.thread_id
       AND ro.runtime_generation = NEW.runtime_generation
       AND ro.engage_attempt = NEW.engage_attempt
       AND ro.backend_instance_id = NEW.backend_instance_id
       AND ro.backend = 'nextcloud'
       AND ro.status = 'engaging'
       AND thread.execution_lane = 'pinned'
       AND thread.runtime_generation = NEW.runtime_generation
       AND thread.runtime_retirement_token IS NULL
       AND thread.status IN ('created', 'active', 'awaiting_user', 'suspended')
       AND backend.routing->>'protected_effect_config_sha256' = NEW.config_sha256
     FOR SHARE OF ro, thread, backend;
    IF exact_authority IS NULL THEN
        RAISE EXCEPTION 'protected effect intent lacks exact live authority'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_effect_intents_insert_authority';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS cloud_ro_effect_intents_insert_authority
    ON public.cloud_ro_effect_intents;
CREATE TRIGGER cloud_ro_effect_intents_insert_authority
BEFORE INSERT ON public.cloud_ro_effect_intents
FOR EACH ROW
EXECUTE FUNCTION public.enforce_cloud_ro_effect_intent_insert_authority();

CREATE OR REPLACE FUNCTION public.enforce_cloud_ro_effect_intent_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    capability jsonb;
    request jsonb;
    dispatch_started_at timestamptz;
    effect_not_after timestamptz;
    expected_safe_after timestamptz;
    handler_bound integer;
    skew_bound integer;
    safety_margin integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'protected effect intent history is retained'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_effect_intents_retained';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.thread_id IS DISTINCT FROM OLD.thread_id
       OR NEW.runtime_generation IS DISTINCT FROM OLD.runtime_generation
       OR NEW.engage_attempt IS DISTINCT FROM OLD.engage_attempt
       OR NEW.backend_instance_id IS DISTINCT FROM OLD.backend_instance_id
       OR NEW.backend_id IS DISTINCT FROM OLD.backend_id
       OR NEW.config_sha256 IS DISTINCT FROM OLD.config_sha256
       OR NEW.request_authority_sha256
            IS DISTINCT FROM OLD.request_authority_sha256
       OR NEW.fence_intent IS DISTINCT FROM OLD.fence_intent
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'protected effect intent authority is immutable'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_effect_intents_immutable';
    END IF;
    IF OLD.status = 'planned' AND NEW.status = 'closed' THEN
        capability := OLD.fence_intent->'capability';
        request := OLD.fence_intent->'request';
        BEGIN
            dispatch_started_at := (
                OLD.fence_intent->>'db_dispatched_at'
            )::timestamptz;
            effect_not_after := (request->>'effect_not_after')::timestamptz;
            handler_bound := (capability->>'handler_bound_seconds')::integer;
            skew_bound := (capability->>'clock_skew_bound_seconds')::integer;
            safety_margin := (capability->>'safety_margin_seconds')::integer;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'protected effect horizon source is malformed'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'cloud_ro_effect_intents_horizon_authority';
        END;
        expected_safe_after := greatest(
            NEW.dispatch_closed_at,
            effect_not_after
        ) + pg_catalog.make_interval(
            secs => handler_bound + skew_bound + safety_margin
        );
        IF jsonb_typeof(NEW.horizon) IS DISTINCT FROM 'object'
           OR NEW.horizon IS DISTINCT FROM pg_catalog.jsonb_build_object(
                'version', NEW.horizon->'version',
                'intent', NEW.horizon->'intent',
                'intent_sha256', NEW.horizon->'intent_sha256',
                'dispatch_closed_at', NEW.horizon->'dispatch_closed_at',
                'safe_after', NEW.horizon->'safe_after'
           )
           OR NEW.horizon->>'version' IS DISTINCT FROM '1'
           OR NEW.horizon->'intent' IS DISTINCT FROM OLD.fence_intent
           OR COALESCE(NEW.horizon->>'intent_sha256', '')
                !~ '^[0-9a-f]{64}$'
           OR NEW.dispatch_closed_at IS NULL
           OR NEW.dispatch_closed_at < dispatch_started_at
           OR NEW.horizon->>'dispatch_closed_at' IS DISTINCT FROM
                to_char(
                    NEW.dispatch_closed_at AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                )
           OR NEW.safe_after IS DISTINCT FROM expected_safe_after
           OR NEW.horizon->>'safe_after' IS DISTINCT FROM
                to_char(
                    NEW.safe_after AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                ) THEN
            RAISE EXCEPTION 'protected effect horizon does not match its intent'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'cloud_ro_effect_intents_horizon_authority';
        END IF;
        NEW.closed_at := now();
        RETURN NEW;
    END IF;
    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'protected effect intent transition is invalid'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_effect_intents_transition';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS cloud_ro_effect_intents_history
    ON public.cloud_ro_effect_intents;
CREATE TRIGGER cloud_ro_effect_intents_history
BEFORE UPDATE OR DELETE ON public.cloud_ro_effect_intents
FOR EACH ROW
EXECUTE FUNCTION public.enforce_cloud_ro_effect_intent_history();

CREATE OR REPLACE FUNCTION public.enforce_cloud_ro_mount_attempt_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    unresolved_old_effect boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'protected reader attempt history is retained'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_attempt_retained';
    END IF;
    IF (
        NEW.thread_id IS DISTINCT FROM OLD.thread_id
        OR NEW.user_id IS DISTINCT FROM OLD.user_id
        OR NEW.backend IS DISTINCT FROM OLD.backend
        OR NEW.backend_instance_id IS DISTINCT FROM OLD.backend_instance_id
        OR NEW.reader_id IS DISTINCT FROM OLD.reader_id
        OR NEW.grant_group_id IS DISTINCT FROM OLD.grant_group_id
        OR NEW.grant_handle IS DISTINCT FROM OLD.grant_handle
        OR NEW.grant_handle_sha256 IS DISTINCT FROM OLD.grant_handle_sha256
        OR NEW.webdav_url IS DISTINCT FROM OLD.webdav_url
        OR NEW.auth_kind IS DISTINCT FROM OLD.auth_kind
        OR NEW.runtime_generation IS DISTINCT FROM OLD.runtime_generation
        OR NEW.engage_attempt IS DISTINCT FROM OLD.engage_attempt
        OR NEW.source_binding IS DISTINCT FROM OLD.source_binding
        OR NEW.source_binding_sha256 IS DISTINCT FROM OLD.source_binding_sha256
        OR NEW.selected_mount_id IS DISTINCT FROM OLD.selected_mount_id
    ) AND NOT (OLD.status = 'revoked' AND NEW.status = 'engaging')
    THEN
        RAISE EXCEPTION 'protected reader attempt authority is immutable'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_attempt_immutable';
    END IF;
    IF NEW.credentials IS DISTINCT FROM OLD.credentials
       AND NOT (
           (OLD.status = 'revoked' AND NEW.status = 'engaging')
           OR (
               OLD.status = 'revoking'
               AND NEW.status = 'revoked'
               AND NEW.credentials IS NULL
           )
       ) THEN
        RAISE EXCEPTION 'protected reader credential authority is immutable'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'cloud_ro_mounts_attempt_immutable';
    END IF;

    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'engaging' AND NEW.status = 'active' THEN
        IF jsonb_typeof(NEW.etag_baseline) <> 'object'
           OR NEW.revocation_started_at IS NOT NULL
           OR NEW.remote_absence_verified_at IS NOT NULL THEN
            RAISE EXCEPTION 'protected reader activation proof is incomplete'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'cloud_ro_mounts_activation_shape';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status IN ('engaging', 'active') AND NEW.status = 'revoking' THEN
        IF NEW.revocation_started_at IS NULL
           OR NEW.remote_absence_verified_at IS NOT NULL THEN
            RAISE EXCEPTION 'protected reader revocation was not begun exactly'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'cloud_ro_mounts_revocation_shape';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'revoking' AND NEW.status = 'revoked' THEN
        IF NEW.remote_absence_verified_at IS NULL
           OR EXISTS (
                SELECT 1
                  FROM public.cloud_ro_effect_intents effect
                 WHERE effect.engage_attempt = OLD.engage_attempt
                   AND (
                       effect.status <> 'closed'
                       OR effect.safe_after > clock_timestamp()
                   )
           ) THEN
            RAISE EXCEPTION 'protected reader revoke lacks elapsed effect fence'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'cloud_ro_mounts_effect_fence';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'revoked' AND NEW.status = 'engaging' THEN
        SELECT EXISTS (
            SELECT 1
              FROM public.cloud_ro_effect_intents effect
             WHERE effect.engage_attempt = OLD.engage_attempt
               AND (
                   effect.status <> 'closed'
                   OR effect.safe_after > clock_timestamp()
               )
        ) INTO unresolved_old_effect;
        IF NEW.engage_attempt IS NOT DISTINCT FROM OLD.engage_attempt
           OR unresolved_old_effect
           OR NEW.revoked_at IS NOT NULL
           OR NEW.revocation_started_at IS NOT NULL
           OR NEW.remote_absence_verified_at IS NOT NULL THEN
            RAISE EXCEPTION 'protected reader replacement lacks a settled predecessor'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'cloud_ro_mounts_attempt_replacement';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'protected reader attempt transition is invalid'
        USING ERRCODE = '23514',
              CONSTRAINT = 'cloud_ro_mounts_attempt_transition';
END;
$$;

DROP TRIGGER IF EXISTS cloud_ro_mounts_attempt_history
    ON public.cloud_ro_mounts;
CREATE TRIGGER cloud_ro_mounts_attempt_history
BEFORE UPDATE OR DELETE ON public.cloud_ro_mounts
FOR EACH ROW
EXECUTE FUNCTION public.enforce_cloud_ro_mount_attempt_history();

COMMENT ON TABLE public.main_cloud_backend_instances IS
    'Immutable non-secret routing and remote-installation authority. Secret values remain external; only exact opaque references may rotate.';
COMMENT ON TABLE public.main_cloud_active_backend IS
    'Singleton CAS pointer used only for fresh cloud effects. Historical resources resolve their recorded backend_instance_id.';
COMMENT ON TABLE public.cloud_ro_effect_intents IS
    'Append-once pre-dispatch signed Nextcloud effect evidence and its exact closed causal horizon; one row per authority-creating request.';
COMMENT ON COLUMN public.thread_mounts.backend_instance_id IS
    'Durable cloud installation authority; protected selection refuses NULL and never falls back to the active adapter.';
COMMENT ON COLUMN public.cloud_ro_mounts.source_binding IS
    'Canonical immutable logical protected source for this engage attempt and any staged review.';

COMMIT;
