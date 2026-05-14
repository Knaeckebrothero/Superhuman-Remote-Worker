"""Authentication for the orchestrator API.

Four auth paths, tried in this order:
1. ``srw_session`` cookie — cookie BFF; mid-stream access-token refresh is
   transparent. See docs/features/auth_bff_and_api_tokens.md.
2. Bearer token (OIDC) — Keycloak access token. Transitional during the
   cockpit cutover; will remain for direct API consumers and for tests.
3. X-MCP-Token — API tokens for Claude Code / CLI clients (unchanged).
4. X-MCP-User-Id + X-Internal-Key — forwarded by the MCP server after
   it has already authenticated the caller via OAuth or API token.

Cookie-path additions are purely additive — the function signature and
the ~86 inline ``await require_approved_user(request, db)`` call sites
do not change.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, UTC

from fastapi import HTTPException, Request
from security.kc_client import KeycloakClientError, kc_bff_client
from security.oidc import oidc_validator

logger = logging.getLogger(__name__)

SESSION_COOKIE = "srw_session"


# Knobs read fresh on each request (so the orchestrator picks up env edits
# without restart). Caller cost is one os.getenv per request which is
# trivially cheap.
def _idle_timeout() -> timedelta:
    return timedelta(seconds=int(os.getenv("SRW_SESSION_IDLE_TIMEOUT_S", "1800")))


def _refresh_skew() -> timedelta:
    return timedelta(seconds=int(os.getenv("SRW_ACCESS_TOKEN_REFRESH_SKEW_S", "60")))


async def get_current_user(request: Request, db) -> dict:
    """FastAPI dependency: resolve the current user.

    Order of attempts:
      1. ``srw_session`` cookie (BFF) — server-side refresh of the stored
         access token when it's within the refresh skew window.
      2. ``Authorization: Bearer <jwt>`` — Keycloak access token directly.
      3. MCP internal header auth (X-MCP-User-Id + X-Internal-Key).

    Returns the full user record (id, display_name, avatar_color, email,
    default_project_id, is_admin, is_approved, keycloak_sub, created_at).
    The ``is_approved`` flag is derived from the access token's realm
    roles on every request, so granting/revoking the role in Keycloak
    takes effect immediately (cookie path: at most one ``access_token``
    refresh later).

    Raises:
        HTTPException 401 if not authenticated or all paths fail.
    """
    # Path 1: cookie BFF.
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        user = await _resolve_from_cookie(session_id, db)
        if user is not None:
            return user
        # Invalid/expired cookie: fall through. The cookie itself will be
        # cleared on the next /auth/me response (the SPA refreshes via the
        # interceptor's 401 handler).

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        # Path 3: MCP internal header auth.
        return await _get_user_from_mcp_headers(request, db)

    # Path 2: Keycloak Bearer JWT.
    token = auth_header[7:]
    claims = oidc_validator.validate_token(token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return await _resolve_user_from_claims(claims, db)


async def _resolve_from_cookie(session_id: str, db) -> dict | None:
    """Validate a BFF session cookie. Returns user dict or None.

    None signals "fall through to other auth paths"; we don't raise here
    so that legacy Bearer clients with a stale cookie still work. The
    cookie gets cleared via the standard /auth/me round-trip.

    Refreshes the stored access token mid-session when it's within
    ``SRW_ACCESS_TOKEN_REFRESH_SKEW_S`` of expiry. A refresh failure
    (KC down, refresh token revoked) deletes the session row and
    returns None.
    """
    sess = await db.get_srw_session(session_id)
    if not sess:
        return None
    now = datetime.now(UTC)
    if sess["absolute_expires_at"] <= now:
        # Past absolute lifetime — refresh attempts would just bounce.
        await db.delete_srw_session(session_id)
        return None
    if sess["last_seen_at"] + _idle_timeout() <= now:
        await db.delete_srw_session(session_id)
        return None

    access_token = sess["access_token"]
    if sess["access_expires_at"] - now <= _refresh_skew():
        access_token = await _refresh_session_in_place(sess, db)
        if access_token is None:
            return None

    claims = oidc_validator.validate_token(access_token)
    if not claims:
        # Stored access token won't validate (signing key rotated?). Kill
        # the session so the user re-authenticates cleanly.
        await db.delete_srw_session(session_id)
        return None

    # Bump idle anchor. Fire-and-forget — this is a hot path and the next
    # request's view of last_seen_at is allowed to lag by a few ms.
    asyncio.create_task(db.touch_srw_session_last_seen(session_id))

    return await _resolve_user_from_claims(claims, db)


async def _refresh_session_in_place(sess: dict, db) -> str | None:
    """Refresh KC tokens and write them back to the session row.

    Returns the new access token on success; None if the refresh fails
    (and the caller should treat the session as dead).
    """
    try:
        tokens = await kc_bff_client.refresh(sess["refresh_token"])
    except KeycloakClientError as e:
        logger.info("Session %s refresh failed (%s) — deleting", sess["id"], e)
        await db.delete_srw_session(sess["id"])
        return None
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token") or sess["refresh_token"]
    expires_in = tokens.get("expires_in")
    if not access_token or not expires_in:
        logger.warning("Session %s refresh returned malformed payload", sess["id"])
        await db.delete_srw_session(sess["id"])
        return None
    new_access_expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
    await db.refresh_srw_session_tokens(
        sess["id"],
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=new_access_expires_at,
        id_token=tokens.get("id_token"),
    )
    return access_token


async def _resolve_user_from_claims(claims: dict, db) -> dict:
    """JIT-provision (or sync) the local user row from Keycloak claims.

    Shared between cookie and Bearer paths. ``is_approved`` and
    ``preferred_username`` are attached as transient fields (not in DB).
    """
    sub = claims["sub"]
    email = claims.get("email", "")
    display_name = (
        claims.get("preferred_username")
        or claims.get("name")
        or email.split("@")[0]
        or "User"
    )
    realm_roles = claims.get("realm_access", {}).get("roles", [])
    is_admin = "admin" in realm_roles
    is_approved = "user" in realm_roles or is_admin

    user = await db.get_user_by_keycloak_sub(sub)
    if user:
        needs_update = {}
        if email and (not user.get("email") or user["email"] != email):
            needs_update["email"] = email
        if user.get("display_name") != display_name:
            needs_update["display_name"] = display_name
        if user.get("is_admin") != is_admin:
            needs_update["is_admin"] = is_admin
        if needs_update:
            async with db.acquire() as conn:
                set_clause = ", ".join(
                    f"{k} = ${i + 2}" for i, k in enumerate(needs_update)
                )
                await conn.execute(
                    f"UPDATE users SET {set_clause} WHERE id = $1",
                    user["id"],
                    *needs_update.values(),
                )
            user.update(needs_update)
            logger.info(
                "Updated user %s from OIDC claims: %s", sub, list(needs_update.keys())
            )
        user["is_approved"] = is_approved
        user["preferred_username"] = claims.get("preferred_username")
        return user

    # First login — create local user row + seed cloud/Gitea in the background.
    user = await db.upsert_user_from_oidc(
        sub=sub,
        email=email,
        display_name=display_name,
        is_admin=is_admin,
    )
    logger.info(
        "JIT-provisioned user %s (sub=%s, admin=%s, approved=%s)",
        display_name,
        sub,
        is_admin,
        is_approved,
    )
    asyncio.create_task(
        _ensure_cloud_user(
            sub=sub,
            issuer=claims.get("iss", ""),
            email=email,
            display_name=display_name,
            preferred_username=claims.get("preferred_username"),
        )
    )
    asyncio.create_task(
        _ensure_gitea_user(
            sub=sub,
            email=email,
            preferred_username=claims.get("preferred_username"),
            display_name=display_name,
        )
    )
    user["is_approved"] = is_approved
    user["preferred_username"] = claims.get("preferred_username")
    return user


async def _ensure_cloud_user(
    *,
    sub: str,
    issuer: str,
    email: str,
    display_name: str,
    preferred_username: str | None,
) -> None:
    """Background call to the active main-cloud backend's ensure_user.

    Imported lazily to avoid a circular import between ``main`` and this
    module (``main`` imports ``get_current_user`` from here).
    """
    try:
        from main import main_cloud_router  # noqa: PLC0415

        backend = main_cloud_router.active
        if not backend.is_initialized:
            return
        await backend.ensure_user(
            sub=sub,
            issuer=issuer,
            email=email,
            display_name=display_name,
            preferred_username=preferred_username,
        )
    except Exception as e:
        logger.warning("Main-cloud ensure_user failed for sub=%s: %s", sub, e)


async def _ensure_gitea_user(
    *,
    sub: str,
    email: str,
    preferred_username: str | None,
    display_name: str,
) -> None:
    """Background pre-provision a Gitea user on first cockpit login.

    Mirrors _ensure_cloud_user but for Gitea. Without this, a user who
    creates a thread immediately after signing into cockpit (but before
    ever visiting Gitea) won't be a collaborator on their own thread
    repo — Gitea's OIDC auto-registration only fires on direct Gitea
    visits.

    Passes ``sub`` through so Gitea stores it as login_name — that's the
    key Gitea matches on during later direct OIDC login, preventing
    duplicate accounts.

    Imported lazily to avoid a circular import with main.
    """
    try:
        from main import gitea_client  # noqa: PLC0415

        if not gitea_client.is_initialized:
            return
        # preferred_username is the canonical Gitea login; fall back to
        # email-localpart for clients that don't emit the claim.
        username = preferred_username or (email.split("@")[0] if email else None)
        if not username or not email:
            return
        await gitea_client.ensure_user(
            email=email,
            username=username,
            full_name=display_name,
            sub=sub,
        )
    except Exception as e:
        logger.warning("Gitea ensure_user failed for email=%s: %s", email, e)


async def _get_user_from_mcp_headers(request: Request, db) -> dict:
    """Authenticate via MCP internal headers.

    The MCP server validates the caller (OAuth / API token) and then
    forwards requests to the orchestrator with X-MCP-User-Id and
    X-Internal-Key headers.  We trust these headers only when the
    internal key matches MCP_INTERNAL_KEY.
    """
    mcp_user_id = request.headers.get("X-MCP-User-Id")
    internal_key = request.headers.get("X-Internal-Key", "")
    expected_key = os.environ.get("MCP_INTERNAL_KEY", "")

    if mcp_user_id and expected_key and internal_key == expected_key:
        user = await db.get_user(mcp_user_id)
        if user:
            user["is_approved"] = True  # MCP tokens are pre-validated
            return user
        logger.warning("MCP header auth: user %s not found in DB", mcp_user_id)

    raise HTTPException(status_code=401, detail="Not authenticated")


async def require_approved_user(request: Request, db) -> dict:
    """Like get_current_user but raises 403 if the user lacks the 'user' role.

    Use this for all endpoints that require an approved account.
    /api/auth/me should use get_current_user directly so the cockpit can
    display a "pending approval" message.
    """
    user = await get_current_user(request, db)
    if not user.get("is_approved"):
        raise HTTPException(
            status_code=403,
            detail="Account pending approval. An administrator must assign you the 'user' role.",
        )
    return user


async def resolve_ws_user(ws, db) -> dict | None:
    """WebSocket auth: resolve the user from the ``srw_session`` cookie.

    WebSockets can't carry custom Authorization headers in browsers, so the
    cookie BFF is the only practical path for cockpit-initiated WS. Returns
    the user dict on success (with ``is_approved`` set), or None if the
    cookie is missing/invalid. The caller is expected to ``ws.close(4401)``
    on None — we don't take the WS state here, callers vary in whether
    they've already accepted.
    """
    session_id = ws.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    return await _resolve_from_cookie(session_id, db)


async def cleanup_expired_tokens(db, shutdown_event: asyncio.Event) -> None:
    """Background task that cleans up expired MCP tokens every hour."""
    logger.info("Token cleanup task started")
    while not shutdown_event.is_set():
        try:
            await db.cleanup_expired_mcp_tokens()
            logger.debug("Expired MCP tokens cleanup completed")
        except Exception as e:
            logger.error("Error cleaning up MCP tokens: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=3600.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Token cleanup task stopped")


async def cleanup_expired_sessions(db, shutdown_event: asyncio.Event) -> None:
    """Background task: prune dead BFF session rows and consumed pre-auth state.

    Hourly cadence matches cleanup_expired_tokens. Idle-timeout enforcement
    is handled by the per-request validator; this loop only removes rows
    past absolute lifetime / 7-day revocation tail.
    """
    logger.info("BFF session cleanup task started")
    while not shutdown_event.is_set():
        try:
            await db.cleanup_expired_srw_sessions()
            await db.cleanup_expired_srw_pre_auth()
            logger.debug("Expired BFF sessions / pre-auth state cleanup completed")
        except Exception as e:
            logger.error("Error cleaning up BFF sessions: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=3600.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("BFF session cleanup task stopped")
