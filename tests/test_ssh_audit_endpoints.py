"""Tests for the SSH-gateway audit endpoints (Task 6A).

Plan 1 built three database methods for SSH auditing —
``mark_ssh_key_used``, ``record_ssh_attachment``, ``close_ssh_attachment``
in ``database/postgres.py`` — and left them for the gateway to call. But the
gateway is a separate process with no database credentials at all
(``GatewayConfig`` has no DB field, by design). It reaches the orchestrator
only over HTTP with an ``X-Internal-Key`` header, so these three endpoints
are the only way those methods can ever be called in production.

* ``POST /api/internal/ssh-keys/used`` is keyed by FINGERPRINT, not
  ``key_id``, on purpose: the gateway calls this from asyncssh's
  ``auth_completed()``, immediately after ``key.verify`` succeeds, but the
  gateway resolves its target — and therefore a key id — lazily, at first
  channel open. At the only moment this call may legitimately fire, it holds
  a fingerprint and nothing else. An unknown fingerprint is a silent no-op
  success, not a 404 — a 404 would turn this into an existence oracle for
  registered keys (same anti-enumeration property ``get_ssh_target``
  protects).
* ``POST /api/internal/ssh-attachments`` opens an audit row and hands back
  its id for the matching close.
* ``POST /api/internal/ssh-attachments/{id}/close`` returns the row count
  from ``close_ssh_attachment`` honestly — 0 for an unknown or
  already-closed id — rather than turning that into an error, since the
  gateway treats this as best-effort bookkeeping.
* ``ssh_key_id`` is added to the ``get_ssh_target`` response so the gateway
  can ever supply one to the create-attachment call above.
"""

import pytest
from fastapi import HTTPException

import main
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
def internal(monkeypatch):
    async def _allow(request):
        return None

    monkeypatch.setattr(main, "require_internal", _allow)


# =============================================================================
# X-Internal-Key enforcement
# =============================================================================


