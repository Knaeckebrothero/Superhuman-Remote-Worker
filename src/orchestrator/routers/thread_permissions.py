"""Thread permission decision and magic-link confirmation routes.

Extracted from ``orchestrator.main`` (R1.B10). Owns the REST permission
resolution endpoint and the magic-link GET/POST/extend HTML flows. The
database handle and the wake-path collaborators are resolved per request
from the owning application — this module imports no application startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from orchestrator.schemas.thread_permissions import ThreadApproveRequest
from orchestrator.security.access import require_thread_owner
from orchestrator.services import headless_notifications
from orchestrator.services import thread_permissions as thread_permissions_service
from orchestrator.services.email import email_service
from orchestrator.services.notification_service import notification_service
from orchestrator.services.thread_permissions import (
    _MAGIC_EXTEND_CAP,
    _magic_link_confirmation_page,
    _magic_link_result_page,
    _phase5_wake_if_suspended,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class ThreadPermissionsDependencies:
    """Per-request collaborators resolved from the owning application."""

    store: Any
    emit_session_provisioning_failure: Callable[..., Any]
    persistent_thread_recycler: Any


def get_thread_permissions_dependencies(
    request: Request,
) -> ThreadPermissionsDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.thread_permissions_dependencies_factory()


@router.post("/api/persistent/threads/{thread_id}/approve/{approval_id}")
async def thread_approve(
    thread_id: str,
    approval_id: str,
    body: ThreadApproveRequest,
    request: Request,
    *,
    dependencies: ThreadPermissionsDependencies = Depends(
        get_thread_permissions_dependencies
    ),
) -> dict[str, Any]:
    """Resolve a pending permission gate by updating thread_permission_requests
    directly. The DB trigger fires NOTIFY → the agent's LISTEN wakes its
    permission_check. No agent forwarding hop — this endpoint is the
    canonical resolution path for magic-link approvals and MCP clients
    alike. The cockpit WS approve method does the same UPDATE inside the
    agent for back-compat.

    Returns:
        200 — request resolved (status flipped)
        400 — invalid decision
        403 — not thread owner
        404 — approval_id not found, or wrong thread, or no pending request
        409 — request already decided (idempotent re-clicks land here)
    """
    user, thread = await require_thread_owner(request, dependencies.store, thread_id)
    decided_by = str(user.get("id") or user.get("sub") or "rest_client")
    outcome = await thread_permissions_service._decide_permission_request(
        thread_id,
        approval_id,
        body.decision,
        decided_by=decided_by,
        db=dependencies.store,
    )
    await notification_service.resolve_source(
        "permission_request", approval_id, resolved_by=f"user:{decided_by}"
    )
    return outcome


@router.get("/magic/approve/{token}")
async def magic_link_get(
    token: str,
    *,
    dependencies: ThreadPermissionsDependencies = Depends(
        get_thread_permissions_dependencies
    ),
) -> HTMLResponse:
    """Show a confirmation page for the magic-link token.

    Does NOT consume the token (POST does). This separation is critical:
    email link previewers (Outlook Safe Links, Gmail) auto-fetch URLs
    server-side; a GET-executes link would be consumed by a bot before
    the human ever clicks.
    """
    cockpit_external_url = email_service.cockpit_url or "http://localhost:4200"

    row = await headless_notifications.validate_magic_link(dependencies.store, token)
    if row is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Link expired or already used",
                body=(
                    "This approval link is no longer valid. It may have "
                    "expired, been used already, or been invalidated by a "
                    "newer approval. Open the cockpit to see the current "
                    "state."
                ),
                cockpit_url=cockpit_external_url,
                is_error=True,
            ),
            status_code=404,
        )

    # Fetch tool details for the confirmation page.
    async with dependencies.store.acquire() as conn:
        permission_row = await conn.fetchrow(
            "SELECT id, tool_name, tool_args, status "
            "FROM thread_permission_requests WHERE id = $1",
            row["approval_id"],
        )

    if permission_row is None or permission_row["status"] != "pending":
        return HTMLResponse(
            _magic_link_result_page(
                title="Already decided",
                body=(
                    "The agent's request has already been resolved. No "
                    "further action is needed."
                ),
                cockpit_url=cockpit_external_url,
            ),
            status_code=409,
        )

    tool_args = permission_row["tool_args"]
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except Exception:
            tool_args = {}
    elif tool_args is None:
        tool_args = {}
    args_preview = json.dumps(tool_args, indent=2, default=str)
    if len(args_preview) > 600:
        args_preview = args_preview[:600] + "\n… (truncated)"

    page = _magic_link_confirmation_page(
        tool_name=permission_row["tool_name"],
        tool_args_preview=args_preview,
        intended_decision=row.get("intended_decision"),
        token=token,
    )
    return HTMLResponse(page)


@router.post("/magic/approve/{token}")
async def magic_link_post(
    token: str,
    *,
    dependencies: ThreadPermissionsDependencies = Depends(
        get_thread_permissions_dependencies
    ),
) -> HTMLResponse:
    """Consume the token and resolve the permission request.

    CAS UPDATE on magic_link_tokens (single-use) + a second UPDATE on
    thread_permission_requests (which the agent's LISTEN picks up via
    the existing trigger). Distinguishes 404 (invalid) from 409 (token
    already used or request already decided) for clean UX on double-clicks.
    """
    cockpit_external_url = email_service.cockpit_url or "http://localhost:4200"

    row = await headless_notifications.validate_magic_link(dependencies.store, token)
    if row is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Link expired or already used",
                body=(
                    "This approval link is no longer valid. It may have "
                    "expired or been used already."
                ),
                cockpit_url=cockpit_external_url,
                is_error=True,
            ),
            status_code=404,
        )

    decision = row.get("intended_decision") or "approved"

    consumed = await headless_notifications.consume_magic_link(
        dependencies.store, str(row["id"]), decision
    )
    if consumed is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Already used",
                body=(
                    "This link has already been used. The agent's request "
                    "is being processed."
                ),
                cockpit_url=cockpit_external_url,
            ),
            status_code=409,
        )

    # Resolve the permission request. CAS-style UPDATE so we don't race
    # with the cockpit having already decided it.
    decided_by_label = "magic_link"
    if consumed.get("user_id"):
        decided_by_label = f"user:{consumed['user_id']}"
    async with dependencies.store.acquire() as conn:
        permission_row = await conn.fetchrow(
            "UPDATE thread_permission_requests "
            "SET status = $2, decided_at = now(), decided_by = $3 "
            "WHERE id = $1 AND status = 'pending' "
            "RETURNING id, status, tool_call_id, tool_name, thread_id",
            consumed["approval_id"],
            decision,
            decided_by_label,
        )

    if permission_row is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Already decided",
                body=(
                    "The agent's request was already resolved by another "
                    "approval path (cockpit click, REST, or expired). "
                    "Your action was not needed."
                ),
                cockpit_url=cockpit_external_url,
            ),
            status_code=409,
        )

    # Phase 5: if attention sleep fired since the email was sent, wake through
    # the thread's existing execution plane. Pinned sessions retain workspace
    # restore + agent-pod re-creation. Stateless sessions retain their exact
    # queued/leased turn and converge the workspace without binding a pod. The
    # permission-row id is the wake task's freshness fence.
    asyncio.create_task(
        _phase5_wake_if_suspended(
            str(permission_row["thread_id"]),
            permission_request_id=str(permission_row["id"]),
            db=dependencies.store,
            emit_session_provisioning_failure=(
                dependencies.emit_session_provisioning_failure
            ),
            persistent_thread_recycler=dependencies.persistent_thread_recycler,
        ),
        name=f"phase5-wake-{str(permission_row['thread_id'])[:8]}",
    )

    pretty = "approved" if decision == "approved" else "denied"
    return HTMLResponse(
        _magic_link_result_page(
            title=f"Tool {pretty}",
            body=(
                f"The agent's request to call "
                f"<code>{permission_row['tool_name']}</code> has been "
                f"{pretty}. The agent will resume shortly."
            ),
            cockpit_url=cockpit_external_url,
        )
    )


@router.post("/magic/extend/{token}")
async def magic_link_extend(
    token: str,
    *,
    dependencies: ThreadPermissionsDependencies = Depends(
        get_thread_permissions_dependencies
    ),
) -> HTMLResponse:
    """Extend the attention-sleep window for the thread bound to this token.

    Validates the token (same hash + expiry + single-use checks as
    /magic/approve) but does NOT consume it — the user is signaling
    "I'm still reviewing" without making the approve decision. Bumps
    threads.awaiting_user_since forward by 60 minutes per click, capped
    at HEADLESS_EXTEND_CAP (default 4 = 4h total ceiling).

    Re-renders the confirmation page with a toast so the user can still
    click approve/deny on the same screen. Status_code 200 throughout —
    the page itself carries the success/cap/not-awaiting signal.

    Why a separate route and not "extend ↔ approve same POST": the
    approve handler consumes the token (single-use CAS). If extend
    shared that path, every extend click would burn the approval token
    and the user couldn't approve afterward.
    """
    cockpit_external_url = email_service.cockpit_url or "http://localhost:4200"

    row = await headless_notifications.validate_magic_link(dependencies.store, token)
    if row is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Link expired or already used",
                body=(
                    "This link is no longer valid. Open the cockpit to "
                    "review the agent's current state."
                ),
                cockpit_url=cockpit_external_url,
                is_error=True,
            ),
            status_code=404,
        )

    thread_id = row.get("thread_id")
    if thread_id is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Cannot extend",
                body="This link is not bound to a thread.",
                cockpit_url=cockpit_external_url,
                is_error=True,
            ),
            status_code=400,
        )

    # Bump awaiting_user_since iff the thread is still in awaiting_user
    # and extend_count < cap. The CAS UPDATE returns the new row state so
    # we can show the right banner. status='active' or 'suspended' means
    # there's nothing to extend — the agent has either woken up already
    # or moved beyond awaiting_user.
    async with dependencies.store.acquire() as conn:
        updated = await conn.fetchrow(
            "UPDATE threads "
            "SET awaiting_user_since = now(), "
            "    extend_count = extend_count + 1 "
            "WHERE id = $1 "
            "  AND status = 'awaiting_user' "
            "  AND extend_count < $2 "
            "RETURNING extend_count",
            str(thread_id),
            _MAGIC_EXTEND_CAP,
        )

    if updated is None:
        # Distinguish cap_reached from not_awaiting for the banner copy.
        async with dependencies.store.acquire() as conn:
            row_state = await conn.fetchrow(
                "SELECT status, extend_count FROM threads WHERE id = $1",
                str(thread_id),
            )
        if row_state is None:
            extend_status = "not_awaiting"
        elif row_state["status"] != "awaiting_user":
            extend_status = "not_awaiting"
        elif row_state["extend_count"] >= _MAGIC_EXTEND_CAP:
            extend_status = "cap_reached"
        else:
            # Edge case — concurrent change between our UPDATE and SELECT.
            # Render not_awaiting which is the gentler banner.
            extend_status = "not_awaiting"
        extends_remaining = None
    else:
        extend_status = "extended"
        extends_remaining = max(0, _MAGIC_EXTEND_CAP - int(updated["extend_count"]))

    # Re-render the confirmation page with the banner. Load the permission
    # row again (status may have changed underneath us).
    approval_id = row.get("approval_id")
    if approval_id is not None:
        async with dependencies.store.acquire() as conn:
            permission_row = await conn.fetchrow(
                "SELECT tool_name, tool_args, status FROM "
                "thread_permission_requests WHERE id = $1",
                approval_id,
            )
    else:
        permission_row = None

    if permission_row is None or permission_row["status"] != "pending":
        return HTMLResponse(
            _magic_link_result_page(
                title="Already decided",
                body=(
                    "The agent's request has been resolved. No further "
                    "action is needed."
                ),
                cockpit_url=cockpit_external_url,
            ),
            status_code=200,
        )

    tool_args = permission_row["tool_args"]
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except Exception:
            tool_args = {}
    elif tool_args is None:
        tool_args = {}
    args_preview = json.dumps(tool_args, indent=2, default=str)
    if len(args_preview) > 600:
        args_preview = args_preview[:600] + "\n… (truncated)"

    page = _magic_link_confirmation_page(
        tool_name=permission_row["tool_name"],
        tool_args_preview=args_preview,
        intended_decision=row.get("intended_decision"),
        token=token,
        extend_status=extend_status,
        extends_remaining=extends_remaining,
    )
    return HTMLResponse(page)
