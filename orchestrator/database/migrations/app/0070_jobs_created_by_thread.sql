-- migration:     0070_jobs_created_by_thread.sql
-- description:   Session ↔ job backref + the wake outbox columns, Phase 1 of
--                docs/features/session_wake_on_job_completion.md.
--
--                `create_worker_job` already passes the creating thread id at
--                creation, but it was only used to inherit user/project/
--                datasource scope and was never persisted. Without a queryable
--                backref the orchestrator cannot answer "which session created
--                this job", so a completing job has nobody to notify and a
--                session cannot ask which jobs are still outstanding.
--
--                The wake_* columns are a single-message-per-row transactional
--                outbox. Nothing in the completion path is transactional
--                (postgres_db.acquire() yields a raw pooled connection; every
--                statement autocommits), so a direct post-commit POST to the
--                agent pod is lost — silently — on a SIGKILL during a rolling
--                deploy. The claim is the mechanism; the post-commit send is a
--                latency optimization layered on top of it, and losing it is
--                harmless because the backstop sweeper re-claims the row after
--                the visibility timeout.
--
--                  wake_state    'none'    → not a wakeable job
--                                'pending' → terminal, wake owed
--                                'sending' → claimed by one replica
--                                'sent'    → delivered (live inject or durable row)
--                                'dead'    → attempt cap exhausted; alert on this
--                  wake_claimed_at  visibility-timeout anchor for re-claim
--                  wake_attempts    retry counter feeding the cap
--                  wake_notified_status  the terminal status last DELIVERED
--
--                wake_notified_status is the dedup key's second half. The key
--                is (job_id, terminal_status), not job_id alone, because
--                pending_review → completed via approve_job is a second,
--                LEGITIMATE wake — the session wants to know a frozen job was
--                approved. Comparing the job's current status against the last
--                delivered one gives that for free and collapses the
--                approve-lands-before-the-send race into a single wake carrying
--                the newer status. jobs.completed_at cannot serve as the key:
--                it is set by an unguarded separate statement, re-set by
--                approve_job, and not set at all by update_job_status(failed).
--
--                wake_on_complete is set SERVER-SIDE (true iff the creator is a
--                session thread), not by the model: an opt-in flag's failure
--                mode is silent — the agent forgets it and then never learns
--                its job finished, which is indistinguishable from the bug this
--                feature exists to fix. It is retained as the off-switch for a
--                future per-project user setting.
-- depends-on:    0069_automation_expert_id.sql
-- expected:      < 1s on dev. VALIDATE CONSTRAINT scans jobs but takes only a
--                SHARE UPDATE EXCLUSIVE lock, so writers keep running.
-- locks:         Brief ACCESS EXCLUSIVE on jobs for the ADD COLUMNs; the FK is
--                added NOT VALID (no scan under that lock) and validated
--                separately under SHARE UPDATE EXCLUSIVE.
-- transactional: yes
-- ============================================================================

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Two-phase FK (NOT VALID then VALIDATE) per docs/db_migration.md — same shape
-- as jobs.expert_id in 0028_experts.sql.
--
-- ON DELETE SET NULL is not cosmetic: threads are genuinely hard-deleted
-- (PostgresDB.delete_thread), so without it those deletes start failing the
-- moment a session has ever created a job.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS created_by_thread_id UUID;
DO $$ BEGIN
    ALTER TABLE jobs ADD CONSTRAINT jobs_created_by_thread_id_fkey
        FOREIGN KEY (created_by_thread_id) REFERENCES threads(id)
        ON DELETE SET NULL NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE jobs VALIDATE CONSTRAINT jobs_created_by_thread_id_fkey;

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS wake_on_complete     BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS wake_state           TEXT        NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS wake_claimed_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS wake_attempts        INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wake_notified_status TEXT;

-- Value guard. Kept as a named constraint so a typo in application code fails
-- loudly at write time instead of parking a row in a state no query matches
-- (an unmatched wake_state is a session that waits forever — exactly the bug
-- this feature removes).
DO $$ BEGIN
    ALTER TABLE jobs ADD CONSTRAINT jobs_wake_state_known
        CHECK (wake_state IN ('none', 'pending', 'sending', 'sent', 'dead'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN jobs.created_by_thread_id IS
    'Session thread that created this job (NULL for cockpit/automation/child '
    'jobs). Queryable backref powering the completion wake and the session''s '
    'own "my outstanding jobs" view. Design: '
    'docs/features/session_wake_on_job_completion.md.';
COMMENT ON COLUMN jobs.wake_state IS
    'Wake outbox state: none|pending|sending|sent|dead. Claimed by an atomic '
    'UPDATE ... FOR UPDATE SKIP LOCKED before the (non-idempotent) send.';
COMMENT ON COLUMN jobs.wake_notified_status IS
    'Terminal status last delivered to the creating session. Second half of the '
    '(job_id, terminal_status) dedup key — a later, different terminal status '
    '(pending_review → completed via approve) is a legitimate second wake.';

COMMIT;