@pytest.mark.asyncio
async def test_mark_used_requires_internal_key(monkeypatch):
    async def _deny(request):
        raise HTTPException(status_code=401, detail="Invalid internal key")

    monkeypatch.setattr(main, "require_internal", _deny)
    with pytest.raises(HTTPException) as excinfo:
        await main.internal_mark_ssh_key_used(
            request=object(), body=main.SshKeyUsedRequest(fingerprint=FINGERPRINT)
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_create_attachment_requires_internal_key(monkeypatch):
    async def _deny(request):
        raise HTTPException(status_code=401, detail="Invalid internal key")

    monkeypatch.setattr(main, "require_internal", _deny)
    with pytest.raises(HTTPException) as excinfo:
        await main.internal_create_ssh_attachment(
            request=object(),
            body=main.SshAttachmentCreate(
                thread_id=THREAD,
                user_id=USER,
                ssh_key_id=KEY_ID,
                client_ip="10.0.0.1",
                handle="s-7f3a91c2",
            ),
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_close_attachment_requires_internal_key(monkeypatch):
    async def _deny(request):
        raise HTTPException(status_code=401, detail="Invalid internal key")

    monkeypatch.setattr(main, "require_internal", _deny)
    with pytest.raises(HTTPException) as excinfo:
        await main.internal_close_ssh_attachment(
            request=object(),
            attachment_id=ATTACHMENT_ID,
            body=main.SshAttachmentClose(channels=["session"]),
        )
    assert excinfo.value.status_code == 401


# =============================================================================
# POST /api/internal/ssh-keys/used
# =============================================================================


@pytest.mark.asyncio
async def test_mark_used_happy_path_resolves_fingerprint_to_key_id(
    internal, monkeypatch
):
    """The endpoint's whole reason to exist: it is handed a fingerprint and
    must resolve it to a key id itself before bumping ``last_used_at``,
    because the gateway holds nothing else at the moment it calls this."""

    async def _user(fp):
        assert fp == FINGERPRINT
        return {"id": USER, "ssh_key_id": KEY_ID}

    calls = []

    async def _mark_used(key_id, fingerprint_sha256):
        calls.append((key_id, fingerprint_sha256))

    monkeypatch.setattr(main.postgres_db, "resolve_user_by_ssh_fingerprint", _user)
    monkeypatch.setattr(main.postgres_db, "mark_ssh_key_used", _mark_used)

    result = await main.internal_mark_ssh_key_used(
        request=object(), body=main.SshKeyUsedRequest(fingerprint=FINGERPRINT)
    )
    assert calls == [(KEY_ID, FINGERPRINT)]
    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_mark_used_unknown_fingerprint_is_silent_success_no_write(
    internal, monkeypatch
):
    """An unknown fingerprint must not 404 (existence oracle) and must not
    reach ``mark_ssh_key_used`` at all — there is no key id to bump."""

    async def _no_user(fp):
        return None

    async def _tripwire(key_id, fingerprint_sha256):
        raise AssertionError("mark_ssh_key_used must not be called for an unknown key")

    monkeypatch.setattr(main.postgres_db, "resolve_user_by_ssh_fingerprint", _no_user)
    monkeypatch.setattr(main.postgres_db, "mark_ssh_key_used", _tripwire)

    result = await main.internal_mark_ssh_key_used(
        request=object(), body=main.SshKeyUsedRequest(fingerprint=FINGERPRINT)
    )
    assert result == {"status": "ok"}


# =============================================================================
# POST /api/internal/ssh-attachments
# =============================================================================


@pytest.mark.asyncio
async def test_create_attachment_happy_path(internal, monkeypatch):
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

    monkeypatch.setattr(main.postgres_db, "record_ssh_attachment", _record)

    result = await main.internal_create_ssh_attachment(
        request=object(),
        body=main.SshAttachmentCreate(
            thread_id=THREAD,
            user_id=USER,
            ssh_key_id=KEY_ID,
            client_ip="10.0.0.1",
            handle="s-7f3a91c2",
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
async def test_create_attachment_allows_optional_fields_to_be_absent(
    internal, monkeypatch
):
    """``ssh_key_id`` and ``client_ip`` are Optional[str] on the DB method."""

    async def _record(thread_id, user_id, ssh_key_id, client_ip, handle):
        assert ssh_key_id is None
        assert client_ip is None
        return ATTACHMENT_ID

    monkeypatch.setattr(main.postgres_db, "record_ssh_attachment", _record)

    result = await main.internal_create_ssh_attachment(
        request=object(),
        body=main.SshAttachmentCreate(
            thread_id=THREAD, user_id=USER, handle="s-7f3a91c2"
        ),
    )
    assert result == {"attachment_id": ATTACHMENT_ID}


# =============================================================================
# POST /api/internal/ssh-attachments/{attachment_id}/close
# =============================================================================


@pytest.mark.asyncio
async def test_close_attachment_happy_path(internal, monkeypatch):
    captured = {}

    async def _close(attachment_id, channels):
        captured["attachment_id"] = attachment_id
        captured["channels"] = channels
        return 1

    monkeypatch.setattr(main.postgres_db, "close_ssh_attachment", _close)

    result = await main.internal_close_ssh_attachment(
        request=object(),
        attachment_id=ATTACHMENT_ID,
        body=main.SshAttachmentClose(channels=["session", "sftp"]),
    )
    assert result == {"closed": 1}
    assert captured == {
        "attachment_id": ATTACHMENT_ID,
        "channels": ["session", "sftp"],
    }


@pytest.mark.asyncio
async def test_close_attachment_unknown_id_returns_zero_not_an_error(
    internal, monkeypatch
):
    """0 rows updated (unknown id, or already closed) is a normal response,
    not an exception — the gateway treats this as best-effort bookkeeping."""

    async def _close(attachment_id, channels):
        return 0

    monkeypatch.setattr(main.postgres_db, "close_ssh_attachment", _close)

    result = await main.internal_close_ssh_attachment(
        request=object(),
        attachment_id=ATTACHMENT_ID,
        body=main.SshAttachmentClose(channels=[]),
    )
    assert result == {"closed": 0}


# =============================================================================
# ssh_key_id added to the ssh-targets response
# =============================================================================


@pytest.mark.asyncio
async def test_ssh_key_id_appears_in_ssh_target_response(monkeypatch):
    """Without this, endpoint 2 (create-attachment) could never be handed a
    key id by the gateway, because ``resolve_user_by_ssh_fingerprint``
    already resolves one but ``_ssh_target_response`` dropped it on the
    floor."""

    async def _allow(request):
        return None

    async def _thread_id(handle):
        return THREAD

    async def _user(fp):
        return {"id": USER, "ssh_key_id": KEY_ID}

    async def _access(user, db, entity_id):
        return True

    async def _get_thread(tid):
        return _thread(
            status="active", metadata={"workspace_container": {"status": "suspended"}}
        )

    monkeypatch.setattr(main, "require_internal", _allow)
    monkeypatch.setattr(main.postgres_db, "get_thread_id_by_ssh_handle", _thread_id)
    monkeypatch.setattr(main.postgres_db, "resolve_user_by_ssh_fingerprint", _user)
    monkeypatch.setattr(main, "user_can_access_ide_entity", _access)
    monkeypatch.setattr(main.postgres_db, "get_thread", _get_thread)

    result = await main.get_ssh_target(
        request=object(), handle="s-7f3a91c2", fingerprint=FINGERPRINT
    )
    assert result["ssh_key_id"] == KEY_ID


@pytest.mark.asyncio
async def test_ssh_key_id_is_none_when_user_dict_carries_none(monkeypatch):
    """Negative control for the test above: a resolved user with no key id
    on it (e.g. a test double that never set one) must not raise, and must
    not fabricate a value."""

    async def _allow(request):
        return None

    async def _thread_id(handle):
        return THREAD

    async def _user(fp):
        return {"id": USER}

    async def _access(user, db, entity_id):
        return True

    async def _get_thread(tid):
        return _thread(
            status="active", metadata={"workspace_container": {"status": "suspended"}}
        )

    monkeypatch.setattr(main, "require_internal", _allow)
    monkeypatch.setattr(main.postgres_db, "get_thread_id_by_ssh_handle", _thread_id)
    monkeypatch.setattr(main.postgres_db, "resolve_user_by_ssh_fingerprint", _user)
    monkeypatch.setattr(main, "user_can_access_ide_entity", _access)
    monkeypatch.setattr(main.postgres_db, "get_thread", _get_thread)

    result = await main.get_ssh_target(
        request=object(), handle="s-7f3a91c2", fingerprint=FINGERPRINT
    )
    assert result["ssh_key_id"] is None


# =============================================================================
# Route wiring
# =============================================================================


def test_ssh_audit_routes_are_mounted():
    """Every test above calls the handler directly, so none of them prove
    FastAPI actually serves these paths at these methods."""
    routes = mounted_routes(main.app)
    assert ("POST", "/api/internal/ssh-keys/used") in routes
    assert ("POST", "/api/internal/ssh-attachments") in routes
    assert (
        "POST",
        "/api/internal/ssh-attachments/{attachment_id}/close",
    ) in routes
