"""Remove account authority while retaining manifest and execution history."""

from uuid import UUID

from fastapi import HTTPException


async def retire_user_manifests(conn, user_id: UUID) -> bool:
    """Prepare the existing user DELETE in its transaction, without cloud effects.

    Account definitions retire with the account. Project definitions belong to
    their membership scope, so the creator's deletion does not retire them.
    Released workspace receipts and immutable execution revisions remain for
    history; live processes and retained storage must be released first.
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('srw-resource-catalog',0))"
    )
    if not await conn.fetchval("SELECT id FROM users WHERE id=$1 FOR UPDATE", user_id):
        return False

    resources = await conn.fetch(
        "SELECT * FROM srw_resources WHERE owner_id=$1 FOR UPDATE", user_id
    )
    await conn.fetch(
        "SELECT id FROM srw_execution_specs WHERE owner_id=$1 FOR UPDATE", user_id
    )
    workspaces = await conn.fetch(
        """SELECT * FROM srw_workspace_instances WHERE owner_id=$1
        OR execution_id IN (SELECT id FROM srw_execution_specs WHERE owner_id=$1)
        FOR UPDATE""",
        user_id,
    )
    if any(
        row["status"] != "Released" or row["execution_id"] or row["pod_uid"]
        for row in workspaces
    ):
        raise HTTPException(
            409,
            "Release this user's retained workspace instances and fence their processes before deleting the user.",
        )

    retired = [
        row["id"]
        for row in resources
        if row["scope_kind"] == "Account"
        and row["kind"] != "Project"
        and row["deleted_at"] is None
    ]
    retired_text = [str(value) for value in retired]
    if await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM srw_execution_specs s
        LEFT JOIN jobs j ON s.work_kind='Job' AND j.id=s.work_id
        LEFT JOIN threads t ON s.work_kind='Session' AND t.id=s.work_id
        WHERE (s.owner_id=$1 OR s.resource_id=ANY($2::uuid[]) OR EXISTS(
          SELECT 1 FROM jsonb_array_elements(s.dependencies) d
          WHERE d->>'uid'=ANY($3::text[])))
        AND ((j.id IS NOT NULL AND j.status NOT IN ('completed','failed','cancelled'))
          OR (t.id IS NOT NULL AND t.status IS DISTINCT FROM 'ended')
          OR EXISTS(SELECT 1 FROM srw_execution_attempts a WHERE a.execution_id=s.id
            AND (a.cleaned_at IS NULL OR a.phase NOT IN ('Succeeded','Failed','Cancelled')))))""",
        user_id,
        retired,
        retired_text,
    ):
        raise HTTPException(
            409,
            "Finish or cancel this user's manifest work, end its sessions, and fence remaining processes before deleting the user.",
        )
    if retired and await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM srw_resources r WHERE r.deleted_at IS NULL
        AND NOT(r.id=ANY($1::uuid[])) AND (r.managed_by=ANY($1::uuid[]) OR EXISTS(
          SELECT 1 FROM jsonb_array_elements(r.dependencies) d
          WHERE d->>'uid'=ANY($2::text[]))))""",
        retired,
        retired_text,
    ):
        raise HTTPException(
            409,
            "A shared resource still references this account's definitions; repoint it before deleting the user.",
        )

    # A Project-scoped Expert's domain owner is bookkeeping for the existing
    # picker. Preserve its UUID/default links under an already-authorized owner.
    # Account Experts keep the legacy user DELETE cascade instead.
    for row in resources:
        if row["deleted_at"] is not None or row["kind"] != "Expert":
            continue
        if row["scope_kind"] == "Account" or not row["linked_id"]:
            continue
        successor = None
        if row["scope_kind"] == "Project":
            successor = await conn.fetchval(
                """SELECT user_id FROM project_members
                WHERE project_id=$1 AND role='owner' AND user_id<>$2
                ORDER BY added_at,user_id LIMIT 1 FOR SHARE""",
                row["project_id"],
                user_id,
            )
        if successor is None:
            raise HTTPException(
                409,
                "Transfer this user's shared Expert definitions to an existing authorized owner before deleting the user.",
            )
        await conn.execute(
            "UPDATE experts SET owner_id=$2 WHERE id=$1 AND owner_id=$3",
            row["linked_id"],
            successor,
            user_id,
        )
        await conn.execute(
            "UPDATE srw_resources SET owner_id=$2 WHERE id=$1", row["id"], successor
        )

    if await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM experts e WHERE e.owner_id=$1 AND (
          EXISTS(SELECT 1 FROM application_expert_defaults d WHERE d.expert_id=e.id)
          OR EXISTS(SELECT 1 FROM user_expert_defaults d WHERE d.expert_id=e.id AND d.user_id<>$1)
          OR EXISTS(SELECT 1 FROM project_experts d WHERE d.expert_id=e.id
            AND d.default_for IS NOT NULL AND EXISTS(SELECT 1 FROM project_members m
              WHERE m.project_id=d.project_id AND m.user_id<>$1))
          OR EXISTS(SELECT 1 FROM automations a WHERE a.expert_id=e.id AND a.owner_id<>$1)))""",
        user_id,
    ):
        raise HTTPException(
            409,
            "Repoint shared Expert defaults and other users' automations before deleting the user.",
        )

    await conn.execute(
        """UPDATE srw_resources SET deleted_at=now(),updated_at=now(),
        resource_version=resource_version+1 WHERE id=ANY($1::uuid[])""",
        retired,
    )
    await conn.execute(
        "UPDATE srw_resources SET owner_id=NULL WHERE owner_id=$1", user_id
    )
    await conn.execute(
        "UPDATE srw_execution_specs SET owner_id=NULL WHERE owner_id=$1", user_id
    )
    await conn.execute(
        """UPDATE srw_workspace_instances SET owner_id=NULL,ssh_ciphertext=NULL,
        updated_at=now() WHERE owner_id=$1""",
        user_id,
    )
    await conn.execute("DELETE FROM srw_manifest_operations WHERE owner_id=$1", user_id)
    await conn.execute(
        "DELETE FROM srw_resource_secrets WHERE scope_kind='Account' AND scope_name=$1",
        str(user_id),
    )
    # An administrator may have created a secret in another live Account. Its
    # scope, rather than that administrator's provenance, determines ownership.
    await conn.execute(
        """UPDATE srw_resource_secrets SET owner_id=scope_name::uuid
        WHERE owner_id=$1 AND scope_kind='Account'""",
        user_id,
    )
    # The creator is provenance, not the authority for a Project-scoped secret.
    await conn.execute(
        "UPDATE srw_resource_secrets SET owner_id=NULL WHERE owner_id=$1", user_id
    )
    return True
