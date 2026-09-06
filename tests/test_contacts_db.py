"""Contacts DB layer — integration tests against a disposable Postgres.

Run:  podman run -d --rm --name contacts-pg -e POSTGRES_USER=srw \
        -e POSTGRES_PASSWORD=t -e POSTGRES_DB=srw -p 5433:5432 \
        docker.io/library/postgres:16
      python -m orchestrator.database.migrate \
        --database-url postgresql://srw:t@localhost:5433/srw \
        --dir src/orchestrator/database/migrations/app
      CONTACTS_TEST_DSN=postgresql://srw:t@localhost:5433/srw \
        python -m pytest tests/test_contacts_db.py -v
"""

import os
import uuid

import pytest
import pytest_asyncio

from orchestrator.database.postgres import PostgresDB

DSN = os.getenv("CONTACTS_TEST_DSN")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DSN, reason="CONTACTS_TEST_DSN not set"),
]


@pytest_asyncio.fixture
async def db():
    d = PostgresDB(connection_string=DSN)
    await d.connect()
    yield d
    await d.close()


@pytest_asyncio.fixture
async def seeded(db):
    """Two users, one shared project; returns ids."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    p = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, display_name) VALUES ($1, $2, $3), ($4, $5, $6)",
            a,
            f"{a}@t",
            "User A",
            b,
            f"{b}@t",
            "User B",
        )
        await conn.execute("INSERT INTO projects (id, name) VALUES ($1, 'T')", p)
        await conn.execute(
            "INSERT INTO project_members (project_id, user_id, role) VALUES ($1, $2, 'owner'), ($1, $3, 'editor')",
            p,
            a,
            b,
        )
    return {"a": a, "b": b, "p": p}


async def test_cross_tenant_same_address(db, seeded):
    """THE test not to skip (spec): two owners each hold anna@acme.de."""
    ca = await db.create_contact(seeded["a"], "Anna A")
    cb = await db.create_contact(seeded["b"], "Anna B")
    assert await db.add_contact_address(ca["id"], seeded["a"], "email", "anna@acme.de")
    assert await db.add_contact_address(cb["id"], seeded["b"], "email", "anna@acme.de")
    # same owner + same address → duplicate → None
    assert (
        await db.add_contact_address(ca["id"], seeded["a"], "email", "anna@acme.de")
        is None
    )


async def test_add_duplicate_primary_rolls_back_demotion(db, seeded):
    """Finding 1 (final review): is_primary=True on a DUPLICATE address must
    not leave the channel primary-less. The old `ON CONFLICT ... DO NOTHING`
    let the demotion UPDATE commit even though the conflicting insert added
    nothing — this reproduces that exact sequence and asserts the demotion
    rolled back with it."""
    c = await db.create_contact(seeded["a"], "Anna Duplicate")
    first = await db.add_contact_address(c["id"], seeded["a"], "email", "anna@dup.de")
    assert first["is_primary"] is True

    dup = await db.add_contact_address(
        c["id"], seeded["a"], "email", "anna@dup.de", is_primary=True
    )
    assert dup is None

    addresses = (await db.get_contact(c["id"]))["addresses"]
    assert len(addresses) == 1
    # get_contact's nested addresses come back JSON-aggregated (id as str);
    # add_contact_address's direct RETURNING row has id as a native UUID —
    # str() both sides rather than fighting that pre-existing quirk.
    assert str(addresses[0]["id"]) == str(first["id"])
    assert addresses[0]["is_primary"] is True


async def test_resolver_statuses(db, seeded):
    c = await db.create_contact(seeded["a"], "Priya Nair")
    await db.add_contact_address(c["id"], seeded["a"], "email", "priya@x.de")
    await db.link_contact_to_project(seeded["p"], c["id"], seeded["a"])
    ok = await db.resolve_contact(seeded["p"], "Priya Nair", "email")
    assert ok["status"] == "ok" and ok["address"] == "priya@x.de"
    by_addr = await db.resolve_contact(seeded["p"], "PRIYA@x.de", "email")
    assert by_addr["status"] == "ok"
    assert (await db.resolve_contact(seeded["p"], "Priya Nair", "whatsapp"))[
        "status"
    ] == "no_channel_address"
    assert (await db.resolve_contact(seeded["p"], "Nobody", "email"))[
        "status"
    ] == "not_found"
    c2 = await db.create_contact(seeded["b"], "Priya Nair")
    await db.add_contact_address(c2["id"], seeded["b"], "email", "priya2@x.de")
    await db.link_contact_to_project(seeded["p"], c2["id"], seeded["b"])
    amb = await db.resolve_contact(seeded["p"], "Priya Nair", "email")
    assert amb["status"] == "ambiguous" and len(amb["candidates"]) == 2


async def test_visibility_union_and_primary(db, seeded):
    c = await db.create_contact(seeded["a"], "Tom")
    await db.add_contact_address(c["id"], seeded["a"], "whatsapp", "+4917011111")
    first = (await db.get_contact(c["id"]))["addresses"][0]
    assert first["is_primary"] is True and first["opt_in_status"] == "pending"
    # b can't see it until linked into the shared project
    assert await db.user_can_see_contact(seeded["b"], c["id"]) is False
    await db.link_contact_to_project(seeded["p"], c["id"], seeded["a"])
    assert await db.user_can_see_contact(seeded["b"], c["id"]) is True
    assert any(x["id"] == c["id"] for x in await db.list_contacts_for_user(seeded["b"]))
    # promotion demotes; address edit resets opt-in
    second = await db.add_contact_address(
        c["id"], seeded["a"], "whatsapp", "+4917022222"
    )
    await db.update_contact_address(second["id"], is_primary=True)
    rows = (await db.get_contact(c["id"]))["addresses"]
    assert [r["is_primary"] for r in sorted(rows, key=lambda r: r["address"])] == [
        False,
        True,
    ]
    async with db.acquire() as conn:  # simulate a prior opt-in
        await conn.execute(
            "UPDATE contact_addresses SET opt_in_status='opted_in', last_inbound_at=NOW() WHERE id=$1",
            second["id"],
        )
    edited = await db.update_contact_address(second["id"], address="+4917033333")
    assert edited["opt_in_status"] == "pending" and edited["last_inbound_at"] is None


async def test_update_contact_notes_tristate(db, seeded):
    """Regression: update_contact must prepare at all.

    ``$3`` used to appear only in ``IS NULL`` / ``NULLIF($3, '')``, neither of
    which pins a type, so asyncpg's untyped PREPARE raised
    AmbiguousParameterError on *every* call — PATCH /api/contacts/{id} was a
    guaranteed 500. Caught by the dev live gate, not by the suite, because
    update_contact had no direct test. Cast is ``$3::text``.
    """
    c = await db.create_contact(seeded["a"], "Tri State", "original")

    unchanged = await db.update_contact(c["id"], None, None)
    assert unchanged["display_name"] == "Tri State"
    assert unchanged["notes"] == "original"

    renamed = await db.update_contact(c["id"], "Tri State II", None)
    assert renamed["display_name"] == "Tri State II"
    assert renamed["notes"] == "original"

    set_notes = await db.update_contact(c["id"], None, "replaced")
    assert set_notes["notes"] == "replaced"

    cleared = await db.update_contact(c["id"], None, "")
    assert cleared["notes"] is None
    assert cleared["display_name"] == "Tri State II"

    assert await db.update_contact("00000000-0000-0000-0000-000000000000") is None
