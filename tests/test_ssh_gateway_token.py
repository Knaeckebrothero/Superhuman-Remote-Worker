"""Tests for the WSS attach token — the credential a USER presents.

Why this module exists at all: the plan's original `_token_valid()` compared
the user-presented bearer token to ``config.internal_key``, which
``ssh_gateway_config`` sources from ``MCP_INTERNAL_KEY`` — the same value the
gateway sends as ``X-Internal-Key`` to the orchestrator's privileged internal
API, and which ``require_internal`` guards ~50 endpoints with. Plan 3's client
helper stores its token in ``~/.config/srw/token`` on a laptop, so shipping
that would have written the platform's master service credential into every
SSH user's home directory. A user-facing credential and a service-to-service
credential are not the same credential (ruling G38; design §6.3, citing
Gitpod CVE-2023-0957 and Daytona CVE-2026-54324).

The replacement is the *identical* construction plan 1 already ships for the
SSH-key registration challenge (``main._mint_ssh_key_challenge`` /
``_verify_ssh_key_challenge``): a stateless HMAC-SHA256 token over a nonce,
the user id and an expiry, keyed with ``SESSION_JWT_SECRET``. Stateless
because the orchestrator runs ``replicas: 2`` with no session affinity and
the gateway is a *separate Deployment* — there is no shared memory anywhere
on this path, so any in-process nonce store would reject roughly half of all
attaches.

The load-bearing property tested here is DOMAIN SEPARATION: a registration
challenge must not be redeemable as an attach token, and vice versa. Both are
minted with the same key, so only the version clause inside the MAC'd head
keeps them apart.
"""

import time

import pytest

from services.ssh_gateway_token import (
    ATTACH_TOKEN_MAX_LENGTH,
    ATTACH_TOKEN_TTL_SECONDS,
    ATTACH_TOKEN_VERSION,
    mint_attach_token,
    verify_attach_token,
)

SECRET = "test-only-attach-secret"
USER = "00000000-0000-0000-0000-000000000001"


def test_a_freshly_minted_token_verifies_and_returns_its_user():
    token, expires_at = mint_attach_token(USER, SECRET)
    assert expires_at > time.time()
    assert verify_attach_token(token, SECRET) == USER


def test_the_token_names_its_version_so_it_can_never_be_a_challenge():
    """Domain separation is carried by a clause INSIDE the MAC'd head.

    Both tokens are HMACs under SESSION_JWT_SECRET. If the version clause
    were outside the MAC (or absent), a registration challenge minted for
    Mallory would verify here as an attach token and vice versa.
    """
    token, _ = mint_attach_token(USER, SECRET)
    assert token.startswith(ATTACH_TOKEN_VERSION + ":")
    assert ATTACH_TOKEN_VERSION != "srw-ssh1"


def test_an_ssh_key_registration_challenge_is_not_an_attach_token():
    """The cross-protocol confusion test, against the real minter.

    ``main._mint_ssh_key_challenge`` signs with the same key. Its token must
    not open the gateway, or every user who ever requested a key-registration
    challenge holds a gateway credential they were never issued.
    """
    import main

    # _mint_ssh_key_challenge reads main's module-level secret, bound once at
    # import time, so drive it through the same value this file uses.
    original = main._session_jwt_secret
    main._session_jwt_secret = SECRET
    try:
        challenge, _ = main._mint_ssh_key_challenge(USER, "alice")
    finally:
        main._session_jwt_secret = original

    assert verify_attach_token(challenge, SECRET) is None


def test_an_attach_token_is_not_an_ssh_key_registration_challenge():
    """The other direction: an attach token must not register a key."""
    import main

    token, _ = mint_attach_token(USER, SECRET)
    original = main._session_jwt_secret
    main._session_jwt_secret = SECRET
    try:
        assert main._verify_ssh_key_challenge(token, USER) is False
    finally:
        main._session_jwt_secret = original


def test_an_expired_token_is_refused():
    now = time.time()
    token, _ = mint_attach_token(USER, SECRET, now=now)
    assert verify_attach_token(token, SECRET, now=now + 1) == USER
    assert verify_attach_token(token, SECRET, now=now + ATTACH_TOKEN_TTL_SECONDS) is None


def test_a_token_signed_with_another_secret_is_refused():
    token, _ = mint_attach_token(USER, SECRET)
    assert verify_attach_token(token, "some-other-secret") is None


def test_a_tampered_user_id_is_refused():
    """The whole point of user-binding: the id is covered by the MAC."""
    token, _ = mint_attach_token(USER, SECRET)
    version, nonce, user_id, expires, signature = token.split(":")
    forged = ":".join(
        [version, nonce, "00000000-0000-0000-0000-0000000000ff", expires, signature]
    )
    assert verify_attach_token(forged, SECRET) is None


def test_a_tampered_expiry_is_refused():
    now = time.time()
    token, _ = mint_attach_token(USER, SECRET, now=now)
    version, nonce, user_id, _expires, signature = token.split(":")
    forged = ":".join([version, nonce, user_id, str(int(now + 10**6)), signature])
    assert verify_attach_token(forged, SECRET, now=now) is None


def test_an_empty_secret_never_mints_and_never_verifies():
    """Fail closed. An empty HMAC key is a well-known key: anyone could
    compute a valid token for any user id."""
    with pytest.raises(RuntimeError):
        mint_attach_token(USER, "")
    token, _ = mint_attach_token(USER, SECRET)
    assert verify_attach_token(token, "") is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "srw-sshws1:only:three:parts",
        "srw-sshws1:n:u:notanumber:" + "a" * 64,
        "srw-ssh1:n:u:9999999999:" + "a" * 64,
        # Non-ASCII: hmac.compare_digest raises TypeError on a non-ASCII str,
        # which is reachable pre-authentication from a header value.
        "srw-sshws1:n:u:9999999999:é",
        "srw-sshws1:n:u:9999999999:" + "\ud800",
    ],
)
def test_malformed_tokens_are_refused_without_raising(token):
    assert verify_attach_token(token, SECRET) is None


def test_an_oversized_token_is_refused_before_the_hmac_runs():
    """Bounds the work an unauthenticated header value can buy."""
    token, _ = mint_attach_token(USER, SECRET)
    padded = token + "x" * ATTACH_TOKEN_MAX_LENGTH
    assert len(padded) > ATTACH_TOKEN_MAX_LENGTH
    assert verify_attach_token(padded, SECRET) is None


def test_two_tokens_for_the_same_user_differ():
    """A nonce is present, so a token is not a stable, cacheable secret."""
    first, _ = mint_attach_token(USER, SECRET)
    second, _ = mint_attach_token(USER, SECRET)
    assert first != second


def test_a_non_string_token_is_refused_rather_than_crashing():
    assert verify_attach_token(None, SECRET) is None
    assert verify_attach_token(b"bytes", SECRET) is None
