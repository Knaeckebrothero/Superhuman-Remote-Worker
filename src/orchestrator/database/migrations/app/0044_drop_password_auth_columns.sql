-- migration:     0044_drop_password_auth_columns.sql
-- description:   Drop users.password_hash and users.email_verified — relics
--                of the pre-Keycloak password/cookie-session auth design that
--                lost to Keycloak OIDC + the BFF. The methods that read/wrote
--                them (get_user_by_email_with_auth, set_user_password,
--                set_email_verified, create_user_with_password,
--                upsert_default_user, migrate_existing_users_verified) were
--                test-only dead code, deleted in the same change. The
--                email_verified read in security/auth.py is the OIDC token
--                CLAIM, not this column. QW-5,
--                docs/features/database_roadmap.md Phase 1.
-- depends-on:    0001_initial.sql
-- expected:      < 1s (metadata-only column drops; no table rewrite).
-- locks:         ACCESS EXCLUSIVE on users, briefly.
-- transactional: yes

ALTER TABLE users DROP COLUMN IF EXISTS password_hash;
ALTER TABLE users DROP COLUMN IF EXISTS email_verified;
