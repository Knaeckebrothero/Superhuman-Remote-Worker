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

Fix round 1 (review findings) adds: the token carries a fifth, MAC-covered
but display-only identity clause so a signer can see whose account they're
about to bind (``test_identity_*`` / ``test_minted_token_contains_the_
identity_clause``) — closing a confused-deputy phishing case a bare UUID
enabled; a ``str.isascii()`` guard so a non-ASCII or lone-surrogate challenge
is rejected rather than crashing ``hmac.compare_digest`` into an unhandled
500 (``test_*non_ascii*`` / ``test_*surrogate*``); fail-closed checks moved
into the helpers themselves, not just the endpoints (``test_mint_raises_
when_secret_is_empty`` / ``test_verify_returns_false_when_secret_is_empty``);
a malformed ``key_id`` on delete folded into the existing 404
(``test_delete_ssh_key_malformed_id_is_404_not_500``); and wiring tests
distinct from logic tests — route registration, the previously-untested GET,
the delete happy path, both branches of ``_serialize_ssh_key_row``'s
``.isoformat()`` calls, and pinned arguments into ``verify_possession`` and
the delete store call.
"""

import json
import time
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

import main
from services.ssh_public_keys import SshKeyRejected
from tests._route_inventory import mounted_routes


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
async def test_key_cap_is_409_naming_the_cap(approved_user, monkeypatch):
    """Spec §4.1 caps registrations at ``MAX_SSH_KEYS_PER_USER``.

    Also 409, like the duplicate above, but a different recovery path — this
    one the user can fix alone, which is why the number has to be in the
    message. The store raises; this pins the translation.
    """
    from database.postgres import MAX_SSH_KEYS_PER_USER, SshKeyLimitReached

    challenge = await main.create_ssh_key_challenge(request=object())
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

    async def _capped(**kwargs):
        raise SshKeyLimitReached(MAX_SSH_KEYS_PER_USER)

    monkeypatch.setattr(main.postgres_db, "create_user_ssh_key", _capped)
    body = _Body(
        name="laptop",
        public_key="ssh-ed25519 AAAA",
        challenge=challenge["challenge"],
        signature="x",
    )
    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key(request=object(), body=body)
    assert excinfo.value.status_code == 409
    assert str(MAX_SSH_KEYS_PER_USER) in excinfo.value.detail
    # Not the duplicate-fingerprint message, which points at support.
    assert "support" not in excinfo.value.detail.lower()


@pytest.mark.asyncio
async def test_delete_reports_miss(approved_user, monkeypatch):
    async def _delete(key_id, user_id):
        return False

    monkeypatch.setattr(main.postgres_db, "delete_user_ssh_key", _delete)
    with pytest.raises(HTTPException) as excinfo:
        await main.delete_ssh_key(request=object(), key_id="k1")
    assert excinfo.value.status_code == 404


# =============================================================================
# Final review, Important 6 — a project-scoped MCP token must not mint or
# revoke a personal SSH credential.
# =============================================================================


@pytest.fixture
def project_scoped_user(monkeypatch):
    """An approved user authenticated by a legacy ``project:<uuid>``-scoped
    MCP token. ``user_can_access_job_or_thread`` denies this principal every
    thread — the IDE included — via ``_scope_permits_personal``."""
    user = {
        "id": "00000000-0000-0000-0000-000000000001",
        "is_approved": True,
        "scopes": ["project:11111111-1111-1111-1111-111111111111"],
    }

    async def _require(request, db):
        return user

    async def _no_audit(**kwargs):
        return None

    monkeypatch.setattr(main, "require_approved_user", _require)
    monkeypatch.setattr(main.postgres_db, "record_security_event", _no_audit)
    return user


def test_the_scope_helper_actually_denies_this_principal(project_scoped_user):
    """Guards the fixture, not the endpoint: if the scope field were named
    something ``_scope_project_id`` does not read, the two tests below would
    pass for the wrong reason — the principal would simply be unscoped.
    """
    from security import access

    assert access._scope_project_id(project_scoped_user) is not None
    assert not access._scope_permits_personal(project_scoped_user)


@pytest.mark.asyncio
async def test_project_scoped_token_cannot_register_a_key(
    project_scoped_user, monkeypatch
):
    """Registration composes with resolution into something neither is alone:
    an SSH key authenticates by fingerprint, so the token's scope is gone by
    the time authorization runs. The gate therefore has to be at minting."""

    def _tripwire(*a, **k):
        raise AssertionError("must refuse before parsing the key")

    monkeypatch.setattr(main, "parse_public_key", _tripwire)
    body = _Body(
        name="laptop",
        public_key="ssh-ed25519 AAAA",
        challenge="whatever",
        signature="x",
    )
    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key(request=object(), body=body)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_project_scoped_token_cannot_list_keys(project_scoped_user, monkeypatch):
    """Listing is read-only and leaks no id the token could act on, so this is
    the weakest of the three gates. It exists for contract consistency:
    _scope_permits_personal's own docstring says such a token "shouldn't be
    able to read or mutate" a personal resource, and leaving one of the three
    call sites open would make them disagree with the helper they cite."""

    async def _tripwire(user_id):
        raise AssertionError("must refuse before reaching the store")

    monkeypatch.setattr(main.postgres_db, "list_user_ssh_keys", _tripwire)
    with pytest.raises(HTTPException) as excinfo:
        await main.list_ssh_keys(request=object())
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_project_scoped_token_cannot_delete_a_key(
    project_scoped_user, monkeypatch
):
    """Deletion is the only revocation this feature has, so leaving it open
    while gating create would let a project-scoped token strip its owner's
    access."""

    async def _tripwire(key_id, user_id):
        raise AssertionError("must refuse before reaching the store")

    monkeypatch.setattr(main.postgres_db, "delete_user_ssh_key", _tripwire)
    with pytest.raises(HTTPException) as excinfo:
        await main.delete_ssh_key(
            request=object(), key_id="00000000-0000-0000-0000-0000000000aa"
        )
    assert excinfo.value.status_code == 403


# =============================================================================
# Fix round 1 — wiring tests distinct from logic tests (review Minor 6)
# =============================================================================


def test_ssh_key_routes_are_mounted():
    """Every test in this file calls handlers directly, so none of them
    prove FastAPI actually serves these paths at these methods — a typo'd
    decorator path or a route registered under the wrong verb would pass
    every other test here and still 404 in production.
    """
    routes = mounted_routes(main.app)
    assert ("POST", "/api/ssh-keys/challenge") in routes
    assert ("POST", "/api/ssh-keys") in routes
    assert ("GET", "/api/ssh-keys") in routes
    assert ("DELETE", "/api/ssh-keys/{key_id}") in routes


@pytest.mark.asyncio
async def test_list_ssh_keys(approved_user, monkeypatch):
    """No test exercised GET /api/ssh-keys at all before this."""

    async def _list(user_id):
        assert user_id == approved_user["id"]
        return [
            {
                "id": "k1",
                "name": "laptop",
                "key_type": "ssh-ed25519",
                "fingerprint_sha256": "SHA256:" + "A" * 43,
                "created_at": None,
                "last_used_at": None,
                "disabled_at": None,
            }
        ]

    monkeypatch.setattr(main.postgres_db, "list_user_ssh_keys", _list)
    result = await main.list_ssh_keys(request=object())
    assert result == [
        {
            "id": "k1",
            "name": "laptop",
            "key_type": "ssh-ed25519",
            "fingerprint": "SHA256:" + "A" * 43,
            "created_at": None,
            "last_used_at": None,
            "disabled": False,
        }
    ]


@pytest.mark.asyncio
async def test_delete_ssh_key_happy_path(approved_user, monkeypatch):
    """The delete happy path, including its response body, plus argument
    pinning: the user_id that reaches the store must be the authenticated
    caller's, not something derived from the path or body.
    """
    captured = {}

    async def _delete(key_id, user_id):
        captured["key_id"] = key_id
        captured["user_id"] = user_id
        return True

    monkeypatch.setattr(main.postgres_db, "delete_user_ssh_key", _delete)
    result = await main.delete_ssh_key(request=object(), key_id="k1")
    assert result == {"status": "deleted"}
    assert captured == {"key_id": "k1", "user_id": approved_user["id"]}


@pytest.mark.asyncio
async def test_delete_ssh_key_malformed_id_is_404_not_500(approved_user, monkeypatch):
    """Review Minor 3: the store's ``UUID(key_id)`` raises ``ValueError`` on
    a malformed id. That must fold into the existing "not found" outcome,
    not surface as an unhandled 500.
    """

    async def _delete(key_id, user_id):
        raise ValueError("badly formed hexadecimal UUID string")

    monkeypatch.setattr(main.postgres_db, "delete_user_ssh_key", _delete)
    with pytest.raises(HTTPException) as excinfo:
        await main.delete_ssh_key(request=object(), key_id="not-a-uuid")
    assert excinfo.value.status_code == 404


def test_serialize_ssh_key_row_with_timestamps():
    """The truthy branch of both ``.isoformat()`` calls — every other
    fixture in this file passes ``created_at: None``.
    """
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    used = datetime(2026, 1, 2, tzinfo=timezone.utc)
    row = {
        "id": "k1",
        "name": "laptop",
        "key_type": "ssh-ed25519",
        "fingerprint_sha256": "SHA256:" + "A" * 43,
        "created_at": created,
        "last_used_at": used,
        "disabled_at": None,
    }
    result = main._serialize_ssh_key_row(row)
    assert result["created_at"] == created.isoformat()
    assert result["last_used_at"] == used.isoformat()
    assert result["disabled"] is False


def test_serialize_ssh_key_row_without_timestamps():
    row = {
        "id": "k1",
        "name": "laptop",
        "key_type": "ssh-ed25519",
        "fingerprint_sha256": "SHA256:" + "A" * 43,
        "created_at": None,
        "last_used_at": None,
        "disabled_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    result = main._serialize_ssh_key_row(row)
    assert result["created_at"] is None
    assert result["last_used_at"] is None
    assert result["disabled"] is True


@pytest.mark.asyncio
async def test_verify_possession_receives_parsed_key_namespace_and_challenge(
    approved_user, monkeypatch
):
    """Argument pinning on verify_possession: the PARSED (normalized)
    public key — not the raw request body string — the module's
    SIGNATURE_NAMESPACE, and the challenge string (encoded) as the signed
    payload, plus the signature verbatim.
    """
    challenge = await main.create_ssh_key_challenge(request=object())

    monkeypatch.setattr(
        main,
        "parse_public_key",
        lambda text: _Body(
            key_type="ssh-ed25519",
            public_key="ssh-ed25519 AAAA-normalized comment",
            fingerprint_sha256="SHA256:" + "A" * 43,
            comment="comment",
        ),
    )

    captured = {}

    def _verify(public_key, namespace, payload, signature):
        captured["public_key"] = public_key
        captured["namespace"] = namespace
        captured["payload"] = payload
        captured["signature"] = signature
        return True

    monkeypatch.setattr(main, "verify_possession", _verify)

    async def _create(**kwargs):
        return {
            "id": "k1",
            "name": kwargs["name"],
            "key_type": "ssh-ed25519",
            "fingerprint_sha256": "SHA256:" + "A" * 43,
            "created_at": None,
            "last_used_at": None,
            "disabled_at": None,
        }

    monkeypatch.setattr(main.postgres_db, "create_user_ssh_key", _create)

    body = _Body(
        name="laptop",
        public_key="ssh-ed25519 AAAA raw-comment",
        challenge=challenge["challenge"],
        signature="the-signature",
    )
    await main.create_ssh_key(request=object(), body=body)

    assert captured["public_key"] == "ssh-ed25519 AAAA-normalized comment"
    assert captured["namespace"] == main.SIGNATURE_NAMESPACE
    assert captured["payload"] == challenge["challenge"].encode("utf-8")
    assert captured["signature"] == "the-signature"


# =============================================================================
# Fix round 1 — Important 1: non-ASCII challenge must reject, never raise
# =============================================================================


def test_verify_ssh_key_challenge_rejects_non_ascii_without_raising():
    """Reproduces the review finding directly: ``hmac.compare_digest``
    raises ``TypeError`` on a non-ASCII ``str`` argument, reachable
    pre-authentication since the raw signature field is compared before its
    validity is known. Before the ``isascii()`` guard this token would have
    raised out of ``_verify_ssh_key_challenge`` instead of returning False.
    """
    token = "srw-ssh1:a:b:c:d:\u00e9"
    assert main._verify_ssh_key_challenge(token, "b") is False


def test_verify_ssh_key_challenge_rejects_lone_surrogate():
    """The specific case the review flagged as the wrong direction to fix
    this in: a lone UTF-16 surrogate is reachable through ``json.loads`` on
    a hostile request body and is non-ASCII (so ``isascii()`` catches it
    too), but would raise ``UnicodeEncodeError`` — not ``TypeError`` — if
    this were "fixed" by encoding to bytes and comparing instead, which
    just moves the crash rather than closing it.
    """
    token = json.loads('{"c": "srw-ssh1:a:b:c:d:\\udcff"}')["c"]
    with pytest.raises(UnicodeEncodeError):
        token.encode("utf-8")  # documents why encode-first is not the fix
    assert main._verify_ssh_key_challenge(token, "b") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_challenge",
    [
        "srw-ssh1:a:b:c:d:\u00e9",
        # A lone surrogate is the case that pins the ORDER, not just the guard:
        # it encodes to UTF-8 with a UnicodeEncodeError rather than succeeding,
        # so this input fails only if verification runs BEFORE
        # body.challenge.encode("utf-8"). The `\u00e9` case above passes either
        # way, because it encodes cleanly.
        "srw-ssh1:a:b:c:d:\udcff",
    ],
)
async def test_non_ascii_challenge_is_rejected_not_raised_through_endpoint(
    approved_user, monkeypatch, bad_challenge
):
    """End-to-end: a non-ASCII challenge in the request body must come back
    as a 4xx HTTPException, never an unhandled exception an authenticated
    caller could loop to produce logged 500s.
    """
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
    body = _Body(
        name="laptop",
        public_key="ssh-ed25519 AAAA",
        challenge=bad_challenge,
        signature="sig",
    )
    with pytest.raises(HTTPException) as excinfo:
        await main.create_ssh_key(request=object(), body=body)
    assert excinfo.value.status_code == 400


# =============================================================================
# Fix round 1 — Important 2: identity clause, MAC-covered, display-only
# =============================================================================


def test_minted_token_contains_the_identity_clause():
    token, _ = main._mint_ssh_key_challenge("user-a-id", "alice@example.com")
    assert "alice@example.com" in token.split(":")


def test_identity_label_is_covered_by_the_mac():
    """Flipping the identity clause after minting must invalidate the token
    — otherwise the label wouldn't actually be trustworthy to a signer."""
    token, _ = main._mint_ssh_key_challenge("user-a-id", "alice")
    assert main._verify_ssh_key_challenge(token, "user-a-id") is True
    tampered = token.replace(":alice:", ":mallory:")
    assert tampered != token
    assert main._verify_ssh_key_challenge(tampered, "user-a-id") is False


