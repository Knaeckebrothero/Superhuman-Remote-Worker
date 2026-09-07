"""Durable last-published bytes for a file Canvas.

A Canvas is a presentation pointer: the bytes live in the workspace and are
re-materialized on every read. That makes the presentation exactly as durable
as the workspace pod, which sessions delete on idle. This module keeps one
copy of the last published bytes so the stage survives its workspace.

Design and the signed-off architectural departure this represents:
``knowledge-base/knowledge/features/canvas_durable_presentation.md``.

Two rules govern everything here:

* **Bytes go to the object store, never to Postgres.** The row carries an
  object key and the identity needed to decide whether that object may still
  be served.
* **A snapshot is usable only while its ``source_version`` matches the live
  ``canvases`` row.** Anything else is stale and is ignored rather than
  repaired, which is what makes capture ordering irrelevant.

Capture is best-effort by design: a snapshot failure must never turn a working
publish into a failed one. Failures log at ``error`` and are meant to be
alerted on.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from orchestrator.services.canvas import MAIN_CANVAS_ID
from orchestrator.services.canvas_files import MAX_FILE_BYTES, ValidatedCanvasFile

logger = logging.getLogger(__name__)

# Renderers the cockpit draws from raw bytes. ``office`` is excluded: those
# bytes reach Collabora through the WOPI read path with write semantics
# attached, which is a separate slice. Live apps and shared browsers have no
# published bytes at all.
SNAPSHOT_RENDERERS = frozenset(
    {"markdown", "text", "html", "html-interactive", "image"}
)

# Redundant guard, not the real ceiling. The renderer validators already bound
# every candidate well below this (2 MiB text/HTML, 25 MiB image, 50 MiB
# absolute), so the rule is simply "if the Canvas can present it, the Canvas
# can remember it". Set to 0 to disable capture and fallback entirely.
CANVAS_SNAPSHOT_MAX_BYTES = int(
    os.getenv("CANVAS_SNAPSHOT_MAX_BYTES", str(MAX_FILE_BYTES))
)

_KEY_PREFIX = "canvas"


@dataclass(frozen=True, slots=True)
class CanvasSnapshot:
    """One stored presentation copy, without its bytes."""

    thread_id: str
    canvas_id: str
    path: str
    renderer: str
    media_type: str
    source_version: str
    object_key: str
    byte_size: int
    last_modified: datetime | None
    captured_at: datetime

    @property
    def filename(self) -> str:
        return PurePosixPath(self.path).name


def snapshots_enabled() -> bool:
    return CANVAS_SNAPSHOT_MAX_BYTES > 0


def snapshot_eligible(file: ValidatedCanvasFile) -> bool:
    """True when these exact bytes may be remembered."""

    if not snapshots_enabled():
        return False
    if file.renderer not in SNAPSHOT_RENDERERS:
        return False
    return 0 < len(file.data) <= CANVAS_SNAPSHOT_MAX_BYTES


def _object_key(thread_id: str, canvas_id: str, source_version: str) -> str:
    """Thread-scoped, deterministic key.

    Deliberately not the content-addressed ``save_blob`` scheme: sharing one
    object between threads that happen to hold identical bytes would make
    clearing one Canvas delete bytes another still points at, and would turn
    object existence into a weak cross-tenant oracle.
    """

    digest = source_version.split(":", 1)[-1]
    return f"{_KEY_PREFIX}/{thread_id}/{canvas_id}/{digest}"


def _row_to_snapshot(row: Any) -> CanvasSnapshot:
    return CanvasSnapshot(
        thread_id=str(row["thread_id"]),
        canvas_id=str(row["canvas_id"]),
        path=str(row["path"]),
        renderer=str(row["renderer"]),
        media_type=str(row["media_type"]),
        source_version=str(row["source_version"]),
        object_key=str(row["object_key"]),
        byte_size=int(row["byte_size"]),
        last_modified=row["last_modified"],
        captured_at=row["captured_at"],
    )


class CanvasSnapshotStore:
    """Row metadata in Postgres, bytes in the object store."""

    def __init__(self, db: Any, *, blobs: Any | None = None) -> None:
        self._db = db
        self._blobs = blobs

    @property
    def blobs(self) -> Any:
        if self._blobs is not None:
            return self._blobs
        # Late import keeps this module usable in tests that never touch S3.
        from orchestrator.services.snapshot_service import snapshot_service

        return snapshot_service

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def lookup(
        self, thread_id: str, *, canvas_id: str = MAIN_CANVAS_ID
    ) -> CanvasSnapshot | None:
        """Row-only lookup for the state path; never fetches bytes."""

        if not snapshots_enabled():
            return None
        try:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT thread_id, canvas_id, path, renderer, media_type,
                           source_version, object_key, byte_size, last_modified,
                           captured_at
                    FROM canvas_snapshots
                    WHERE thread_id = $1 AND canvas_id = $2
                    """,
                    thread_id,
                    canvas_id,
                )
            # Conversion stays inside the guard on purpose: a snapshot is an
            # optimization over the pre-existing `unavailable` behavior, so any
            # failure to read one degrades to that rather than failing a Canvas
            # the workspace could still serve.
            return _row_to_snapshot(row) if row is not None else None
        except Exception:
            logger.exception(
                "Canvas snapshot lookup failed for thread=%s canvas=%s",
                thread_id,
                canvas_id,
            )
            return None

    async def usable(
        self,
        thread_id: str,
        expected_source_version: str | None,
        *,
        canvas_id: str = MAIN_CANVAS_ID,
    ) -> CanvasSnapshot | None:
        """The snapshot for exactly this published version, or nothing."""

        if not expected_source_version:
            return None
        snapshot = await self.lookup(thread_id, canvas_id=canvas_id)
        if snapshot is None or snapshot.source_version != expected_source_version:
            return None
        return snapshot

    async def load(
        self,
        thread_id: str,
        expected_source_version: str | None,
        *,
        canvas_id: str = MAIN_CANVAS_ID,
    ) -> tuple[CanvasSnapshot, bytes] | None:
        """Fetch and verify the stored bytes for one published version."""

        snapshot = await self.usable(
            thread_id, expected_source_version, canvas_id=canvas_id
        )
        if snapshot is None:
            return None
        try:
            data = await self.blobs.get_blob(snapshot.object_key)
        except Exception:
            logger.exception(
                "Canvas snapshot fetch failed for thread=%s key=%s",
                thread_id,
                snapshot.object_key,
            )
            return None
        if not data:
            # Missing object, or the store is unavailable. Callers degrade to
            # the pre-existing `unavailable` behavior.
            return None
        # The live path hashes on every read too, so this costs what it
        # replaces. Serving bytes under a `source_version` they do not hash to
        # would put a wrong strong ETag into browser and proxy caches.
        actual = "sha256:" + hashlib.sha256(data).hexdigest()
        if actual != snapshot.source_version:
            logger.error(
                "Canvas snapshot integrity mismatch thread=%s key=%s "
                "expected=%s actual=%s — dropping the row",
                thread_id,
                snapshot.object_key,
                snapshot.source_version,
                actual,
            )
            await self.delete(thread_id, canvas_id=canvas_id)
            return None
        return snapshot, data

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def capture(
        self,
        thread_id: str,
        file: ValidatedCanvasFile,
        *,
        canvas_id: str = MAIN_CANVAS_ID,
    ) -> bool:
        """Remember these exact published bytes. Never raises."""

        if not snapshot_eligible(file):
            return False
        key = _object_key(thread_id, canvas_id, file.source_version)
        try:
            stored = await self.blobs.put_blob(
                key, file.data, content_type=file.media_type
            )
            if not stored:
                logger.error(
                    "Canvas snapshot upload failed thread=%s key=%s (%d bytes) — "
                    "this presentation will not survive its workspace",
                    thread_id,
                    key,
                    len(file.data),
                )
                return False

            async with self._db.acquire() as conn:
                async with conn.transaction():
                    # Read the superseded key under a row lock rather than
                    # trying to recover it from RETURNING, which reports the
                    # post-update row.
                    previous = await conn.fetchval(
                        """
                        SELECT object_key FROM canvas_snapshots
                        WHERE thread_id = $1 AND canvas_id = $2
                        FOR UPDATE
                        """,
                        thread_id,
                        canvas_id,
                    )
                    await conn.execute(
                        """
                        INSERT INTO canvas_snapshots (
                            thread_id, canvas_id, path, renderer, media_type,
                            source_version, object_key, byte_size,
                            last_modified, captured_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                        ON CONFLICT (thread_id, canvas_id) DO UPDATE
                        SET path           = EXCLUDED.path,
                            renderer       = EXCLUDED.renderer,
                            media_type     = EXCLUDED.media_type,
                            source_version = EXCLUDED.source_version,
                            object_key     = EXCLUDED.object_key,
                            byte_size      = EXCLUDED.byte_size,
                            last_modified  = EXCLUDED.last_modified,
                            captured_at    = now()
                        """,
                        thread_id,
                        canvas_id,
                        file.path,
                        file.renderer,
                        file.media_type,
                        file.source_version,
                        key,
                        len(file.data),
                        file.last_modified,
                    )
        except Exception:
            logger.exception(
                "Canvas snapshot capture failed for thread=%s path=%s — the "
                "publish itself succeeded; this presentation will not survive "
                "its workspace",
                thread_id,
                file.path,
            )
            return False

        # Drop the object the previous version pointed at. Best-effort: a
        # failure leaks one bounded object, which is strictly better than
        # deleting bytes a live row still names.
        if previous and previous != key:
            await self.discard_object(str(previous))
        return True

    @staticmethod
    async def delete_row(
        conn: Any, thread_id: str, *, canvas_id: str = MAIN_CANVAS_ID
    ) -> str | None:
        """Drop the row inside a caller's transaction; return the freed key.

        Clearing a Canvas nulls the source on the ``canvases`` row rather than
        deleting it, so the ``ON DELETE CASCADE`` never fires. The row delete
        therefore has to ride the same locked transaction as the clear, and the
        object delete follows once that has committed.
        """

        key = await conn.fetchval(
            """
            DELETE FROM canvas_snapshots
            WHERE thread_id = $1 AND canvas_id = $2
            RETURNING object_key
            """,
            thread_id,
            canvas_id,
        )
        return str(key) if key else None

    async def delete(self, thread_id: str, *, canvas_id: str = MAIN_CANVAS_ID) -> None:
        """Forget a Canvas copy. Row first, then object. Never raises."""

        try:
            async with self._db.acquire() as conn:
                key = await self.delete_row(conn, thread_id, canvas_id=canvas_id)
        except Exception:
            logger.exception(
                "Canvas snapshot row delete failed for thread=%s canvas=%s",
                thread_id,
                canvas_id,
            )
            return
        if key:
            await self.discard_object(key)

    async def discard_object(self, key: str) -> None:
        """Best-effort object delete. A failure leaks one bounded object.

        Deliberately ordered after the row delete: an orphaned object wastes
        space, whereas an object deleted out from under a live row would serve
        a broken Canvas.
        """

        try:
            deleted = await self.blobs.delete_blob(key)
        except Exception:
            logger.exception("Canvas snapshot object delete failed key=%s", key)
            return
        if not deleted:
            logger.error(
                "Canvas snapshot object delete returned false key=%s — orphaned "
                "object left in the bucket",
                key,
            )


__all__ = [
    "CANVAS_SNAPSHOT_MAX_BYTES",
    "SNAPSHOT_RENDERERS",
    "CanvasSnapshot",
    "CanvasSnapshotStore",
    "snapshot_eligible",
    "snapshots_enabled",
]
