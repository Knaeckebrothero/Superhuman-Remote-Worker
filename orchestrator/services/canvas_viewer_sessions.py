"""Durable authentication for isolated Dynamic Canvas viewer origins.

Parent APIs use the normal BFF session and create a non-credential attachment.
The dedicated viewer gateway starts a browser-bound challenge, the authenticated
parent authorizes that exact challenge, and a same-origin exchange mints a
separate host-only viewer cookie. Plaintext challenge, binding, exchange, and
cookie material never enters PostgreSQL.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
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
CANVAS_BOOTSTRAP_COOKIE_PREFIX = "__Host-canvas_bootstrap_"
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
    """Domain-separate every stored Canvas viewer secret hash."""

    if purpose not in {
        "binding",
        "bridge",
        "challenge",
        "exchange",
        "receipt",
        "session",
    }:
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
    bridge_nonce: str
    bootstrap_expires_at: datetime
    expires_at: datetime
    renew_after: datetime

    def public_dict(self) -> dict[str, str]:
        return {
            "attachment_id": str(self.attachment_id),
            "origin": self.origin,
            "bootstrap_url": self.bootstrap_url,
            "bridge_nonce": self.bridge_nonce,
            "bootstrap_expires_at": _iso(self.bootstrap_expires_at),
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


@dataclass(frozen=True, slots=True)
class CanvasBootstrapStart:
    attachment_id: UUID
    challenge: str
    ready_receipt: str
    browser_binding: str
    embedding_origin: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CanvasBootstrapAuthorization:
    challenge: str
    ready_receipt: str
    exchange_code: str
    expires_at: datetime

    def public_dict(self) -> dict[str, str]:
        return {
            "challenge": self.challenge,
            "ready_receipt": self.ready_receipt,
            "exchange_code": self.exchange_code,
            "expires_at": _iso(self.expires_at),
        }


def canvas_bootstrap_cookie_name(attachment_id: UUID) -> str:
    """Return one host-only transient-cookie name per frame attachment."""

    return f"{CANVAS_BOOTSTRAP_COOKIE_PREFIX}{attachment_id}"


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
        conn: Any, parent_session_id: UUID, user_id: str, *, for_share: bool = True
    ) -> dict[str, Any]:
        suffix = " FOR SHARE" if for_share else ""
        row = await conn.fetchrow(
            f"""
            SELECT id, user_id, absolute_expires_at, revoked_at
            FROM srw_sessions
            WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
              AND absolute_expires_at > now(){suffix}
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
        conn: Any, *, user_id: str, thread_id: str, for_share: bool = True
    ) -> dict[str, Any]:
        user = await conn.fetchrow(
            "SELECT id, is_admin, is_approved FROM users WHERE id = $1", user_id
        )
        suffix = " FOR SHARE" if for_share else ""
        thread = await conn.fetchrow(
            f"SELECT id, user_id, metadata FROM threads WHERE id = $1{suffix}",
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
                        id, attachment_id,
                        expected_presentation_revision, source_fingerprint,
                        workspace_generation, origin_generation, expires_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    bootstrap_id,
                    attachment_id,
                    current.presentation_revision,
                    current.source_fingerprint,
                    source.workspace_generation,
                    current.origin_generation,
                    bootstrap_expiry,
                )

        assert expected_record.origin_generation is not None
        origin = self._config.public_origin(expected_record.origin_generation)
        bootstrap_url = f"{origin}/_canvas/bootstrap?attachment_id={attachment_id}"
        session_expiry = min(
            attachment_expiry,
            now + timedelta(seconds=self._config.session_ttl_seconds),
        )
        return CanvasViewerAttachmentGrant(
            attachment_id=attachment_id,
            origin=origin,
            bootstrap_url=bootstrap_url,
            bridge_nonce=bridge,
            bootstrap_expires_at=bootstrap_expiry,
            expires_at=session_expiry,
            renew_after=_renew_after(now, session_expiry),
        )

    async def begin_bootstrap(
        self, *, attachment_id: UUID, host_generation: UUID
    ) -> CanvasBootstrapStart:
        """Start one browser-bound challenge without granting authorization.

        Gateway-only, so every read of an authoritative row here is unlocked.
        The public gateway holds the restricted database role, which is granted
        SELECT and nothing else on ``users``/``threads``/``srw_sessions``/
        ``canvases``; PostgreSQL requires UPDATE on at least one column for any
        row-locking clause, so ``FOR SHARE`` here is not a stricter check but a
        guaranteed ``permission denied``. Nothing is lost: this method only
        starts a challenge, and ``authenticate`` re-derives every one of these
        facts — parent session, approval, thread ownership, canvas identity —
        without locks on each proxied exchange before any byte is served.
        """

        self._require_enabled()
        now = _now()
        challenge = _secret()
        browser_binding = _secret()
        ready_receipt = _secret()

        async with self._db.acquire() as conn:
            async with conn.transaction():
                identity = await conn.fetchrow(
                    """
                    SELECT a.thread_id
                    FROM canvas_view_attachments a
                    JOIN canvas_view_bootstraps b ON b.attachment_id = a.id
                    WHERE a.id = $1
                    """,
                    attachment_id,
                )
                if identity is None:
                    raise CanvasViewerError(
                        401,
                        "canvas_bootstrap_invalid",
                        "Canvas bootstrap is invalid or expired",
                    )
                current = await self._canvas_record(
                    conn, str(identity["thread_id"]), for_share=False
                )
                source = _require_app_record(current)
                row = await conn.fetchrow(
                    """
                    SELECT b.id AS bootstrap_id, b.attachment_id,
                           b.expected_presentation_revision,
                           b.source_fingerprint, b.workspace_generation,
                           b.origin_generation, b.expires_at AS bootstrap_expires_at,
                           b.challenge_hash, b.browser_binding_hash,
                           b.ready_receipt_hash, b.exchange_token_hash,
                           b.authorized_at, b.consumed_at,
                           a.user_id, a.thread_id, a.canvas_id,
                           a.parent_srw_session_id, a.embedding_origin,
                           a.cookie_mode AS attachment_cookie_mode,
                           a.expires_at AS attachment_expires_at, a.closed_at
                    FROM canvas_view_bootstraps b
                    JOIN canvas_view_attachments a ON a.id = b.attachment_id
                    WHERE b.attachment_id = $1
                    FOR UPDATE OF b, a
                    """,
                    attachment_id,
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
                    or _utc(values["bootstrap_expires_at"]) <= now
                    or UUID(str(values["origin_generation"])) != host_generation
                    or str(values.get("embedding_origin") or "")
                    not in self._config.cockpit_origins
                    or str(values.get("attachment_cookie_mode") or "")
                    != self._config.cookie_mode
                    or any(
                        values.get(name) is not None
                        for name in (
                            "challenge_hash",
                            "browser_binding_hash",
                            "ready_receipt_hash",
                            "exchange_token_hash",
                            "authorized_at",
                            "consumed_at",
                        )
                    )
                ):
                    raise CanvasViewerError(
                        409,
                        "canvas_bootstrap_unavailable",
                        "Canvas bootstrap must be recreated",
                    )
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
                parent_id = values.get("parent_srw_session_id")
                if parent_id is None:
                    raise CanvasViewerError(
                        401,
                        "canvas_parent_session_invalid",
                        "The Cockpit session has ended",
                    )
                await self._parent_session(
                    conn, UUID(str(parent_id)), str(values["user_id"]), for_share=False
                )
                await self._authorized_thread(
                    conn,
                    user_id=str(values["user_id"]),
                    thread_id=str(values["thread_id"]),
                    for_share=False,
                )
                started = await conn.execute(
                    """
                    UPDATE canvas_view_bootstraps
                    SET challenge_hash = $2, browser_binding_hash = $3,
                        ready_receipt_hash = $4
                    WHERE id = $1 AND challenge_hash IS NULL
                      AND browser_binding_hash IS NULL
                      AND ready_receipt_hash IS NULL
                      AND exchange_token_hash IS NULL
                      AND authorized_at IS NULL AND consumed_at IS NULL
                    """,
                    values["bootstrap_id"],
                    hash_canvas_viewer_secret("challenge", challenge),
                    hash_canvas_viewer_secret("binding", browser_binding),
                    hash_canvas_viewer_secret("receipt", ready_receipt),
                )
                if started != "UPDATE 1":
                    raise CanvasViewerError(
                        409,
                        "canvas_bootstrap_unavailable",
                        "Canvas bootstrap must be recreated",
                    )

        return CanvasBootstrapStart(
            attachment_id=attachment_id,
            challenge=challenge,
            ready_receipt=ready_receipt,
            browser_binding=browser_binding,
            embedding_origin=str(values["embedding_origin"]),
            expires_at=_utc(values["bootstrap_expires_at"]),
        )

    async def authorize_bootstrap(
        self,
        *,
        attachment_id: UUID,
        user_id: str,
        thread_id: str,
        parent_session_id: UUID,
        embedding_origin: str | None,
        challenge: str,
        ready_receipt: str,
        bridge_nonce: str,
    ) -> CanvasBootstrapAuthorization:
        """Authorize the exact browser challenge through its current BFF."""

        self._require_enabled()
        try:
            embedding = self._config.require_cockpit_origin(embedding_origin)
            challenge_hash = hash_canvas_viewer_secret("challenge", challenge)
            receipt_hash = hash_canvas_viewer_secret("receipt", ready_receipt)
            bridge_hash = hash_canvas_viewer_secret("bridge", bridge_nonce)
        except (CanvasViewerConfigurationError, ValueError) as exc:
            raise CanvasViewerError(
                401,
                "canvas_bootstrap_invalid",
                "Canvas bootstrap authorization is invalid",
            ) from exc
        now = _now()
        exchange_code = _secret()

        async with self._db.acquire() as conn:
            async with conn.transaction():
                parent = await self._parent_session(conn, parent_session_id, user_id)
                await self._authorized_thread(
                    conn, user_id=user_id, thread_id=thread_id
                )
                current = await self._canvas_record(conn, thread_id, for_share=True)
                source = _require_app_record(current)
                row = await conn.fetchrow(
                    """
                    SELECT b.id AS bootstrap_id, b.expected_presentation_revision,
                           b.source_fingerprint, b.workspace_generation,
                           b.origin_generation, b.expires_at AS bootstrap_expires_at,
                           b.challenge_hash, b.browser_binding_hash,
                           b.ready_receipt_hash, b.exchange_token_hash,
                           b.authorized_at, b.consumed_at,
                           a.bridge_nonce_hash, a.embedding_origin,
                           a.cookie_mode AS attachment_cookie_mode,
                           a.expires_at AS attachment_expires_at, a.closed_at
                    FROM canvas_view_bootstraps b
                    JOIN canvas_view_attachments a ON a.id = b.attachment_id
                    WHERE a.id = $1 AND a.user_id = $2 AND a.thread_id = $3
                      AND a.canvas_id = 'main' AND a.parent_srw_session_id = $4
                    FOR UPDATE OF b, a
                    """,
                    attachment_id,
                    user_id,
                    thread_id,
                    parent_session_id,
                )
                if row is None:
                    raise CanvasViewerError(
                        403,
                        "canvas_bootstrap_forbidden",
                        "Canvas bootstrap does not belong to this session",
                    )
                values = dict(row)
                if (
                    values.get("closed_at") is not None
                    or _utc(values["attachment_expires_at"]) <= now
                    or _utc(values["bootstrap_expires_at"]) <= now
                    or values.get("consumed_at") is not None
                    or values.get("authorized_at") is not None
                    or values.get("exchange_token_hash") is not None
                    or not values.get("browser_binding_hash")
                    or str(values.get("embedding_origin") or "") != embedding
                    or str(values.get("attachment_cookie_mode") or "")
                    != self._config.cookie_mode
                    or not secrets.compare_digest(
                        str(values.get("challenge_hash") or ""), challenge_hash
                    )
                    or not secrets.compare_digest(
                        str(values.get("ready_receipt_hash") or ""), receipt_hash
                    )
                    or not secrets.compare_digest(
                        str(values.get("bridge_nonce_hash") or ""), bridge_hash
                    )
                ):
                    raise CanvasViewerError(
                        401,
                        "canvas_bootstrap_invalid",
                        "Canvas bootstrap authorization is invalid or expired",
                    )
                if (
                    current.presentation_revision
                    != int(values["expected_presentation_revision"])
                    or current.source_fingerprint != values["source_fingerprint"]
                    or source.workspace_generation
                    != UUID(str(values["workspace_generation"]))
                    or current.origin_generation
                    != UUID(str(values["origin_generation"]))
                ):
                    raise CanvasViewerError(
                        409,
                        "canvas_bootstrap_stale",
                        "Canvas changed before the viewer was authorized",
                    )
                authorized = await conn.execute(
                    """
                    UPDATE canvas_view_bootstraps
                    SET exchange_token_hash = $2, authorized_at = now()
                    WHERE id = $1 AND exchange_token_hash IS NULL
                      AND authorized_at IS NULL AND consumed_at IS NULL
                    """,
                    values["bootstrap_id"],
                    hash_canvas_viewer_secret("exchange", exchange_code),
                )
                if authorized != "UPDATE 1":
                    raise CanvasViewerError(
                        401,
                        "canvas_bootstrap_invalid",
                        "Canvas bootstrap authorization was already used",
                    )
                expires_at = min(
                    _utc(values["bootstrap_expires_at"]),
                    _utc(values["attachment_expires_at"]),
                    _utc(parent["absolute_expires_at"]),
                )

        return CanvasBootstrapAuthorization(
            challenge=challenge,
            ready_receipt=ready_receipt,
            exchange_code=exchange_code,
            expires_at=expires_at,
        )

    async def exchange_bootstrap(
        self,
        *,
        attachment_id: UUID,
        challenge: str,
        exchange_code: str,
        browser_binding: str,
        host_generation: UUID,
        existing_session_secret: str | None,
    ) -> CanvasBootstrapExchange:
        """Consume one authorized exchange bound to this browser and host.

        Gateway-only, so the authoritative rows are read unlocked for the same
        reason ``begin_bootstrap`` reads them unlocked. The one-time exchange
        is still serialized: the bootstrap and attachment rows below are taken
        ``FOR UPDATE``, and those the restricted role may lock because it holds
        UPDATE on their columns.
        """

        self._require_enabled()
        try:
            challenge_hash = hash_canvas_viewer_secret("challenge", challenge)
            exchange_hash = hash_canvas_viewer_secret("exchange", exchange_code)
            binding_hash = hash_canvas_viewer_secret("binding", browser_binding)
        except ValueError as exc:
            raise CanvasViewerError(
                401, "canvas_bootstrap_invalid", "Canvas bootstrap is invalid"
            ) from exc
        now = _now()
        new_secret: str | None = None

        async with self._db.acquire() as conn:
            async with conn.transaction():
                identity = await conn.fetchrow(
                    """
                    SELECT a.thread_id
                    FROM canvas_view_attachments a
                    JOIN canvas_view_bootstraps b ON b.attachment_id = a.id
                    WHERE a.id = $1
                    """,
                    attachment_id,
                )
                if identity is None:
                    raise CanvasViewerError(
                        401,
                        "canvas_bootstrap_invalid",
                        "Canvas bootstrap is invalid or expired",
                    )
                current = await self._canvas_record(
                    conn, str(identity["thread_id"]), for_share=False
                )
                source = _require_app_record(current)
                row = await conn.fetchrow(
                    """
                    SELECT b.id AS bootstrap_id, b.attachment_id,
                           b.expected_presentation_revision,
                           b.source_fingerprint, b.workspace_generation,
                           b.origin_generation, b.expires_at AS bootstrap_expires_at,
                           b.challenge_hash, b.browser_binding_hash,
                           b.exchange_token_hash, b.authorized_at, b.consumed_at,
                           a.user_id, a.thread_id, a.canvas_id,
                           a.parent_srw_session_id, a.embedding_origin,
                           a.cookie_mode AS attachment_cookie_mode,
                           a.expires_at AS attachment_expires_at, a.closed_at
                    FROM canvas_view_bootstraps b
                    JOIN canvas_view_attachments a ON a.id = b.attachment_id
                    WHERE b.attachment_id = $1
                    FOR UPDATE OF b, a
                    """,
                    attachment_id,
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
                    or _utc(values["bootstrap_expires_at"]) <= now
                    or values.get("authorized_at") is None
                    or values.get("consumed_at") is not None
                    or UUID(str(values["origin_generation"])) != host_generation
                    or str(values.get("embedding_origin") or "")
                    not in self._config.cockpit_origins
                    or str(values.get("attachment_cookie_mode") or "")
                    != self._config.cookie_mode
                    or not secrets.compare_digest(
                        str(values.get("challenge_hash") or ""), challenge_hash
                    )
                    or not secrets.compare_digest(
                        str(values.get("browser_binding_hash") or ""), binding_hash
                    )
                    or not secrets.compare_digest(
                        str(values.get("exchange_token_hash") or ""), exchange_hash
                    )
                ):
                    raise CanvasViewerError(
                        401,
                        "canvas_bootstrap_invalid",
                        "Canvas bootstrap is invalid, expired, or already used",
                    )
                parent_id = values.get("parent_srw_session_id")
                if parent_id is None:
                    raise CanvasViewerError(
                        401,
                        "canvas_parent_session_invalid",
                        "The Cockpit session has ended",
                    )
                parent = await self._parent_session(
                    conn, UUID(str(parent_id)), str(values["user_id"]), for_share=False
                )
                await self._authorized_thread(
                    conn,
                    user_id=str(values["user_id"]),
                    thread_id=str(values["thread_id"]),
                    for_share=False,
                )
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
                            SELECT id FROM canvas_origin_sessions
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

                expires_at = min(
                    now + timedelta(seconds=self._config.session_ttl_seconds),
                    _utc(parent["absolute_expires_at"]),
                    _utc(values["attachment_expires_at"]),
                )
                if session_row is None:
                    new_secret = _secret()
                    session_id = uuid4()
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
                    new_secret = existing_session_secret
                    session_id = UUID(str(session_row["id"]))
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
                SELECT id, user_id, thread_id, canvas_id,
                       parent_srw_session_id, source_fingerprint,
                       workspace_generation, origin_generation,
                       embedding_origin, cookie_mode, expires_at, revoked_at
                FROM canvas_origin_sessions
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
    "CANVAS_BOOTSTRAP_COOKIE_PREFIX",
    "CANVAS_VIEWER_COOKIE",
    "CanvasBootstrapAuthorization",
    "CanvasBootstrapExchange",
    "CanvasBootstrapStart",
    "CanvasOriginSession",
    "CanvasViewerAttachmentGrant",
    "CanvasViewerError",
    "CanvasViewerRenewal",
    "CanvasViewerSessionService",
    "canvas_bootstrap_cookie_name",
    "canvas_viewer_error_detail",
    "hash_canvas_viewer_secret",
]
