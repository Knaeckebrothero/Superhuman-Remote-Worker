-- migration:     0056_datasources_read_only.sql
-- description:   Declarative read-only flag for published (is_global) data-
--                sources. NULL = not applicable (private/job-scoped rows).
--                Publishing defaults it to TRUE; type='kb' is always TRUE.
--                Declarative only — credentials remain the enforcement
--                boundary (docs/features/public_datasources.md).
-- depends-on:    0055_datasource_config.sql
-- expected:      < 1s; brief table lock for ADD COLUMN (nullable, no default
--                value rewrite).
-- locks:         Brief ACCESS EXCLUSIVE on datasources.
-- transactional: yes
-- ============================================================================

ALTER TABLE datasources
    ADD COLUMN IF NOT EXISTS read_only BOOLEAN;

COMMENT ON COLUMN datasources.read_only IS
    'Declared read-only flag for public (is_global) datasources. NULL = not applicable. Declarative: credentials are the enforcement boundary; kb datasources are read-only by architecture.';
