-- migration:     0049_backfill_admin_grants.sql
-- description:   Seed max-level user-scope capability grants for admins.
--                Admins bypass the PDP at every PEP (capability_grants.py
--                takes is_admin; all PEPs short-circuit), so an admin with
--                ZERO grant rows is fully entitled — but the Grants UI (and
--                any admin-agnostic caller) renders that empty list as "no
--                permission". That illusion drove the vm_upgrade incident
--                response ("give him the VM grant" for a user who was already
--                admin). Make the data tell the truth: every current admin
--                gets explicit max-level rows. Forward-fill lives in
--                auth.py (_resolve_user_from_claims → seed_admin_grants) on
--                first admin login / promotion; the seed set is kept in
--                lockstep with PostgresDB._ADMIN_GRANT_SEED.
--                CAVEAT: on admin DEMOTION these become live grants for the
--                ex-admin (restrict-only meet still applies) — review the
--                user's rows when demoting.
--                docs/issues/vm_upgrade_pause_workspace_reaped_before_approval.md
--                (fix 5.2), following 0030's idempotent backfill precedent.
-- depends-on:    0030_capability_grants.sql
-- expected:      < 1s (a handful of admin rows).
-- locks:         Row-level only (plain INSERT ... ON CONFLICT DO NOTHING).
-- transactional: yes

INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'vm_workspace', 'true'::jsonb, NULL
FROM users WHERE is_admin = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;

INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'shell_tools', 'true'::jsonb, NULL
FROM users WHERE is_admin = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;

INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'delegation', 'true'::jsonb, NULL
FROM users WHERE is_admin = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;

INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'autonomy_ceiling', '"full"'::jsonb, NULL
FROM users WHERE is_admin = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;

INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'permission_mode', '"autonomous"'::jsonb, NULL
FROM users WHERE is_admin = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;
