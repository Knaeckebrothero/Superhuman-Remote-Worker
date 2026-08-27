"""/api/contacts — cross-channel contact registry (knowledge-history/done/contacts_registry.md).

Owner mutates; project editors link/unlink; members read. Channel addresses
are normalized here (emails lowercased, whatsapp to E.164) so the DB only
ever sees canonical values. project_router carries the two kept
/api/projects/{id}/contacts endpoints (find-or-create-then-link semantics).
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from security.auth import require_approved_user
from security.access import require_internal, require_project_member

router = APIRouter(prefix="/api/contacts", tags=["Contacts"])
project_router = APIRouter(prefix="/api/projects", tags=["Contacts"])

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def _get_db() -> Any:
    import main

    return main.postgres_db


def _normalize_address(channel: str, address: str) -> str:
    """Canonical form or 400. Spec: emails lowercased; whatsapp E.164."""
    address = address.strip()
    if channel == "email":
        if "@" not in address or "." not in address.split("@")[-1]:
            raise HTTPException(status_code=400, detail="Invalid email format")
        return address.lower()
    if channel == "whatsapp":
        cleaned = re.sub(r"[\s\-().]", "", address)
        if not _E164.match(cleaned):
            raise HTTPException(
                status_code=400,
                detail="Invalid WhatsApp number — expected E.164 like +4917012345678",
            )
        return cleaned
    raise HTTPException(status_code=400, detail=f"Unknown channel '{channel}'")


class ContactAddressIn(BaseModel):
    channel: str
    address: str
    is_primary: bool = False


class ContactCreate(BaseModel):
    display_name: str
    notes: Optional[str] = None
    addresses: List[ContactAddressIn] = []


class ContactPatch(BaseModel):
    display_name: Optional[str] = None
    notes: Optional[str] = None


class AddressPatch(BaseModel):
    address: Optional[str] = None
    is_primary: Optional[bool] = None


async def _owned_contact(request: Request, contact_id: str) -> dict:
    db = _get_db()
    user = await require_approved_user(request, db)
    contact = await db.get_contact(contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    if str(contact["owner_user_id"]) != str(user["id"]):
        raise HTTPException(
            status_code=403, detail="Only the contact owner can modify it"
        )
    return contact


@router.get("/internal/list")
async def list_internal_contacts(
    request: Request,
    job_id: str | None = None,
    thread_id: str | None = None,
) -> dict:
    """Contacts linked to the caller's project. Agent-internal.

    The project is derived server-side from the job/thread — the agent never
    supplies a project_id, so it cannot read another project's contacts. Same
    trust posture as send_message recipient resolution.
    """
    await require_internal(request)
    if bool(job_id) == bool(thread_id):
        raise HTTPException(
            status_code=400, detail="Provide exactly one of job_id or thread_id"
        )
    db = _get_db()
    project_id = await db.resolve_project_for_agent(job_id=job_id, thread_id=thread_id)
    if not project_id:
        return {"contacts": []}
    return {"contacts": await db.get_project_contacts(project_id)}


@router.get("")
async def list_contacts(
    request: Request,
    project_id: Optional[str] = None,
    channel: Optional[str] = None,
    q: Optional[str] = None,
) -> dict:
    db = _get_db()
    user = await require_approved_user(request, db)
    rows = await db.list_contacts_for_user(
        user["id"], project_id=project_id, channel=channel, q=q
    )
    return {"contacts": rows}


@router.post("")
async def create_contact(request: Request, body: ContactCreate) -> dict:
    db = _get_db()
    user = await require_approved_user(request, db)
    if not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name is required")
    normalized = [
        (a.channel, _normalize_address(a.channel, a.address), a.is_primary)
        for a in body.addresses
    ]
    contact = await db.create_contact(user["id"], body.display_name.strip(), body.notes)
    for channel_, addr, prim in normalized:
        added = await db.add_contact_address(
            contact["id"], user["id"], channel_, addr, prim
        )
        if added is None:
            # This contact was just created by this request (not pre-existing), so
            # deleting it on failed creation is safe — the 409 hint still points at
            # the pre-existing OTHER contact that already holds this address.
            await db.delete_contact(contact["id"])
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Address {addr} already belongs to one of your contacts — "
                    "link that contact to the project instead"
                ),
            )
    return {"contact": await db.get_contact(contact["id"])}


@router.patch("/{contact_id}")
async def patch_contact(request: Request, contact_id: str, body: ContactPatch) -> dict:
    await _owned_contact(request, contact_id)
    display_name = body.display_name
    if display_name is not None:
        display_name = display_name.strip()
        if not display_name:
            raise HTTPException(status_code=400, detail="display_name cannot be empty")
    updated = await _get_db().update_contact(contact_id, display_name, body.notes)
    return {"contact": updated}


@router.delete("/{contact_id}")
async def delete_contact(request: Request, contact_id: str) -> dict:
    await _owned_contact(request, contact_id)
    await _get_db().delete_contact(contact_id)
    return {"status": "deleted"}


@router.post("/{contact_id}/addresses")
async def add_address(
    request: Request, contact_id: str, body: ContactAddressIn
) -> dict:
    contact = await _owned_contact(request, contact_id)
    addr = _normalize_address(body.channel, body.address)
    added = await _get_db().add_contact_address(
        contact["id"], contact["owner_user_id"], body.channel, addr, body.is_primary
    )
    if added is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Address {addr} already belongs to one of your contacts — "
                "link that contact to the project instead"
            ),
        )
    return {"address": added}


@router.patch("/addresses/{address_id}")
async def patch_address(request: Request, address_id: str, body: AddressPatch) -> dict:
    db = _get_db()
    user = await require_approved_user(request, db)
    existing = await db.get_contact_address(address_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Address not found")
    if str(existing["owner_user_id"]) != str(user["id"]):
        raise HTTPException(
            status_code=403, detail="Only the contact owner can modify it"
        )
    new_addr = None
    if body.address is not None:
        new_addr = _normalize_address(existing["channel"], body.address)
    updated = await db.update_contact_address(address_id, new_addr, body.is_primary)
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Address {new_addr or existing['address']} already belongs to "
                "one of your contacts — link that contact to the project instead"
            ),
        )
    return {"address": updated}


@router.delete("/addresses/{address_id}")
async def delete_address(request: Request, address_id: str) -> dict:
    db = _get_db()
    user = await require_approved_user(request, db)
    existing = await db.get_contact_address(address_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Address not found")
    if str(existing["owner_user_id"]) != str(user["id"]):
        raise HTTPException(
            status_code=403, detail="Only the contact owner can modify it"
        )
    await db.delete_contact_address(address_id)
    return {"status": "deleted"}


@router.post("/{contact_id}/projects/{project_id}")
async def link_contact_to_project(
    request: Request, contact_id: str, project_id: str
) -> dict:
    db = _get_db()
    # require_project_member returns (user, project) — index instead of
    # unpacking so a test double that stands in for the whole gate (a bare
    # AsyncMock, not configured to return a 2-tuple) doesn't blow up on
    # `a, b = ...` unpacking. Real calls still get the real user dict.
    member = await require_project_member(
        request, db, project_id, min_role="editor", allow_archived=False
    )
    user = member[0]
    if not await db.user_can_see_contact(user["id"], contact_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    await db.link_contact_to_project(project_id, contact_id, user["id"])
    return {"status": "linked"}


@router.delete("/{contact_id}/projects/{project_id}")
async def unlink_contact_from_project(
    request: Request, contact_id: str, project_id: str
) -> dict:
    db = _get_db()
    await require_project_member(request, db, project_id, min_role="editor")
    removed = await db.unlink_contact_from_project(project_id, contact_id)
    if not removed:
        raise HTTPException(
            status_code=404, detail="Contact not linked to this project"
        )
    return {"status": "unlinked"}


@project_router.get("/{project_id}/contacts")
async def get_project_contacts(request: Request, project_id: str) -> dict:
    db = _get_db()
    await require_project_member(request, db, project_id)
    return {"contacts": await db.get_project_contacts(project_id)}


@project_router.post("/{project_id}/contacts")
async def add_project_contact(
    request: Request, project_id: str, body: ContactCreate
) -> dict:
    """Find-or-create-then-link (spec): match by supplied address among
    caller-visible contacts, else by exact display_name, else create owned
    by caller; then link."""
    db = _get_db()
    # require_project_member returns (user, project) — index instead of
    # unpacking so a test double that stands in for the whole gate (a bare
    # AsyncMock, not configured to return a 2-tuple) doesn't blow up on
    # `a, b = ...` unpacking. Real calls still get the real user dict.
    member = await require_project_member(
        request, db, project_id, min_role="editor", allow_archived=False
    )
    user = member[0]
    normalized = [
        (a.channel, _normalize_address(a.channel, a.address), a.is_primary)
        for a in body.addresses
    ]
    visible = await db.list_contacts_for_user(user["id"])
    match = None
    for c in visible:
        for ch, addr, _ in normalized:
            if any(a["channel"] == ch and a["address"] == addr for a in c["addresses"]):
                match = c
                break
        if match:
            break
    if match is None:
        wanted = body.display_name.strip().lower()
        named = [c for c in visible if c["display_name"].strip().lower() == wanted]
        match = named[0] if named else None
    if match is None:
        created = await db.create_contact(
            user["id"], body.display_name.strip(), body.notes
        )
        for ch, addr, prim in normalized:
            added = await db.add_contact_address(
                created["id"], user["id"], ch, addr, prim
            )
            if added is None:
                # This contact was just created by this request (not pre-existing), so
                # deleting it on failed creation is safe — the 409 hint still points at
                # the pre-existing OTHER contact that already holds this address.
                await db.delete_contact(created["id"])
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Address {addr} already belongs to one of your contacts — "
                        "link that contact to the project instead"
                    ),
                )
        match = {"id": created["id"]}
    await db.link_contact_to_project(project_id, match["id"], user["id"])
    return {"contact": await db.get_contact(match["id"])}
