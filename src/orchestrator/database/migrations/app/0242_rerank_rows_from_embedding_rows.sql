-- migration:     0242_rerank_rows_from_embedding_rows.sql
-- description:   Non-disruptive upgrade for the new required `rerank`
--                capability (0240). Before 0240 the memory reranker posted
--                `qwen3-reranker-8b` to every embedding row's host implicitly;
--                this materialises that implicit transport as one `rerank`
--                catalog row per enabled embedding provider and pins it, so
--                the readiness gate stays green and runtime behaviour is
--                unchanged until an admin replaces the row in Admin → Models.
--                Deployments that already carry a rerank row are untouched.
-- depends-on:    0241_validate_rerank_capability.sql
-- expected:      < 1s. Inserts at most one row per embedding provider on an
--                admin-curated table (tens of rows) plus one settings row.
-- locks:         ROW EXCLUSIVE on models and system_settings.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '1min';

INSERT INTO models (
    provider_kind, provider_ref, model_id, display_label, capabilities,
    family, enabled, seeded_from, notes
)
SELECT DISTINCT ON (e.provider_kind, e.provider_ref)
    e.provider_kind,
    e.provider_ref,
    'qwen3-reranker-8b',
    'Qwen3 Reranker 8B (auto)',
    ARRAY['rerank']::TEXT[],
    e.family,
    TRUE,
    'migration:0242',
    'Auto-created by migration 0242: before the rerank catalog slot existed the '
    || 'memory reranker rode this embedding row''s endpoint implicitly '
    || '(POST /rerank, model qwen3-reranker-8b). Replace or disable it in '
    || 'Admin -> Models once a real rerank row exists.'
FROM models AS e
WHERE e.enabled
  AND 'embedding' = ANY (e.capabilities)
  AND NOT EXISTS (
      SELECT 1 FROM models AS r WHERE 'rerank' = ANY (r.capabilities)
  )
ORDER BY e.provider_kind, e.provider_ref, e.created_at;

-- Pin it only when the rows above were created and no admin pin exists, so
-- the readiness gate's "pin a default for: rerank" step is satisfied too.
INSERT INTO system_settings (key, value, updated_by)
SELECT 'llm.default_rerank_model',
       '{"model": "qwen3-reranker-8b"}'::jsonb,
       'migration:0242'
WHERE EXISTS (
        SELECT 1 FROM models WHERE seeded_from = 'migration:0242'
      )
  AND NOT EXISTS (
        SELECT 1 FROM system_settings WHERE key = 'llm.default_rerank_model'
      );

COMMIT;
