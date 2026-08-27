from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import canvases as canvas_routes
from services.canvas import (
    CanvasCapabilities,
    CanvasRecord,
    WorkspaceAppSource,
    build_public_canvas_representation,
)
from services.canvas_session_notifications import (
    CANVAS_SESSION_CHANGE_CHANNEL,
    CanvasConnectionRegistry,
    CanvasSessionNotificationListener,
)
from services.canvas_viewer_config import (
    CanvasViewerConfig,
    CanvasViewerConfigurationError,
    canvas_viewer_config,
)
from services.canvas_viewer_sessions import (
    CanvasBootstrapAuthorization,
    CanvasViewerAttachmentGrant,
    CanvasViewerError,
    CanvasViewerSessionService,
    hash_canvas_viewer_secret,
)


_VIEWER_ENV = (
    "CANVAS_LIVE_PREVIEW_ENABLED",
    "CANVAS_VIEWER_ENABLED",
    "CANVAS_VIEWER_DEPLOYMENT_PROFILE",
    "CANVAS_VIEWER_COOKIE_MODE",
    "CANVAS_VIEWER_DOMAIN",
    "CANVAS_VIEWER_HOST_SUFFIX",
    "CANVAS_VIEWER_COCKPIT_ORIGINS",
    "CANVAS_VIEWER_PSL_BOUNDARY_VERIFIED",
)


def _clear_viewer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _VIEWER_ENV:
        monkeypatch.delenv(name, raising=False)


