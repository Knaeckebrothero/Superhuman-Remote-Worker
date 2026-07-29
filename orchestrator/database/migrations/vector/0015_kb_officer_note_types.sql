-- migration:     0015_kb_officer_note_types.sql
-- description:   Officer (centurion) note types on knowledge_index: 'charter'
--                (the project's pinned commander's-intent note — identity,
--                authority bounds, preferences, posture; injected
--                unconditionally into officer/conference sessions) and
--                'report' (a worker recon deliverable — provenance rides the
--                existing job_id column; reports inform, never steer).
--                One-ACTIVE-charter-per-project is enforced at the write path
--                (kb_write), NOT by a partial unique index: the reindexer
--                upserts rows from human-editable files, and a constraint
--                violation there would wedge a whole reindex run over one
--                duplicate file. See docs/features/centurion.md §5.
-- depends-on:    0014_kb_backlog_index.notx.sql
-- transactional: YES.

ALTER TABLE knowledge_index DROP CONSTRAINT IF EXISTS valid_note_type;
ALTER TABLE knowledge_index
    ADD CONSTRAINT valid_note_type CHECK (note_type IN (
        'goal', 'plan', 'decision', 'learning', 'code',
        'source', 'question', 'state', 'retrospective', 'datasource',
        'feature', 'issue', 'idea',
        'charter', 'report'
    ))
    NOT VALID;
ALTER TABLE knowledge_index
    VALIDATE CONSTRAINT valid_note_type;
