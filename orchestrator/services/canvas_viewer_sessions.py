"""Durable authentication for isolated Dynamic Canvas viewer origins.

Parent APIs use the normal BFF session and create a non-credential attachment
plus a single-use bootstrap.  The dedicated viewer gateway consumes that
bootstrap and authenticates later app requests with a separate, host-only
cookie.  Plaintext bootstrap/cookie material never enters PostgreSQL.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from services.canvas import CanvasRecord, WorkspaceAppSource
from services.canvas_viewer_config import (
    CanvasViewerConfig,
    CanvasViewerConfigurationError,
    canvas_viewer_config,
)
from services.canvas_ssh import (
    CanvasSSHError,
    RemoteWorkspaceTarget,
    resolve_remote_workspace_target,
)

CANVAS_VIEWER_COOKIE = "__Host-canvas_session"
_SECRET_BYTES = 32


class CanvasViewerError(Exception):
    """Typed attachment/session failure shared by parent and gateway APIs."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _secret() -> str:
    return secrets.token_urlsafe(_SECRET_BYTES)


def hash_canvas_viewer_secret(purpose: str, value: str) -> str:
    """Domain-separate stored bootstrap, cookie, and bridge secret hashes."""

    if purpose not in {"bootstrap", "session", "bridge"}:
        raise ValueError("unknown Canvas viewer secret purpose")
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("invalid Canvas viewer secret")
    material = b"srw-canvas-viewer-v1\x00" + purpose.encode("ascii") + b"\x00"
    return hashlib.sha256(material + value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CanvasViewerAttachmentGrant:
    attachment_id: UUID
    origin: str
    bootstrap_url: str
    expires_at: datetime
    renew_after: datetime

    def public_dict(self) -> dict[str, str]:
        return {
            "attachment_id": str(self.attachment_id),
            "origin": self.origin,
            "bootstrap_url": self.bootstrap_url,
            "expires_at": _iso(self.expires_at),
            "renew_after": _iso(self.renew_after),
        }


@dataclass(frozen=True, slots=True)
class CanvasViewerRenewal:
    expires_at: datetime
    renew_after: datetime

    def public_dict(self) -> dict[str, str]:
        return {
            "expires_at": _iso(self.expires_at),
            "renew_after": _iso(self.renew_after),
        }


@dataclass(frozen=True, slots=True)
class CanvasOriginSession:
    id: UUID
    user_id: UUID
    thread_id: UUID
    canvas_id: str
    parent_srw_session_id: UUID
    source_fingerprint: str
    workspace_generation: UUID
    origin_generation: UUID
    embedding_origin: str
    cookie_mode: str
    expires_at: datetime
    record: CanvasRecord
    thread: dict[str, Any]
    remote_target: RemoteWorkspaceTarget | None = None


@dataclass(frozen=True, slots=True)
class CanvasBootstrapExchange:
    session: CanvasOriginSession
    entry_path: str
    session_secret: str | None
    # The browser may retain the opaque cookie only as long as its immutable
    # parent BFF session. PostgreSQL still enforces the shorter renewable
    # Canvas-session expiry on every request, so retention is not authorization.
    cookie_expires_at: datetime | None = None


def _renew_after(now: datetime, expires_at: datetime) -> datetime:
    remaining = max(1.0, (expires_at - now).total_seconds())
    return now + timedelta(seconds=max(1.0, remaining * 2 / 3))


def _require_app_record(record: CanvasRecord | None) -> WorkspaceAppSource:
    if (
        record is None
        or not isinstance(record.source, WorkspaceAppSource)
        or record.origin_generation is None
        or record.source_fingerprint is None
    ):
        raise CanvasViewerError(
            409,
            "canvas_not_live_app",
            "The current Canvas is not a live workspace application",
        )
    return record.source


def _same_canvas_identity(left: CanvasRecord, right: CanvasRecord) -> bool:
    return (
        left.thread_id == right.thread_id
        and left.canvas_id == right.canvas_id
        and left.presentation_revision == right.presentation_revision
        and left.source_fingerprint == right.source_fingerprint
        and left.origin_generation == right.origin_generation
        and isinstance(left.source, WorkspaceAppSource)
        and isinstance(right.source, WorkspaceAppSource)
        and left.source.workspace_generation == right.source.workspace_generation
    )


def _viewer_policy_changed(values: dict[str, Any], config: CanvasViewerConfig) -> bool:
    """Invalidate credentials minted under a superseded viewer policy."""

    return (
        str(values.get("cookie_mode") or "") != config.cookie_mode
        or str(values.get("embedding_origin") or "") not in config.cockpit_origins
    )


_CANVAS_COLUMNS = """
    thread_id, canvas_id, source, title, renderer, editable, alt_text,
    presentation_revision, source_fingerprint, source_version,
    origin_generation, created_at, updated_at
"""


class CanvasViewerSessionService:
    """Cluster-shared viewer attachment, bootstrap, and session actions."""

    def __init__(self, db: Any, *, config: CanvasViewerConfig | None = None):
        self._db = db
        try:
            self._config = config or canvas_viewer_config()
        except CanvasViewerConfigurationError as exc:
            raise CanvasViewerError(
                503,
                "canvas_viewer_configuration_invalid",
                "Canvas viewer configuration is invalid",
            ) from exc

    @property
    def config(self) -> CanvasViewerConfig:
        return self._config

    def _require_enabled(self) -> None:
        if not self._config.enabled:
            raise CanvasViewerError(
                503, "canvas_viewer_disabled", "Canvas live viewer is disabled"
            )

    async def _canvas_record(
        self, conn: Any, thread_id: str, *, for_share: bool = False
    ) -> CanvasRecord | None:
        suffix = " FOR SHARE" if for_share else ""
        row = await conn.fetchrow(
            f"""
            SELECT {_CANVAS_COLUMNS}
            FROM canvases
            WHERE thread_id = $1 AND canvas_id = 'main'{suffix}
            """,
            thread_id,
        )
        return CanvasRecord.from_row(row) if row is not None else None

    @staticmethod
    async def _parent_session(
        conn: Any, parent_session_id: UUID, user_id: str
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, absolute_expires_at, revoked_at
            FROM srw_sessions
            WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
              AND absolute_expires_at > now()
            FOR SHARE
            """,
            parent_session_id,
            user_id,
        )
        if row is None:
            raise CanvasViewerError(
                401,
                "canvas_parent_session_invalid",
                "A current Cockpit session is required",
            )
        return dict(row)

    @staticmethod
    async def _authorized_thread(
        conn: Any, *, user_id: str, thread_id: str
    ) -> dict[str, Any]:
        user = await conn.fetchrow(
            "SELECT id, is_admin, is_approved FROM users WHERE id = $1", user_id
        )
        thread = await conn.fetchrow(
            "SELECT id, user_id, metadata FROM threads WHERE id = $1 FOR SHARE",
            thread_id,
        )
        if (
            user is None
            or not bool(user.get("is_approved"))
            or thread is None
            or not (
                bool(user.get("is_admin"))
                or str(thread.get("user_id") or "") == str(user_id)
            )
        ):
            raise CanvasViewerError(
                403,
                "canvas_not_authorized",
                "Canvas thread authorization changed",
            )
        return dict(thread)

    async def create_attachment(
        self,
        *,
        user_id: str,
        thread_id: str,
        parent_session_id: UUID,
        embedding_origin: str | None,
        expected_record: CanvasRecord,
    ) -> CanvasViewerAttachmentGrant:
        """Mint one pending frame attachment bound to exact current state."""

        self._require_enabled()
        try:
            embedding = self._config.require_cockpit_origin(embedding_origin)
        except CanvasViewerConfigurationError as exc:
            raise CanvasViewerError(
                403,
                "canvas_embedding_origin_forbidden",
                "The Canvas embedding origin is not allowed",
            ) from exc
        source = _require_app_record(expected_record)
        now = _now()
        attachment_id = uuid4()
        bootstrap_id = uuid4()
        bootstrap = _secret()
        bridge = _secret()

        async with self._db.acquire() as conn:
            async with conn.transaction():
                parent = await self._parent_session(conn, parent_session_id, user_id)
                await self._authorized_thread(
                    conn, user_id=user_id, thread_id=thread_id
                )
                current = await self._canvas_record(conn, thread_id, for_share=True)
                if current is None or not _same_canvas_identity(
                    current, expected_record
                ):
                    raise CanvasViewerError(
                        412,
                        "canvas_viewer_precondition_failed",
                        "Canvas state changed before the viewer was attached",
                    )
                _require_app_record(current)
                parent_expiry = _utc(parent["absolute_expires_at"])
                attachment_expiry = min(
                    now + timedelta(seconds=self._config.attachment_ttl_seconds),
                    parent_expiry,
                )
                bootstrap_expiry = min(
                    now + timedelta(seconds=self._config.bootstrap_ttl_seconds),
                    attachment_expiry,
                )
                if bootstrap_expiry <= now:
                    raise CanvasViewerError(
                        401,
                        "canvas_parent_session_invalid",
                        "The Cockpit session expires too soon to attach a viewer",
                    )
                await conn.execute(
                    """
                    INSERT INTO canvas_view_attachments (
                        id, user_id, thread_id, canvas_id,
                        parent_srw_session_id, bridge_nonce_hash,
                        embedding_origin, cookie_mode, expires_at
                    ) VALUES ($1, $2, $3, 'main', $4, $5, $6, $7, $8)
                    """,
                    attachment_id,
                    user_id,
                    thread_id,
                    parent_session_id,
                    hash_canvas_viewer_secret("bridge", bridge),
                    embedding,
                    self._config.cookie_mode,
                    attachment_expiry,
                )
                await conn.execute(
                    """
                    INSERT INTO canvas_view_bootstraps (
                        id, token_hash, attachment_id,
                        expected_presentation_revision, source_fingerprint,
                        workspace_generation, origin_generation, expires_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    bootstrap_id,
                    hash_canvas_viewer_secret("bootstrap", bootstrap),
                    attachment_id,
                    current.presentation_revision,
                    current.source_fingerprint,
                    source.workspace_generation,
                    current.origin_generation,
                    bootstrap_expiry,
                )

        assert expected_record.origin_generation is not None
        origin = self._config.public_origin(expected_record.origin_generation)
        bootstrap_url = f"{origin}/_canvas/bootstrap?token={quote(bootstrap, safe='')}"
        session_expiry = min(
            attachment_expiry,
            now + timedelta(seconds=self._config.session_ttl_seconds),
        )
        return CanvasViewerAttachmentGrant(
            attachment_id=attachment_id,
            origin=origin,
            bootstrap_url=bootstrap_url,
            expires_at=session_expiry,
            renew_after=_renew_after(now, session_expiry),
        )

    async def consume_bootstrap(
        self,
        *,
        token: str,
        host_generation: UUID,
        existing_session_secret: str | None,
    ) -> CanvasBootstrapExchange:
        """Atomically consume one bootstrap after gateway metadata checks."""

        self._require_enabled()
        try:
            token_hash = hash_canvas_viewer_secret("bootstrap", token)
        except ValueError as exc:
            raise CanvasViewerError(
                401, "canvas_bootstrap_invalid", "Canvas bootstrap is invalid"
            ) from exc
        now = _now()
        new_secret: str | None = None

        async with self._db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT b.id AS bootstrap_id, b.attachment_id,
                           b.expected_presentation_revision,
                           b.source_fingerprint, b.workspace_generation,
                           b.origin_generation, b.expires_at AS bootstrap_expires_at,
                           a.user_id, a.thread_id, a.canvas_id,
                           a.parent_srw_session_id, a.embedding_origin,
                           a.cookie_mode AS attachment_cookie_mode,
                           a.expires_at AS attachment_expires_at, a.closed_at
                    FROM canvas_view_bootstraps b
                    JOIN canvas_view_attachments a ON a.id = b.attachment_id
                    WHERE b.token_hash = $1 AND b.consumed_at IS NULL
                      AND b.expires_at > now()
                    FOR UPDATE OF b, a
                    """,
                    token_hash,
                )
                if row is None:
                    raise CanvasViewerError(
                        401,
                        "canvas_bootstrap_invalid",
                        "Canvas bootstrap is invalid or expired",
                    )
                values = dict(row)
                if (
                    values.get("closed_at") is not None
                    or _utc(values["attachment_expires_at"]) <= now
                    or UUID(str(values["origin_generation"])) != host_generation
                    or str(values.get("embedding_origin") or "")
                    not in self._config.cockpit_origins
                    or str(values.get("attachment_cookie_mode") or "")
                    != self._config.cookie_mode
                ):
                    raise CanvasViewerError(
                        401,
                        "canvas_bootstrap_invalid",
                        "Canvas bootstrap is no longer valid",
                    )
                parent_id = values.get("parent_srw_session_id")
                if parent_id is None:
                    raise CanvasViewerError(
                        401,
                        "canvas_parent_session_invalid",
                        "The Cockpit session has ended",
                    )
                parent = await self._parent_session(
                    conn, UUID(str(parent_id)), str(values["user_id"])
                )
                await self._authorized_thread(
                    conn,
                    user_id=str(values["user_id"]),
                    thread_id=str(values["thread_id"]),
                )
                current = await self._canvas_record(
                    conn, str(values["thread_id"]), for_share=True
                )
                source = _require_app_record(current)
                if (
                    current.presentation_revision
                    != int(values["expected_presentation_revision"])
                    or current.source_fingerprint != values["source_fingerprint"]
                    or source.workspace_generation
                    != UUID(str(values["workspace_generation"]))
                    or current.origin_generation != host_generation
                ):
                    raise CanvasViewerError(
                        409,
                        "canvas_bootstrap_stale",
                        "Canvas changed before the viewer loaded",
                    )

                session_row = None
                if existing_session_secret:
                    try:
                        existing_hash = hash_canvas_viewer_secret(
                            "session", existing_session_secret
                        )
                    except ValueError:
                        existing_hash = ""
                    if existing_hash:
                        session_row = await conn.fetchrow(
                            """
                            SELECT * FROM canvas_origin_sessions
                            WHERE session_secret_hash = $1
                              AND user_id = $2 AND thread_id = $3
                              AND canvas_id = $4 AND parent_srw_session_id = $5
                              AND source_fingerprint = $6
                              AND workspace_generation = $7
                              AND origin_generation = $8
                              AND embedding_origin = $9 AND cookie_mode = $10
                              AND revoked_at IS NULL AND expires_at > now()
                            FOR UPDATE
                            """,
                            existing_hash,
                            values["user_id"],
                            values["thread_id"],
                            values["canvas_id"],
                            parent_id,
                            values["source_fingerprint"],
                            values["workspace_generation"],
                            values["origin_generation"],
                            values["embedding_origin"],
                            self._config.cookie_mode,
                        )

                if session_row is None:
                    new_secret = _secret()
                    session_id = uuid4()
                    expires_at = min(
                        now + timedelta(seconds=self._config.session_ttl_seconds),
                        _utc(parent["absolute_expires_at"]),
                        _utc(values["attachment_expires_at"]),
                    )
                    await conn.execute(
                        """
                        INSERT INTO canvas_origin_sessions (
                            id, session_secret_hash, user_id, thread_id, canvas_id,
                            parent_srw_session_id, issued_presentation_revision,
                            source_fingerprint, workspace_generation,
                            origin_generation, embedding_origin, cookie_mode,
                            expires_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
                        )
                        """,
                        session_id,
                        hash_canvas_viewer_secret("session", new_secret),
                        values["user_id"],
                        values["thread_id"],
                        values["canvas_id"],
                        parent_id,
                        current.presentation_revision,
                        current.source_fingerprint,
                        source.workspace_generation,
                        current.origin_generation,
                        values["embedding_origin"],
                        self._config.cookie_mode,
                        expires_at,
                    )
                else:
                    # Reissue the already-validated value so the gateway can
                    # reset its browser retention bound to the immutable parent
                    # BFF lifetime. This does not rotate or reveal a stored
                    # secret: the plaintext came from this exact host cookie
                    # and matched the persisted hash above.
                    new_secret = existing_session_secret
                    session_id = UUID(str(session_row["id"]))
                    # A newly authorized attachment must not inherit a shared
                    # session which is only seconds from expiry while Cockpit
                    # schedules renewal against a fresh grant. Bootstrap is
                    # already bound to the same parent/user/source identity, so
                    # extend the short server lease now without rotating the
                    # browser cookie.
                    expires_at = min(
                        now + timedelta(seconds=self._config.session_ttl_seconds),
                        _utc(parent["absolute_expires_at"]),
                        _utc(values["attachment_expires_at"]),
                    )
                    await conn.execute(
                        """
                        UPDATE canvas_origin_sessions
                        SET expires_at = $2, last_renewed_at = now(),
                            updated_at = now()
                        WHERE id = $1 AND revoked_at IS NULL
                        """,
                        session_id,
                        expires_at,
                    )

                consumed = await conn.execute(
                    """
                    UPDATE canvas_view_bootstraps
                    SET consumed_at = now(), consumed_origin_session_id = $2
                    WHERE id = $1 AND consumed_at IS NULL
                    """,
                    values["bootstrap_id"],
                    session_id,
                )
                if consumed != "UPDATE 1":
                    raise CanvasViewerError(
                        401,
                        "canvas_bootstrap_invalid",
                        "Canvas bootstrap was already consumed",
                    )
                await conn.execute(
                    """
                    UPDATE canvas_view_attachments
                    SET origin_session_id = $2, last_seen_at = now()
                    WHERE id = $1 AND closed_at IS NULL
                    """,
                    values["attachment_id"],
                    session_id,
                )

        assert current is not None
        session = CanvasOriginSession(
            id=session_id,
            user_id=UUID(str(values["user_id"])),
            thread_id=UUID(str(values["thread_id"])),
            canvas_id=str(values["canvas_id"]),
            parent_srw_session_id=UUID(str(parent_id)),
            source_fingerprint=str(values["source_fingerprint"]),
            workspace_generation=source.workspace_generation,
            origin_generation=host_generation,
            embedding_origin=str(values["embedding_origin"]),
            cookie_mode=self._config.cookie_mode,
            expires_at=expires_at,
            record=current,
            thread={},
        )
        return CanvasBootstrapExchange(
            session=session,
            entry_path=source.entry_path,
            session_secret=new_secret,
            cookie_expires_at=_utc(parent["absolute_expires_at"]),
        )

    async def authenticate(
        self, *, session_secret: str, host_generation: UUID
    ) -> CanvasOriginSession:
        """Authorize one gateway exchange against current shared identity."""

        self._require_enabled()
        try:
            secret_hash = hash_canvas_viewer_secret("session", session_secret)
        except ValueError as exc:
            raise CanvasViewerError(
                401, "canvas_session_invalid", "Canvas viewer session is invalid"
            ) from exc

        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM canvas_origin_sessions
                WHERE session_secret_hash = $1
                """,
                secret_hash,
            )
            if row is None:
                raise CanvasViewerError(
                    401, "canvas_session_invalid", "Canvas viewer session is invalid"
                )
            values = dict(row)
            session_id = UUID(str(values["id"]))
            invalid_reason: str | None = None
            if values.get("revoked_at") is not None:
                invalid_reason = "revoked"
            elif _utc(values["expires_at"]) <= _now():
                invalid_reason = "expired"
            elif UUID(str(values["origin_generation"])) != host_generation:
                invalid_reason = "origin_changed"
            elif _viewer_policy_changed(values, self._config):
                invalid_reason = "viewer_policy_changed"

            parent = None
            user = None
            thread = None
            current = None
            remote_target = None
            if invalid_reason is None:
                parent = await conn.fetchrow(
                    """
                    SELECT id, user_id, absolute_expires_at, revoked_at
                    FROM srw_sessions
                    WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
                      AND absolute_expires_at > now()
                    """,
                    values.get("parent_srw_session_id"),
                    values["user_id"],
                )
                user = await conn.fetchrow(
                    "SELECT id, is_admin, is_approved FROM users WHERE id = $1",
                    values["user_id"],
                )
                thread = await conn.fetchrow(
                    "SELECT id, user_id, metadata FROM threads WHERE id = $1",
                    values["thread_id"],
                )
                current = await self._canvas_record(conn, str(values["thread_id"]))
                if parent is None:
                    invalid_reason = "parent_session_ended"
                elif user is None or not bool(user.get("is_approved")):
                    invalid_reason = "user_not_approved"
                elif thread is None or not (
                    bool(user.get("is_admin"))
                    or str(thread.get("user_id") or "") == str(values["user_id"])
                ):
                    invalid_reason = "authorization_changed"
                elif current is None or not isinstance(
                    current.source, WorkspaceAppSource
                ):
                    invalid_reason = "canvas_replaced"
                elif (
                    current.source_fingerprint != values["source_fingerprint"]
                    or current.source.workspace_generation
                    != UUID(str(values["workspace_generation"]))
                    or current.origin_generation != host_generation
                ):
                    invalid_reason = "canvas_identity_changed"
                else:
                    try:
                        remote_target = resolve_remote_workspace_target(
                            dict(thread), current.source.workspace_generation
                        )
                    except CanvasSSHError:
                        invalid_reason = "workspace_unavailable"

            if invalid_reason is not None:
                if values.get("revoked_at") is None:
                    await conn.execute(
                        """
                        UPDATE canvas_origin_sessions
                        SET revoked_at = now(), revocation_reason = $2,
                            updated_at = now()
                        WHERE id = $1 AND revoked_at IS NULL
                        """,
                        session_id,
                        invalid_reason,
                    )
                raise CanvasViewerError(
                    401,
                    "canvas_session_invalid",
                    "Canvas viewer session is invalid or expired",
                )

        assert current is not None and thread is not None
        return CanvasOriginSession(
            id=session_id,
            user_id=UUID(str(values["user_id"])),
            thread_id=UUID(str(values["thread_id"])),
            canvas_id=str(values["canvas_id"]),
            parent_srw_session_id=UUID(str(values["parent_srw_session_id"])),
            source_fingerprint=str(values["source_fingerprint"]),
            workspace_generation=UUID(str(values["workspace_generation"])),
            origin_generation=host_generation,
            embedding_origin=str(values["embedding_origin"]),
            cookie_mode=str(values["cookie_mode"]),
            expires_at=_utc(values["expires_at"]),
            record=current,
            thread=dict(thread),
            remote_target=remote_target,
        )

    async def renew_attachment(
        self,
        *,
        attachment_id: UUID,
        user_id: str,
        thread_id: str,
        parent_session_id: UUID,
    ) -> CanvasViewerRenewal:
        """Renew the shared session through its authenticated parent only."""

        self._require_enabled()
        now = _now()
        async with self._db.acquire() as conn:
            async with conn.transaction():
                parent = await self._parent_session(conn, parent_session_id, user_id)
                await self._authorized_thread(
                    conn, user_id=user_id, thread_id=thread_id
                )
                # Keep the global lock order Canvas -> origin session. Canvas
                # replacement holds this row before its revocation trigger
                # updates sessions, so taking the locks in reverse here could
                # deadlock a renewal against a replacement.
                current = await self._canvas_record(conn, thread_id, for_share=True)
                source = _require_app_record(current)
                row = await conn.fetchrow(
                    """
                    SELECT a.*, s.source_fingerprint, s.workspace_generation,
                           s.origin_generation, s.revoked_at,
                           s.embedding_origin AS session_embedding_origin,
                           s.cookie_mode AS session_cookie_mode
                    FROM canvas_view_attachments a
                    JOIN canvas_origin_sessions s ON s.id = a.origin_session_id
                    WHERE a.id = $1 AND a.user_id = $2 AND a.thread_id = $3
                      AND a.canvas_id = 'main' AND a.parent_srw_session_id = $4
                      AND s.user_id = $2 AND s.thread_id = $3
                      AND s.canvas_id = 'main' AND s.parent_srw_session_id = $4
                      AND s.expires_at > now()
                      AND a.closed_at IS NULL AND a.expires_at > now()
                    FOR UPDATE OF a, s
                    """,
                    attachment_id,
                    user_id,
                    thread_id,
                    parent_session_id,
                )
                if row is None or row.get("revoked_at") is not None:
                    raise CanvasViewerError(
                        409,
                        "canvas_attachment_stale",
                        "Canvas viewer attachment is no longer current",
                    )
                session_policy = {
                    "embedding_origin": row["session_embedding_origin"],
                    "cookie_mode": row["session_cookie_mode"],
                }
                policy_changed = _viewer_policy_changed(session_policy, self._config)
                if policy_changed:
                    await conn.execute(
                        """
                        UPDATE canvas_origin_sessions
                        SET revoked_at = now(),
                            revocation_reason = 'viewer_policy_changed',
                            updated_at = now()
                        WHERE id = $1 AND revoked_at IS NULL
                        """,
                        row["origin_session_id"],
                    )
                elif (
                    current.source_fingerprint != row["source_fingerprint"]
                    or source.workspace_generation
                    != UUID(str(row["workspace_generation"]))
                    or current.origin_generation != UUID(str(row["origin_generation"]))
                ):
                    raise CanvasViewerError(
                        409,
                        "canvas_attachment_stale",
                        "Canvas viewer attachment is no longer current",
                    )
                else:
                    parent_expiry = _utc(parent["absolute_expires_at"])
                    attachment_expiry = min(
                        now + timedelta(seconds=self._config.attachment_ttl_seconds),
                        parent_expiry,
                    )
                    session_expiry = min(
                        now + timedelta(seconds=self._config.session_ttl_seconds),
                        attachment_expiry,
                    )
                    await conn.execute(
                        """
                        UPDATE canvas_origin_sessions
                        SET expires_at = $2, last_renewed_at = now(), updated_at = now()
                        WHERE id = $1 AND revoked_at IS NULL
                        """,
                        row["origin_session_id"],
                        session_expiry,
                    )
                    await conn.execute(
                        """
                        UPDATE canvas_view_attachments
                        SET expires_at = $2, last_seen_at = now()
                        WHERE id = $1 AND closed_at IS NULL
                        """,
                        attachment_id,
                        attachment_expiry,
                    )
        if policy_changed:
            raise CanvasViewerError(
                409,
                "canvas_attachment_stale",
                "Canvas viewer attachment was revoked by a policy change",
            )
        return CanvasViewerRenewal(
            expires_at=session_expiry,
            renew_after=_renew_after(now, session_expiry),
        )

    async def close_attachment(
        self,
        *,
        attachment_id: UUID,
        user_id: str,
        thread_id: str,
        parent_session_id: UUID,
    ) -> None:
        """Idempotently close presence without revoking a shared session."""

        self._require_enabled()
        async with self._db.acquire() as conn:
            async with conn.transaction():
                await self._parent_session(conn, parent_session_id, user_id)
                await conn.execute(
                    """
                    UPDATE canvas_view_attachments
                    SET closed_at = COALESCE(closed_at, now()), last_seen_at = now()
                    WHERE id = $1 AND user_id = $2 AND thread_id = $3
                      AND canvas_id = 'main' AND parent_srw_session_id = $4
                    """,
                    attachment_id,
                    user_id,
                    thread_id,
                    parent_session_id,
                )
                await conn.execute(
                    """
                    DELETE FROM canvas_view_bootstraps
                    WHERE attachment_id = $1 AND consumed_at IS NULL
                    """,
                    attachment_id,
                )

    async def revoke_origin(self, origin_generation: UUID, reason: str) -> int:
        if not reason or len(reason) > 64:
            raise ValueError("invalid Canvas revocation reason")
        async with self._db.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE canvas_origin_sessions
                SET revoked_at = now(), revocation_reason = $2, updated_at = now()
                WHERE origin_generation = $1 AND revoked_at IS NULL
                """,
                origin_generation,
                reason,
            )
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, AttributeError):
            return 0

    async def cleanup(self) -> None:
        """Remove expired credential material after a bounded audit window."""

        async with self._db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM canvas_view_bootstraps WHERE expires_at < now() - interval '1 hour'"
                )
                await conn.execute(
                    "DELETE FROM canvas_view_attachments WHERE expires_at < now() - interval '1 day'"
                )
                await conn.execute(
                    """
                    DELETE FROM canvas_origin_sessions
                    WHERE COALESCE(revoked_at, expires_at) < now() - interval '1 day'
                    """
                )


def canvas_viewer_error_detail(error: CanvasViewerError) -> dict[str, str]:
    return {"code": error.code, "message": error.message}


__all__ = [
    "CANVAS_VIEWER_COOKIE",
    "CanvasBootstrapExchange",
    "CanvasOriginSession",
    "CanvasViewerAttachmentGrant",
    "CanvasViewerError",
    "CanvasViewerRenewal",
    "CanvasViewerSessionService",
    "canvas_viewer_error_detail",
    "hash_canvas_viewer_secret",
]
