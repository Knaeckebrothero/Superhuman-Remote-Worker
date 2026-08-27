"""Email magic-link generation + outbound notification dispatch for
headless persistent sessions.

Phase 4 of knowledge-base/knowledge/features/headless_persistent_sessions.md. When the agent's
permission_check inserts a row into thread_permission_requests and no
client has been attached for >30s, the permission-pending watcher
(orchestrator/main.py) calls send_permission_pending_email() here. We:

  1. Look up the thread owner and their email.
  2. Check dedup ((thread_id, request_id) already emailed) and rate-limit
     (5/hr and 1/5min per thread).
  3. Generate two magic-link tokens — one for "approve", one for "deny".
     Plaintext lives only in the email body; the DB stores SHA-256 hashes.
  4. Compose and send the email (subject + plaintext + HTML).
  5. Record the send in thread_notifications for audit + future dedup.

Magic-link click flow (HTTP routes in orchestrator/main.py):

  GET /magic/approve/{token}
    → hash + look up the token row → render confirmation HTML showing
      the tool, args, and a single POST button. We deliberately do NOT
      execute the decision on GET because email-link prefetchers (Outlook
      Safe Links, Gmail link preview) auto-fetch every link.

  POST /magic/approve/{token}
    → validate_magic_link → consume_magic_link (CAS UPDATE used_at) →
      UPDATE thread_permission_requests via the same DB trigger that the
      cockpit and REST paths use → render success HTML.

Token security:
  - Opaque random 32-byte token (secrets.token_urlsafe). NOT a JWT —
    single-use enforcement forces server state anyway, and JWT adds
    algorithm-confusion footguns without compensating benefit.
  - DB stores SHA-256(token) hex. A DB leak does not yield usable tokens.
  - 30-minute expiry. AWS Step Functions allows up to 30 days for slow
    approval flows, but persistent-session permission requests are
    expected to be acted on quickly.
  - Bound to a specific approval_id. A leaked token cannot be replayed
    against a different request.
  - Single-use via CAS: UPDATE ... WHERE used_at IS NULL. Double-clicks
    land on the "already used" path with a friendly response.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from services.brand import TRAVERTINE as _C
from services.email_layout import Action, escape_text, render_email

logger = logging.getLogger(__name__)


# Module-level config knobs. Overridable via env for ops tuning without
# changing call sites.
MAGIC_LINK_TTL_SECONDS: int = int(os.environ.get("MAGIC_LINK_TTL_S", "1800"))  # 30 min
NOTIFICATION_RATE_LIMIT_5MIN: int = int(os.environ.get("HEADLESS_RATE_5MIN", "1"))
NOTIFICATION_RATE_LIMIT_HOUR: int = int(os.environ.get("HEADLESS_RATE_HOUR", "5"))


def _hash_token(raw_token: str) -> str:
    """SHA-256 of the raw token, hex-encoded. The only form stored in DB."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def generate_magic_link_token(
    db: Any,
    *,
    purpose: str,
    user_id: Optional[str],
    approval_id: Optional[str],
    thread_id: Optional[str],
    intended_decision: Optional[str] = None,
    ttl_seconds: int = MAGIC_LINK_TTL_SECONDS,
) -> tuple[str, str]:
    """Generate a fresh opaque token, store its hash, return (raw, token_id).

    The raw token is what we embed in the email href. token_id is the row
    UUID, useful for logging / observability. The raw token is never
    persisted in plaintext — only its hash is.
    """
    if intended_decision is not None and intended_decision not in (
        "approved",
        "denied",
    ):
        raise ValueError(
            f"intended_decision must be 'approved'/'denied'/None, got "
            f"{intended_decision!r}"
        )
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = _now_utc() + timedelta(seconds=ttl_seconds)

    async with db.acquire() as conn:
        row_id = await conn.fetchval(
            "INSERT INTO magic_link_tokens "
            "(token_hash, purpose, user_id, approval_id, thread_id, "
            " intended_decision, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "RETURNING id",
            token_hash,
            purpose,
            user_id,
            approval_id,
            thread_id,
            intended_decision,
            expires_at,
        )
    return raw, str(row_id)