def test_identity_label_is_never_consulted_for_authorization():
    """The confused-deputy fix itself (review Important 2): the identity
    clause is display-only. A token minted for ``user-a-id`` whose label
    happens to name a different account must still authorize ONLY
    ``user-a-id`` — never the account the label names.
    """
    token, _ = main._mint_ssh_key_challenge("user-a-id", "looks-like-user-b")
    assert main._verify_ssh_key_challenge(token, "user-a-id") is True
    assert main._verify_ssh_key_challenge(token, "looks-like-user-b") is False


def test_identity_with_whitespace_falls_back_to_user_id():
    token, _ = main._mint_ssh_key_challenge("user-a-id", "alice smith")
    assert "alice smith" not in token
    assert "user-a-id" in token.split(":")


def test_non_ascii_identity_falls_back_to_user_id():
    token, _ = main._mint_ssh_key_challenge("user-a-id", "\u00c9tienne")
    assert "\u00c9tienne" not in token
    assert token.isascii()
    assert "user-a-id" in token.split(":")


def test_overlong_identity_falls_back_to_user_id():
    long_label = "x" * (main._SSH_CHALLENGE_IDENTITY_MAX_LEN + 1)
    token, _ = main._mint_ssh_key_challenge("user-a-id", long_label)
    assert long_label not in token
    assert "user-a-id" in token.split(":")


