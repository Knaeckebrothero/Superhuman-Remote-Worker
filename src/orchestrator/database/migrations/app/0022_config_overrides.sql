-- migration:     0022_config_overrides.sql
-- description:   Rename prompt_overrides -> config_overrides and generalize it
--                to the whole config matrix. Adds value_json for structured
--                kinds (settings, guardrails); relaxes content/content_format
--                (text kinds only); widens `kind`. RENAME preserves all data.
--                Do NOT edit 0021 (already applied + checksummed).
-- depends-on:    0021_prompt_overrides.sql
-- expected:      < 100ms. Rename + column add + constraint swaps; no data rewrite.
-- locks:         Brief ACCESS EXCLUSIVE on the table for the rename/ALTERs.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

ALTER TABLE prompt_overrides RENAME TO config_overrides;

-- Tidy the carried-over object names (RENAME TABLE leaves these unchanged).
ALTER INDEX uq_prompt_override RENAME TO uq_config_override;
ALTER INDEX idx_prompt_override_lookup RENAME TO idx_config_override_lookup;
ALTER TABLE config_overrides RENAME CONSTRAINT prompt_overrides_kind_check
    TO config_overrides_kind_check;

-- Structured value for settings/guardrails (text kinds keep using `content`).
ALTER TABLE config_overrides ADD COLUMN IF NOT EXISTS value_json JSONB;

-- content / content_format only apply to text kinds now -> allow NULL.
ALTER TABLE config_overrides ALTER COLUMN content DROP NOT NULL;
ALTER TABLE config_overrides ALTER COLUMN content_format DROP NOT NULL;

-- Widen the kind domain.
ALTER TABLE config_overrides DROP CONSTRAINT config_overrides_kind_check;
ALTER TABLE config_overrides ADD CONSTRAINT config_overrides_kind_check
    CHECK (kind IN ('prompts', 'instructions', 'settings', 'guardrails'));

-- Exactly one payload column populated, by kind.
ALTER TABLE config_overrides ADD CONSTRAINT config_overrides_payload_check CHECK (
    (kind IN ('prompts', 'instructions') AND content IS NOT NULL AND value_json IS NULL) OR
    (kind IN ('settings', 'guardrails')  AND value_json IS NOT NULL AND content IS NULL)
);

COMMENT ON TABLE config_overrides IS
    'DB-backed overrides for the bundled config matrix (prompts, instructions, '
    'settings, guardrails). One row overrides one (family, kind, name); NULL '
    'family = global. File matrix is the immutable floor. Design: '
    'docs/superpowers/specs/2026-05-31-config-matrix-db-overrides-design.md.';

COMMIT;
