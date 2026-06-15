-- migration:     0028_experts.sql
-- description:   DB-backed user/admin experts (User-Defined Experts, Slice 1).
--                Overlay model, exactly like config_overrides (0022): bundled
--                YAML experts in config/experts/ stay disk-canonical; this table
--                holds only user/admin rows. Delete a row => shipped behavior
--                returns. Adds project_experts (link + per-project default +
--                override) and jobs.expert_id (nullable, SET NULL on delete --
--                history is safe because jobs.resolved_config is frozen). The
--                jobs FK uses the two-phase NOT VALID -> VALIDATE pattern
--                (docs/db_migration.md) so squawk stays green; the column is
--                born all-NULL so VALIDATE is instant.
--                Design: docs/features/global_expert_management.md (Slice 1).
-- depends-on:    0001_initial.sql
-- expected:      < 1s on dev DB. New empty tables + one nullable FK column
--                (metadata-only ADD COLUMN in PostgreSQL 11+, no table rewrite).
-- locks:         AccessExclusiveLock on the new tables; brief on jobs for the
--                column add + constraint (validated against an all-NULL column).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS experts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(100) NOT NULL,        -- slug ^[a-z][a-z0-9_-]*$
    display_name VARCHAR(200) NOT NULL,
    description  TEXT,
    icon         VARCHAR(100) NOT NULL DEFAULT 'smart_toy',
    color        VARCHAR(7)   NOT NULL DEFAULT '#6B7280',
    tags         TEXT[]       NOT NULL DEFAULT '{}',
    expert_type  VARCHAR(10)  NOT NULL CHECK (expert_type IN ('worker', 'session')),
    config       JSONB        NOT NULL DEFAULT '{}',  -- fragment vs the type's base; never the merged result
    prompts      JSONB        NOT NULL DEFAULT '{}',  -- v1 keys: persona, instructions
    owner_id     UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    is_global    BOOLEAN      NOT NULL DEFAULT FALSE,
    version      INTEGER      NOT NULL DEFAULT 1,
    updated_by   UUID         REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_experts_name_owner ON experts (name, owner_id);
CREATE INDEX IF NOT EXISTS idx_experts_owner ON experts (owner_id);
CREATE INDEX IF NOT EXISTS idx_experts_type  ON experts (expert_type);

CREATE TABLE IF NOT EXISTS project_experts (
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    expert_id       UUID NOT NULL REFERENCES experts(id) ON DELETE CASCADE,
    default_for     VARCHAR(10) CHECK (default_for IN ('worker', 'session')),  -- NULL = linked, not default
    config_override JSONB,                        -- project-level tweaks on top of the expert fragment
    linked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, expert_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_project_default_expert
    ON project_experts (project_id, default_for) WHERE default_for IS NOT NULL;

-- jobs.expert_id: two-phase FK (NOT VALID then VALIDATE) per docs/db_migration.md.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS expert_id UUID;
DO $$ BEGIN
    ALTER TABLE jobs ADD CONSTRAINT jobs_expert_id_fkey
        FOREIGN KEY (expert_id) REFERENCES experts(id) ON DELETE SET NULL NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE jobs VALIDATE CONSTRAINT jobs_expert_id_fkey;

COMMENT ON TABLE experts IS
    'DB-backed user/admin experts (overlay over bundled config/experts/). '
    'config = fragment vs the expert_type base; prompts = {persona, instructions}. '
    'Design: docs/features/global_expert_management.md.';

COMMIT;
