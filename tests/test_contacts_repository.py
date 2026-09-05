"""Connection ownership and import boundaries of the contacts repository."""

import asyncio
from contextlib import asynccontextmanager
import subprocess
import sys
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from orchestrator.database import postgres
from orchestrator.database.repositories.contacts import ContactsRepository


@pytest.mark.asyncio
async def test_create_releases_write_acquisition_before_nested_readback():
    contact_id = uuid4()
    writer = AsyncMock()
    writer.fetchval.return_value = contact_id
    reader = AsyncMock()
    reader.fetchrow.return_value = {
        "id": contact_id,
        "addresses": "[]",
        "projects": "[]",
    }
    connections = iter([writer, reader])
    events = []

    @asynccontextmanager
    async def acquire():
        connection = next(connections)
        events.append(("acquire", connection))
        try:
            yield connection
        finally:
            events.append(("release", connection))

    repository = ContactsRepository(acquire)
    assert events == []
    result = await repository.create_contact(uuid4(), "Created", "notes")

    assert result == {"id": contact_id, "addresses": [], "projects": []}
    assert events == [
        ("acquire", writer),
        ("release", writer),
        ("acquire", reader),
        ("release", reader),
    ]
    writer.fetchval.assert_awaited_once()
    reader.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_facade_resolves_replaced_acquire_in_calling_task(monkeypatch):
    database = postgres.PostgresDB(connection_string="postgresql://unused")
    owner = asyncio.current_task()
    acquired = []

    def acquire_for(contact_id):
        @asynccontextmanager
        async def acquire():
            acquired.append((contact_id, asyncio.current_task()))
            connection = AsyncMock()
            connection.fetchrow.return_value = {
                "id": contact_id,
                "addresses": [],
                "projects": [],
            }
            yield connection

        return acquire

    first, second = uuid4(), uuid4()
    monkeypatch.setattr(database, "acquire", acquire_for(first))
    assert (await database.get_contact(str(first)))["id"] == first
    monkeypatch.setattr(database, "acquire", acquire_for(second))
    assert (await database.get_contact(str(second)))["id"] == second
    assert acquired == [(first, owner), (second, owner)]


def test_repository_imports_without_asyncpg_or_facade(tmp_path):
    script = """
import importlib.abc
import sys

class BlockBoundaryImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'asyncpg' or fullname.startswith('asyncpg.'):
            raise ModuleNotFoundError('asyncpg deliberately unavailable')
        if fullname in {'orchestrator.database.postgres', 'orchestrator.main'}:
            raise AssertionError('repository crossed import boundary: ' + fullname)

sys.meta_path.insert(0, BlockBoundaryImports())
from orchestrator.database.repositories import contacts as module
assert module.asyncpg is None
module.ContactsRepository(lambda: None)
assert 'orchestrator.database.postgres' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_facade_still_refuses_construction_without_asyncpg(monkeypatch):
    monkeypatch.setattr(postgres, "asyncpg", None)
    with pytest.raises(ImportError, match="asyncpg is required"):
        postgres.PostgresDB(connection_string="postgresql://unused")
