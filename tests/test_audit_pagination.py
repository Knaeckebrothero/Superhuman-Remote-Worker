"""BUG-6 regression tests: /api/jobs/{id}/audit + /chat pagination contract.

Covers the DB-layer signatures for `AuditStore.get_job_audit()` and
`AuditStore.get_chat_history()`. Uses the store's "unavailable"
branch (no URL/pool) so no live DB is required — the tests only have to prove
the signatures accept offset/limit (and legacy page/pageSize) and the response
echoes them back correctly. The chat case guards the migration that repointed
the MCP `get_chat_bulk` tool off the removed `/chat/bulk` route onto the paged
`/chat` endpoint (see knowledge-base/knowledge/features/debug_audit_view_refactor.md).
"""

import pytest

from orchestrator.database.audit_store import AuditStore


@pytest.fixture
def stub_audit_store():
    """A live AuditStore with no pool (is_available=False until connect())."""
    return AuditStore(dsn=None)


class TestAuditPaginationContract:
    @pytest.mark.asyncio
    async def test_accepts_offset_limit(self, stub_audit_store):
        """offset/limit must be accepted and echoed back in the response."""
        result = await stub_audit_store.get_job_audit("job-abc", offset=50, limit=25)
        assert result["offset"] == 50
        assert result["limit"] == 25
        assert result["pageSize"] == 25
        assert result["entries"] == []
        assert result["hasMore"] is False

    @pytest.mark.asyncio
    async def test_accepts_page_pagesize(self, stub_audit_store):
        """Legacy page/page_size style still works."""
        result = await stub_audit_store.get_job_audit("job-abc", page=3, page_size=20)
        assert result["page"] == 3
        assert result["pageSize"] == 20
        assert result["limit"] == 20
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_accepts_order_param(self, stub_audit_store):
        """order=asc|desc must be accepted without error."""
        for direction in ("asc", "desc"):
            result = await stub_audit_store.get_job_audit("job-abc", order=direction)
            assert "entries" in result

    @pytest.mark.asyncio
    async def test_default_response_shape(self, stub_audit_store):
        """Default call (no params) still returns the expected keys."""
        result = await stub_audit_store.get_job_audit("job-abc")
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


class TestChatPaginationContract:
    """AuditStore.get_chat_history must accept offset/limit (mirrors audit).

    This is what lets the `/chat` endpoint serve the MCP `get_chat_bulk` tool
    after the dedicated `/chat/bulk` route was removed.
    """

    @pytest.mark.asyncio
    async def test_accepts_offset_limit(self, stub_audit_store):
        """offset/limit must be accepted and echoed back in the response."""
        result = await stub_audit_store.get_chat_history("job-abc", offset=50, limit=25)
        assert result["offset"] == 50
        assert result["limit"] == 25
        assert result["pageSize"] == 25
        assert result["entries"] == []
        assert result["hasMore"] is False

    @pytest.mark.asyncio
    async def test_accepts_page_pagesize(self, stub_audit_store):
        """Legacy page/page_size style still works (frontend uses it)."""
        result = await stub_audit_store.get_chat_history(
            "job-abc", page=3, page_size=20
        )
        assert result["page"] == 3
        assert result["pageSize"] == 20
        assert result["limit"] == 20
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_default_response_shape(self, stub_audit_store):
        """Default call returns both pagination styles' keys."""
        result = await stub_audit_store.get_chat_history("job-abc")
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
