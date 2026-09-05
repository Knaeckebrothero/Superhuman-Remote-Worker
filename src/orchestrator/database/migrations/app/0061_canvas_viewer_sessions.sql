-- migration:     0061_canvas_viewer_sessions.sql
-- description:   Hashed, short-lived authentication and presence state for
--                isolated Dynamic Canvas live-application origins.
-- depends-on:    0060_canvas_events_epoch_comment.sql
-- expected:      < 1s (three new empty tables, indexes, and small triggers)
-- locks:         Brief SHARE ROW EXCLUSIVE on users, threads, canvases, and
--                srw_sessions while foreign keys/triggers are installed
-- transactional: yes
-- ============================================================================

CREATE TABLE canvas_origin_sessions (
    id                           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_secret_hash          VARCHAR(64) NOT NULL UNIQUE,
    user_id                      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    thread_id                    UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    canvas_id                    VARCHAR(64) NOT NULL DEFAULT 'main',
    parent_srw_session_id        UUID REFERENCES srw_sessions(id) ON DELETE SET NULL,
    issued_presentation_revision BIGINT NOT NULL,
    source_fingerprint           TEXT NOT NULL,
    workspace_generation         UUID NOT NULL,
    origin_generation            UUID NOT NULL,
    embedding_origin             TEXT NOT NULL,
    cookie_mode                  VARCHAR(32) NOT NULL,
    expires_at                   TIMESTAMPTZ NOT NULL,
    last_renewed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at                   TIMESTAMPTZ,
    revocation_reason            VARCHAR(64),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_canvas_origin_session_hash
        CHECK (session_secret_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_canvas_origin_session_revision
        CHECK (issued_presentation_revision > 0),
    CONSTRAINT ck_canvas_origin_session_cookie_mode
        CHECK (cookie_mode IN ('development-cookie-free', 'psl-isolated')),
    CONSTRAINT ck_canvas_origin_session_revocation
        CHECK ((revoked_at IS NULL) = (revocation_reason IS NULL))
);

CREATE INDEX idx_canvas_origin_sessions_active_identity
    ON canvas_origin_sessions (origin_generation, thread_id, canvas_id)
    WHERE revoked_at IS NULL;
CREATE INDEX idx_canvas_origin_sessions_parent
    ON canvas_origin_sessions (parent_srw_session_id)
    WHERE revoked_at IS NULL AND parent_srw_session_id IS NOT NULL;
CREATE INDEX idx_canvas_origin_sessions_user_active
    ON canvas_origin_sessions (user_id)
    WHERE revoked_at IS NULL;
CREATE INDEX idx_canvas_origin_sessions_expires
    ON canvas_origin_sessions (expires_at);

CREATE TABLE canvas_view_attachments (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id               UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    thread_id             UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    canvas_id             VARCHAR(64) NOT NULL DEFAULT 'main',
    parent_srw_session_id UUID REFERENCES srw_sessions(id) ON DELETE SET NULL,
    origin_session_id     UUID REFERENCES canvas_origin_sessions(id) ON DELETE SET NULL,
    bridge_nonce_hash     VARCHAR(64) NOT NULL,
    embedding_origin      TEXT NOT NULL,
    expires_at            TIMESTAMPTZ NOT NULL,
    last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at             TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_canvas_attachment_nonce_hash
        CHECK (bridge_nonce_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX idx_canvas_view_attachments_active
    ON canvas_view_attachments (thread_id, canvas_id, user_id)
    WHERE closed_at IS NULL;
CREATE INDEX idx_canvas_view_attachments_origin_session
    ON canvas_view_attachments (origin_session_id)
    WHERE closed_at IS NULL AND origin_session_id IS NOT NULL;
CREATE INDEX idx_canvas_view_attachments_expires
    ON canvas_view_attachments (expires_at);

CREATE TABLE canvas_view_bootstraps (
    id                             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash                     VARCHAR(64) NOT NULL UNIQUE,
    attachment_id                  UUID NOT NULL REFERENCES canvas_view_attachments(id) ON DELETE CASCADE,
    expected_presentation_revision BIGINT NOT NULL,
    source_fingerprint             TEXT NOT NULL,
    workspace_generation           UUID NOT NULL,
    origin_generation              UUID NOT NULL,
    expires_at                     TIMESTAMPTZ NOT NULL,
    consumed_at                    TIMESTAMPTZ,
    consumed_origin_session_id     UUID REFERENCES canvas_origin_sessions(id) ON DELETE SET NULL,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_canvas_bootstrap_hash
        CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_canvas_bootstrap_revision
        CHECK (expected_presentation_revision > 0)
);

CREATE INDEX idx_canvas_view_bootstraps_pending
    ON canvas_view_bootstraps (token_hash)
    WHERE consumed_at IS NULL;
CREATE INDEX idx_canvas_view_bootstraps_expires
    ON canvas_view_bootstraps (expires_at);

-- Transaction-delivered notifications contain opaque row identifiers only.
-- A listener can miss one, so long exchanges still revalidate PostgreSQL on a
-- bounded interval; this trigger only accelerates cancellation.
CREATE FUNCTION notify_canvas_origin_session_change() RETURNS trigger AS $$
BEGIN
    IF OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL THEN
        PERFORM pg_notify(
            'canvas_session_changes',
            json_build_object('kind', 'session', 'id', NEW.id)::text
        );
    ELSIF OLD.expires_at IS DISTINCT FROM NEW.expires_at THEN
        PERFORM pg_notify(
            'canvas_session_changes',
            json_build_object('kind', 'session_renewed', 'id', NEW.id)::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_canvas_origin_session_change
AFTER UPDATE OF revoked_at, expires_at ON canvas_origin_sessions
FOR EACH ROW EXECUTE FUNCTION notify_canvas_origin_session_change();

CREATE FUNCTION revoke_canvas_sessions_for_retired_origin() RETURNS trigger AS $$
BEGIN
    IF OLD.origin_generation IS NOT NULL
       AND OLD.origin_generation IS DISTINCT FROM NEW.origin_generation THEN
        UPDATE canvas_origin_sessions
        SET revoked_at = COALESCE(revoked_at, now()),
            revocation_reason = COALESCE(revocation_reason, 'origin_retired'),
            updated_at = now()
        WHERE thread_id = OLD.thread_id
          AND canvas_id = OLD.canvas_id
          AND origin_generation = OLD.origin_generation
          AND revoked_at IS NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_canvas_revoke_retired_origin
AFTER UPDATE OF origin_generation ON canvases
FOR EACH ROW EXECUTE FUNCTION revoke_canvas_sessions_for_retired_origin();

CREATE FUNCTION revoke_canvas_sessions_for_bff_session() RETURNS trigger AS $$
BEGIN
    UPDATE canvas_origin_sessions
    SET revoked_at = COALESCE(revoked_at, now()),
        revocation_reason = COALESCE(revocation_reason, 'parent_session_ended'),
        parent_srw_session_id = NULL,
        updated_at = now()
    WHERE parent_srw_session_id = OLD.id
      AND revoked_at IS NULL;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_canvas_revoke_bff_session
BEFORE DELETE ON srw_sessions
FOR EACH ROW EXECUTE FUNCTION revoke_canvas_sessions_for_bff_session();

CREATE FUNCTION revoke_canvas_sessions_for_user_admission() RETURNS trigger AS $$
BEGIN
    IF OLD.is_approved IS TRUE AND NEW.is_approved IS NOT TRUE THEN
        UPDATE canvas_origin_sessions
        SET revoked_at = COALESCE(revoked_at, now()),
            revocation_reason = COALESCE(revocation_reason, 'user_not_approved'),
            updated_at = now()
        WHERE user_id = NEW.id AND revoked_at IS NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_canvas_revoke_user_admission
AFTER UPDATE OF is_approved ON users
FOR EACH ROW EXECUTE FUNCTION revoke_canvas_sessions_for_user_admission();

COMMENT ON TABLE canvas_origin_sessions IS
    'Short-lived isolated Canvas gateway credentials; only SHA-256 secret hashes are persisted.';
COMMENT ON TABLE canvas_view_attachments IS
    'Non-credential frame/window presence records linked to a shared origin session after bootstrap.';
COMMENT ON TABLE canvas_view_bootstraps IS
    'Single-use, short-lived iframe bootstrap credentials stored only as SHA-256 hashes.';
