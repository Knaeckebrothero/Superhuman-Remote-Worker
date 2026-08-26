-- migration:     0184_managed_repository_legacy_reconciliation.sql
-- description:   Add restart-safe intents for reconciling legacy managed
--                repository URLs without rewriting historical rows.
-- depends-on:    0183_compute_initial_recovery_epoch_authority.sql
-- expected:      < 1s. Two empty tables, one sequence, two indexes, and three
--                fail-closed/audit triggers; no
--                historical row scan or rewrite.
-- locks:         Brief SHARE ROW EXCLUSIVE locks on jobs and project_officers
--                for trigger installation; ACCESS EXCLUSIVE only on new objects.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE SEQUENCE public.managed_repository_legacy_reconcile_claim_seq;

CREATE TABLE public.managed_repository_legacy_reconciliations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_kind TEXT NOT NULL,
    source_id UUID NOT NULL,
    project_id UUID,
    classification TEXT NOT NULL,
    authority_kind TEXT,
    authority_id UUID,
    authority_record_id UUID REFERENCES
        public.managed_repository_authorities(id) ON DELETE RESTRICT,
    authority_generation BIGINT,
    repository_owner TEXT,
    repo_name TEXT,
    access_mode TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    result_kind TEXT,
    reason_code TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lifetime_attempts INTEGER NOT NULL DEFAULT 0,
    last_failure_reason_code TEXT,
    rearm_generation INTEGER NOT NULL DEFAULT 0,
    claim_token BIGINT NOT NULL DEFAULT 0,
    claimed_by UUID,
    claim_expires_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_scanned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT managed_repository_legacy_source_unique
        UNIQUE (source_kind, source_id),
    CONSTRAINT managed_repository_legacy_source_kind_check
        CHECK (source_kind IN ('job', 'thread', 'project_repository')),
    CONSTRAINT managed_repository_legacy_classification_check
        CHECK (classification IN (
            'runnable_job',
            'resumable_thread',
            'current_officer_thread',
            'shared_project_jobs_repository',
            'project_runtime_repository',
            'server_only_repository',
            'terminal_historical',
            'ambiguous'
        )),
    CONSTRAINT managed_repository_legacy_authority_kind_check
        CHECK (
            authority_kind IS NULL
            OR authority_kind IN ('job', 'thread', 'project_repository')
        ),
    CONSTRAINT managed_repository_legacy_authority_record_shape_check
        CHECK (
            (authority_record_id IS NULL AND authority_generation IS NULL)
            OR
            (authority_record_id IS NOT NULL AND authority_generation > 0)
        ),
    CONSTRAINT managed_repository_legacy_access_mode_check
        CHECK (access_mode IS NULL OR access_mode IN ('none', 'read', 'write')),
    CONSTRAINT managed_repository_legacy_state_check
        CHECK (state IN (
            'pending', 'claimed', 'retry', 'completed', 'ambiguous', 'failed'
        )),
    CONSTRAINT managed_repository_legacy_result_check
        CHECK (
            result_kind IS NULL
            OR result_kind IN (
                'adopted',
                'scrubbed_terminal',
                'source_absent',
                'authority_revoked'
            )
        ),
    CONSTRAINT managed_repository_legacy_attempts_check CHECK (attempts >= 0),
    CONSTRAINT managed_repository_legacy_lifetime_attempts_check
        CHECK (lifetime_attempts >= attempts AND lifetime_attempts >= 0),
    CONSTRAINT managed_repository_legacy_rearm_generation_check
        CHECK (rearm_generation >= 0),
    CONSTRAINT managed_repository_legacy_claim_shape_check
        CHECK (
            (state = 'claimed' AND claimed_by IS NOT NULL
                               AND claim_expires_at IS NOT NULL
                               AND claim_token > 0)
            OR
            (state <> 'claimed' AND claimed_by IS NULL
                                AND claim_expires_at IS NULL)
        ),
    CONSTRAINT managed_repository_legacy_authority_shape_check
        CHECK (
            (
                classification IN (
                    'runnable_job',
                    'resumable_thread',
                    'current_officer_thread',
                    'shared_project_jobs_repository',
                    'project_runtime_repository'
                )
                AND authority_kind IS NOT NULL
                AND authority_id IS NOT NULL
                AND repository_owner IS NOT NULL
                AND repo_name IS NOT NULL
                AND access_mode IN ('read', 'write')
            )
            OR
            (
                classification = 'terminal_historical'
                AND authority_kind IN ('job', 'thread', 'project_repository')
                AND authority_id IS NOT NULL
                AND repository_owner IS NOT NULL
                AND repo_name IS NOT NULL
                AND access_mode IN ('read', 'write')
            )
            OR
            (
                classification = 'server_only_repository'
                AND authority_kind = 'project_repository'
                AND authority_id IS NOT NULL
                AND repository_owner IS NOT NULL
                AND repo_name IS NOT NULL
                AND access_mode = 'none'
            )
            OR
            (
                classification = 'ambiguous'
                AND authority_kind IS NULL
                AND authority_id IS NULL
                AND repository_owner IS NULL
                AND repo_name IS NULL
                AND access_mode IS NULL
            )
        ),
    CONSTRAINT managed_repository_legacy_completion_shape_check
        CHECK (
            (state = 'completed' AND result_kind IS NOT NULL
                                 AND completed_at IS NOT NULL)
            OR
            (state <> 'completed' AND result_kind IS NULL
                                  AND completed_at IS NULL)
    )
);

