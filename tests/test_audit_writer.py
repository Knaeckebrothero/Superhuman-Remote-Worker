"""Real-Postgres tests for SyncAuditWriter (PR2).

Spins up an ephemeral PostgreSQL via testcontainers, applies the audit migration,
and exercises the writer against it. The writer's facade methods are synchronous
(they marshal onto a private loop on a daemon thread), so these tests are plain
sync functions; the async DB setup/readback runs via ``asyncio.run`` inside the
fixtures/helpers and is independent of the writer's own loop.

Skips cleanly without ``testcontainers[postgres]`` (requirements-dev.txt) or a
container runtime — see tests/test_audit_store.py for the same pattern.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest

pytest.importorskip("testcontainers.postgres")

from orchestrator.database.migrate import run_migrations  # noqa: E402
from orchestrator.database.postgres import MIGRATIONS_AUDIT_DIR  # noqa: E402
from src.database.audit_writer import SyncAuditWriter  # noqa: E402

AUDIT_IMAGE = "postgres:16"


def _swap_db(dsn: str, dbname: str) -> str:
    head, _, tail = dsn.rpartition("/")
    query = "?" + tail.split("?", 1)[1] if "?" in tail else ""
    return f"{head}/{dbname}{query}"


@pytest.fixture(scope="module")
def pg_dsn():
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(AUDIT_IMAGE)
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"no container runtime for testcontainers: {exc}")
    try:
        import re

        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


@pytest.fixture
def audit_dsn(pg_dsn):
    """A fresh, migrated database; yields its DSN. Dropped on teardown."""
    dbname = f"audit_w_{uuid4().hex[:12]}"

    async def setup():
        admin = await asyncpg.connect(pg_dsn)
        try:
            await admin.execute(f'CREATE DATABASE "{dbname}"')
        finally:
            await admin.close()
        pool = await asyncpg.create_pool(
            _swap_db(pg_dsn, dbname), min_size=1, max_size=2
        )
        try:
            await run_migrations(pool, MIGRATIONS_AUDIT_DIR)
        finally:
            await pool.close()

    async def teardown():
        admin = await asyncpg.connect(pg_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()

    asyncio.run(setup())
    yield _swap_db(pg_dsn, dbname)
    asyncio.run(teardown())


def _fetchrow(dsn, sql, *args):
    async def go():
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetchrow(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(go())


def _make_writer(dsn) -> SyncAuditWriter:
    w = SyncAuditWriter(dsn)
    assert w.ensure_ready() is True
    return w


def test_insert_llm_request_roundtrip(audit_dsn):
    w = _make_writer(audit_dsn)
    try:
        job = str(uuid4())
        rid = w.insert_llm_request(
            {
                "job_id": job,
                "agent_type": "universal",
                "call_type": "main",
                "model": "m",
                "iteration": 1,
                "timestamp": datetime.now(timezone.utc),
                "latency_ms": 5,
                "request": {
                    "messages": [{"type": "HumanMessage", "content": "hi"}],
                    "message_count": 1,
                },
                "response": {"type": "AIMessage", "content": "yo"},
                "metadata": None,
                "auxiliary_metadata": None,
                "metrics": {"token_usage": {"total_tokens": 7}},
            }
        )
        assert isinstance(rid, int) and rid >= 1
        row = _fetchrow(
            audit_dsn, "SELECT request, metrics FROM llm_requests WHERE id=$1", rid
        )
        import json

        req = (
            json.loads(row["request"])
            if isinstance(row["request"], str)
            else row["request"]
        )
        assert req["messages"][0]["content"] == "hi"
    finally:
        w.close()


def test_two_phase_audit_post(audit_dsn):
    w = _make_writer(audit_dsn)
    try:
        job = str(uuid4())
        pre = w.insert_audit_pre(
            {
                "job_id": job,
                "agent_type": "u",
                "iteration": 1,
                "step_type": "tool",
                "node_name": "tools",
                "phase": None,
                "phase_number": None,
                "timestamp": datetime.now(timezone.utc),
                "latency_ms": None,
                "payload": {"tool": {"name": "t"}},
                "metadata": None,
            }
        )
        assert isinstance(pre, int)
        # valid pre -> True; missing pre -> False; None pre -> False (no DB touch)
        assert w.insert_audit_post(pre, {"tool": {"success": True}}, 9, None) is True
        assert w.insert_audit_post(9_999_999, {"x": 1}, 1, None) is False
        assert w.insert_audit_post(None, {"x": 1}, 1, None) is False
        post = _fetchrow(
            audit_dsn,
            "SELECT job_id, pre_id FROM agent_audit WHERE pre_id=$1 AND event_phase='post'",
            pre,
        )
        assert post is not None and post["pre_id"] == pre
        assert str(post["job_id"]) == job  # derived from the pre row
    finally:
        w.close()


def test_thread_safety_from_worker_thread(audit_dsn):
    w = _make_writer(audit_dsn)
    try:
        job = str(uuid4())

        def write():
            return w.insert_audit_pre(
                {
                    "job_id": job,
                    "agent_type": "vision",
                    "iteration": 0,
                    "step_type": "llm",
                    "node_name": "execute",
                    "timestamp": datetime.now(timezone.utc),
                    "payload": {"llm": {"model": "v"}},
                    "metadata": None,
                }
            )

        with ThreadPoolExecutor(max_workers=2) as ex:
            ids = [f.result(timeout=20) for f in [ex.submit(write), ex.submit(write)]]
        assert all(isinstance(i, int) and i >= 1 for i in ids)
        assert ids[0] != ids[1]
    finally:
        w.close()


def test_from_env_unconfigured_returns_none(monkeypatch):
    for k in (
        "AUDIT_POSTGRES_USER",
        "AUDIT_POSTGRES_PASSWORD",
        "AUDIT_POSTGRES_HOST",
        "AUDIT_POSTGRES_DB",
        "AUDIT_DB_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    assert SyncAuditWriter.from_env() is None
