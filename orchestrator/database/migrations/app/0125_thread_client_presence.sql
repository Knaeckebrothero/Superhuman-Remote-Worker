-- migration:     0125_thread_client_presence.sql
-- description:   Durable, lane-private attached-client presence for stateless
--                sessions. The owner-gated SSE stream renews one TTL row per
--                thread; executors use it only for permission/natural-pause UX.
-- depends-on:    0124_cloud_sync_marker_comment.sql
-- expected:      < 5s. New empty table plus one small btree index.
-- locks:         Brief REFERENCES lock on threads while the FK is created.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE thread_client_presence (
    thread_id    UUID        PRIMARY KEY
                             REFERENCES threads(id) ON DELETE CASCADE,
    refreshed_at TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,

    CONSTRAINT thread_client_presence_expiry_order
        CHECK (expires_at > refreshed_at)
);

CREATE INDEX idx_thread_client_presence_expires_at
    ON thread_client_presence (expires_at);

COMMENT ON TABLE thread_client_presence IS
    'Owner-gated SSE attestation that at least one browser is attached to a '
    'stateless session. One TTL row per thread deliberately collapses tabs: '
    'reload and reconnect renew the same row, and disconnect never deletes it. '
    'This is cooperative UX state only, never authorization, queue ownership, '
    'a fencing token, or a worker/finalizer liveness signal.';

COMMENT ON COLUMN thread_client_presence.refreshed_at IS
    'Database-clock time of the latest successful SSE establishment or renewal.';

COMMENT ON COLUMN thread_client_presence.expires_at IS
    'Database-clock presence deadline. Absence means no row or expires_at at or '
    'before clock_timestamp(); rows are retained and overwritten so cardinality '
    'remains bounded by threads.';

COMMIT;
