-- migration:     0162_officer_ticket_claims.sql
-- description:   Durable, project-scoped Officer backlog-ticket claims. A
--                claim outlives physical job deletion; only a newer trusted
--                ready_at generation can be claimed again, and an extant or
--                deleted non-terminal predecessor remains a blocker.
--                docs/issues/deleting_a_job_releases_its_backlog_ticket_claim.md
-- depends-on:    0161_runtime_actor_credentials.sql
-- expected:      seconds. One small table plus a fail-closed scan/backfill of
--                verifiable Officer-admitted jobs carrying ticket_note_id.
-- locks:         SHARE ROW EXCLUSIVE on jobs is acquired before the ledger is
--                created and held through backfill + trigger installation.
--                That lock is the rolling-upgrade cut: pre-lock INSERTs commit
--                before the scan; post-commit INSERT/DELETE statements see the
--                integrity/audit triggers. Runtime admission remains post ->
--                claim -> new job; deletion remains existing job -> claim.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Mechanically quiesce job writers for the whole compatibility cutover. The
-- migration never locks project_officers or threads, so it cannot invert the
-- runtime post -> current thread -> claim/jobs order. A busy rollout retries
-- rather than accepting a partially governed ticket job.
LOCK TABLE public.jobs IN SHARE ROW EXCLUSIVE MODE;

