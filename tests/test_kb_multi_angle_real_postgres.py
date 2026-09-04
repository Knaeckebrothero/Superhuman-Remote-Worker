"""Real-Postgres proof for the multi-angle KB search function (spec WP7, D9/D11).

``knowledge_chunk_multi_angle_search`` is a plpgsql function whose whole body is
a *dynamic* string (0017's shape — ``LANGUAGE sql`` never takes the HNSW index).
A dynamic ``EXECUTE`` string is not parsed, let alone type-checked, until the
function actually runs, so nothing short of a real server executing it can prove
the 13 ``$n`` placeholders line up with the ``USING`` list, that the trigram
``exact`` arm resolves, or that the per-arm attribution array comes back in the
declared order.

The third test guards H6: the pre-existing ``knowledge_chunk_hybrid_search`` —
which every current caller, memory injection included, still uses — must be
left byte-identical by migration 0025.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

pytest.importorskip("testcontainers.postgres")

import asyncpg  # noqa: E402
import pytest_asyncio  # noqa: E402

from orchestrator.database.migrate import run_migrations  # noqa: E402
from src.services.knowledge_store import KnowledgeStore  # noqa: E402

PG_IMAGE = "pgvector/pgvector:pg15"
VECTOR_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "vector"
)


@pytest.fixture(scope="module")
def pg_dsn():
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(PG_IMAGE)
    try:
        container.start()
    except Exception as exc:  # pragma: no cover - environment without a runtime
        pytest.skip(f"no container runtime for testcontainers: {exc}")
    try:
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


@pytest_asyncio.fixture
async def vector_pool(pg_dsn):
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
    try:
        await run_migrations(pool, VECTOR_MIGRATIONS)
        yield pool
    finally:
        await pool.close()


async def _seed_returning_id(pool, kb, note_id, title, content, tags=()):
    """`_seed` from tests/test_kb_grep_real_postgres.py, returning the row id.

    $3 is cast to ::text at both use sites (the note_id column value and the
    path concatenation) — reusing an untyped parameter in two positions that
    asyncpg would otherwise infer as different types (character varying for the
    column, text for the `||` concat) raises AmbiguousParameterError.
    """
    return await pool.fetchval(
        """
        INSERT INTO knowledge_index
            (id, project_id, kb_id, note_id, title, note_type, status, content,
             tags, path, indexed_at)
        VALUES ($1, $2, $2, $3::text, $4, 'learning', 'active', $5, $6,
                'knowledge/' || $3::text || '.md', NOW())
        RETURNING id
        """,
        uuid.uuid4(),
        kb,
        note_id,
        title,
        content,
        list(tags),
    )


async def _chunk(pool, note_row, kb, text, version="v1"):
    """One chunk per note. `knowledge_chunks`' NOT NULL set is
    (id, note_row, kb_id, chunk_ix, content) — see
    orchestrator/database/vector_schema_current.sql; `embedding` stays NULL so
    only the lexical arms can fire."""
    await pool.execute(
        """
        INSERT INTO knowledge_chunks (id, note_row, kb_id, chunk_ix, heading_path,
                                      content, search_doc, embedding_version)
        VALUES ($1, $2, $3, 0, '', $4::text, to_tsvector('english', $4::text), $5)
        """,
        uuid.uuid4(),
        note_row,
        kb,
        text,
        version,
    )


class _NullEmbeddings:
    """Embedding service that yields no vector. The seeded chunks carry no
    embedding either, so the dense arm is empty on both paths and the lexical
    arms decide the ranking — which is what these tests are about."""

    async def embed(self, text):
        return None


@pytest.mark.asyncio
async def test_exact_arm_finds_identifier_sparse_cannot(vector_pool):
    """The stemmed-English sparse arm cannot match a bare identifier fragment
    (`sales_page_2026` is not a lexeme of `sales_page_2026_09`), so with an
    empty query_text the ONLY arm that can return the note is `exact`."""
    kb = uuid.uuid4()
    n1 = await _seed_returning_id(
        vector_pool,
        kb,
        "sales_page_2026_09",
        "Sales",
        "the id is sales_page_2026_09 here",
        tags=["sales"],
    )
    n2 = await _seed_returning_id(
        vector_pool, kb, "other", "Other", "prose only", tags=["web"]
    )
    for n, t in ((n1, "the id is sales_page_2026_09 here"), (n2, "prose only")):
        await _chunk(vector_pool, n, kb, t)

    rows = await vector_pool.fetch(
        "SELECT * FROM knowledge_chunk_multi_angle_search($1, NULL, $2, 'v1', 10,"
        " 0.6, 0.3, 0.1, $3, 0.6, ARRAY[]::text[], 0.2, 60)",
        "",
        [kb],
        ["sales_page_2026"],
    )
    assert [r["note_row"] for r in rows] == [n1]
    assert rows[0]["arms"] == ["exact"]


@pytest.mark.asyncio
async def test_tag_arm_boosts_without_filtering(vector_pool):
    """The tag arm is a BOOST, not a filter: the untagged note must still come
    back, just below the tagged one."""
    kb = uuid.uuid4()
    n1 = await _seed_returning_id(
        vector_pool, kb, "a", "A", "shared words", tags=["hot"]
    )
    n2 = await _seed_returning_id(
        vector_pool, kb, "b", "B", "shared words", tags=["cold"]
    )
    for n in (n1, n2):
        await _chunk(vector_pool, n, kb, "shared words")

    rows = await vector_pool.fetch(
        "SELECT * FROM knowledge_chunk_multi_angle_search($1, NULL, $2, 'v1', 10,"
        " 0.6, 0.3, 0.1, ARRAY[]::text[], 0.6, $3, 0.2, 60)",
        "shared words",
        [kb],
        ["hot"],
    )
    ids = [r["note_row"] for r in rows]
    assert ids[0] == n1 and n2 in ids  # boosted first, other still present
    assert "tag" in rows[0]["arms"] and "sparse" in rows[0]["arms"]


@pytest.mark.asyncio
async def test_existing_hybrid_search_is_untouched_by_0025(vector_pool):
    """H6: `knowledge_chunk_hybrid_search` is the function every existing caller
    (kb_search without exact/tags, and memory injection) still runs. 0025 adds a
    NEW function beside it; applying the whole migration chain must leave the old
    one's catalog definition free of the new arms, still returning
    SETOF knowledge_index, and still carrying its original (NULLS-first) recency
    ordering — the NULLS LAST hazard fix is confined to the new function."""
    definition = await vector_pool.fetchval(
        "SELECT pg_get_functiondef('knowledge_chunk_hybrid_search'::regproc)"
    )
    assert "RETURNS SETOF knowledge_index" in definition
    assert "exact AS (" not in definition
    assert "tagged AS (" not in definition
    assert "similarity(" not in definition
    assert "ORDER BY ki.modified_at DESC) AS rank_ix" in definition
    assert "NULLS LAST" not in definition

    # ...and the new function is a genuinely separate catalog entry.
    names = {
        r["proname"]
        for r in await vector_pool.fetch(
            "SELECT proname FROM pg_proc WHERE proname LIKE 'knowledge_chunk_%search'"
        )
    }
    assert names == {
        "knowledge_chunk_hybrid_search",
        "knowledge_chunk_multi_angle_search",
    }


# =============================================================================
# The KnowledgeStore seam (task S4): search_chunks(exact=, tags=) end-to-end
# =============================================================================


@pytest.mark.asyncio
async def test_search_chunks_exact_sets_matched_arms(vector_pool):
    """`search_chunks(exact=[...])` with no query text: only the exact arm can
    fire (the recency arm is gated on a semantic query), so the identifier note
    comes back alone, attributed `['exact']`. Same seeding as
    test_exact_arm_finds_identifier_sparse_cannot, but driven through the store
    — this is what proves the 13 params the method binds line up with the
    function, and that `matched_arms` survives the second (knowledge_index)
    query's arbitrary row order."""
    kb = uuid.uuid4()
    n1 = await _seed_returning_id(
        vector_pool,
        kb,
        "sales_page_2026_09",
        "Sales",
        "the id is sales_page_2026_09 here",
        tags=["sales"],
    )
    n2 = await _seed_returning_id(
        vector_pool, kb, "other", "Other", "prose only", tags=["web"]
    )
    for n, t in ((n1, "the id is sales_page_2026_09 here"), (n2, "prose only")):
        await _chunk(vector_pool, n, kb, t)

    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    recs = await store.search_chunks(
        [kb], "", exact=["sales_page_2026"], embedding_version="v1"
    )
    assert [r.note_id for r in recs] == ["sales_page_2026_09"]
    assert recs[0].matched_arms == ["exact"]


