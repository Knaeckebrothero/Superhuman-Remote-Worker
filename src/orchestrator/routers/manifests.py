"""Authenticated manifest configuration operations; no resource mutations."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from orchestrator.schemas.manifests import (
    ManifestExportInput,
    ManifestInput,
    ManifestPreviewInput,
)
from orchestrator.services.manifests import ManifestService
from shared.manifests import ManifestError

router = APIRouter(tags=["Manifests"])


@dataclass(frozen=True)
class ManifestDependencies:
    service: ManifestService
    require_approved_user: Callable[[Request], Awaitable[dict[str, Any]]]


def get_manifest_dependencies(request: Request) -> ManifestDependencies:
    return request.app.state.manifest_dependencies_factory()


async def _call(operation, *args, **kwargs):
    try:
        return await run_in_threadpool(operation, *args, **kwargs)
    except ManifestError as exc:
        raise HTTPException(status_code=422, detail=exc.as_dict()) from None


@router.get("/api/manifests/schema")
async def manifest_schema(
    request: Request,
    *,
    dependencies: ManifestDependencies = Depends(get_manifest_dependencies),
) -> dict:
    """Return the packaged v1alpha1 authored-resource JSON Schema."""
    await dependencies.require_approved_user(request)
    return await _call(dependencies.service.schema)


@router.post("/api/manifests/validate")
async def validate_manifests(
    request: Request,
    body: ManifestInput,
    *,
    dependencies: ManifestDependencies = Depends(get_manifest_dependencies),
) -> dict:
    """Check JSON/YAML structure and project-local aliases without resolving access."""
    await dependencies.require_approved_user(request)
    return await _call(dependencies.service.validate, body.source, format=body.format)


@router.post("/api/manifests/preview")
async def preview_manifests(
    request: Request,
    body: ManifestPreviewInput,
    *,
    dependencies: ManifestDependencies = Depends(get_manifest_dependencies),
) -> dict:
    """Resolve only definitions supplied in the bundle. No live admission checks.

    Scope names describe authored configuration; they grant no access. Stored
    manifests, credentials, images and workspace instances are not fetched.
    This response cannot be used as an admission token or an apply operation.
    """
    await dependencies.require_approved_user(request)
    return await _call(
        dependencies.service.preview,
        body.source,
        format=body.format,
        default_scope=body.default_scope.model_dump() if body.default_scope else None,
    )


@router.post("/api/manifests/export")
async def export_manifests(
    request: Request,
    body: ManifestExportInput,
    *,
    dependencies: ManifestDependencies = Depends(get_manifest_dependencies),
) -> dict:
    """Export authored ownership/references with explicit scopes, without deployment."""
    await dependencies.require_approved_user(request)
    return await _call(
        dependencies.service.export,
        body.source,
        format=body.format,
        default_scope=body.default_scope.model_dump() if body.default_scope else None,
        output_format=body.output_format,
    )
