"""list_llm_requests call_type/status filtering + projection (Phase 1.6).

Surfaces auxiliary calls + auxiliary failures (status="error") in the
per-call debug view. See docs/issues/surface_silent_aux_failures.md.
"""

import pytest

from orchestrator.database.mongodb import MongoDB


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def skip(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield dict(d)

        return _gen()


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs
        self.find_calls = []

    async def count_documents(self, query):
        return len(self._docs)

    def find(self, query, projection=None):
        self.find_calls.append((query, projection))
        return _FakeCursor(self._docs)


class _FakeDB:
    def __init__(self, collection):
        self._collection = collection

    def __getitem__(self, name):
        return self._collection


def _make_mongo(docs):
    m = MongoDB(url=None)
    coll = _FakeCollection(docs)
    m._available = True
    m._db = _FakeDB(coll)
    return m, coll


class TestListLlmRequestsFilter:
    @pytest.mark.asyncio
    async def test_unavailable_branch_accepts_new_params(self):
        # Mirrors the existing DB-layer contract tests: no connection -> empty shape.
        m = MongoDB(url=None)
        out = await m.list_llm_requests(
            "job-1", call_type="memory_extraction", status="error"
        )
        assert out["entries"] == []
        assert out["total"] == 0

    @pytest.mark.asyncio
    async def test_default_query_is_job_only_and_projects_new_fields(self):
        m, coll = _make_mongo([{"_id": "1", "call_type": "main", "response": {}}])
        await m.list_llm_requests("job-1")
        query, projection = coll.find_calls[0]
        assert query == {"job_id": "job-1"}
        # call_type/status/error must be projected so the UI can label + show errors.
        assert projection.get("call_type")
        assert projection.get("status")
        assert projection.get("error")

    @pytest.mark.asyncio
    async def test_call_type_all_does_not_filter(self):
        m, coll = _make_mongo([])
        await m.list_llm_requests("job-1", call_type="all")
        query, _ = coll.find_calls[0]
        assert "call_type" not in query

    @pytest.mark.asyncio
    async def test_call_type_and_status_filter_query(self):
        m, coll = _make_mongo([])
        await m.list_llm_requests(
            "job-1", call_type="memory_extraction", status="error"
        )
        query, _ = coll.find_calls[0]
        assert query == {
            "job_id": "job-1",
            "call_type": "memory_extraction",
            "status": "error",
        }

    @pytest.mark.asyncio
    async def test_error_row_surfaced_and_missing_call_type_defaults_main(self):
        docs = [
            {
                "_id": "a",
                "call_type": "memory_extraction",
                "status": "error",
                "error": {"type": "ConnectionError", "message": "503 down"},
                "response": None,
            },
            {"_id": "b", "response": {"tool_calls": [{"name": "search"}]}},
        ]
        m, _ = _make_mongo(docs)
        out = await m.list_llm_requests("job-1", call_type="all")
        by_id = {d["_id"]: d for d in out["entries"]}
        # Error row: status/error preserved; response=None handled gracefully.
        assert by_id["a"]["status"] == "error"
        assert by_id["a"]["error"]["type"] == "ConnectionError"
        assert by_id["a"]["tool_calls"] == []
        # Legacy row without call_type defaults to "main"; tool calls extracted.
        assert by_id["b"]["call_type"] == "main"
        assert by_id["b"]["tool_calls"] == [{"name": "search"}]
