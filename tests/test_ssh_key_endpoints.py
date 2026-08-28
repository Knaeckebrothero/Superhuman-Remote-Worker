"""Tests for the SSH key registration REST surface (``/api/ssh-keys*``).

Registration requires proving possession of the private key before a public
key is accepted. Without that check, anyone could claim a public key they
merely read (e.g. published at ``github.com/<user>.keys``), and because
fingerprints are globally unique that would deny the rightful owner the
ability to register their own key.

Possession challenges are STATELESS — an HMAC-signed token over a nonce, the
authenticated user's id and an expiry, verified with ``SESSION_JWT_SECRET`` —
not an in-process dict. The orchestrator runs multiple replicas behind one
Service with no session affinity (deployment/values-experimental.yaml
``orchestrator.replicas: 2`` on dev, deliberately), so the pod that issues a
challenge is frequently not the pod that redeems it; an in-process store
would reject roughly half of all registrations with "unknown challenge".
See ruling F24 in
``.superpowers/sdd/2026-08-28-workspace-ssh-access-foundation/progress.md``.

Because the token is stateless rather than single-use, ``test_challenge_*``
below documents the actual anti-replay property: a token is bound to the
user id that requested it (rejected cross-account, ``test_challenge_minted_
for_one_user_is_rejected_for_another``) and to a five-minute window
(``test_expired_challenge_is_rejected``), and integrity-checked
(``test_tampered_challenge_is_rejected``); a same-user replay is allowed at
the token layer and is instead caught by the fingerprint uniqueness
constraint on ``user_ssh_keys`` (409, not a token-layer 400).
"""

import time

import pytest
from fastapi import HTTPException

import main
from services.ssh_public_keys import SshKeyRejected


class _Body:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture(autouse=True)
def _ssh_challenge_secret(monkeypatch):
    """Every test gets a non-empty SESSION_JWT_SECRET by default.

    ``main._session_jwt_secret`` is bound once at import time from the
    environment, so setting the env var mid-test has no effect — tests that
    want the empty-secret (fail-closed) behavior override this directly with
    ``monkeypatch.setattr(main, "_session_jwt_secret", "")``.
    """
    monkeypatch.setattr(main, "_session_jwt_secret", "test-only-ssh-challenge-secret")


@pytest.fixture
def approved_user(monkeypatch):
    user = {"id": "00000000-0000-0000-0000-000000000001", "is_approved": True}

    async def _require(request, db):
        return user

    monkeypatch.setattr(main, "require_approved_user", _require)
    return user


