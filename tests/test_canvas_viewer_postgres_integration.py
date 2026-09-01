"""Real-PostgreSQL lifecycle coverage for Dynamic Canvas viewer credentials.

The normal fast suite skips this module unless ``CANVAS_TEST_DATABASE_URL``
names a disposable, fully migrated application database. The migration CI job
sets that variable after applying the real migration chain.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import pytest

from services.canvas import CanvasService, WorkspaceAppSource
from services.canvas_ssh import RemoteWorkspaceTarget
from services.canvas_viewer_config import CanvasViewerConfig
from services.canvas_viewer_database import (
    CanvasViewerDatabasePrivilegeError,
    attest_canvas_viewer_database_privileges,
)
from services.canvas_viewer_sessions import (
    CanvasBootstrapExchange,
    CanvasViewerError,
    CanvasViewerSessionService,
)
import services.canvas_viewer_sessions as viewer_sessions_module


_DATABASE_URL = os.getenv("CANVAS_TEST_DATABASE_URL", "").strip()
_ROOT = Path(__file__).resolve().parents[1]
_GATEWAY_ROLE = "srw_canvas_gateway_ci"
_GATEWAY_PASSWORD = "canvas_gateway_ci_password_123"
_OWNER_ROLE = "srw_canvas_owner_ci"
_OWNER_PASSWORD = "canvas_owner_ci_password_123"
_GATEWAY_RELATIONS = (
    "users",
    "threads",
    "srw_sessions",
    "canvases",
    "canvas_view_attachments",
    "canvas_view_bootstraps",
    "canvas_origin_sessions",
)
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _DATABASE_URL,
        reason="CANVAS_TEST_DATABASE_URL must name a disposable migrated database",
    ),
]


class _PoolDB:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    def acquire(self):
        return self.pool.acquire()


def _config() -> CanvasViewerConfig:
    return CanvasViewerConfig(
        enabled=True,
        host_suffix=".canvas.user-content.test",
        cookie_mode="psl-isolated",
        deployment_profile="production",
        cockpit_origins=frozenset({"https://cockpit.platform.test"}),
        session_ttl_seconds=900,
        bootstrap_ttl_seconds=60,
        attachment_ttl_seconds=1200,
        revalidate_seconds=15,
    )


def _bootstrap_attachment_id(url: str, *, expected_origin: str) -> UUID:
    """Assert the bootstrap URL contains only its non-credential locator."""

    parsed = urlsplit(url)
    assert f"{parsed.scheme}://{parsed.netloc}" == expected_origin
    assert parsed.path == "/_canvas/bootstrap"
    assert parsed.fragment == ""
    assert parsed.username is None
    assert parsed.password is None
    values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    assert set(values) == {"attachment_id"}
    assert len(values["attachment_id"]) == 1
    attachment_id = UUID(values["attachment_id"][0])
    assert str(attachment_id) == values["attachment_id"][0]
    assert parsed.query == f"attachment_id={attachment_id}"
    assert "token" not in parsed.query
    return attachment_id


def _database_url_for(role: str, password: str) -> str:
    parsed = urlsplit(_DATABASE_URL)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{hostname}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def _role_database_url() -> str:
    return _database_url_for(_GATEWAY_ROLE, _GATEWAY_PASSWORD)


def _run_packaged_sql(database_url: str, filename: str) -> None:
    result = subprocess.run(
        [
            "psql",
            database_url,
            "--no-psqlrc",
            "--quiet",
            "--set",
            "ON_ERROR_STOP=1",
            "--file",
            str(_ROOT / "helm/files" / filename),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CANVAS_VIEWER_POSTGRES_USER": _GATEWAY_ROLE,
            "CANVAS_VIEWER_POSTGRES_PASSWORD": _GATEWAY_PASSWORD,
        },
    )
    assert result.returncode == 0, f"{filename}: {result.stderr}"


def _apply_packaged_role_script() -> None:
    """Run the chart's identity/target/owner phases in release order."""

    phases = (
        (_DATABASE_URL, "canvas-viewer-role.sql"),
        (_DATABASE_URL, "canvas-viewer-role-safety.sql"),
        (_role_database_url(), "canvas-viewer-self-configure.sql"),
        (_DATABASE_URL, "canvas-viewer-grants.sql"),
    )
    for database_url, filename in phases:
        _run_packaged_sql(database_url, filename)


