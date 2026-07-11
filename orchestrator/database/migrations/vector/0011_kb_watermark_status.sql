-- migration:     0011_kb_watermark_status.sql
-- description:   Operational status for centrally indexed OKF KB datasources.
--                indexed_commit remains the authoritative as-of watermark;
--                these fields surface pending/partial/failure state without
--                putting any repository credential or URL in the vector DB.
-- depends-on:    0010_knowledge_links.sql
-- transactional: YES.

ALTER TABLE kb_index_watermark
    ADD COLUMN IF NOT EXISTS source_head VARCHAR(64),
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ready',
    ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_success_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error TEXT;

UPDATE kb_index_watermark
SET source_head = COALESCE(source_head, indexed_commit),
    last_attempt_at = COALESCE(last_attempt_at, updated_at),
    last_success_at = COALESCE(last_success_at, updated_at)
WHERE indexed_commit IS NOT NULL;

ALTER TABLE kb_index_watermark
    ADD CONSTRAINT kb_index_watermark_status_valid
    CHECK (status IN ('pending', 'indexing', 'ready', 'partial', 'failed'))
    NOT VALID;

ALTER TABLE kb_index_watermark
    VALIDATE CONSTRAINT kb_index_watermark_status_valid;
