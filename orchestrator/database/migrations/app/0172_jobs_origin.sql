-- migration:     0172_jobs_origin.sql
-- description:   Stamp where a job came from. GET /api/jobs returns a flat
--                list in which a job a user typed into the cockpit, a critic
--                subjob, a loop iteration, an automation fire and an officer
--                dispatch are indistinguishable without inspecting each row's
--                JSONB — which makes the list unreadable on any deployment
--                running unattended work, and makes "how much of our spend is
--                unattended?" a bespoke query every time.
--
--                Values: user|session|automation|loop|officer|subjob|
--                lifecycle|bench. NOT a duplicate of runner_kind: 0053's own
--                description states that "human-created and automation jobs
--                stay runner_kind='user'" because that column is the dispatch
--                grant class, not a provenance record.
--
--                No CHECK constraint, following 0118_jobs_execution_lane's
--                deliberate "App-validated by design" precedent on this same
--                hot table. The concrete cost of a CHECK here would be two
--                further migration files: VALIDATE CONSTRAINT must run in a
--                different transaction from ADD ... NOT VALID, and since the
--                runner wraps each transactional file in exactly one
--                transaction, "different transaction" means "different file"
--                — otherwise the ADD's ACCESS EXCLUSIVE lock is held across
--                the validation scan and NOT VALID buys nothing. create_job()
--                validates against KNOWN_JOB_ORIGINS instead, so a typo fails
--                loudly in tests and a new value needs no migration.
--
--                knowledge-base/knowledge/features/job_origin_provenance.md §3, §4.
-- depends-on:    0171_officer_runtime_grant_liveness.sql
-- expected:      < 1s. ADD COLUMN ... NOT NULL DEFAULT '<constant>' is
--                catalog-only since PG 11 — no table rewrite. The backfill is
--                a single unbatched UPDATE, consistent with every migration in
--                this repo (none has ever batched); jobs is ~150 rows on k3d
--                and ~800 on the dev cluster, so the write is trivial. If a
--                deployment's jobs table is materially larger, revisit: the
--                alternative is a ctid-ranged batch loop, which would force
--                this file non-transactional and therefore single-statement.
-- locks:         Brief ACCESS EXCLUSIVE on jobs for the ADD COLUMN, bounded by
--                lock_timeout and retried with jittered backoff. The backfill
--                UPDATE then takes ROW EXCLUSIVE for the duration.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

DO $$
DECLARE
    max_attempts CONSTANT int    := 30;
    cap_ms       CONSTANT bigint := 60000;
    base_ms      CONSTANT bigint := 10;
    delay_ms              bigint;
    done                  boolean := false;
BEGIN
    FOR i IN 1..max_attempts LOOP
        BEGIN
            ALTER TABLE jobs
                ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'user';
            done := true;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            delay_ms := round(random() * least(cap_ms, base_ms * 2 ^ i));
            PERFORM pg_sleep(delay_ms::numeric / 1000);
        END;
    END LOOP;
    IF NOT done THEN
        RAISE EXCEPTION 'lock acquisition failed on jobs after % attempts',
            max_attempts;
    END IF;
END $$;

-- Backfill from the signals that already exist. Every one is derivable at
-- write time by the caller, which is what makes this lossless.
--
-- THE ORDER IS LOAD-BEARING. Do not "tidy" it. Verified against the k3d
-- dataset (149 rows) on 2026-08-20 rather than assumed:
--
--   * officer MUST precede session. Officer admission sets
--     created_by_thread_id to the officer's own thread, so with the session
--     arm ahead of it every officer dispatch in history backfills as
--     'session'. (Unexercised on k3d — zero officer_admission rows — so this
--     arm is reasoned, not measured. Kept because the write path is real.)
--   * subjob MUST precede lifecycle, and 'lifecycle' consequently receives
--     ZERO rows. That is correct, not a bug, and it is worth stating because
--     it looks exactly like one. All 11 runner_kind='lifecycle' rows also
--     carry parent_job_id, so the arms collide; inspecting them settles it —
--     they are 4 "Research phase for: …" (scholar) and 7 "Verify deliverables
--     of job …" (critic), i.e. subjobs. Scholar and critic creation simply
--     pass runner_kind='lifecycle', because that column is the dispatch GRANT
--     class (it raises the autonomy ceiling), not a provenance record. So
--     'lifecycle' has no owner today: nothing in the tree creates a job that
--     is a system lifecycle job rather than a scholar/critic child. The value
--     stays in the vocabulary as reserved — origin is app-validated, so
--     giving it an owner later costs no migration.
--   * parent_job_id MUST precede created_by_thread_id, because a subjob of a
--     session-created job is a subjob first. (Also unexercised here — the two
--     never co-occur on k3d.)
--
-- Two arms the original plan did not have, both found by querying the data:
--
--   * 'bench' is NOT unrecoverable. The plan recorded that historic bench
--     runs "have no distinguishing marker" and would land on 'user'; in fact
--     38 rows — 26% of the table — carry context->'bench' with the campaign
--     arm, task and run_id. Classifying them as human 'user' work would have
--     put benchmark traffic in every user's job list and in their spend
--     attribution, which is precisely what §8.2 gave bench its own value to
--     prevent.
--   * the officer arm reads officer_slot as well as officer_admission, which
--     is what 0162_officer_ticket_claims.sql already does. The single officer
--     dispatch on k3d carries only officer_slot and would otherwise have
--     backfilled as 'session' — the exact misclassification the officer
--     precedence exists to stop, arriving through a key the plan did not list.
--
-- Context is NOT inherited by children (verified: zero rows have
-- parent_job_id together with loop_id or bench), so the marker arms sitting
-- above the parent arm cannot steal a subjob from its parent. If that ever
-- changes, this order must be revisited — origin records the IMMEDIATE
-- creator, so a critic subjob of a loop iteration is 'subjob', not 'loop'.
--
-- Guarded by origin = 'user' so a re-run after a partial apply cannot
-- reclassify rows already stamped correctly by create_job().
UPDATE jobs
   SET origin = CASE
           WHEN context ?| array['officer_admission', 'officer_slot']
                                                 THEN 'officer'
           WHEN context ? 'automation_id'        THEN 'automation'
           WHEN context ? 'loop_id'              THEN 'loop'
           WHEN context ? 'bench'                THEN 'bench'
           WHEN parent_job_id IS NOT NULL        THEN 'subjob'
           WHEN runner_kind = 'lifecycle'        THEN 'lifecycle'
           WHEN created_by_thread_id IS NOT NULL THEN 'session'
           ELSE 'user'
       END
 WHERE origin = 'user';

COMMENT ON COLUMN jobs.origin IS
    'Where this job came from: user|session|automation|loop|officer|subjob|'
    'lifecycle|bench. Records the IMMEDIATE creator, not the root of the '
    'chain — a critic subjob of a loop iteration is ''subjob'', and the chain '
    'stays reconstructable through parent_job_id. Stamped explicitly by each '
    'caller of create_job() and validated there against KNOWN_JOB_ORIGINS; '
    'there is deliberately no CHECK constraint (see 0118 precedent). Distinct '
    'from runner_kind, which is the dispatch grant class.';

COMMIT;
