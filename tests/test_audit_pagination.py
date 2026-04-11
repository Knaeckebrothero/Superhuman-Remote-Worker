"""BUG-6 regression tests: /api/jobs/{id}/audit pagination contract.

Covers the DB-layer signature for `MongoDB.get_job_audit()`. Uses the
"MongoDB unavailable" branch (no URL configured) so no live Mongo is
required — the test only has to prove the signature accepts the new
params and the response echoes them back correctly.
"""

import pytest

from orchestrator.database.mongodb import MongoDB


@pytest.fixture
def stub_mongo(monkeypatch):
    """A MongoDB instance with no backing connection (is_available=False)."""
    monkeypatch.delenv("MONGODB_URL", raising=False)
    return MongoDB(url=None)


class TestAuditPaginationContract:
    @pytest.mark.asyncio
    async def test_accepts_offset_limit(self, stub_mongo):
        """offset/limit must be accepted and echoed back in the response."""
        result = await stub_mongo.get_job_audit("job-abc", offset=50, limit=25)
        assert result["offset"] == 50
        assert result["limit"] == 25
        assert result["pageSize"] == 25
        assert result["entries"] == []
        assert result["hasMore"] is False

    @pytest.mark.asyncio
    async def test_accepts_page_pagesize(self, stub_mongo):
        """Legacy page/page_size style still works."""
        result = await stub_mongo.get_job_audit("job-abc", page=3, page_size=20)
        assert result["page"] == 3
        assert result["pageSize"] == 20
        assert result["limit"] == 20
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_accepts_order_param(self, stub_mongo):
        """order=asc|desc must be accepted without error."""
        for direction in ("asc", "desc"):
            result = await stub_mongo.get_job_audit("job-abc", order=direction)
            assert "entries" in result

    @pytest.mark.asyncio
    async def test_default_response_shape(self, stub_mongo):
        """Default call (no params) still returns the expected keys."""
        result = await stub_mongo.get_job_audit("job-abc")
        for key in (
            "entries",
            "total",
            "page",
            "pageSize",
            "offset",
            "limit",
            "hasMore",
        ):
            assert key in result, f"missing response key: {key}"
        assert result["page"] == 1
        assert result["pageSize"] == 50
        assert result["offset"] == 0
        assert result["limit"] == 50
