-- migration:     0090_infrastructure_interval_overlap_idx.notx.sql
-- description:   Fleet interval-overlap lookup for the dark usage reader.
-- depends-on:    0089_infrastructure_plan_period_idx.notx.sql
-- expected:      Online build; duration depends on retained interval history.
-- locks:         SHARE UPDATE EXCLUSIVE; normal reads and writes continue.
-- transactional: no (single CREATE INDEX CONCURRENTLY statement)
--
-- Recovery: an interrupted concurrent build can leave an INVALID index. Drop
-- resource_intervals_overlap_idx CONCURRENTLY, then rerun.

CREATE INDEX CONCURRENTLY IF NOT EXISTS resource_intervals_overlap_idx
    ON resource_intervals USING gist (
        tstzrange(started_at, ended_at, '[)')
    );