async def _drop_gateway_test_role(conn: asyncpg.Connection) -> None:
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
        _GATEWAY_ROLE,
    )
    if exists:
        # The role name is a fixed test constant, never user input.
        await conn.execute(f"DROP OWNED BY {_GATEWAY_ROLE}")
        await conn.execute(f"DROP ROLE {_GATEWAY_ROLE}")


async def test_restricted_gateway_role_script_and_runtime_attestation() -> None:
    """Exercise the packaged grants and prove representative denials."""

    admin = await asyncpg.connect(_DATABASE_URL)
    restricted: asyncpg.Connection | None = None
    try:
        await _drop_gateway_test_role(admin)
        _apply_packaged_role_script()

        # Reconciliation must also remove a stale direct database CREATE grant
        # from a previous/operator-created contract before restoring CONNECT.
        database_identifier = await admin.fetchval(
            "SELECT quote_ident(current_database())"
        )
        await admin.execute(
            f"GRANT CREATE ON DATABASE {database_identifier} TO {_GATEWAY_ROLE}"
        )
        await admin.execute(f"GRANT CREATE ON SCHEMA public TO {_GATEWAY_ROLE}")
        _apply_packaged_role_script()

        await admin.execute(f"SET ROLE {_GATEWAY_ROLE}")
        try:
            with pytest.raises(
                CanvasViewerDatabasePrivilegeError,
                match="authenticated session role differs",
            ):
                await attest_canvas_viewer_database_privileges(admin)
        finally:
            await admin.execute("RESET ROLE")

        restricted = await asyncpg.connect(_role_database_url())
        await attest_canvas_viewer_database_privileges(restricted)
        await restricted.fetchval("SELECT metadata FROM threads LIMIT 1")

        forbidden_statements = (
            "SELECT access_token FROM srw_sessions LIMIT 1",
            "SELECT * FROM user_api_keys LIMIT 1",
            "UPDATE canvases SET updated_at = updated_at WHERE false",
            "DELETE FROM canvas_origin_sessions WHERE false",
            "SELECT nextval('thread_messages_seq_seq')",
            "CREATE TABLE public.canvas_gateway_forbidden(id integer)",
        )
        for statement in forbidden_statements:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await restricted.execute(statement)
    finally:
        if restricted is not None:
            await restricted.close()
        await _drop_gateway_test_role(admin)
        await admin.close()


async def test_cnpg_acl_path_needs_only_unprivileged_object_owner() -> None:
    """Prove the long-term ACL half has no hidden role-admin dependency."""

    admin = await asyncpg.connect(_DATABASE_URL)
    restricted: asyncpg.Connection | None = None
    owner_created = False
    try:
        await _drop_gateway_test_role(admin)
        await admin.execute(
            f"CREATE ROLE {_OWNER_ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB "
            f"NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS "
            f"PASSWORD '{_OWNER_PASSWORD}'"
        )
        owner_created = True
        await admin.execute(
            f"CREATE ROLE {_GATEWAY_ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB "
            f"NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
            f"PASSWORD '{_GATEWAY_PASSWORD}'"
        )
        database_identifier = await admin.fetchval(
            "SELECT quote_ident(current_database())"
        )
        await admin.execute(
            f"ALTER DATABASE {database_identifier} OWNER TO {_OWNER_ROLE}"
        )
        for relation in _GATEWAY_RELATIONS:
            await admin.execute(
                f"ALTER TABLE public.{relation} OWNER TO {_OWNER_ROLE}"
            )

        owner_url = _database_url_for(_OWNER_ROLE, _OWNER_PASSWORD)
        _run_packaged_sql(owner_url, "canvas-viewer-role-safety.sql")
        _run_packaged_sql(_role_database_url(), "canvas-viewer-self-configure.sql")
        _run_packaged_sql(owner_url, "canvas-viewer-grants.sql")

        restricted = await asyncpg.connect(_role_database_url())
        await attest_canvas_viewer_database_privileges(restricted)
        owner_is_unprivileged = await admin.fetchval(
            """
            SELECT NOT rolsuper AND NOT rolcreaterole AND NOT rolcreatedb
                   AND NOT rolreplication AND NOT rolbypassrls
            FROM pg_roles
            WHERE rolname = $1
            """,
            _OWNER_ROLE,
        )
        assert owner_is_unprivileged is True
    finally:
        if restricted is not None:
            await restricted.close()
        if owner_created:
            admin_identifier = await admin.fetchval("SELECT quote_ident(current_user)")
            await admin.execute(
                f"REASSIGN OWNED BY {_OWNER_ROLE} TO {admin_identifier}"
            )
            await admin.execute(f"DROP OWNED BY {_OWNER_ROLE}")
        await _drop_gateway_test_role(admin)
        if owner_created:
            await admin.execute(f"DROP ROLE {_OWNER_ROLE}")
        await admin.close()


