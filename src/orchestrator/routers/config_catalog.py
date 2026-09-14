"""HTTP adapters for the config catalog application owner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator.schemas.config_catalog import (
    ConfigOverrideCreate,
    ConfigOverrideUpdate,
)
from orchestrator.services.config_catalog import ConfigCatalogService

router = APIRouter()


@dataclass(frozen=True)
class ConfigCatalogDependencies:
    service: ConfigCatalogService
    require_admin: Callable[[Request], Awaitable[dict[str, Any]]]


def get_config_catalog_dependencies(request: Request) -> ConfigCatalogDependencies:
    return request.app.state.config_catalog_dependencies_factory()


@router.get("/api/admin/config/overrides")
async def admin_list_config_overrides(
    request: Request,
    *,
    dependencies: ConfigCatalogDependencies = Depends(get_config_catalog_dependencies),
) -> list[dict[str, Any]]:
    """List all prompt overrides (system-wide)."""
    await dependencies.require_admin(request)
    return await dependencies.service.list_config_overrides()


@router.get("/api/admin/config/overrides/{override_id}")
async def admin_get_config_override(
    request: Request,
    override_id: str,
    *,
    dependencies: ConfigCatalogDependencies = Depends(get_config_catalog_dependencies),
) -> dict[str, Any]:
    """Fetch a single prompt override by id."""
    await dependencies.require_admin(request)
    return await dependencies.service.get_config_override(override_id=override_id)


@router.post("/api/admin/config/overrides")
async def admin_create_config_override(
    request: Request,
    body: ConfigOverrideCreate,
    *,
    dependencies: ConfigCatalogDependencies = Depends(get_config_catalog_dependencies),
) -> dict[str, Any]:
    """Create or replace the override for (family, kind, name)."""
    user = await dependencies.require_admin(request)
    return await dependencies.service.create_config_override(body=body, user=user)


@router.put("/api/admin/config/overrides/{override_id}")
async def admin_update_config_override(
    request: Request,
    override_id: str,
    body: ConfigOverrideUpdate,
    *,
    dependencies: ConfigCatalogDependencies = Depends(get_config_catalog_dependencies),
) -> dict[str, Any]:
    """Update an existing override's payload (family/kind/name are immutable)."""
    user = await dependencies.require_admin(request)
    return await dependencies.service.update_config_override(
        override_id=override_id, body=body, user=user
    )


@router.delete("/api/admin/config/overrides/{override_id}")
async def admin_delete_config_override(
    request: Request,
    override_id: str,
    *,
    dependencies: ConfigCatalogDependencies = Depends(get_config_catalog_dependencies),
) -> dict[str, Any]:
    """Delete an override (reset to the bundled default)."""
    await dependencies.require_admin(request)
    return await dependencies.service.delete_config_override(override_id=override_id)


@router.get("/api/admin/config/catalog")
async def admin_config_catalog(
    request: Request,
    *,
    dependencies: ConfigCatalogDependencies = Depends(get_config_catalog_dependencies),
) -> list[dict[str, Any]]:
    """List the editable prompt keys with human descriptions."""
    await dependencies.require_admin(request)
    return await dependencies.service.config_catalog()


@router.get("/api/admin/config/bundled/{family}/{kind}/{name}")
async def admin_get_bundled_config(
    request: Request,
    family: str,
    kind: str,
    name: str,
    *,
    dependencies: ConfigCatalogDependencies = Depends(get_config_catalog_dependencies),
) -> dict[str, Any]:
    """Return the bundled (shipped) default for a key, plus its catalog entry.

    ``family='_'`` stands in for the global/default family.
    """
    await dependencies.require_admin(request)
    return await dependencies.service.get_bundled_config(
        family=family, kind=kind, name=name
    )
