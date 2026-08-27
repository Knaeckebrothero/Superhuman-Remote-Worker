-- migration:     0182_deliverable_contract_authority.sql
-- description:   Make job deliverable contracts immutable, add exact PR
--                proof, preserve Officer ticket-generation anti-downgrade
--                requirements, and represent undelivered terminal outcomes.
-- depends-on:    0181_sudo_requests_validate_constraints.sql
-- expected:      < 2min. Adds three small authority tables and nullable outcome
--                columns; backfills only jobs carrying required_deliverables.
-- locks:         ACCESS EXCLUSIVE briefly for ALTER TABLE and trigger install;
--                row-level writes while contract rows are backfilled.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

ALTER TABLE public.jobs
    ADD COLUMN completion_outcome_kind TEXT;

ALTER TABLE public.jobs
    ADD CONSTRAINT jobs_completion_outcome_kind
    CHECK (
        completion_outcome_kind IS NULL
        OR completion_outcome_kind = 'blocked_undelivered'
    ) NOT VALID;

-- Keep the long-standing project-jobs compatibility view truthful. Adding the
-- column at the end preserves its existing row shape for rolling old readers.
CREATE OR REPLACE VIEW public.job_summary AS
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
    j.runner_kind,
    j.completion_outcome_kind
   FROM public.jobs j;

ALTER TABLE public.officer_ticket_claims
    ADD COLUMN completion_outcome_kind_at_delete TEXT;

ALTER TABLE public.officer_ticket_claims
    ADD CONSTRAINT officer_ticket_claim_delete_outcome_kind
    CHECK (
        completion_outcome_kind_at_delete IS NULL
        OR completion_outcome_kind_at_delete = 'blocked_undelivered'
    ) NOT VALID;

CREATE TABLE public.job_deliverable_contracts (
    job_id UUID PRIMARY KEY
        REFERENCES public.jobs(id) ON DELETE CASCADE,
    normalized_deliverables TEXT[] NOT NULL,
    pr_repositories TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    pr_bindings JSONB NOT NULL DEFAULT '[]'::jsonb,
    contract_digest TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'server_normalized',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT job_deliverable_contract_provenance
        CHECK (provenance IN ('server_normalized', 'rolling_trigger_backfill')),
    CONSTRAINT job_deliverable_contract_pr_shape
        CHECK (jsonb_typeof(pr_bindings) = 'array')
);

COMMENT ON TABLE public.job_deliverable_contracts IS
    'Server-normalized immutable job delivery contract. Pull-request identity '
    'and proof live separately from mutable jobs.context.';

CREATE FUNCTION public.enforce_job_deliverable_contract_row_immutability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- The INSERT capture trigger necessarily runs before the current writer
    -- can attach its exact datasource bindings. Permit that single promotion
    -- only inside the transaction that created the compatibility row. Once
    -- server-normalized (or once the creating transaction commits), contract
    -- identity can never change; live PR proof has separate mutable columns.
    IF OLD.provenance = 'rolling_trigger_backfill'
       AND NEW.provenance = 'server_normalized'
       AND OLD.created_at = transaction_timestamp() THEN
        RETURN NEW;
    END IF;

    IF NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.normalized_deliverables IS DISTINCT FROM OLD.normalized_deliverables
       OR NEW.pr_repositories IS DISTINCT FROM OLD.pr_repositories
       OR NEW.pr_bindings IS DISTINCT FROM OLD.pr_bindings
       OR NEW.contract_digest IS DISTINCT FROM OLD.contract_digest
       OR NEW.provenance IS DISTINCT FROM OLD.provenance THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'job_deliverable_contract_row_is_immutable',
            MESSAGE = 'The normalized deliverable contract cannot change after admission';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_job_deliverable_contract_row_immutability
BEFORE UPDATE OF job_id, normalized_deliverables, pr_repositories, pr_bindings,
                 contract_digest, provenance, created_at
ON public.job_deliverable_contracts
FOR EACH ROW EXECUTE FUNCTION
    public.enforce_job_deliverable_contract_row_immutability();

