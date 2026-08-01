-- migration:     0016_knowledge_note_revisions.sql
-- description:   Attributable knowledge history (docs/features/workspace_and_change_records.md
--                §6.2). knowledge_index is one row per note and BOTH write paths
--                overwrite the body in place — upsert_note's
--                `ON CONFLICT (project_id, note_id) DO UPDATE SET ... content =
--                EXCLUDED.content` and upsert_kb_note's `(kb_id, path)` branch
--                (src/services/knowledge_store.py) — so an agent re-writing an
--                existing slug destroys the prior body with no recovery, and
--                "which job changed what note when" is unanswerable.
--
--                (1) knowledge_note_revisions — the version of a note as it stood
--                    BEFORE an overwrite or deletion, copied verbatim from the OLD
--                    row. Deliberately CHEAP: no embedding, no search_doc, no
--                    retrieval_messages — history is for recovery + attribution,
--                    not retrieval; a restore re-embeds through the normal upsert
--                    path (§6.2: "omit the embedding from the revision
--                    (regenerate on restore) to keep history cheap"). No FK to
--                    knowledge_index(id): the whole point of the 'delete' action
--                    is to outlive the note row. project_id / job_id carry no FK
--                    either — they reference the APP database (same no-cross-DB-FK
--                    convention as knowledge_index itself).
--                (2) Capture is a TRIGGER, not application code — on purpose. The
--                    trigger sees every write path with zero application changes:
--                    both existing overwrite sites, the orchestrator's inline
--                    DELETEs (orchestrator/main.py), rebuild_from_notes' wipe, and
--                    any path added later. No code path TRUNCATEs knowledge_index,
--                    so row-level triggers observe every destructive write.
--                      - BEFORE UPDATE, gated by a WHEN clause (see the comment at
--                        the trigger below): a revision is cut only when the words
--                        change, not on status/bookkeeping flips.
--                      - BEFORE DELETE, unconditional: deletion destroys the row
--                        (kb note deletion exists via the API, and the reindexer
--                        reaps notes that vanish from the git tree), so the final
--                        version is always preserved with action='delete'.
--                    replaced_by_job_id = NEW.job_id on update (the job whose
--                    write displaced this version), NULL on delete.
-- depends-on:    0015_kb_officer_note_types.sql
-- expected:      < 1s — one CREATE TABLE (starts empty), one index on the empty
--                table, one function, two triggers. No existing data touched, no
--                table rewrite.
-- locks:         brief SHARE ROW EXCLUSIVE on knowledge_index (CREATE TRIGGER).
-- transactional: YES.

-- ---------------------------------------------------------------------------
-- 1. Revision table — the OLD row minus the derived/regenerable columns
--    (embedding, search_doc, retrieval_messages, content_hash), plus the
--    change envelope (action / replaced_by_job_id / changed_at).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_note_revisions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL,          -- references projects(id) in app DB (no FK across databases)
    note_id VARCHAR(100) NOT NULL,     -- note slug: identity that survives the note row

    -- The replaced version, verbatim from OLD:
    title TEXT NOT NULL,
    note_type VARCHAR(50) NOT NULL,
    status VARCHAR(50),
    confidence VARCHAR(20),
    tags TEXT[] DEFAULT '{}',
    keywords TEXT[] DEFAULT '{}',
    job_id UUID,                       -- the job that wrote THIS (now replaced) version
    phase INT,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ,
    modified_at TIMESTAMPTZ,

    -- The change envelope:
    action VARCHAR(10) NOT NULL,       -- 'update' (overwritten) | 'delete' (removed)
    replaced_by_job_id UUID,           -- NEW.job_id on update; NULL on delete
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_revision_action CHECK (action IN ('update', 'delete'))
);

-- Per-note history reads (KnowledgeStore.get_note_revisions): newest change
-- first. Brand-new empty table, so a plain (non-CONCURRENT) build is instant.
CREATE INDEX IF NOT EXISTS idx_knowledge_note_revisions_note
    ON knowledge_note_revisions (project_id, note_id, changed_at DESC);

-- ---------------------------------------------------------------------------
-- 2. Capture function — shared by the UPDATE and DELETE triggers. NEW is only
--    referenced inside the TG_OP = 'UPDATE' branch (it is unassigned during a
--    DELETE), and BEFORE-trigger contract: return NEW on UPDATE, OLD on DELETE.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION knowledge_index_capture_revision()
RETURNS TRIGGER AS $$
DECLARE
    v_action TEXT := 'delete';
    v_replaced_by UUID := NULL;        -- a deleted version was replaced by nothing
BEGIN
    IF TG_OP = 'UPDATE' THEN
        v_action := 'update';
        v_replaced_by := NEW.job_id;
    END IF;
    INSERT INTO knowledge_note_revisions (
        project_id, note_id, title, note_type, status, confidence,
        tags, keywords, job_id, phase, content, created_at, modified_at,
        action, replaced_by_job_id
    ) VALUES (
        OLD.project_id, OLD.note_id, OLD.title, OLD.note_type, OLD.status,
        OLD.confidence, OLD.tags, OLD.keywords, OLD.job_id, OLD.phase,
        OLD.content, OLD.created_at, OLD.modified_at,
        v_action, v_replaced_by
    );
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- WHEN clause: cut a revision only when the words changed. Status-only flips
-- (superseded/archived gardening) are frequent and already expressed in-row,
-- as are TTL countdowns (remaining_cycles), reindex stamps (blob_sha /
-- embedding_version / indexed_at), centroid writes and priority nudges —
-- copying the full body on each of those would bloat history with
-- byte-identical content. IS DISTINCT FROM (not <>) so a NULL-involved title
-- change still fires.
DROP TRIGGER IF EXISTS trg_knowledge_index_revision_update ON knowledge_index;
CREATE TRIGGER trg_knowledge_index_revision_update
    BEFORE UPDATE ON knowledge_index
    FOR EACH ROW
    WHEN (OLD.content IS DISTINCT FROM NEW.content
          OR OLD.title IS DISTINCT FROM NEW.title)
    EXECUTE FUNCTION knowledge_index_capture_revision();

-- Deletion always preserves the final version — no WHEN gate.
DROP TRIGGER IF EXISTS trg_knowledge_index_revision_delete ON knowledge_index;
CREATE TRIGGER trg_knowledge_index_revision_delete
    BEFORE DELETE ON knowledge_index
    FOR EACH ROW
    EXECUTE FUNCTION knowledge_index_capture_revision();
