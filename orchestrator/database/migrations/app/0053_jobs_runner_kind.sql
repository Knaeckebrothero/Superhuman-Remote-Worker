-- migration:     0053_jobs_runner_kind.sql
-- description:   Stamp the dispatch runner class on jobs. Human-created and
--                automation jobs stay runner_kind='user'; system lifecycle
--                subjobs use runner_kind='lifecycle' so dispatch can elevate
--                only the pause/autonomy policy while keeping owner
--                capabilities clamped.
-- depends-on:    0001_initial.sql
-- expected:      < 1s on normal tables; brief table lock for ADD COLUMN.
-- locks:         Brief ACCESS EXCLUSIVE on jobs.
-- transactional: yes
-- ============================================================================

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS runner_kind TEXT NOT NULL DEFAULT 'user';

ALTER TABLE jobs
    DROP CONSTRAINT IF EXISTS jobs_runner_kind_check;

ALTER TABLE jobs
    ADD CONSTRAINT jobs_runner_kind_check
    CHECK (runner_kind IN ('user', 'lifecycle', 'service'));

COMMENT ON COLUMN jobs.runner_kind IS
    'Dispatch runner class. user = owner grants; lifecycle = system subjob with owner capabilities and full autonomy ceiling; service = reserved for ownerless system jobs.';

CREATE OR REPLACE VIEW job_summary AS
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
    j.runner_kind
   FROM jobs j;