CREATE TABLE public.job_pull_request_authorities (
    job_id UUID PRIMARY KEY
        REFERENCES public.jobs(id) ON DELETE CASCADE,
    record_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    record_generation BIGINT NOT NULL DEFAULT 1,
    datasource_id UUID NOT NULL,
    repository TEXT NOT NULL,
    forge TEXT NOT NULL,
    number INTEGER NOT NULL,
    url TEXT NOT NULL,
    head TEXT NOT NULL,
    base TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at TIMESTAMPTZ,
    verified_record_id UUID,
    verified_generation BIGINT,
    verified_state TEXT,
    verified_head TEXT,
    verified_base TEXT,
    verified_head_revision TEXT,
    CONSTRAINT job_pull_request_authority_identity CHECK (
        record_generation > 0
        AND number > 0
        AND repository ~ '^[a-z0-9][a-z0-9._-]{0,99}/[a-z0-9][a-z0-9._-]{0,99}$'
        AND forge <> '' AND length(forge) <= 32
        AND head <> '' AND length(head) <= 500
        AND base <> '' AND length(base) <= 500
        AND source_revision ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
        AND length(url) <= 2000
        AND url !~ '^[A-Za-z][A-Za-z0-9+.-]*://[^/]*@'
    ),
    CONSTRAINT job_pull_request_authority_proof CHECK (
        (
            verified_at IS NULL
            AND verified_record_id IS NULL
            AND verified_generation IS NULL
            AND verified_state IS NULL
            AND verified_head IS NULL
            AND verified_base IS NULL
            AND verified_head_revision IS NULL
        ) OR (
            verified_at IS NOT NULL
            AND verified_record_id = record_id
            AND verified_generation = record_generation
            AND verified_state IN ('open', 'merged', 'closed')
            AND verified_head = head
            AND verified_base = base
            AND verified_head_revision = source_revision
        )
    )
);

COMMENT ON TABLE public.job_pull_request_authorities IS
    'Server-owned PR identity written only after repo_open_pr verifies the '
    'exact pushed source branch. jobs.context.pull_request is a trigger-checked '
    'safe projection and never completion authority.';

CREATE FUNCTION public.enforce_job_pull_request_authority_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    contract public.job_deliverable_contracts%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.job_id IS DISTINCT FROM OLD.job_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'job_pull_request_authority_job_is_immutable',
            MESSAGE = 'Pull-request authority cannot move between jobs';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM public.jobs AS job
          JOIN public.job_datasources AS attachment
            ON attachment.job_id = job.id
          JOIN public.datasources AS datasource
            ON datasource.id = attachment.datasource_id
         WHERE job.id = NEW.job_id
           AND attachment.datasource_id = NEW.datasource_id
           AND datasource.type = 'repository'
           AND datasource.read_only IS NOT TRUE
           AND datasource.policy_revision = NEW.policy_revision
           AND NOT EXISTS (
               SELECT 1
                 FROM public.project_datasources AS project_link
                WHERE project_link.project_id = job.project_id
                  AND project_link.datasource_id = datasource.id
                  AND project_link.read_only
           )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'job_pull_request_authority_requires_writable_attachment',
            MESSAGE = 'Pull-request authority requires the exact writable attachment';
    END IF;

    SELECT * INTO contract
      FROM public.job_deliverable_contracts
     WHERE job_id = NEW.job_id;
    IF FOUND AND cardinality(contract.pr_repositories) > 0 AND (
        cardinality(contract.pr_repositories) <> 1
        OR jsonb_array_length(contract.pr_bindings) <> 1
        OR contract.pr_repositories[1] IS DISTINCT FROM NEW.repository
        OR contract.pr_bindings->0->>'repository'
            IS DISTINCT FROM NEW.repository
        OR contract.pr_bindings->0->>'datasource_id'
            IS DISTINCT FROM NEW.datasource_id::text
        OR contract.pr_bindings->0->>'forge' IS DISTINCT FROM NEW.forge
        OR contract.pr_bindings->0->>'policy_revision'
            IS DISTINCT FROM NEW.policy_revision::text
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'job_pull_request_authority_mismatches_contract',
            MESSAGE = 'Pull-request authority does not match the immutable contract';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_job_pull_request_authority_scope
BEFORE INSERT OR UPDATE ON public.job_pull_request_authorities
FOR EACH ROW EXECUTE FUNCTION public.enforce_job_pull_request_authority_scope();

CREATE TABLE public.officer_ticket_deliverable_requirements (
    project_id UUID NOT NULL
        REFERENCES public.projects(id) ON DELETE CASCADE,
    ticket_note_id TEXT NOT NULL,
    ready_generation_at TIMESTAMPTZ NOT NULL,
    required_pr_repositories TEXT[] NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'rejected_cloned_repository_path',
    -- Historical provenance, not live authority. Keeping the UUID snapshot
    -- without a threads FK lets an ended/decommissioned Officer thread be
    -- retired without erasing or wedging the ticket-generation requirement.
    officer_thread_id UUID NOT NULL,
    officer_incarnation INTEGER NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, ticket_note_id, ready_generation_at),
    CONSTRAINT officer_ticket_delivery_requirement_nonempty
        CHECK (cardinality(required_pr_repositories) > 0),
    CONSTRAINT officer_ticket_delivery_requirement_source
        CHECK (source_kind = 'rejected_cloned_repository_path')
);