async def validate_magic_link(db: Any, raw_token: str) -> Optional[dict[str, Any]]:
    """Hash the incoming token, look up the row. Returns the row dict if
    the token is valid (exists, not expired, not used) — else None.

    Does NOT consume the token. Callers should call consume_magic_link()
    only on POST, never GET (email-link prefetch protection).
    """
    if not raw_token:
        return None
    token_hash = _hash_token(raw_token)
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, purpose, user_id, approval_id, thread_id, "
            "       intended_decision, expires_at, used_at, consumed_decision "
            "FROM magic_link_tokens "
            "WHERE token_hash = $1",
            token_hash,
        )
    if row is None:
        return None
    if row["used_at"] is not None:
        return None  # already consumed
    if row["expires_at"] < _now_utc():
        return None  # expired
    return dict(row)


async def consume_magic_link(
    db: Any, token_id: str, decision: str
) -> Optional[dict[str, Any]]:
    """CAS UPDATE: marks the token consumed iff still unused. Returns the
    updated row dict on success, None on contention (already consumed by
    a racing click — also the bot-prefetch case where the link was hit
    twice in close succession).

    Single-use enforcement: WHERE used_at IS NULL. Even if the same human
    clicks twice in 50ms, only one POST wins.
    """
    if decision not in ("approved", "denied"):
        return None
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE magic_link_tokens "
            "SET used_at = now(), consumed_decision = $2 "
            "WHERE id = $1 AND used_at IS NULL "
            "RETURNING id, purpose, user_id, approval_id, thread_id, "
            "          intended_decision, consumed_decision",
            token_id,
            decision,
        )
    return dict(row) if row else None


async def thread_rate_limited(db: Any, thread_id: str) -> bool:
    """Rate-limit probe: HEADLESS_RATE_5MIN per 5min OR HEADLESS_RATE_HOUR
    per 60min. Returns True if the thread is over either ceiling."""
    async with db.acquire() as conn:
        recent_5 = await conn.fetchval(
            "SELECT COUNT(*) FROM thread_notifications "
            "WHERE thread_id = $1 AND sent_at > now() - interval '5 minutes' "
            "  AND delivery_status = 'sent'",
            thread_id,
        )
        recent_60 = await conn.fetchval(
            "SELECT COUNT(*) FROM thread_notifications "
            "WHERE thread_id = $1 AND sent_at > now() - interval '60 minutes' "
            "  AND delivery_status = 'sent'",
            thread_id,
        )
    return (recent_5 or 0) >= NOTIFICATION_RATE_LIMIT_5MIN or (
        recent_60 or 0
    ) >= NOTIFICATION_RATE_LIMIT_HOUR


