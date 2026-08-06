-- migration:     0089_infrastructure_plan_period_idx.notx.sql
-- description:   Plan range lookup for the dark interval-tail reader. Planned
--                and conflict rows are intentionally queried with published
--                rows so crash windows remain provisional and visible.
-- depends-on:    0088_inventory_ingestion_logical_size.sql
-- expected:      Online build; duration depends on retained publication plans.
-- locks:         SHARE UPDATE EXCLUSIVE; normal reads and writes continue.
-- transactional: no (single CREATE INDEX CONCURRENTLY statement)
--
-- Recovery: an interrupted concurrent build can leave an INVALID index. Drop
-- resource_publication_plans_period_idx CONCURRENTLY, then rerun.

CREATE INDEX CONCURRENTLY IF NOT EXISTS resource_publication_plans_period_idx
    ON resource_publication_plans USING gist (
        tstzrange(period_start, period_end, '[)')
    );
