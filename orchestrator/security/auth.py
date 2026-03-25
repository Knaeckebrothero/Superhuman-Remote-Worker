"""Authentication for the orchestrator API.

Two auth paths:
1. Bearer token (OIDC) — Keycloak access token from the cockpit
2. X-MCP-Token — API tokens for Claude Code / CLI clients (unchanged)

Session-based auth has been replaced by Keycloak OIDC (Phase 2).
"""

import asyncio
import logging

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
        raise HTTPException(status_code=401, detail="Not authenticated")

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
        # Attach transient approval flag (not stored in DB)
        user["is_approved"] = is_approved
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
    # Attach transient approval flag (not stored in DB)
    user["is_approved"] = is_approved
    return user


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
