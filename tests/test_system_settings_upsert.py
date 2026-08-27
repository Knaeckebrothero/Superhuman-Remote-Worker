"""Regression tests for ``PostgresDB.upsert_system_setting`` actor coercion.

The ``system_settings.updated_by`` column is ``text``. ``PUT
/api/admin/system-settings/main_cloud`` used to pass the admin id straight
through as a ``UUID`` object, which asyncpg rejects ("expected str, got UUID")
— a 500 that broke the admin main-cloud settings page. The helper now coerces
any non-None ``updated_by`` to ``str`` so every caller is safe.

TestBed-free: ``upsert_system_setting`` only touches ``self.acquire()`` and the
static ``_row_to_dict``, so we build the instance via ``__new__`` and swap in a
fake connection that records the query args.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

from database.postgres import PostgresDB  # noqa: E402


class _FakeAcquire:
    """Minimal async context manager standing in for ``pool.acquire()``."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _db_with_conn(conn) -> PostgresDB:
    # Skip __init__ (no pool/env needed); upsert_system_setting only uses
    # self.acquire() + the static _row_to_dict.
    db = PostgresDB.__new__(PostgresDB)
    db.acquire = lambda: _FakeAcquire(conn)
    return db


def _fake_conn():
    conn = MagicMock()
    # RETURNING row isn't needed for these assertions; None → helper returns {}.
    conn.fetchrow = AsyncMock(return_value=None)
    return conn


class TestUpsertSystemSettingUpdatedBy:
    @pytest.mark.asyncio
    async def test_uuid_updated_by_coerced_to_str(self):
        conn = _fake_conn()
        db = _db_with_conn(conn)
        uid = UUID("d32df192-77e7-4c5b-8c1d-9f7ace423b08")

        await db.upsert_system_setting(
            "main_cloud", {"backend_id": "nextcloud"}, updated_by=uid
        )

        updated_by_arg = conn.fetchrow.await_args.args[4]
        assert isinstance(updated_by_arg, str)
        assert updated_by_arg == str(uid)

    @pytest.mark.asyncio
    async def test_none_updated_by_stays_none(self):
        conn = _fake_conn()
        db = _db_with_conn(conn)

        await db.upsert_system_setting("vm_workspaces", {"enabled": True})

        assert conn.fetchrow.await_args.args[4] is None

    @pytest.mark.asyncio
    async def test_str_updated_by_unchanged(self):
        conn = _fake_conn()
        db = _db_with_conn(conn)

        await db.upsert_system_setting("k", {"a": 1}, updated_by="admin@example.test")

        assert conn.fetchrow.await_args.args[4] == "admin@example.test"

    @pytest.mark.asyncio
    async def test_value_is_json_serialized(self):
        conn = _fake_conn()
        db = _db_with_conn(conn)

        await db.upsert_system_setting("k", {"backend_id": "nextcloud"}, updated_by="x")

        # arg[2] is the json-dumped value passed to $2::jsonb.
        assert conn.fetchrow.await_args.args[2] == json.dumps(
            {"backend_id": "nextcloud"}
        )
