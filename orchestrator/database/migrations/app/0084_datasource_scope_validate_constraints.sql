-- migration:     0084_datasource_scope_validate_constraints.sql
-- description:   Validate the two datasources CHECK constraints that 0083
--                added NOT VALID. Split into its own migration because the
--                runner wraps each transactional file in a single
--                transaction: validating in the same transaction as the ADD
--                would hold the ADD's ACCESS EXCLUSIVE lock across the scan
--                and defeat the point of NOT VALID entirely (squawk's
--                constraint-missing-not-valid rule flags exactly that).
-- depends-on:    0083_datasource_scope_auto_attach.sql
-- expected:      < 1s. One sequential scan of datasources, which is small
--                (one row per configured connector, not per job).
-- locks:         SHARE UPDATE EXCLUSIVE on datasources — concurrent reads
--                and writes continue throughout.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout      = '2s';
SET LOCAL statement_timeout = '5min';

-- Both are expected to pass without touching a row: 0083 added the columns
-- with satisfying defaults ('all' / 1) and its backfills only ever write
-- 'projects'. VALIDATE is what flips convalidated so the planner may use the
-- constraint and so a later ALTER can rely on it.
ALTER TABLE datasources
    VALIDATE CONSTRAINT datasources_scope_mode_check;

ALTER TABLE datasources
    VALIDATE CONSTRAINT datasources_policy_revision_positive;

COMMIT;
