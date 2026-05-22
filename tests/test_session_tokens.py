"""Tests for the session JWT module — mint and validate."""

import time

import pytest

from orchestrator.services.session_tokens import (
    SessionTokenService,
    InvalidSessionTokenError,
)


@pytest.fixture
def svc():
    """A SessionTokenService with a fixed secret and 60s TTL."""
    return SessionTokenService(secret="test-secret-do-not-use", ttl_seconds=60)


def test_mint_returns_token_payload_and_expiry(svc):
    """Minting yields a token string and the absolute expiry timestamp."""
    before = int(time.time())
    token, expires_at = svc.mint(user_id="u1", thread_id="t1")
    after = int(time.time())

    assert isinstance(token, str) and len(token) > 0
    assert before + 60 <= expires_at <= after + 60


def test_validate_accepts_valid_token(svc):
    """A freshly minted token validates and exposes its claims."""
    token, _ = svc.mint(user_id="u1", thread_id="t1")
    claims = svc.validate(token)

    assert claims["sub"] == "u1"
    assert claims["tid"] == "t1"
    assert claims["aud"] == "agent"
    assert "exp" in claims
    assert "iat" in claims
    assert "jti" in claims


def test_validate_rejects_wrong_signature(svc):
    """A token signed by a different secret is rejected."""
    other = SessionTokenService(secret="different-secret", ttl_seconds=60)
    token, _ = other.mint(user_id="u1", thread_id="t1")

    with pytest.raises(InvalidSessionTokenError):
        svc.validate(token)


def test_validate_rejects_expired_token():
    """A token past its expiry (plus 2s leeway) is rejected."""
    svc = SessionTokenService(secret="test-secret", ttl_seconds=1)
    token, _ = svc.mint(user_id="u1", thread_id="t1")
    time.sleep(4)  # exceed ttl (1s) + leeway (2s) with margin

    with pytest.raises(InvalidSessionTokenError):
        svc.validate(token)


def test_validate_rejects_wrong_audience(svc):
    """A token with audience != 'agent' is rejected."""
    import jwt

    bad = jwt.encode(
        {"sub": "u1", "tid": "t1", "aud": "other", "exp": int(time.time()) + 60},
        "test-secret-do-not-use",
        algorithm="HS256",
    )
    with pytest.raises(InvalidSessionTokenError):
        svc.validate(bad)


def test_validate_rejects_malformed_token(svc):
    """Garbage strings are rejected, not crashed on."""
    with pytest.raises(InvalidSessionTokenError):
        svc.validate("not-a-jwt-at-all")


def test_validate_rejects_token_missing_required_claims(svc):
    """A token missing `sub`, `tid`, `aud`, `iat`, or `exp` is rejected.

    Defensive boundary: validate() must reject incomplete tokens so callers
    don't need to recheck claim presence.
    """
    import time as _t
    import jwt as _jwt

    # Token missing `tid` — should be rejected even though signature is valid.
    incomplete = _jwt.encode(
        {
            "sub": "u1",
            "aud": "agent",
            "iat": int(_t.time()),
            "exp": int(_t.time()) + 60,
            # NO `tid` claim
        },
        "test-secret-do-not-use",
        algorithm="HS256",
    )
    with pytest.raises(InvalidSessionTokenError):
        svc.validate(incomplete)
