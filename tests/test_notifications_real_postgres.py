"""Unified notification feed against a real PostgreSQL (migration 0191).

Mocked ``conn.execute`` cannot catch what these are for: the idempotent
insert racing itself, the partial-unique claim index, keyset pagination with
tied timestamps, and the CHECK constraints. Guarded testcontainers fixture
(skips without a container runtime); applies ``schema_current.sql`` once, so
a stale snapshot fails here before it fails on k3d.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from orchestrator.security import crypto
from services.notification_catalog import notification_id
from services.notification_service import NotificationService

SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)

USER = str(uuid.uuid4())
OTHER = str(uuid.uuid4())


@pytest.fixture(scope="module")
def pg_dsn():
    try:
        container = PostgresContainer("postgres:15")
        container.start()
    except Exception as exc:
        pytest.skip(f"local PostgreSQL container unavailable: {exc}")
    try:
        yield container.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql"
        )
    finally:
        container.stop()


@pytest_asyncio.fixture(scope="module")
async def _schema_applied(pg_dsn):
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text())
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db(pg_dsn, _schema_applied, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "G" * 32)
    crypto.reset_cipher_cache()
    store = PostgresDB(connection_string=pg_dsn, min_connections=1, max_connections=8)
    await store.connect()
    async with store.acquire() as conn:
        await conn.execute("TRUNCATE notifications, notification_deliveries CASCADE")
    try:
        yield store
    finally:
        await store.close()
        crypto.reset_cipher_cache()


def _insert_kwargs(**overrides):
    kwargs = dict(
        recipient_kind="user",
        recipient_id=USER,
        category="review_queue",
        severity="normal",
        subject="Job abc completed — review required",
        body="body",
        source_kind="job",
        source_id="job-1",
        dedup_key="freeze_notification:cmd-1",
        actions=[{"type": "approve", "params": {"job_id": "job-1"}}],
        payload={"job_id": "job-1"},
    )
    kwargs.update(overrides)
    kwargs.setdefault(
        "notification_id",
        str(
            notification_id(
                kwargs["recipient_kind"], kwargs["recipient_id"], kwargs["dedup_key"]
            )
        ),
    )
    return kwargs


async def _count(db, sql, *args):
    async with db.acquire() as conn:
        return await conn.fetchval(sql, *args)


class TestInsertOnce:
    @pytest.mark.asyncio
    async def test_twenty_concurrent_inserts_yield_one_row_and_one_winner(self, db):
        results = await asyncio.gather(
            *[db.insert_notification_once(**_insert_kwargs()) for _ in range(20)]
        )
        assert results.count(True) == 1
        assert results.count(False) == 19
        assert await _count(db, "SELECT count(*) FROM notifications") == 1
        assert (
            await _count(
                db,
                "SELECT count(*) FROM notification_deliveries WHERE channel='in_app' AND state='sent'",
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_same_key_two_recipients_two_rows(self, db):
        assert await db.insert_notification_once(**_insert_kwargs()) is True
        assert (
            await db.insert_notification_once(**_insert_kwargs(recipient_id=OTHER))
            is True
        )
        assert await _count(db, "SELECT count(*) FROM notifications") == 2

    @pytest.mark.asyncio
    async def test_replay_with_a_different_identity_raises(self, db):
        await db.insert_notification_once(**_insert_kwargs())
        with pytest.raises(RuntimeError, match="different payload"):
            await db.insert_notification_once(**_insert_kwargs(category="incident"))
        with pytest.raises(RuntimeError, match="different payload"):
            await db.insert_notification_once(**_insert_kwargs(source_id="job-2"))
        # Subject/body drift on replay is tolerated — identity is the key.
        assert (
            await db.insert_notification_once(**_insert_kwargs(subject="edited"))
            is False
        )

    @pytest.mark.asyncio
    async def test_constraints_hold(self, db):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.insert_notification_once(**_insert_kwargs(severity="urgent"))
        with pytest.raises(asyncpg.CheckViolationError):
            await db.insert_notification_once(
                **_insert_kwargs(source_kind="job", source_id=None)
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await db.insert_notification_once(**_insert_kwargs(dedup_key=""))


class TestDeliveryClaims:
    @pytest.mark.asyncio
    async def test_concurrent_claims_one_wins_and_failed_frees_the_slot(self, db):
        kwargs = _insert_kwargs()
        await db.insert_notification_once(**kwargs)
        nid = kwargs["notification_id"]
        claims = await asyncio.gather(
            *[
                db.claim_notification_delivery(
                    notification_id=nid, channel="email", recipient_address="a@b"
                )
                for _ in range(10)
            ]
        )
        won = [c for c in claims if c]
        assert len(won) == 1
        # A pending claim still holds the slot.
        assert (
            await db.claim_notification_delivery(notification_id=nid, channel="email")
            is None
        )
        assert await db.settle_notification_delivery(won[0], state="failed", error="x")
        # Settling twice is a no-op (the CAS on state='pending').
        assert not await db.settle_notification_delivery(won[0], state="sent")
        second = await db.claim_notification_delivery(
            notification_id=nid, channel="email"
        )
        assert second and second != won[0]
        assert await db.settle_notification_delivery(
            second, state="sent", provider_msg_id="<m@x>"
        )
        # A sent claim holds the slot for good.
        assert (
            await db.claim_notification_delivery(notification_id=nid, channel="email")
            is None
        )
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT attempt, state, provider_msg_id FROM notification_deliveries "
                "WHERE notification_id=$1 AND channel='email' ORDER BY attempt",
                uuid.UUID(nid),
            )
        assert [(r["attempt"], r["state"]) for r in rows] == [
            (1, "failed"),
            (2, "sent"),
        ]
        assert rows[1]["provider_msg_id"] == "<m@x>"

    @pytest.mark.asyncio
    async def test_channels_are_independent_slots(self, db):
        kwargs = _insert_kwargs()
        await db.insert_notification_once(**kwargs)
        nid = kwargs["notification_id"]
        assert await db.claim_notification_delivery(
            notification_id=nid, channel="email"
        )
        assert await db.claim_notification_delivery(notification_id=nid, channel="ntfy")


class TestFeed:
    async def _seed(self, db, n):
        ids = []
        for i in range(n):
            kwargs = _insert_kwargs(dedup_key=f"k{i}", source_id=f"job-{i}")
            await db.insert_notification_once(**kwargs)
            ids.append(kwargs["notification_id"])
        return ids

    @pytest.mark.asyncio
    async def test_keyset_pagination_with_tied_timestamps(self, db):
        ids = await self._seed(db, 7)
        # Force ties: every row gets the same created_at.
        async with db.acquire() as conn:
            await conn.execute("UPDATE notifications SET created_at = now()")
        seen: list[str] = []
        before = None
        pages = 0
        while True:
            rows, before = await db.list_notifications_page(
                recipient_kind="user", recipient_id=USER, before=before, limit=3
            )
            seen.extend(str(r["id"]) for r in rows)
            pages += 1
            if before is None:
                break
        assert pages == 3
        assert sorted(seen) == sorted(ids)
        assert len(seen) == len(set(seen))

    @pytest.mark.asyncio
    async def test_status_and_category_filters(self, db):
        await self._seed(db, 3)
        await db.insert_notification_once(
            **_insert_kwargs(dedup_key="inc", category="incident", severity="critical")
        )
        rows, _ = await db.list_notifications_page(
            recipient_kind="user", recipient_id=USER, categories=["incident"]
        )
        assert [r["category"] for r in rows] == ["incident"]
        await db.resolve_notifications_by_source(
            source_kind="job", source_id="job-0", resolved_by="user:x"
        )
        pending, _ = await db.list_notifications_page(
            recipient_kind="user", recipient_id=USER, status="pending"
        )
        assert len(pending) == 3
        resolved, _ = await db.list_notifications_page(
            recipient_kind="user", recipient_id=USER, status="resolved"
        )
        assert [r["source_id"] for r in resolved] == ["job-0"]
        with pytest.raises(ValueError):
            await db.list_notifications_page(
                recipient_kind="user", recipient_id=USER, status="bogus"
            )

    @pytest.mark.asyncio
    async def test_engagement_and_counts(self, db):
        ids = await self._seed(db, 3)
        counts = await db.get_notification_counts(
            recipient_kind="user", recipient_id=USER
        )
        assert (counts["unseen"], counts["unread"], counts["pending"]) == (3, 3, 3)
        assert counts["by_category"]["review_queue"] == {"pending": 3, "unseen": 3}

        stamped = await db.mark_notifications_seen(
            recipient_kind="user", recipient_id=USER, ids=ids[:2]
        )
        assert sorted(str(s["id"]) for s in stamped) == sorted(ids[:2])
        # Idempotent, never regresses, ignores foreign ids.
        again = await db.mark_notifications_seen(
            recipient_kind="user", recipient_id=USER, ids=ids[:2]
        )
        assert again == []
        foreign = await db.mark_notifications_seen(
            recipient_kind="user", recipient_id=OTHER, ids=ids
        )
        assert foreign == []

        # read implies seen (the CHECK), even when the row was never seen.
        row = await db.mark_notification_read_v2(
            recipient_kind="user", recipient_id=USER, notification_id=ids[2]
        )
        assert row["read_at"] is not None and row["seen_at"] is not None
        counts = await db.get_notification_counts(
            recipient_kind="user", recipient_id=USER
        )
        assert (counts["unseen"], counts["unread"]) == (0, 2)

        row = await db.stamp_notification_interacted(ids[0])
        assert row["interacted_at"] and row["read_at"] and row["seen_at"]

        archived = await db.archive_notification(
            recipient_kind="user", recipient_id=USER, notification_id=ids[0]
        )
        assert archived["archived_at"] is not None
        rows, _ = await db.list_notifications_page(
            recipient_kind="user", recipient_id=USER
        )
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_resolve_by_source_settles_every_recipient_once(self, db):
        await db.insert_notification_once(**_insert_kwargs())
        await db.insert_notification_once(**_insert_kwargs(recipient_id=OTHER))
        first = await db.resolve_notifications_by_source(
            source_kind="job", source_id="job-1", resolved_by="officer:t1"
        )
        assert len(first) == 2
        assert {r["resolved_by"] for r in first} == {"officer:t1"}
        second = await db.resolve_notifications_by_source(
            source_kind="job", source_id="job-1", resolved_by="user:u"
        )
        assert second == []


class TestRecordReplay:
    """The completion journal replays a callback if the process dies between
    the send and the effect mark. record() twice with one command id must
    produce one row, one SSE frame and one email."""

    def _service(self, db, feed):
        svc = NotificationService()
        svc.connect(db=db, email_service=MagicMock(), notification_feed=feed)
        svc._email_service.send_notification_email = AsyncMock(
            return_value=(True, "<one@srw>")
        )
        svc._get_user = AsyncMock(
            return_value={"id": USER, "email": "legate@example.org"}
        )
        return svc

    @pytest.mark.asyncio
    async def test_effect_replay_sends_once(self, db):
        feed = MagicMock()
        svc = self._service(db, feed)

        async def effect_callback():
            return await svc.record(
                recipient_id=USER,
                category="review_queue",
                dedup_key="freeze_notification:cmd-9",
                subject="s",
                body="b",
                source_kind="job",
                source_id="job-9",
                action_params={"job_id": "job-9"},
            )

        first = await effect_callback()
        second = await effect_callback()  # the replay
        assert first.inserted is True and second.inserted is False
        assert first.notification_id == second.notification_id
        assert first.deliveries["email"] is True
        assert second.deliveries["email"] == "already_delivered"
        assert svc._email_service.send_notification_email.await_count == 1
        frames = [c.kwargs["event_type"] for c in feed.broadcast.call_args_list]
        assert frames.count("notification") == 1
        assert await _count(db, "SELECT count(*) FROM notifications") == 1
        assert (
            await _count(
                db,
                "SELECT count(*) FROM notification_deliveries WHERE channel='email' AND state='sent'",
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_failed_send_is_retried_by_the_replay(self, db):
        feed = MagicMock()
        svc = self._service(db, feed)
        svc._email_service.send_notification_email = AsyncMock(
            side_effect=[OSError("smtp down"), (True, "<two@srw>")]
        )
        kwargs = dict(
            recipient_id=USER,
            category="incident",
            dedup_key="llm_give_up_operator_alert:cmd-3",
            subject="s",
            body="b",
            source_kind="job",
            source_id="job-3",
        )
        first = await svc.record(**kwargs)
        assert first.deliveries["email"] is False
        second = await svc.record(**kwargs)
        assert second.inserted is False and second.deliveries["email"] is True
        async with db.acquire() as conn:
            states = await conn.fetch(
                "SELECT attempt, state FROM notification_deliveries "
                "WHERE channel='email' ORDER BY attempt"
            )
        assert [(r["attempt"], r["state"]) for r in states] == [
            (1, "failed"),
            (2, "sent"),
        ]
        assert feed.broadcast.call_count == 1
