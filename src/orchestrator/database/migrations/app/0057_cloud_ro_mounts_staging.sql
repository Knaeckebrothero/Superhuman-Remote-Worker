-- migration:     0057_cloud_ro_mounts_staging.sql
-- description:   Protected cloud Slice C staging state on cloud_ro_mounts:
--                persisted etag baseline (conflict gate + manifest
--                classification) and staged-epoch bookkeeping for the
--                S3 staging pipeline (design spec 2026-07-12 §5).
-- depends-on:    0056_datasources_read_only.sql
-- expected:      < 1s
-- locks:         Brief ACCESS EXCLUSIVE on cloud_ro_mounts (ADD COLUMN, no rewrite)
-- transactional: yes
-- ============================================================================

ALTER TABLE cloud_ro_mounts ADD COLUMN IF NOT EXISTS etag_baseline JSONB;
ALTER TABLE cloud_ro_mounts ADD COLUMN IF NOT EXISTS staged_epoch  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cloud_ro_mounts ADD COLUMN IF NOT EXISTS staged_at     TIMESTAMPTZ;
ALTER TABLE cloud_ro_mounts ADD COLUMN IF NOT EXISTS staged_summary JSONB;

COMMENT ON COLUMN cloud_ro_mounts.etag_baseline  IS 'path->etag map (files only) captured at engage, re-captured after each apply';
COMMENT ON COLUMN cloud_ro_mounts.staged_epoch   IS 'monotonic staging epoch: bumped on every successful stage push, apply, and reject';
COMMENT ON COLUMN cloud_ro_mounts.staged_at      IS 'when the current epoch was pushed; NULL when nothing staged';
COMMENT ON COLUMN cloud_ro_mounts.staged_summary IS 'manifest counts + content signature for the current epoch (entry lists live in S3); NULL when nothing staged';
