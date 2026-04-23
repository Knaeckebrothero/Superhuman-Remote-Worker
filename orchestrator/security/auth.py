"""Authentication for the orchestrator API.

Three auth paths:
1. Bearer token (OIDC) — Keycloak access token from the cockpit
2. X-MCP-Token — API tokens for Claude Code / CLI clients (unchanged)
3. X-MCP-User-Id + X-Internal-Key — forwarded by the MCP server after
   it has already authenticated the caller via OAuth or API token.

Session-based auth has been replaced by Keycloak OIDC (Phase 2).
"""

import asyncio
import logging
import os

from fastapi import HTTPException, Request
from security.oidc import oidc_validator

logger = logging.getLogger(__name__)


async def get_current_user(request: Request, db) -> dict:
    """FastAPI dependency: extract current user from Bearer token.

    Validates the Keycloak access token from the Authorization header,
    then looks up (or JIT-provisions) the local user row.

    Returns the full user record (id, display_name, avatar_color, email,
    default_project_id, is_admin, is_approved, keycloak_sub, created_at).

    The ``is_approved`` flag is derived from the Keycloak token's realm roles
    (``user`` or ``admin``).  It is NOT stored in the database — it is
    recomputed on every request so that granting/revoking the role in Keycloak
    takes effect immediately.

    Raises:
        HTTPException 401 if not authenticated or token is invalid
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        # Fallback: MCP internal header auth (the MCP server has already
        # authenticated the caller and forwards the resolved user ID).
        return await _get_user_from_mcp_headers(request, db)

    token = auth_header[7:]
    claims = oidc_validator.validate_token(token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # JIT provision: find or create local user from Keycloak sub
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
        # Sync fields that may have changed in Keycloak (e.g. email added later)
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
        # Attach transient approval flag + preferred_username (not stored in DB)
        user["is_approved"] = is_approved
        user["preferred_username"] = claims.get("preferred_username")
        return user

    # First login — create local user row
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
    # Seed the main-cloud user record so the first session folder share
    # doesn't race the user's first browser login to the cloud. Fire-and-
    # forget: cloud reachability must not gate SSO, and the share path
    # still falls back to lazy resolution if this misses.
    asyncio.create_task(
        _ensure_cloud_user(
            sub=sub,
            issuer=claims.get("iss", ""),
            email=email,
            display_name=display_name,
            preferred_username=claims.get("preferred_username"),
        )
    )
    # Same treatment for Gitea — without this, the first thread created
    # immediately after cockpit login hits a race where grant_user_repo_access
    # can't find the user in Gitea yet (OIDC auto-registration only happens
    # on first direct Gitea visit), leaving the thread repo invisible
    # behind Gitea's 404-for-private-repos-you-can't-see behavior.
    asyncio.create_task(
        _ensure_gitea_user(
            sub=sub,
            email=email,
            preferred_username=claims.get("preferred_username"),
            display_name=display_name,
        )
    )
    # Attach transient approval flag (not stored in DB) plus preferred_username
    # from the claim — downstream handlers (e.g. thread creation) pass this to
    # Gitea so ensure_user can provision with the correct login_name.
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
