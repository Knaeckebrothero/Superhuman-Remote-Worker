"""HTTP adapters for user- and project-scoped provider API keys.

Reads are prefix-only by construction (see
:mod:`orchestrator.services.provider_credentials`); a write echoes the stored
row, never the submitted key.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator.schemas.provider_catalog import ApiKeySet
from orchestrator.security.auth import require_approved_user
from orchestrator.services import provider_credentials

router = APIRouter()


@dataclass(frozen=True)
class ProviderCredentialsDependencies:
    store: Any
    operations: provider_credentials.ProviderCredentialDependencies
    require_approved_user: Callable[..., Awaitable[Any]] = require_approved_user


def get_provider_credentials_dependencies(
    request: Request,
) -> ProviderCredentialsDependencies:
    return request.app.state.provider_credentials_dependencies_factory()


# =============================================================================
# User Settings & API Key Endpoints
# =============================================================================


@router.get("/api/settings/api-keys")
async def list_user_api_keys(
    request: Request,
    *,
    dependencies: ProviderCredentialsDependencies = Depends(
        get_provider_credentials_dependencies
    ),
) -> list[dict[str, Any]]:
    """List the current user's API keys (prefix only, no full keys)."""
    user = await dependencies.require_approved_user(request, dependencies.store)
    return await provider_credentials.list_user_api_keys(
        user_id=str(user["id"]), dependencies=dependencies.operations
    )


@router.put("/api/settings/api-keys/{provider}")
async def set_user_api_key(
    request: Request,
    provider: str,
    body: ApiKeySet,
    *,
    dependencies: ProviderCredentialsDependencies = Depends(
        get_provider_credentials_dependencies
    ),
) -> dict[str, Any]:
    """Set (create or replace) an API key for a provider."""
    user = await dependencies.require_approved_user(request, dependencies.store)
    return await provider_credentials.set_user_api_key(
        user_id=str(user["id"]),
        provider=provider,
        body=body,
        dependencies=dependencies.operations,
    )


@router.delete("/api/settings/api-keys/{provider}")
async def delete_user_api_key(
    request: Request,
    provider: str,
    *,
    dependencies: ProviderCredentialsDependencies = Depends(
        get_provider_credentials_dependencies
    ),
) -> dict[str, str]:
    """Delete the current user's API key for a provider."""
    user = await dependencies.require_approved_user(request, dependencies.store)
    return await provider_credentials.delete_user_api_key(
        user_id=str(user["id"]),
        provider=provider,
        dependencies=dependencies.operations,
    )


# =============================================================================
# Project API Key Endpoints
# =============================================================================


@router.get("/api/projects/{project_id}/api-keys")
async def list_project_api_keys(
    request: Request,
    project_id: str,
    *,
    dependencies: ProviderCredentialsDependencies = Depends(
        get_provider_credentials_dependencies
    ),
) -> list[dict[str, Any]]:
    """List a project's API keys (prefix only). Requires project membership."""
    user = await dependencies.require_approved_user(request, dependencies.store)
    return await provider_credentials.list_project_api_keys(
        project_id=project_id,
        user_id=str(user["id"]),
        dependencies=dependencies.operations,
    )


@router.put("/api/projects/{project_id}/api-keys/{provider}")
async def set_project_api_key(
    request: Request,
    project_id: str,
    provider: str,
    body: ApiKeySet,
    *,
    dependencies: ProviderCredentialsDependencies = Depends(
        get_provider_credentials_dependencies
    ),
) -> dict[str, Any]:
    """Set (create or replace) a project API key. Requires owner or editor role."""
    user = await dependencies.require_approved_user(request, dependencies.store)
    return await provider_credentials.set_project_api_key(
        project_id=project_id,
        provider=provider,
        user_id=str(user["id"]),
        body=body,
        dependencies=dependencies.operations,
    )


@router.delete("/api/projects/{project_id}/api-keys/{provider}")
async def delete_project_api_key(
    request: Request,
    project_id: str,
    provider: str,
    *,
    dependencies: ProviderCredentialsDependencies = Depends(
        get_provider_credentials_dependencies
    ),
) -> dict[str, str]:
    """Delete a project's API key for a provider. Requires owner or editor role."""
    user = await dependencies.require_approved_user(request, dependencies.store)
    return await provider_credentials.delete_project_api_key(
        project_id=project_id,
        provider=provider,
        user_id=str(user["id"]),
        dependencies=dependencies.operations,
    )
