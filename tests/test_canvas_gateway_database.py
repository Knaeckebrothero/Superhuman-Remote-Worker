from __future__ import annotations

from collections import deque
from contextlib import asynccontextmanager

import pytest

import orchestrator.canvas_gateway as canvas_gateway_module
from orchestrator.canvas_gateway import CanvasGatewayApp
from orchestrator.services.canvas_session_notifications import CanvasConnectionRegistry
from orchestrator.services.canvas_viewer_config import CanvasViewerConfig
from orchestrator.services.canvas_viewer_database import (
    CanvasViewerDatabaseConfigurationError,
    CanvasViewerDatabasePrivilegeError,
    attest_canvas_viewer_database_privileges,
)
import orchestrator.services.canvas_viewer_database as viewer_database_module


_VIEWER_DATABASE_ENV = (
    "CANVAS_VIEWER_POSTGRES_USER",
    "CANVAS_VIEWER_POSTGRES_PASSWORD",
    "CANVAS_VIEWER_POSTGRES_HOST",
    "CANVAS_VIEWER_POSTGRES_PORT",
    "CANVAS_VIEWER_POSTGRES_DB",
    "CANVAS_VIEWER_POSTGRES_MIN_CONNECTIONS",
    "CANVAS_VIEWER_POSTGRES_MAX_CONNECTIONS",
)


def _clear_viewer_database_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _VIEWER_DATABASE_ENV:
        monkeypatch.delenv(name, raising=False)


def _viewer_config(*, enabled: bool = True) -> CanvasViewerConfig:
    return CanvasViewerConfig(
        enabled=enabled,
        host_suffix=".canvas.user-content.test",
        cookie_mode="psl-isolated",
        deployment_profile="production",
        cockpit_origins=frozenset({"https://cockpit.platform.test"}),
        session_ttl_seconds=900,
        bootstrap_ttl_seconds=60,
        attachment_ttl_seconds=1200,
        revalidate_seconds=15,
    )


def test_viewer_database_rejects_shared_application_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_viewer_database_env(monkeypatch)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://broad-application-role:secret@db/srw"
    )
    monkeypatch.setenv("POSTGRES_USER", "broad-application-role")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    with pytest.raises(
        CanvasViewerDatabaseConfigurationError,
        match="CANVAS_VIEWER_POSTGRES_USER is required",
    ):
        viewer_database_module.create_canvas_viewer_database()


def test_viewer_database_uses_only_explicit_identity_and_small_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_viewer_database_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://must:not@be-used/shared")
    monkeypatch.setenv("POSTGRES_USER", "must-not-be-used")
    monkeypatch.setenv("POSTGRES_PASSWORD", "must-not-be-used")
    monkeypatch.setenv("CANVAS_VIEWER_POSTGRES_USER", "canvas viewer")
    monkeypatch.setenv("CANVAS_VIEWER_POSTGRES_PASSWORD", "p@ss/word")
    monkeypatch.setenv("CANVAS_VIEWER_POSTGRES_HOST", "viewer-db.internal")
    monkeypatch.setenv("CANVAS_VIEWER_POSTGRES_PORT", "5544")
    monkeypatch.setenv("CANVAS_VIEWER_POSTGRES_DB", "canvas db")
    captured: dict[str, object] = {}

    class _ConstructedDatabase:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(viewer_database_module, "PostgresDB", _ConstructedDatabase)

    database = viewer_database_module.create_canvas_viewer_database()

    assert isinstance(database, _ConstructedDatabase)
    assert captured == {
        "connection_string": (
            "postgresql://canvas%20viewer:p%40ss%2Fword@"
            "viewer-db.internal:5544/canvas%20db"
        ),
        "min_connections": 1,
        "max_connections": 4,
        "command_timeout": 30.0,
        "env_prefix": "CANVAS_VIEWER_POSTGRES",
        "default_min_connections": 1,
        "default_max_connections": 4,
        "server_settings": {"search_path": "pg_catalog, public, pg_temp"},
    }


