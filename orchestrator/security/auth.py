"""Session-based authentication for the cockpit.

Ported from Advanced-LLM-Chat, adapted for:
- Async DB calls (asyncpg via PostgresDB)
- UUID user IDs
- No guest sessions or security event logging
"""
import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from security.csrf import generate_csrf_token

logger = logging.getLogger(__name__)


def generate_session_key(length: int = 32) -> str:
    """Generate a cryptographically secure session key."""
    return secrets.token_urlsafe(length)


async def create_session(
    db,
    user_id: str,
    user_email: str,
    session_duration_hours: int = 24,
) -> tuple[str, str]:
    """Create a new session in the database.

    Args:
        db: PostgresDB instance
        user_id: UUID string of the user
        user_email: User's email address
        session_duration_hours: How long the session lasts

    Returns:
        Tuple of (session_key, csrf_token)
    """
    session_key = generate_session_key()
    csrf_token = generate_csrf_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=session_duration_hours)

    await db.create_session(
        session_key=session_key,
        user_id=user_id,
        email=user_email,
        expires_at=expires_at,
        csrf_token=csrf_token,
    )

    logger.info(f"Session created for user_id={user_id}, expires_in={session_duration_hours}h")
    return session_key, csrf_token


async def validate_session(db, session_key: str) -> dict | None:
    """Validate a session key and return user info.

    Args:
        db: PostgresDB instance
        session_key: The session key from the cookie

    Returns:
        Dict with user_id, email, csrf_token, expires_at, expires_in — or None
    """
    if not session_key:
        return None

    session = await db.get_session(session_key)
    if not session:
        return None

    # Update last activity
    await db.update_session_activity(session_key)

    # Calculate time until expiry
    expires_at = session["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    expires_in = int((expires_at - now).total_seconds())

    return {
        "user_id": str(session["user_id"]),
        "email": session["email"],
        "csrf_token": session["csrf_token"],
        "expires_at": expires_at.isoformat(),
        "expires_in": expires_in,
    }


async def delete_session(db, session_key: str) -> None:
    """Delete a session from the database."""
    await db.delete_session(session_key)
    logger.debug("Session deleted")


async def get_current_user(request: Request, db) -> dict:
    """FastAPI dependency: extract current user from session cookie.

    Args:
        request: FastAPI Request
        db: PostgresDB instance

    Returns:
        User info dict

    Raises:
        HTTPException 401 if not authenticated
    """
    session_key = request.cookies.get("session")
    if not session_key:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_info = await validate_session(db, session_key)
    if not user_info:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    return user_info


async def cleanup_expired_sessions(db, shutdown_event: asyncio.Event) -> None:
    """Background task that cleans up expired sessions every hour."""
    logger.info("Session cleanup task started")
    while not shutdown_event.is_set():
        try:
            await db.delete_expired_sessions()
            await db.delete_expired_auth_tokens()
            await db.cleanup_expired_mcp_tokens()
            logger.debug("Expired sessions, auth tokens, and MCP tokens cleanup completed")
        except Exception as e:
            logger.error(f"Error cleaning up sessions/tokens: {e}")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=3600.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Session cleanup task stopped")
