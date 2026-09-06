-- migration:     0206_threads_subagent_kind.sql
-- description:   Child-session identity on threads (U3 subagents, plan B.1).
--                A subagent is a threads row of kind='subagent' hanging off
--                the parent job (parent_job_id) or, for a session parent
--                (U5), the parent thread (parent_thread_id): its handle,
--                roster type, open-set subagent_status, the driver's full
--                outcome, the error text, the spilled report path and the
--                parent tool call it answered. Every existing row is a
--                session (kind DEFAULT 'session'); the subagent columns stay
--                NULL for sessions. valid_thread_status is deliberately NOT
--                widened: a child row moves active -> ended and the outcome
--                lives in subagent_status, so the closed status vocabulary
--                its four service consumers, the status endpoint and both
--                update_thread_status accessors read is untouched.
-- depends-on:    0202_threads_ssh_handle.sql,
--                0185_thread_runtime_generation_retirement.sql
-- expected:      Ten in-place ADD COLUMNs on threads (nullable, or a constant
--                DEFAULT — no rewrite on PG 11+), one CHECK and two foreign
--                keys added NOT VALID (no scan; 0208 validates), plus firing
--                0185's deferred reciprocity fence early exactly as 0202
--                does. No row rewrites.
-- locks:         Brief ACCESS EXCLUSIVE on threads to attach the columns and
--                constraints (retried: threads is hot and carries several
--                triggers); the NOT VALID foreign keys take SHARE ROW
--                EXCLUSIVE on jobs and threads for the same window.
-- transactional: yes
-- rollout:       Inert until an agent with delegation.enabled creates a
--                child row through POST /api/agents/jobs/{job_id}/subagents.

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '5min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

-- Drain 0185's deferred reciprocity fence before touching the table, the
-- 0202 shape: the runner applies every pending transactional migration in
-- ONE transaction, and an ALTER TABLE on threads is refused while AFTER-row
-- events queued by an earlier migration's UPDATE are still pending. 0202
-- already drains the 0191 events on any span that reaches back that far, and
-- no migration between 0202 and this file writes threads rows today, so this
-- is defensive: it keeps "nothing before us wrote threads" from being a
-- hidden precondition of this file. Scoped to the one constraint, never
-- SET CONSTRAINTS ALL (see 0202).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_constraint
         WHERE conname      = 'threads_agent_reciprocity_fence'
           AND connamespace = 'public'::regnamespace
           AND conrelid     = 'public.threads'::regclass
           AND condeferrable
    ) THEN
        EXECUTE 'SET CONSTRAINTS public.threads_agent_reciprocity_fence IMMEDIATE';
    END IF;
END $$;

DO $$
DECLARE
    attempt int := 0;
BEGIN
    LOOP
        BEGIN
            ALTER TABLE public.threads
                ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'session',
                ADD COLUMN IF NOT EXISTS parent_job_id uuid,
                ADD COLUMN IF NOT EXISTS parent_thread_id uuid,
                ADD COLUMN IF NOT EXISTS parent_tool_call_id text,
                ADD COLUMN IF NOT EXISTS subagent_handle text,
                ADD COLUMN IF NOT EXISTS subagent_type text,
                ADD COLUMN IF NOT EXISTS subagent_status text,
                ADD COLUMN IF NOT EXISTS subagent_outcome text,
                ADD COLUMN IF NOT EXISTS subagent_error text,
                ADD COLUMN IF NOT EXISTS report_path text;

            -- ADD CONSTRAINT has no IF NOT EXISTS; guard on the catalog so a
            -- rerun after a partial failure is still idempotent. Every one is
            -- NOT VALID: existing rows are all sessions with NULL parents, so
            -- nothing here is ever violated, but a scan under ACCESS EXCLUSIVE
            -- is exactly what the two-phase shape avoids. 0208 validates.
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_constraint
                 WHERE conname = 'threads_kind_check'
                   AND conrelid = 'public.threads'::regclass
            ) THEN
                ALTER TABLE public.threads
                    ADD CONSTRAINT threads_kind_check
                    CHECK (kind IN ('session', 'subagent')) NOT VALID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_constraint
                 WHERE conname = 'threads_parent_job_id_fkey'
                   AND conrelid = 'public.threads'::regclass
            ) THEN
                ALTER TABLE public.threads
                    ADD CONSTRAINT threads_parent_job_id_fkey
                    FOREIGN KEY (parent_job_id) REFERENCES public.jobs (id)
                    ON DELETE CASCADE NOT VALID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_constraint
                 WHERE conname = 'threads_parent_thread_id_fkey'
                   AND conrelid = 'public.threads'::regclass
            ) THEN
                ALTER TABLE public.threads
                    ADD CONSTRAINT threads_parent_thread_id_fkey
                    FOREIGN KEY (parent_thread_id) REFERENCES public.threads (id)
                    ON DELETE CASCADE NOT VALID;
            END IF;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            attempt := attempt + 1;
            IF attempt >= 10 THEN
                RAISE;
            END IF;
            PERFORM pg_sleep(1);
        END;
    END LOOP;
