"""Contacts persistence using the owning database's connection acquisition."""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Dict, List

try:
    import asyncpg
except ImportError:
    asyncpg = None

# =============================================================================
# Contacts registry (knowledge-history/done/contacts_registry.md)
# =============================================================================
# House gotcha: asyncpg returns json_agg()/JSONB columns as strings, not
# parsed Python objects — _contact_row() decodes them as nested contact records.

CONTACT_OPT_IN_DEFAULT = {"email": "opted_in", "whatsapp": "pending"}

_CONTACT_SELECT = """
    SELECT c.id, c.owner_user_id, c.display_name, c.notes, c.created_at, c.updated_at,
           COALESCE((SELECT json_agg(json_build_object(
               'id', ca.id, 'channel', ca.channel, 'address', ca.address,
               'is_primary', ca.is_primary, 'opt_in_status', ca.opt_in_status,
               'last_inbound_at', ca.last_inbound_at, 'created_at', ca.created_at)
               ORDER BY ca.created_at)
             FROM contact_addresses ca WHERE ca.contact_id = c.id), '[]') AS addresses,
           COALESCE((SELECT json_agg(json_build_object('id', p.id, 'name', p.name))
             FROM project_contacts pc JOIN projects p ON p.id = pc.project_id
            WHERE pc.contact_id = c.id), '[]') AS projects
    FROM contacts c
"""


def _contact_row(row) -> Dict[str, Any]:
    """Convert a contacts-select asyncpg Record to a dict with nested lists.

    Use anywhere ``_CONTACT_SELECT`` is the source query — the ``addresses``
    and ``projects`` columns come back from asyncpg as JSON *strings* (the
    house json_agg/JSONB gotcha), not parsed lists.
    """
    d = dict(row)
    d["addresses"] = (
        json.loads(d["addresses"])
        if isinstance(d["addresses"], str)
        else d["addresses"]
    )
    d["projects"] = (
        json.loads(d["projects"]) if isinstance(d["projects"], str) else d["projects"]
    )
    return d


