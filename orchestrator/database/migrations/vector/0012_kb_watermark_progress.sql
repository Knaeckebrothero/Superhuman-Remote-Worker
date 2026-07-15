-- migration:     0012_kb_watermark_progress.sql
-- description:   Per-run progress counters for a centrally indexed OKF KB so
--                Cockpit can render a determinate progress bar while a KB is
--                still embedding. notes_total = notes to process this run
--                (the changed set on an incremental run, the whole vault on a
--                full rebuild); notes_done = notes durably stamped so far this
--                run. Both reset at the start of each run and are advisory only
--                — neither affects retrieval or the as-of watermark.
-- depends-on:    0011_kb_watermark_status.sql
-- transactional: YES.

ALTER TABLE kb_index_watermark
    ADD COLUMN IF NOT EXISTS notes_done INTEGER,
    ADD COLUMN IF NOT EXISTS notes_total INTEGER;
