"""HTTP adapters for diagnostics with per-app dependencies."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from orchestrator.services import diagnostics

router = APIRouter()


@dataclass(frozen=True)
class DiagnosticsDependencies:
    operations: diagnostics.DiagnosticDependencies
    require_admin: Callable[..., Awaitable[Any]]


def get_diagnostics_dependencies(request: Request) -> DiagnosticsDependencies:
    return request.app.state.diagnostics_dependencies_factory()


# nosec: public k8s-liveness-probe
@router.get("/api/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@router.get("/debug/emails", response_class=HTMLResponse)
async def debug_email_index(
    *, dependencies: DiagnosticsDependencies = Depends(get_diagnostics_dependencies)
) -> str:
    """Dev-only index of rendered transactional emails (Zulip's /emails idea).

    Calls the real builders with fixture data — never fixtures rendered
    independently of the code that ships — so this page cannot drift from
    the emails users actually receive.

    Gated by ``EMAIL_PREVIEW_ENABLED`` (default off, same as
    ``CANVAS_LIVE_PREVIEW_ENABLED`` / ``COLLABORA_ENABLED``): disabled, it
    raises a bare 404 with no detail, matching FastAPI's own default response
    for an unmapped route exactly — not the ``"unknown email preview"`` detail
    used below for a bad name, which would leak that the route exists.
    """
    return await diagnostics.debug_email_index(dependencies=dependencies.operations)


@router.get("/debug/emails/{name}", response_class=HTMLResponse)
async def debug_email_preview(
    name: str,
    *,
    dependencies: DiagnosticsDependencies = Depends(get_diagnostics_dependencies),
) -> str:
    """Render one transactional email through its real builder for inspection.

    Gated by ``EMAIL_PREVIEW_ENABLED`` — see ``debug_email_index`` for why
    the disabled-state 404 has no detail.
    """
    return await diagnostics.debug_email_preview(
        name=name, dependencies=dependencies.operations
    )


@router.get("/api/workspace/status")
async def workspace_status(
    request: Request,
    *,
    dependencies: DiagnosticsDependencies = Depends(get_diagnostics_dependencies),
) -> dict[str, Any]:
    """Get workspace configuration status for debugging.

    **Admin only** (P4a): leaks job UUIDs, filesystem paths, and env-var
    values, so it shouldn't be anonymous. No callers currently rely on it.

    Returns:
        Dict with workspace path, availability, and sample job directories
    """
    await dependencies.require_admin(request)
    return await diagnostics.workspace_status(dependencies=dependencies.operations)
