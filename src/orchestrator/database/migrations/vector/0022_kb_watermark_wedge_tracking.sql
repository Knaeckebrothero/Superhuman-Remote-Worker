-- migration:     0022_kb_watermark_wedge_tracking.sql
-- description:   Wedge detector state for the KB reindex sweep (Slice E item 3,
--                kb_retrieval_hardening_and_slice_d_additive.md H3): the
--                fingerprint of the run's per-note errors, how many consecutive
--                `partial` sweeps carried the same fingerprint, when the streak
--                crossed the alarm threshold, and an advisory line for
--                conditions that are skipped rather than failed (duplicate
--                note ids, H2). All nullable/defaulted; no backfill.
-- depends-on:    0021_kb_backlog_keyset_index.notx.sql
-- expected:      Instant ALTER; no rewrite.

ALTER TABLE kb_index_watermark
    ADD COLUMN IF NOT EXISTS error_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS error_streak      INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wedged_since      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS advisory          TEXT;
