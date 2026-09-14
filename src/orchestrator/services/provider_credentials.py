"""User- and project-scoped provider API key storage.

Secret handling is the whole point of this module: a stored provider key is
write-only from the API's perspective. Every read path returns the persisted
row as the store hands it back — which carries ``key_prefix``, never
``api_key`` — and the write paths echo the same row rather than the request
body. Nothing here logs a key or puts one in an error detail; the invalid
provider message deliberately names only the *valid provider set*.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from fastapi import HTTPException

from orchestrator.schemas.provider_catalog import VALID_API_KEY_PROVIDERS, ApiKeySet

#: Project roles allowed to author a project-scoped provider key.
PROJECT_KEY_WRITE_ROLES = ("owner", "editor")


class ProviderCredentialStore(Protocol):
    """The persistence surface these operations need.

    Deliberately narrow: the routes must not be able to reach a store method
    that returns a decrypted key.
    """

    def list_user_api_keys(
        self, user_id: str
    ) -> Awaitable[Sequence[Mapping[str, Any]]]: ...

    def upsert_user_api_key(
        self,
        *,
        user_id: str,
        provider: str,
        api_key: str,
        key_prefix: str,
        label: Any,
    ) -> Awaitable[Mapping[str, Any]]: ...

    def delete_user_api_key(self, user_id: str, provider: str) -> Awaitable[Any]: ...

    def get_project_members(
        self, project_id: str
    ) -> Awaitable[Sequence[Mapping[str, Any]]]: ...

    def list_project_api_keys(
        self, project_id: str
    ) -> Awaitable[Sequence[Mapping[str, Any]]]: ...

    def upsert_project_api_key(
        self,
        *,
        project_id: str,
        provider: str,
        api_key: str,
        key_prefix: str,
        label: Any,
    ) -> Awaitable[Mapping[str, Any]]: ...

    def delete_project_api_key(
        self, project_id: str, provider: str
    ) -> Awaitable[Any]: ...


@dataclass(frozen=True)
class ProviderCredentialDependencies:
    store: ProviderCredentialStore


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Stringify UUID/datetime columns for JSON, preserving every other value.

    The row shape is the store's; this never adds or removes a column, so a
    column the store does not select (``api_key``) cannot appear here.
    """
    return {k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in row.items()}


def _validate_provider(provider: str) -> None:
    """Reject an unknown provider before any key material is persisted."""
    if provider not in VALID_API_KEY_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider '{provider}'. Valid: {sorted(VALID_API_KEY_PROVIDERS)}",
        )


# =============================================================================
# User-scoped keys
# =============================================================================


async def list_user_api_keys(
    *, user_id: str, dependencies: ProviderCredentialDependencies
) -> list[dict[str, Any]]:
    """List the user's API keys (prefix only, no full keys)."""
    rows = await dependencies.store.list_user_api_keys(user_id)
    return [_public_row(r) for r in rows]


async def set_user_api_key(
    *,
    user_id: str,
    provider: str,
    body: ApiKeySet,
    dependencies: ProviderCredentialDependencies,
) -> dict[str, Any]:
    """Set (create or replace) an API key for a provider."""
    _validate_provider(provider)

    key_prefix = body.api_key[:8]
    row = await dependencies.store.upsert_user_api_key(
        user_id=user_id,
        provider=provider,
        api_key=body.api_key,
        key_prefix=key_prefix,
        label=body.label,
    )
    return _public_row(row)


async def delete_user_api_key(
    *, user_id: str, provider: str, dependencies: ProviderCredentialDependencies
) -> dict[str, str]:
    """Delete the user's API key for a provider."""
    deleted = await dependencies.store.delete_user_api_key(user_id, provider)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"No API key for provider '{provider}'"
        )
    return {"status": "deleted"}


# =============================================================================
# Project-scoped keys
# =============================================================================


async def _require_project_membership(
    *, project_id: str, user_id: str, dependencies: ProviderCredentialDependencies
) -> None:
    members = await dependencies.store.get_project_members(project_id)
    if not any(str(m["user_id"]) == user_id for m in members):
        raise HTTPException(status_code=403, detail="Not a member of this project")


async def _require_project_write_role(
    *, project_id: str, user_id: str, dependencies: ProviderCredentialDependencies
) -> None:
    members = await dependencies.store.get_project_members(project_id)
    member = next((m for m in members if str(m["user_id"]) == user_id), None)
    if not member or member["role"] not in PROJECT_KEY_WRITE_ROLES:
        raise HTTPException(status_code=403, detail="Requires owner or editor role")


async def list_project_api_keys(
    *, project_id: str, user_id: str, dependencies: ProviderCredentialDependencies
) -> list[dict[str, Any]]:
    """List a project's API keys (prefix only). Requires project membership."""
    await _require_project_membership(
        project_id=project_id, user_id=user_id, dependencies=dependencies
    )

    rows = await dependencies.store.list_project_api_keys(project_id)
    return [_public_row(r) for r in rows]


async def set_project_api_key(
    *,
    project_id: str,
    provider: str,
    user_id: str,
    body: ApiKeySet,
    dependencies: ProviderCredentialDependencies,
) -> dict[str, Any]:
    """Set (create or replace) a project API key. Requires owner or editor role."""
    _validate_provider(provider)

    await _require_project_write_role(
        project_id=project_id, user_id=user_id, dependencies=dependencies
    )

    key_prefix = body.api_key[:8]
    row = await dependencies.store.upsert_project_api_key(
        project_id=project_id,
        provider=provider,
        api_key=body.api_key,
        key_prefix=key_prefix,
        label=body.label,
    )
    return _public_row(row)


async def delete_project_api_key(
    *,
    project_id: str,
    provider: str,
    user_id: str,
    dependencies: ProviderCredentialDependencies,
) -> dict[str, str]:
    """Delete a project's API key for a provider. Requires owner or editor role."""
    await _require_project_write_role(
        project_id=project_id, user_id=user_id, dependencies=dependencies
    )

    deleted = await dependencies.store.delete_project_api_key(project_id, provider)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"No API key for provider '{provider}'"
        )
    return {"status": "deleted"}