def _enable_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANVAS_LIVE_PREVIEW_ENABLED", "true")
    monkeypatch.setenv("CANVAS_VIEWER_ENABLED", "true")
    monkeypatch.setenv("CANVAS_VIEWER_DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("CANVAS_VIEWER_COOKIE_MODE", "psl-isolated")
    monkeypatch.setenv("CANVAS_VIEWER_DOMAIN", "user-content.test")
    monkeypatch.setenv("CANVAS_VIEWER_HOST_SUFFIX", ".user-content.test")
    monkeypatch.setenv("CANVAS_VIEWER_COCKPIT_ORIGINS", "https://cockpit.platform.test")
    monkeypatch.setenv("CANVAS_VIEWER_PSL_BOUNDARY_VERIFIED", "true")


def test_viewer_defaults_off_without_requiring_domain_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_viewer_env(monkeypatch)
    assert canvas_viewer_config().enabled is False

    monkeypatch.setenv("CANVAS_VIEWER_ENABLED", "true")
    # The browser viewer cannot outrun the master callable-app gate.
    assert canvas_viewer_config().enabled is False


def test_production_viewer_requires_all_isolation_attestations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_viewer_env(monkeypatch)
    _enable_production(monkeypatch)
    config = canvas_viewer_config()
    generation = uuid4()
    host = f"{generation}.user-content.test"

    assert config.enabled is True
    assert config.cookie_mode == "psl-isolated"
    assert config.generation_for_host(host) == generation
    assert config.public_origin(generation) == f"https://{host}"
    assert (
        config.require_cockpit_origin("https://COCKPIT.platform.test/")
        == "https://cockpit.platform.test"
    )

    monkeypatch.delenv("CANVAS_VIEWER_PSL_BOUNDARY_VERIFIED")
    with pytest.raises(CanvasViewerConfigurationError, match="PSL"):
        canvas_viewer_config()

    monkeypatch.setenv("CANVAS_VIEWER_PSL_BOUNDARY_VERIFIED", "true")
    monkeypatch.setenv("CANVAS_VIEWER_DOMAIN", "different.test")
    with pytest.raises(CanvasViewerConfigurationError, match="parent"):
        canvas_viewer_config()

    monkeypatch.setenv("CANVAS_VIEWER_DOMAIN", "user-content.test")
    monkeypatch.setenv(
        "CANVAS_VIEWER_COCKPIT_ORIGINS",
        "https://cockpit.platform.test;script-src *",
    )
    with pytest.raises(CanvasViewerConfigurationError, match="invalid origin"):
        canvas_viewer_config()


def test_production_viewer_accepts_origins_directly_below_dedicated_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_viewer_env(monkeypatch)
    _enable_production(monkeypatch)
    monkeypatch.setenv("CANVAS_VIEWER_HOST_SUFFIX", ".user-content.test")

    config = canvas_viewer_config()
    generation = uuid4()

    assert config.generation_for_host(f"{generation}.user-content.test") == generation
    assert config.public_origin(generation) == (
        f"https://{generation}.user-content.test"
    )
    with pytest.raises(CanvasViewerConfigurationError, match="host is invalid"):
        config.generation_for_host(f"nested.{generation}.user-content.test")


def test_production_viewer_rejects_suffix_below_attested_psl_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_viewer_env(monkeypatch)
    _enable_production(monkeypatch)
    monkeypatch.setenv(
        "CANVAS_VIEWER_HOST_SUFFIX",
        ".canvas.user-content.test",
    )

    with pytest.raises(CanvasViewerConfigurationError, match="exact effective-PSL"):
        canvas_viewer_config()


def test_cookie_free_mode_is_development_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_viewer_env(monkeypatch)
    _enable_production(monkeypatch)
    monkeypatch.setenv("CANVAS_VIEWER_COOKIE_MODE", "development-cookie-free")
    with pytest.raises(CanvasViewerConfigurationError, match="Production"):
        canvas_viewer_config()

    monkeypatch.setenv("CANVAS_VIEWER_DEPLOYMENT_PROFILE", "development")
    monkeypatch.delenv("CANVAS_VIEWER_PSL_BOUNDARY_VERIFIED")
    assert canvas_viewer_config().cookie_mode == "development-cookie-free"


@pytest.mark.parametrize(
    "host",
    [
        "not-a-uuid.user-content.test",
        "00000000-0000-0000-0000-000000000000.evil.user-content.test",
        "00000000-0000-0000-0000-000000000000.user-content.test:443",
        "00000000-0000-0000-0000-000000000000.user-content.test.evil",
    ],
)
def test_viewer_host_resolution_is_exact(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    _clear_viewer_env(monkeypatch)
    _enable_production(monkeypatch)
    with pytest.raises(CanvasViewerConfigurationError):
        canvas_viewer_config().generation_for_host(host)


def test_viewer_secret_hashes_are_purpose_separated_and_never_plaintext() -> None:
    secret = "a" * 43
    purposes = ("binding", "bridge", "challenge", "exchange", "receipt", "session")
    hashes = {
        purpose: hash_canvas_viewer_secret(purpose, secret) for purpose in purposes
    }
    assert len(set(hashes.values())) == len(purposes)
    assert all(secret not in digest and len(digest) == 64 for digest in hashes.values())
    with pytest.raises(ValueError):
        hash_canvas_viewer_secret("unknown", secret)


@pytest.mark.asyncio
async def test_connection_registry_bounds_and_revokes_active_exchanges() -> None:
    registry = CanvasConnectionRegistry(max_connections=2, max_per_session=1)
    first = uuid4()
    second = uuid4()
    async with registry.register(first) as lease:
        with pytest.raises(CanvasViewerError) as limited:
            async with registry.register(first):
                pass
        assert limited.value.status_code == 429

        async with registry.register(second) as other:
            assert not lease.cancelled.is_set()
            assert not other.cancelled.is_set()
            await registry.revoke_session(first)
            assert lease.cancelled.is_set()
            assert not other.cancelled.is_set()


@pytest.mark.asyncio
async def test_notification_listener_cancels_the_matching_session_only() -> None:
    registry = CanvasConnectionRegistry(max_connections=2, max_per_session=1)
    first = uuid4()
    second = uuid4()
    listener = CanvasSessionNotificationListener(SimpleNamespace(), registry)
    async with registry.register(first) as lease:
        async with registry.register(second) as other:
            listener._notification(  # noqa: SLF001
                None,
                1,
                CANVAS_SESSION_CHANGE_CHANNEL,
                json.dumps({"kind": "session", "id": str(first)}),
            )
            await asyncio.sleep(0)
            assert lease.cancelled.is_set()
            assert not other.cancelled.is_set()


def test_viewer_migration_stores_only_hashes_and_installs_revocation_triggers() -> None:
    initial_path = Path(
        "orchestrator/database/migrations/app/0061_canvas_viewer_sessions.sql"
    )
    initial_bytes = initial_path.read_bytes()
    initial = initial_bytes.decode()
    assert hashlib.sha256(initial_bytes).hexdigest() == (
        "e6a379188114dab466690ec7397300d8ae952a5077880be87b6de6fe05cdb003"
    )
    for table in (
        "canvas_origin_sessions",
        "canvas_view_attachments",
        "canvas_view_bootstraps",
    ):
        assert f"CREATE TABLE {table}" in initial
    assert "session_secret_hash" in initial
    assert "token_hash" in initial
    assert "session_secret TEXT" not in initial
    assert "bootstrap_token" not in initial
    assert "ck_canvas_attachment_cookie_mode" not in initial
    assert "trg_canvas_revoke_retired_origin" in initial
    assert "trg_canvas_revoke_bff_session" in initial
    assert "trg_canvas_revoke_user_admission" in initial
    assert "canvas_session_changes" in initial

    exchange_path = Path(
        "orchestrator/database/migrations/app/0062_canvas_bootstrap_exchange.sql"
    )
    exchange_bytes = exchange_path.read_bytes()
    exchange = exchange_bytes.decode()
    assert hashlib.sha256(exchange_bytes).hexdigest() == (
        "53b5956c4f7dc31cf4a7277521244d8f26f62832321dcbfc6ee212d88d902a6d"
    )
    for stored_hash in (
        "exchange_token_hash",
        "challenge_hash",
        "browser_binding_hash",
        "ready_receipt_hash",
    ):
        assert stored_hash in exchange
    assert "ck_canvas_bootstrap_exchange_state" in exchange
    assert "ck_canvas_attachment_cookie_mode" in exchange
    assert "uq_canvas_bootstrap_attachment" in exchange
    assert "ALTER COLUMN exchange_token_hash DROP NOT NULL" in exchange
    assert "REFERENCES canvas_origin_sessions(id) ON DELETE CASCADE" in exchange
    for plaintext_column in (
        "exchange_code TEXT",
        "challenge TEXT",
        "browser_binding TEXT",
        "ready_receipt TEXT",
        "bridge_nonce TEXT",
    ):
        assert plaintext_column not in exchange


class _RouteCanvasService:
    def __init__(self, record: CanvasRecord):
        self.record = record

    async def get(self, thread_id: str) -> CanvasRecord:
        assert thread_id == self.record.thread_id
        return self.record


class _RouteViewerService:
    def __init__(
        self,
        grant: CanvasViewerAttachmentGrant,
        *,
        authorization: CanvasBootstrapAuthorization | None = None,
        authorized_parent: object | None = None,
    ):
        self.grant = grant
        self.calls: list[dict[str, object]] = []
        self.authorization = authorization
        self.authorized_parent = authorized_parent
        self.authorization_calls: list[dict[str, object]] = []

    async def create_attachment(self, **kwargs):
        self.calls.append(kwargs)
        return self.grant

    async def authorize_bootstrap(self, **kwargs):
        self.authorization_calls.append(kwargs)
        if (
            self.authorized_parent is not None
            and kwargs.get("parent_session_id") != self.authorized_parent
        ):
            raise CanvasViewerError(
                403,
                "canvas_bootstrap_forbidden",
                "Canvas bootstrap does not belong to this session",
            )
        assert self.authorization is not None
        return self.authorization


def _app_record() -> CanvasRecord:
    now = datetime.now(UTC)
    return CanvasRecord(
        thread_id="a3333333-3333-3333-3333-333333333333",
        canvas_id="main",
        source=WorkspaceAppSource(
            entry_port=8501,
            entry_path="/demo",
            workspace_generation=uuid4(),
        ),
        title="Live demo",
        renderer="auto",
        editable=False,
        alt_text=None,
        presentation_revision=3,
        source_fingerprint="sha256:" + "a" * 64,
        source_version=None,
        origin_generation=uuid4(),
        created_at=now,
        updated_at=now,
    )


def _viewer_config(*, origins: frozenset[str]) -> CanvasViewerConfig:
    return CanvasViewerConfig(
        enabled=True,
        host_suffix=".canvas.user-content.test",
        cookie_mode="psl-isolated",
        deployment_profile="production",
        cockpit_origins=origins,
        session_ttl_seconds=900,
        bootstrap_ttl_seconds=60,
        attachment_ttl_seconds=1200,
        revalidate_seconds=15,
    )


class _AsyncValueContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ViewerPolicyConnection:
    def __init__(self, *, session: dict[str, object], record: CanvasRecord | None):
        self.session = session
        self.record = record
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return _AsyncValueContext(self)

    async def fetchrow(self, query: str, *args):
        if "FROM canvas_origin_sessions" in query:
            return self.session
        if "FROM srw_sessions" in query:
            return {
                "id": args[0],
                "user_id": args[1],
                "absolute_expires_at": datetime.now(UTC) + timedelta(hours=1),
                "revoked_at": None,
            }
        if "FROM users" in query:
            return {"id": args[0], "is_admin": False, "is_approved": True}
        if "FROM threads" in query:
            return {
                "id": args[-1],
                "user_id": self.session["user_id"],
                "metadata": {},
            }
        if "FROM canvases" in query:
            assert self.record is not None
            return {
                "thread_id": self.record.thread_id,
                "canvas_id": self.record.canvas_id,
                "source": self.record.source.model_dump(mode="json"),
                "title": self.record.title,
                "renderer": self.record.renderer,
                "editable": self.record.editable,
                "alt_text": self.record.alt_text,
                "presentation_revision": self.record.presentation_revision,
                "source_fingerprint": self.record.source_fingerprint,
                "source_version": self.record.source_version,
                "origin_generation": self.record.origin_generation,
                "created_at": self.record.created_at,
                "updated_at": self.record.updated_at,
            }
        if "FROM canvas_view_attachments" in query:
            return self.session
        raise AssertionError(f"Unexpected query: {query}")

    async def execute(self, query: str, *args):
        self.executions.append((query, args))
        return "UPDATE 1"


class _ViewerPolicyDB:
    def __init__(self, connection: _ViewerPolicyConnection):
        self.connection = connection

    def acquire(self):
        return _AsyncValueContext(self.connection)


class _CreateAttachmentConnection:
    def __init__(self, *, record: CanvasRecord, user_id: object, parent_id: object):
        self.record = record
        self.user_id = user_id
        self.parent_id = parent_id
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return _AsyncValueContext(self)

    async def fetchrow(self, query: str, *args):
        if "FROM srw_sessions" in query:
            assert args == (self.parent_id, str(self.user_id))
            return {
                "id": self.parent_id,
                "user_id": self.user_id,
                "absolute_expires_at": datetime.now(UTC) + timedelta(hours=1),
                "revoked_at": None,
            }
        if "FROM users" in query:
            return {"id": self.user_id, "is_admin": False, "is_approved": True}
        if "FROM threads" in query:
            return {
                "id": self.record.thread_id,
                "user_id": str(self.user_id),
                "metadata": {},
            }
        if "FROM canvases" in query:
            return {
                "thread_id": self.record.thread_id,
                "canvas_id": self.record.canvas_id,
                "source": self.record.source.model_dump(mode="json"),
                "title": self.record.title,
                "renderer": self.record.renderer,
                "editable": self.record.editable,
                "alt_text": self.record.alt_text,
                "presentation_revision": self.record.presentation_revision,
                "source_fingerprint": self.record.source_fingerprint,
                "source_version": self.record.source_version,
                "origin_generation": self.record.origin_generation,
                "created_at": self.record.created_at,
                "updated_at": self.record.updated_at,
            }
        raise AssertionError(f"Unexpected query: {query}")

    async def execute(self, query: str, *args):
        self.executions.append((query, args))
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_attachment_url_is_only_canonical_public_locator() -> None:
    record = _app_record()
    assert record.origin_generation is not None
    user_id = uuid4()
    parent_id = uuid4()
    connection = _CreateAttachmentConnection(
        record=record,
        user_id=user_id,
        parent_id=parent_id,
    )
    config = _viewer_config(origins=frozenset({"https://cockpit.platform.test"}))
    service = CanvasViewerSessionService(
        _ViewerPolicyDB(connection),
        config=config,
    )

    grant = await service.create_attachment(
        user_id=str(user_id),
        thread_id=record.thread_id,
        parent_session_id=parent_id,
        embedding_origin="https://cockpit.platform.test",
        expected_record=record,
    )

    assert grant.bootstrap_url == (
        f"{config.public_origin(record.origin_generation)}/_canvas/bootstrap?"
        f"attachment_id={grant.attachment_id}"
    )
    assert "token=" not in grant.bootstrap_url
    assert grant.bridge_nonce not in grant.bootstrap_url
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,128}", grant.bridge_nonce)
    assert grant.bootstrap_expires_at <= grant.expires_at
    persisted_arguments = repr(connection.executions)
    assert grant.bridge_nonce not in persisted_arguments


class _BootstrapPolicyConnection:
    def __init__(self, row: dict[str, object], record: CanvasRecord):
        self.row = row
        self.record = record

    def transaction(self):
        return _AsyncValueContext(self)

    async def fetchrow(self, query: str, *args):
        if "SELECT a.thread_id" in query:
            return {"thread_id": self.record.thread_id}
        if "FROM canvases" in query:
            return {
                "thread_id": self.record.thread_id,
                "canvas_id": self.record.canvas_id,
                "source": self.record.source.model_dump(mode="json"),
                "title": self.record.title,
                "renderer": self.record.renderer,
                "editable": self.record.editable,
                "alt_text": self.record.alt_text,
                "presentation_revision": self.record.presentation_revision,
                "source_fingerprint": self.record.source_fingerprint,
                "source_version": self.record.source_version,
                "origin_generation": self.record.origin_generation,
                "created_at": self.record.created_at,
                "updated_at": self.record.updated_at,
            }
        if "FROM canvas_view_bootstraps" in query:
            return self.row
        raise AssertionError(f"Unexpected query: {query}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_origin", "stored_mode"),
    [
        ("https://removed-cockpit.platform.test", "psl-isolated"),
        ("https://cockpit.platform.test", "development-cookie-free"),
    ],
)
async def test_bootstrap_rejects_attachment_after_viewer_policy_change(
    stored_origin: str, stored_mode: str
) -> None:
    record = _app_record()
    assert record.origin_generation is not None
    assert record.source_fingerprint is not None
    assert isinstance(record.source, WorkspaceAppSource)
    generation = record.origin_generation
    now = datetime.now(UTC)
    connection = _BootstrapPolicyConnection(
        {
            "bootstrap_id": uuid4(),
            "attachment_id": uuid4(),
            "expected_presentation_revision": record.presentation_revision,
            "source_fingerprint": record.source_fingerprint,
            "workspace_generation": record.source.workspace_generation,
            "origin_generation": generation,
            "bootstrap_expires_at": now + timedelta(seconds=30),
            "user_id": uuid4(),
            "thread_id": record.thread_id,
            "canvas_id": "main",
            "parent_srw_session_id": uuid4(),
            "embedding_origin": stored_origin,
            "attachment_cookie_mode": stored_mode,
            "attachment_expires_at": now + timedelta(minutes=10),
            "closed_at": None,
            "challenge_hash": None,
            "browser_binding_hash": None,
            "ready_receipt_hash": None,
            "exchange_token_hash": None,
            "authorized_at": None,
            "consumed_at": None,
        },
        record,
    )
    service = CanvasViewerSessionService(
        _ViewerPolicyDB(connection),
        config=_viewer_config(origins=frozenset({"https://cockpit.platform.test"})),
    )

    with pytest.raises(CanvasViewerError) as rejected:
        await service.begin_bootstrap(
            attachment_id=connection.row["attachment_id"],
            host_generation=generation,
        )

    assert rejected.value.status_code == 409
    assert rejected.value.code == "canvas_bootstrap_unavailable"