@pytest.mark.asyncio
async def test_search_chunks_with_query_and_tags_reports_every_arm(vector_pool):
    """With query text present the recency arm is ungated, so a tagged note that
    also matches the sparse arm is attributed sparse + recency + tag. The
    recency assertion is the live proof of the function's `$1 <> '' OR $2 IS NOT
    NULL` gate (the difference between this and the exact-only call above)."""
    kb = uuid.uuid4()
    n1 = await _seed_returning_id(
        vector_pool, kb, "hot_note", "Hot", "shared words", tags=["hot"]
    )
    n2 = await _seed_returning_id(
        vector_pool, kb, "cold_note", "Cold", "shared words", tags=["cold"]
    )
    for n in (n1, n2):
        await _chunk(vector_pool, n, kb, "shared words")

    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    recs = await store.search_chunks(
        [kb], "shared words", tags=["hot"], embedding_version="v1"
    )
    ids = [r.note_id for r in recs]
    assert ids[0] == "hot_note" and "cold_note" in ids  # boost, not filter
    assert "recency" in recs[0].matched_arms
    assert {"sparse", "tag"} <= set(recs[0].matched_arms)
    assert "tag" not in recs[ids.index("cold_note")].matched_arms


@pytest.mark.asyncio
async def test_search_chunks_without_exact_or_tags_still_runs_the_old_function(
    vector_pool,
):
    """H6 at the seam, against a real server: a plain call must execute
    knowledge_chunk_hybrid_search — which returns SETOF knowledge_index, an
    entirely different shape from the new function's (note_row, rrf_score, arms)
    — and must carry no arm attribution."""
    kb = uuid.uuid4()
    n1 = await _seed_returning_id(
        vector_pool, kb, "plain_note", "Plain", "shared words", tags=["hot"]
    )
    await _chunk(vector_pool, n1, kb, "shared words")

    store = KnowledgeStore(db=vector_pool, embedding_service=_NullEmbeddings())
    recs = await store.search_chunks([kb], "shared words", embedding_version="v1")
    assert [r.note_id for r in recs] == ["plain_note"]
    assert recs[0].matched_arms == []