async def record_notification(
    db: Any,
    *,
    thread_id: str,
    request_id: Optional[str],
    kind: str,
    delivery_status: str,
    email_to: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Append a row to thread_notifications. Best-effort — logs but does
    not raise on failure (we don't want to break the watcher loop)."""
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO thread_notifications "
                "(thread_id, request_id, kind, delivery_status, "
                " email_to, metadata) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                thread_id,
                request_id,
                kind,
                delivery_status,
                email_to,
                json.dumps(metadata or {}),
            )
    except Exception as e:
        logger.warning(
            "thread_notifications INSERT failed (thread=%s kind=%s): %s",
            thread_id,
            kind,
            e,
        )


async def claim_sent_notification(
    db: Any,
    *,
    thread_id: str,
    request_id: Optional[str],
    kind: str,
    email_to: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """Atomically claim the single 'sent' slot for (request_id, kind) BEFORE
    sending (HA / M1 — dual-leader dedup).

    Inserts a thread_notifications row with delivery_status='sent' guarded by
    the partial unique index uq_tn_sent_request_kind (migration 0038). Returns
    the new row id iff THIS call won the slot; None means a concurrent sweeper
    in the transient dual-leader window already claimed it (is sending), so the
    caller MUST NOT send again. Unlike record_notification this is NOT
    best-effort — a DB error propagates so we never silently double-send.
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO thread_notifications "
            "(thread_id, request_id, kind, delivery_status, email_to, metadata) "
            "VALUES ($1, $2, $3, 'sent', $4, $5::jsonb) "
            "ON CONFLICT (request_id, kind) WHERE delivery_status = 'sent' "
            "DO NOTHING "
            "RETURNING id",
            thread_id,
            request_id,
            kind,
            email_to,
            json.dumps(metadata or {}),
        )
    return row["id"] if row else None


async def downgrade_sent_claim(db: Any, claim_id: int) -> None:
    """Downgrade a claimed 'sent' row to 'failed' when the send didn't go
    through. Frees the unique slot and keeps the audit honest. 'failed' is
    terminal in the sweeper query, so this won't re-dispatch — matching the
    behaviour before claim-before-send. Best-effort."""
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE thread_notifications SET delivery_status = 'failed' "
                "WHERE id = $1",
                claim_id,
            )
    except Exception as e:
        logger.warning(
            "thread_notifications downgrade to 'failed' failed (id=%s): %s",
            claim_id,
            e,
        )


def _build_magic_link_url(cockpit_url: str, raw_token: str) -> str:
    """Compose the user-visible URL we embed in the email. The cockpit
    serves /magic/approve/{token} via the orchestrator proxy in
    production; locally it lands directly on the orchestrator port.
    """
    return f"{cockpit_url.rstrip('/')}/magic/approve/{urllib.parse.quote(raw_token, safe='')}"


def _build_permission_email_bodies(
    *,
    tool_name: str,
    tool_args_preview: str,
    approve_url: str,
    deny_url: str,
    cockpit_link: str,
    request_age_minutes: int,
) -> tuple[str, str]:
    """Return (plain_text, html) bodies for the permission-pending email."""
    text_body = (
        f"The persistent agent is waiting for your approval to run a tool.\n\n"
        f"Tool: {tool_name}\n"
        f"Arguments preview: {tool_args_preview}\n"
        f"Waiting for: ~{request_age_minutes} minutes\n\n"
        f"Approve: {approve_url}\n"
        f"Deny:    {deny_url}\n\n"
        f"Or open the cockpit to review the full context:\n"
        f"{cockpit_link}\n\n"
        f"This link expires in 30 minutes and can be used once.\n"
    )

    safe_args = escape_text(tool_args_preview)
    safe_tool = escape_text(tool_name)

    body_html = (
        f"<p style='margin:0 0 12px 0;'>The agent wants to call "
        f'<code style="background:{_C["surface-0"]};padding:2px 6px;'
        f'font-family:monospace;">{safe_tool}</code>:</p>'
        f'<pre style="background:{_C["surface-0"]};padding:12px;'
        f"margin:0;overflow-x:auto;font-size:12px;line-height:18px;"
        f'font-family:monospace;color:{_C["text-primary"]};">{safe_args}</pre>'
    )

    html_body = render_email(
        title="Permission requested",
        subtitle=f"Waiting {request_age_minutes} min for your decision.",
        body_html=body_html,
        actions=[
            Action(label="Approve", url=approve_url, variant="approve"),
            Action(label="Deny", url=deny_url, variant="deny"),
        ],
        footer_note="Open the cockpit for full context.",
    )

    return text_body, html_body


def _truncate_args_for_email(tool_args: dict[str, Any], max_chars: int = 600) -> str:
    """Build a human-readable args preview, bounded for email body."""
    try:
        rendered = json.dumps(tool_args, indent=2, default=str)
    except Exception:
        rendered = str(tool_args)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n… (truncated)"
    return rendered


async def send_permission_pending_email(
    db: Any,
    email_service: Any,
    *,
    thread_id: str,
    approval_id: str,
    cockpit_external_url: str,
) -> dict[str, Any]:
    """Compose and send the approve-pending email.

    Returns a dict with `status` ∈ {sent, failed, skipped_rate_limit,
    skipped_no_email, skipped_smtp, skipped_already_resolved}. Every
    outcome is recorded in thread_notifications for observability.

    Idempotent under the sweeper: the sweeper's SQL filters out
    requests with terminal/permanent delivery outcomes, so this entry
    point only sees first-time dispatches plus the small window where
    a transient skip has expired.
    """
    # 1. Rate limit.
    if await thread_rate_limited(db, thread_id):
        await record_notification(
            db,
            thread_id=thread_id,
            request_id=approval_id,
            kind="permission_pending",
            delivery_status="skipped_rate_limit",
        )
        return {"status": "skipped_rate_limit"}

    # 2. Load context: thread → user → email; approval → tool details.
    async with db.acquire() as conn:
        thread_row = await conn.fetchrow(
            "SELECT id, user_id, title FROM threads WHERE id = $1",
            thread_id,
        )
        permission_row = await conn.fetchrow(
            "SELECT id, tool_name, tool_args, requested_at, status "
            "FROM thread_permission_requests WHERE id = $1",
            approval_id,
        )
    if thread_row is None or permission_row is None:
        await record_notification(
            db,
            thread_id=thread_id,
            request_id=approval_id,
            kind="permission_pending",
            delivery_status="failed",
            metadata={"reason": "thread_or_request_missing"},
        )
        return {"status": "failed", "reason": "thread_or_request_missing"}

    if permission_row["status"] != "pending":
        # Resolved between sweeper detection and send. Record it so the
        # sweeper's widened IN-set suppresses re-dispatch — the
        # status='pending' filter does the same job today, but recording
        # the race keeps thread_notifications a complete audit trail and
        # protects against future changes to the sweeper SQL.
        await record_notification(
            db,
            thread_id=thread_id,
            request_id=approval_id,
            kind="permission_pending",
            delivery_status="skipped_already_resolved",
        )
        return {"status": "skipped_already_resolved"}

    user_id = thread_row["user_id"]
    user_row = None
    if user_id is not None:
        async with db.acquire() as conn:
            user_row = await conn.fetchrow(
                "SELECT id, email, display_name FROM users WHERE id = $1",
                user_id,
            )

    recipient_email = (user_row or {}).get("email") if user_row else None
    if not recipient_email:
        await record_notification(
            db,
            thread_id=thread_id,
            request_id=approval_id,
            kind="permission_pending",
            delivery_status="skipped_no_email",
        )
        return {"status": "skipped_no_email"}

    if email_service is None or not getattr(email_service, "is_configured", False):
        await record_notification(
            db,
            thread_id=thread_id,
            request_id=approval_id,
            kind="permission_pending",
            delivery_status="skipped_smtp",
        )
        return {"status": "skipped_smtp"}

    # 3. Generate two tokens — one per decision. Both bound to the same
    # approval_id and expire on the same clock.
    approve_token, _ = await generate_magic_link_token(
        db,
        purpose="approve_permission",
        user_id=str(user_id) if user_id else None,
        approval_id=approval_id,
        thread_id=thread_id,
        intended_decision="approved",
    )
    deny_token, _ = await generate_magic_link_token(
        db,
        purpose="approve_permission",
        user_id=str(user_id) if user_id else None,
        approval_id=approval_id,
        thread_id=thread_id,
        intended_decision="denied",
    )

    # 4. Compose bodies.
    requested_at = permission_row["requested_at"]
    if isinstance(requested_at, datetime):
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=timezone.utc)
        age_min = max(0, int((_now_utc() - requested_at).total_seconds() // 60))
    else:
        age_min = 0

    tool_args = permission_row["tool_args"]
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except Exception:
            tool_args = {}
    elif tool_args is None:
        tool_args = {}

    text_body, html_body = _build_permission_email_bodies(
        tool_name=permission_row["tool_name"],
        tool_args_preview=_truncate_args_for_email(tool_args),
        approve_url=_build_magic_link_url(cockpit_external_url, approve_token),
        deny_url=_build_magic_link_url(cockpit_external_url, deny_token),
        cockpit_link=f"{cockpit_external_url.rstrip('/')}/sessions/{thread_id}",
        request_age_minutes=age_min,
    )

    subject = f"[SRW] Approval needed: {permission_row['tool_name']}"

    # 5. Claim the single 'sent' slot for this (request, kind) BEFORE sending,
    # so a concurrent sweeper in the transient dual-leader window (leader
    # election has no fencing) cannot also send. Losing the claim means another
    # sweeper already sent (or is sending) → skip silently. In steady state
    # leadership is single, so this only fires during a partition / failover.
    claim_id = await claim_sent_notification(
        db,
        thread_id=thread_id,
        request_id=approval_id,
        kind="permission_pending",
        email_to=recipient_email,
        metadata={"tool": permission_row["tool_name"]},
    )
    if claim_id is None:
        return {"status": "skipped_dedup"}

    # 6. Dispatch via EmailService internals (private _send is fine —
    # same package). We avoid send_agent_message because that template
    # is job-oriented; this notification is thread-only.
    sent_ok = False
    try:
        sent_ok = await email_service._send(
            to=recipient_email,
            subject=subject,
            body_text=text_body,
            body_html=html_body,
        )
    except Exception as e:
        logger.warning(
            "Permission-pending email send raised (thread=%s req=%s): %s",
            thread_id,
            approval_id,
            e,
        )

    if not sent_ok:
        # Send failed after we claimed the slot — downgrade the audit row to
        # 'failed' (terminal in the sweeper query, same as before this change).
        await downgrade_sent_claim(db, claim_id)

    status = "sent" if sent_ok else "failed"
    return {"status": status, "email_to": recipient_email}