CREATE TABLE public.managed_repository_legacy_reconciliation_rearms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_id UUID NOT NULL REFERENCES
        public.managed_repository_legacy_reconciliations(id) ON DELETE RESTRICT,
    generation INTEGER NOT NULL,
    actor_id UUID NOT NULL,
    reason_code TEXT NOT NULL,
    attempts_in_generation INTEGER NOT NULL,
    lifetime_attempts INTEGER NOT NULL,
    failure_reason_code TEXT,
    rearmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT managed_repository_legacy_rearm_unique
        UNIQUE (reconciliation_id, generation),
    CONSTRAINT managed_repository_legacy_rearm_request_unique
        UNIQUE (reconciliation_id, actor_id, reason_code),
    CONSTRAINT managed_repository_legacy_rearm_generation_positive
        CHECK (generation > 0),
    CONSTRAINT managed_repository_legacy_rearm_attempts_check
        CHECK (
            attempts_in_generation >= 0
            AND lifetime_attempts >= attempts_in_generation
        ),
    CONSTRAINT managed_repository_legacy_rearm_reason_check
        CHECK (reason_code ~ '^[a-z0-9][a-z0-9_.-]{0,99}$')
);

CREATE FUNCTION public.protect_managed_repository_legacy_rearm_history()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'managed repository reconciliation re-arms are append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER managed_repository_legacy_rearms_append_only
BEFORE UPDATE OR DELETE
ON public.managed_repository_legacy_reconciliation_rearms
FOR EACH ROW EXECUTE FUNCTION
    public.protect_managed_repository_legacy_rearm_history();

CREATE FUNCTION public.lock_managed_repository_job_lineage_on_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    lineage_root UUID;
    confirmed_root UUID;
