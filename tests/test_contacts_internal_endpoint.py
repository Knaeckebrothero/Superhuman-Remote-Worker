import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

from orchestrator.routers import contacts as contacts_router


@pytest.mark.asyncio
async def test_internal_contacts_requires_the_internal_key():
    """No X-Internal-Key -> 401, regardless of query parameters."""

    async def deny(request):
        raise HTTPException(status_code=401, detail="Invalid internal key")

    dependencies = contacts_router.ContactsDependencies(
        db=MagicMock(), require_internal=deny
    )
    with pytest.raises(HTTPException) as excinfo:
        await contacts_router.list_internal_contacts(
            request=object(), job_id="job-1", thread_id=None, dependencies=dependencies
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_requires_exactly_one_of_job_or_thread():
    async def allow(request):
        return None

    dependencies = contacts_router.ContactsDependencies(
        db=MagicMock(), require_internal=allow
    )
    with pytest.raises(HTTPException) as excinfo:
        await contacts_router.list_internal_contacts(
            request=object(), job_id=None, thread_id=None, dependencies=dependencies
        )
    assert excinfo.value.status_code == 400
