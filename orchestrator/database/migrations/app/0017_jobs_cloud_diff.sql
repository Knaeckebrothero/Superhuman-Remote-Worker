-- migration:     0017_jobs_cloud_diff.sql
-- description:   Add four columns to the jobs table to support the two modes
--                of the job cloud workflow (docs/features/job_cloud_export.md):
--
--                Mode A (project-attached, primary): the orchestrator clones
--                the project folder into the agent workspace at job-start,
--                takes a Gitea baseline commit, and stores that commit hash
--                on cloud_diff_baseline_commit. At job-completion, the diff
--                between baseline and final is computed; if non-empty, the
--                job enters pending_review with diff_status='pending'.
--                Accept/reject transitions diff_status to 'accepted'/'rejected'.
--
--                Mode B (loose, no project, fallback): on a completed loose
--                job the user can hit "Export to shared folder". We allocate
--                a shared session-folder via the existing main-cloud
--                primitives, store the opaque handle on
--                exported_folder_handle, and stamp exported_at. Re-export
--                is refused while exported_folder_handle is non-NULL.
--
--                The four columns are independent: a job is in Mode A iff it
--                has a project_id; Mode B iff it does not. The two states
--                cannot coexist by construction.
-- depends-on:    0016_project_network_tier.sql
-- expected:      < 50ms. Four metadata-only column adds; no table rewrite.
-- locks:         AccessExclusive on jobs (fast, metadata-only).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Mode A: diff baseline + review state
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN cloud_diff_baseline_commit TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN diff_status TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Closed set of diff_status values. NOT VALID + VALIDATE keeps the
-- constraint add fast (no full table scan under lock).
DO $$ BEGIN
    ALTER TABLE jobs
        ADD CONSTRAINT jobs_diff_status_check
        CHECK (diff_status IS NULL
            OR diff_status IN ('pending', 'accepted', 'rejected')) NOT VALID;
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE jobs VALIDATE CONSTRAINT jobs_diff_status_check;
EXCEPTION WHEN undefined_object THEN null;
END $$;

-- Mode B: shared-folder export state
DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN exported_folder_handle TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE jobs ADD COLUMN exported_at TIMESTAMP WITH TIME ZONE;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- The cockpit review queue scans for jobs awaiting decision.
CREATE INDEX IF NOT EXISTS idx_jobs_diff_status_pending
    ON jobs (id)
    WHERE diff_status = 'pending';

COMMENT ON COLUMN jobs.cloud_diff_baseline_commit IS
    'Gitea commit hash captured at job-start over the mounted project '
    'folder (projects/<slug>/). The agent edits the working tree in '
    'place; the diff against this baseline at job-completion is what '
    'gets written back to the cloud on accept. Set only for project-'
    'attached Mode A jobs; NULL for loose Mode B jobs.';

COMMENT ON COLUMN jobs.diff_status IS
    'State of the project-folder diff review. NULL = no diff captured '
    '(job not finished, or no project attached, or empty diff). '
    '''pending'' = diff captured, awaiting user accept/reject. '
    '''accepted'' = diff applied to the cloud folder. '
    '''rejected'' = diff discarded without write-back.';

COMMENT ON COLUMN jobs.exported_folder_handle IS
    'Mode B only: opaque handle of the shared cloud folder created when '
    'the user clicked "Export to shared folder" on a completed loose '
    'job. Re-export is refused while non-NULL.';

COMMENT ON COLUMN jobs.exported_at IS
    'Timestamp the Mode B shared-folder export was made. Paired with '
    'exported_folder_handle.';

COMMIT;
