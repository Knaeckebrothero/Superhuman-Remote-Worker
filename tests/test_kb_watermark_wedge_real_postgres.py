"""Real-Postgres proof for watermark wedge streak arithmetic (spec WP3, H3).

The wedge detector (Slice E item 3,
knowledge-base/knowledge/features/kb_retrieval_hardening_and_slice_d_additive.md
H3) needs to tell "the same failure repeating" from "a new failure" purely from
``kb_index_watermark`` state, and needs it computed atomically in the UPSERT
statement so two concurrent sweeps can't race the streak counter. A mock
can't catch a wrong ``CASE`` expression — only a real server evaluating the
real SQL proves the streak increments on a repeated fingerprint, resets on a
different one, and clears entirely once the KB goes ``ready`` again.
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


@pytest.mark.asyncio
async def test_streak_counts_identical_partial_runs_and_resets_on_ready(vector_pool):
    """Mirrors the real caller (``kb_reindex.py``): every sweep writes an
    ``indexing`` status up front, before its own terminal ``partial``/``ready``
    write. That ``indexing`` write must NOT reset the streak — only ``ready``
    or a fingerprint change may (spec H3). A prior version reset on any
    non-``partial`` status, so the intervening ``indexing`` write silently
    wiped the streak every run and the counter could never reach the
    threshold via the real call pattern; this test would have caught it.
    """
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    kb = uuid.uuid4()

    for expected in (1, 2, 3, 4):
        # The sweep-start write every real reindex run makes before its own
        # terminal write.
        await store.set_watermark_status(kb, "indexing", repo_name="r", branch="main")
        await store.set_watermark_status(
            kb,
            "partial",
            last_error="1 note operation(s) failed",
            error_fingerprint="abc123",
            repo_name="r",
            branch="main",
        )
        wm = await store.get_watermark(kb)
        assert wm.error_streak == expected
        assert (wm.wedged_since is not None) == (expected >= 4)

    # wedged_since is preserved (not re-stamped) on further identical-fingerprint
    # runs once the streak is already past the threshold.
    wedged_at_four = wm.wedged_since
    await store.set_watermark_status(kb, "indexing", repo_name="r", branch="main")
    await store.set_watermark_status(
        kb,
        "partial",
        last_error="1 note operation(s) failed",
        error_fingerprint="abc123",
        repo_name="r",
        branch="main",
    )
    wm = await store.get_watermark(kb)
    assert wm.error_streak == 5
    assert wm.wedged_since == wedged_at_four

    # A different failure set is a new streak, not a continuation — even with
    # an intervening indexing write.
    await store.set_watermark_status(kb, "indexing", repo_name="r", branch="main")
    await store.set_watermark_status(kb, "partial", error_fingerprint="zzz999")
    wm = await store.get_watermark(kb)
    assert wm.error_streak == 1 and wm.wedged_since is None

    # Ready clears everything and may carry an advisory.
    await store.upsert_watermark(
        kb, "r", "main", "deadbeef", "v1", status="ready", advisory="1 note skipped"
    )
    wm = await store.get_watermark(kb)
    assert wm.error_streak == 0 and wm.error_fingerprint is None
    assert wm.wedged_since is None and wm.advisory == "1 note skipped"


@pytest.mark.asyncio
async def test_upsert_watermark_resets_wedge_fields_only_on_ready(vector_pool):
    """Controller addendum (S0 re-review, 2026-09-04): a resumable-rebuild
    call to ``upsert_watermark`` with ``status="indexing"`` (the
    pipeline-version write ``kb_reindex.py`` makes before re-diffing, see
    ``clear_note_stamps``) must not reset the wedge streak — only a
    ``status="ready"`` write may. The prior unconditional reset in
    ``upsert_watermark``'s ON CONFLICT branch would silently wipe a streak
    mid-rebuild, the same reset class the S0 review caught in
    ``set_watermark_status``.
    """
    store = KnowledgeStore(db=vector_pool, embedding_service=None)
    kb = uuid.uuid4()

    for expected in (1, 2):
        await store.set_watermark_status(kb, "indexing", repo_name="r", branch="main")
        await store.set_watermark_status(
            kb,
            "partial",
            last_error="1 note operation(s) failed",
            error_fingerprint="abc123",
            repo_name="r",
            branch="main",
        )
        wm = await store.get_watermark(kb)
        assert wm.error_streak == expected

    # A resumable-rebuild "indexing" write via upsert_watermark must leave
    # the streak and fingerprint untouched.
    await store.upsert_watermark(kb, "r", "main", None, "v2", status="indexing")
    wm = await store.get_watermark(kb)
    assert wm.error_streak == 2
    assert wm.error_fingerprint == "abc123"

    # Only a "ready" write clears all three wedge fields.
    await store.upsert_watermark(kb, "r", "main", "deadbeef", "v2", status="ready")
    wm = await store.get_watermark(kb)
    assert wm.error_streak == 0
    assert wm.error_fingerprint is None
    assert wm.wedged_since is None