class _AuthorizeIdentityConnection:
    def __init__(
        self,
        *,
        record: CanvasRecord,
        attachment_id: object,
        stored_user_id: object,
        stored_parent_id: object,
    ):
        self.record = record
        self.attachment_id = attachment_id
        self.stored_user_id = stored_user_id
        self.stored_parent_id = stored_parent_id
        self.bootstrap_queries: list[tuple[object, ...]] = []

    def transaction(self):
        return _AsyncValueContext(self)

    async def fetchrow(self, query: str, *args):
        if "FROM srw_sessions" in query:
            return {
                "id": args[0],
                "user_id": args[1],
                "absolute_expires_at": datetime.now(UTC) + timedelta(hours=1),
                "revoked_at": None,
            }
        if "FROM users" in query:
            # Model an approved admin so different-user denial is specifically
            # the attachment/BFF binding, not thread authorization.
            return {"id": args[0], "is_admin": True, "is_approved": True}
        if "FROM threads" in query:
            return {
                "id": self.record.thread_id,
                "user_id": self.stored_user_id,
                "metadata": {},
            }
        if "FROM canvases" in query:
            return {
                "thread_id": self.record.thread_id,
                "canvas_id": self.record.canvas_id,
                "source": self.record.source.model_dump(mode="json"),
                "title": self.record.title,
                "renderer": self.record.renderer,
                "editable": self.record.editable,
                "alt_text": self.record.alt_text,
                "presentation_revision": self.record.presentation_revision,
                "source_fingerprint": self.record.source_fingerprint,
                "source_version": self.record.source_version,
                "origin_generation": self.record.origin_generation,
                "created_at": self.record.created_at,
                "updated_at": self.record.updated_at,
            }
        if "FROM canvas_view_bootstraps" in query:
            self.bootstrap_queries.append(args)
            if args == (
                self.attachment_id,
                self.stored_user_id,
                self.record.thread_id,
                self.stored_parent_id,
            ):
                raise AssertionError(
                    "denial fixture unexpectedly matched stored identity"
                )
            return None
        raise AssertionError(f"Unexpected query: {query}")


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_mismatch", ["different_user", "different_parent"])
async def test_bootstrap_authorization_is_bound_to_exact_user_and_parent_session(
    identity_mismatch: str,
) -> None:
    record = _app_record()
    attachment_id = uuid4()
    stored_user = uuid4()
    stored_parent = uuid4()
    caller_user = uuid4() if identity_mismatch == "different_user" else stored_user
    caller_parent = (
        uuid4() if identity_mismatch == "different_parent" else stored_parent
    )
    connection = _AuthorizeIdentityConnection(
        record=record,
        attachment_id=attachment_id,
        stored_user_id=str(stored_user),
        stored_parent_id=stored_parent,
    )
    service = CanvasViewerSessionService(
        _ViewerPolicyDB(connection),
        config=_viewer_config(origins=frozenset({"https://cockpit.platform.test"})),
    )

    with pytest.raises(CanvasViewerError) as rejected:
        await service.authorize_bootstrap(
            attachment_id=attachment_id,
            user_id=str(caller_user),
            thread_id=record.thread_id,
            parent_session_id=caller_parent,
            embedding_origin="https://cockpit.platform.test",
            challenge="c" * 43,
            ready_receipt="r" * 43,
            bridge_nonce="b" * 43,
        )

    assert rejected.value.status_code == 403
    assert rejected.value.code == "canvas_bootstrap_forbidden"
    assert connection.bootstrap_queries == [
        (attachment_id, str(caller_user), record.thread_id, caller_parent)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_mode", "stored_origin"),
    [
        ("development-cookie-free", "https://cockpit.platform.test"),
        ("psl-isolated", "https://removed-cockpit.platform.test"),
    ],
)
async def test_gateway_auth_revokes_sessions_after_viewer_policy_changes(
    stored_mode: str, stored_origin: str
) -> None:
    generation = uuid4()
    session_id = uuid4()
    secret = "s" * 43
    connection = _ViewerPolicyConnection(
        session={
            "id": session_id,
            "user_id": uuid4(),
            "thread_id": uuid4(),
            "canvas_id": "main",
            "parent_srw_session_id": uuid4(),
            "session_secret_hash": hash_canvas_viewer_secret("session", secret),
            "source_fingerprint": "sha256:" + "a" * 64,
            "workspace_generation": uuid4(),
            "origin_generation": generation,
            "embedding_origin": stored_origin,
            "cookie_mode": stored_mode,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "revoked_at": None,
        },
        record=None,
    )
    service = CanvasViewerSessionService(
        _ViewerPolicyDB(connection),
        config=_viewer_config(origins=frozenset({"https://cockpit.platform.test"})),
    )

    with pytest.raises(CanvasViewerError) as rejected:
        await service.authenticate(
            session_secret=secret,
            host_generation=generation,
        )

    assert rejected.value.status_code == 401
    assert len(connection.executions) == 1
    assert connection.executions[0][1] == (session_id, "viewer_policy_changed")


