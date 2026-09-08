-- migration: 0233_run_queue_bg_tasks.sql
-- description: Durable payloads for queue-owned background cloud-push recovery.
-- depends-on: 0232_thread_cloud_sync_push_ownership.sql
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE run_queue_bg_tasks (
    unit_id UUID PRIMARY KEY REFERENCES run_queue(unit_id) ON DELETE CASCADE,
    thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    task_kind TEXT NOT NULL,
    detail JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT run_queue_bg_tasks_detail_object CHECK (jsonb_typeof(detail) = 'object')
);

CREATE INDEX idx_run_queue_bg_tasks_generation
    ON run_queue_bg_tasks(thread_id, task_kind, (detail->>'mount_id'), (detail->>'generation'));

COMMENT ON TABLE run_queue_bg_tasks IS
    'Scheduling payloads only; cloud generation rows and remote markers remain '
    'the authority for completion. History preserves bounded retries per effect.';

COMMIT;
