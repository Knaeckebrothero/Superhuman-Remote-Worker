-- migration:     0073_canvas_snapshots.sql
-- description:   Durable last-published copy of a file Canvas, so a presentation
--                survives its workspace. Design + signed-off architectural
--                departure: docs/features/canvas_durable_presentation.md.
--
--                Until now a Canvas was strictly a pointer: `canvases` stored
--                the logical selection and every read re-materialized the bytes
--                over host-key-pinned SSH from the live workspace pod. That made
--                the presentation only as durable as the pod. Sessions suspend
--                to S3 on idle and their workspace is deleted, and a rebuilt
--                workspace always mints a new Canvas generation (its SSH host
--                key lives on a pod-private emptyDir), so a returning user found
--                a permanently `unavailable` Canvas and had to ask the agent to
--                present the same file again.
--
--                This table is the metadata half of the fix. The BYTES live in
--                the object store (the existing snapshot bucket, via
--                SnapshotService.put_blob) — never in Postgres. Only the key and
--                the identity needed to decide whether that object may be served
--                are stored here.
--
--                Serving invariant, enforced in the application layer: a
--                snapshot is served ONLY when its `source_version` equals the
--                live `canvases.source_version`. Any disagreement means the
--                snapshot is stale, and stale snapshots are ignored, never
--                repaired. That makes capture ordering relative to the
--                `canvases` update irrelevant — a superseded or half-written
--                snapshot can never be served under a newer presentation.
--
--                `object_key` is stored rather than derived from
--                `source_version`. It is derivable today
--                (canvas/<thread_id>/<canvas_id>/<sha>), but storing it lets a
--                future prefix or scheme change land without a backfill.
--
--                The composite FK cascades the ROW only. Objects are not
--                cascaded by the database: every row delete must be paired with
--                a SnapshotService.delete_blob call. Deletion order is
--                row-first, object-second, so an interrupted delete degrades to
--                `unavailable` (safe) rather than leaking a live pointer.
-- depends-on:    0072_jobs_failed_at.sql
-- expected:      < 1s (new empty table)
-- locks:         Brief SHARE ROW EXCLUSIVE on canvases for the foreign key
-- transactional: yes
-- ============================================================================

CREATE TABLE canvas_snapshots (
    thread_id       UUID        NOT NULL,
    canvas_id       VARCHAR(64) NOT NULL DEFAULT 'main',
    path            TEXT        NOT NULL,
    renderer        VARCHAR(32) NOT NULL,
    media_type      TEXT        NOT NULL,
    source_version  TEXT        NOT NULL,
    object_key      TEXT        NOT NULL,
    byte_size       BIGINT      NOT NULL,
    last_modified   TIMESTAMPTZ,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_canvas_snapshots PRIMARY KEY (thread_id, canvas_id),
    CONSTRAINT fk_canvas_snapshots_canvas
        FOREIGN KEY (thread_id, canvas_id)
        REFERENCES canvases (thread_id, canvas_id) ON DELETE CASCADE,
    CONSTRAINT ck_canvas_snapshots_source_version
        CHECK (source_version ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT ck_canvas_snapshots_object_key
        CHECK (char_length(object_key) BETWEEN 1 AND 1024),
    CONSTRAINT ck_canvas_snapshots_path_length
        CHECK (char_length(path) BETWEEN 1 AND 4096),
    CONSTRAINT ck_canvas_snapshots_size
        CHECK (byte_size > 0)
);

COMMENT ON TABLE canvas_snapshots IS
    'Last published bytes of a file Canvas, held in the object store. One row '
    'per Canvas, replaced on each publish. Read-only: never a write target and '
    'never merged back into the workspace.';
COMMENT ON COLUMN canvas_snapshots.source_version IS
    'sha256 of the captured bytes. Served only while it equals the live '
    'canvases.source_version; any disagreement means stale and is ignored.';
COMMENT ON COLUMN canvas_snapshots.object_key IS
    'Object-store key, canvas/<thread_id>/<canvas_id>/<sha>. Thread-scoped '
    'rather than content-addressed so deletion is unambiguous and per-tenant.';
COMMENT ON COLUMN canvas_snapshots.renderer IS
    'Renderer the bytes were validated for. Deliberately unconstrained, '
    'matching canvases.renderer: renderer vocabulary stays app-enforced.';
