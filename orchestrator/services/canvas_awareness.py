"""Durable, lane-independent Canvas editor awareness.

Canvas awareness is courtesy UX state, not authorization or execution
ownership. Browser editors write monotonic per-editor leases through the
owner-gated orchestrator API; SSE consumers read complete live snapshots from
Postgres. Nothing in this module writes the session event journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Literal


CANVAS_AWARENESS_TTL_SECONDS = 15.0
CANVAS_AWARENESS_RENEW_SECONDS = 5.0
CANVAS_AWARENESS_TOMBSTONE_RETENTION_SECONDS = 300.0
CANVAS_AWARENESS_CLEANUP_LIMIT = 500
CANVAS_AWARENESS_MAX_ROWS_PER_THREAD = 256
CANVAS_AWARENESS_MAX_SEQUENCE = 9_007_199_254_740_991

CanvasAwarenessState = Literal["editing", "idle"]


class CanvasAwarenessConflict(RuntimeError):
    """A sequence or Canvas identity cannot be applied safely."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CanvasAwarenessMutation:
    applied: bool
    sender_id: str
    sequence: int
    state: CanvasAwarenessState
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CanvasAwarenessEditor:
    sender_id: str
    editing_session_id: str
    path: str
    presentation_revision: int
    source_version: str
    sequence: int
    ttl_ms: int

    def public_dict(self) -> dict[str, str | int]:
        return {
            "sender_id": self.sender_id,
            "editing_session_id": self.editing_session_id,
            "path": self.path,
            "presentation_revision": self.presentation_revision,
            "source_version": self.source_version,
            "sequence": self.sequence,
            "ttl_ms": self.ttl_ms,
        }

    def signature(self) -> tuple[str, str, str, int, str, int]:
        """Stable change identity; deliberately excludes a ticking TTL."""

        return (
            self.sender_id,
            self.editing_session_id,
            self.path,
            self.presentation_revision,
            self.source_version,
            self.sequence,
        )


def _same_payload(
    row: Any,
    *,
    state: CanvasAwarenessState,
    path: str,
    presentation_revision: int,
    source_version: str,
) -> bool:
    return (
        str(row["state"]) == state
        and str(row["path"]) == path
        and int(row["presentation_revision"]) == presentation_revision
        and str(row["source_version"]) == source_version
    )


def _same_identity(
    row: Any,
    *,
    path: str,
    presentation_revision: int,
    source_version: str,
) -> bool:
    return (
        str(row["path"]) == path
        and int(row["presentation_revision"]) == presentation_revision
        and str(row["source_version"]) == source_version
    )


