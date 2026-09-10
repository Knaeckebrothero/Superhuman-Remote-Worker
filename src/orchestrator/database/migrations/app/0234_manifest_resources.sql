-- migration:     0234_manifest_resources.sql
-- description:   Authored resources, immutable revisions and execution
--                admission snapshots for the manifest tier.
-- depends-on:    0233_run_queue_bg_tasks.sql
-- expected:      < 5s. New tables only; no row backfill.
-- locks:         New tables, plus one ADD COLUMN each on experts and projects.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone = 'UTC';

-- Authored resources, immutable revisions and execution admission snapshots.
-- Existing lifecycle tables remain the authority for work and workspace status.
CREATE TABLE srw_resources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL CHECK (kind IN ('Expert', 'WorkspaceTemplate', 'Connector', 'Project', 'Job')),
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('Account', 'Project', 'Catalog')),
    scope_name TEXT NOT NULL,
    name TEXT NOT NULL,
    owner_id UUID REFERENCES users(id) ON DELETE RESTRICT,
    project_id UUID REFERENCES projects(id) ON DELETE RESTRICT,
    linked_id UUID,
    managed_by UUID REFERENCES srw_resources(id) ON DELETE RESTRICT,
    resource_version BIGINT NOT NULL DEFAULT 1 CHECK (resource_version > 0),
    document JSONB NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    resolved JSONB NOT NULL CHECK (jsonb_typeof(resolved) = 'object'),
    revision TEXT NOT NULL,
    dependencies JSONB NOT NULL DEFAULT '[]'::jsonb,
    active_revision TEXT,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX srw_resources_identity ON srw_resources(kind, scope_kind, scope_name, name)
    WHERE deleted_at IS NULL;
CREATE INDEX srw_resources_owner ON srw_resources(owner_id) WHERE deleted_at IS NULL;
CREATE INDEX srw_resources_project ON srw_resources(project_id) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX srw_resources_link ON srw_resources(kind, linked_id)
    WHERE linked_id IS NOT NULL AND deleted_at IS NULL;

-- experts and projects predate this chain, so the reference is added NOT VALID
-- and validated by 0239. The column arrives all-NULL and no row can reference a
-- resource yet, so the deferred scan can only ever confirm what is already true.
ALTER TABLE experts ADD COLUMN manifest_resource_id UUID;
ALTER TABLE experts
    ADD CONSTRAINT experts_manifest_resource_id_fkey
    FOREIGN KEY (manifest_resource_id) REFERENCES srw_resources(id)
    ON DELETE RESTRICT NOT VALID;
ALTER TABLE projects ADD COLUMN manifest_resource_id UUID;
ALTER TABLE projects
    ADD CONSTRAINT projects_manifest_resource_id_fkey
    FOREIGN KEY (manifest_resource_id) REFERENCES srw_resources(id)
    ON DELETE RESTRICT NOT VALID;

CREATE TABLE srw_resource_revisions (
    resource_id UUID NOT NULL REFERENCES srw_resources(id) ON DELETE RESTRICT,
    resource_version BIGINT NOT NULL,
    document JSONB NOT NULL,
    resolved JSONB NOT NULL,
    revision TEXT NOT NULL,
    dependencies JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(resource_id, resource_version)
);
CREATE INDEX srw_resource_revisions_digest ON srw_resource_revisions(resource_id, revision);

CREATE TABLE srw_manifest_operations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    idempotency_key TEXT,
    request_revision TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(owner_id, idempotency_key)
);

-- Values are encrypted with the same application credential cipher as existing
-- provider credentials. No plaintext credential values enter resource revisions.
CREATE TABLE srw_resource_secrets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('Account', 'Project')),
    scope_name TEXT NOT NULL,
    name TEXT NOT NULL,
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    ciphertext TEXT NOT NULL,
    keys TEXT[] NOT NULL,
    version BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(scope_kind, scope_name, name)
);

CREATE TABLE srw_execution_specs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id UUID REFERENCES srw_resources(id) ON DELETE RESTRICT,
    resource_version BIGINT,
    work_kind TEXT NOT NULL CHECK (work_kind IN ('Job', 'Session')),
    work_id UUID NOT NULL,
    owner_id UUID REFERENCES users(id) ON DELETE RESTRICT,
    project_ids UUID[] NOT NULL DEFAULT '{}',
    document JSONB NOT NULL,
    resolved JSONB NOT NULL,
    dependencies JSONB NOT NULL DEFAULT '[]',
    revision TEXT NOT NULL,
    harness_adapter TEXT NOT NULL,
    generation BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(work_kind, work_id)
);
CREATE UNIQUE INDEX srw_execution_specs_resource_job ON srw_execution_specs(resource_id)
    WHERE work_kind = 'Job' AND resource_id IS NOT NULL;

