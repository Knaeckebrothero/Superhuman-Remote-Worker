-- migration:     0240_rerank_capability.sql
-- description:   Add `rerank` to the locked catalog capability enum so the
--                memory reranker becomes an admin-curated catalog row (Admin →
--                Models) with its own default pin, seedable via llm.seed.
-- depends-on:    0239_validate_manifest_deferred_constraints.sql
-- expected:      < 1s. Swaps the CHECK on the small admin-curated models
--                table; NOT VALID so no scan runs under the lock — 0241
--                validates.
-- locks:         ACCESS EXCLUSIVE on models for the constraint swap (tens of
--                rows, admin writes only).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '1min';

-- One transaction: no row can be written between the DROP and the ADD. The
-- new constraint is a strict superset of the old one (one more allowed
-- value), so every existing row already satisfies it; NOT VALID keeps the
-- swap lock-cheap and 0241 validates.
ALTER TABLE models
    DROP CONSTRAINT IF EXISTS models_capabilities_check;

ALTER TABLE models
    ADD CONSTRAINT models_capabilities_check CHECK (
        cardinality(capabilities) >= 1
        AND capabilities <@ ARRAY[
            'chat', 'auxiliary', 'embedding', 'vision', 'whisper', 'tts',
            'search', 'fetch', 'rerank'
        ]::TEXT[]
    ) NOT VALID;

COMMIT;