def _mutation(row: Any, *, applied: bool) -> CanvasAwarenessMutation:
    return CanvasAwarenessMutation(
        applied=applied,
        sender_id=str(row["sender_id"]),
        sequence=int(row["client_seq"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        expires_at=row["expires_at"],
    )


async def mutate_canvas_awareness(
    pool: Any,
    *,
    thread_id: str,
    editing_session_id: str,
    sequence: int,
    state: CanvasAwarenessState,
    path: str,
    presentation_revision: int,
    source_version: str,
    ttl_seconds: float = CANVAS_AWARENESS_TTL_SECONDS,
    tombstone_retention_seconds: float = (CANVAS_AWARENESS_TOMBSTONE_RETENTION_SECONDS),
    max_rows_per_thread: int = CANVAS_AWARENESS_MAX_ROWS_PER_THREAD,
) -> CanvasAwarenessMutation:
    """Apply one monotonic editor lease/tombstone mutation.

    A transaction-scoped advisory lock covers the first-write/no-row race.
    ``canvases FOR SHARE`` makes an editing lease's exact identity check and
    the awareness write one atomic observation relative to Canvas mutation.
    Idle may retire a stored identity after the Canvas itself changed.
    """

    ttl = float(ttl_seconds)
    if ttl <= 0:
        raise ValueError("Canvas awareness TTL must be positive")
    seq = int(sequence)
    if seq <= 0 or seq > CANVAS_AWARENESS_MAX_SEQUENCE:
        raise ValueError("Canvas awareness sequence must be a positive JS-safe integer")

    retention = max(
        CANVAS_AWARENESS_TTL_SECONDS * 2.0,
        float(tombstone_retention_seconds),
    )
    row_cap = max(1, int(max_rows_per_thread))
    # A thread-scoped lock, rather than an editor-scoped lock, makes the cap
    # exact across concurrent first writes from many tabs. Awareness renewals
    # are tiny and infrequent (5s), so that serialization is acceptable.
    lock_key = f"canvas-awareness:{thread_id}:main"
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
                lock_key,
            )
            canvas = await conn.fetchrow(
                "SELECT source, presentation_revision, source_version "
                "FROM canvases "
                "WHERE thread_id = $1::uuid AND canvas_id = 'main' "
                "FOR SHARE",
                thread_id,
            )
            if canvas is None:
                raise CanvasAwarenessConflict(
                    "canvas_awareness_stale",
                    "The Canvas no longer exists",
                )

            # Retain tombstones far longer than the ordinary request/TTL
            # window, then prune before enforcing the hard per-thread cap.
            # Awareness is courtesy state: after this bound, an extraordinarily
            # delayed request can at worst reappear for one 15s TTL.
            await conn.execute(
                "DELETE FROM canvas_editor_awareness "
                "WHERE thread_id = $1::uuid AND canvas_id = 'main' "
                "  AND expires_at < clock_timestamp() "
                "      - make_interval(secs => $2::double precision)",
                thread_id,
                retention,
            )

            current = await conn.fetchrow(
                "SELECT sender_id, state, client_seq, path, "
                "       presentation_revision, source_version, expires_at "
                "FROM canvas_editor_awareness "
                "WHERE thread_id = $1::uuid AND canvas_id = 'main' "
                "  AND editing_session_id = $2 "
                "FOR UPDATE",
                thread_id,
                editing_session_id,
            )

            if current is not None:
                current_seq = int(current["client_seq"])
                if seq < current_seq:
                    return _mutation(current, applied=False)
                if seq == current_seq and not _same_payload(
                    current,
                    state=state,
                    path=path,
                    presentation_revision=presentation_revision,
                    source_version=source_version,
                ):
                    raise CanvasAwarenessConflict(
                        "canvas_awareness_sequence_reused",
                        "This Canvas awareness sequence was already used for "
                        "different state",
                    )
                if seq == current_seq:
                    # A response retry is observationally idempotent. In
                    # particular, it must not extend an editing TTL: only a
                    # new client sequence proves a new heartbeat happened.
                    return _mutation(current, applied=False)
                if state == "idle" and not _same_identity(
                    current,
                    path=path,
                    presentation_revision=presentation_revision,
                    source_version=source_version,
                ):
                    raise CanvasAwarenessConflict(
                        "canvas_awareness_identity_mismatch",
                        "The idle tombstone does not match this editor lease",
                    )

            if current is None:
                row_count = int(
                    await conn.fetchval(
                        "SELECT COUNT(*) FROM canvas_editor_awareness "
                        "WHERE thread_id = $1::uuid AND canvas_id = 'main'",
                        thread_id,
                    )
                    or 0
                )
                if row_count >= row_cap:
                    raise CanvasAwarenessConflict(
                        "canvas_awareness_capacity_exhausted",
                        "Canvas editor awareness capacity is exhausted",
                    )

            if state == "editing":
                raw_source = canvas["source"]
                if isinstance(raw_source, str):
                    import json

                    raw_source = json.loads(raw_source)
                exact_canvas = (
                    isinstance(raw_source, dict)
                    and raw_source.get("type") == "workspace_file"
                    and raw_source.get("path") == path
                    and int(canvas["presentation_revision"] or 0)
                    == presentation_revision
                    and str(canvas["source_version"] or "") == source_version
                )
                if not exact_canvas:
                    raise CanvasAwarenessConflict(
                        "canvas_awareness_stale",
                        "Canvas state changed; reload before editing",
                    )

            row = await conn.fetchrow(
                "WITH timestamp AS (SELECT clock_timestamp() AS at) "
                "INSERT INTO canvas_editor_awareness ("
                "    thread_id, canvas_id, editing_session_id, state, client_seq, "
                "    path, presentation_revision, source_version, "
                "    refreshed_at, expires_at"
                ") "
                "SELECT $1::uuid, 'main', $2::varchar(128), $3::varchar(16), "
                "       $4::bigint, $5::text, $6::bigint, $7::varchar(71), "
                "       timestamp.at, "
                "       CASE WHEN $3::varchar(16) = 'editing' "
                "            THEN timestamp.at + make_interval(secs => $8::double precision) "
                "            ELSE timestamp.at END "
                "FROM timestamp "
                "ON CONFLICT (thread_id, canvas_id, editing_session_id) "
                "DO UPDATE SET "
                "    state = EXCLUDED.state, client_seq = EXCLUDED.client_seq, "
                "    path = EXCLUDED.path, "
                "    presentation_revision = EXCLUDED.presentation_revision, "
                "    source_version = EXCLUDED.source_version, "
                "    refreshed_at = EXCLUDED.refreshed_at, "
                "    expires_at = EXCLUDED.expires_at "
                "RETURNING sender_id, state, client_seq, expires_at",
                thread_id,
                editing_session_id,
                state,
                seq,
                path,
                presentation_revision,
                source_version,
                ttl,
            )
            assert row is not None
            return _mutation(row, applied=True)


async def fetch_canvas_awareness_snapshot(
    pool: Any,
    *,
    thread_id: str,
) -> tuple[CanvasAwarenessEditor, ...]:
    """Return the exact current live-editor set in stable order."""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT awareness.sender_id, awareness.editing_session_id, "
            "       awareness.path, awareness.presentation_revision, "
            "       awareness.source_version, awareness.client_seq, "
            "       CEIL(EXTRACT(EPOCH FROM ("
            "           awareness.expires_at - clock_timestamp())) * 1000) "
            "           AS ttl_ms "
            "FROM canvas_editor_awareness AS awareness "
            "JOIN canvases AS canvas "
            "  ON canvas.thread_id = awareness.thread_id "
            " AND canvas.canvas_id = awareness.canvas_id "
            "WHERE awareness.thread_id = $1::uuid "
            "  AND awareness.canvas_id = 'main' "
            "  AND awareness.state = 'editing' "
            "  AND awareness.expires_at > clock_timestamp() "
            "  AND canvas.source->>'type' = 'workspace_file' "
            "  AND canvas.source->>'path' = awareness.path "
            "  AND canvas.presentation_revision = awareness.presentation_revision "
            "  AND canvas.source_version = awareness.source_version "
            "ORDER BY awareness.sender_id",
            thread_id,
        )
    editors: list[CanvasAwarenessEditor] = []
    for row in rows:
        # The existing Cockpit awareness contract accepts 1s..60s. A lease
        # observed in its final sub-second window may therefore display for at
        # most one extra second; the next complete snapshot removes it.
        ttl_ms = max(1_000, min(60_000, int(math.ceil(float(row["ttl_ms"])))))
        editors.append(
            CanvasAwarenessEditor(
                sender_id=str(row["sender_id"]),
                editing_session_id=str(row["editing_session_id"]),
                path=str(row["path"]),
                presentation_revision=int(row["presentation_revision"]),
                source_version=str(row["source_version"]),
                sequence=int(row["client_seq"]),
                ttl_ms=ttl_ms,
            )
        )
    return tuple(editors)


