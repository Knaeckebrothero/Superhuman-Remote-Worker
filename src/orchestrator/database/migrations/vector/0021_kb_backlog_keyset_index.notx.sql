-- migration:     0021_kb_backlog_keyset_index.notx.sql
-- description:   Complete the deterministic Officer backlog page order with
--                note_id so keyset scans cross equal priority/timestamp
--                boundaries without a sort, duplicate, or skipped ticket.
-- depends-on:    0020_kb_ready_authorization.sql
-- expected:      Concurrent index build; no read/write blocking at supported
--                backlog population.
-- locks:         ShareUpdateExclusiveLock only. CREATE INDEX CONCURRENTLY.
-- transactional: NO. One statement only.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_knowledge_backlog_page
    ON knowledge_index (project_id, priority, created_at, note_id)
    WHERE status = 'active' AND note_type IN ('feature', 'issue', 'idea');