def test_viewer_database_rejects_invalid_pool_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_viewer_database_env(monkeypatch)
    for name, value in {
        "CANVAS_VIEWER_POSTGRES_USER": "canvas-viewer",
        "CANVAS_VIEWER_POSTGRES_PASSWORD": "secret",
        "CANVAS_VIEWER_POSTGRES_HOST": "viewer-db.internal",
        "CANVAS_VIEWER_POSTGRES_PORT": "5432",
        "CANVAS_VIEWER_POSTGRES_DB": "srw",
        "CANVAS_VIEWER_POSTGRES_MIN_CONNECTIONS": "4",
        "CANVAS_VIEWER_POSTGRES_MAX_CONNECTIONS": "2",
    }.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(
        CanvasViewerDatabaseConfigurationError,
        match="MAX_CONNECTIONS must be greater than or equal to",
    ):
        viewer_database_module.create_canvas_viewer_database()


class _PrivilegeConnection:
    def __init__(
        self,
        *,
        identity: dict[str, object] | None = None,
        missing_required: set[tuple[str, str, str]] | None = None,
        dangerous: set[tuple[str, str]] | None = None,
        extra_columns: set[tuple[str, str, str]] | None = None,
        unrelated_dangerous: set[tuple[str, str]] | None = None,
        dangerous_sequences: set[tuple[str, str]] | None = None,
        omitted_forbidden: set[tuple[str, str]] | None = None,
    ) -> None:
        self.identity = {
            "role_name": "canvas-viewer",
            "session_role_name": "canvas-viewer",
            "session_role_matches": True,
            "search_path_safe": True,
            "database_connect": True,
            "database_create": False,
            "public_schema_usage": True,
            "public_schema_create": False,
            "elevated_role": False,
            "direct_role_membership": False,
            **(identity or {}),
        }
        self.missing_required = missing_required or set()
        self.dangerous = dangerous or set()
        self.extra_columns = extra_columns or set()
        self.unrelated_dangerous = unrelated_dangerous or set()
        self.dangerous_sequences = dangerous_sequences or set()
        self.omitted_forbidden = omitted_forbidden or set()
        self.identity_checks = 0
        self.required_contract: set[tuple[str, str, str]] = set()
        self.forbidden_contract: set[tuple[str, str]] = set()
        self.required_query = ""
        self.forbidden_query = ""
        self.allowed_column_query = ""
        self.unrelated_query = ""
        self.sequence_query = ""
        self.allowed_relations: set[str] = set()

    async def fetchrow(self, query: str) -> dict[str, object]:
        assert "pg_catalog.pg_roles" in query
        assert "rolbypassrls" in query
        assert "session_user" in query
        assert "current_setting('search_path')" in query
        self.identity_checks += 1
        return self.identity

    async def fetch(self, query: str, *args: list[str]) -> list[dict[str, object]]:
        if len(args) == 1:
            (privileges,) = args
            self.sequence_query = query
            assert set(privileges) == {"USAGE", "SELECT", "UPDATE"}
            return [
                {"sequence_name": sequence_name, "privilege": privilege}
                for sequence_name, privilege in self.dangerous_sequences
            ]
        if len(args) == 3:
            tables, columns, privileges = args
            self.required_query = query
            self.required_contract = set(zip(tables, columns, privileges, strict=True))
            return [
                {
                    "table_name": table_name,
                    "column_name": column_name,
                    "privilege": privilege,
                    "granted": requirement not in self.missing_required,
                }
                for requirement in self.required_contract
                for table_name, column_name, privilege in (requirement,)
            ]
        assert len(args) == 2
        tables, privileges = args
        if "attribute.attnum > 0" in query:
            self.allowed_column_query = query
            return [
                {
                    "table_name": table_name,
                    "column_name": column_name,
                    "privilege": privilege,
                }
                for table_name, column_name, privilege in (
                    self.required_contract | self.extra_columns
                )
            ]
        if "NOT relation.relname" in query:
            self.unrelated_query = query
            self.allowed_relations = set(tables)
            return [
                {"table_name": table_name, "privilege": privilege}
                for table_name, privilege in self.unrelated_dangerous
            ]
        self.forbidden_query = query
        self.forbidden_contract = set(zip(tables, privileges, strict=True))
        return [
            {
                "table_name": table_name,
                "privilege": privilege,
                "granted": requirement in self.dangerous,
            }
            for requirement in self.forbidden_contract
            if requirement not in self.omitted_forbidden
            for table_name, privilege in (requirement,)
        ]


