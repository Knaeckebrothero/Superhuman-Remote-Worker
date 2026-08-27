-- migration:     0014_kb_backlog_index.notx.sql
-- description:   The backlog pool index for 0013's feature/issue/idea ticket
--                types: open tickets for one project, priority then age.
--                Split out of 0013 because CREATE INDEX CONCURRENTLY cannot
--                run inside that file's transaction.
-- depends-on:    0013_kb_backlog_ticket_types.sql
-- expected:      sub-second at current sizes. Concurrent build; safe on a
--                populated table.
-- locks:         ShareUpdateExclusiveLock — CREATE INDEX CONCURRENTLY
--                doesn't block reads or writes against knowledge_index.
-- transactional: NO. CONCURRENTLY can't run inside a transaction block;
--                hence the .notx.sql suffix the runner recognises. One
--                statement per file: asyncpg executes a multi-statement
--                string as one implicit transaction, which CONCURRENTLY
--                also rejects.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_knowledge_backlog
    ON knowledge_index (project_id, priority, created_at)
    WHERE status = 'active' AND note_type IN ('feature', 'issue', 'idea');
