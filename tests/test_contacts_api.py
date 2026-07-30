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


async def test_link_extracts_user_from_member_tuple(db, as_user_a, monkeypatch):
    """Regression net for the (user, project) contract: require_project_member
    returns a 2-tuple, not just the user — the router must index [0], not treat
    the whole tuple as the user dict."""
    gate = AsyncMock(return_value=(USER_A, {"id": "p1"}))
    monkeypatch.setattr(contacts_router, "require_project_member", gate)
    db.user_can_see_contact.return_value = True
    out = await contacts_router.link_contact_to_project(_req(), "c1", "p1")
    assert db.user_can_see_contact.await_args.args[0] == USER_A["id"]
    db.link_contact_to_project.assert_awaited_once_with("p1", "c1", USER_A["id"])
    assert out == {"status": "linked"}


async def test_unlink_contact_from_project(db, as_user_a, monkeypatch):
    gate = AsyncMock(return_value=(USER_A, {"id": "p1"}))
    monkeypatch.setattr(contacts_router, "require_project_member", gate)

    db.unlink_contact_from_project.return_value = False
    with pytest.raises(HTTPException) as e:
        await contacts_router.unlink_contact_from_project(_req(), "c1", "p1")
    assert e.value.status_code == 404
    db.unlink_contact_from_project.assert_awaited_once_with("p1", "c1")

    db.unlink_contact_from_project.return_value = True
    out = await contacts_router.unlink_contact_from_project(_req(), "c1", "p1")
    assert out == {"status": "unlinked"}


async def test_patch_address_not_found(db, as_user_a):
    db.get_contact_address.return_value = None
    with pytest.raises(HTTPException) as e:
        await contacts_router.patch_address(
            _req(), "a1", contacts_router.AddressPatch()
        )
    assert e.value.status_code == 404


async def test_patch_address_owner_only(db, as_user_a):
    db.get_contact_address.return_value = {
        "id": "a1",
        "owner_user_id": USER_B["id"],
        "channel": "email",
    }
    with pytest.raises(HTTPException) as e:
        await contacts_router.patch_address(
            _req(), "a1", contacts_router.AddressPatch(address="x@y.com")
        )
    assert e.value.status_code == 403
    db.update_contact_address.assert_not_awaited()


async def test_patch_contact_blank_display_name_400(db, as_user_a):
    """Finding 3: an all-whitespace display_name must 400, not silently blank
    the contact's name (COALESCE in the DB layer only skips NULL, not "")."""
    db.get_contact.return_value = {"id": "c1", "owner_user_id": USER_A["id"]}
    with pytest.raises(HTTPException) as e:
        await contacts_router.patch_contact(
            _req(), "c1", contacts_router.ContactPatch(display_name="   ")
        )
    assert e.value.status_code == 400
    db.update_contact.assert_not_awaited()


async def test_patch_contact_strips_display_name(db, as_user_a):
    db.get_contact.return_value = {"id": "c1", "owner_user_id": USER_A["id"]}
    db.update_contact.return_value = {"id": "c1", "display_name": "Anna"}
    out = await contacts_router.patch_contact(
        _req(), "c1", contacts_router.ContactPatch(display_name="  Anna  ")
    )
    db.update_contact.assert_awaited_once_with("c1", "Anna", None)
    assert out == {"contact": {"id": "c1", "display_name": "Anna"}}


async def test_patch_address_duplicate_409(db, as_user_a):
    """Finding 2: update_contact_address maps a unique violation to None —
    the router must turn that into 409, not pass {"address": null} through
    as a 200 (ContactsService.patchAddress types the response non-null)."""
    db.get_contact_address.return_value = {
        "id": "a1",
        "owner_user_id": USER_A["id"],
        "channel": "email",
        "address": "old@acme.de",
    }
    db.update_contact_address.return_value = None  # duplicate
    with pytest.raises(HTTPException) as e:
        await contacts_router.patch_address(
            _req(), "a1", contacts_router.AddressPatch(address="dupe@acme.de")
        )
    assert e.value.status_code == 409
    assert "dupe@acme.de" in e.value.detail


async def test_patch_address_normalizes_on_success(db, as_user_a):
    db.get_contact_address.return_value = {
        "id": "a1",
        "owner_user_id": USER_A["id"],
        "channel": "email",
    }
    db.update_contact_address.return_value = {"id": "a1", "address": "new@acme.de"}
    out = await contacts_router.patch_address(
        _req(),
        "a1",
        contacts_router.AddressPatch(address="  NEW@ACME.DE  ", is_primary=True),
    )
    db.update_contact_address.assert_awaited_once_with("a1", "new@acme.de", True)
    assert out == {"address": {"id": "a1", "address": "new@acme.de"}}


async def test_delete_address_owner_only(db, as_user_a):
    db.get_contact_address.return_value = {"id": "a1", "owner_user_id": USER_B["id"]}
    with pytest.raises(HTTPException) as e:
        await contacts_router.delete_address(_req(), "a1")
    assert e.value.status_code == 403
    db.delete_contact_address.assert_not_awaited()


async def test_delete_address_success(db, as_user_a):
    db.get_contact_address.return_value = {"id": "a1", "owner_user_id": USER_A["id"]}
    out = await contacts_router.delete_address(_req(), "a1")
    db.delete_contact_address.assert_awaited_once_with("a1")
    assert out == {"status": "deleted"}


