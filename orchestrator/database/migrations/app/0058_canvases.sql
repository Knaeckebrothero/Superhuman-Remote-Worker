-- migration:     0058_canvases.sql
-- description:   Durable presentation state for the Dynamic Canvas shared
--                stage. Content remains in its workspace/browser source; this
--                table stores only the thread-scoped logical selection.
-- depends-on:    0057_cloud_ro_mounts_staging.sql
-- expected:      < 1s (new empty table and two indexes)
-- locks:         Brief SHARE ROW EXCLUSIVE on threads for the foreign key
-- transactional: yes
-- ============================================================================

CREATE TABLE canvases (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id             UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    canvas_id             VARCHAR(64) NOT NULL DEFAULT 'main',
    source                JSONB,
    title                 TEXT,
    renderer              VARCHAR(32) NOT NULL DEFAULT 'auto',
    editable              BOOLEAN NOT NULL DEFAULT FALSE,
    alt_text              TEXT,
    presentation_revision BIGINT NOT NULL DEFAULT 0,
    source_fingerprint    TEXT,
    source_version        TEXT,
    origin_generation     UUID,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_canvases_thread_canvas UNIQUE (thread_id, canvas_id),
    CONSTRAINT ck_canvases_revision_nonnegative
        CHECK (presentation_revision >= 0),
    CONSTRAINT ck_canvases_title_length
        CHECK (title IS NULL OR char_length(title) <= 200),
    CONSTRAINT ck_canvases_alt_text_length
        CHECK (alt_text IS NULL OR char_length(alt_text) <= 1000),
    CONSTRAINT ck_canvases_source_shape CHECK (
        (source IS NULL
         AND source_fingerprint IS NULL
         AND source_version IS NULL
         AND origin_generation IS NULL
         AND title IS NULL
         AND alt_text IS NULL
         AND editable = FALSE
         AND renderer = 'auto')
        OR
        (source IS NOT NULL
         AND source_fingerprint IS NOT NULL
         AND title IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_canvases_origin_generation
    ON canvases (origin_generation)
    WHERE origin_generation IS NOT NULL;

COMMENT ON TABLE canvases IS
    'Thread-scoped Dynamic Canvas presentation pointers; source content is not copied here.';
COMMENT ON COLUMN canvases.canvas_id IS
    'Presentation slot. V1 application services accept only main.';
COMMENT ON COLUMN canvases.source IS
    'Server-normalized logical source; never contains credentials, proxy URLs, or workspace addresses.';
COMMENT ON COLUMN canvases.presentation_revision IS
    'Monotonic presentation-domain revision, advanced once per successful state transition.';
COMMENT ON COLUMN canvases.source_fingerprint IS
    'sha256 fingerprint of the canonical security-relevant logical source identity.';
COMMENT ON COLUMN canvases.source_version IS
    'Strong sha256 content version for file-backed sources; distinct from presentation_revision.';
COMMENT ON COLUMN canvases.origin_generation IS
    'Random revocable browser-origin generation for a live application trust unit.';
