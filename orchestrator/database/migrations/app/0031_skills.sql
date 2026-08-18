-- migration:     0031_skills.sql
-- description:   DB-backed user/admin Agent Skills (Slice 1, authoring foundation).
--                Mirrors experts (0028): bundled SKILL.md skills in config/skills/
--                stay disk-canonical; this table holds user/admin rows. A skill is
--                a directory, so skill_files holds the file tree (SKILL.md + refs).
--                The SKILL.md is canonical; name/description are denormalized here
--                for the catalog/menu. No project junction and no jobs.skill_id yet
--                (project skills come from the Gitea repo; expert<->skill bindings
--                are a later slice). Deleting a row cascades its files away.
--                Design: docs/features/agent_skills.md (Slice 1).
-- depends-on:    0001_initial.sql
-- expected:      < 1s on dev DB. Two new empty tables, no table rewrite.
-- locks:         AccessExclusiveLock on the two new tables only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS skills (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(100) NOT NULL,        -- slug ^[a-z][a-z0-9_-]*$, from SKILL.md
    display_name VARCHAR(200) NOT NULL,
    description  TEXT,
    icon         VARCHAR(100) NOT NULL DEFAULT 'extension',
    color        VARCHAR(7)   NOT NULL DEFAULT '#6B7280',
    tags         TEXT[]       NOT NULL DEFAULT '{}',
    owner_id     UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    is_global    BOOLEAN      NOT NULL DEFAULT FALSE,
    version      INTEGER      NOT NULL DEFAULT 1,
    updated_by   UUID         REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_name_owner ON skills (name, owner_id);
CREATE INDEX IF NOT EXISTS idx_skills_owner ON skills (owner_id);

CREATE TABLE IF NOT EXISTS skill_files (
    skill_id  UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    path      TEXT NOT NULL,            -- relative, e.g. 'SKILL.md', 'references/x.md'
    content   TEXT NOT NULL,            -- UTF-8; binary assets deferred (Slice 4)
    PRIMARY KEY (skill_id, path)
);

COMMENT ON TABLE skills IS
    'DB-backed user/admin Agent Skills (overlay over bundled config/skills/). '
    'name/description denormalized from the canonical SKILL.md in skill_files. '
    'Design: docs/features/agent_skills.md.';

COMMIT;
