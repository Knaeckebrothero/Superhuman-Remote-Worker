from __future__ import annotations

import asyncio
import json
from pathlib import Path
from datetime import UTC, datetime, timedelta
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
    "CANVAS_VIEWER_RAW_PATH_VERIFIED",
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
    monkeypatch.setenv("CANVAS_VIEWER_HOST_SUFFIX", ".canvas.user-content.test")
    monkeypatch.setenv("CANVAS_VIEWER_COCKPIT_ORIGINS", "https://cockpit.platform.test")
    monkeypatch.setenv("CANVAS_VIEWER_RAW_PATH_VERIFIED", "true")
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
    host = f"{generation}.canvas.user-content.test"

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
        "not-a-uuid.canvas.user-content.test",
        "00000000-0000-0000-0000-000000000000.evil.canvas.user-content.test",
        "00000000-0000-0000-0000-000000000000.canvas.user-content.test:443",
        "00000000-0000-0000-0000-000000000000.canvas.user-content.test.evil",
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
    bootstrap = hash_canvas_viewer_secret("bootstrap", secret)
    session = hash_canvas_viewer_secret("session", secret)
    assert bootstrap != session
    assert secret not in bootstrap
    assert len(bootstrap) == 64
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
    migration = Path(
        "orchestrator/database/migrations/app/0061_canvas_viewer_sessions.sql"
    ).read_text()
    for table in (
        "canvas_origin_sessions",
        "canvas_view_attachments",
        "canvas_view_bootstraps",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migration
    assert "session_secret_hash" in migration
    assert "token_hash" in migration
    assert "session_secret TEXT" not in migration
    assert "bootstrap_token" not in migration
    assert "ck_canvas_attachment_cookie_mode" in migration
    assert "trg_canvas_revoke_retired_origin" in migration
    assert "trg_canvas_revoke_bff_session" in migration
    assert "trg_canvas_revoke_user_admission" in migration
    assert "canvas_session_changes" in migration


class _RouteCanvasService:
    def __init__(self, record: CanvasRecord):
        self.record = record

    async def get(self, thread_id: str) -> CanvasRecord:
        assert thread_id == self.record.thread_id
        return self.record


class _RouteViewerService:
    def __init__(self, grant: CanvasViewerAttachmentGrant):
        self.grant = grant
        self.calls: list[dict[str, object]] = []

    async def create_attachment(self, **kwargs):
        self.calls.append(kwargs)
        return self.grant


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


class _BootstrapPolicyConnection:
    def __init__(self, row: dict[str, object]):
        self.row = row

    def transaction(self):
        return _AsyncValueContext(self)

    async def fetchrow(self, query: str, *args):
        assert "FROM canvas_view_bootstraps" in query
        return self.row


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
    generation = uuid4()
    now = datetime.now(UTC)
    connection = _BootstrapPolicyConnection(
        {
            "bootstrap_id": uuid4(),
            "attachment_id": uuid4(),
            "expected_presentation_revision": 1,
            "source_fingerprint": "sha256:" + "a" * 64,
            "workspace_generation": uuid4(),
            "origin_generation": generation,
            "bootstrap_expires_at": now + timedelta(seconds=30),
            "user_id": uuid4(),
            "thread_id": uuid4(),
            "canvas_id": "main",
            "parent_srw_session_id": uuid4(),
            "embedding_origin": stored_origin,
            "attachment_cookie_mode": stored_mode,
            "attachment_expires_at": now + timedelta(minutes=10),
            "closed_at": None,
        }
    )
    service = CanvasViewerSessionService(
        _ViewerPolicyDB(connection),
        config=_viewer_config(origins=frozenset({"https://cockpit.platform.test"})),
    )

    with pytest.raises(CanvasViewerError) as rejected:
        await service.consume_bootstrap(
            token="b" * 43,
            host_generation=generation,
            existing_session_secret=None,
        )

    assert rejected.value.status_code == 401
    assert rejected.value.code == "canvas_bootstrap_invalid"


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
    grant = CanvasViewerAttachmentGrant(
        attachment_id=uuid4(),
        origin=f"https://{record.origin_generation}.canvas.user-content.test",
        bootstrap_url=(
            f"https://{record.origin_generation}.canvas.user-content.test/"
            "_canvas/bootstrap?token=secret"
        ),
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
        },
    )
    assert stale.status_code == 412
    assert viewer.calls == []

    response = client.post(
        url,
        headers={
            "Cookie": f"srw_session={parent}",
            "If-Match": representation.etag,
            "Origin": "https://cockpit.platform.test",
        },
    )
    assert response.status_code == 200
    assert response.json()["attachment_id"] == str(grant.attachment_id)
    assert response.headers["cache-control"] == "private, no-store"
    assert len(viewer.calls) == 1
    assert viewer.calls[0]["parent_session_id"] == parent
    assert viewer.calls[0]["expected_record"] is record
