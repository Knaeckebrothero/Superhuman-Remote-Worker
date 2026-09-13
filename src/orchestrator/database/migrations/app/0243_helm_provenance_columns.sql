-- migration:     0243_helm_provenance_columns.sql
-- description:   Per-row provenance for Helm-reconciled configuration: who
--                wrote the row last (source), the digest of the value Helm
--                last applied (helm_value_hash) and when source changed, on
--                system_api_keys, llm_endpoints, models and system_settings.
--                Backfills source from the existing seeded_from / updated_by
--                breadcrumbs (same rule as shared.helm_provenance).
-- depends-on:    0242_rerank_rows_from_embedding_rows.sql
-- expected:      < 1s. All four are small admin-curated tables (tens to a
--                few hundred rows); ADD COLUMN with a constant default is a
--                catalog-only change on PG 11+. The CHECKs are NOT VALID so
--                no scan runs under the lock — 0244 validates.
-- locks:         ACCESS EXCLUSIVE on each table for the column adds (brief;
--                admin writes only), then row locks for the backfill.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '1min';

ALTER TABLE system_api_keys
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ui',
    ADD COLUMN IF NOT EXISTS helm_value_hash TEXT,
    ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE system_api_keys
    ADD CONSTRAINT system_api_keys_source_check
    CHECK (source IN ('default', 'helm', 'ui')) NOT VALID;

ALTER TABLE llm_endpoints
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ui',
    ADD COLUMN IF NOT EXISTS helm_value_hash TEXT,
    ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE llm_endpoints
    ADD CONSTRAINT llm_endpoints_source_check
    CHECK (source IN ('default', 'helm', 'ui')) NOT VALID;

ALTER TABLE models
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ui',
    ADD COLUMN IF NOT EXISTS helm_value_hash TEXT,
    ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE models
    ADD CONSTRAINT models_source_check
    CHECK (source IN ('default', 'helm', 'ui')) NOT VALID;

ALTER TABLE system_settings
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ui',
    ADD COLUMN IF NOT EXISTS helm_value_hash TEXT,
    ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE system_settings
    ADD CONSTRAINT system_settings_source_check
    CHECK (source IN ('default', 'helm', 'ui')) NOT VALID;

-- Backfill provenance from the breadcrumbs the rows already carry. The
-- llm.seed Job tags what it writes with 'helm:llm.seed'; every other
-- breadcrumb (env:TAVILY_API_KEY, helm:searxng, helm:crawl4ai,
-- helm:openrouter-defaults, migration:...) is an image-shipped seeder; no
-- breadcrumb means an admin wrote the row (the column default, 'ui'). Rows
-- written after this migration carry source explicitly
-- (shared.helm_provenance.provenance_from_breadcrumb is the same rule).
UPDATE system_api_keys
   SET source = CASE
                    WHEN seeded_from LIKE 'helm:llm.seed%' THEN 'helm'
                    WHEN seeded_from IS NOT NULL THEN 'default'
                    ELSE 'ui'
                END
 WHERE seeded_from IS NOT NULL;

-- llm_endpoints carries no breadcrumb of its own. A system endpoint's
-- provenance is that of the catalog rows anchored to it (the seed Job and
-- the boot-time seeders create the endpoint and its rows together); the
-- shared subscription proxy is identified by its transport marker.
UPDATE llm_endpoints AS e
   SET source = derived.src
  FROM (
        SELECT provider_ref,
               CASE
                   WHEN bool_or(seeded_from LIKE 'helm:llm.seed%') THEN 'helm'
                   WHEN bool_or(seeded_from IS NOT NULL) THEN 'default'
                   ELSE 'ui'
               END AS src
          FROM models
         WHERE provider_kind = 'endpoint'
         GROUP BY provider_ref
       ) AS derived
 WHERE e.user_id IS NULL
   AND e.id::text = derived.provider_ref
   AND derived.src <> 'ui';

UPDATE llm_endpoints
   SET source = 'default'
 WHERE user_id IS NULL
   AND transport_kind = 'subscription-proxy'
   AND source = 'ui';

UPDATE models
   SET source = CASE
                    WHEN seeded_from LIKE 'helm:llm.seed%' THEN 'helm'
                    WHEN seeded_from IS NOT NULL THEN 'default'
                    ELSE 'ui'
                END
 WHERE seeded_from IS NOT NULL;

-- system_settings has no seeded_from; the seed Job and the boot-time
-- seeders leave their mark in updated_by instead (an admin write carries the
-- admin's user id).
UPDATE system_settings
   SET source = CASE
                    WHEN updated_by LIKE 'helm:%' THEN 'helm'
                    WHEN updated_by IS NULL THEN 'default'
                    ELSE 'ui'
                END
 WHERE updated_by IS NULL OR updated_by LIKE 'helm:%';

COMMIT;
