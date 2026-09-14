"""``/api/actions/pending`` — the cockpit's pending-decision badge.

Extracted from ``orchestrator.main`` (R1.B07 lane M). One route declaration,
moved with its handler name, path, method and docstring intact. It carried no
``tags``, ``response_model``, ``status_code`` or ``dependencies`` list, and
acquires none here; the approval gate is called in the declaration body exactly
as before.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from orchestrator.security.auth import require_approved_user
from orchestrator.services import pending_actions

router = APIRouter()


def get_pending_actions_dependencies(
    request: Request,
) -> pending_actions.PendingActionsDependencies:
    """Resolve collaborators — including this application's own cache."""
    return request.app.state.pending_actions_dependencies_factory()


@router.get("/api/actions/pending")
async def get_pending_actions(request: Request) -> dict[str, Any]:
    """Get counts of pending actions visible to the caller. Cached 5s per user.

    **P4e** — pre-fix this was anonymous and returned global counts AND the
    most-urgent sudo's command string. Now caller must be approved, and
    non-admins see only their own / project-member jobs.
    """
    dependencies = get_pending_actions_dependencies(request)
    caller = await require_approved_user(request, dependencies.store)
    return await pending_actions.get_pending_actions(
        request, dependencies=dependencies, caller=caller
    )


__all__ = ["get_pending_actions", "get_pending_actions_dependencies", "router"]
