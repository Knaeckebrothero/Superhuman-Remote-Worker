"""Auth mode detection for dual-mode authentication.

AUTH_MODE=dev       — email-only login, no password required (development)
AUTH_MODE=production — email + password + email verification (default)
"""

import os


def get_auth_mode() -> str:
    """Return 'dev' or 'production'. Default is 'production'."""
    return os.getenv("AUTH_MODE", "production").lower()