CREATE TABLE srw_execution_spec_revisions (
    execution_id UUID NOT NULL REFERENCES srw_execution_specs(id) ON DELETE RESTRICT,
    generation BIGINT NOT NULL,
    document JSONB NOT NULL,
    resolved JSONB NOT NULL,
    dependencies JSONB NOT NULL DEFAULT '[]',
    revision TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (execution_id, generation)
);

CREATE TABLE srw_execution_attempts (
    execution_id UUID NOT NULL REFERENCES srw_execution_specs(id) ON DELETE RESTRICT,
    attempt BIGINT NOT NULL CHECK (attempt > 0),
    pod_name TEXT NOT NULL,
    pod_uid TEXT,
    phase TEXT NOT NULL CHECK (phase IN ('Preparing', 'Running', 'Succeeded', 'Failed', 'Cancelling', 'Cancelled')),
    exit_code INTEGER,
    image_id TEXT,
    reported_outcome TEXT CHECK (reported_outcome IN ('Succeeded', 'Failed')),
    cleaned_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY(execution_id, attempt),
    UNIQUE(pod_name)
);

CREATE TABLE srw_workspace_instances (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    project_id UUID REFERENCES projects(id) ON DELETE RESTRICT,
    recipe JSONB NOT NULL,
    revision TEXT NOT NULL,
    pvc_name TEXT NOT NULL,
    pvc_uid TEXT,
    generation BIGINT NOT NULL DEFAULT 0,
    execution_id UUID REFERENCES srw_execution_specs(id) ON DELETE RESTRICT,
    active_attempt BIGINT,
    pod_name TEXT,
    pod_uid TEXT,
    initialized BOOLEAN NOT NULL DEFAULT false,
    image_id TEXT,
    ssh_ciphertext TEXT,
    status TEXT NOT NULL DEFAULT 'Reserved',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX srw_workspace_instances_owner ON srw_workspace_instances(owner_id);
CREATE TABLE srw_execution_workspace_bindings (
    execution_id UUID PRIMARY KEY REFERENCES srw_execution_specs(id) ON DELETE RESTRICT,
    instance_id UUID NOT NULL REFERENCES srw_workspace_instances(id) ON DELETE RESTRICT
);

COMMENT ON TABLE srw_resources IS 'Canonical SRW authored resources; definition changes never replay an existing Job execution.';
COMMENT ON TABLE srw_execution_specs IS 'Server-authorized immutable configuration; jobs/threads retain lifecycle authority.';

-- Generic hosting never acquires an SRW agent or run-queue lease. Its process
-- authority is the durable attempt and exact observed pod UID instead.
CREATE FUNCTION enforce_manifest_job_dispatch() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    manifest_execution_id UUID;
    attempt_row srw_execution_attempts%ROWTYPE;
BEGIN
    SELECT id INTO manifest_execution_id FROM srw_execution_specs
      WHERE work_kind='Job' AND work_id=NEW.id AND harness_adapter='generic';
    IF manifest_execution_id IS NULL THEN RETURN NEW; END IF;
    IF NEW.assigned_agent_id IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
       OR NEW.execution_lane IS DISTINCT FROM 'pinned' THEN
        RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='manifest_job_controller_ownership',
          MESSAGE='Generic manifest work cannot be claimed by an SRW agent or worker lease';
    END IF;
    SELECT * INTO attempt_row FROM srw_execution_attempts
      WHERE execution_id=manifest_execution_id
      ORDER BY attempt DESC LIMIT 1;
    IF NEW.status='processing' AND (attempt_row.pod_uid IS NULL OR attempt_row.phase<>'Running') THEN
        RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='manifest_job_attempt_authority',
          MESSAGE='Generic processing requires a recorded running attempt and pod identity';
    END IF;
    IF NEW.status='completed' AND (attempt_row.cleaned_at IS NULL OR
       COALESCE(attempt_row.reported_outcome,attempt_row.phase) IS DISTINCT FROM 'Succeeded') THEN
        RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='manifest_job_outcome_authority',
          MESSAGE='Generic completion requires a recorded outcome and fenced process cleanup';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_manifest_job_dispatch
BEFORE UPDATE OF status,assigned_agent_id,lease_expires_at,execution_lane ON jobs
FOR EACH ROW EXECUTE FUNCTION enforce_manifest_job_dispatch();

COMMIT;