@pytest.mark.asyncio
async def test_challenge_is_reusable_but_duplicate_key_is_rejected_by_fingerprint(
    approved_user, monkeypatch
):
    """Not single-use at the token layer, by design (ruling F24).

    Binding the token to the caller's user id is what carries the security
    property a stateful single-use store would have provided: a captured
    (challenge, signature) pair can't be replayed to register someone else's
    key, because the embedded user id is checked against the authenticated
    caller (see the cross-user test below). Replaying your OWN token just
    re-registers your own key, which the database's fingerprint-uniqueness
    constraint rejects — surfaced as 409 here, not the 400 "unknown
    challenge" a dict-backed single-use store would have produced on the
    second call. If this starts asserting 400, someone put the nonce store
    back — see the comment on ``_mint_ssh_key_challenge``.
    """
    from database.postgres import SshKeyAlreadyRegistered

    challenge = await main.create_ssh_key_challenge(request=object())
    assert challenge["namespace"]
    assert len(challenge["challenge"]) >= 32

    monkeypatch.setattr(
        main,
        "parse_public_key",
        lambda text: _Body(
            key_type="ssh-ed25519",
            public_key=text,
            fingerprint_sha256="SHA256:" + "A" * 43,
            comment="",
        ),
    )
    monkeypatch.setattr(main, "verify_possession", lambda *a, **k: True)

    calls = {"n": 0}

    async def _create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "id": "k1",
                "name": kwargs["name"],
                "key_type": "ssh-ed25519",
                "fingerprint_sha256": "SHA256:" + "A" * 43,
                "created_at": None,
                "last_used_at": None,
                "disabled_at": None,
            }
        raise SshKeyAlreadyRegistered("SHA256:" + "A" * 43)

    monkeypatch.setattr(main.postgres_db, "create_user_ssh_key", _create)

    body = _Body(
        name="laptop",
        public_key="ssh-ed25519 AAAA",
        challenge=challenge["challenge"],
        signature="-----BEGIN SSH SIGNATURE-----",
    )
    first = await main.create_ssh_key(request=object(), body=body)
    assert first["id"] == "k1"

    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key(request=object(), body=body)
    assert excinfo.value.status_code == 409
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_challenge_minted_for_one_user_is_rejected_for_another(monkeypatch):
    """The exact lockout attack the possession check exists to prevent:
    replaying a captured (challenge, signature) pair to register a key under
    a *different* account. A stateless token only carries this property if
    the embedded user id is checked against the authenticated caller, not
    just the signature — this pins that check.
    """
    user_a = {"id": "00000000-0000-0000-0000-0000000000aa", "is_approved": True}
    user_b = {"id": "00000000-0000-0000-0000-0000000000bb", "is_approved": True}

    async def _require_a(request, db):
        return user_a

    monkeypatch.setattr(main, "require_approved_user", _require_a)
    challenge = await main.create_ssh_key_challenge(request=object())

    async def _require_b(request, db):
        return user_b

    monkeypatch.setattr(main, "require_approved_user", _require_b)
    monkeypatch.setattr(
        main,
        "parse_public_key",
        lambda text: _Body(
            key_type="ssh-ed25519",
            public_key=text,
            fingerprint_sha256="SHA256:" + "B" * 43,
            comment="",
        ),
    )
    monkeypatch.setattr(main, "verify_possession", lambda *a, **k: True)

    body = _Body(
        name="stolen",
        public_key="ssh-ed25519 AAAA",
        challenge=challenge["challenge"],
        signature="sig",
    )
    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key(request=object(), body=body)
    assert excinfo.value.status_code == 400


def test_expired_challenge_is_rejected():
    """Five-minute window, enforced from the embedded (HMAC-protected)
    expiry rather than any external state.
    """
    user_id = "00000000-0000-0000-0000-000000000042"
    past = time.time() - 10_000
    token, expires_at = main._mint_ssh_key_challenge(user_id, now=past)
    assert expires_at <= time.time()
    assert not main._verify_ssh_key_challenge(token, user_id)


def test_tampered_challenge_is_rejected():
    """Flipping one character anywhere in the token must invalidate the
    HMAC — this is what makes the embedded user id and expiry trustworthy.
    """
    user_id = "00000000-0000-0000-0000-000000000042"
    token, _ = main._mint_ssh_key_challenge(user_id)
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert tampered != token
    assert not main._verify_ssh_key_challenge(tampered, user_id)