class _SchemaConnection(_PrivilegeConnection):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.schema_checks = 0

    async def fetchval(self, query: str, columns: list[str]) -> bool:
        assert "information_schema.columns" in query
        assert set(columns) == {
            "authorized_at",
            "browser_binding_hash",
            "challenge_hash",
            "exchange_token_hash",
            "ready_receipt_hash",
        }
        self.schema_checks += 1
        return True


class _LifecycleDatabase:
    def __init__(self, connection: _SchemaConnection | None = None) -> None:
        self.connection = connection or _SchemaConnection()
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class _Listener:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _TransportPool:
    def __init__(self) -> None:
        self.closed = False

    async def close_all(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_privilege_attestation_accepts_only_the_gateway_contract() -> None:
    connection = _PrivilegeConnection()

    await attest_canvas_viewer_database_privileges(connection)

    assert connection.identity_checks == 1
    assert ("users", "id", "SELECT") in connection.required_contract
    assert ("threads", "metadata", "SELECT") in connection.required_contract
    assert (
        "canvas_origin_sessions",
        "revocation_reason",
        "UPDATE",
    ) in connection.required_contract
    assert ("users", "DELETE") in connection.forbidden_contract
    assert (
        "canvas_origin_sessions",
        "DELETE",
    ) in connection.forbidden_contract
    assert "user_api_keys" not in connection.allowed_relations
    assert "has_column_privilege" in connection.required_query
    assert "has_table_privilege" in connection.forbidden_query
    assert "has_any_column_privilege" in connection.forbidden_query
    assert "has_column_privilege" in connection.allowed_column_query
    assert "has_any_column_privilege" in connection.unrelated_query
    assert "has_sequence_privilege" in connection.sequence_query


@pytest.mark.asyncio
async def test_privilege_attestation_rejects_a_missing_required_column_grant() -> None:
    connection = _PrivilegeConnection(
        missing_required={("threads", "metadata", "SELECT")}
    )

    with pytest.raises(
        CanvasViewerDatabasePrivilegeError,
        match=r"missing required: SELECT public\.threads\(metadata\)",
    ):
        await attest_canvas_viewer_database_privileges(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ({"elevated_role": True}, "superuser-like role"),
        ({"direct_role_membership": True}, "direct role membership"),
        (
            {
                "session_role_name": "broad-application-role",
                "session_role_matches": False,
            },
            "authenticated session role differs",
        ),
        ({"database_create": True}, "CREATE current_database"),
        ({"public_schema_create": True}, "CREATE public schema"),
        ({"search_path_safe": False}, "search_path differs"),
    ],
)
async def test_privilege_attestation_rejects_elevated_identity_capabilities(
    identity: dict[str, object], expected: str
) -> None:
    connection = _PrivilegeConnection(identity=identity)

    with pytest.raises(CanvasViewerDatabasePrivilegeError, match=expected):
        await attest_canvas_viewer_database_privileges(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_column",
    [
        ("srw_sessions", "access_token", "SELECT"),
        ("canvas_view_attachments", "id", "INSERT"),
        ("canvas_view_attachments", "bridge_nonce_hash", "UPDATE"),
    ],
)
async def test_privilege_attestation_rejects_extra_allowlisted_column_privileges(
    extra_column: tuple[str, str, str],
) -> None:
    connection = _PrivilegeConnection(extra_columns={extra_column})

    with pytest.raises(
        CanvasViewerDatabasePrivilegeError,
        match=(f"{extra_column[2]} public\\.{extra_column[0]}\\({extra_column[1]}\\)"),
    ):
        await attest_canvas_viewer_database_privileges(connection)


@pytest.mark.asyncio
async def test_privilege_attestation_rejects_forbidden_relation_privilege() -> None:
    connection = _PrivilegeConnection(dangerous={("canvas_view_bootstraps", "DELETE")})

    with pytest.raises(
        CanvasViewerDatabasePrivilegeError,
        match=r"DELETE public\.canvas_view_bootstraps",
    ):
        await attest_canvas_viewer_database_privileges(connection)


