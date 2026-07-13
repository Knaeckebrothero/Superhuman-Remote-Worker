"""Real-PostgreSQL lifecycle coverage for Dynamic Canvas viewer credentials.

The normal fast suite skips this module unless ``CANVAS_TEST_DATABASE_URL``
names a disposable, fully migrated application database. The migration CI job
sets that variable after applying the real migration chain.
"""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import asyncpg
import pytest

from services.canvas import CanvasService, WorkspaceAppSource
from services.canvas_ssh import RemoteWorkspaceTarget
from services.canvas_viewer_config import CanvasViewerConfig
from services.canvas_viewer_sessions import (
    CanvasBootstrapExchange,
    CanvasViewerError,
    CanvasViewerSessionService,
)
import services.canvas_viewer_sessions as viewer_sessions_module


_DATABASE_URL = os.getenv("CANVAS_TEST_DATABASE_URL", "").strip()
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


def _bootstrap_token(url: str) -> str:
    values = parse_qs(urlsplit(url).query).get("token") or []
    assert len(values) == 1
    return values[0]


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
                INSERT INTO threads (id, title, user_id, status, metadata)
                VALUES ($1, 'Canvas integration thread', $2, 'active', '{}'::jsonb)
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
                    $1, $2, $3, 'access', 'refresh', 'identity',
                    now() + interval '30 minutes', now() + interval '2 hours'
                )
                """,
                parent_id,
                user_id,
                f"canvas-integration-{user_id}",
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
        token = _bootstrap_token(first_grant.bootstrap_url)

        # Two replicas race the same one-time credential. PostgreSQL's locked
        # consumed-at transition admits exactly one and the loser observes the
        # committed state rather than creating a second origin session.
        raced = await asyncio.gather(
            first_replica.consume_bootstrap(
                token=token,
                host_generation=origin_generation,
                existing_session_secret=None,
            ),
            second_replica.consume_bootstrap(
                token=token,
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
        reused = await first_replica.consume_bootstrap(
            token=_bootstrap_token(second_grant.bootstrap_url),
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
            await conn.execute("DELETE FROM srw_sessions WHERE id = $1", parent_id)
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        await pool.close()
