"""Real-Postgres parity tests for AuditStore (PR3).

Seeds rows via SyncAuditWriter, reads them back through AuditStore, and asserts
the load-bearing translations: the pre/post logical-row stitch, global
step_number preserved under FilterCategory, the single-query version, and
list_llm_requests surfacing token_usage + the status/call_type filters.

Skips cleanly without testcontainers[postgres] or a container runtime.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest

pytest.importorskip("testcontainers.postgres")

from orchestrator.database.audit_store import AuditStore  # noqa: E402
from orchestrator.database.migrate import run_migrations  # noqa: E402
from orchestrator.database.postgres import MIGRATIONS_AUDIT_DIR  # noqa: E402
from src.database.audit_writer import SyncAuditWriter  # noqa: E402

pytestmark = pytest.mark.asyncio
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
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


def _seed(dsn: str) -> dict:
    """Seed a known fixture set via the real writer. Returns ids/job."""
    now = datetime.now(timezone.utc)
    iso = now.isoformat().replace("+00:00", "Z")
    job = str(uuid4())
    w = SyncAuditWriter(dsn)
    rid_main = w.insert_llm_request(
        {
            "job_id": job,
            "agent_type": "universal",
            "call_type": "main",
            "model": "gpt",
            "iteration": 2,
            "timestamp": now,
            "latency_ms": 11,
            "request": {"messages": [], "message_count": 0},
            "response": {"content": "yo", "tool_calls": [{"name": "read_file"}]},
            "metadata": None,
            "auxiliary_metadata": None,
            "metrics": {"token_usage": {"total_tokens": 9}},
        }
    )
    w.insert_llm_request(
        {
            "job_id": job,
            "agent_type": "memory",
            "call_type": "auxiliary",
            "model": "aux",
            "iteration": None,
            "timestamp": now,
            "latency_ms": 1,
            "request": {"messages": [], "message_count": 0},
            "response": {},
            "metadata": {
                "status": "error",
                "error": {"type": "TimeoutError", "message": "boom"},
            },
            "auxiliary_metadata": None,
            "metrics": {"token_usage": {}},
        }
    )
    # tool pre/post (stitch target)
    tpre = w.insert_audit_pre(
        {
            "job_id": job,
            "agent_type": "u",
            "iteration": 2,
            "step_type": "tool",
            "node_name": "tools",
            "phase": "tactical",
            "phase_number": 2,
            "timestamp": now,
            "latency_ms": None,
            "payload": {
                "tool": {"name": "read_file", "arguments": {"path": "/x"}},
                "started_at": iso,
            },
            "metadata": None,
        }
    )
    w.insert_audit_post(
        tpre,
        {"tool": {"result_preview": "done", "success": True}, "completed_at": iso},
        40,
        None,
    )
    # llm pre (no post)
    w.insert_audit_pre(
        {
            "job_id": job,
            "agent_type": "u",
            "iteration": 2,
            "step_type": "llm",
            "node_name": "execute",
            "phase": None,
            "phase_number": None,
            "timestamp": now,
            "latency_ms": None,
            "payload": {"llm": {"model": "gpt"}},
            "metadata": None,
        }
    )
    # error step
    w.insert_audit_pre(
        {
            "job_id": job,
            "agent_type": "u",
            "iteration": 2,
            "step_type": "error",
            "node_name": "execute",
            "phase": None,
            "phase_number": None,
            "timestamp": now,
            "latency_ms": None,
            "payload": {"error": {"message": "x"}},
            "metadata": None,
        }
    )
    w.close()
    return {"job": job, "rid_main": rid_main}


@pytest.fixture
def seeded(pg_dsn):
    dbname = f"audit_r_{uuid4().hex[:12]}"

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
    dsn = _swap_db(pg_dsn, dbname)
    info = _seed(dsn)
    info["dsn"] = dsn
    yield info
    asyncio.run(teardown())


async def _store(dsn) -> AuditStore:
    s = AuditStore(dsn)
    await s.connect()
    assert s.is_available
    return s


async def test_logical_row_stitch(seeded):
    s = await _store(seeded["dsn"])
    try:
        res = await s.get_job_audit(seeded["job"])
        assert res["total"] == 3  # 3 pre rows (tool, llm, error); post is not counted
        tool = next(
            e for e in res["entries"] if e.get("tool", {}).get("name") == "read_file"
        )
        # pre fields + post delta merged into one logical row
        assert tool["tool"]["name"] == "read_file"
        assert tool["tool"]["result_preview"] == "done"
        assert tool["tool"]["success"] is True
        assert tool["latency_ms"] == 40  # coalesced from post
        assert tool["started_at"].endswith("Z")
    finally:
        await s.disconnect()


async def test_step_number_global_under_filter(seeded):
    s = await _store(seeded["dsn"])
    try:
        full = await s.get_job_audit(seeded["job"])
        assert [e["step_number"] for e in full["entries"]] == [1, 2, 3]
        # Filtering must NOT renumber: the tool row keeps its global step_number 1.
        tools = await s.get_job_audit(seeded["job"], filter_category="tools")
        assert [e["step_number"] for e in tools["entries"]] == [1]
        errors = await s.get_job_audit(seeded["job"], filter_category="errors")
        assert [e["step_number"] for e in errors["entries"]] == [3]
    finally:
        await s.disconnect()


async def test_version_counts_logical(seeded):
    s = await _store(seeded["dsn"])
    try:
        ver = await s.get_job_version(seeded["job"])
        assert ver["auditEntryCount"] == 3  # pre rows only
        assert ver["graphDeltaCount"] == 0
        assert isinstance(ver["version"], int)
        # Empty job -> None.
        assert await s.get_job_version(str(uuid4())) is None
    finally:
        await s.disconnect()


async def test_list_llm_requests_token_usage_and_status(seeded):
    s = await _store(seeded["dsn"])
    try:
        lst = await s.list_llm_requests(seeded["job"])
        assert lst["total"] == 2
        main = next(e for e in lst["entries"] if e["call_type"] == "main")
        assert main["token_usage"] == {"total_tokens": 9}  # surfaced from metrics
        assert main["tool_calls"] == [{"name": "read_file"}]
        # status filter reads from metadata (where archive_error folds it).
        errs = await s.list_llm_requests(seeded["job"], status="error")
        assert errs["total"] == 1
        assert errs["entries"][0]["error"]["type"] == "TimeoutError"
    finally:
        await s.disconnect()


async def test_get_request_and_non_uuid(seeded):
    s = await _store(seeded["dsn"])
    try:
        req = await s.get_request(seeded["rid_main"])
        assert req["id"] == seeded["rid_main"] and req["call_type"] == "main"
        assert await s.get_request("not-a-number") is None  # str route param parity
        assert await s.get_request(999999) is None
        # Non-UUID job_id must degrade to empty, never 500.
        bad = await s.get_job_audit("not-a-uuid")
        assert bad["total"] == 0 and bad["entries"] == []
    finally:
        await s.disconnect()
