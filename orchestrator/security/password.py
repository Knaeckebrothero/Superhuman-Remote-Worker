"""Password hashing and validation for production auth mode.

Uses argon2id (winner of the Password Hashing Competition) via argon2-cffi.
Default params: memory=65536, time=3, parallelism=4.
"""

import logging

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a password using argon2id."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its argon2id hash (constant-time)."""
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception as e:
        logger.error(f"Password verification error: {e}")
        return False


def validate_password_strength(password: str) -> tuple[bool, str]:
    """Basic password strength validation.

    Returns:
        Tuple of (is_valid, error_message). error_message is empty if valid.
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not any(c.isalpha() for c in password):
        return False, "Password must contain at least one letter"
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one digit"
    return True, ""
