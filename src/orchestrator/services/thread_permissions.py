"""Thread permission decisions, magic-link pages and phase5 wake helpers.

Extracted from ``orchestrator.main`` (R1.B10). Owns the single UPDATE that
decides a permission gate, the magic-link HTML page builders, the phase5
suspended-thread wake pair, and the permission-notification sweep. The
database handle and wake collaborators are injected by application
composition — this module imports no application startup.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from fastapi import HTTPException
from orchestrator.services import headless_notifications
from orchestrator.services.brand import TRAVERTINE as _BRAND
from orchestrator.services.container_provisioner import container_provisioner
from orchestrator.services.email import email_service
from orchestrator.services.notification_service import notification_service
from orchestrator.services.persistent_provisioner import persistent_provisioner
from orchestrator.services.persistent_recycler import read_recycle_record
from orchestrator.services.session_class_policy import (
    require_stateless_workspace as _require_stateless_workspace,
)
from orchestrator.services.session_provisioner import ensure_session_workspace
from orchestrator.services.session_runtime_admission import (
    same_thread_runtime_authority,
    thread_runtime_authority,
)
from orchestrator.services.session_runtime_identity import (
    thread_uses_pinned_execution as _thread_uses_pinned_execution,
)
from orchestrator.services.workspace_lifecycle import EnsureOutcome
from orchestrator.services.workspace_suspension import workspace_suspension_service
from shared.runtime.core.loader import canonical_config_name

logger = logging.getLogger(__name__)


# Background watcher (thread_permission_notify_sweeper) detects pending
# requests older than 30s with no notification on record and dispatches
# the email via services.headless_notifications.


# Phase 5: per-thread cap on /magic/extend clicks. 4 × 60min = 4h total
# awaiting_user before unconditional suspension. Configurable via env for
# ops tuning during incident response.
_MAGIC_EXTEND_CAP: int = int(os.environ.get("HEADLESS_EXTEND_CAP", "4"))


async def _decide_permission_request(
    thread_id: str,
    approval_id: str,
    decision: str,
    *,
    decided_by: str,
    db: Any,
) -> dict[str, Any]:
    """The one UPDATE that decides a permission gate — shared by the REST
    endpoint and the notification's approve/deny actions. Raises the
    endpoint's HTTP errors: 400 bad decision, 404 unknown, 409 decided."""
    if decision == "approve":
        new_status = "approved"
    elif decision == "deny":
        new_status = "denied"
    else:
        raise HTTPException(
            status_code=400,
            detail="decision must be 'approve' or 'deny'",
        )

    async with db.acquire() as conn:
        # Lookup-then-update so we can distinguish 404 (wrong id/thread)
        # from 409 (already decided).
        existing = await conn.fetchrow(
            "SELECT id, status, tool_call_id FROM thread_permission_requests "
            "WHERE id = $1 AND thread_id = $2",
            approval_id,
            thread_id,
        )
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail="Permission request not found for this thread",
            )
        if existing["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Already {existing['status']}",
            )
        row = await conn.fetchrow(
            "UPDATE thread_permission_requests "
            "SET status = $2, decided_at = now(), decided_by = $3 "
            "WHERE id = $1 AND status = 'pending' "
            "RETURNING id, status, tool_call_id",
            approval_id,
            new_status,
            decided_by,
        )
    if row is None:
        # Lost the race — somebody else just decided this. Idempotency.
        raise HTTPException(
            status_code=409,
            detail="Already decided (race lost)",
        )
    return {
        "accepted": True,
        "decision": decision,
        "approval_id": str(row["id"]),
        "status": row["status"],
        "tool_call_id": row["tool_call_id"],
    }


