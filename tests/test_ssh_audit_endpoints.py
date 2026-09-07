"""Tests for the SSH-gateway audit endpoints (Task 6A, plus fix round 1).

Plan 1 built mark_ssh_key_used, record_ssh_attachment and
close_ssh_attachment on PostgresDB but left them with no caller — the
gateway holds no DB credentials and reaches the orchestrator only over
HTTP with an X-Internal-Key header. This module covers the three endpoints
that make them callable:

* ``POST /api/internal/ssh-keys/used`` is keyed by fingerprint (not
  key_id), since the gateway calls this from auth_completed() before it
  has resolved a target/key id. Unknown fingerprint is a silent success,
  not a 404, to avoid an existence oracle. A failed bump (fix round 1,
  Minor 2) must not surface as a 500 — the call is best-effort bookkeeping
  after an authentication that already succeeded.
* ``POST /api/internal/ssh-attachments`` opens an audit row and hands back
  its id. Fix round 1, Important 1: the original body took ``thread_id``/
  ``user_id``/``ssh_key_id`` as asserted values, which any
  ``X-Internal-Key`` holder (every agent pod) could forge — the audit
  table's whole purpose is a trustworthy record of who reached a workspace
  over SSH. The body now carries only ``fingerprint``/``handle``/
  ``client_ip``; identity is resolved server-side on the same path
  ``get_ssh_target`` uses, reusing its opaque-404 contract for every
  resolution failure. A foreign-key violation on the insert (Minor 1) also
  maps to 400, not 500.
* ``POST /api/internal/ssh-attachments/{id}/close`` returns the row count
  from close_ssh_attachment honestly — 0 for an unknown or already-closed
  id — rather than turning that into an error.
* ``ssh_key_id`` is added to the get_ssh_target response so the gateway can
  log a key id — no longer load-bearing for attachment-create, which
  resolves its own copy server-side, but kept for that purpose.
* Fix round 1, Minor 4: ``fingerprint`` (both endpoints) and ``channels``
  are capped, so an internal-key holder cannot inflate a query or an audit
  row arbitrarily.

These cases first passed against the original handlers in ``main`` and now
exercise ``orchestrator.routers.ssh_access``, which keeps the identity
resolution and its opaque 404 while the store writes moved to
``orchestrator.services.ssh_access``.
"""

import asyncpg
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import orchestrator.main
from orchestrator.routers import ssh_access as ssh_access_routes
from orchestrator.schemas.ssh_access import (
    SshAttachmentClose,
    SshAttachmentCreate,
    SshKeyUsedRequest,
)
from orchestrator.services.workspace_suspension import _thread_is_vm_tier
from tests._ssh_access_harness import SshAccessHarness
from tests._route_inventory import mounted_routes

USER = "00000000-0000-0000-0000-000000000001"
THREAD = "00000000-0000-0000-0000-000000000002"
KEY_ID = "00000000-0000-0000-0000-0000000000ab"
ATTACHMENT_ID = "00000000-0000-0000-0000-0000000000cd"
FINGERPRINT = "SHA256:" + "A" * 43


def _thread(**over):
    base = {
        "id": THREAD,
        "user_id": USER,
        "status": "active",
        "execution_lane": "pinned",
        "runtime_retirement_token": None,
    }
    base.update(over)
    return base


@pytest.fixture
def harness():
    built = SshAccessHarness()
    built.thread_is_vm_tier = _thread_is_vm_tier
    return built


_DEFAULT_USER = object()  # sentinel: "user" itself must be able to mean None


def _resolved(harness, *, thread_id=THREAD, user=_DEFAULT_USER, access=True):
    """Wire the three lookups internal_create_ssh_attachment performs, the
    same three get_ssh_target performs. Shared by every create-attachment
    test below so each one only overrides what it's testing."""
    if user is _DEFAULT_USER:
        user = {"id": USER, "ssh_key_id": KEY_ID}

    async def _thread_id_fn(handle):
        return thread_id

    async def _user_fn(fp):
        return user

    async def _access_fn(u, db, entity_id):
        return access

    harness.store.set("get_thread_id_by_ssh_handle", _thread_id_fn)
    harness.store.set("resolve_user_by_ssh_fingerprint", _user_fn)
    harness.user_can_access_ide_entity = _access_fn


