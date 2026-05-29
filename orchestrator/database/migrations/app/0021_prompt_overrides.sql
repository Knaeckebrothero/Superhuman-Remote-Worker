-- migration:     0021_prompt_overrides.sql
-- description:   DB-backed prompt overrides. Bundled config/ prompt and
--                instruction files remain the immutable floor; a row here
--                overrides resolution for exactly one (family, kind, name).
--                NULL family = global default (applies to every family).
--
--                Resolution order (src/core/loader.py MatrixResolver.load):
--                family-specific override -> global override -> bundled file.
--                The lookup is flag-gated (PROMPT_DB_OVERRIDES_ENABLED) and an
--                empty table is byte-identical to today. Overrides are read by
--                the agent at job first-run and frozen into
--                jobs.resolved_config, so reproducibility needs no versioning.
--                Design: docs/features/prompt_editing_page.md (v1).
--
--                `kind` is the resolver subsection
--                (MatrixResolver.MATRIX_SUBSECTION): 'prompts' or
--                'instructions'. `name` is the resolver entry_type
--                (e.g. 'persona', 'systemprompt', 'todo_guide').
-- depends-on:    0020_thread_messages_window_index.notx.sql
-- expected:      < 100ms. New table + two indexes; no existing-data rewrite.
-- locks:         No existing-table locks. New table; the FK additions to users
--                take a brief ShareRowExclusive on that table for validation,
--                covered by lock_timeout.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS prompt_overrides (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    family          VARCHAR(64),            -- NULL = global default (all families)
    kind            VARCHAR(32) NOT NULL
                      CHECK (kind IN ('prompts', 'instructions')),
    name            VARCHAR(128) NOT NULL,  -- resolver entry_type, e.g. 'persona'

    content         TEXT NOT NULL,
    content_format  VARCHAR(16) NOT NULL DEFAULT 'text'
                      CHECK (content_format IN ('text', 'markdown', 'jinja', 'yaml')),
    notes           TEXT,

    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One override per (family, kind, name). COALESCE folds NULL family into a
-- single global slot (Postgres treats NULLs as distinct otherwise). This is
-- also the ON CONFLICT inference target for upserts from the admin API.
CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_override
    ON prompt_overrides (COALESCE(family, ''), kind, name);

-- Lookup path for the agent's per-family read at job first-run.
CREATE INDEX IF NOT EXISTS idx_prompt_override_lookup
    ON prompt_overrides (family, kind, name);

COMMENT ON TABLE prompt_overrides IS
    'DB-backed overrides for bundled config/ prompts. One row overrides one '
    '(family, kind, name); NULL family = global. Resolved beneath the matrix '
    'resolver and frozen into jobs.resolved_config. Design: '
    'docs/features/prompt_editing_page.md (v1).';

COMMENT ON COLUMN prompt_overrides.kind IS
    'Resolver subsection (MatrixResolver.MATRIX_SUBSECTION): prompts | instructions.';

COMMENT ON COLUMN prompt_overrides.family IS
    'Model family (family_of(model)); NULL applies to all families. Matched '
    'against MatrixResolver.model_family at resolution time.';

COMMIT;