async def test_postgres_viewer_lifecycle_concurrency_reuse_and_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = await asyncpg.create_pool(_DATABASE_URL, min_size=2, max_size=8)
    assert pool is not None
    db = _PoolDB(pool)
    first_replica = CanvasViewerSessionService(db, config=_config())
    second_replica = CanvasViewerSessionService(db, config=_config())
    user_id = uuid4()
    thread_id = uuid4()
    parent_id = uuid4()
    other_parent_id = uuid4()
    workspace_generation = uuid4()
    origin_generation = uuid4()
    listener = None

    source = WorkspaceAppSource(
        entry_port=8501,
        entry_path="/demo",
        workspace_generation=workspace_generation,
    )
    source_fingerprint = "sha256:" + "a" * 64
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (id, display_name, is_approved, approved_at)
                VALUES ($1, 'Canvas integration user', true, now())
                """,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO threads (
                    id, title, user_id, status, execution_lane, metadata
                ) VALUES (
                    $1, 'Canvas integration thread', $2, 'active',
                    'stateless', '{}'::jsonb
                )
                """,
                thread_id,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO srw_sessions (
                    id, user_id, kc_sub, access_token, refresh_token, id_token,
                    access_expires_at, absolute_expires_at
                ) VALUES (
                    $1, $3, $4, 'access', 'refresh', 'identity',
                    now() + interval '30 minutes', now() + interval '2 hours'
                ), (
                    $2, $3, $5, 'access', 'refresh', 'identity',
                    now() + interval '30 minutes', now() + interval '2 hours'
                )
                """,
                parent_id,
                other_parent_id,
                user_id,
                f"canvas-integration-{user_id}",
                f"canvas-integration-other-{user_id}",
            )
            await conn.execute(
                """
                INSERT INTO canvases (
                    thread_id, canvas_id, source, title, renderer, editable,
                    presentation_revision, source_fingerprint, origin_generation
                ) VALUES ($1, 'main', $2::jsonb, 'Integration app', 'auto', false,
                          1, $3, $4)
                """,
                thread_id,
                json.dumps(source.model_dump(mode="json")),
                source_fingerprint,
                origin_generation,
            )

        record = await CanvasService(db).get(str(thread_id))
        assert record is not None
        first_grant = await first_replica.create_attachment(
            user_id=str(user_id),
            thread_id=str(thread_id),
            parent_session_id=parent_id,
            embedding_origin="https://cockpit.platform.test",
            expected_record=record,
        )
        assert (
            _bootstrap_attachment_id(
                first_grant.bootstrap_url, expected_origin=first_grant.origin
            )
            == first_grant.attachment_id
        )
        assert first_grant.bootstrap_expires_at < first_grant.expires_at
        first_start = await first_replica.begin_bootstrap(
            attachment_id=first_grant.attachment_id,
            host_generation=origin_generation,
        )

        # Authorization is bound to the exact parent BFF session, user, and
        # thread. Failed probes must leave the one-time challenge available to
        # its rightful parent.
        for overrides, expected_status in (
            ({"parent_session_id": other_parent_id}, 403),
            ({"user_id": str(uuid4())}, 401),
            ({"thread_id": str(uuid4())}, 403),
        ):
            authorize_args = {
                "attachment_id": first_grant.attachment_id,
                "user_id": str(user_id),
                "thread_id": str(thread_id),
                "parent_session_id": parent_id,
                "embedding_origin": "https://cockpit.platform.test",
                "challenge": first_start.challenge,
                "ready_receipt": first_start.ready_receipt,
                "bridge_nonce": first_grant.bridge_nonce,
                **overrides,
            }
            with pytest.raises(CanvasViewerError) as forbidden:
                await second_replica.authorize_bootstrap(**authorize_args)
            assert forbidden.value.status_code == expected_status

        first_authorization = await first_replica.authorize_bootstrap(
            attachment_id=first_grant.attachment_id,
            user_id=str(user_id),
            thread_id=str(thread_id),
            parent_session_id=parent_id,
            embedding_origin="https://cockpit.platform.test",
            challenge=first_start.challenge,
            ready_receipt=first_start.ready_receipt,
            bridge_nonce=first_grant.bridge_nonce,
        )
        assert first_authorization.challenge == first_start.challenge
        assert first_authorization.ready_receipt == first_start.ready_receipt

        # Two replicas race the same browser-bound exchange. PostgreSQL's
        # locked consumed-at transition admits exactly one; the loser observes
        # committed state rather than creating a second origin session.
        raced = await asyncio.gather(
            first_replica.exchange_bootstrap(
                attachment_id=first_grant.attachment_id,
                challenge=first_start.challenge,
                exchange_code=first_authorization.exchange_code,
                browser_binding=first_start.browser_binding,
                host_generation=origin_generation,
                existing_session_secret=None,
            ),
            second_replica.exchange_bootstrap(
                attachment_id=first_grant.attachment_id,
                challenge=first_start.challenge,
                exchange_code=first_authorization.exchange_code,
                browser_binding=first_start.browser_binding,
                host_generation=origin_generation,
                existing_session_secret=None,
            ),
            return_exceptions=True,
        )
        exchanges = [
            value for value in raced if isinstance(value, CanvasBootstrapExchange)
        ]
        failures = [value for value in raced if isinstance(value, CanvasViewerError)]
        assert len(exchanges) == 1
        assert len(failures) == 1
        assert failures[0].status_code == 401
        exchange = exchanges[0]
        assert exchange.session_secret is not None

        target = RemoteWorkspaceTarget(
            thread_id=str(thread_id),
            generation=workspace_generation,
            host="workspace.integration.invalid",
            port=22,
            fingerprint="SHA256:" + "a" * 43,
        )
        monkeypatch.setattr(
            viewer_sessions_module,
            "resolve_remote_workspace_target",
            lambda thread, generation: target,
        )
        authenticated = await second_replica.authenticate(
            session_secret=exchange.session_secret,
            host_generation=origin_generation,
        )
        assert authenticated.id == exchange.session.id
        assert authenticated.remote_target == target

        # A second tab gets its own presence row/bootstrap but reuses the
        # already authenticated origin session and exact validated cookie.
        second_grant = await second_replica.create_attachment(
            user_id=str(user_id),
            thread_id=str(thread_id),
            parent_session_id=parent_id,
            embedding_origin="https://cockpit.platform.test",
            expected_record=record,
        )
        assert (
            _bootstrap_attachment_id(
                second_grant.bootstrap_url, expected_origin=second_grant.origin
            )
            == second_grant.attachment_id
        )
        second_start = await second_replica.begin_bootstrap(
            attachment_id=second_grant.attachment_id,
            host_generation=origin_generation,
        )
        second_authorization = await second_replica.authorize_bootstrap(
            attachment_id=second_grant.attachment_id,
            user_id=str(user_id),
            thread_id=str(thread_id),
            parent_session_id=parent_id,
            embedding_origin="https://cockpit.platform.test",
            challenge=second_start.challenge,
            ready_receipt=second_start.ready_receipt,
            bridge_nonce=second_grant.bridge_nonce,
        )
        reused = await first_replica.exchange_bootstrap(
            attachment_id=second_grant.attachment_id,
            challenge=second_start.challenge,
            exchange_code=second_authorization.exchange_code,
            browser_binding=second_start.browser_binding,
            host_generation=origin_generation,
            existing_session_secret=exchange.session_secret,
        )
        assert reused.session.id == exchange.session.id
        assert reused.session_secret == exchange.session_secret

        renewed = await second_replica.renew_attachment(
            attachment_id=first_grant.attachment_id,
            user_id=str(user_id),
            thread_id=str(thread_id),
            parent_session_id=parent_id,
        )
        assert renewed.renew_after < renewed.expires_at
        await first_replica.close_attachment(
            attachment_id=first_grant.attachment_id,
            user_id=str(user_id),
            thread_id=str(thread_id),
            parent_session_id=parent_id,
        )

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, closed_at, origin_session_id
                FROM canvas_view_attachments
                WHERE id = ANY($1::uuid[])
                ORDER BY id
                """,
                [first_grant.attachment_id, second_grant.attachment_id],
            )
            assert len(rows) == 2
            by_id = {UUID(str(row["id"])): row for row in rows}
            assert by_id[first_grant.attachment_id]["closed_at"] is not None
            assert by_id[second_grant.attachment_id]["closed_at"] is None
            assert (
                UUID(str(by_id[second_grant.attachment_id]["origin_session_id"]))
                == exchange.session.id
            )

        notification = asyncio.get_running_loop().create_future()
        listener = await asyncpg.connect(_DATABASE_URL)

        def on_change(connection, pid, channel, payload):
            del connection, pid, channel
            if not notification.done():
                notification.set_result(json.loads(payload))

        await listener.add_listener("canvas_session_changes", on_change)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE canvases SET origin_generation = $2 WHERE thread_id = $1",
                thread_id,
                uuid4(),
            )

        event = await asyncio.wait_for(notification, timeout=2)
        assert event == {
            "kind": "session",
            "id": str(exchange.session.id),
        }
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT revoked_at, revocation_reason
                FROM canvas_origin_sessions WHERE id = $1
                """,
                exchange.session.id,
            )
        assert row is not None
        assert row["revoked_at"] is not None
        assert row["revocation_reason"] == "origin_retired"
        with pytest.raises(CanvasViewerError) as revoked:
            await first_replica.authenticate(
                session_secret=exchange.session_secret,
                host_generation=origin_generation,
            )
        assert revoked.value.status_code == 401
    finally:
        if listener is not None:
            await listener.close()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM threads WHERE id = $1", thread_id)
            await conn.execute(
                "DELETE FROM srw_sessions WHERE id = ANY($1::uuid[])",
                [parent_id, other_parent_id],
            )
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        await pool.close()


async def test_gateway_role_completes_the_bootstrap_it_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the gateway-only methods under the identity the gateway holds.

    Every other test drives this service as the migration owner, which can do
    anything, so none of them can see what the packaged grants withhold. The
    public gateway process only ever authenticates as the restricted role, and
    a statement that role may not execute is a 500 on the browser's first hop
    into the isolated origin however correct the surrounding logic is.
    """

    admin = await asyncpg.connect(_DATABASE_URL)
    owner_pool: asyncpg.Pool | None = None
    gateway_pool: asyncpg.Pool | None = None
    user_id = uuid4()
    thread_id = uuid4()
    parent_id = uuid4()
    workspace_generation = uuid4()
    origin_generation = uuid4()
    source_fingerprint = "sha256:" + "b" * 64
    try:
        await _drop_gateway_test_role(admin)
        _apply_packaged_role_script()
        await admin.execute(
            """
            INSERT INTO users (id, display_name, is_approved, approved_at)
            VALUES ($1, 'Canvas gateway role user', true, now())
            """,
            user_id,
        )
        await admin.execute(
            """
            INSERT INTO threads (
                id, title, user_id, status, execution_lane, metadata
            ) VALUES (
                $1, 'Canvas gateway role thread', $2, 'active',
                'stateless', '{}'::jsonb
            )
            """,
            thread_id,
            user_id,
        )
        await admin.execute(
            """
            INSERT INTO srw_sessions (
                id, user_id, kc_sub, access_token, refresh_token, id_token,
                access_expires_at, absolute_expires_at
            ) VALUES (
                $1, $2, $3, 'access', 'refresh', 'identity',
                now() + interval '30 minutes', now() + interval '2 hours'
            )
            """,
            parent_id,
            user_id,
            f"canvas-gateway-role-{user_id}",
        )
        await admin.execute(
            """
            INSERT INTO canvases (
                thread_id, canvas_id, source, title, renderer, editable,
                presentation_revision, source_fingerprint, origin_generation
            ) VALUES ($1, 'main', $2::jsonb, 'Gateway role app', 'auto', false,
                      1, $3, $4)
            """,
            thread_id,
            json.dumps(
                WorkspaceAppSource(
                    entry_port=8501,
                    entry_path="/demo",
                    workspace_generation=workspace_generation,
                ).model_dump(mode="json")
            ),
            source_fingerprint,
            origin_generation,
        )

        owner_pool = await asyncpg.create_pool(_DATABASE_URL, min_size=1, max_size=4)
        gateway_pool = await asyncpg.create_pool(
            _role_database_url(), min_size=1, max_size=4
        )
        assert owner_pool is not None and gateway_pool is not None

        # The attestation the gateway runs at boot passes: the role holds every
        # column privilege the contract names. Whatever fails below therefore
        # fails on a statement no grant in that contract can carry.
        async with gateway_pool.acquire() as conn:
            await attest_canvas_viewer_database_privileges(conn)

        cockpit = CanvasViewerSessionService(_PoolDB(owner_pool), config=_config())
        gateway = CanvasViewerSessionService(_PoolDB(gateway_pool), config=_config())

        record = await CanvasService(_PoolDB(owner_pool)).get(str(thread_id))
        assert record is not None
        grant = await cockpit.create_attachment(
            user_id=str(user_id),
            thread_id=str(thread_id),
            parent_session_id=parent_id,
            embedding_origin="https://cockpit.platform.test",
            expected_record=record,
        )

        start = await gateway.begin_bootstrap(
            attachment_id=grant.attachment_id,
            host_generation=origin_generation,
        )
        authorization = await cockpit.authorize_bootstrap(
            attachment_id=grant.attachment_id,
            user_id=str(user_id),
            thread_id=str(thread_id),
            parent_session_id=parent_id,
            embedding_origin="https://cockpit.platform.test",
            challenge=start.challenge,
            ready_receipt=start.ready_receipt,
            bridge_nonce=grant.bridge_nonce,
        )
        exchange = await gateway.exchange_bootstrap(
            attachment_id=grant.attachment_id,
            challenge=start.challenge,
            exchange_code=authorization.exchange_code,
            browser_binding=start.browser_binding,
            host_generation=origin_generation,
            existing_session_secret=None,
        )
        assert exchange.session_secret is not None

        target = RemoteWorkspaceTarget(
            thread_id=str(thread_id),
            generation=workspace_generation,
            host="workspace.integration.invalid",
            port=22,
            fingerprint="SHA256:" + "b" * 43,
        )
        monkeypatch.setattr(
            viewer_sessions_module,
            "resolve_remote_workspace_target",
            lambda thread, generation: target,
        )
        authenticated = await gateway.authenticate(
            session_secret=exchange.session_secret,
            host_generation=origin_generation,
        )
        assert authenticated.id == exchange.session.id
        assert authenticated.remote_target == target

        # A second tab reuses the origin session, which locks it FOR UPDATE.
        # That lock is legal for this role precisely because the packaged
        # grants carry UPDATE columns on canvas_origin_sessions: the same
        # PostgreSQL rule that forbids locking the authoritative tables.
        second_grant = await cockpit.create_attachment(
            user_id=str(user_id),
            thread_id=str(thread_id),
            parent_session_id=parent_id,
            embedding_origin="https://cockpit.platform.test",
            expected_record=record,
        )
        second_start = await gateway.begin_bootstrap(
            attachment_id=second_grant.attachment_id,
            host_generation=origin_generation,
        )
        second_authorization = await cockpit.authorize_bootstrap(
            attachment_id=second_grant.attachment_id,
            user_id=str(user_id),
            thread_id=str(thread_id),
            parent_session_id=parent_id,
            embedding_origin="https://cockpit.platform.test",
            challenge=second_start.challenge,
            ready_receipt=second_start.ready_receipt,
            bridge_nonce=second_grant.bridge_nonce,
        )
        reused = await gateway.exchange_bootstrap(
            attachment_id=second_grant.attachment_id,
            challenge=second_start.challenge,
            exchange_code=second_authorization.exchange_code,
            browser_binding=second_start.browser_binding,
            host_generation=origin_generation,
            existing_session_secret=exchange.session_secret,
        )
        assert reused.session.id == exchange.session.id

        # Repointing the Canvas retires the origin: the gateway must be able to
        # write that verdict itself, not just refuse the exchange.
        with pytest.raises(CanvasViewerError) as stale:
            await gateway.authenticate(
                session_secret=exchange.session_secret,
                host_generation=uuid4(),
            )
        assert stale.value.status_code == 401
        revocation = await admin.fetchrow(
            "SELECT revoked_at, revocation_reason FROM canvas_origin_sessions "
            "WHERE id = $1",
            exchange.session.id,
        )
        assert revocation is not None
        assert revocation["revoked_at"] is not None
        assert revocation["revocation_reason"] == "origin_changed"
    finally:
        if gateway_pool is not None:
            await gateway_pool.close()
        if owner_pool is not None:
            await owner_pool.close()
        await admin.execute("DELETE FROM threads WHERE id = $1", thread_id)
        await admin.execute("DELETE FROM srw_sessions WHERE id = $1", parent_id)
        await admin.execute("DELETE FROM users WHERE id = $1", user_id)
        await _drop_gateway_test_role(admin)
        await admin.close()
