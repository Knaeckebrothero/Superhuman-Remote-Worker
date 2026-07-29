"""Contacts materialization — gather gate + gitignore floor + write loop."""

import os
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_gather_returns_files_dict():
    import main

    db = AsyncMock()
    db.get_project_contacts.return_value = [
        {
            "id": "c1",
            "display_name": "Anna Weber",
            "notes": "CET.",
            "addresses": [
                {
                    "channel": "email",
                    "address": "anna@x.de",
                    "is_primary": True,
                    "opt_in_status": "opted_in",
                }
            ],
            "projects": [{"id": "p1", "name": "P"}],
        }
    ]
    with patch.object(main, "postgres_db", db):
        out = await main._gather_project_contacts("u1", ["p1"])
    assert list(out["files"]) == ["contacts/anna-weber.md"]


@pytest.mark.asyncio
async def test_gather_gates():
    import main

    with patch.dict(os.environ, {"CONTACTS_MATERIALIZE_ENABLED": "false"}):
        assert await main._gather_project_contacts("u1", ["p1"]) == {}
    assert await main._gather_project_contacts(None, ["p1"]) == {}
    assert await main._gather_project_contacts("u1", []) == {}


def test_loop_main_gitignore_floors_contacts():
    from services.job_provisioning import _LOOP_MAIN_GITIGNORE

    assert "contacts/" in _LOOP_MAIN_GITIGNORE.splitlines()


def test_resolver_blob_carries_contacts():
    from services.config_resolver import resolve_config
    import inspect

    assert "contacts" in inspect.signature(resolve_config).parameters