@pytest.mark.asyncio
async def test_parent_renewal_commits_revocation_after_embedding_origin_removal() -> (
    None
):
    record = _app_record()
    session_id = uuid4()
    attachment_id = uuid4()
    user_id = uuid4()
    parent_id = uuid4()
    assert record.origin_generation is not None
    assert record.source_fingerprint is not None
    assert isinstance(record.source, WorkspaceAppSource)
    connection = _ViewerPolicyConnection(
        session={
            "id": attachment_id,
            "origin_session_id": session_id,
            "user_id": user_id,
            "thread_id": record.thread_id,
            "parent_srw_session_id": parent_id,
            "source_fingerprint": record.source_fingerprint,
            "workspace_generation": record.source.workspace_generation,
            "origin_generation": record.origin_generation,
            "session_embedding_origin": "https://removed-cockpit.platform.test",
            "session_cookie_mode": "psl-isolated",
            "revoked_at": None,
        },
        record=record,
    )
    service = CanvasViewerSessionService(
        _ViewerPolicyDB(connection),
        config=_viewer_config(origins=frozenset({"https://cockpit.platform.test"})),
    )

    with pytest.raises(CanvasViewerError) as rejected:
        await service.renew_attachment(
            attachment_id=attachment_id,
            user_id=str(user_id),
            thread_id=record.thread_id,
            parent_session_id=parent_id,
        )

    assert rejected.value.status_code == 409
    assert len(connection.executions) == 1
    query, args = connection.executions[0]
    assert "viewer_policy_changed" in query
    assert args == (session_id,)


