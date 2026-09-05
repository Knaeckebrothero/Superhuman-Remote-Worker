"""Contacts facade contracts against an owned, fully migrated PostgreSQL.

These tests need no ambient database URL. Testcontainers owns the database;
the contention barriers observe PostgreSQL locks rather than elapsed time.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.migrate import run_migrations
from orchestrator.database.postgres import PostgresDB

MIGRATIONS = (
    Path(__file__).resolve().parents[1] / "src/orchestrator/database/migrations/app"
)
pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as postgres:
        yield postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture(scope="module")
async def migrated_dsn(pg_dsn):
    async with asyncpg.create_pool(pg_dsn, min_size=1, max_size=2) as pool:
        await run_migrations(pool, MIGRATIONS)
    return pg_dsn


@asynccontextmanager
async def _database(dsn, *, max_connections=3):
    database = PostgresDB(
        connection_string=dsn,
        min_connections=1,
        max_connections=max_connections,
        command_timeout=10,
    )
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def db(migrated_dsn):
    async with _database(migrated_dsn) as database:
        yield database


@pytest_asyncio.fixture
async def seeded(db):
    owner, member, project = uuid4(), uuid4(), uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, display_name) "
            "VALUES ($1, $2, 'Owner'), ($3, $4, 'Member')",
            owner,
            f"{owner}@contacts.test",
            member,
            f"{member}@contacts.test",
        )
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, 'Contacts characterization')",
            project,
        )
        await conn.execute(
            "INSERT INTO project_members (project_id, user_id, role) "
            "VALUES ($1, $2, 'owner'), ($1, $3, 'editor')",
            project,
            owner,
            member,
        )
    return {"owner": owner, "member": member, "project": project}


async def test_update_collision_rolls_back_primary_and_consent_changes(db, seeded):
    contact = await db.create_contact(seeded["owner"], "Collision")
    primary = await db.add_contact_address(
        contact["id"], seeded["owner"], "whatsapp", "+491700000001"
    )
    secondary = await db.add_contact_address(
        contact["id"], seeded["owner"], "whatsapp", "+491700000002"
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE contact_addresses SET opt_in_status='opted_in', "
            "last_inbound_at='2026-01-02T03:04:05Z' WHERE id=$1",
            secondary["id"],
        )
    before = await db.get_contact_address(secondary["id"])

    assert (
        await db.update_contact_address(
            secondary["id"], address=primary["address"], is_primary=True
        )
        is None
    )

    assert await db.get_contact_address(primary["id"]) == primary
    assert await db.get_contact_address(secondary["id"]) == before
    assert before["is_primary"] is False
    assert before["opt_in_status"] == "opted_in"
    assert before["last_inbound_at"] is not None


async def test_outer_scope_rolls_back_contact_address_and_link_with_one_connection(
    migrated_dsn, seeded
):
    class AbortTransaction(Exception):
        pass

    async with _database(migrated_dsn, max_connections=1) as database:
        # A repository bypassing acquire() would wait forever for the sole
        # connection; keep the original task identity with asyncio.timeout().
        async with asyncio.timeout(5):
            with pytest.raises(AbortTransaction):
                async with database.transaction_scope():
                    contact = await database.create_contact(
                        seeded["owner"], "Rolled back", "outer transaction"
                    )
                    address = await database.add_contact_address(
                        contact["id"],
                        seeded["owner"],
                        "email",
                        "rollback@contacts.test",
                    )
                    assert await database.link_contact_to_project(
                        seeded["project"], contact["id"], seeded["owner"]
                    )
                    nested = await database.get_contact(contact["id"])
                    assert nested["addresses"][0]["id"] == str(address["id"])
                    assert nested["projects"][0]["id"] == str(seeded["project"])
                    assert await database.get_project_contacts(seeded["project"]) == [
                        nested
                    ]
                    raise AbortTransaction

        observer = await asyncpg.connect(migrated_dsn)
        try:
            assert (
                await observer.fetchval(
                    "SELECT count(*) FROM contacts WHERE id=$1", contact["id"]
                )
                == 0
            )
            assert (
                await observer.fetchval(
                    "SELECT count(*) FROM contact_addresses WHERE contact_id=$1",
                    contact["id"],
                )
                == 0
            )
            assert (
                await observer.fetchval(
                    "SELECT count(*) FROM project_contacts WHERE contact_id=$1",
                    contact["id"],
                )
                == 0
            )
        finally:
            await observer.close()
        assert await database.get_contact(contact["id"]) is None


async def test_duplicate_savepoint_preserves_outer_commit_and_connection(db, seeded):
    contact = await db.create_contact(seeded["owner"], "Savepoint", "before")
    primary = await db.add_contact_address(
        contact["id"], seeded["owner"], "email", "savepoint@contacts.test"
    )

    async with db.transaction_scope() as outer:
        await db.update_contact(contact["id"], notes="outer commit")
        async with db.transaction_scope() as nested:
            assert nested is outer
            assert (
                await db.add_contact_address(
                    contact["id"],
                    seeded["owner"],
                    "email",
                    primary["address"],
                    is_primary=True,
                )
                is None
            )
            assert await nested.fetchval("SELECT 1") == 1
            assert await db.link_contact_to_project(
                seeded["project"], contact["id"], seeded["owner"]
            )
        assert (await db.get_contact(contact["id"]))["notes"] == "outer commit"

    committed = await db.get_contact(contact["id"])
    assert committed["notes"] == "outer commit"
    assert committed["addresses"][0]["id"] == str(primary["id"])
    assert committed["addresses"][0]["is_primary"] is True
    assert committed["projects"][0]["id"] == str(seeded["project"])


async def _wait_for_blocked_inserts(observer, pids, table):
    """Release only after both real statements have reached the lock barrier."""
    async with asyncio.timeout(5):
        while True:
            blocked = await observer.fetch(
                "SELECT pid FROM pg_stat_activity WHERE pid = ANY($1::int[]) "
                "AND wait_event_type = 'Lock' AND cardinality(pg_blocking_pids(pid)) > 0 "
                "AND query ILIKE $2",
                pids,
                f"%INSERT INTO {table}%",
            )
            if {row["pid"] for row in blocked} == set(pids):
                return


async def _contend_after_blocker_rollback(db, dsn, table, insert_blocker, mutate):
    """Run two facade writers behind one uncommitted unique-index entry."""
    observer = await asyncpg.connect(dsn)
    started = asyncio.Queue()
    tasks = []

    async def writer(index):
        async with db.transaction_scope() as conn:
            await started.put(await conn.fetchval("SELECT pg_backend_pid()"))
            result = await mutate(index)
            # A caught UniqueViolationError must leave the caller transaction
            # usable; otherwise the outer commit could silently roll it back.
            assert await conn.fetchval("SELECT 1") == 1
            return result

    try:
        async with db.acquire() as blocker:
            transaction = blocker.transaction()
            await transaction.start()
            try:
                await insert_blocker(blocker)
                tasks = [asyncio.create_task(writer(index)) for index in range(2)]
                async with asyncio.timeout(5):
                    pids = [await started.get(), await started.get()]
                assert len(set(pids)) == 2
                await _wait_for_blocked_inserts(observer, pids, table)
            finally:
                await transaction.rollback()
        async with asyncio.timeout(5):
            return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await observer.close()


async def test_concurrent_first_addresses_return_one_primary_and_one_none(
    db, migrated_dsn, seeded
):
    contact = await db.create_contact(seeded["owner"], "First primary")

    async def insert_blocker(conn):
        # Invisible to either SELECT EXISTS, but its unique-index entry blocks
        # both INSERTs after each has selected itself as the first primary.
        await conn.execute(
            "INSERT INTO contact_addresses "
            "(contact_id, owner_user_id, channel, address, is_primary) "
            "VALUES ($1, $2, 'email', 'blocker@contacts.test', true)",
            contact["id"],
            seeded["owner"],
        )

    async def mutate(index):
        return await db.add_contact_address(
            contact["id"], seeded["owner"], "email", f"writer{index}@contacts.test"
        )

    results = await _contend_after_blocker_rollback(
        db, migrated_dsn, "contact_addresses", insert_blocker, mutate
    )

    assert sum(result is None for result in results) == 1
    winner = next(result for result in results if result is not None)
    assert winner["is_primary"] is True
    assert winner["address"] in {"writer0@contacts.test", "writer1@contacts.test"}
    addresses = (await db.get_contact(contact["id"]))["addresses"]
    assert len(addresses) == 1
    assert addresses[0]["id"] == str(winner["id"])
    assert addresses[0]["is_primary"] is True


async def test_concurrent_identical_links_keep_one_row_and_cascade(
    db, migrated_dsn, seeded
):
    contact = await db.create_contact(seeded["owner"], "Shared contact")
    address = await db.add_contact_address(
        contact["id"], seeded["owner"], "email", "shared@contacts.test"
    )
    assert await db.user_can_see_contact(seeded["member"], contact["id"]) is False

    async def insert_blocker(conn):
        await conn.execute(
            "INSERT INTO project_contacts (project_id, contact_id, added_by) "
            "VALUES ($1, $2, $3)",
            seeded["project"],
            contact["id"],
            seeded["owner"],
        )

    async def mutate(_index):
        return await db.link_contact_to_project(
            seeded["project"], contact["id"], seeded["owner"]
        )

    results = await _contend_after_blocker_rollback(
        db, migrated_dsn, "project_contacts", insert_blocker, mutate
    )
    assert sorted(results) == [False, True]

    linked = await db.get_project_contacts(seeded["project"])
    assert len(linked) == 1
    assert isinstance(linked[0]["id"], UUID)
    assert linked[0]["id"] == contact["id"]
    assert linked[0]["addresses"][0]["id"] == str(address["id"])
    assert linked[0]["projects"] == [
        {"id": str(seeded["project"]), "name": "Contacts characterization"}
    ]
    assert await db.user_can_see_contact(seeded["member"], contact["id"]) is True
    assert await db.delete_contact(contact["id"]) is True
    assert await db.delete_contact(contact["id"]) is False
    assert await db.get_contact_address(address["id"]) is None
    assert await db.get_project_contacts(seeded["project"]) == []
