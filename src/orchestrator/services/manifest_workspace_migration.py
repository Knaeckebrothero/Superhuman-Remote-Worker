"""Reviewed, versioned migration of SRW Expert backend pins to preferences."""

from fastapi import HTTPException

from orchestrator.services.manifest_experts import sync_expert_identity
from orchestrator.services.manifest_store import ManifestStore, decoded
from shared.manifests import preview_documents
from shared.manifests.resolution import content_revision
from shared.runtime.core.workspace_selection import migrate_expert_workspace_preference


async def migrate_workspace_preferences(db, *, apply=False, plan_revision=None):
    store = ManifestStore(db)
    async with db.transaction_scope():
        await store.lock_catalog()
        rows = [
            decoded(row)
            for row in await db.fetch(
                "SELECT * FROM srw_resources WHERE kind='Expert' AND deleted_at IS NULL ORDER BY id"
            )
        ]
        changes = []
        managed = []
        for row in rows:
            document = migrate_expert_workspace_preference(row["document"])
            if document == row["document"]:
                continue
            if row.get("managed_by"):
                managed.append(str(row["id"]))
                continue
            resolved = preview_documents([document])["resolved"][0]
            changes.append((row, document, resolved))
        plan = [
            {
                "uid": str(row["id"]),
                "resourceVersion": row["resource_version"],
                "from": row["revision"],
                "to": content_revision(resolved["spec"]),
            }
            for row, _, resolved in changes
        ]
        revision = content_revision({"changes": plan, "managed": managed})
        if apply:
            if plan_revision != revision:
                raise HTTPException(
                    409,
                    "Workspace migration plan changed; preview again before applying.",
                )
            for row, document, resolved in changes:
                saved, changed = await store.save(
                    document,
                    resolved,
                    content_revision(resolved["spec"]),
                    row["dependencies"],
                    owner_id=row.get("owner_id"),
                    project_id=row.get("project_id"),
                    linked_id=row.get("linked_id"),
                    expected_version=row["resource_version"],
                )
                if changed:
                    await sync_expert_identity(db, saved)
        return {
            "operation": "apply" if apply else "preview",
            "planRevision": revision,
            "changes": plan,
            "managedExpertsRequireProjectUpdate": managed,
            "effects": ["new Expert revisions"] if apply and plan else [],
        }