class ContactsRepository:
    """Contact queries; the facade owns pool and transaction lifecycle."""

    def __init__(self, acquire: Callable[[], AbstractAsyncContextManager[Any]]):
        self.acquire = acquire

    async def list_contacts_for_user(
        self,
        user_id: str,
        project_id: str | None = None,
        channel: str | None = None,
        q: str | None = None,
    ) -> List[Dict[str, Any]]:
        """List contacts visible to a user: owned ∪ linked into their projects.

        Optional filters narrow further: ``project_id`` to contacts linked
        into that specific project, ``channel`` to contacts with at least one
        address on that channel, ``q`` to a case-insensitive substring match
        on display name or any address.
        """
        conds = [
            """(c.owner_user_id = $1 OR EXISTS (
            SELECT 1 FROM project_contacts pc
            JOIN project_members pm ON pm.project_id = pc.project_id AND pm.user_id = $1
            WHERE pc.contact_id = c.id))"""
        ]
        args: list = [user_id]
        if project_id:
            args.append(project_id)
            conds.append(
                f"EXISTS (SELECT 1 FROM project_contacts pc2 WHERE pc2.contact_id = c.id AND pc2.project_id = ${len(args)})"
            )
        if channel:
            args.append(channel)
            conds.append(
                f"EXISTS (SELECT 1 FROM contact_addresses ca2 WHERE ca2.contact_id = c.id AND ca2.channel = ${len(args)})"
            )
        if q:
            args.append(f"%{q}%")
            conds.append(
                f"(c.display_name ILIKE ${len(args)} OR EXISTS (SELECT 1 FROM contact_addresses ca3 WHERE ca3.contact_id = c.id AND ca3.address ILIKE ${len(args)}))"
            )
        sql = (
            _CONTACT_SELECT
            + " WHERE "
            + " AND ".join(conds)
            + " ORDER BY c.display_name ASC"
        )
        async with self.acquire() as conn:
            return [_contact_row(r) for r in await conn.fetch(sql, *args)]

    async def get_contact(self, contact_id: str) -> Dict[str, Any] | None:
        """Fetch one contact with nested ``addresses`` and ``projects``."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(_CONTACT_SELECT + " WHERE c.id = $1", contact_id)
        return _contact_row(row) if row else None

    async def create_contact(
        self, owner_user_id: str, display_name: str, notes: str | None = None
    ) -> Dict[str, Any]:
        """Create a contact owned by ``owner_user_id``. Returns the nested shape."""
        async with self.acquire() as conn:
            contact_id = await conn.fetchval(
                """
                INSERT INTO contacts (owner_user_id, display_name, notes)
                VALUES ($1, $2, NULLIF($3, ''))
                RETURNING id
                """,
                owner_user_id,
                display_name,
                notes,
            )
        return await self.get_contact(str(contact_id))

    async def update_contact(
        self,
        contact_id: str,
        display_name: str | None = None,
        notes: str | None = None,
    ) -> Dict[str, Any] | None:
        """Patch display_name/notes.

        ``notes``: ``None`` leaves it unchanged, ``""`` clears it, any other
        text sets it — the Cockpit form sends ``""`` to clear.

        ``$3`` is cast explicitly. asyncpg prepares statements without
        declaring parameter types, and ``$3`` only ever appears in
        ``IS NULL`` and ``NULLIF($3, '')`` — neither pins a type, so Postgres
        raised ``AmbiguousParameterError`` at PREPARE time for *every* call,
        whatever the values, making ``PATCH /api/contacts/{id}`` a guaranteed
        500. ``$2`` needs no cast: ``COALESCE($2, display_name)`` resolves
        against the column.
        """
        async with self.acquire() as conn:
            row_id = await conn.fetchval(
                """
                UPDATE contacts
                   SET display_name = COALESCE($2, display_name),
                       notes = CASE WHEN $3::text IS NULL THEN notes
                                    ELSE NULLIF($3::text, '') END,
                       updated_at = NOW()
                 WHERE id = $1
             RETURNING id
                """,
                contact_id,
                display_name,
                notes,
            )
        if row_id is None:
            return None
        return await self.get_contact(contact_id)

    async def delete_contact(self, contact_id: str) -> bool:
        """Delete a contact (cascades to its addresses and project links)."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM contacts WHERE id = $1", contact_id
            )
        return result == "DELETE 1"

    async def add_contact_address(
        self,
        contact_id: str,
        owner_user_id: str,
        channel: str,
        address: str,
        is_primary: bool = False,
    ) -> Dict[str, Any] | None:
        """Add a channel address. Returns None on duplicate
        ``(owner_user_id, channel, address)``.

        The first address added on a given channel is auto-primary; forcing
        ``is_primary=True`` demotes the previous primary in the same
        transaction.
        """
        async with self.acquire() as conn:
            try:
                async with conn.transaction():
                    has_primary = await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM contact_addresses WHERE contact_id=$1 AND channel=$2 AND is_primary)",
                        contact_id,
                        channel,
                    )
                    make_primary = is_primary or not has_primary
                    if is_primary and has_primary:
                        await conn.execute(
                            "UPDATE contact_addresses SET is_primary=false WHERE contact_id=$1 AND channel=$2 AND is_primary",
                            contact_id,
                            channel,
                        )
                    row = await conn.fetchrow(
                        """INSERT INTO contact_addresses
                               (contact_id, owner_user_id, channel, address, is_primary, opt_in_status)
                           VALUES ($1, $2, $3, $4, $5, $6)
                           RETURNING *""",
                        contact_id,
                        owner_user_id,
                        channel,
                        address,
                        make_primary,
                        CONTACT_OPT_IN_DEFAULT.get(channel, "pending"),
                    )
                    return dict(row) if row else None
            except asyncpg.UniqueViolationError:
                # Two distinct unique indexes can raise here:
                #  - (owner_user_id, channel, address): a plain duplicate.
                #  - uq_contact_primary_per_channel: two concurrent calls both
                #    observed "no primary yet" for this (contact_id, channel)
                #    and raced to insert one.
                # Either way the exception propagates out of the `async with
                # conn.transaction()` block above, so Postgres rolls back the
                # whole transaction — including the is_primary=false demotion
                # UPDATE a few lines up. Without that rollback (the previous
                # `ON CONFLICT ... DO NOTHING`), a duplicate-address call with
                # is_primary=True would silently commit the demotion while
                # inserting nothing, leaving the channel with no primary at
                # all. No data corruption from the index itself either way —
                # just report it like any other duplicate.
                return None

    async def update_contact_address(
        self,
        address_id: str,
        address: str | None = None,
        is_primary: bool | None = None,
    ) -> Dict[str, Any] | None:
        """Patch an address.

        Changing ``address`` resets ``opt_in_status`` to the channel default
        and clears ``last_inbound_at``. Promoting to primary demotes the old
        primary for that contact+channel atomically. A no-op call (both args
        None/unchanged) returns the row as-is.
        """
        async with self.acquire() as conn:
            try:
                async with conn.transaction():
                    cur = await conn.fetchrow(
                        "SELECT * FROM contact_addresses WHERE id=$1", address_id
                    )
                    if cur is None:
                        return None
                    if is_primary is True and not cur["is_primary"]:
                        await conn.execute(
                            "UPDATE contact_addresses SET is_primary=false WHERE contact_id=$1 AND channel=$2 AND is_primary",
                            cur["contact_id"],
                            cur["channel"],
                        )
                    sets, args = [], []
                    if address is not None and address != cur["address"]:
                        args.append(address)
                        sets.append(f"address=${len(args)}")
                        args.append(
                            CONTACT_OPT_IN_DEFAULT.get(cur["channel"], "pending")
                        )
                        sets.append(f"opt_in_status=${len(args)}")
                        sets.append("last_inbound_at=NULL")
                    if is_primary is not None:
                        args.append(is_primary)
                        sets.append(f"is_primary=${len(args)}")
                    if not sets:
                        return dict(cur)
                    args.append(address_id)
                    row = await conn.fetchrow(
                        f"UPDATE contact_addresses SET {', '.join(sets)} WHERE id=${len(args)} RETURNING *",
                        *args,
                    )
                    return dict(row) if row else None
            except asyncpg.UniqueViolationError:
                # Concurrent promotion race on uq_contact_primary_per_channel:
                # another call promoted a different address on this same
                # (contact_id, channel) between our read and our write. No
                # data corruption (the index held); report it like any other
                # duplicate — the transaction already rolled back.
                return None

    async def delete_contact_address(self, address_id: str) -> bool:
        """Delete a contact address by ID. Returns True if deleted."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM contact_addresses WHERE id = $1", address_id
            )
        return result == "DELETE 1"

    async def get_contact_address(self, address_id: str) -> Dict[str, Any] | None:
        """Fetch one address row (includes owner_user_id for router ownership gates)."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM contact_addresses WHERE id = $1", address_id
            )
        return dict(row) if row else None

    async def link_contact_to_project(
        self, project_id: str, contact_id: str, added_by: str | None
    ) -> bool:
        """Link a contact into a project. Returns True if newly linked,
        False if the link already existed."""
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO project_contacts (project_id, contact_id, added_by)
                VALUES ($1, $2, $3)
                ON CONFLICT DO NOTHING
                """,
                project_id,
                contact_id,
                added_by,
            )
        return result == "INSERT 0 1"

    async def unlink_contact_from_project(
        self, project_id: str, contact_id: str
    ) -> bool:
        """Unlink a contact from a project. Returns True if a link was removed."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM project_contacts WHERE project_id = $1 AND contact_id = $2",
                project_id,
                contact_id,
            )
        return result == "DELETE 1"

    async def get_project_contacts(self, project_id: str) -> List[Dict[str, Any]]:
        """List a project's linked contacts, nested."""
        sql = (
            _CONTACT_SELECT
            + " JOIN project_contacts pc ON pc.contact_id = c.id AND pc.project_id = $1"
            + " ORDER BY c.display_name"
        )
        async with self.acquire() as conn:
            return [_contact_row(r) for r in await conn.fetch(sql, project_id)]

    async def user_can_see_contact(self, user_id: str, contact_id: str) -> bool:
        """True if the contact is owned by, or project-linked to, this user."""
        async with self.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM contacts c
                        WHERE c.id = $2
                          AND (c.owner_user_id = $1 OR EXISTS (
                            SELECT 1 FROM project_contacts pc
                            JOIN project_members pm ON pm.project_id = pc.project_id AND pm.user_id = $1
                            WHERE pc.contact_id = c.id))
                    )
                    """,
                    user_id,
                    contact_id,
                )
            )

    async def resolve_contact(
        self, project_id: str, to: str, channel: str
    ) -> Dict[str, Any]:
        """Channel-aware recipient resolution among project-linked contacts.

        Statuses: ok {contact_id, display_name, address, channel} ·
        not_found {} · ambiguous {candidates: [{display_name, addresses}]} ·
        no_channel_address {display_name, channels}.
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """SELECT c.id, c.display_name, ca.id AS addr_id, ca.channel,
                          ca.address, ca.is_primary, ca.created_at
                     FROM contacts c
                     JOIN project_contacts pc ON pc.contact_id = c.id AND pc.project_id = $1
                     LEFT JOIN contact_addresses ca ON ca.contact_id = c.id
                    WHERE LOWER(c.display_name) = LOWER($2)
                       OR c.id IN (SELECT contact_id FROM contact_addresses
                                    WHERE LOWER(address) = LOWER($2))""",
                project_id,
                to,
            )
        if not rows:
            return {"status": "not_found"}
        by_contact: dict = {}
        for r in rows:
            by_contact.setdefault(
                r["id"], {"display_name": r["display_name"], "addrs": []}
            )
            if r["addr_id"]:
                by_contact[r["id"]]["addrs"].append(dict(r))
        if len(by_contact) > 1:
            return {
                "status": "ambiguous",
                "candidates": [
                    {
                        "display_name": v["display_name"],
                        "addresses": [a["address"] for a in v["addrs"]],
                    }
                    for v in by_contact.values()
                ],
            }
        cid, entry = next(iter(by_contact.items()))
        on_channel = [a for a in entry["addrs"] if a["channel"] == channel]
        if not on_channel:
            return {
                "status": "no_channel_address",
                "display_name": entry["display_name"],
                "channels": sorted({a["channel"] for a in entry["addrs"]}),
            }
        on_channel.sort(
            key=lambda a: (
                not a["is_primary"],
                a["created_at"] and -a["created_at"].timestamp(),
            )
        )
        best = on_channel[0]
        return {
            "status": "ok",
            "contact_id": str(cid),
            "display_name": entry["display_name"],
            "address": best["address"],
            "channel": channel,
        }
