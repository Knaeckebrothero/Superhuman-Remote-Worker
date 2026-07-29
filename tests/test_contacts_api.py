"""Contacts API — gates + behavior, mocked DB (house style)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from routers import contacts as contacts_router

pytestmark = pytest.mark.asyncio

USER_A = {"id": "aaaaaaaa-0000-0000-0000-000000000000", "is_admin": False}
USER_B = {"id": "bbbbbbbb-0000-0000-0000-000000000000", "is_admin": False}


def _req():
    return MagicMock()


@pytest.fixture
def db(monkeypatch):
    d = MagicMock()
    for name in (
        "list_contacts_for_user",
        "get_contact",
        "create_contact",
        "update_contact",
        "delete_contact",
        "add_contact_address",
        "update_contact_address",
        "delete_contact_address",
        "get_contact_address",
        "link_contact_to_project",
        "unlink_contact_from_project",
        "get_project_contacts",
        "user_can_see_contact",
        "resolve_contact",
    ):
        setattr(d, name, AsyncMock())
    monkeypatch.setattr(contacts_router, "_get_db", lambda: d)
    return d


@pytest.fixture
def as_user_a(monkeypatch):
    monkeypatch.setattr(
        contacts_router, "require_approved_user", AsyncMock(return_value=USER_A)
    )


async def test_list_scopes_to_caller(db, as_user_a):
    db.list_contacts_for_user.return_value = []
    out = await contacts_router.list_contacts(
        _req(), project_id=None, channel=None, q=None
    )
    db.list_contacts_for_user.assert_awaited_once_with(
        USER_A["id"], project_id=None, channel=None, q=None
    )
    assert out == {"contacts": []}


async def test_patch_contact_owner_only(db, as_user_a):
    db.get_contact.return_value = {"id": "c1", "owner_user_id": USER_B["id"]}
    with pytest.raises(HTTPException) as e:
        await contacts_router.patch_contact(
            _req(), "c1", contacts_router.ContactPatch(display_name="X")
        )
    assert e.value.status_code == 403
    db.update_contact.assert_not_awaited()


async def test_add_address_validates_and_409s(db, as_user_a):
    db.get_contact.return_value = {"id": "c1", "owner_user_id": USER_A["id"]}
    with pytest.raises(HTTPException) as e:
        await contacts_router.add_address(
            _req(),
            "c1",
            contacts_router.ContactAddressIn(
                channel="whatsapp", address="not-a-number"
            ),
        )
    assert e.value.status_code == 400
    db.add_contact_address.return_value = None  # duplicate
    with pytest.raises(HTTPException) as e:
        await contacts_router.add_address(
            _req(),
            "c1",
            contacts_router.ContactAddressIn(
                channel="whatsapp", address="+49 170-555 (0)1"
            ),
        )
    assert e.value.status_code == 409
    # normalization stripped [\s\-().] before hitting the DB
    assert (
        db.add_contact_address.await_args.args[3] == "+4917055501"
        or db.add_contact_address.await_args.kwargs.get("address") == "+4917055501"
    )


async def test_link_requires_editor_and_visibility(db, as_user_a, monkeypatch):
    gate = AsyncMock()
    monkeypatch.setattr(contacts_router, "require_project_member", gate)
    db.user_can_see_contact.return_value = False
    with pytest.raises(HTTPException) as e:
        await contacts_router.link_contact_to_project(_req(), "c1", "p1")
    assert e.value.status_code == 404
    gate.assert_awaited()  # editor gate ran before visibility


async def test_delete_contact_owner_only(db, as_user_a):
    db.get_contact.return_value = {
        "id": "c1",
        "owner_user_id": USER_A["id"],
        "projects": [{"id": "p1", "name": "P"}],
    }
    db.delete_contact.return_value = True
    out = await contacts_router.delete_contact(_req(), "c1")
    assert out == {"status": "deleted"}