@pytest.mark.asyncio
async def test_privilege_attestation_rejects_any_unrelated_relation_access() -> None:
    connection = _PrivilegeConnection(unrelated_dangerous={("user_api_keys", "SELECT")})

    with pytest.raises(
        CanvasViewerDatabasePrivilegeError,
        match=r"SELECT public\.user_api_keys",
    ):
        await attest_canvas_viewer_database_privileges(connection)


@pytest.mark.asyncio
async def test_privilege_attestation_rejects_any_sequence_access() -> None:
    connection = _PrivilegeConnection(
        dangerous_sequences={("thread_messages_seq_seq", "USAGE")}
    )

    with pytest.raises(
        CanvasViewerDatabasePrivilegeError,
        match=r"USAGE public\.thread_messages_seq_seq sequence",
    ):
        await attest_canvas_viewer_database_privileges(connection)


@pytest.mark.asyncio
async def test_privilege_attestation_fails_closed_on_incomplete_results() -> None:
    connection = _PrivilegeConnection(
        omitted_forbidden={("canvas_origin_sessions", "TRIGGER")}
    )

    with pytest.raises(
        CanvasViewerDatabasePrivilegeError,
        match="attestation result for TRIGGER public.canvas_origin_sessions",
    ):
        await attest_canvas_viewer_database_privileges(connection)


@pytest.mark.asyncio
async def test_gateway_constructs_dedicated_database_only_during_enabled_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _LifecycleDatabase()
    listener = _Listener()
    transport = _TransportPool()
    factory_calls = 0

    def factory() -> _LifecycleDatabase:
        nonlocal factory_calls
        factory_calls += 1
        return database

    monkeypatch.setattr(
        canvas_gateway_module,
        "CanvasSessionNotificationListener",
        lambda db, registry: listener,
    )
    app = CanvasGatewayApp(
        database_factory=factory,
        config=_viewer_config(),
        session_service=object(),  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
        transport_pool=transport,  # type: ignore[arg-type]
    )
    assert factory_calls == 0

    incoming = deque(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    outgoing: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return incoming.popleft()

    async def send(message: dict[str, object]) -> None:
        outgoing.append(message)

    await app({"type": "lifespan"}, receive, send)

    assert outgoing == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]
    assert factory_calls == 1
    assert database.connect_calls == 1
    assert database.connection.identity_checks == 1
    assert database.connection.required_contract
    assert database.connection.forbidden_contract
    assert database.connection.schema_checks == 1
    assert database.disconnect_calls == 1
    assert listener.started is True
    assert listener.stopped is True
    assert transport.closed is True


@pytest.mark.asyncio
async def test_gateway_startup_fails_before_schema_or_listener_for_broad_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _SchemaConnection(extra_columns={("users", "display_name", "UPDATE")})
    database = _LifecycleDatabase(connection)
    listener_constructed = False

    def listener_factory(*args: object) -> _Listener:
        nonlocal listener_constructed
        listener_constructed = True
        return _Listener()

    monkeypatch.setattr(
        canvas_gateway_module,
        "CanvasSessionNotificationListener",
        listener_factory,
    )
    app = CanvasGatewayApp(
        database_factory=lambda: database,
        config=_viewer_config(),
        session_service=object(),  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    outgoing: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "lifespan.startup"}

    async def send(message: dict[str, object]) -> None:
        outgoing.append(message)

    await app({"type": "lifespan"}, receive, send)

    assert outgoing[0]["type"] == "lifespan.startup.failed"
    assert "UPDATE public.users(display_name)" in str(outgoing[0]["message"])
    assert connection.identity_checks == 1
    assert connection.schema_checks == 0
    assert listener_constructed is False


@pytest.mark.asyncio
async def test_disabled_gateway_does_not_construct_database() -> None:
    factory_calls = 0

    def factory() -> _LifecycleDatabase:
        nonlocal factory_calls
        factory_calls += 1
        return _LifecycleDatabase()

    app = CanvasGatewayApp(
        database_factory=factory,
        config=_viewer_config(enabled=False),
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    outgoing: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "lifespan.startup"}

    async def send(message: dict[str, object]) -> None:
        outgoing.append(message)

    await app({"type": "lifespan"}, receive, send)

    assert factory_calls == 0
    assert outgoing[0]["type"] == "lifespan.startup.failed"
