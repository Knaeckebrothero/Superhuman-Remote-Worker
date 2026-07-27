-- migration:     0013_kb_backlog_ticket_types.sql
-- description:   Backlog / idea-pipeline support on knowledge_index: the
--                feature/issue/idea ticket types and a non-binding priority
--                rank, so the project loop can read an ordered work pool
--                instead of re-deriving a fictional backlog by similarity
--                search. Priority is a LABEL — nothing gates on it.
-- depends-on:    0012_kb_watermark_progress.sql
-- transactional: YES.

ALTER TABLE knowledge_index DROP CONSTRAINT IF EXISTS valid_note_type;
ALTER TABLE knowledge_index ADD CONSTRAINT valid_note_type CHECK (note_type IN (
    'goal', 'plan', 'decision', 'learning', 'code',
    'source', 'question', 'state', 'retrospective', 'datasource',
    'feature', 'issue', 'idea'
));

-- 0 = high, 1 = normal, 2 = low. Default normal, so every pre-existing note
-- and every note written by a client that does not know about priority sorts
-- in the middle. No backfill needed beyond this default.
ALTER TABLE knowledge_index
    ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 1;

ALTER TABLE knowledge_index
    ADD CONSTRAINT knowledge_index_priority_valid
    CHECK (priority BETWEEN 0 AND 2)
    NOT VALID;
ALTER TABLE knowledge_index
    VALIDATE CONSTRAINT knowledge_index_priority_valid;

-- The pool query: open tickets for one project, priority then age.
CREATE INDEX IF NOT EXISTS idx_knowledge_backlog
    ON knowledge_index (project_id, priority, created_at)
    WHERE status = 'active' AND note_type IN ('feature', 'issue', 'idea');

COMMENT ON COLUMN knowledge_index.priority IS
    'Backlog rank: 0=high, 1=normal, 2=low. A display label only — no code '
    'path may gate or reorder work on it.';
