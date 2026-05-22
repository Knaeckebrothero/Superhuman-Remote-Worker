"""Pod-side validation of the orchestrator-minted session JWT.

The pod knows two things from its env:
  - SESSION_JWT_SECRET — shared with the orchestrator (K8s Secret)
  - SESSION_BOUND_THREAD_ID — the thread this pod was provisioned for

A valid handshake: token signature OK, audience=agent, not expired,
and claim `tid` matches SESSION_BOUND_THREAD_ID.

See docs/features/direct_session_websockets.md §Component details.
"""

from __future__ import annotations

import logging
import os

import jwt
from fastapi import WebSocket

logger = logging.getLogger(__name__)


async def validate_session_token(ws: WebSocket) -> bool:
    """Validate the WS query param `t`. Closes the WS with an appropriate
    code on failure. Returns True if the connection should proceed."""
    secret = os.environ.get("SESSION_JWT_SECRET", "")
    bound_tid = os.environ.get("SESSION_BOUND_THREAD_ID", "")
    if not secret or not bound_tid:
        # Misconfigured pod — fail closed.
        await ws.accept()
        await ws.close(code=4500, reason="pod missing session auth config")
        return False

    token = ws.query_params.get("t")
    if not token:
        await ws.accept()
        await ws.close(code=4401, reason="missing session token")
        return False

    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="agent",
            leeway=2,
            options={
                "require": ["exp", "iat", "aud", "sub", "tid"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": True,
            },
        )
    except jwt.PyJWTError as e:
        logger.warning("ws_chat: invalid session token: %s", e)
        await ws.accept()
        await ws.close(code=4401, reason="invalid session token")
        return False

    if str(claims.get("tid") or "") != bound_tid:
        logger.warning(
            "ws_chat: token tid %r != bound %r — rejecting",
            claims.get("tid"),
            bound_tid,
        )
        await ws.accept()
        await ws.close(code=4403, reason="session token mismatch")
        return False

    return True