BEGIN
    IF NEW.parent_job_id IS NULL THEN
        lineage_root := NEW.id;
    ELSE
        WITH RECURSIVE ancestors AS (
            SELECT job.id, job.parent_job_id
              FROM public.jobs AS job
             WHERE job.id = NEW.parent_job_id
            UNION
            SELECT parent.id, parent.parent_job_id
              FROM public.jobs AS parent
              JOIN ancestors AS child ON parent.id = child.parent_job_id
        )
        SELECT id INTO lineage_root
          FROM ancestors
         WHERE parent_job_id IS NULL;
        IF lineage_root IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'managed_repository_job_lineage_invalid',
                MESSAGE = 'Job parent lineage has no authoritative root';
        END IF;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(
        'managed_repository_job_lineage:' || lineage_root::text,
        0
    ));

    IF NEW.parent_job_id IS NOT NULL THEN
        WITH RECURSIVE ancestors AS (
            SELECT job.id, job.parent_job_id
              FROM public.jobs AS job
             WHERE job.id = NEW.parent_job_id
            UNION
            SELECT parent.id, parent.parent_job_id
              FROM public.jobs AS parent
              JOIN ancestors AS child ON parent.id = child.parent_job_id
        )
        SELECT id INTO confirmed_root
          FROM ancestors
         WHERE parent_job_id IS NULL;
        IF confirmed_root IS DISTINCT FROM lineage_root THEN
            RAISE EXCEPTION USING
                ERRCODE = '40001',
                MESSAGE = 'Job parent lineage changed during admission';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_managed_repository_job_lineage_admission
BEFORE INSERT ON public.jobs
FOR EACH ROW EXECUTE FUNCTION
    public.lock_managed_repository_job_lineage_on_insert();

CREATE FUNCTION public.enforce_officer_post_thread_repository_authority()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    repository_name TEXT;
    repository_url TEXT;
    authority_record UUID;
BEGIN
    IF NEW.thread_id IS NULL
       OR (TG_OP = 'UPDATE' AND NEW.thread_id IS NOT DISTINCT FROM OLD.thread_id)
    THEN
        RETURN NEW;
    END IF;

    SELECT thread.metadata->'workspace_container'->>'repo_name',
           thread.metadata->'workspace_container'->>'git_remote_url'
      INTO repository_name, repository_url
      FROM public.threads AS thread
     WHERE thread.id = NEW.thread_id;

    IF repository_name IS NOT NULL THEN
        SELECT authority.id
          INTO authority_record
          FROM public.managed_repository_authorities AS authority
         WHERE authority.authority_kind = 'thread'
           AND authority.authority_id = NEW.thread_id
           AND authority.project_id = NEW.project_id
           AND authority.repo_name = repository_name
           AND authority.access_mode = 'write'
           AND authority.status = 'active'
           AND authority.clean_repo_url = repository_url
         FOR KEY SHARE;
    END IF;

    IF repository_name IS NOT NULL AND authority_record IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'officer_post_requires_repository_authority',
            MESSAGE = 'Officer thread repository authority is not active',
            HINT = 'Adopt or provision the repository before commissioning.';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_officer_post_thread_repository_authority
BEFORE INSERT OR UPDATE OF thread_id
ON public.project_officers
FOR EACH ROW EXECUTE FUNCTION
    public.enforce_officer_post_thread_repository_authority();

CREATE INDEX idx_managed_repository_legacy_reconcile_due
    ON public.managed_repository_legacy_reconciliations (
        next_attempt_at, updated_at, id
    )
    WHERE state IN ('pending', 'retry', 'claimed');

CREATE INDEX idx_managed_repository_legacy_reconcile_progress
    ON public.managed_repository_legacy_reconciliations (
        state, classification, updated_at, id
    );

COMMENT ON TABLE public.managed_repository_legacy_reconciliations IS
    'Server-owned, restart-safe intent and leased progress for legacy managed '
    'repository adoption or terminal credential-URL scrubbing. It stores no '
    'raw URL, credential, private key, ciphertext, or transport endpoint.';

COMMENT ON COLUMN public.managed_repository_legacy_reconciliations.claim_token IS
    'Never-reused settlement generation. A predecessor cannot acknowledge a '
    'claim reclaimed after lease expiry.';

COMMENT ON COLUMN public.managed_repository_legacy_reconciliations.lifetime_attempts IS
    'Monotonic count across bounded attempt windows and explicit operator '
    're-arms. Unlike attempts, this value is never reset.';

COMMENT ON TABLE public.managed_repository_legacy_reconciliation_rearms IS
    'Append-only attribution for exact-scope operator re-arms. It stores the '
    'actor, non-secret reason, failed attempt window, and cumulative attempts; '
    'it contains no repository coordinate or credential material.';

COMMIT;