COMMENT ON TABLE public.officer_ticket_deliverable_requirements IS
    'Monotonic per-ready-generation PR requirement recorded before a rejected '
    'Officer external-repository contract returns; prevents kb: laundering.';

CREATE FUNCTION public.enforce_job_deliverable_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    contract public.job_deliverable_contracts%ROWTYPE;
    pr_authority public.job_pull_request_authorities%ROWTYPE;
    expected_projection JSONB;
    contract_found BOOLEAN;
    pr_authority_found BOOLEAN;
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Old and new raw creation paths both cross this boundary. A PR record
        -- exists only after repo_open_pr succeeds on an already-created job.
        IF NEW.completion_outcome_kind IS NOT NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'completion_outcome_is_server_owned',
                MESSAGE = 'A terminal completion outcome cannot be authored at job creation';
        END IF;
        NEW.context := COALESCE(NEW.context, '{}'::jsonb)
            - 'pull_request'
            - 'deliverable_contract_provenance'
            - 'prior_deliverable_contract'
            - 'required_pr_repositories';
        IF NEW.status = 'completed'
           AND jsonb_typeof(NEW.context->'required_deliverables') = 'array'
           AND EXISTS (
               SELECT 1
                 FROM jsonb_array_elements_text(
                     NEW.context->'required_deliverables'
                ) AS entry(value)
                WHERE lower(btrim(entry.value)) LIKE 'pr:%'
                   OR btrim(entry.value) ~ '^(\./)*/*repos/.+'
                   OR btrim(entry.value) ~ '^(\./)*/*repo/repos/.+'
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'pr_deliverable_requires_live_proof',
                MESSAGE = 'A PR-contracted job cannot insert as completed';
        END IF;
    ELSE
        IF COALESCE(NEW.context, '{}'::jsonb)->'required_deliverables'
           IS DISTINCT FROM
           COALESCE(OLD.context, '{}'::jsonb)->'required_deliverables' THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'job_deliverable_contract_is_immutable',
                MESSAGE = 'The admitted deliverable contract is immutable';
        END IF;
        IF OLD.completion_outcome_kind = 'blocked_undelivered'
           AND (
               NEW.completion_outcome_kind IS DISTINCT FROM
                   'blocked_undelivered'
               OR NEW.status <> 'cancelled'
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'blocked_undelivered_is_terminal',
                MESSAGE = 'Blocked/undelivered work cannot be resumed';
        END IF;

        -- Rolling old replicas may still attempt a generic context merge.
        -- A projection change is accepted only after the exact authoritative
        -- row exists in this transaction.  Context alone can never create or
        -- replace PR evidence.
        IF COALESCE(NEW.context, '{}'::jsonb)->'pull_request'
           IS DISTINCT FROM
           COALESCE(OLD.context, '{}'::jsonb)->'pull_request' THEN
            SELECT * INTO pr_authority
              FROM public.job_pull_request_authorities
             WHERE job_id = NEW.id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'pull_request_projection_requires_authority',
                    MESSAGE = 'Pull-request context is a server-owned projection';
            END IF;
            expected_projection := jsonb_build_object(
                'forge', pr_authority.forge,
                'repo', pr_authority.repository,
                'number', pr_authority.number,
                'url', pr_authority.url,
                'head', pr_authority.head,
                'base', pr_authority.base
            );
            IF NEW.context->'pull_request' IS DISTINCT FROM expected_projection THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    CONSTRAINT = 'pull_request_projection_mismatches_authority',
                    MESSAGE = 'Pull-request context does not match server authority';
            END IF;
        END IF;
    END IF;

    IF NEW.completion_outcome_kind = 'blocked_undelivered'
       AND NEW.status <> 'cancelled' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'blocked_undelivered_is_terminal',
            MESSAGE = 'Blocked/undelivered work must remain terminal';
    END IF;

    IF NEW.status = 'completed' THEN
        SELECT * INTO contract
          FROM public.job_deliverable_contracts
         WHERE job_id = NEW.id;
        contract_found := FOUND;
        SELECT * INTO pr_authority
          FROM public.job_pull_request_authorities
         WHERE job_id = NEW.id;
        pr_authority_found := FOUND;
        IF contract_found AND (
            EXISTS (
                SELECT 1
                  FROM unnest(contract.normalized_deliverables) AS item(value)
                 WHERE btrim(item.value) ~ '^(\./)*/*repos/.+'
                    OR btrim(item.value) ~ '^(\./)*/*repo/repos/.+'
            )
            OR (
                cardinality(contract.pr_repositories) > 0
                AND (
                cardinality(contract.pr_repositories) <> 1
                    OR jsonb_array_length(contract.pr_bindings) <> 1
                    OR NOT pr_authority_found
                    OR pr_authority.repository
                        IS DISTINCT FROM contract.pr_repositories[1]
                    OR pr_authority.datasource_id::text
                        IS DISTINCT FROM contract.pr_bindings->0->>'datasource_id'
                    OR pr_authority.forge
                        IS DISTINCT FROM contract.pr_bindings->0->>'forge'
                    OR pr_authority.policy_revision::text
                        IS DISTINCT FROM contract.pr_bindings->0->>'policy_revision'
                    OR pr_authority.verified_at IS NULL
                    OR pr_authority.verified_record_id
                        IS DISTINCT FROM pr_authority.record_id
                    OR pr_authority.verified_generation
                        IS DISTINCT FROM pr_authority.record_generation
                    OR pr_authority.verified_head
                        IS DISTINCT FROM pr_authority.head
                    OR pr_authority.verified_base
                        IS DISTINCT FROM pr_authority.base
                    OR pr_authority.verified_head_revision
                        IS DISTINCT FROM pr_authority.source_revision
                    OR NOT EXISTS (
                        SELECT 1
                          FROM public.job_datasources AS attachment
                          JOIN public.datasources AS datasource
                            ON datasource.id = attachment.datasource_id
                         WHERE attachment.job_id = NEW.id
                           AND attachment.datasource_id =
                               pr_authority.datasource_id
                           AND datasource.type = 'repository'
                           AND datasource.read_only IS NOT TRUE
                           AND datasource.policy_revision IS NOT DISTINCT FROM
                               CASE
                                   WHEN COALESCE(
                                       contract.pr_bindings->0->>'policy_revision', ''
                                   ) ~ '^[0-9]+$'
                                   THEN (contract.pr_bindings->0->>'policy_revision')::integer
                                   ELSE NULL
                               END
                           AND NOT EXISTS (
                               SELECT 1
                                 FROM public.project_datasources AS project_link
                                WHERE project_link.project_id = NEW.project_id
                                  AND project_link.datasource_id = datasource.id
                                  AND project_link.read_only
                           )
                    )
                )
            )
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'pr_deliverable_requires_live_proof',
                MESSAGE = 'The pull-request deliverable has not been verified';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_job_deliverable_authority
BEFORE INSERT OR UPDATE OF context, status, completion_outcome_kind
ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.enforce_job_deliverable_authority();

CREATE FUNCTION public.capture_job_deliverable_contract()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    normalized TEXT[];
    pr_repos TEXT[];
BEGIN
    IF jsonb_typeof(NEW.context->'required_deliverables') <> 'array' THEN
        RETURN NEW;
    END IF;

    SELECT COALESCE(array_agg(value ORDER BY ordinal), ARRAY[]::TEXT[])
      INTO normalized
      FROM (
          SELECT DISTINCT ON (btrim(entry.value))
                 btrim(entry.value) AS value,
                 entry.ordinality AS ordinal
            FROM jsonb_array_elements_text(
                     NEW.context->'required_deliverables'
                 ) WITH ORDINALITY AS entry(value, ordinality)
           WHERE btrim(entry.value) <> ''
           ORDER BY btrim(entry.value), entry.ordinality
      ) AS values_in_declared_order;

    SELECT COALESCE(array_agg(lower(substr(value, 4))), ARRAY[]::TEXT[])
      INTO pr_repos
      FROM unnest(normalized) AS value
     WHERE lower(value) LIKE 'pr:%';

    INSERT INTO public.job_deliverable_contracts (
        job_id, normalized_deliverables, pr_repositories, contract_digest,
        provenance
    ) VALUES (
        NEW.id,
        normalized,
        pr_repos,
        md5(array_to_string(normalized, E'\n')),
        'rolling_trigger_backfill'
    )
    ON CONFLICT (job_id) DO NOTHING;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_capture_job_deliverable_contract
AFTER INSERT ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.capture_job_deliverable_contract();

-- A rejected repos/... request never reaches the jobs table. During a rolling
-- deployment an older replica therefore cannot participate safely in Officer
-- ticket admission: it has no way to record the monotonic publication
-- requirement before a later kb: retry. New replicas leave a normalized
-- contract receipt for every claimed ticket, including an intentionally empty
-- contract. Check it at transaction commit so create_job may insert the job
-- first and upgrade the capture trigger's compatibility row afterward.
CREATE FUNCTION public.enforce_officer_ticket_delivery_writer()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM public.officer_ticket_claims AS claim
         WHERE claim.job_id = NEW.id
    ) AND NOT EXISTS (
        SELECT 1
          FROM public.job_deliverable_contracts AS contract
         WHERE contract.job_id = NEW.id
           AND contract.provenance = 'server_normalized'
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'officer_ticket_delivery_writer_is_current',
            MESSAGE = 'Officer ticket admission requires current deliverable authority';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_officer_ticket_delivery_writer
AFTER INSERT ON public.jobs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.enforce_officer_ticket_delivery_writer();

-- Preserve the server-owned terminal interpretation when a job is deleted.
-- CREATE OR REPLACE keeps 0162 immutable and lets old application replicas
-- continue using the same trigger while 0182 rolls out.
CREATE OR REPLACE FUNCTION public.audit_officer_ticket_claim_job_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    UPDATE public.officer_ticket_claims
       SET job_deleted_at = COALESCE(job_deleted_at, statement_timestamp()),
           job_status_at_delete = COALESCE(job_status_at_delete, OLD.status),
           completion_outcome_kind_at_delete = COALESCE(
               completion_outcome_kind_at_delete,
               OLD.completion_outcome_kind
           ),
           deletion_reason = COALESCE(
               deletion_reason,
               'database_delete_compatibility_trigger'
           )
     WHERE job_id = OLD.id;
    RETURN OLD;
END
$function$;

COMMENT ON FUNCTION public.audit_officer_ticket_claim_job_delete() IS
    '0162 deletion audit extended by 0182 to retain the server-owned terminal outcome used by breaker and claim inspection.';

INSERT INTO public.job_deliverable_contracts (
    job_id, normalized_deliverables, pr_repositories, contract_digest,
    provenance
)
SELECT job.id,
       normalized.values,
       ARRAY(
           SELECT lower(substr(value, 4))
             FROM unnest(normalized.values) AS value
            WHERE lower(value) LIKE 'pr:%'
       ),
       md5(array_to_string(normalized.values, E'\n')),
       'rolling_trigger_backfill'
  FROM public.jobs AS job
 CROSS JOIN LATERAL (
       SELECT COALESCE(array_agg(value ORDER BY ordinal), ARRAY[]::TEXT[]) AS values
         FROM (
             SELECT DISTINCT ON (btrim(entry.value))
                    btrim(entry.value) AS value,
                    entry.ordinality AS ordinal
               FROM jsonb_array_elements_text(
                        job.context->'required_deliverables'
                    ) WITH ORDINALITY AS entry(value, ordinality)
              WHERE btrim(entry.value) <> ''
              ORDER BY btrim(entry.value), entry.ordinality
         ) AS ordered_values
 ) AS normalized
 WHERE jsonb_typeof(job.context->'required_deliverables') = 'array'
ON CONFLICT (job_id) DO NOTHING;

COMMIT;