async def cleanup_canvas_awareness(
    pool: Any,
    *,
    retention_seconds: float = CANVAS_AWARENESS_TOMBSTONE_RETENTION_SECONDS,
    limit: int = CANVAS_AWARENESS_CLEANUP_LIMIT,
) -> int:
    """Bound old expired leases/tombstones without blocking active editors."""

    retention = max(CANVAS_AWARENESS_TTL_SECONDS * 2.0, float(retention_seconds))
    capped_limit = max(1, min(10_000, int(limit)))
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            "WITH doomed AS ("
            "    SELECT thread_id, canvas_id, editing_session_id "
            "    FROM canvas_editor_awareness "
            "    WHERE expires_at < clock_timestamp() "
            "        - make_interval(secs => $1::double precision) "
            "    ORDER BY expires_at ASC "
            "    LIMIT $2 "
            "    FOR UPDATE SKIP LOCKED"
            "), removed AS ("
            "    DELETE FROM canvas_editor_awareness AS awareness "
            "    USING doomed "
            "    WHERE awareness.thread_id = doomed.thread_id "
            "      AND awareness.canvas_id = doomed.canvas_id "
            "      AND awareness.editing_session_id = doomed.editing_session_id "
            "    RETURNING 1"
            ") SELECT COUNT(*) FROM removed",
            retention,
            capped_limit,
        )
    return int(deleted or 0)
