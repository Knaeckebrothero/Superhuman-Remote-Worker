-- Collapse persistent thread lifecycle from 4 states to 3.
-- 'idle' was always functionally identical to 'ended' (both resumable, both
-- equally non-active). It existed only because nothing flipped sessions to
-- 'ended' automatically. The new model: idle-timeout flips straight to 'ended'.

UPDATE threads
SET status   = 'ended',
    ended_at = COALESCE(ended_at, last_activity, CURRENT_TIMESTAMP)
WHERE status = 'idle';

ALTER TABLE threads DROP CONSTRAINT IF EXISTS valid_thread_status;
ALTER TABLE threads
    ADD CONSTRAINT valid_thread_status
        CHECK (status IN ('created', 'active', 'ended'));
