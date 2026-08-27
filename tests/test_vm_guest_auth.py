from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
import pytest

import orchestrator.security.vm_guest as vm_guest_auth
from orchestrator.security.vm_guest import VmGuestIdentity, require_vm_guest
from orchestrator.services.vm_lifecycle_auth import guest_token

SECRET = "0123456789abcdef0123456789abcdef"
PREVIOUS_SECRET = "abcdef0123456789abcdef0123456789"
JOB_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "11111111-1111-4111-8111-111111111112"
THREAD_ID = "11111111-1111-4111-8111-111111111113"
GENERATION = "22222222-2222-4222-8222-222222222222"
OLD_GENERATION = "22222222-2222-4222-8222-222222222223"


def request_with(token: str | None) -> MagicMock:
    request = MagicMock()
    request.headers = {} if token is None else {"authorization": f"Bearer {token}"}
    request.client.host = "192.0.2.10"
    return request


def job_row(*, status="created", generation=GENERATION):
    return {
        "id": JOB_ID,
        "context": {"vm": {"status": status, "provision_generation": generation}},
    }


def thread_row(*, status="created", generation=GENERATION):
    return {
        "id": THREAD_ID,
        "metadata": {"vm": {"status": status, "provision_generation": generation}},
    }


@pytest.fixture(autouse=True)
def configured_secret(monkeypatch):
    monkeypatch.setenv("VM_LIFECYCLE_HMAC_SECRET", SECRET)
    monkeypatch.delenv("VM_LIFECYCLE_HMAC_SECRET_PREVIOUS", raising=False)
    monkeypatch.setattr(
        "orchestrator.security.vm_guest.log_security_event", AsyncMock()
    )
    vm_guest_auth._preauth_rate_limiter.reset()
    vm_guest_auth._security_log_last_at.clear()


@pytest.mark.asyncio
async def test_job_token_ok():
    db = AsyncMock()
    db.get_thread.return_value = None
    db.get_job.return_value = job_row()
    token = guest_token(SECRET.encode(), "job", JOB_ID, GENERATION)

    identity = await require_vm_guest(request_with(token), db, JOB_ID)

    assert identity == VmGuestIdentity("job", JOB_ID, GENERATION)


@pytest.mark.asyncio
async def test_thread_token_ok_and_resolves_thread_first():
    db = AsyncMock()
    db.get_thread.return_value = thread_row()
    token = guest_token(SECRET.encode(), "thread", THREAD_ID, GENERATION)

    identity = await require_vm_guest(request_with(token), db, THREAD_ID)

    assert identity == VmGuestIdentity("thread", THREAD_ID, GENERATION)
    db.get_job.assert_not_awaited()


async def assert_unauthorized(request, db, entity_id):
    with pytest.raises(HTTPException) as exc:
        await require_vm_guest(request, db, entity_id)
    assert (exc.value.status_code, exc.value.detail) == (401, "Unauthorized")


@pytest.mark.asyncio
async def test_wrong_entity_is_indistinguishable_401():
    db = AsyncMock()
    db.get_thread.return_value = None
    db.get_job.return_value = {**job_row(), "id": OTHER_ID}
    token = guest_token(SECRET.encode(), "job", JOB_ID, GENERATION)
    await assert_unauthorized(request_with(token), db, OTHER_ID)


@pytest.mark.asyncio
async def test_stale_generation_is_indistinguishable_401():
    db = AsyncMock()
    db.get_thread.return_value = None
    db.get_job.return_value = job_row()
    token = guest_token(SECRET.encode(), "job", JOB_ID, OLD_GENERATION)
    await assert_unauthorized(request_with(token), db, JOB_ID)


@pytest.mark.asyncio
async def test_previous_secret_is_accepted(monkeypatch):
    monkeypatch.setenv("VM_LIFECYCLE_HMAC_SECRET_PREVIOUS", PREVIOUS_SECRET)
    db = AsyncMock()
    db.get_thread.return_value = None
    db.get_job.return_value = job_row()
    token = guest_token(PREVIOUS_SECRET.encode(), "job", JOB_ID, GENERATION)
    assert await require_vm_guest(request_with(token), db, JOB_ID) == VmGuestIdentity(
        "job", JOB_ID, GENERATION
    )


@pytest.mark.asyncio
async def test_deleted_vm_is_indistinguishable_401():
    db = AsyncMock()
    db.get_thread.return_value = None
    db.get_job.return_value = job_row(status="deleted")
    token = guest_token(SECRET.encode(), "job", JOB_ID, GENERATION)
    await assert_unauthorized(request_with(token), db, JOB_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "not-hex", "a" * 63])
async def test_malformed_header_is_indistinguishable_401(token):
    db = AsyncMock()
    await assert_unauthorized(request_with(token), db, JOB_ID)


@pytest.mark.asyncio
async def test_unknown_entity_is_indistinguishable_401():
    db = AsyncMock()
    db.get_thread.return_value = None
    db.get_job.return_value = None
    token = guest_token(SECRET.encode(), "job", JOB_ID, GENERATION)
    token_spy = MagicMock(wraps=vm_guest_auth.guest_token)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(vm_guest_auth, "guest_token", token_spy)
        await assert_unauthorized(request_with(token), db, JOB_ID)
    token_spy.assert_called_once()


@pytest.mark.asyncio
async def test_bad_mac_audit_is_sampled_and_pre_auth_rate_limited():
    db = AsyncMock()
    db.get_thread.return_value = None
    db.get_job.return_value = job_row()
    request = request_with("f" * 64)

    for _ in range(vm_guest_auth._PREAUTH_REQUESTS_PER_MINUTE + 1):
        await assert_unauthorized(request, db, JOB_ID)

    assert db.get_thread.await_count == vm_guest_auth._PREAUTH_REQUESTS_PER_MINUTE
    assert vm_guest_auth.log_security_event.await_count == 1