def _close_resolved(harness, *, thread_id=THREAD, user=_DEFAULT_USER, access=True):
    """Wire the three lookups internal_close_ssh_attachment performs (fix
    round 2) -- the same shape as _resolved above, but keyed off
    get_ssh_attachment_thread_id(attachment_id) rather than
    get_thread_id_by_ssh_handle(handle), since close has no handle."""
    if user is _DEFAULT_USER:
        user = {"id": USER, "ssh_key_id": KEY_ID}

    async def _thread_id_fn(attachment_id):
        return thread_id

    async def _user_fn(fp):
        return user

    async def _access_fn(u, db, entity_id):
        return access

    harness.store.set("get_ssh_attachment_thread_id", _thread_id_fn)
    harness.store.set("resolve_user_by_ssh_fingerprint", _user_fn)
    harness.user_can_access_ide_entity = _access_fn


async def _mark_used(harness, fingerprint=FINGERPRINT):
    return await ssh_access_routes.internal_mark_ssh_key_used(
        request=object(),
        body=SshKeyUsedRequest(fingerprint=fingerprint),
        dependencies=harness.dependencies,
    )


async def _create_attachment(harness, body):
    return await ssh_access_routes.internal_create_ssh_attachment(
        request=object(), body=body, dependencies=harness.dependencies
    )


async def _close_attachment(harness, body, attachment_id=ATTACHMENT_ID):
    return await ssh_access_routes.internal_close_ssh_attachment(
        request=object(),
        attachment_id=attachment_id,
        body=body,
        dependencies=harness.dependencies,
    )


async def _deny_internal(request):
    raise HTTPException(status_code=401, detail="Invalid internal key")


# =============================================================================
# X-Internal-Key enforcement
# =============================================================================


