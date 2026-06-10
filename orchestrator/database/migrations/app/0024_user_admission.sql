-- migration:     0024_user_admission.sql
-- description:   App-side admission. Move user approval out of the Keycloak
--                `user` realm role and into a persisted column the
--                orchestrator checks per request. Keycloak keeps answering
--                "who are you" (authn); the app DB now answers "may you use
--                this product" (admission). This unblocks the bulk-approve
--                workflow, a pending list/notification, an approval audit
--                trail, and instant suspension (a per-request DB flag flips
--                immediately, unlike a realm role that only takes effect at
--                the next token refresh). See
--                docs/features/app_side_admission.md.
--
--                Columns:
--                  - is_approved       BOOLEAN NOT NULL DEFAULT FALSE — the
--                                      admission flag, checked by
--                                      require_approved_user.
--                  - approved_at       TIMESTAMPTZ — when admission was
--                                      granted (audit/history; kept on
--                                      suspension).
--                  - approved_by       UUID FK users(id) — which admin
--                                      approved. NULL + approved_at set means
--                                      "migrated from the Keycloak role /
--                                      system"; a real UUID means a human
--                                      clicked approve.
--                  - preferred_username TEXT — persisted at JIT time so
--                                      approval-time Gitea/cloud provisioning
--                                      can rebuild the ensure-user payload
--                                      from the row (today it is transient).
--
--                Backfill: ONLY admins. Deliberately NOT a blanket TRUE —
--                users rows already exist for never-admitted registrants
--                (JIT provisioning runs before the approval gate), so a
--                blanket backfill would silently admit strangers. Legacy
--                `user`-role holders self-migrate to the DB flag via the
--                login write-through in _resolve_user_from_claims on their
--                first request after deploy.
-- depends-on:    0023_thread_messages_seq.sql
-- expected:      < 50ms. Four column adds with constant/NULL defaults
--                (metadata-only, no table rewrite in PG >= 11). Backfill
--                UPDATE touches only the handful of admin rows.
-- locks:         AccessExclusive on users for the ALTER TABLEs (fast,
--                metadata-only).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

DO $$ BEGIN
    ALTER TABLE users
        ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE users
        ADD COLUMN approved_at TIMESTAMPTZ;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE users
        ADD COLUMN approved_by UUID REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE users
        ADD COLUMN preferred_username TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Backfill admins only (see header). approved_by stays NULL → "system".
UPDATE users SET is_approved = TRUE, approved_at = NOW()
WHERE is_admin = TRUE AND is_approved = FALSE;

COMMENT ON COLUMN users.is_approved IS
    'App-side admission flag. Checked per request by require_approved_user. '
    'Owned by the orchestrator, not the Keycloak realm role. See '
    'docs/features/app_side_admission.md.';
COMMENT ON COLUMN users.approved_by IS
    'Admin who approved this user. NULL + approved_at set = migrated from '
    'Keycloak role / system; a real UUID = a human clicked approve.';

COMMIT;
