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
    """Each gated case must short-circuit before any DB access.

    ``_gather_project_contacts`` wraps its body in a broad ``except
    Exception: return {}``, so a mock that merely returns ``{}`` (or a real,
    disconnected ``postgres_db``) can't distinguish "gate worked" from "gate
    broke, DB blew up, exception swallowed". Force a loud failure on touch
    AND assert the mock was never awaited — that assertion runs outside the
    gather and survives even if the exception itself got swallowed.
    """
    import main

    db = AsyncMock()
    db.get_project_contacts.side_effect = AssertionError("db touched despite gate")
    with patch.object(main, "postgres_db", db):
        with patch.dict(os.environ, {"CONTACTS_MATERIALIZE_ENABLED": "false"}):
            assert await main._gather_project_contacts("u1", ["p1"]) == {}
        db.get_project_contacts.assert_not_awaited()

        assert await main._gather_project_contacts(None, ["p1"]) == {}
        db.get_project_contacts.assert_not_awaited()

        assert await main._gather_project_contacts("u1", []) == {}
        db.get_project_contacts.assert_not_awaited()


def test_loop_main_gitignore_floors_contacts():
    from services.job_provisioning import _LOOP_MAIN_GITIGNORE

    assert "contacts/" in _LOOP_MAIN_GITIGNORE.splitlines()


def test_resolver_blob_carries_contacts():
    """Full round-trip, mirroring test_skill_runtime.py's skills= precedent:
    resolve_config attaches contacts to the blob, and load_config_from_resolved
    (what UniversalAgent.from_resolved calls) hydrates it back out."""
    from services.config_resolver import resolve_config
    from src.core.loader import load_config_from_resolved

    payload = {"files": {"contacts/x.md": "y"}}
    blob = resolve_config(base_config_name="persistent_defaults", contacts=payload)
    assert blob["contacts"] == payload

    config = load_config_from_resolved(blob)
    assert config.extra["_resolved_contacts"] == payload