async def test_add_address_unknown_channel_400(db, as_user_a):
    db.get_contact.return_value = {"id": "c1", "owner_user_id": USER_A["id"]}
    with pytest.raises(HTTPException) as e:
        await contacts_router.add_address(
            _req(),
            "c1",
            contacts_router.ContactAddressIn(channel="sms", address="+491234567"),
        )
    assert e.value.status_code == 400


async def test_create_contact_success_adds_addresses(db, as_user_a):
    db.create_contact.return_value = {"id": "c1"}
    db.add_contact_address.return_value = {"id": "addr1"}
    db.get_contact.return_value = {
        "id": "c1",
        "display_name": "Anna",
        "addresses": [{"id": "addr1"}],
    }
    body = contacts_router.ContactCreate(
        display_name="Anna",
        addresses=[
            contacts_router.ContactAddressIn(channel="email", address="Anna@Acme.de")
        ],
    )
    out = await contacts_router.create_contact(_req(), body)
    db.create_contact.assert_awaited_once_with(USER_A["id"], "Anna", None)
    db.add_contact_address.assert_awaited_once_with(
        "c1", USER_A["id"], "email", "anna@acme.de", False
    )
    db.delete_contact.assert_not_awaited()
    assert out == {
        "contact": {"id": "c1", "display_name": "Anna", "addresses": [{"id": "addr1"}]}
    }


async def test_create_contact_rolls_back_on_duplicate_address(db, as_user_a):
    db.create_contact.return_value = {"id": "c1"}
    db.add_contact_address.return_value = None  # duplicate mid-loop
    body = contacts_router.ContactCreate(
        display_name="Anna",
        addresses=[
            contacts_router.ContactAddressIn(channel="email", address="anna@acme.de")
        ],
    )
    with pytest.raises(HTTPException) as e:
        await contacts_router.create_contact(_req(), body)
    assert e.value.status_code == 409
    db.delete_contact.assert_awaited_once_with("c1")


async def test_project_get_contacts_gated(db, monkeypatch):
    gate = AsyncMock(return_value=USER_A)
    monkeypatch.setattr(contacts_router, "require_project_member", gate)
    db.get_project_contacts.return_value = []
    out = await contacts_router.get_project_contacts(_req(), "p1")
    gate.assert_awaited()
    assert out == {"contacts": []}


async def test_project_post_find_or_create_then_link(db, monkeypatch):
    # require_project_member returns (user, project) — the router indexes
    # [0], so the gate double must stand in for the whole tuple (see
    # test_link_extracts_user_from_member_tuple in this same file).
    monkeypatch.setattr(
        contacts_router,
        "require_project_member",
        AsyncMock(return_value=(USER_A, {"id": "p1"})),
    )
    # no address/name match among visible → create then link
    db.list_contacts_for_user.return_value = []
    db.create_contact.return_value = {"id": "c-new"}
    db.get_contact.return_value = {"id": "c-new", "owner_user_id": USER_A["id"]}
    db.add_contact_address.return_value = {"id": "a1"}
    out = await contacts_router.add_project_contact(
        _req(),
        "p1",
        contacts_router.ContactCreate(
            display_name="Anna",
            addresses=[
                contacts_router.ContactAddressIn(channel="email", address="Anna@X.de")
            ],
        ),
    )
    db.link_contact_to_project.assert_awaited_once_with("p1", "c-new", USER_A["id"])
    assert out["contact"]["id"] == "c-new"
    # address match among visible → link existing, no create
    db.reset_mock()
    db.list_contacts_for_user.return_value = [
        {
            "id": "c-exist",
            "display_name": "Anna",
            "addresses": [{"channel": "email", "address": "anna@x.de"}],
        }
    ]
    db.get_contact.return_value = {"id": "c-exist", "owner_user_id": USER_B["id"]}
    await contacts_router.add_project_contact(
        _req(),
        "p1",
        contacts_router.ContactCreate(
            display_name="ignored",
            addresses=[
                contacts_router.ContactAddressIn(channel="email", address="ANNA@x.de")
            ],
        ),
    )
    db.create_contact.assert_not_awaited()
    db.link_contact_to_project.assert_awaited_once_with("p1", "c-exist", USER_A["id"])


async def test_project_post_create_rolls_back_on_duplicate_address(db, monkeypatch):
    monkeypatch.setattr(
        contacts_router,
        "require_project_member",
        AsyncMock(return_value=(USER_A, {"id": "p1"})),
    )
    # no visible contact matches → create branch; duplicate address mid-loop
    db.list_contacts_for_user.return_value = []
    db.create_contact.return_value = {"id": "c-new"}
    db.add_contact_address.return_value = None
    with pytest.raises(HTTPException) as e:
        await contacts_router.add_project_contact(
            _req(),
            "p1",
            contacts_router.ContactCreate(
                display_name="Anna",
                addresses=[
                    contacts_router.ContactAddressIn(
                        channel="email", address="anna@x.de"
                    )
                ],
            ),
        )
    assert e.value.status_code == 409
    db.delete_contact.assert_awaited_once_with("c-new")
    db.link_contact_to_project.assert_not_awaited()
