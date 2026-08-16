-- migration:     0162_officer_ticket_claims.sql
-- description:   Durable, project-scoped Officer backlog-ticket claims. A
--                claim outlives physical job deletion; only a newer trusted
--                ready_at generation can be claimed again, and an extant or
--                deleted non-terminal predecessor remains a blocker.
--                docs/issues/deleting_a_job_releases_its_backlog_ticket_claim.md
-- depends-on:    0161_runtime_actor_credentials.sql
-- expected:      seconds. One small table plus a fail-closed scan/backfill of
--                all project-scoped jobs carrying ticket_note_id. History
--                without a provable ready generation becomes an unversioned
--                cutover barrier; the migration never guesses a generation.
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
    ready_generation_at          TIMESTAMPTZ,
    claimed_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    source                       TEXT NOT NULL,

    -- Durable values deliberately have no jobs/threads FKs. Operational rows
    -- may be physically removed while dispatch provenance must remain.
    officer_thread_id            UUID,
    officer_incarnation          INTEGER,
    officer_slot                 TEXT,
    work_category                TEXT,
    admission_config_fingerprint TEXT,
    admission_lineage_size       INTEGER,
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
    CONSTRAINT officer_ticket_claim_generation_finite
        CHECK (ready_generation_at IS NULL OR isfinite(ready_generation_at)),
    CONSTRAINT officer_ticket_claim_incarnation_valid
        CHECK (officer_incarnation IS NULL OR officer_incarnation >= 0),
    CONSTRAINT officer_ticket_claim_fingerprint_valid
        CHECK (
            admission_config_fingerprint IS NULL
            OR admission_config_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    CONSTRAINT officer_ticket_claim_lineage_size_valid
        CHECK (
            admission_lineage_size IS NULL
            OR admission_lineage_size = officer_incarnation + 1
        ),
    CONSTRAINT officer_ticket_claim_authority_shape
        CHECK (
            (
                source = 'legacy_unversioned'
                AND ready_generation_at IS NULL
                AND officer_incarnation IS NULL
                AND admission_config_fingerprint IS NULL
                AND admission_lineage_size IS NULL
            )
            OR
            (
                source <> 'legacy_unversioned'
                AND ready_generation_at IS NOT NULL
                AND officer_thread_id IS NOT NULL
                AND officer_incarnation IS NOT NULL
                AND admission_config_fingerprint IS NOT NULL
                AND admission_lineage_size IS NOT NULL
            )
        ),
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
    'NULL only for source=legacy_unversioned: claimed_at is then the database '
    'cutover barrier and no historical generation is guessed.';
COMMENT ON COLUMN public.officer_ticket_claims.claimed_at IS
    'Claim time. For source=legacy_unversioned this is the server cutover '
    'timestamp and the ticket must be explicitly re-readied strictly later.';
COMMENT ON COLUMN public.officer_ticket_claims.job_id IS
    'Durable job identity without a jobs FK so physical deletion cannot erase '
    'or null claim history.';
COMMENT ON COLUMN public.officer_ticket_claims.job_status_at_delete IS
    'Status observed under the jobs row lock immediately before deletion. A '
    'non-terminal value remains a later-generation admission blocker.';

-- Backfill every project-scoped pre-cutover ticket job without promoting
-- unverifiable context to authority. A complete, internally consistent stamp
-- keeps its trusted generation as source=backfill. Any missing, partial,
-- malformed, or contradictory stamp becomes source=legacy_unversioned with a
-- NULL generation and transaction_timestamp() as its re-arm barrier. That is
-- conservative: it can delay work until an explicit post-cutover re-ready but
-- can never authorize duplicate work from a guessed historical timestamp.
DO $backfill$
DECLARE
    candidate              RECORD;
    generation             TIMESTAMPTZ;
    incarnation            INTEGER;
    lineage_size           INTEGER;
    expected_incarnation   INTEGER;
    admission_project_id   UUID;
    admission_thread_id    UUID;
    stamp_parsed           BOOLEAN;
    stamp_verified         BOOLEAN;
    collision_job_id       UUID;
    collision_status       TEXT;
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

        generation := NULL;
        incarnation := NULL;
        lineage_size := NULL;
        expected_incarnation := NULL;
        admission_project_id := NULL;
        admission_thread_id := NULL;
        stamp_parsed := FALSE;
        stamp_verified := FALSE;

        IF candidate.created_by_thread_id IS NOT NULL
           AND jsonb_typeof(candidate.admission) = 'object' THEN
            BEGIN
                admission_project_id :=
                    (candidate.admission->>'project_id')::uuid;
                admission_thread_id :=
                    (candidate.admission->>'thread_id')::uuid;
                generation :=
                    (candidate.admission->>'ticket_ready_at')::timestamptz;
                incarnation :=
                    (candidate.admission->>'incarnation')::integer;
                lineage_size :=
                    (candidate.admission->>'lineage_size')::integer;
                stamp_parsed := TRUE;
            EXCEPTION
                WHEN invalid_text_representation
                   OR invalid_datetime_format
                   OR datetime_field_overflow
                   OR numeric_value_out_of_range THEN
                    stamp_parsed := FALSE;
            END;
        END IF;

        IF stamp_parsed
           AND admission_project_id IS NOT NULL
           AND admission_thread_id IS NOT NULL
           AND admission_project_id IS NOT DISTINCT FROM candidate.project_id
           AND admission_thread_id IS NOT DISTINCT FROM candidate.created_by_thread_id
           AND generation IS NOT NULL
           AND isfinite(generation)
           AND incarnation IS NOT NULL
           AND incarnation >= 0
           AND lineage_size IS NOT NULL
           AND lineage_size IS NOT DISTINCT FROM incarnation + 1
           AND COALESCE(candidate.admission->>'config_fingerprint', '')
                  ~ '^[0-9a-f]{64}$'
           AND (candidate.admission->>'slot')
                  IS NOT DISTINCT FROM candidate.officer_slot
           AND (candidate.admission->>'category')
                  IS NOT DISTINCT FROM candidate.work_category
           AND candidate.incarnations IS NOT NULL THEN
            IF candidate.current_officer_thread_id = candidate.created_by_thread_id THEN
                expected_incarnation :=
                    jsonb_array_length(candidate.incarnations);
            ELSE
                SELECT history.ordinality::integer - 1
                  INTO expected_incarnation
                  FROM jsonb_array_elements(candidate.incarnations)
                       WITH ORDINALITY AS history(entry, ordinality)
                 WHERE history.entry->>'thread_id' =
                       candidate.created_by_thread_id::text
                 ORDER BY history.ordinality
                 LIMIT 1;
            END IF;
            stamp_verified := expected_incarnation IS NOT NULL
                              AND incarnation IS NOT DISTINCT FROM
                                  expected_incarnation;
        END IF;

        IF stamp_verified THEN
            collision_job_id := NULL;
            collision_status := NULL;
            SELECT claim.job_id,
                   COALESCE(live.status, claim.job_status_at_delete, 'unknown')
              INTO collision_job_id, collision_status
              FROM public.officer_ticket_claims claim
              LEFT JOIN public.jobs live ON live.id = claim.job_id
             WHERE claim.project_id = candidate.project_id
               AND claim.ticket_note_id = candidate.ticket_note_id
               AND claim.ready_generation_at = generation
             LIMIT 1;
            IF collision_job_id IS NOT NULL THEN
                RAISE EXCEPTION 'BP-05 backfill collision for project %, ticket %, generation %: jobs [%:%, %:%]',
                    candidate.project_id,
                    candidate.ticket_note_id,
                    generation,
                    collision_job_id,
                    collision_status,
                    candidate.job_id,
                    candidate.status
                    USING ERRCODE = 'unique_violation',
                          HINT = 'Reconcile the conflicting verified historical jobs explicitly; migration 0162 will not choose an authoritative claim.';
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
            ON CONFLICT (job_id) DO NOTHING;
        ELSE
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
                NULL,
                transaction_timestamp(),
                'legacy_unversioned',
                candidate.created_by_thread_id,
                NULL,
                candidate.officer_slot,
                candidate.work_category,
                NULL,
                NULL,
                candidate.job_id
            )
            ON CONFLICT (job_id) DO NOTHING;
        END IF;
    END LOOP;
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

    -- These rows are a quarantine boundary, not recovered admission
    -- authority. Preserve the observable job identity while allowing ordinary
    -- context merges against genuine stamp-less/partial historical rows. Any
    -- admission-looking JSON on such a job remains non-authoritative because
    -- the immutable ledger source/generation shape, not jobs.context, governs
    -- eligibility. Only a new post-locked claim can consume a post-cutover
    -- ready_at.
    IF durable_claim.source = 'legacy_unversioned' THEN
        IF NEW.project_id IS NULL
           OR durable_claim.project_id IS DISTINCT FROM NEW.project_id
           OR durable_claim.ticket_note_id
                  IS DISTINCT FROM NEW.context->>'ticket_note_id'
           OR (
               durable_claim.officer_thread_id IS NOT NULL
               AND durable_claim.officer_thread_id
                      IS DISTINCT FROM NEW.created_by_thread_id
           )
           OR durable_claim.officer_slot
                  IS DISTINCT FROM NEW.context->>'officer_slot'
           OR durable_claim.work_category
                  IS DISTINCT FROM NEW.context->>'work_category' THEN
            RAISE EXCEPTION 'legacy ticket-bearing job % does not match its durable cutover barrier', NEW.id
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'officer_ticket_claim_job_integrity';
        END IF;
        RETURN NEW;
    END IF;

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
    '0162 rolling-upgrade backstop: a ticket-bearing jobs row must match a durable claim already visible in the same transaction; legacy_unversioned rows remain non-authoritative cutover barriers.';
COMMENT ON FUNCTION public.audit_officer_ticket_claim_job_delete() IS
    '0162 rolling-upgrade backstop: any claimed job DELETE records status/time before the operational row disappears.';

COMMIT;
