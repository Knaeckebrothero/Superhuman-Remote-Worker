-- migration:     0009_srw_sessions.sql
-- description:   Cookie BFF sessions + OAuth pre-auth state for the
--                cockpit-side auth refactor.
--
--                PR 1 of docs/features/auth_bff_and_api_tokens.md. The
--                orchestrator becomes a Backend-for-Frontend: it holds
--                Keycloak access/refresh/id tokens server-side keyed by an
--                opaque session UUID, and the browser only ever sees the
--                HttpOnly srw_session cookie. The pre-auth table parks the
--                PKCE verifier + return-to URL between /auth/login and
--                /auth/callback (signed cookies were rejected — too much
--                key management for the value).
--
--                Drops the dormant pre-Keycloak `sessions` table in the
--                same step. That table has zero callers (see comment at
--                orchestrator/security/auth.py:9) and its
--                `session_key TEXT PRIMARY KEY` / `csrf_token` shape
--                doesn't fit the new design.
-- depends-on:    0008_thread_awaiting_user.sql
-- expected:      < 1s. Two CREATE TABLE, four indexes, one DROP. All
--                metadata-only; the dropped table is empty.
-- locks:         AccessExclusiveLock briefly per CREATE TABLE and on the
--                DROP. No live readers/writers — pre-Keycloak code is gone.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Drop the dormant pre-Keycloak session table. Comment at
-- orchestrator/security/auth.py:9 confirms session-based auth was
-- retired in favour of Keycloak OIDC; no callers remain.
DROP TABLE IF EXISTS sessions;

-- BFF session rows. One row per logged-in browser. The cookie value is
-- this row's UUID; nothing about the user identity lives in the cookie
-- itself. Access/refresh/id tokens are stored encrypted-at-rest only by
-- way of Postgres' on-disk encryption; the threat model here treats DB
-- compromise as game-over already (the same DB stores datasource
-- credentials).
CREATE TABLE IF NOT EXISTS srw_sessions (
    id                   UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id              UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kc_sub               TEXT         NOT NULL,
    kc_sid               TEXT,
    access_token         TEXT         NOT NULL,
    refresh_token        TEXT         NOT NULL,
    id_token             TEXT         NOT NULL,
    access_expires_at    TIMESTAMPTZ  NOT NULL,
    absolute_expires_at  TIMESTAMPTZ  NOT NULL,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    user_agent           TEXT,
    created_ip           INET,
    revoked_at           TIMESTAMPTZ
);

-- "What sessions does this user have?" — settings UI, admin force-logout.
-- Partial index over un-revoked rows keeps it small.
CREATE INDEX IF NOT EXISTS idx_srw_sessions_user_active
    ON srw_sessions (user_id)
    WHERE revoked_at IS NULL;

-- Back-channel logout: Keycloak posts a logout token bearing the KC SID;
-- we delete every session row with that sid.
CREATE INDEX IF NOT EXISTS idx_srw_sessions_kc_sid
    ON srw_sessions (kc_sid)
    WHERE kc_sid IS NOT NULL;

-- Cleanup loop hot path: "rows past their absolute lifetime".
CREATE INDEX IF NOT EXISTS idx_srw_sessions_absolute_expires
    ON srw_sessions (absolute_expires_at);

COMMENT ON TABLE srw_sessions IS
    'BFF session rows. Cookie value = id (UUID). KC tokens held server-side '
    'and refreshed in place. See docs/features/auth_bff_and_api_tokens.md §1.2.';
COMMENT ON COLUMN srw_sessions.kc_sid IS
    'Keycloak session ID from id_token claim "sid". Used for back-channel logout.';
COMMENT ON COLUMN srw_sessions.absolute_expires_at IS
    'Anchored to Keycloak refresh-token TTL at session creation. After this, '
    'no refresh attempt is made; user must re-authenticate.';
COMMENT ON COLUMN srw_sessions.last_seen_at IS
    'Idle timeout anchor: validator rejects if last_seen_at + idle < now(). '
    'Touched on every authenticated request.';

-- OAuth pre-auth state. Parks the PKCE verifier and the cockpit
-- return-to URL between the GET /auth/login redirect and the
-- GET /auth/callback that follows the user's KC round-trip. Single-use:
-- consumed_at acts as a one-shot guard, mirroring magic_link_tokens.
CREATE TABLE IF NOT EXISTS srw_pre_auth_states (
    id            UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    state         TEXT         NOT NULL,
    pkce_verifier TEXT         NOT NULL,
    return_to     TEXT         NOT NULL DEFAULT '/',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ  NOT NULL,
    consumed_at   TIMESTAMPTZ
);

-- Callback hot path: validator matches the `state` query param to a row.
CREATE INDEX IF NOT EXISTS idx_srw_pre_auth_states_state
    ON srw_pre_auth_states (state)
    WHERE consumed_at IS NULL;

-- Cleanup loop: find rows past TTL, regardless of consumption.
CREATE INDEX IF NOT EXISTS idx_srw_pre_auth_states_expires
    ON srw_pre_auth_states (expires_at);

COMMENT ON TABLE srw_pre_auth_states IS
    'Server-side OAuth state + PKCE verifier between /auth/login and '
    '/auth/callback. 5-minute TTL, single-use via consumed_at CAS.';

COMMIT;
