"""Real-Postgres proof for the KB exact/grep channel's trigram index (spec WP6).

``CREATE EXTENSION`` and ``CREATE INDEX ... USING gin`` are exactly the kind of
statement a mock can't validate — only a real server confirms the extension
actually installs and the index actually exists after ``run_migrations``.

S1 (this file's origin) adds only the index-existence assertion below; S2
extends this file with the grep-channel behavior it backs.
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
from src.services.knowledge_store import (  # noqa: E402
    KnowledgeStore,
    _grep_candidates_sql,
)

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


@pytest.mark.asyncio
async def test_pg_trgm_extension_and_content_index_exist(vector_pool):
    async with vector_pool.acquire() as conn:
        extnames = {
            row["extname"]
            for row in await conn.fetch("SELECT extname FROM pg_extension")
        }
        assert "pg_trgm" in extnames

        indexnames = {
            row["indexname"]
            for row in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
        }
        assert "idx_knowledge_content_trgm" in indexnames


async def _seed(pool, kb, note_id, title, content, tags=()):
    # $3 is cast to ::text at both use sites (the note_id column value and the
    # path concatenation) — reusing an untyped parameter in two positions that
    # asyncpg would otherwise infer as different types (character varying for
    # the column, text for the `||` concat) raises AmbiguousParameterError.
    await pool.execute(
        """
        INSERT INTO knowledge_index
            (id, project_id, kb_id, note_id, title, note_type, status, content,
             tags, path, indexed_at)
        VALUES ($1, $2, $2, $3::text, $4, 'learning', 'active', $5, $6,
                'knowledge/' || $3::text || '.md', NOW())
        """,
        uuid.uuid4(),
        kb,
        note_id,
        title,
        content,
        list(tags),
    )


@pytest.mark.asyncio
async def test_substring_grep_returns_lines_with_context_and_note_count(vector_pool):
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    kb = uuid.uuid4()
    await _seed(
        vector_pool,
        kb,
        "sales_page_2026_09",
        "Sales",
        "intro\nsee sales_page_2026_09 for the page\noutro",
        tags=["sales", "web"],
    )
    await _seed(vector_pool, kb, "other", "Other", "nothing here", tags=["web"])

    matches, total = await store.grep_notes([kb], "SALES_PAGE_2026")
    assert total == 1
    assert [m.note_id for m in matches] == ["sales_page_2026_09"]
    m = matches[0]
    assert m.line_no == 2 and "sales_page_2026_09" in m.line
    assert m.before == ["intro"] and m.after == ["outro"]


@pytest.mark.asyncio
async def test_substring_grep_pattern_with_percent_and_underscore_is_literal(
    vector_pool,
):
    """`%` and `_` in the pattern must match themselves, not act as SQL
    wildcards. The decoy line satisfies the *unescaped* ILIKE pattern
    (`%50%_off%` reads as: contains "50", then anything, then exactly one
    char, then "off") but not the literal string "50%_off" — so a match on
    only the literal-target note proves the pattern was escaped."""
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    kb = uuid.uuid4()
    await _seed(
        vector_pool,
        kb,
        "literal-target",
        "Literal",
        "before\nprice is 50%_off today\nafter",
    )
    await _seed(
        vector_pool,
        kb,
        "wildcard-decoy",
        "Decoy",
        "before\nprice is 50xyzZoff today\nafter",
    )

    matches, total = await store.grep_notes([kb], "50%_off")
    assert total == 1
    assert [m.note_id for m in matches] == ["literal-target"]
    assert matches[0].line == "price is 50%_off today"


@pytest.mark.asyncio
async def test_regex_grep_and_cap(vector_pool):
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    kb = uuid.uuid4()
    await _seed(vector_pool, kb, "a", "A", "\n".join(f"err-{i:03d}" for i in range(10)))
    matches, total = await store.grep_notes(
        [kb], r"err-00\d", regex=True, max_matches=3
    )
    assert total == 1 and len(matches) == 3


@pytest.mark.asyncio
async def test_grep_notes_cap_across_multiple_notes_reports_full_count(vector_pool):
    """Fix round 1, finding 4: the candidate fetch is `LIMIT`-ed to
    `max_matches` notes (so a broad pattern can't pull every matching body
    across the wire before the cap applies), but `matching_note_count` must
    still report the true, uncapped candidate count — computed by a separate
    `SELECT count(*)` over the same predicate."""
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    kb = uuid.uuid4()
    for i in range(5):
        await _seed(vector_pool, kb, f"needle-{i}", f"Needle {i}", "needle-xyz line")

    matches, total = await store.grep_notes([kb], "needle-xyz", max_matches=2)
    assert len(matches) == 2
    assert total == 5


@pytest.mark.asyncio
async def test_regex_grep_is_newline_sensitive_anchors_match_per_line(vector_pool):
    """Fix round 1, finding 2: Postgres's `~*` is newline-*insensitive* by
    default, so `^` anchors only the start of the WHOLE body, not each
    line — a note whose only match is on a later line would never even
    become a SQL candidate (silent false negative on the most common grep
    idiom). The `(?n)` prefix on the regex predicate (`_grep_where_clause`)
    fixes this; this seeds a note whose match is on line 2."""
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    kb = uuid.uuid4()
    await _seed(vector_pool, kb, "todo-note", "Todo", "intro\nTODO fix this\noutro")

    matches, total = await store.grep_notes([kb], "^TODO", regex=True)
    assert total == 1
    assert len(matches) == 1
    assert matches[0].line_no == 2


@pytest.mark.asyncio
async def test_regex_grep_dot_does_not_cross_newline(vector_pool):
    """Fix round 1, finding 2: without `(?n)`, Postgres's `.` matches `\\n`,
    so a pattern like `1.l` could select a note as a SQL candidate by
    matching ACROSS a line boundary that no single Python-extracted line
    then satisfies — silently returning `matches=[]` with `total=1`. With
    `(?n)`, `.` doesn't cross the newline, so this note is correctly not a
    candidate at all: `matches=[]` AND `total=0`."""
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    kb = uuid.uuid4()
    await _seed(vector_pool, kb, "cross-line", "Cross", "abc1\nlxyz")

    matches, total = await store.grep_notes([kb], "1.l", regex=True)
    assert matches == []
    assert total == 0


@pytest.mark.asyncio
async def test_pattern_guards(vector_pool):
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    with pytest.raises(ValueError):
        await store.grep_notes([uuid.uuid4()], "")
    with pytest.raises(ValueError):
        await store.grep_notes([uuid.uuid4()], "x" * 257)


@pytest.mark.asyncio
async def test_max_matches_below_one_raises(vector_pool):
    """Fix round 1, finding 3: `max_matches=0` (or negative) must reject
    rather than silently returning a single match."""
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    with pytest.raises(ValueError):
        await store.grep_notes([uuid.uuid4()], "x", max_matches=0)
    with pytest.raises(ValueError):
        await store.grep_notes([uuid.uuid4()], "x", max_matches=-1)


@pytest.mark.asyncio
async def test_grep_notes_empty_kb_ids_short_circuits(vector_pool):
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    assert await store.grep_notes([], "anything") == ([], 0)


@pytest.mark.asyncio
async def test_tag_vocabulary_counts_desc(vector_pool):
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    kb = uuid.uuid4()
    await _seed(vector_pool, kb, "n1", "N1", "b", tags=["web", "sales"])
    await _seed(vector_pool, kb, "n2", "N2", "b", tags=["web"])
    assert await store.tag_vocabulary(kb) == [("web", 2), ("sales", 1)]


@pytest.mark.asyncio
async def test_ilike_path_uses_trigram_index(vector_pool):
    """Fix round 1, finding 1: a single combined
    `(content ILIKE … OR title ILIKE …)` predicate defeats the trigram index
    — Postgres can't build a `BitmapOr` across an indexed column (`content`)
    and an unindexed one (`title`, H8), so it seq-scans regardless of table
    size, including on the *default* call (`include_titles=True`). This runs
    the literal SQL `grep_notes` emits for that default shape
    (`_grep_candidates_sql`, not a hand-typed approximation of it) and
    asserts the UNION-restructured query still lets the `content` branch use
    `idx_knowledge_content_trgm`."""
    sql = _grep_candidates_sql(regex=False, include_titles=True)
    kb = uuid.uuid4()

    async def _plan():
        rows = await vector_pool.fetch(f"EXPLAIN {sql}", [kb], "needle-xyz", 50)
        text = "\n".join(r[0] for r in rows)
        return text, "idx_knowledge_content_trgm" in text

    plan, used = await _plan()
    if not used:
        # An almost-empty table makes a seq scan look cheaper than the GIN
        # index to the planner. Measured directly against this schema+image:
        # short (~60 byte) filler content never crosses over even at 4000
        # rows (still a seq scan, cost ~180) — such small rows pack
        # many-per-page. Content *identical* across every row is unreliable
        # in the other direction: with only a handful of distinct trigrams
        # in the whole table, ANALYZE's row-sampling estimate of the GIN
        # index's selectivity is noisy enough to flip the chosen plan between
        # otherwise-identical runs. ~1.1KB of shared filler text *plus a
        # unique per-row marker* (so ANALYZE sees real trigram diversity)
        # reliably crosses the seq-scan/index-scan cost crossover by 6000
        # rows — seeded via a bulk multi-row INSERT (not a loop of
        # single-row `_seed` calls, which took 12s+ for a third this many
        # rows) under the SAME kb this query filters on, then ANALYZE so the
        # row-count estimate reflects the just-inserted rows instead of
        # stale (empty-table) statistics autovacuum hasn't refreshed yet.
        filler = "lorem ipsum dolor sit amet consectetur adipiscing elit " * 20
        rows_to_insert = [
            (
                uuid.uuid4(),
                kb,
                kb,
                f"seed-{i:06d}",
                f"Seed {i}",
                "learning",
                "active",
                f"{filler} row-marker-{i:06d}",
                [],
                f"knowledge/seed-{i:06d}.md",
            )
            for i in range(6000)
        ]
        await vector_pool.executemany(
            """
            INSERT INTO knowledge_index
                (id, project_id, kb_id, note_id, title, note_type, status,
                 content, tags, path, indexed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            """,
            rows_to_insert,
        )
        await vector_pool.execute("ANALYZE knowledge_index")
        plan, used = await _plan()
    assert used, (
        "planner did not choose idx_knowledge_content_trgm for the default "
        "(include_titles=True) call:\n" + plan
    )
