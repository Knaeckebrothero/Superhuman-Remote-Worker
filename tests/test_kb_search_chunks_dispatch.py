"""Dispatch tests for ``KnowledgeStore.search_chunks`` (spec WP7, H6).

``search_chunks`` is the single seam where the new multi-angle SQL function
(migration 0025) becomes reachable — and therefore the seam where H6 ("no
ranking change for existing callers") either holds or silently breaks. These
tests are the H6 proof: a plain ``search_chunks(kb_ids, query)`` must issue
*exactly* the old ``knowledge_chunk_hybrid_search`` call with *exactly* its nine
positional parameters and must never mention the new function; only a call
carrying a non-empty ``exact`` or ``tags`` takes the new path.

Everything here runs against a mocked ``db``/``embedding_service`` — this file
asserts on which SQL is issued and with what, not on ranking. Ranking lives in
tests/test_kb_multi_angle_real_postgres.py, which needs a real server.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from src.services.knowledge_store import KnowledgeRecord, KnowledgeStore


def _store(embedding_service: bool = True):
    db = AsyncMock()
    db.fetch.return_value = []
    svc = None
    if embedding_service:
        svc = AsyncMock()
        svc.embed = AsyncMock(return_value=[0.1] * 8)
    return KnowledgeStore(db=db, embedding_service=svc), db, svc


def _row(note_row, note_id):
    """A ``knowledge_index`` row as the second query returns it."""
    return {
        "id": note_row,
        "note_id": note_id,
        "title": note_id.upper(),
        "note_type": "learning",
        "status": "active",
        "content": "body",
    }


# =============================================================================
# H6 — the plain path is untouched
# =============================================================================


@pytest.mark.asyncio
async def test_plain_query_calls_the_old_function_unchanged():
    store, db, _svc = _store()
    await store.search_chunks(
        [uuid.uuid4()], "auth", embedding_version="v1", match_count=5
    )
    sql = db.fetch.call_args.args[0]
    assert "knowledge_chunk_hybrid_search(" in sql
    assert "multi_angle" not in sql
    # exactly the nine existing params (plus the SQL string itself)
    assert len(db.fetch.call_args.args) == 1 + 9


@pytest.mark.asyncio
async def test_plain_query_threads_the_nine_params_in_order():
    store, db, svc = _store()
    kb_ids = [uuid.uuid4()]
    await store.search_chunks(
        kb_ids,
        "auth",
        embedding_version="v1",
        over_fetch=50,
        rrf_k=60,
    )
    args = db.fetch.call_args.args
    assert args[1:] == (
        "auth",
        svc.embed.return_value,
        kb_ids,
        "v1",
        50,
        0.6,
        0.3,
        0.1,
        60,
    )


@pytest.mark.asyncio
async def test_empty_exact_and_tags_still_take_the_plain_path():
    """Empty/whitespace-only filters are not filters — a caller that passes
    ``exact=[]`` or ``tags=["  "]`` must keep the old ranking exactly."""
    store, db, _svc = _store()
    await store.search_chunks([uuid.uuid4()], "auth", exact=[], tags=["  ", ""])
    sql = db.fetch.call_args.args[0]
    assert "knowledge_chunk_hybrid_search(" in sql
    assert len(db.fetch.call_args.args) == 1 + 9


@pytest.mark.asyncio
async def test_no_kb_ids_short_circuits_before_any_query():
    store, db, _svc = _store()
    assert await store.search_chunks([], "auth", exact=["x"]) == []
    db.fetch.assert_not_called()


# =============================================================================
# The new path — only when exact/tags are supplied
# =============================================================================


@pytest.mark.asyncio
async def test_exact_only_skips_embedding_and_calls_multi_angle():
    store, db, svc = _store()
    await store.search_chunks([uuid.uuid4()], "", exact=["sales_page"])
    svc.embed.assert_not_awaited()
    assert "knowledge_chunk_multi_angle_search(" in db.fetch.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_tags_only_takes_the_multi_angle_path():
    store, db, _svc = _store()
    await store.search_chunks([uuid.uuid4()], "", tags=["hot"])
    assert "knowledge_chunk_multi_angle_search(" in db.fetch.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_multi_angle_params_are_threaded_in_declared_order():
    store, db, svc = _store()
    kb_ids = [uuid.uuid4()]
    await store.search_chunks(
        kb_ids,
        "auth",
        exact=["ident"],
        tags=["hot"],
        exact_weight=0.7,
        tag_weight=0.25,
        embedding_version="v1",
        over_fetch=40,
        rrf_k=42,
    )
    args = db.fetch.call_args_list[0].args
    assert args[1:] == (
        "auth",
        svc.embed.return_value,
        kb_ids,
        "v1",
        40,
        0.6,
        0.3,
        0.1,
        ["ident"],
        0.7,
        ["hot"],
        0.25,
        42,
    )


@pytest.mark.asyncio
async def test_exact_terms_are_like_escaped_but_tags_are_not():
    """The SQL function builds its ILIKE pattern by plain concatenation and does
    NOT escape, so ``%``/``_``/``\\`` in an ``exact`` term would act as
    wildcards. Escape at the call site (the same helper ``grep_notes`` uses).
    Tags go through ``&&`` array containment, which has no wildcards to escape.
    """
    store, db, _svc = _store()
    await store.search_chunks(
        [uuid.uuid4()], "", exact=["a%b"], tags=["tag_with_underscore"]
    )
    args = db.fetch.call_args_list[0].args
    assert args[9] == ["a\\%b"]
    assert args[11] == ["tag_with_underscore"]


@pytest.mark.asyncio
async def test_terms_are_stripped_and_blanks_dropped():
    store, db, _svc = _store()
    await store.search_chunks(
        [uuid.uuid4()], "", exact=[" ident ", "", "   ", None], tags=[" hot ", ""]
    )
    args = db.fetch.call_args_list[0].args
    assert args[9] == ["ident"]
    assert args[11] == ["hot"]


@pytest.mark.asyncio
async def test_empty_query_passes_a_null_embedding():
    store, db, svc = _store()
    await store.search_chunks([uuid.uuid4()], "   ", exact=["ident"])
    svc.embed.assert_not_awaited()
    assert db.fetch.call_args_list[0].args[2] is None


@pytest.mark.asyncio
async def test_missing_embedding_service_skips_the_dense_arm():
    """A caller may hold a store with no embedding service (filter-only usage).
    A non-empty query must degrade to the lexical arms, not raise."""
    store, db, _svc = _store(embedding_service=False)
    await store.search_chunks([uuid.uuid4()], "auth", tags=["hot"])
    args = db.fetch.call_args_list[0].args
    assert args[1] == "auth"
    assert args[2] is None


# =============================================================================
# Attribution + assembly
# =============================================================================


@pytest.mark.asyncio
async def test_matched_arms_attach_in_the_functions_order():
    store, db, _svc = _store()
    a, b = uuid.uuid4(), uuid.uuid4()
    db.fetch.side_effect = [
        [
            {"note_row": a, "rrf_score": 0.9, "arms": ["exact"]},
            {"note_row": b, "rrf_score": 0.4, "arms": ["sparse", "tag"]},
        ],
        # deliberately returned in the OTHER order — the ranking is the
        # function's, not the second query's.
        [_row(b, "b"), _row(a, "a")],
    ]
    recs = await store.search_chunks([uuid.uuid4()], "", exact=["x"])
    assert [r.note_id for r in recs] == ["a", "b"]
    assert recs[0].matched_arms == ["exact"]
    assert recs[1].matched_arms == ["sparse", "tag"]


@pytest.mark.asyncio
async def test_second_query_selects_notes_by_row_id():
    store, db, _svc = _store()
    a = uuid.uuid4()
    db.fetch.side_effect = [
        [{"note_row": a, "rrf_score": 0.9, "arms": ["exact"]}],
        [_row(a, "a")],
    ]
    kb = uuid.uuid4()
    await store.search_chunks([kb], "", exact=["x"])
    second = db.fetch.call_args_list[1].args
    assert "knowledge_index" in second[0]
    assert second[1] == [a]
    # Final review, M4: the body fetch re-states the caller's scope. The two
    # queries do not share a snapshot, so trusting the ranking row alone could
    # hand back a note archived (or re-homed) in between as a live result.
    assert "status = 'active'" in second[0]
    assert "kb_id = ANY($2)" in second[0]
    assert second[2] == [kb]


@pytest.mark.asyncio
async def test_missing_note_row_is_skipped_not_fatal():
    """The exact arm can return a note with no current-version chunks; nothing
    guarantees the second query finds every id (a concurrent delete, say)."""
    store, db, _svc = _store()
    a, ghost = uuid.uuid4(), uuid.uuid4()
    db.fetch.side_effect = [
        [
            {"note_row": ghost, "rrf_score": 0.9, "arms": ["exact"]},
            {"note_row": a, "rrf_score": 0.4, "arms": ["exact"]},
        ],
        [_row(a, "a")],
    ]
    recs = await store.search_chunks([uuid.uuid4()], "", exact=["x"])
    assert [r.note_id for r in recs] == ["a"]


@pytest.mark.asyncio
async def test_no_ranked_rows_skips_the_second_query():
    store, db, _svc = _store()
    db.fetch.side_effect = [[]]
    assert await store.search_chunks([uuid.uuid4()], "", exact=["x"]) == []
    assert db.fetch.call_count == 1


@pytest.mark.asyncio
async def test_multi_angle_truncates_to_match_count():
    store, db, _svc = _store()
    ids = [uuid.uuid4() for _ in range(10)]
    db.fetch.side_effect = [
        [{"note_row": i, "rrf_score": 1.0, "arms": ["exact"]} for i in ids],
        [_row(i, f"n{ix}") for ix, i in enumerate(ids)],
    ]
    recs = await store.search_chunks(
        [uuid.uuid4()], "", exact=["x"], match_count=3, over_fetch=50
    )
    assert [r.note_id for r in recs] == ["n0", "n1", "n2"]
    # over_fetch, not match_count, is what the SQL function is asked for.
    assert db.fetch.call_args_list[0].args[5] == 50


def test_matched_arms_defaults_empty_and_is_not_read_from_a_row():
    """``matched_arms`` is attached by ``search_chunks``; ``knowledge_index`` has
    no such column, so ``from_row`` must never try to read one."""
    assert KnowledgeRecord().matched_arms == []
    assert (
        KnowledgeRecord.from_row(
            {"note_id": "n", "matched_arms": ["exact"]}
        ).matched_arms
        == []
    )
