"""CSRF (Cross-Site Request Forgery) protection.

Double-submit cookie pattern: the csrf_token cookie value must match
the X-CSRF-Token header on mutating requests.
"""
import logging
import secrets

from fastapi import Request

logger = logging.getLogger(__name__)


def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token."""
    return secrets.token_urlsafe(32)


async def validate_csrf_token(request: Request) -> bool:
    """Validate CSRF token using double-submit cookie pattern.

    Skips validation for safe methods and auth endpoints.

    Returns:
        True if valid, False otherwise
    """
    # Skip safe methods
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True

    # Skip auth endpoints (login needs to work without a CSRF token)
    if request.url.path.startswith("/api/auth/"):
        return True

    # Skip requests without a session cookie — these are server-to-server
    # calls (agent heartbeats, job updates, etc.), not browser requests.
    # CSRF only protects browser sessions using cookies.
    if not request.cookies.get("session"):
        return True

    csrf_cookie = request.cookies.get("csrf_token")
    if not csrf_cookie:
        logger.debug("CSRF validation failed: missing csrf_token cookie")
        return False

    csrf_header = request.headers.get("X-CSRF-Token")
    if not csrf_header:
        logger.debug("CSRF validation failed: missing X-CSRF-Token header")
        return False

    is_valid = csrf_cookie == csrf_header
    if not is_valid:
        logger.warning(f"CSRF validation failed: token mismatch on {request.method} {request.url.path}")

    return is_valid
