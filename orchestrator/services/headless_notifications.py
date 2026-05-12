"""Email magic-link generation + outbound notification dispatch for
headless persistent sessions.

Phase 4 of docs/features/headless_persistent_sessions.md. When the agent's
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


async def already_notified(db: Any, thread_id: str, request_id: str) -> bool:
    """Dedup probe: has (thread_id, request_id) already produced a sent
    notification? Skipped rows do not count."""
    async with db.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT 1 FROM thread_notifications "
            "WHERE thread_id = $1 AND request_id = $2 "
            "  AND delivery_status IN ('sent', 'failed') "
            "LIMIT 1",
            thread_id,
            request_id,
        )
    return bool(existing)


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

    # Escape HTML special chars in the args preview.
    safe_args = (
        tool_args_preview.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    safe_tool = tool_name.replace("&", "&amp;").replace("<", "&lt;")

    html_body = f"""\
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; color: #cdd6f4; background: #1e1e2e;">
  <div style="border: 1px solid #313244; border-radius: 12px; overflow: hidden;">
    <div style="background: #181825; padding: 16px 20px; border-bottom: 1px solid #313244;">
      <h2 style="margin: 0 0 4px 0; color: #cba6f7; font-size: 16px;">Permission requested</h2>
      <p style="margin: 0; color: #a6adc8; font-size: 13px;">
        Waiting ~{request_age_minutes} minute(s)
      </p>
    </div>
    <div style="padding: 20px; font-size: 14px; line-height: 1.6; color: #cdd6f4;">
      <p style="margin: 0 0 12px 0;">
        The agent wants to call <code style="background: #181825; padding: 2px 6px; border-radius: 4px;">{safe_tool}</code>:
      </p>
      <pre style="background: #181825; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; color: #a6e3a1;">{safe_args}</pre>
    </div>
    <div style="background: #181825; padding: 16px 20px; border-top: 1px solid #313244; text-align: center;">
      <a href="{approve_url}" style="display: inline-block; background: #a6e3a1; color: #1e1e2e; padding: 10px 24px; margin: 4px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 14px;">Approve</a>
      <a href="{deny_url}" style="display: inline-block; background: #f38ba8; color: #1e1e2e; padding: 10px 24px; margin: 4px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 14px;">Deny</a>
      <p style="margin: 16px 0 0 0; color: #6c7086; font-size: 12px;">
        Or <a href="{cockpit_link}" style="color: #cba6f7;">open the cockpit</a> for full context.
      </p>
      <p style="margin: 8px 0 0 0; color: #6c7086; font-size: 11px;">
        Links expire in 30 minutes and can be used once.
      </p>
    </div>
  </div>
</div>"""
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

    Returns a dict with `status` ∈ {sent, failed, skipped_dedup,
    skipped_rate_limit, skipped_no_email, skipped_smtp}. Every outcome
    is recorded in thread_notifications for observability.

    Idempotent: dedup on (thread_id, approval_id) means safe to call
    from a polling watcher.
    """
    # 1. Dedup.
    if await already_notified(db, thread_id, approval_id):
        await record_notification(
            db,
            thread_id=thread_id,
            request_id=approval_id,
            kind="permission_pending",
            delivery_status="skipped_dedup",
        )
        return {"status": "skipped_dedup"}

    # 2. Rate limit.
    if await thread_rate_limited(db, thread_id):
        await record_notification(
            db,
            thread_id=thread_id,
            request_id=approval_id,
            kind="permission_pending",
            delivery_status="skipped_rate_limit",
        )
        return {"status": "skipped_rate_limit"}

    # 3. Load context: thread → user → email; approval → tool details.
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
        # Resolved between watcher detection and send — don't email.
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

    # 4. Generate two tokens — one per decision. Both bound to the same
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

    # 5. Compose bodies.
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

    status = "sent" if sent_ok else "failed"
    await record_notification(
        db,
        thread_id=thread_id,
        request_id=approval_id,
        kind="permission_pending",
        delivery_status=status,
        email_to=recipient_email,
        metadata={"tool": permission_row["tool_name"]},
    )
    return {"status": status, "email_to": recipient_email}
