-- migration:     0080_job_change_records.sql
-- description:   Move terminal job/loop change records out of project-repo
--                retros/ files and into one structured PostgreSQL row per job.
-- depends-on:    0079_bench_runs.sql
-- expected:      < 1s; creates one empty table and two empty indexes.
-- locks:         Brief catalog locks to validate existing foreign keys only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE job_change_records (
    job_id UUID PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
    loop_id UUID REFERENCES project_loops(id) ON DELETE SET NULL,
    record_type VARCHAR(32) NOT NULL
        CHECK (record_type IN ('job_record', 'loop_record')),
    role VARCHAR(200) NOT NULL,
    iteration INTEGER,
    status VARCHAR(50) NOT NULL,
    repo_name VARCHAR(200),
    branch_name VARCHAR(200),
    delivery_status VARCHAR(50) NOT NULL DEFAULT 'none',
    delivery_ref TEXT,
    delivery_sha TEXT,
    completion_notes TEXT NOT NULL DEFAULT '',
    delivery_notes JSONB NOT NULL DEFAULT '[]'::JSONB
        CHECK (jsonb_typeof(delivery_notes) = 'array'),
    changes JSONB NOT NULL DEFAULT '[]'::JSONB
        CHECK (jsonb_typeof(changes) = 'array'),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_job_change_records_project_created
    ON job_change_records (project_id, created_at DESC);
CREATE INDEX idx_job_change_records_loop_iteration
    ON job_change_records (loop_id, iteration DESC)
    WHERE loop_id IS NOT NULL;

COMMENT ON TABLE job_change_records IS
    'One immutable terminal outcome per job. Replaces retros/*.md in shared '
    'project jobs repositories; PostgreSQL is the project-history authority.';

COMMIT;
