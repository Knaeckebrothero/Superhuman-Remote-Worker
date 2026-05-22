"""Short-lived JWTs that authorize a single browser→agent-pod WS handshake.

The token is signed by the orchestrator (HS256, shared secret) and validated
by the agent pod on the `/ws/chat` upgrade. Claims are deliberately narrow:
`sub` (user ID), `tid` (thread ID), short `exp` (default 60 s).

This is **not** the same credential as the BFF cookie or API token — those
authenticate user→orchestrator. This authenticates orchestrator→pod for a
specific session handshake, so we can hand the pod a narrowly-scoped trust
without giving it the BFF signing key.

See `docs/features/direct_session_websockets.md` §Component details.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt


class InvalidSessionTokenError(Exception):
    """Raised when a token fails signature, audience, expiry, or shape checks."""


class SessionTokenService:
    """Mint + validate short-lived session JWTs."""

    _AUDIENCE = "agent"
    _ALGORITHM = "HS256"

    def __init__(self, secret: str, ttl_seconds: int = 60) -> None:
        if not secret:
            raise ValueError("SessionTokenService requires a non-empty secret")
        self._secret = secret
        self._ttl = int(ttl_seconds)

    def mint(self, user_id: str, thread_id: str) -> tuple[str, int]:
        """Return ``(token, absolute_expiry_unix_ts)``."""
        now = int(time.time())
        exp = now + self._ttl
        claims = {
            "sub": str(user_id),
            "tid": str(thread_id),
            "aud": self._AUDIENCE,
            "iat": now,
            "exp": exp,
            "jti": str(uuid.uuid4()),
        }
        token = jwt.encode(claims, self._secret, algorithm=self._ALGORITHM)
        return token, exp

    def validate(self, token: str) -> dict[str, Any]:
        """Return claims dict if valid, raise ``InvalidSessionTokenError`` otherwise."""
        try:
            return jwt.decode(
                token,
                self._secret,
                algorithms=[self._ALGORITHM],
                audience=self._AUDIENCE,
            )
        except jwt.PyJWTError as e:
            raise InvalidSessionTokenError(str(e)) from e