@pytest.mark.asyncio
async def test_tampered_challenge_is_rejected_through_the_endpoint(
    approved_user, monkeypatch
):
    challenge = await main.create_ssh_key_challenge(request=object())
    original = challenge["challenge"]
    tampered = original[:-1] + ("0" if original[-1] != "0" else "1")

    monkeypatch.setattr(
        main,
        "parse_public_key",
        lambda text: _Body(
            key_type="ssh-ed25519",
            public_key=text,
            fingerprint_sha256="SHA256:" + "A" * 43,
            comment="",
        ),
    )
    monkeypatch.setattr(main, "verify_possession", lambda *a, **k: True)
    body = _Body(
        name="laptop",
        public_key="ssh-ed25519 AAAA",
        challenge=tampered,
        signature="sig",
    )
    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key(request=object(), body=body)
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_challenge_endpoint_503_when_secret_is_empty(approved_user, monkeypatch):
    """Fail closed: main.py:1403 only logs a warning for an empty
    SESSION_JWT_SECRET today. The challenge endpoint must not rely on that —
    an empty secret is a well-known HMAC key, so every replica would sign
    forgeable tokens.
    """
    monkeypatch.setattr(main, "_session_jwt_secret", "")
    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key_challenge(request=object())
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_create_ssh_key_503_when_secret_is_empty(approved_user, monkeypatch):
    """Same fail-closed guard on the redemption side: if the secret goes
    empty between mint and redeem, verifying the challenge would be
    meaningless, so refuse outright rather than accept.
    """
    challenge = await main.create_ssh_key_challenge(request=object())
    monkeypatch.setattr(main, "_session_jwt_secret", "")
    body = _Body(
        name="laptop",
        public_key="ssh-ed25519 AAAA",
        challenge=challenge["challenge"],
        signature="sig",
    )
    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key(request=object(), body=body)
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_rejects_unproven_key(approved_user, monkeypatch):
    challenge = await main.create_ssh_key_challenge(request=object())
    monkeypatch.setattr(
        main,
        "parse_public_key",
        lambda text: _Body(
            key_type="ssh-ed25519",
            public_key=text,
            fingerprint_sha256="SHA256:" + "A" * 43,
            comment="",
        ),
    )
    monkeypatch.setattr(main, "verify_possession", lambda *a, **k: False)
    body = _Body(
        name="laptop",
        public_key="ssh-ed25519 AAAA",
        challenge=challenge["challenge"],
        signature="bogus",
    )
    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key(request=object(), body=body)
    assert excinfo.value.status_code == 400
    assert "possession" in excinfo.value.detail.lower()


@pytest.mark.asyncio
async def test_rejects_bad_key_with_its_reason(approved_user, monkeypatch):
    challenge = await main.create_ssh_key_challenge(request=object())

    def _reject(text):
        raise SshKeyRejected("RSA keys must be at least 3072 bits; this one is 2048.")

    monkeypatch.setattr(main, "parse_public_key", _reject)
    body = _Body(
        name="old",
        public_key="ssh-rsa AAAA",
        challenge=challenge["challenge"],
        signature="x",
    )
    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key(request=object(), body=body)
    assert excinfo.value.status_code == 400
    assert "3072" in excinfo.value.detail


@pytest.mark.asyncio
async def test_duplicate_fingerprint_is_409_with_a_recovery_path(
    approved_user, monkeypatch
):
    from database.postgres import SshKeyAlreadyRegistered

    challenge = await main.create_ssh_key_challenge(request=object())
    monkeypatch.setattr(
        main,
        "parse_public_key",
        lambda text: _Body(
            key_type="ssh-ed25519",
            public_key=text,
            fingerprint_sha256="SHA256:" + "A" * 43,
            comment="",
        ),
    )
    monkeypatch.setattr(main, "verify_possession", lambda *a, **k: True)

    async def _boom(**kwargs):
        raise SshKeyAlreadyRegistered("SHA256:" + "A" * 43)

    monkeypatch.setattr(main.postgres_db, "create_user_ssh_key", _boom)
    body = _Body(
        name="laptop",
        public_key="ssh-ed25519 AAAA",
        challenge=challenge["challenge"],
        signature="x",
    )
    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key(request=object(), body=body)
    assert excinfo.value.status_code == 409
    assert "support" in excinfo.value.detail.lower()


@pytest.mark.asyncio
async def test_delete_reports_miss(approved_user, monkeypatch):
    async def _delete(key_id, user_id):
        return False

    monkeypatch.setattr(main.postgres_db, "delete_user_ssh_key", _delete)
    with pytest.raises(HTTPException) as excinfo:
        await main.delete_ssh_key(request=object(), key_id="k1")
    assert excinfo.value.status_code == 404
