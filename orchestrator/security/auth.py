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
    default_project_id, is_admin, keycloak_sub, created_at).

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
    user = await db.get_user_by_keycloak_sub(sub)
    if user:
        return user

    # First login — create local user row
    email = claims.get("email", "")
    display_name = (
        claims.get("name")
        or claims.get("preferred_username")
        or email.split("@")[0]
        or "User"
    )
    realm_roles = claims.get("realm_access", {}).get("roles", [])
    is_admin = "admin" in realm_roles

    user = await db.upsert_user_from_oidc(
        sub=sub,
        email=email,
        display_name=display_name,
        is_admin=is_admin,
    )
    logger.info("JIT-provisioned user %s (sub=%s, admin=%s)", display_name, sub, is_admin)
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
