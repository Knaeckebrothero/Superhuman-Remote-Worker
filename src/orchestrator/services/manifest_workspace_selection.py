"""Workspace selections shared by the existing SRW Job and Session APIs."""

from copy import deepcopy
from typing import Any

from fastapi import HTTPException

from orchestrator.services.manifest_authority import ManifestAuthority
from orchestrator.services.manifest_workspace_binding import (
    validate_workspace_selection,
)
from orchestrator.services.manifest_resolution import LiveManifestResolver
from orchestrator.services.manifest_store import ManifestStore
from shared.runtime.core.workspace_selection import execution_workspace_config


def srw_workspace_config(workspace: dict | None) -> dict:
    """Render supported workspace recipes; never silently discard recipe fields."""
    if workspace is None:
        return {"backend": "none"}
    recipe = workspace.get("template", {}).get("inline")
    if (
        not isinstance(recipe, dict)
        or set(recipe) - {"backend", "retention"}
        or recipe.get("retention", "Delete") != "Delete"
    ):
        raise HTTPException(
            422,
            "The SRW workspace provisioner currently supports backend-only templates "
            "with Delete retention. Prepared/initialized recipes, retained instances "
            "and instanceRef require a supported workspace provisioner.",
        )
    return execution_workspace_config({"workspace": recipe})


async def select_execution_workspace(
    db: Any,
    user: dict,
    *,
    project_id: str | None,
    role: str,
    workspace: dict | None = None,
    supplied: bool = False,
    config_override: dict | None = None,
    account_defaults: dict | None = None,
    request: Any = None,
) -> tuple[dict, dict | None]:
    """Explicit selection > Project default > account/role fallback.

    The returned receipt carries frozen workspace and Project revisions into
    the insertion transaction. A recommendation never enters this function.
    Legacy config_override.workspace remains an explicit execution input.
    """
    from orchestrator.services.manifest_projects import (
        active_project_resource,
        source_recipe,
    )

    fallback = execution_workspace_config(account_defaults, config_override, role=role)
    legacy = (config_override or {}).get("workspace") or {}
    if supplied and "backend" in legacy:
        raise HTTPException(
            422,
            "Select workspace once; do not also set config_override.workspace.backend.",
        )
    if not supplied and "backend" in legacy:
        return fallback, None
    authority = ManifestAuthority(db, user, request=request)
    resolver = LiveManifestResolver(ManifestStore(db), authority)
    scope = {"kind": "Project", "name": project_id} if project_id else authority.account
    dependencies: list[dict] = []
    project_revision = None
    if not supplied and project_id:
        project = await active_project_resource(db, project_id)
        if project:
            defaults = project["resolved"]["spec"].get("defaults", {})
            if "workspace" in defaults:
                alias = defaults["workspace"]
                workspace = (
                    {
                        "template": deepcopy(
                            project["resolved"]["spec"]["resources"]["workspaces"][
                                alias
                            ]
                        )
                    }
                    if alias is not None
                    else None
                )
            else:
                # A versioned legacy Project source is already frozen. Its old
                # workspace default belongs to the Project, not to its Experts.
                shared = (source_recipe(project) or {}).get("sharedConfig", {})
                backend = (shared.get("workspace") or {}).get("backend")
                if backend is None:
                    return fallback, None
                workspace = (
                    None
                    if backend == "none"
                    else {"template": {"inline": {"backend": backend}}}
                )
            await authority.resource(project)
            await resolver.authorize_dependencies(project["dependencies"])
            dependencies = deepcopy(project["dependencies"])
            dependencies.append(
                {"uid": str(project["id"]), "revision": project["revision"]}
            )
            project_revision = project["revision"]
            supplied = True
    if not supplied:
        return fallback, None
    validate_workspace_selection(workspace)
    resolved = deepcopy(workspace)
    if resolved and "template" in resolved:
        resolved["template"] = await resolver.selection(
            "WorkspaceTemplate", resolved["template"], scope, dependencies
        )
    config = srw_workspace_config(resolved)
    return config, {
        "document": deepcopy(workspace),
        "resolved": resolved,
        "dependencies": dependencies,
        "project_id": project_id if project_revision else None,
        "project_revision": project_revision,
    }


async def verify_workspace_selection(
    db: Any, selection: dict, owner_id: str | None
) -> None:
    """Recheck current access and the active Project at the atomic write boundary."""
    from orchestrator.services.manifest_projects import active_project_resource

    if selection.get("project_revision"):
        project = await active_project_resource(db, selection["project_id"])
        if project is None or project["revision"] != selection["project_revision"]:
            raise HTTPException(
                409, "The Project changed during workspace selection; submit again."
            )
    if selection.get("dependencies"):
        user = await db.get_user(owner_id) if owner_id else None
        if not user:
            raise HTTPException(409, "The workspace selection owner is unavailable.")
        resolver = LiveManifestResolver(ManifestStore(db), ManifestAuthority(db, user))
        await resolver.authorize_dependencies(selection["dependencies"])


async def select_project_workspace_default(db, owner_id, project_id, config_override):
    """Unattended callers choose the workspace before selecting connectors/grants."""
    if (
        not owner_id
        or not project_id
        or "backend" in ((config_override or {}).get("workspace") or {})
    ):
        return config_override, None
    project = await db.get_project(str(project_id))
    if not project or project.get("manifest_composed") is not True:
        return config_override, None
    user = await db.get_user(str(owner_id))
    if not user:
        raise HTTPException(409, "The execution owner is unavailable.")
    config, selection = await select_execution_workspace(
        db,
        user,
        project_id=str(project_id),
        role="worker",
        config_override=config_override,
    )
    if selection is None:
        return config_override, None
    from shared.runtime.core.workspace_selection import bind_execution_workspace

    return bind_execution_workspace(config_override or {}, config), selection
