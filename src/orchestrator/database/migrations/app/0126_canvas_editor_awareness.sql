-- migration:     0126_canvas_editor_awareness.sql
-- description:   Durable, lane-independent Canvas editor courtesy leases.
--                One row per browser editor session carries a monotonic
--                client sequence, DB-clock TTL, and an idle tombstone.
-- depends-on:    0125_thread_client_presence.sql
-- expected:      < 5s. New empty table plus one small expiry index.
-- locks:         Brief REFERENCES lock on canvases while the FK is created.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';
SET LOCAL timezone                            = 'UTC';

CREATE TABLE canvas_editor_awareness (
    thread_id             UUID         NOT NULL,
    canvas_id             VARCHAR(64)  NOT NULL DEFAULT 'main',
    editing_session_id    VARCHAR(128) NOT NULL,
    sender_id             UUID         NOT NULL DEFAULT gen_random_uuid(),
    state                 VARCHAR(16)  NOT NULL,
    client_seq            BIGINT       NOT NULL,
    path                  TEXT         NOT NULL,
    presentation_revision BIGINT       NOT NULL,
    source_version        VARCHAR(71)  NOT NULL,
    refreshed_at          TIMESTAMPTZ  NOT NULL,
    expires_at            TIMESTAMPTZ  NOT NULL,

    CONSTRAINT pk_canvas_editor_awareness
        PRIMARY KEY (thread_id, canvas_id, editing_session_id),
    CONSTRAINT uq_canvas_editor_awareness_sender UNIQUE (sender_id),
    CONSTRAINT fk_canvas_editor_awareness_canvas
        FOREIGN KEY (thread_id, canvas_id)
        REFERENCES canvases (thread_id, canvas_id) ON DELETE CASCADE,
    CONSTRAINT ck_canvas_editor_awareness_main
        CHECK (canvas_id = 'main'),
    CONSTRAINT ck_canvas_editor_awareness_session_id
        CHECK (editing_session_id ~ '^[A-Za-z0-9_-]{8,128}$'),
    CONSTRAINT ck_canvas_editor_awareness_state
        CHECK (state IN ('editing', 'idle')),
    CONSTRAINT ck_canvas_editor_awareness_client_seq
        CHECK (client_seq > 0),
    CONSTRAINT ck_canvas_editor_awareness_path
        CHECK (char_length(path) BETWEEN 1 AND 4096),
    CONSTRAINT ck_canvas_editor_awareness_revision
        CHECK (presentation_revision > 0),
    CONSTRAINT ck_canvas_editor_awareness_source_version
        CHECK (source_version ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT ck_canvas_editor_awareness_expiry
        CHECK (
            (state = 'editing' AND expires_at > refreshed_at)
            OR (state = 'idle' AND expires_at = refreshed_at)
        )
);

CREATE INDEX idx_canvas_editor_awareness_expires_at
    ON canvas_editor_awareness (expires_at);

COMMENT ON TABLE canvas_editor_awareness IS
    'Owner-authenticated Canvas editor courtesy leases. Per-editor rows keep '
    'tabs independent; idle tombstones and client_seq reject reordered stale '
    'renewals. This is UX state only, never authorization or execution lease.';

COMMENT ON COLUMN canvas_editor_awareness.sender_id IS
    'Server-minted stable public fan-out identity for this editor row.';

COMMENT ON COLUMN canvas_editor_awareness.client_seq IS
    'Client-monotonic sequence. Lower values never mutate the row; equal values '
    'are idempotent only when the complete state and Canvas identity match.';

COMMENT ON COLUMN canvas_editor_awareness.expires_at IS
    'Database-clock editing deadline. Idle tombstones set expires_at equal to '
    'refreshed_at and remain briefly so delayed lower-sequence renewals lose.';

COMMIT;