def test_attachment_route_requires_bff_cookie_and_exact_state_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _app_record()
    representation = build_public_canvas_representation(
        record,
        status="ready",
        capabilities=CanvasCapabilities(can_create_viewer_session=True),
    )
    now = datetime.now(UTC)
    attachment_id = uuid4()
    origin = f"https://{record.origin_generation}.canvas.user-content.test"
    grant = CanvasViewerAttachmentGrant(
        attachment_id=attachment_id,
        origin=origin,
        bootstrap_url=f"{origin}/_canvas/bootstrap?attachment_id={attachment_id}",
        bridge_nonce="b" * 43,
        bootstrap_expires_at=now + timedelta(seconds=60),
        expires_at=now + timedelta(minutes=20),
        renew_after=now + timedelta(minutes=10),
    )
    viewer = _RouteViewerService(grant)
    db = SimpleNamespace()

    async def owner(request, current_db, thread_id):
        assert current_db is db
        return {"id": "b4444444-4444-4444-4444-444444444444"}, {
            "id": thread_id,
            "user_id": "b4444444-4444-4444-4444-444444444444",
        }

    async def represent(*args, **kwargs):
        return representation

    monkeypatch.setattr(canvas_routes, "_get_db", lambda: db)
    monkeypatch.setattr(
        canvas_routes,
        "_get_canvas_service",
        lambda current_db: _RouteCanvasService(record),
    )
    monkeypatch.setattr(canvas_routes, "_get_viewer_service", lambda current_db: viewer)
    monkeypatch.setattr(canvas_routes, "require_thread_owner", owner)
    monkeypatch.setattr(canvas_routes, "_represent", represent)
    app = FastAPI()
    app.include_router(canvas_routes.router)
    client = TestClient(app)
    url = f"/api/persistent/threads/{record.thread_id}/canvases/main/view-attachments"

    no_cookie = client.post(
        url,
        headers={
            "If-Match": representation.etag,
            "Origin": "https://cockpit.platform.test",
            "Authorization": "Bearer ignored-for-viewer-credentials",
        },
    )
    assert no_cookie.status_code == 401
    assert viewer.calls == []

    parent = uuid4()
    hybrid_bearer = client.post(
        url,
        headers={
            "Cookie": f"srw_session={parent}",
            "If-Match": representation.etag,
            "Origin": "https://cockpit.platform.test",
            "Authorization": "Bearer not-allowed-for-viewer-credentials",
        },
    )
    assert hybrid_bearer.status_code == 401
    assert viewer.calls == []

    stale = client.post(
        url,
        headers={
            "Cookie": f"srw_session={parent}",
            "If-Match": '"canvas:stale"',
            "Origin": "https://cockpit.platform.test",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        },
    )
    assert stale.status_code == 412
    assert viewer.calls == []

    unsupported = client.post(
        url,
        headers={
            "Cookie": f"srw_session={parent}",
            "If-Match": representation.etag,
            "Origin": "https://cockpit.platform.test",
        },
    )
    assert unsupported.status_code == 409
    assert unsupported.json()["detail"]["code"] == "canvas_browser_unsupported"
    assert viewer.calls == []

    response = client.post(
        url,
        headers={
            "Cookie": f"srw_session={parent}",
            "If-Match": representation.etag,
            "Origin": "https://cockpit.platform.test",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["attachment_id"] == str(grant.attachment_id)
    assert payload["bootstrap_url"] == (
        f"{origin}/_canvas/bootstrap?attachment_id={grant.attachment_id}"
    )
    assert payload["bridge_nonce"] == "b" * 43
    assert "token=" not in payload["bootstrap_url"]
    assert payload["bridge_nonce"] not in payload["bootstrap_url"]
    assert "exchange_code" not in payload
    assert response.headers["cache-control"] == "private, no-store"
    assert len(viewer.calls) == 1
    assert viewer.calls[0]["parent_session_id"] == parent
    assert viewer.calls[0]["expected_record"] is record


def test_attachment_route_accepts_the_weakened_form_of_its_own_state_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compressing CDN is the only ETag the cockpit can ever echo back.

    Cloudflare rewrites ``ETag: "canvas:…"`` to ``W/"canvas:…"`` whenever it
    compresses the state response, so the browser stores — and returns in
    ``If-Match`` — the weak form of the exact state we authorized. The digest
    still names that representation; only the transfer encoding changed.
    """

    record = _app_record()
    representation = build_public_canvas_representation(
        record,
        status="ready",
        capabilities=CanvasCapabilities(can_create_viewer_session=True),
    )
    now = datetime.now(UTC)
    attachment_id = uuid4()
    origin = f"https://{record.origin_generation}.canvas.user-content.test"
    grant = CanvasViewerAttachmentGrant(
        attachment_id=attachment_id,
        origin=origin,
        bootstrap_url=f"{origin}/_canvas/bootstrap?attachment_id={attachment_id}",
        bridge_nonce="b" * 43,
        bootstrap_expires_at=now + timedelta(seconds=60),
        expires_at=now + timedelta(minutes=20),
        renew_after=now + timedelta(minutes=10),
    )
    viewer = _RouteViewerService(grant)
    db = SimpleNamespace()

    async def owner(request, current_db, thread_id):
        return {"id": "b4444444-4444-4444-4444-444444444444"}, {
            "id": thread_id,
            "user_id": "b4444444-4444-4444-4444-444444444444",
        }

    async def represent(*args, **kwargs):
        return representation

    monkeypatch.setattr(canvas_routes, "_get_db", lambda: db)
    monkeypatch.setattr(
        canvas_routes,
        "_get_canvas_service",
        lambda current_db: _RouteCanvasService(record),
    )
    monkeypatch.setattr(canvas_routes, "_get_viewer_service", lambda current_db: viewer)
    monkeypatch.setattr(canvas_routes, "require_thread_owner", owner)
    monkeypatch.setattr(canvas_routes, "_represent", represent)
    app = FastAPI()
    app.include_router(canvas_routes.router)
    client = TestClient(app)
    url = f"/api/persistent/threads/{record.thread_id}/canvases/main/view-attachments"
    headers = {
        "Cookie": f"srw_session={uuid4()}",
        "Origin": "https://cockpit.platform.test",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }

    weakened = client.post(
        url, headers={**headers, "If-Match": f"W/{representation.etag}"}
    )

    assert weakened.status_code == 200
    assert weakened.json()["attachment_id"] == str(grant.attachment_id)
    assert len(viewer.calls) == 1

    other_state = client.post(
        url,
        headers={**headers, "If-Match": 'W/"canvas:1:' + "0" * 64 + '"'},
    )

    assert other_state.status_code == 412
    assert other_state.json()["detail"]["code"] == "canvas_precondition_failed"
    assert len(viewer.calls) == 1


def test_authorize_route_requires_exact_bff_session_origin_and_closed_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _app_record()
    now = datetime.now(UTC)
    attachment_id = uuid4()
    parent = uuid4()
    origin = f"https://{record.origin_generation}.canvas.user-content.test"
    challenge = "c" * 43
    receipt = "r" * 43
    bridge = "b" * 43
    exchange = "e" * 43
    grant = CanvasViewerAttachmentGrant(
        attachment_id=attachment_id,
        origin=origin,
        bootstrap_url=f"{origin}/_canvas/bootstrap?attachment_id={attachment_id}",
        bridge_nonce=bridge,
        bootstrap_expires_at=now + timedelta(seconds=60),
        expires_at=now + timedelta(minutes=20),
        renew_after=now + timedelta(minutes=10),
    )
    authorization = CanvasBootstrapAuthorization(
        challenge=challenge,
        ready_receipt=receipt,
        exchange_code=exchange,
        expires_at=now + timedelta(seconds=30),
    )
    viewer = _RouteViewerService(
        grant,
        authorization=authorization,
        authorized_parent=parent,
    )
    db = SimpleNamespace()
    user_id = "b4444444-4444-4444-4444-444444444444"

    async def owner(request, current_db, thread_id):
        assert current_db is db
        return {"id": user_id}, {"id": thread_id, "user_id": user_id}

    monkeypatch.setattr(canvas_routes, "_get_db", lambda: db)
    monkeypatch.setattr(canvas_routes, "_get_viewer_service", lambda current_db: viewer)
    monkeypatch.setattr(canvas_routes, "require_thread_owner", owner)
    app = FastAPI()
    app.include_router(canvas_routes.router)
    client = TestClient(app)
    url = (
        f"/api/persistent/threads/{record.thread_id}/canvases/main/"
        f"view-attachments/{attachment_id}/authorize"
    )
    body = {
        "challenge": challenge,
        "ready_receipt": receipt,
        "bridge_nonce": bridge,
    }

    copied_to_other_bff = client.post(
        url,
        json=body,
        headers={
            "Cookie": f"srw_session={uuid4()}",
            "Origin": "https://cockpit.platform.test",
        },
    )
    assert copied_to_other_bff.status_code == 403
    assert copied_to_other_bff.json()["detail"]["code"] == "canvas_bootstrap_forbidden"

    calls_before_schema_rejection = len(viewer.authorization_calls)
    extra_field = client.post(
        url,
        json={**body, "origin": origin},
        headers={
            "Cookie": f"srw_session={parent}",
            "Origin": "https://cockpit.platform.test",
        },
    )
    assert extra_field.status_code == 422
    short_challenge = client.post(
        url,
        json={**body, "challenge": "short"},
        headers={
            "Cookie": f"srw_session={parent}",
            "Origin": "https://cockpit.platform.test",
        },
    )
    assert short_challenge.status_code == 422
    assert len(viewer.authorization_calls) == calls_before_schema_rejection

    hybrid = client.post(
        url,
        json=body,
        headers={
            "Cookie": f"srw_session={parent}",
            "Origin": "https://cockpit.platform.test",
            "Authorization": "Bearer must-not-authorize-canvas",
        },
    )
    assert hybrid.status_code == 401
    assert len(viewer.authorization_calls) == calls_before_schema_rejection

    response = client.post(
        url,
        json=body,
        headers={
            "Cookie": f"srw_session={parent}",
            "Origin": "https://cockpit.platform.test",
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    payload = response.json()
    assert set(payload) == {"challenge", "ready_receipt", "exchange_code", "expires_at"}
    assert payload["challenge"] == challenge
    assert payload["ready_receipt"] == receipt
    assert payload["exchange_code"] == exchange
    call = viewer.authorization_calls[-1]
    assert call == {
        "attachment_id": attachment_id,
        "user_id": user_id,
        "thread_id": record.thread_id,
        "parent_session_id": parent,
        "embedding_origin": "https://cockpit.platform.test",
        "challenge": challenge,
        "ready_receipt": receipt,
        "bridge_nonce": bridge,
    }