END $$;

-- Put the fence back the way 0185 declared it, so a migration appended after
-- this one still sees INITIALLY DEFERRED timing.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_constraint
         WHERE conname      = 'threads_agent_reciprocity_fence'
           AND connamespace = 'public'::regnamespace
           AND conrelid     = 'public.threads'::regclass
           AND condeferrable
    ) THEN
        EXECUTE 'SET CONSTRAINTS public.threads_agent_reciprocity_fence DEFERRED';
    END IF;
END $$;

COMMENT ON COLUMN public.threads.kind IS
    'What this row is: ''session'' (an interactive thread, the default) or '
    '''subagent'' (a child session a worker job or a session spawned through '
    'delegate_agent; U3). Session listings filter on kind so child rows never '
    'reach the sessions page; the subagent_* / parent_* columns are NULL for '
    'sessions.';
COMMENT ON COLUMN public.threads.parent_job_id IS
    'kind=subagent only: the worker job that spawned this child. ON DELETE '
    'CASCADE — a job takes its children with it (delete_job ends live children '
    'first, because the pinned delete authority only lets an ended, '
    'authority-free row go).';
COMMENT ON COLUMN public.threads.parent_thread_id IS
    'kind=subagent only: the session thread that spawned this child (U5 '
    'session parents). NULL for a worker-job parent. ON DELETE CASCADE.';
COMMENT ON COLUMN public.threads.parent_tool_call_id IS
    'kind=subagent only: the parent''s delegate_agent tool call this child '
    'answered. (parent_job_id, parent_tool_call_id) is the idempotency key: a '
    'parent re-running its tools node after a hard kill replays the stored '
    'report instead of spawning again.';
COMMENT ON COLUMN public.threads.subagent_handle IS
    'kind=subagent only: the short handle the parent sees (<type>-<4 hex>, '
    'e.g. implementer-7f3a) — unique per parent, not globally; the durable '
    'identity is the thread id.';
COMMENT ON COLUMN public.threads.subagent_type IS
    'kind=subagent only: the roster entry name the child ran as (explorer, '
    'implementer, reviewer, ...).';
COMMENT ON COLUMN public.threads.subagent_status IS
    'kind=subagent only: the bare lifecycle kind, written by the agent-side '
    'ledger. Open set by design — app-validated, no CHECK, so a new kind '
    'never needs a migration. Today: running | completed | parked | '
    'interrupted | capped | error | cancelled (src/subagents/ledger.py '
    'SUBAGENT_STATUSES). Anything other than running is terminal, and a '
    'terminal write also moves status to ended and stamps ended_at.';
COMMENT ON COLUMN public.threads.subagent_outcome IS
    'kind=subagent only: the driver''s full classification behind '
    'subagent_status (capped:turns, interrupted:drain, interrupted:stale, '
    'cancelled:parent_deleted, ...). Free text; the cockpit shows it as the '
    'outcome badge detail.';
COMMENT ON COLUMN public.threads.subagent_error IS
    'kind=subagent only: the error text of an error / cancelled child, NULL '
    'otherwise.';
COMMENT ON COLUMN public.threads.report_path IS
    'kind=subagent only: workspace-relative path of the child''s full spilled '
    'report in the PARENT tree (.subagents/<handle>/report.md); NULL when the '
    'spill failed. The replay path re-renders the envelope from this file.';

COMMIT;