def _magic_link_confirmation_page(
    *,
    tool_name: str,
    tool_args_preview: str,
    intended_decision: Optional[str],
    token: str,
    extend_status: Optional[str] = None,
    extends_remaining: Optional[int] = None,
) -> str:
    """Render the GET landing page. Single button POSTs back to the same
    URL with the actual decision; this is what prevents email-link
    prefetchers (Outlook Safe Links, Gmail) from auto-consuming tokens.

    Phase 5: a second form lets the user POST /magic/extend/{token} to
    bump the attention-sleep clock by 60 min without consuming the
    approval token. extend_status (when set) drives an inline toast:
    'extended' on success, 'cap_reached' when extend_count >= cap,
    'not_awaiting' when the thread is no longer in awaiting_user.
    """
    # Both values come from the agent's pending tool call and land in element
    # content; the token below lands in an attribute. html.escape(quote=True)
    # covers & < > " ' in one pass — the hand-rolled chains here missed ">" on
    # the tool name and the quotes on both, which is the reflected-XSS hole.
    safe_args = html.escape(tool_args_preview, quote=True)
    safe_tool = html.escape(tool_name, quote=True)
    if intended_decision == "approved":
        button_label = "Confirm: Approve"
        button_color = _BRAND["success"]
    elif intended_decision == "denied":
        button_label = "Confirm: Deny"
        button_color = _BRAND["danger"]
    else:
        button_label = "Confirm decision"
        button_color = _BRAND["accent-color"]

    # The token lands in a form ``action`` attribute. Percent-encoding already
    # removes every character that could close the attribute; escaping the
    # result as well is a no-op on that output but keeps the sanitizer
    # explicit at the sink rather than inferred from the encoder.
    quoted_token = html.escape(urllib.parse.quote(token, safe=""), quote=True)

    # Extend banner copy — friendly, action-specific.
    extend_banner_html = ""
    if extend_status == "extended":
        remaining_str = (
            f" — {extends_remaining} extends remaining"
            if extends_remaining is not None
            else ""
        )
        extend_banner_html = (
            f'<div style="background: {_BRAND["surface-0"]}; border: 1px solid {_BRAND["success"]}; '
            "padding: 10px 12px; margin: 0 0 12px 0; "
            f'color: {_BRAND["success"]}; font-size: 13px;">Window extended by 60 minutes'
            f"{remaining_str}.</div>"
        )
    elif extend_status == "cap_reached":
        extend_banner_html = (
            f'<div style="background: {_BRAND["surface-0"]}; border: 1px solid {_BRAND["text-secondary"]}; '
            "padding: 10px 12px; margin: 0 0 12px 0; "
            f'color: {_BRAND["text-secondary"]}; font-size: 13px;">Extend limit reached — please '
            "approve, deny, or open the cockpit.</div>"
        )
    elif extend_status == "not_awaiting":
        extend_banner_html = (
            f'<div style="background: {_BRAND["surface-0"]}; border: 1px solid {_BRAND["accent-color"]}; '
            "padding: 10px 12px; margin: 0 0 12px 0; "
            f'color: {_BRAND["accent-color"]}; font-size: 13px;">No extend needed — the agent '
            "is already active.</div>"
        )

    # Disable the extend button if we already know the cap was hit.
    #
    # The disabled look MUST be merged into the button's own style attribute.
    # HTML keeps the FIRST style= on an element and ignores every later one,
    # so emitting a second one meant the cap_reached branch -- and only that
    # branch -- rendered a button with opacity/cursor and none of the brand
    # colours, border or type scale.
    _extend_cap_reached = extend_status == "cap_reached"
    extend_disabled_attr = " disabled" if _extend_cap_reached else ""
    extend_button_style = (
        f"background: transparent; color: {_BRAND['accent-color']}; "
        f"padding: 10px 20px; border: 1px solid {_BRAND['accent-color']}; "
        f"font-weight: 600; font-size: 14px; "
        + (
            "opacity: 0.5; cursor: not-allowed;"
            if _extend_cap_reached
            else "cursor: pointer;"
        )
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>SRW — Confirm Decision</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: {_BRAND["app-bg"]}; color: {_BRAND["text-primary"]}; padding: 40px 20px;">
  <div style="max-width: 600px; margin: 0 auto; background: {_BRAND["panel-bg"]}; border: 1px solid {_BRAND["border-color"]}; overflow: hidden;">
    <div style="background: {_BRAND["surface-0"]}; padding: 16px 20px; border-bottom: 1px solid {_BRAND["border-color"]};">
      <h2 style="margin: 0; color: {_BRAND["accent-color"]}; font-size: 16px;">Confirm tool decision</h2>
    </div>
    <div style="padding: 20px; font-size: 14px; line-height: 1.6;">
      {extend_banner_html}
      <p>The agent wants to call <code style="background: {_BRAND["surface-0"]}; padding: 2px 6px;">{safe_tool}</code> with these arguments:</p>
      <pre style="background: {_BRAND["surface-0"]}; padding: 12px; overflow-x: auto; font-size: 12px; color: {_BRAND["success"]};">{safe_args}</pre>
    </div>
    <div style="background: {_BRAND["surface-0"]}; padding: 16px 20px; border-top: 1px solid {_BRAND["border-color"]}; text-align: center;">
      <form method="POST" action="/magic/approve/{quoted_token}" style="display: inline;">
        <button type="submit" style="background: {button_color}; color: {_BRAND["on-accent"]}; padding: 10px 28px; border: 0; cursor: pointer; font-weight: 600; font-size: 14px;">{button_label}</button>
      </form>
      <form method="POST" action="/magic/extend/{quoted_token}" style="display: inline; margin-left: 8px;">
        <button type="submit"{extend_disabled_attr} style="{extend_button_style}">I'm reviewing — extend 60min</button>
      </form>
      <p style="margin: 16px 0 0 0; color: {_BRAND["text-secondary"]}; font-size: 12px;">Approve link is single-use and expires in 30 minutes.</p>
    </div>
  </div>
</body></html>"""


def _magic_link_result_page(
    *,
    title: str,
    body: str,
    cockpit_url: str,
    is_error: bool = False,
) -> str:
    accent = _BRAND["danger"] if is_error else _BRAND["success"]
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>SRW — {title}</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: {_BRAND["app-bg"]}; color: {_BRAND["text-primary"]}; padding: 40px 20px;">
  <div style="max-width: 600px; margin: 0 auto; background: {_BRAND["panel-bg"]}; border: 1px solid {_BRAND["border-color"]}; overflow: hidden;">
    <div style="background: {_BRAND["surface-0"]}; padding: 16px 20px; border-bottom: 1px solid {_BRAND["border-color"]};">
      <h2 style="margin: 0; color: {accent}; font-size: 16px;">{title}</h2>
    </div>
    <div style="padding: 20px; font-size: 14px; line-height: 1.6;">
      <p>{body}</p>
      <p style="margin-top: 16px;"><a href="{cockpit_url}" style="color: {_BRAND["accent-color"]};">Open the cockpit</a></p>
    </div>
  </div>
</body></html>"""


async def _phase5_wake_stateless_if_suspended(
    thread_id: str,
    *,
    permission_request_id: str | None,
    db: Any,
) -> None:
    """Wake one queue-served permission continuation without binding a pod.

    A magic-link task can run well after its originating request was resolved.
    Revalidate every authority under the global ``threads -> run_queue`` lock
    order: exact stateless lane/class/tier, no pinned-agent binding, the exact
    terminal permission row, and a queued/leased session turn whose human input
    is still unconsumed.  ``done`` is deliberately not revived: no durable
    permission-continuation watermark exists yet, so a done row would hit the
    executor's skip-if-answered edge and falsely claim the tool resumed.

    The queue row itself is left untouched.  A live lease keeps ownership; a
    queued retry keeps its token/fairness/affinity.  Workspace convergence uses
    the owner-keyed session provisioner, which restores a Kubernetes sandbox,
    refreshes a virtual binding, and is a no-op for ``none``.  It never creates
    a persistent agent pod.
    """
    from shared.run_queue import (
        LANE_STATELESS,
        STATE_LEASED,
        STATE_QUEUED,
        UNIT_KIND_SESSION_TURN,
    )

    if permission_request_id is None:
        logger.warning(
            "magic-link wake: refusing unfenced stateless wake for thread %s",
            thread_id,
        )
        return

    should_ensure_workspace = False
    async with db.acquire() as conn:
        async with conn.transaction():
            locked_thread = await conn.fetchrow(
                "SELECT id, execution_lane, agent_id, status, metadata "
                "FROM threads WHERE id = $1::uuid FOR UPDATE",
                thread_id,
            )
            if locked_thread is None:
                return
            thread = dict(locked_thread)
            if (
                thread.get("execution_lane") != LANE_STATELESS
                or thread.get("agent_id") is not None
            ):
                logger.warning(
                    "magic-link wake: stateless authority moved for thread %s "
                    "(lane=%r agent_id=%r)",
                    thread_id,
                    thread.get("execution_lane"),
                    thread.get("agent_id"),
                )
                return
            try:
                _require_stateless_workspace(thread)
            except HTTPException as exc:
                logger.warning(
                    "magic-link wake: refusing stateless workspace/class for "
                    "thread %s: %s",
                    thread_id,
                    exc.detail,
                )
                return

            # Keep the repository-wide threads -> run_queue lock order.  The
            # lock makes the pending-input test atomic with a concurrent claim,
            # completion, release or reaper steal.
            queue = await conn.fetchrow(
                "SELECT state, input_seq, consumed_seq "
                "FROM run_queue "
                "WHERE unit_id = $1::uuid AND unit_kind = $2 "
                "FOR UPDATE",
                thread_id,
                UNIT_KIND_SESSION_TURN,
            )
            if queue is None:
                logger.warning(
                    "magic-link wake: no session queue authority for thread %s",
                    thread_id,
                )
                return
            queue_state = str(queue["state"] or "")
            input_seq = queue["input_seq"]
            consumed_seq = queue["consumed_seq"]
            has_unconsumed_input = input_seq is not None and (
                consumed_seq is None or int(input_seq) > int(consumed_seq)
            )
            if (
                queue_state not in {STATE_QUEUED, STATE_LEASED}
                or not has_unconsumed_input
            ):
                logger.warning(
                    "magic-link wake: refusing stale stateless continuation for "
                    "thread %s (queue_state=%s input_seq=%r consumed_seq=%r)",
                    thread_id,
                    queue_state,
                    input_seq,
                    consumed_seq,
                )
                return

            decision = await conn.fetchval(
                "SELECT status FROM thread_permission_requests "
                "WHERE id = $2::uuid AND thread_id = $1::uuid "
                "  AND status IN ('approved', 'denied')",
                thread_id,
                permission_request_id,
            )
            if decision not in {"approved", "denied"}:
                logger.warning(
                    "magic-link wake: exact permission fence rejected thread %s "
                    "request %s",
                    thread_id,
                    permission_request_id,
                )
                return

            thread_status = str(thread.get("status") or "")
            if thread_status not in {"active", "awaiting_user", "suspended"}:
                logger.warning(
                    "magic-link wake: thread %s is not resumable (status=%r)",
                    thread_id,
                    thread_status,
                )
                return

            if thread_status in {"awaiting_user", "suspended"}:
                updated = await conn.fetchval(
                    "UPDATE threads "
                    "SET status = 'active', "
                    "    awaiting_user_since = NULL, "
                    "    extend_count = 0, "
                    "    control_admission_agent_id = NULL "
                    "WHERE id = $1::uuid "
                    "  AND execution_lane = $2 "
                    "  AND agent_id IS NULL "
                    "  AND status IN ('suspended', 'awaiting_user') "
                    "RETURNING id",
                    thread_id,
                    LANE_STATELESS,
                )
                if updated is None:
                    return
            should_ensure_workspace = True

    if not should_ensure_workspace:
        return
    # Queue/lifecycle admission commits before this potentially slow side
    # effect.  A claimant may arrive first, but its attach path polls the same
    # durable workspace lifecycle until it is ready.
    await ensure_session_workspace(
        thread_id,
        db=db,
        provisioner=container_provisioner,
        suspension=workspace_suspension_service,
    )
    logger.info(
        "magic-link wake: stateless permission continuation admitted for "
        "thread %s request %s",
        thread_id,
        permission_request_id,
    )


async def _phase5_wake_if_suspended(
    thread_id: str,
    *,
    permission_request_id: str | None = None,
    db: Any,
    emit_session_provisioning_failure: Callable[..., Awaitable[Any]],
    persistent_thread_recycler: Any,
) -> None:
    """Wake a suspended thread after a magic-link decision.

    Fire-and-forget — the HTTP response has already returned. Stateless
    sessions delegate to the queue-fenced, topology-neutral helper above.
    Pinned sessions preserve the historical resume pattern: restore from S3,
    then spawn the agent pod if the persistent provisioner is wired.
    """
    try:
        thread = await db.get_thread(thread_id)
        if not thread:
            return
        if thread.get("execution_lane") == "stateless":
            await _phase5_wake_stateless_if_suspended(
                thread_id,
                permission_request_id=permission_request_id,
                db=db,
            )
            return
        if not _thread_uses_pinned_execution(thread):
            logger.warning(
                "magic-link wake: refusing pinned wake for thread %s on "
                "execution lane %r",
                thread_id,
                thread.get("execution_lane"),
            )
            return
        wake_authority = thread_runtime_authority(thread)
        if wake_authority is None:
            return
        metadata = thread.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        recycle = read_recycle_record(metadata)
        if isinstance(recycle, dict) and recycle.get("phase") not in {
            None,
            "",
            "complete",
            "cancelled",
        }:
            if persistent_thread_recycler is not None:
                await persistent_thread_recycler.request_and_reconcile(
                    thread_id=thread_id,
                    reason="resume_during_recycle",
                    expected_build_sha=persistent_provisioner.expected_build_sha,
                    expected_project_id=(
                        str(thread.get("project_id"))
                        if thread.get("project_id")
                        else None
                    ),
                )
            return
        ws_ctx = metadata.get("workspace_container") or {}
        ws_status = ws_ctx.get("status")
        if ws_status == "suspended" and workspace_suspension_service.is_enabled:
            logger.info(
                "magic-link wake: restoring suspended workspace for thread %s",
                thread_id,
            )
            restored = await ensure_session_workspace(
                thread_id,
                db=db,
                provisioner=container_provisioner,
                suspension=workspace_suspension_service,
                expected_runtime_generation=wake_authority.generation,
            )
            if restored is None or restored.outcome is EnsureOutcome.FAILED:
                logger.warning(
                    "magic-link wake: workspace restore failed or lost authority "
                    "for thread %s",
                    thread_id,
                )
                return

        # Publish wake only to the exact post-suspension generation. A G2
        # restore delayed across another End/Resume cannot wake G3.
        async with db.acquire() as conn:
            woke = await conn.fetchval(
                "UPDATE threads "
                "SET status = 'active', "
                "    awaiting_user_since = NULL, "
                "    extend_count = 0, "
                "    control_admission_agent_id = NULL "
                "WHERE id = $1::uuid "
                "  AND execution_lane='pinned' "
                "  AND runtime_generation=$2::uuid "
                "  AND runtime_retirement_token IS NULL "
                "  AND status IN ('suspended', 'awaiting_user') "
                "RETURNING id",
                thread_id,
                wake_authority.generation,
            )
        if woke is None and not same_thread_runtime_authority(
            await db.get_thread(thread_id), wake_authority
        ):
            return

        # Agent pod may also have been deleted on suspension
        # (workspace_suspension.py:502-504). Re-provision if a persistent
        # provisioner is configured. fire-and-forget — the agent's boot
        # will restore the LangGraph checkpoint and re-enter permission_check
        # for the same tool_call_id, where the select-first guard picks up
        # the decision we just UPDATEd.
        current = await db.get_thread(thread_id)
        if not same_thread_runtime_authority(current, wake_authority):
            return
        if persistent_provisioner is not None and not current.get("agent_id"):
            config_name = canonical_config_name(
                thread.get("config_name", "session_base")
            )

            async def _create_after_magic_link() -> None:
                # This closure sits lexically inside the wake handler's
                # try/except, but it is scheduled as its own task — so that
                # handler NEVER sees anything raised here. Its own guard is the
                # only thing between a raise and a silently vanished wake.
                try:
                    result = await persistent_provisioner.create_agent_pod(
                        thread_id,
                        config_name=config_name,
                        expected_runtime_generation=wake_authority.generation,
                    )
                    if not result.usable:
                        logger.warning(
                            "magic-link persistent provisioning for thread %s "
                            "is %s (%s)",
                            thread_id,
                            result.status.value,
                            result.failure_class or "no-detail",
                        )
                        await emit_session_provisioning_failure(
                            thread_id,
                            str(thread.get("user_id") or "") or None,
                            wake_authority,
                            f"magic-link wake provisioning {result.status.value}"
                            f" ({result.failure_class or 'no-detail'})",
                        )
                except Exception as exc:
                    logger.exception(
                        "magic-link persistent provisioning for thread %s raised: %s",
                        thread_id,
                        exc,
                    )
                    await emit_session_provisioning_failure(
                        thread_id,
                        str(thread.get("user_id") or "") or None,
                        wake_authority,
                        str(exc),
                    )

            asyncio.create_task(
                _create_after_magic_link(),
                name=f"phase5-create-agent-{thread_id[:8]}",
            )
    except Exception as e:
        logger.warning(
            "magic-link wake task failed for thread %s: %s",
            thread_id,
            e,
        )


async def thread_permission_notify_sweeper(
    shutdown_event: asyncio.Event,
    *,
    db: Any,
) -> None:
    """Background task: a permission request that has waited longer than
    HEADLESS_NOTIFY_AGE_S without a decision becomes a ``session_permission``
    feed row for the thread owner — ``high``, so the mail (with the two magic
    links) goes out now, and the row resolves when the gate is decided by any
    path. In-session gates are answered within seconds through the agent's
    LISTEN, so only abandoned ones ever get here.

    Runs every HEADLESS_NOTIFY_INTERVAL_S (default 30s). Idempotent: the
    feed row is keyed on the request id, and rows already recorded are
    filtered out so the magic-link tokens are minted once.

    Best-effort. Survives transient errors by logging and continuing.
    """
    interval_s = int(os.environ.get("HEADLESS_NOTIFY_INTERVAL_S", "30"))
    age_threshold_s = int(os.environ.get("HEADLESS_NOTIFY_AGE_S", "30"))
    logger.info(
        "Headless permission-notify sweeper started (interval=%ds, age_threshold=%ds)",
        interval_s,
        age_threshold_s,
    )
    cockpit_external_url = email_service.cockpit_url or "http://localhost:4200"

    while not shutdown_event.is_set():
        try:
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT r.id, r.thread_id, r.tool_name, r.tool_args, "
                    "       r.requested_at, t.user_id, t.title "
                    "FROM thread_permission_requests r "
                    "JOIN threads t ON t.id = r.thread_id "
                    "WHERE r.status = 'pending' "
                    "  AND r.requested_at < now() - ($1::int * interval '1 second') "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM notifications n "
                    "    WHERE n.source_kind = 'permission_request' "
                    "      AND n.source_id = r.id::text"
                    "  ) "
                    "ORDER BY r.requested_at ASC "
                    "LIMIT 50",
                    age_threshold_s,
                )
            for row in rows:
                try:
                    result = await headless_notifications.record_permission_pending(
                        db,
                        notification_service,
                        row=dict(row),
                        cockpit_external_url=cockpit_external_url,
                    )
                    if result.get("status") == "recorded":
                        logger.info(
                            "Recorded permission-pending notification "
                            "(thread=%s req=%s)",
                            str(row["thread_id"])[:8],
                            str(row["id"])[:8],
                        )
                except Exception as e:
                    logger.warning(
                        "Permission-pending notification failed (req=%s): %s",
                        str(row["id"])[:8],
                        e,
                    )
        except Exception as e:
            logger.warning("headless permission-notify sweep error: %s", e)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval_s))
            break
        except asyncio.TimeoutError:
            pass
    logger.info("Headless permission-notify sweeper stopped")
