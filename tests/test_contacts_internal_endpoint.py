import pytest
from fastapi import HTTPException

from routers import contacts as contacts_router


@pytest.mark.asyncio
async def test_internal_contacts_requires_the_internal_key(monkeypatch):
    """No X-Internal-Key -> 401, regardless of query parameters."""

    async def deny(request):
        raise HTTPException(status_code=401, detail="Invalid internal key")

    monkeypatch.setattr(contacts_router, "require_internal", deny)
    with pytest.raises(HTTPException) as excinfo:
        await contacts_router.list_internal_contacts(
            request=object(), job_id="job-1", thread_id=None
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_requires_exactly_one_of_job_or_thread(monkeypatch):
    async def allow(request):
        return None

    monkeypatch.setattr(contacts_router, "require_internal", allow)
    with pytest.raises(HTTPException) as excinfo:
        await contacts_router.list_internal_contacts(
            request=object(), job_id=None, thread_id=None
        )
    assert excinfo.value.status_code == 400