@pytest.mark.asyncio
async def test_mark_used_requires_internal_key(harness):
    harness.require_internal = _deny_internal
    with pytest.raises(HTTPException) as excinfo:
        await _mark_used(harness)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_create_attachment_requires_internal_key(harness):
    harness.require_internal = _deny_internal
    with pytest.raises(HTTPException) as excinfo:
        await _create_attachment(
            harness,
            SshAttachmentCreate(
                fingerprint=FINGERPRINT,
                client_ip="10.0.0.1",
                handle="s-7f3a91c2",
            ),
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_close_attachment_requires_internal_key(harness):
    harness.require_internal = _deny_internal
    with pytest.raises(HTTPException) as excinfo:
        await _close_attachment(
            harness,
            SshAttachmentClose(fingerprint=FINGERPRINT, channels=["session"]),
        )
    assert excinfo.value.status_code == 401


# =============================================================================
# POST /api/internal/ssh-keys/used
# =============================================================================


@pytest.mark.asyncio
async def test_mark_used_happy_path_resolves_fingerprint_to_key_id(harness):
    """The endpoint's whole reason to exist: it is handed a fingerprint and
    must resolve it to a key id itself before bumping ``last_used_at``,
    because the gateway holds nothing else at the moment it calls this."""

    async def _user(fp):
        assert fp == FINGERPRINT
        return {"id": USER, "ssh_key_id": KEY_ID}

    calls = []

    async def _mark(key_id, fingerprint_sha256):
        calls.append((key_id, fingerprint_sha256))

    harness.store.set("resolve_user_by_ssh_fingerprint", _user)
    harness.store.set("mark_ssh_key_used", _mark)

    result = await _mark_used(harness)
    assert calls == [(KEY_ID, FINGERPRINT)]
    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_mark_used_unknown_fingerprint_is_silent_success_no_write(harness):
    """An unknown fingerprint must not 404 (existence oracle) and must not
    reach ``mark_ssh_key_used`` at all — there is no key id to bump."""

    async def _no_user(fp):
        return None

    async def _tripwire(key_id, fingerprint_sha256):
        raise AssertionError("mark_ssh_key_used must not be called for an unknown key")

    harness.store.set("resolve_user_by_ssh_fingerprint", _no_user)
    harness.store.set("mark_ssh_key_used", _tripwire)

    assert await _mark_used(harness) == {"status": "ok"}


@pytest.mark.asyncio
async def test_mark_used_db_hiccup_does_not_500(harness):
    """Fix round 1, Minor 2: mark_ssh_key_used's own docstring says to wrap
    the call site — a failed bump must not discard an authentication that
    already succeeded. A transient DB error here must not propagate."""

    async def _user(fp):
        return {"id": USER, "ssh_key_id": KEY_ID}

    async def _boom(key_id, fingerprint_sha256):
        raise asyncpg.PostgresConnectionError("connection lost")

    harness.store.set("resolve_user_by_ssh_fingerprint", _user)
    harness.store.set("mark_ssh_key_used", _boom)

    assert await _mark_used(harness) == {"status": "ok"}


def test_mark_used_fingerprint_is_capped():
    """Fix round 1, Minor 4: an unbounded fingerprint reaches a SQL
    predicate verbatim. 500KB is comfortably past any real fingerprint."""
    with pytest.raises(ValidationError):
        SshKeyUsedRequest(fingerprint="A" * (500 * 1024))


# =============================================================================
# POST /api/internal/ssh-attachments
# =============================================================================


@pytest.mark.asyncio
async def test_create_attachment_happy_path(harness):
    """thread_id/user_id/ssh_key_id passed to the DB layer are the
    SERVER-resolved ones, not anything from the body — the body no longer
    even carries those fields (Important 1)."""
    _resolved(harness, user={"id": USER, "ssh_key_id": KEY_ID})
    captured = {}

    async def _record(thread_id, user_id, ssh_key_id, client_ip, handle):
        captured.update(
            thread_id=thread_id,
            user_id=user_id,
            ssh_key_id=ssh_key_id,
            client_ip=client_ip,
            handle=handle,
        )
        return ATTACHMENT_ID

    harness.store.set("record_ssh_attachment", _record)

    result = await _create_attachment(
        harness,
        SshAttachmentCreate(
            fingerprint=FINGERPRINT, client_ip="10.0.0.1", handle="s-7f3a91c2"
        ),
    )
    assert result == {"attachment_id": ATTACHMENT_ID}
    assert captured == {
        "thread_id": THREAD,
        "user_id": USER,
        "ssh_key_id": KEY_ID,
        "client_ip": "10.0.0.1",
        "handle": "s-7f3a91c2",
    }


@pytest.mark.asyncio
async def test_create_attachment_ssh_key_id_absent_is_none(harness):
    """A resolved user with no ``ssh_key_id`` (a plain ``{"id": ...}``, as a
    defensive caller might return) must pass ``None`` through, not crash."""
    _resolved(harness, user={"id": USER})

    async def _record(thread_id, user_id, ssh_key_id, client_ip, handle):
        assert ssh_key_id is None
        return ATTACHMENT_ID

    harness.store.set("record_ssh_attachment", _record)

    result = await _create_attachment(
        harness, SshAttachmentCreate(fingerprint=FINGERPRINT, handle="s-7f3a91c2")
    )
    assert result == {"attachment_id": ATTACHMENT_ID}


@pytest.mark.asyncio
async def test_create_attachment_ignores_any_caller_supplied_identity(harness):
    """Important 1's negative control: even if a caller smuggles thread_id/
    user_id into the raw JSON body, the record call must use the
    server-resolved identity, never the attacker's. Pydantic silently drops
    the unknown fields (matching the house pattern for internal POST
    bodies), so this also proves that drop isn't just cosmetic — nothing
    downstream ever sees the attacker's values, because the model has
    nowhere to put them.
    """
    ATTACKER_THREAD = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    ATTACKER_USER = "ffffffff-ffff-ffff-ffff-fffffffffffe"
    ATTACKER_KEY = "ffffffff-ffff-ffff-ffff-fffffffffffd"

    raw_body = {
        "fingerprint": FINGERPRINT,
        "handle": "s-7f3a91c2",
        "client_ip": "10.0.0.1",
        # attacker-supplied extras that must never reach the DB call:
        "thread_id": ATTACKER_THREAD,
        "user_id": ATTACKER_USER,
        "ssh_key_id": ATTACKER_KEY,
    }
    body = SshAttachmentCreate.model_validate(raw_body)
    assert not hasattr(body, "thread_id")
    assert not hasattr(body, "user_id")
    assert not hasattr(body, "ssh_key_id")

    _resolved(harness, user={"id": USER, "ssh_key_id": KEY_ID})
    captured = {}

    async def _record(thread_id, user_id, ssh_key_id, client_ip, handle):
        captured.update(thread_id=thread_id, user_id=user_id, ssh_key_id=ssh_key_id)
        return ATTACHMENT_ID

    harness.store.set("record_ssh_attachment", _record)

    await _create_attachment(harness, body)

    assert captured == {"thread_id": THREAD, "user_id": USER, "ssh_key_id": KEY_ID}
    assert ATTACKER_THREAD not in captured.values()
    assert ATTACKER_USER not in captured.values()
    assert ATTACKER_KEY not in captured.values()


def test_attachment_create_body_has_no_identity_fields():
    """Structural half of the negative control above: the schema itself
    must not accept an asserted identity, under any field name this task's
    original (rejected) design used."""
    fields = SshAttachmentCreate.model_fields
    assert "thread_id" not in fields
    assert "user_id" not in fields
    assert "ssh_key_id" not in fields


@pytest.mark.asyncio
async def test_create_attachment_unknown_handle_is_opaque_404(harness):
    _resolved(harness, thread_id=None)

    with pytest.raises(HTTPException) as excinfo:
        await _create_attachment(
            harness, SshAttachmentCreate(fingerprint=FINGERPRINT, handle="s-aaaaaaaa")
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_create_attachment_unknown_fingerprint_is_opaque_404(harness):
    _resolved(harness, user=None)

    with pytest.raises(HTTPException) as excinfo:
        await _create_attachment(
            harness, SshAttachmentCreate(fingerprint=FINGERPRINT, handle="s-7f3a91c2")
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_create_attachment_not_authorized_is_opaque_404(harness):
    """Same status/detail as the two tests above — mirrors get_ssh_target's
    anti-enumeration contract, which this endpoint deliberately reuses."""
    _resolved(harness, access=False)

    with pytest.raises(HTTPException) as not_yours:
        await _create_attachment(
            harness, SshAttachmentCreate(fingerprint=FINGERPRINT, handle="s-7f3a91c2")
        )

    _resolved(harness, thread_id=None)
    with pytest.raises(HTTPException) as unknown:
        await _create_attachment(
            harness, SshAttachmentCreate(fingerprint=FINGERPRINT, handle="s-aaaaaaaa")
        )

    assert not_yours.value.status_code == unknown.value.status_code == 404
    assert not_yours.value.detail == unknown.value.detail


@pytest.mark.asyncio
async def test_create_attachment_malformed_handle_is_opaque_404_before_touching_db(
    harness,
):
    called = False

    async def _tripwire(handle):
        nonlocal called
        called = True
        return None

    harness.store.set("get_thread_id_by_ssh_handle", _tripwire)

    with pytest.raises(HTTPException) as excinfo:
        await _create_attachment(
            harness,
            SshAttachmentCreate(
                fingerprint=FINGERPRINT, handle="s-abc\nProxyCommand x"
            ),
        )
    assert excinfo.value.status_code == 404
    assert called is False


@pytest.mark.asyncio
async def test_create_attachment_value_error_maps_to_400(harness):
    """Fix round 1, Minor 3 / process item 4: this must be a real, killing
    test, not one that passes whether or not the except block exists.
    Deleting ``except (ValueError, ...)`` around record_ssh_attachment in
    ``ssh_access.record_attachment`` lets this ValueError propagate
    uncaught, which is not an HTTPException — pytest.raises below then
    fails."""
    _resolved(harness, user={"id": USER, "ssh_key_id": KEY_ID})

    async def _record(*a, **kw):
        raise ValueError("invalid ssh handle: boom")

    harness.store.set("record_ssh_attachment", _record)

    with pytest.raises(HTTPException) as excinfo:
        await _create_attachment(
            harness, SshAttachmentCreate(fingerprint=FINGERPRINT, handle="s-7f3a91c2")
        )
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_create_attachment_fk_violation_maps_to_400(harness):
    """Fix round 1, Minor 1: a well-formed but nonexistent id (thread, user
    or key deleted between resolution and insert — a real race, e.g. a
    thread torn down mid-session) raises asyncpg.ForeignKeyViolationError,
    which must not escape as a 500."""
    _resolved(harness, user={"id": USER, "ssh_key_id": KEY_ID})

    async def _record(*a, **kw):
        raise asyncpg.ForeignKeyViolationError(
            'insert or update on table "ssh_attachments" violates foreign '
            "key constraint"
        )

    harness.store.set("record_ssh_attachment", _record)

    with pytest.raises(HTTPException) as excinfo:
        await _create_attachment(
            harness, SshAttachmentCreate(fingerprint=FINGERPRINT, handle="s-7f3a91c2")
        )
    assert excinfo.value.status_code < 500
    assert excinfo.value.status_code == 400


def test_create_attachment_fingerprint_is_capped():
    with pytest.raises(ValidationError):
        SshAttachmentCreate(fingerprint="A" * (500 * 1024), handle="s-7f3a91c2")


# =============================================================================
# POST /api/internal/ssh-attachments/{attachment_id}/close
# =============================================================================


@pytest.mark.asyncio
async def test_close_attachment_happy_path(harness):
    """A caller who can prove (via fingerprint) that it has access to the
    attachment's OWN thread may close it."""
    _close_resolved(harness, user={"id": USER, "ssh_key_id": KEY_ID})
    captured = {}

    async def _close(attachment_id, channels):
        captured["attachment_id"] = attachment_id
        captured["channels"] = channels
        return 1

    harness.store.set("close_ssh_attachment", _close)

    result = await _close_attachment(
        harness,
        SshAttachmentClose(fingerprint=FINGERPRINT, channels=["session", "sftp"]),
    )
    assert result == {"closed": 1}
    assert captured == {
        "attachment_id": ATTACHMENT_ID,
        "channels": ["session", "sftp"],
    }


@pytest.mark.asyncio
async def test_close_attachment_unknown_id_returns_zero_not_an_error(harness):
    """Fix round 2: an unknown attachment id has no thread_id to authorize
    against, so close_ssh_attachment must never even be called — this is
    now a refusal, not the "0 rows updated" case it used to be. The
    response shape is preserved anyway ({"closed": 0}), so the gateway's
    best-effort contract is unchanged even though the reason for the 0
    changed."""
    _close_resolved(harness, thread_id=None)

    async def _tripwire(attachment_id, channels):
        raise AssertionError("close_ssh_attachment must not run for an unknown id")

    harness.store.set("close_ssh_attachment", _tripwire)

    result = await _close_attachment(
        harness, SshAttachmentClose(fingerprint=FINGERPRINT, channels=[])
    )
    assert result == {"closed": 0}


@pytest.mark.asyncio
async def test_close_attachment_unknown_fingerprint_returns_zero_not_an_error(harness):
    """A caller that cannot be resolved to any user cannot be proven to have
    access to the attachment's thread, so this refuses the same way an
    unknown attachment id does."""
    _close_resolved(harness, user=None)

    async def _tripwire(attachment_id, channels):
        raise AssertionError(
            "close_ssh_attachment must not run for an unresolved caller"
        )

    harness.store.set("close_ssh_attachment", _tripwire)

    result = await _close_attachment(
        harness, SshAttachmentClose(fingerprint=FINGERPRINT, channels=[])
    )
    assert result == {"closed": 0}


@pytest.mark.asyncio
async def test_close_attachment_not_authorized_is_refused(harness):
    """The item the review actually asked for: a caller who resolves to a
    real user, and names a real attachment, but whose user cannot access
    THAT attachment's thread must be refused -- close_ssh_attachment must
    not run."""
    _close_resolved(harness, access=False)

    async def _tripwire(attachment_id, channels):
        raise AssertionError(
            "close_ssh_attachment must not run for an unauthorized user"
        )

    harness.store.set("close_ssh_attachment", _tripwire)

    result = await _close_attachment(
        harness, SshAttachmentClose(fingerprint=FINGERPRINT, channels=[])
    )
    assert result == {"closed": 0}


@pytest.mark.asyncio
async def test_close_attachment_unknown_and_unauthorized_are_indistinguishable(harness):
    """Second required test: a caller must not be able to tell "no such
    attachment" apart from "real attachment, not yours" -- both must
    produce the identical response, and neither may ever reach
    close_ssh_attachment (the thing being protected)."""

    async def _tripwire(attachment_id, channels):
        raise AssertionError("close_ssh_attachment must not run for either case")

    harness.store.set("close_ssh_attachment", _tripwire)

    _close_resolved(harness, thread_id=None)
    unknown = await _close_attachment(
        harness, SshAttachmentClose(fingerprint=FINGERPRINT, channels=[])
    )

    _close_resolved(harness, access=False)
    not_yours = await _close_attachment(
        harness, SshAttachmentClose(fingerprint=FINGERPRINT, channels=[])
    )

    assert unknown == not_yours == {"closed": 0}


@pytest.mark.asyncio
async def test_close_attachment_resolves_the_caller_even_for_an_unknown_id(harness):
    """Both lookups run unconditionally, mirroring ``get_ssh_target``:
    skipping the fingerprint resolution once the attachment id is already
    known-bad would make "was the id real" a timing oracle."""
    resolved_with = []

    async def _no_thread(attachment_id):
        return None

    async def _user(fp):
        resolved_with.append(fp)
        return {"id": USER}

    async def _access(u, db, entity_id):
        raise AssertionError("authorization cannot run without a thread")

    harness.store.set("get_ssh_attachment_thread_id", _no_thread)
    harness.store.set("resolve_user_by_ssh_fingerprint", _user)
    harness.user_can_access_ide_entity = _access

    assert await _close_attachment(
        harness, SshAttachmentClose(fingerprint=FINGERPRINT, channels=[])
    ) == {"closed": 0}
    assert resolved_with == [FINGERPRINT]


@pytest.mark.asyncio
async def test_close_attachment_already_closed_returns_zero_not_an_error(harness):
    """Pre-existing contract, still true once authorized: 0 rows updated
    (already closed) is a normal response, not an exception -- the gateway
    treats this as best-effort bookkeeping."""
    _close_resolved(harness)

    async def _close(attachment_id, channels):
        return 0

    harness.store.set("close_ssh_attachment", _close)

    result = await _close_attachment(
        harness, SshAttachmentClose(fingerprint=FINGERPRINT, channels=[])
    )
    assert result == {"closed": 0}


@pytest.mark.asyncio
async def test_close_attachment_malformed_id_maps_to_400_not_500(harness):
    """Process item 4, close-endpoint half. Deliberately wires the REAL
    ``PostgresDB.get_ssh_attachment_thread_id``: ``UUID(attachment_id)``
    raises ValueError inside it, before any connection is acquired
    (database/postgres.py) and before the fingerprint/access checks ever
    run, so this exercises the actual production code path end to end.
    Deleting the router's ``except ValueError`` lets a bare ValueError
    propagate — not an HTTPException — which fails this test.
    """
    from orchestrator.database import PostgresDB

    harness.store.set(
        "get_ssh_attachment_thread_id", PostgresDB().get_ssh_attachment_thread_id
    )

    with pytest.raises(HTTPException) as excinfo:
        await _close_attachment(
            harness,
            SshAttachmentClose(fingerprint=FINGERPRINT, channels=[]),
            attachment_id="not-a-uuid",
        )
    assert excinfo.value.status_code == 400


def test_close_attachment_channels_count_is_capped():
    with pytest.raises(ValidationError):
        SshAttachmentClose(fingerprint=FINGERPRINT, channels=["session"] * 9)


def test_close_attachment_channel_name_length_is_capped():
    with pytest.raises(ValidationError):
        SshAttachmentClose(fingerprint=FINGERPRINT, channels=["x" * 33])


def test_close_attachment_fingerprint_is_capped():
    with pytest.raises(ValidationError):
        SshAttachmentClose(fingerprint="A" * (500 * 1024), channels=[])


# =============================================================================
# ssh_key_id added to the ssh-targets response
# =============================================================================


async def _ssh_target(harness):
    return await ssh_access_routes.get_ssh_target(
        request=object(),
        handle="s-7f3a91c2",
        fingerprint=FINGERPRINT,
        dependencies=harness.dependencies,
    )


def _suspended_thread(harness, user):
    async def _thread_id(handle):
        return THREAD

    async def _user(fp):
        return user

    async def _access(u, db, entity_id):
        return True

    async def _get_thread(tid):
        return _thread(
            status="active", metadata={"workspace_container": {"status": "suspended"}}
        )

    harness.store.set("get_thread_id_by_ssh_handle", _thread_id)
    harness.store.set("resolve_user_by_ssh_fingerprint", _user)
    harness.store.set("get_thread", _get_thread)
    harness.user_can_access_ide_entity = _access


@pytest.mark.asyncio
async def test_ssh_key_id_appears_in_ssh_target_response(harness):
    """Without this, endpoint 2 (create-attachment) could never be handed a
    key id by the gateway for logging, because
    ``resolve_user_by_ssh_fingerprint`` already resolves one but
    ``ssh_target_response`` dropped it on the floor. No longer
    load-bearing for the create-attachment call itself (that now resolves
    its own copy server-side) — kept for gateway-side logging."""
    _suspended_thread(harness, {"id": USER, "ssh_key_id": KEY_ID})

    result = await _ssh_target(harness)
    assert result["ssh_key_id"] == KEY_ID


@pytest.mark.asyncio
async def test_ssh_key_id_is_none_when_user_dict_carries_none(harness):
    """Negative control for the test above: a resolved user with no key id
    on it (e.g. a test double that never set one) must not raise, and must
    not fabricate a value."""
    _suspended_thread(harness, {"id": USER})

    result = await _ssh_target(harness)
    assert result["ssh_key_id"] is None


# =============================================================================
# Route wiring
# =============================================================================


def test_ssh_audit_routes_are_mounted():
    """Every test above calls the handler directly, so none of them prove
    FastAPI actually serves these paths at these methods."""
    routes = mounted_routes(orchestrator.main.app)
    assert ("POST", "/api/internal/ssh-keys/used") in routes
    assert ("POST", "/api/internal/ssh-attachments") in routes
    assert (
        "POST",
        "/api/internal/ssh-attachments/{attachment_id}/close",
    ) in routes
