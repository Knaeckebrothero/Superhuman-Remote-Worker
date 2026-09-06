-- migration:     0030_capability_grants.sql
-- description:   Capability grants (User-Defined Experts, Slice 2). Scoped twin of
--                config_overrides; generalizes users.can_use_vm into deny-by-default
--                per-principal entitlements (user>project>global>default, restrict-only)
--                gating which tools/models/autonomy a config may use. Append-only audit
--                (decision 23). Grandfathers existing approved users for the base-shipped
--                always-on capabilities (shell, delegation) so deny-by-default is a no-op
--                on upgrade (decision 19). 0029 was claimed by add_mistral_provider.
--                Design: docs/features/global_expert_management.md (Slice 2).
-- depends-on:    0001_initial.sql
-- expected:      < 1s. Two empty tables + INSERT..SELECT backfills over (small) users.
-- locks:         AccessExclusiveLock on the new tables only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS capability_grants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_kind  TEXT NOT NULL CHECK (scope_kind IN ('user', 'project', 'global')),
    scope_id    UUID,                          -- NULL for global; no FK (polymorphic)
    key         TEXT NOT NULL,
    value_json  JSONB NOT NULL,
    granted_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- One grant per (scope, key). NULLS NOT DISTINCT (PG15+, fleet is PG15/16) so the
-- single global row per key collides correctly despite scope_id being NULL.
CREATE UNIQUE INDEX IF NOT EXISTS uq_grants_scope_key
    ON capability_grants (scope_kind, scope_id, key) NULLS NOT DISTINCT;
CREATE INDEX IF NOT EXISTS idx_grants_scope ON capability_grants (scope_kind, scope_id);

CREATE TABLE IF NOT EXISTS capability_grant_audit (
    id          BIGSERIAL PRIMARY KEY,
    actor       UUID,
    scope_kind  TEXT NOT NULL,
    scope_id    UUID,
    key         TEXT NOT NULL,
    old_value   JSONB,
    new_value   JSONB,
    action      TEXT NOT NULL CHECK (action IN ('set', 'update', 'revoke')),
    reason      TEXT,
    at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_grant_audit_scope ON capability_grant_audit (scope_kind, scope_id);

-- Migrate the existing one-off VM grant (decision 8).
INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'vm_workspace', 'true'::jsonb, NULL
FROM users WHERE can_use_vm = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;

-- GRANDFATHER: the operator base ships shell + delegation enabled for every job, so
-- without this every existing non-admin user's jobs would be rejected under deny-by-
-- default. Grant both to all currently-approved users; NEW users stay deny-by-default.
INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'shell_tools', 'true'::jsonb, NULL
FROM users WHERE is_approved = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;
INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'delegation', 'true'::jsonb, NULL
FROM users WHERE is_approved = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;

COMMENT ON TABLE capability_grants IS
  'Scoped capability entitlements (Slice 2). user>project>global>default, restrict-only, deny-by-default. Deleting a user/project must delete its grant rows in app code — no cascade fires.';

COMMIT;
