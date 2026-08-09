"""delete_datasource writes a tombstone and scrubs thread references.

The normal fast suite skips this module unless ``DRIFT_TEST_DATABASE_URL``
names a disposable, fully migrated application database — mirroring the
convention in ``tests/test_canvas_viewer_postgres_integration.py``. A mocked
connection would validate nothing about the SQL itself (untyped asyncpg
parameters, jsonb_set on NULL, atomicity), which is the entire risk here.
See docs/features/session_config_drift_resume.md §5.1 and §6.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
import pytest_asyncio

from orchestrator.database.postgres import PostgresDB


_DATABASE_URL = os.getenv("DRIFT_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _DATABASE_URL,
        reason="DRIFT_TEST_DATABASE_URL must name a disposable migrated database",
    ),
]


@pytest_asyncio.fixture
async def db():
    database = PostgresDB(connection_string=_DATABASE_URL)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _make_datasource(database: PostgresDB, *, label: str) -> str:
    row = await database.create_datasource(
        name=f"{label}-{uuid4().hex[:8]}", ds_type="generic"
    )
    return str(row["id"])


async def _make_thread(database: PostgresDB, *, datasource_ids: list[str]) -> str:
    thread_id = uuid4()
    async with database.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO threads (id, title, status, metadata)
            VALUES ($1, 'drift cleanup test thread', 'active', $2::jsonb)
            """,
            thread_id,
            json.dumps({"datasource_ids": datasource_ids}),
        )
    return str(thread_id)


async def test_delete_writes_tombstone_and_scrubs_thread_references(db):
    """The deleted id is named in a tombstone and scrubbed from live threads."""
    survivor_id = await _make_datasource(db, label="Survivor")
    doomed_name = f"KurortEngine-{uuid4().hex[:8]}"
    doomed_row = await db.create_datasource(name=doomed_name, ds_type="generic")
    doomed_id = str(doomed_row["id"])

    thread_id = await _make_thread(
        db, datasource_ids=[doomed_id, survivor_id]
    )

    try:
        assert await db.delete_datasource(doomed_id) is True

        # A second delete of the same (now-gone) id is a no-op, not an error.
        assert await db.delete_datasource(doomed_id) is False

        tombstones = await db.get_datasource_tombstones([doomed_id, survivor_id])
        assert tombstones == {doomed_id: doomed_name}

        row = await db.get_thread(thread_id)
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        assert metadata["datasource_ids"] == [survivor_id]
    finally:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM threads WHERE id = $1", thread_id)
        await db.delete_datasource(survivor_id)


async def test_delete_scoped_path_tombstones_and_scrubs_inside_existing_txn(db):
    """The scoped path (``authority_project_scope_id`` set) already opens a
    transaction via ``_transaction_if``. The delete/tombstone/scrub trio must
    still run inside it, not be silently skipped by the complementary
    ``_transaction_if(conn, authority_scope_uuid is None)`` guard that exists
    only to avoid a *redundant* transaction on this branch.
    """
    project = await db.create_project(name=f"drift-test-{uuid4().hex[:8]}")
    project_id = str(project["id"])
    other_id = await _make_datasource(db, label="Other")
    doomed_name = f"ScopedDoomed-{uuid4().hex[:8]}"
    doomed_row = await db.create_datasource(
        name=doomed_name,
        ds_type="generic",
        scope_mode="projects",
        project_ids=[project_id],
    )
    doomed_id = str(doomed_row["id"])
    thread_id = await _make_thread(db, datasource_ids=[doomed_id, other_id])

    try:
        deleted = await db.delete_datasource(
            doomed_id, authority_project_scope_id=project_id
        )
        assert deleted is True

        tombstones = await db.get_datasource_tombstones([doomed_id])
        assert tombstones == {doomed_id: doomed_name}

        row = await db.get_thread(thread_id)
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        assert metadata["datasource_ids"] == [other_id]
    finally:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM threads WHERE id = $1", thread_id)
        await db.delete_datasource(other_id)


async def test_delete_leaves_non_array_and_unrelated_metadata_untouched(db):
    """The scrub UPDATE's WHERE clause must not touch rows it shouldn't.

    A thread with no ``datasource_ids`` key, and one where the key is not a
    JSON array, must be left exactly as they were — this is what guards the
    ``jsonb_set(NULL, ...) -> NULL`` hazard: the WHERE clause is the only
    thing standing between a missing/non-array key and the whole metadata
    column being nulled out.
    """
    doomed_id = await _make_datasource(db, label="Doomed")
    bare_thread_id = uuid4()
    scalar_thread_id = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO threads (id, title, status, metadata)
            VALUES ($1, 'no datasource_ids key', 'active', '{}'::jsonb)
            """,
            bare_thread_id,
        )
        await conn.execute(
            """
            INSERT INTO threads (id, title, status, metadata)
            VALUES ($1, 'non-array datasource_ids', 'active', $2::jsonb)
            """,
            scalar_thread_id,
            json.dumps({"datasource_ids": doomed_id}),
        )

    try:
        assert await db.delete_datasource(doomed_id) is True

        bare_row = await db.get_thread(str(bare_thread_id))
        bare_metadata = bare_row["metadata"]
        if isinstance(bare_metadata, str):
            bare_metadata = json.loads(bare_metadata)
        assert bare_metadata == {}

        scalar_row = await db.get_thread(str(scalar_thread_id))
        scalar_metadata = scalar_row["metadata"]
        if isinstance(scalar_metadata, str):
            scalar_metadata = json.loads(scalar_metadata)
        assert scalar_metadata == {"datasource_ids": doomed_id}
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "DELETE FROM threads WHERE id = ANY($1::uuid[])",
                [bare_thread_id, scalar_thread_id],
            )


async def test_get_datasource_tombstones_empty_and_invalid_ids(db):
    assert await db.get_datasource_tombstones([]) == {}
    assert await db.get_datasource_tombstones(["not-a-uuid"]) == {}