@pytest.mark.parametrize(
    "label",
    [
        "vict\x1b[2Kmallory@srw.works",  # ESC: rewrites the line as it renders
        "alice\x08\x08\x08\x08\x08mallory",  # backspaces: erases what precedes
        "alice\x00mallory",  # NUL: truncates in C-string consumers
        "alice\x7f",  # DEL
    ],
)
def test_control_character_identity_falls_back_to_user_id(label):
    """A control character is ASCII and is not whitespace, so it slipped past
    the other three guards. It defeats the exact property the identity clause
    exists to provide: a signer inspecting the token to see whose account it
    binds can have that display rewritten by terminal escapes, putting them
    back in the phished state the label was added to prevent. The MAC covering
    the label does not help — the label is authentic, it just does not render
    as what it is.
    """
    token, _ = main._mint_ssh_key_challenge("user-a-id", label)
    assert label not in token
    assert token.isprintable()
    assert "user-a-id" in token.split(":")


def test_empty_identity_falls_back_to_user_id():
    token, _ = main._mint_ssh_key_challenge("user-a-id", None)
    assert "user-a-id" in token.split(":")


# =============================================================================
# Fix round 1 — Minor 5: fail-closed lives on the helpers, not just callers
# =============================================================================


def test_mint_raises_when_secret_is_empty(monkeypatch):
    monkeypatch.setattr(main, "_session_jwt_secret", "")
    with pytest.raises(RuntimeError):
        main._mint_ssh_key_challenge("user-a-id", "alice")


def test_verify_returns_false_when_secret_is_empty(monkeypatch):
    token, _ = main._mint_ssh_key_challenge("user-a-id", "alice")
    monkeypatch.setattr(main, "_session_jwt_secret", "")
    assert main._verify_ssh_key_challenge(token, "user-a-id") is False
