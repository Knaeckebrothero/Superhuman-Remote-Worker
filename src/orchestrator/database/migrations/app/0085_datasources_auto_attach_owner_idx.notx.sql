-- migration:     0085_datasources_auto_attach_owner_idx.notx.sql
-- description:   Partial index backing the owner-defaults lookup: "which of
--                my connectors auto-attach to a new job?" — read on every
--                job/session create, so it must not seq-scan datasources.
--                The predicate is the point: only non-clone (job_id IS NULL)
--                connectors with auto_attach set are ever candidates, which
--                is a small fraction of the table.
--
--                Split out of 0083 (where it was a plain CREATE INDEX on the
--                pre-existing datasources table) so it can be built
--                CONCURRENTLY. A non-concurrent build blocks writes to
--                datasources for its duration; concurrent cannot run inside a
--                transaction, hence .notx.sql and the single-statement rule.
-- depends-on:    0083_datasource_scope_auto_attach.sql
-- expected:      < 1s. datasources holds one row per configured connector.
-- locks:         SHARE UPDATE EXCLUSIVE on datasources — writes continue.
-- transactional: no (CREATE INDEX CONCURRENTLY must run outside a transaction)
--
-- Recovery: a CONCURRENTLY build that is interrupted leaves an INVALID index
-- behind, and IF NOT EXISTS then silently no-ops on re-run (the name exists).
-- Recover with:
--     DROP INDEX CONCURRENTLY IF EXISTS idx_datasources_auto_attach_owner;
-- then re-run. Detect stragglers via:
--     SELECT indexrelid::regclass FROM pg_index
--     WHERE NOT indisvalid
--       AND indexrelid::regclass::text = 'idx_datasources_auto_attach_owner';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_datasources_auto_attach_owner
    ON datasources (created_by)
    WHERE job_id IS NULL AND auto_attach = TRUE;
