-- migration:     0062_canvas_bootstrap_exchange.sql
-- description:   Replace transferable Canvas bootstrap URL bearers with a
--                browser-bound, BFF-authorized challenge/exchange state.
-- depends-on:    0061_canvas_viewer_sessions.sql
-- expected:      < 1s while the Canvas viewer remains default-off
-- locks:         Brief ACCESS EXCLUSIVE on canvas_view_bootstraps
-- transactional: yes
-- ============================================================================

-- Slice 3B has never been enabled in a hosted deployment. Pending and consumed
-- bootstrap/attachment rows are short-lived browser-presence material rather
-- than an audit log, so discard the old URL-bearer state before tightening the
-- table contract. Origin sessions remain valid and independently revocable.
-- Existing canvas_origin_sessions remain valid and independently revocable.
DELETE FROM canvas_view_bootstraps;
DELETE FROM canvas_view_attachments;

-- This field was designed after 0061 had already run in development. Keep 0061
-- byte-for-byte immutable and introduce it in the next migration instead.
ALTER TABLE canvas_view_attachments
    ADD COLUMN cookie_mode VARCHAR(32) NOT NULL,
    ADD CONSTRAINT ck_canvas_attachment_cookie_mode
        CHECK (cookie_mode IN ('development-cookie-free', 'psl-isolated'));

DROP INDEX IF EXISTS idx_canvas_view_bootstraps_pending;
ALTER TABLE canvas_view_bootstraps
    DROP CONSTRAINT IF EXISTS canvas_view_bootstraps_token_hash_key;
ALTER TABLE canvas_view_bootstraps
    DROP CONSTRAINT IF EXISTS ck_canvas_bootstrap_hash;

ALTER TABLE canvas_view_bootstraps
    RENAME COLUMN token_hash TO exchange_token_hash;
ALTER TABLE canvas_view_bootstraps
    ALTER COLUMN exchange_token_hash DROP NOT NULL;

ALTER TABLE canvas_view_bootstraps
    ADD COLUMN challenge_hash VARCHAR(64),
    ADD COLUMN browser_binding_hash VARCHAR(64),
    ADD COLUMN ready_receipt_hash VARCHAR(64),
    ADD COLUMN authorized_at TIMESTAMPTZ;

-- Bootstrap rows are ephemeral authorization state. Keeping a consumed row
-- after its origin session is deleted both adds no audit value and would make
-- ON DELETE SET NULL violate the paired consumed-state constraint below.
ALTER TABLE canvas_view_bootstraps
    DROP CONSTRAINT canvas_view_bootstraps_consumed_origin_session_id_fkey,
    ADD CONSTRAINT canvas_view_bootstraps_consumed_origin_session_id_fkey
        FOREIGN KEY (consumed_origin_session_id)
        REFERENCES canvas_origin_sessions(id) ON DELETE CASCADE;

ALTER TABLE canvas_view_bootstraps
    ADD CONSTRAINT uq_canvas_bootstrap_attachment UNIQUE (attachment_id),
    ADD CONSTRAINT ck_canvas_bootstrap_exchange_hash
        CHECK (exchange_token_hash IS NULL OR exchange_token_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_canvas_bootstrap_challenge_hash
        CHECK (challenge_hash IS NULL OR challenge_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_canvas_bootstrap_binding_hash
        CHECK (browser_binding_hash IS NULL OR browser_binding_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_canvas_bootstrap_receipt_hash
        CHECK (ready_receipt_hash IS NULL OR ready_receipt_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_canvas_bootstrap_exchange_state CHECK (
        (
            challenge_hash IS NULL
            AND browser_binding_hash IS NULL
            AND ready_receipt_hash IS NULL
            AND exchange_token_hash IS NULL
            AND authorized_at IS NULL
            AND consumed_at IS NULL
            AND consumed_origin_session_id IS NULL
        ) OR (
            challenge_hash IS NOT NULL
            AND browser_binding_hash IS NOT NULL
            AND ready_receipt_hash IS NOT NULL
            AND exchange_token_hash IS NULL
            AND authorized_at IS NULL
            AND consumed_at IS NULL
            AND consumed_origin_session_id IS NULL
        ) OR (
            challenge_hash IS NOT NULL
            AND browser_binding_hash IS NOT NULL
            AND ready_receipt_hash IS NOT NULL
            AND exchange_token_hash IS NOT NULL
            AND authorized_at IS NOT NULL
            AND (
                (consumed_at IS NULL AND consumed_origin_session_id IS NULL)
                OR (
                    consumed_at IS NOT NULL
                    AND consumed_origin_session_id IS NOT NULL
                )
            )
        )
    );

-- squawk-ignore require-concurrent-index-creation
CREATE UNIQUE INDEX idx_canvas_view_bootstraps_exchange_token
    ON canvas_view_bootstraps (exchange_token_hash)
    WHERE exchange_token_hash IS NOT NULL;
-- squawk-ignore require-concurrent-index-creation
CREATE INDEX idx_canvas_view_bootstraps_pending
    ON canvas_view_bootstraps (attachment_id, expires_at)
    WHERE consumed_at IS NULL;

COMMENT ON TABLE canvas_view_bootstraps IS
    'Single-use Canvas bootstrap challenge/exchange state; every browser and authorization secret is stored only as a purpose-separated SHA-256 hash.';
