-- 0002_capabilities_array.sql
-- Replace models.role TEXT with models.capabilities TEXT[].
-- Folds rows that previously appeared once per role into a single row per
-- (provider_kind, provider_ref, model_id) carrying the union of roles.
--
-- Pre-staged before the runner exists, per the transition plan in
-- docs/db_migration.md. Numbered 0002 because 0001 is reserved for the
-- pg_dump initial-snapshot file generated at cutover time.
--
-- Idempotent (safe to re-run): each step guards with IF EXISTS / IF NOT EXISTS.
-- Once the runner ships and tracks this migration in schema_migrations, the
-- guards become defense-in-depth — the runner is the primary gate.

BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE models ADD COLUMN IF NOT EXISTS capabilities TEXT[];

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'models' AND column_name = 'role'
    ) THEN
        WITH groups AS (
            SELECT
                provider_kind, provider_ref, model_id,
                array_agg(DISTINCT role ORDER BY role) AS caps,
                (array_agg(id ORDER BY created_at, id))[1] AS keep_id
            FROM models
            GROUP BY provider_kind, provider_ref, model_id
        )
        UPDATE models m
        SET capabilities = g.caps
        FROM groups g
        WHERE m.id = g.keep_id;

        DELETE FROM models m
        WHERE m.id NOT IN (
            SELECT (array_agg(id ORDER BY created_at, id))[1]
            FROM models
            GROUP BY provider_kind, provider_ref, model_id
        );

        ALTER TABLE models DROP CONSTRAINT IF EXISTS uq_model_provider;
        DROP INDEX IF EXISTS idx_models_role_enabled;
        ALTER TABLE models DROP COLUMN role;
    END IF;
END $$;

ALTER TABLE models ALTER COLUMN capabilities SET NOT NULL;

ALTER TABLE models DROP CONSTRAINT IF EXISTS models_capabilities_check;
ALTER TABLE models ADD CONSTRAINT models_capabilities_check
    CHECK (
        cardinality(capabilities) >= 1
        AND capabilities <@ ARRAY['chat', 'auxiliary', 'embedding', 'vision', 'whisper', 'tts']::TEXT[]
    );

DO $$ BEGIN
    ALTER TABLE models ADD CONSTRAINT uq_model_provider_v2
        UNIQUE (provider_kind, provider_ref, model_id);
-- See schema.sql: implicit backing index trips 42P07 ahead of 42710.
EXCEPTION WHEN duplicate_object OR duplicate_table THEN null;
END $$;

DROP INDEX IF EXISTS idx_models_role_enabled;
CREATE INDEX IF NOT EXISTS idx_models_capabilities_enabled
    ON models USING GIN (capabilities) WHERE enabled = TRUE;

COMMIT;