CREATE TABLE public.officer_ticket_claims (
    id                           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id                   UUID NOT NULL
                                     REFERENCES public.projects(id) ON DELETE CASCADE,
    ticket_note_id               TEXT NOT NULL,
    ready_generation_at          TIMESTAMPTZ NOT NULL,
    claimed_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    source                       TEXT NOT NULL,

    -- Durable values deliberately have no jobs/threads FKs. Operational rows
    -- may be physically removed while dispatch provenance must remain.
    officer_thread_id            UUID NOT NULL,
    officer_incarnation          INTEGER NOT NULL,
    officer_slot                 TEXT,
    work_category                TEXT,
    admission_config_fingerprint TEXT NOT NULL,
    admission_lineage_size       INTEGER NOT NULL,
    job_id                       UUID NOT NULL,

    -- Deletion is an audit event, never a claim release. A non-terminal value
    -- here remains a blocker even after the jobs row is gone.
    job_deleted_at               TIMESTAMPTZ,
    job_status_at_delete         TEXT,
    deletion_actor_user_id       UUID,
    deletion_reason              TEXT,

    CONSTRAINT officer_ticket_claim_ticket_nonempty
        CHECK (btrim(ticket_note_id) <> ''),
    CONSTRAINT officer_ticket_claim_source_nonempty
        CHECK (btrim(source) <> ''),
    CONSTRAINT officer_ticket_claim_incarnation_valid
        CHECK (officer_incarnation >= 0),
    CONSTRAINT officer_ticket_claim_fingerprint_valid
        CHECK (admission_config_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT officer_ticket_claim_lineage_size_valid
        CHECK (admission_lineage_size = officer_incarnation + 1),
    CONSTRAINT uq_officer_ticket_claim_generation
        UNIQUE (project_id, ticket_note_id, ready_generation_at),
    CONSTRAINT uq_officer_ticket_claim_job UNIQUE (job_id)
);

CREATE INDEX idx_officer_ticket_claims_project_ticket
    ON public.officer_ticket_claims
       (project_id, ticket_note_id, ready_generation_at DESC, claimed_at DESC);

-- Permanent history consumers start with one or more incarnation ids, narrow
-- optionally by slot, and read newest claims first. This prevents that bounded
-- query from degrading into a global ledger scan as retention grows.
CREATE INDEX idx_officer_ticket_claims_lineage_slot_claimed
    ON public.officer_ticket_claims
       (officer_thread_id, officer_slot, claimed_at DESC)
    WHERE officer_thread_id IS NOT NULL;

COMMENT ON TABLE public.officer_ticket_claims IS
    'Durable Officer backlog claim ledger. Claim identities survive job/thread deletion; '
    'job_deleted_at is audit only and never re-arms a ticket (BP-05).';
COMMENT ON COLUMN public.officer_ticket_claims.ready_generation_at IS
    'The server-resolved Officer ready_at generation consumed by this claim. '
    'Backfill accepts only a valid server admission stamp; it never trusts a '
    'model timestamp or guesses from job.created_at.';
COMMENT ON COLUMN public.officer_ticket_claims.job_id IS
    'Durable job identity without a jobs FK so physical deletion cannot erase '
    'or null claim history.';
COMMENT ON COLUMN public.officer_ticket_claims.job_status_at_delete IS
    'Status observed under the jobs row lock immediately before deletion. A '
    'non-terminal value remains a later-generation admission blocker.';

-- Backfill every *verifiable* pre-cutover ticket job. The deployed pre-0162
-- tick wrote the full Officer admission stamp plus ticket_ready_at, whereas
-- raw context accepted by an ordinary/manual request is indistinguishable
-- from caller prose when that trusted generation is absent. Refuse such rows
-- with job-specific diagnostics: an operator must reconcile them before
-- rollout rather than this migration silently promoting them to authority.
DO $backfill$
DECLARE
    candidate              RECORD;
    generation             TIMESTAMPTZ;
    incarnation            INTEGER;
    lineage_size           INTEGER;
    expected_incarnation   INTEGER;
    admission_project_id   UUID;
    admission_thread_id    UUID;
    collision              RECORD;
BEGIN
    FOR candidate IN
        SELECT j.id AS job_id,
               j.project_id,
               j.created_by_thread_id,
               j.created_at,
               j.status,
               j.context->>'ticket_note_id' AS ticket_note_id,
               j.context->>'officer_slot' AS officer_slot,
               j.context->>'work_category' AS work_category,
               j.context->'officer_admission' AS admission,
               po.thread_id AS current_officer_thread_id,
               po.incarnations
          FROM public.jobs j
          LEFT JOIN public.officer_ticket_claims existing
            ON existing.job_id = j.id
          LEFT JOIN public.project_officers po
            ON po.project_id = j.project_id
         WHERE existing.job_id IS NULL
           AND j.context ? 'ticket_note_id'
         ORDER BY j.created_at, j.id
    LOOP
        IF candidate.project_id IS NULL THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: project_id is NULL',
                candidate.job_id
                USING ERRCODE = 'check_violation',
                      HINT = 'Remove forged claim context or reconcile the job to a verified Officer ticket before retrying migration 0162.';
        END IF;
        IF btrim(COALESCE(candidate.ticket_note_id, '')) = '' THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: ticket_note_id is blank',
                candidate.job_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF candidate.created_by_thread_id IS NULL THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: created_by_thread_id is missing',
                candidate.job_id
                USING ERRCODE = 'check_violation',
                      HINT = 'The row cannot be proven to have crossed authoritative Officer admission; remove forged context or reconcile it explicitly.';
        END IF;
        IF jsonb_typeof(candidate.admission) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: officer_admission is missing or malformed',
                candidate.job_id
                USING ERRCODE = 'check_violation';
        END IF;

        BEGIN
            admission_project_id := (candidate.admission->>'project_id')::uuid;
            admission_thread_id := (candidate.admission->>'thread_id')::uuid;
            generation := (candidate.admission->>'ticket_ready_at')::timestamptz;
            incarnation := (candidate.admission->>'incarnation')::integer;
            lineage_size := (candidate.admission->>'lineage_size')::integer;
        EXCEPTION
            WHEN invalid_text_representation
               OR invalid_datetime_format
               OR datetime_field_overflow
               OR numeric_value_out_of_range THEN
                RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: admission identity/generation/incarnation is invalid',
                    candidate.job_id
                    USING ERRCODE = 'check_violation',
                          HINT = 'Migration 0162 accepts no job.created_at fallback and no model-authored ready timestamp.';
        END;

        IF admission_project_id IS NULL
           OR admission_thread_id IS NULL
           OR admission_project_id IS DISTINCT FROM candidate.project_id
           OR admission_thread_id IS DISTINCT FROM candidate.created_by_thread_id THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: admission project/thread disagrees with the job row',
                candidate.job_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF generation IS NULL OR NOT isfinite(generation) THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: ticket_ready_at is missing or not finite',
                candidate.job_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF incarnation IS NULL OR incarnation < 0
           OR lineage_size IS NULL
           OR lineage_size IS DISTINCT FROM incarnation + 1 THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: admission incarnation/lineage is missing or invalid',
                candidate.job_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF COALESCE(candidate.admission->>'config_fingerprint', '')
               !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: admission fingerprint/lineage is invalid',
                candidate.job_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF (candidate.admission->>'slot') IS DISTINCT FROM candidate.officer_slot
           OR (candidate.admission->>'category')
                  IS DISTINCT FROM candidate.work_category THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: admission slot/category disagrees with job context',
                candidate.job_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF candidate.incarnations IS NULL THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: project has no durable Officer Post',
                candidate.job_id
                USING ERRCODE = 'check_violation';
        END IF;

        IF candidate.current_officer_thread_id = candidate.created_by_thread_id THEN
            expected_incarnation := jsonb_array_length(candidate.incarnations);
        ELSE
            SELECT history.ordinality::integer - 1
              INTO expected_incarnation
              FROM jsonb_array_elements(candidate.incarnations)
                   WITH ORDINALITY AS history(entry, ordinality)
             WHERE history.entry->>'thread_id' = candidate.created_by_thread_id::text
             ORDER BY history.ordinality
             LIMIT 1;
        END IF;
        IF expected_incarnation IS NULL
           OR incarnation IS DISTINCT FROM expected_incarnation THEN
            RAISE EXCEPTION 'BP-05 preflight rejected ticket job %: thread/incarnation is not in the durable post lineage',
                candidate.job_id
                USING ERRCODE = 'check_violation';
        END IF;

        INSERT INTO public.officer_ticket_claims (
            project_id,
            ticket_note_id,
            ready_generation_at,
            claimed_at,
            source,
            officer_thread_id,
            officer_incarnation,
            officer_slot,
            work_category,
            admission_config_fingerprint,
            admission_lineage_size,
            job_id
        ) VALUES (
            candidate.project_id,
            candidate.ticket_note_id,
            generation,
            candidate.created_at,
            'backfill',
            candidate.created_by_thread_id,
            incarnation,
            candidate.officer_slot,
            candidate.work_category,
            candidate.admission->>'config_fingerprint',
            lineage_size,
            candidate.job_id
        )
        -- Idempotency is job-specific. A generation collision with a different
        -- job must hit uq_officer_ticket_claim_generation and abort loudly.
        ON CONFLICT (job_id) DO NOTHING;
    END LOOP;
EXCEPTION
    WHEN unique_violation THEN
        SELECT grouped.project_id,
               grouped.ticket_note_id,
               grouped.ready_generation_at,
               grouped.jobs
          INTO collision
          FROM (
              SELECT j.project_id,
                     j.context->>'ticket_note_id' AS ticket_note_id,
                     (j.context->'officer_admission'->>'ticket_ready_at')::timestamptz
                         AS ready_generation_at,
                     string_agg(j.id::text || ':' || j.status, ', ' ORDER BY j.id)
                         AS jobs
                FROM public.jobs j
               WHERE j.context ? 'ticket_note_id'
                 AND COALESCE(j.context->'officer_admission'->>'ticket_ready_at', '') <> ''
               GROUP BY j.project_id,
                        j.context->>'ticket_note_id',
                        (j.context->'officer_admission'->>'ticket_ready_at')::timestamptz
              HAVING COUNT(*) > 1
               ORDER BY j.project_id, j.context->>'ticket_note_id'
               LIMIT 1
          ) AS grouped;
        IF collision.project_id IS NOT NULL THEN
            RAISE EXCEPTION 'BP-05 backfill collision for project %, ticket %, generation %: jobs [%]',
                collision.project_id,
                collision.ticket_note_id,
                collision.ready_generation_at,
                collision.jobs
                USING ERRCODE = 'unique_violation',
                      HINT = 'Reconcile the conflicting historical jobs explicitly; migration 0162 will not choose an authoritative claim.';
        END IF;
        RAISE;
END
$backfill$;

-- Ticket identity is server-owned. The new application inserts the immutable
-- claim first and then the exact job UUID in one post-locked transaction. An
-- old replica (or future direct writer) that attempts only the jobs INSERT is
-- rejected atomically; it cannot leave a ticket-shaped row without a claim.
CREATE FUNCTION public.enforce_officer_ticket_claim_job_integrity()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    durable_claim public.officer_ticket_claims%ROWTYPE;
    admission     JSONB;
    generation    TIMESTAMPTZ;
    incarnation   INTEGER;
    lineage_size  INTEGER;
    claim_exists  BOOLEAN;
BEGIN
    SELECT *
      INTO durable_claim
      FROM public.officer_ticket_claims
     WHERE job_id = NEW.id;
    claim_exists := FOUND;

    IF NOT (COALESCE(NEW.context, '{}'::jsonb) ? 'ticket_note_id') THEN
        IF claim_exists THEN
            RAISE EXCEPTION 'claimed job % cannot remove its server-owned ticket/admission provenance', NEW.id
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'officer_ticket_claim_job_integrity';
        END IF;
        RETURN NEW;
    END IF;

    IF NOT claim_exists THEN
        RAISE EXCEPTION 'ticket-bearing job % has no durable Officer claim; retry after rolling upgrade', NEW.id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'officer_ticket_claim_job_integrity',
                  HINT = 'Old replicas cannot dispatch ticket work after migration 0162; use the post-locked claim+job admission path.';
    END IF;

    admission := COALESCE(NEW.context, '{}'::jsonb)->'officer_admission';
    IF jsonb_typeof(admission) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'ticket-bearing job % has no server Officer admission provenance', NEW.id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'officer_ticket_claim_job_integrity';
    END IF;
    BEGIN
        generation := (admission->>'ticket_ready_at')::timestamptz;
        incarnation := (admission->>'incarnation')::integer;
        lineage_size := (admission->>'lineage_size')::integer;
    EXCEPTION
        WHEN invalid_text_representation
           OR invalid_datetime_format
           OR datetime_field_overflow
           OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'ticket-bearing job % has invalid server Officer admission provenance', NEW.id
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'officer_ticket_claim_job_integrity';
    END;

    IF generation IS NULL OR NOT isfinite(generation)
       OR incarnation IS NULL OR incarnation < 0
       OR lineage_size IS NULL
       OR lineage_size IS DISTINCT FROM incarnation + 1 THEN
        RAISE EXCEPTION 'ticket-bearing job % has missing or invalid Officer generation/incarnation/lineage provenance', NEW.id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'officer_ticket_claim_job_integrity';
    END IF;

    IF NEW.project_id IS NULL
       OR durable_claim.project_id IS DISTINCT FROM NEW.project_id
       OR durable_claim.ticket_note_id
              IS DISTINCT FROM NEW.context->>'ticket_note_id'
       OR durable_claim.ready_generation_at IS DISTINCT FROM generation
       OR (
           durable_claim.source = 'backfill'
           AND admission ? 'ticket_claim_source'
       )
       OR (
           durable_claim.source <> 'backfill'
           AND durable_claim.source
                  IS DISTINCT FROM admission->>'ticket_claim_source'
       )
       OR durable_claim.officer_thread_id
              IS DISTINCT FROM NEW.created_by_thread_id
       OR durable_claim.officer_thread_id::text
              IS DISTINCT FROM admission->>'thread_id'
       OR durable_claim.project_id::text
              IS DISTINCT FROM admission->>'project_id'
       OR durable_claim.officer_incarnation IS DISTINCT FROM incarnation
       OR durable_claim.officer_slot
              IS DISTINCT FROM NEW.context->>'officer_slot'
       OR durable_claim.officer_slot
              IS DISTINCT FROM admission->>'slot'
       OR durable_claim.work_category
              IS DISTINCT FROM NEW.context->>'work_category'
       OR durable_claim.work_category
              IS DISTINCT FROM admission->>'category'
       OR durable_claim.admission_config_fingerprint
              IS DISTINCT FROM admission->>'config_fingerprint'
       OR durable_claim.admission_lineage_size IS DISTINCT FROM lineage_size THEN
        RAISE EXCEPTION 'ticket-bearing job % does not match its durable Officer claim', NEW.id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'officer_ticket_claim_job_integrity';
    END IF;

    RETURN NEW;
END
$function$;

CREATE CONSTRAINT TRIGGER officer_ticket_claim_job_integrity
AFTER INSERT OR UPDATE OF id, context, project_id, created_by_thread_id
ON public.jobs
DEFERRABLE INITIALLY IMMEDIATE
FOR EACH ROW
EXECUTE FUNCTION public.enforce_officer_ticket_claim_job_integrity();

-- Old application deletion knows nothing about the ledger. Audit from OLD
-- while its jobs row lock is held. The current application may have already
-- supplied actor/reason; COALESCE preserves that richer authority.
CREATE FUNCTION public.audit_officer_ticket_claim_job_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    UPDATE public.officer_ticket_claims
       SET job_deleted_at = COALESCE(job_deleted_at, statement_timestamp()),
           job_status_at_delete = COALESCE(job_status_at_delete, OLD.status),
           deletion_reason = COALESCE(
               deletion_reason,
               'database_delete_compatibility_trigger'
           )
     WHERE job_id = OLD.id;
    RETURN OLD;
END
$function$;

CREATE TRIGGER officer_ticket_claim_job_delete_audit
BEFORE DELETE ON public.jobs
FOR EACH ROW
EXECUTE FUNCTION public.audit_officer_ticket_claim_job_delete();

COMMENT ON FUNCTION public.enforce_officer_ticket_claim_job_integrity() IS
    '0162 rolling-upgrade backstop: a ticket-bearing jobs row must match a durable claim already visible in the same transaction.';
COMMENT ON FUNCTION public.audit_officer_ticket_claim_job_delete() IS
    '0162 rolling-upgrade backstop: any claimed job DELETE records status/time before the operational row disappears.';

COMMIT;
