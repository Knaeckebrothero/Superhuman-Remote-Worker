-- migration:     0025_security_events.sql
-- description:   Security event log (cross-user 403 audit). Every access
--                gate in security/access.py — plus _require_admin and the
--                IDE proxy deny paths in main.py — currently denies
--                silently: 1000 UUID-probe attempts against another user's
--                resources produce 1000 identical 403s and zero detection
--                signal. This table is the durable sink for those denials
--                (a structured log line fires alongside each insert).
--                Closes M1.B #4 in docs/multi_tenancy.md. See
--                docs/features/security_event_log.md.
--
--                Design notes:
--                  - user_id has NO FK to users(id): rows must survive
--                    user deletion — forensics about a deleted account are
--                    the rows you want most.
--                  - resource_id is TEXT, not UUID: ids arriving at the
--                    gates include non-UUID slugs and paths.
--                  - real_is_admin/view_as record the admin "view as user"
--                    shadow (docs/features/admin_view_as_user.md) so an
--                    admin exercising the toggle is distinguishable from a
--                    genuine cross-user attempt.
--                  - No org_id: M2 multi-org is deferred; user_id suffices
--                    to backfill an org mapping retroactively.
--                  - Growth is bounded by the retention sweeper in main.py
--                    (SECURITY_EVENTS_RETENTION_DAYS, default 90). Writes
--                    only happen on the post-auth 403 path, so anonymous
--                    callers can't flood the table.
-- depends-on:    0024_user_admission.sql
-- expected:      < 50ms. New empty table + two btree indexes.
-- locks:         none of consequence (CREATE TABLE on a new relation).
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS security_events (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type    TEXT NOT NULL,
    user_id       UUID,
    auth_method   TEXT,
    real_is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    view_as       BOOLEAN NOT NULL DEFAULT FALSE,
    resource_type TEXT NOT NULL,
    resource_id   TEXT,
    method        TEXT,
    path          TEXT,
    detail        TEXT,
    client_ip     TEXT
);

COMMENT ON TABLE security_events IS
    'Denied-access audit log. One row per 403 raised by a security/access.py '
    'gate (plus _require_admin and IDE proxy denials). Written best-effort — '
    'a failed insert never blocks the 403. Pruned by the retention sweeper. '
    'See docs/features/security_event_log.md.';
COMMENT ON COLUMN security_events.event_type IS
    'access_denied (resource gates) | admin_denied (_require_admin). '
    'Open enum — future: login_failed, token_revoked, ...';
COMMENT ON COLUMN security_events.user_id IS
    'Authenticated caller. Deliberately no FK — rows outlive user deletion.';
COMMENT ON COLUMN security_events.view_as IS
    'TRUE when an admin had the X-Admin-View-As shadow on: the denial came '
    'from narrowed visibility, not a genuine cross-user attempt.';

CREATE INDEX IF NOT EXISTS idx_security_events_created_at
    ON security_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_security_events_user_created
    ON security_events (user_id, created_at DESC);

COMMIT;
