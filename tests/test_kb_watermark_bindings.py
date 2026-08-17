"""Real-Postgres tests for ``KnowledgeStore.upsert_watermark`` parameter typing.

Guards knowledge-history/done/kb_reindex_watermark_never_advances.md. ``upsert_watermark``
bound ``$4`` (``indexed_commit``) both directly — deducing ``character
varying`` from the column — and inside ``COALESCE($6, $4)``, whose two untyped
arguments made Postgres resolve it to ``text``. One parameter cannot be both,
so the statement was rejected *at parse time* with::

    inconsistent types deduced for parameter $4
    DETAIL:  text versus character varying

That statement is the last step of a reindex, so every run wrote all its rows,
died here, and left ``indexed_commit`` unadvanced — each subsequent reindex
re-diffed from zero and re-embedded the entire vault.

**A mock cannot fail this way.** The defect lives entirely in Postgres's
parameter-type deduction; ``AsyncMock.execute`` accepts the broken SQL happily.
So these run against a real pgvector container. The sibling
``set_watermark_status`` is exercised alongside because it is the control: its
``COALESCE``'s second argument is a *column*, which pins the type on its own,
and it worked throughout.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.postgres")

from orchestrator.database.migrate import run_migrations  # noqa: E402
from orchestrator.database.postgres import PostgresDB  # noqa: E402
from src.services.knowledge_store import KnowledgeStore  # noqa: E402

# pgvector, not plain postgres: the vector migrations CREATE EXTENSION vector.
PG_IMAGE = "pgvector/pgvector:pg15"
VECTOR_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "vector"
)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
HEAD_A = "c" * 40
HEAD_B = "d" * 40


@pytest.fixture(scope="module")
def pg_dsn():
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(PG_IMAGE)
    try:
        container.start()
    except Exception as exc:  # pragma: no cover - env without a runtime
        pytest.skip(f"no container runtime for testcontainers: {exc}")
    try:
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


@pytest_asyncio.fixture(scope="module")
async def migrated_dsn(pg_dsn):
    """Apply the real vector migrations once for the module.

    Deliberately the real migration set rather than a hand-copied CREATE TABLE:
    the bug is a mismatch against the *actual* column types, so the schema under
    test has to be the one production runs (``indexed_commit``/``source_head``
    are ``VARCHAR(64)``; ``status``/``repo_name`` are ``TEXT``). If a future
    migration retypes those columns, this test follows it.
    """
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
    try:
        await run_migrations(pool, VECTOR_MIGRATIONS)
    finally:
        await pool.close()
    return pg_dsn


@pytest_asyncio.fixture
async def store(migrated_dsn):
    """A KnowledgeStore over a real connection pool.

    ``embedding_service=None`` — the watermark path never embeds.
    """
    db = PostgresDB(connection_string=migrated_dsn)
    await db.connect()
    try:
        yield KnowledgeStore(db=db, embedding_service=None)
    finally:
        await db.close()


async def _row(store, kb_id):
    return await store.db.fetchrow(
        "SELECT * FROM kb_index_watermark WHERE kb_id = $1", kb_id
    )


# =============================================================================
# The regression: every one of these raised AmbiguousParameterError before the
# ``::varchar`` casts. The failure is at parse time, so it did not depend on
# the parameter *values* — the NULL and non-NULL source_head paths, and both
# the INSERT and the ON CONFLICT branch, all died identically.
# =============================================================================


@pytest.mark.asyncio
async def test_fresh_insert_without_source_head(store):
    """The live reindex path: source_head omitted, no row yet."""
    kb_id = uuid.uuid4()

    await store.upsert_watermark(kb_id, "vault", "main", COMMIT_A, "v3")

    row = await _row(store, kb_id)
    assert row is not None, "watermark row was never written"
    assert row["indexed_commit"] == COMMIT_A
    # COALESCE($6, $4): source_head falls back to the indexed commit.
    assert row["source_head"] == COMMIT_A
    assert row["status"] == "ready"
    assert row["last_success_at"] is not None
    assert row["repo_name"] == "vault"
    assert row["branch"] == "main"
    assert row["pipeline_version"] == "v3"


@pytest.mark.asyncio
async def test_conflict_update_advances_indexed_commit(store):
    """The ON CONFLICT branch — the one that must advance the cursor.

    This is the whole point of the issue: a second reindex has to move
    ``indexed_commit`` forward, or every run re-embeds the whole vault.
    """
    kb_id = uuid.uuid4()

    await store.upsert_watermark(kb_id, "vault", "main", COMMIT_A, "v3")
    await store.upsert_watermark(kb_id, "vault", "main", COMMIT_B, "v3")

    row = await _row(store, kb_id)
    assert row["indexed_commit"] == COMMIT_B
    assert row["source_head"] == COMMIT_B


@pytest.mark.asyncio
async def test_explicit_source_head_is_kept_distinct(store):
    """source_head non-NULL on both branches: COALESCE must prefer $6.

    Casting must not collapse the two columns into one value — an over-eager
    "fix" that dropped $6 would still pass the NULL-source_head tests above.
    """
    kb_id = uuid.uuid4()

    await store.upsert_watermark(
        kb_id, "vault", "main", COMMIT_A, "v3", source_head=HEAD_A
    )
    row = await _row(store, kb_id)
    assert (row["indexed_commit"], row["source_head"]) == (COMMIT_A, HEAD_A)

    await store.upsert_watermark(
        kb_id, "vault", "main", COMMIT_B, "v4", source_head=HEAD_B
    )
    row = await _row(store, kb_id)
    assert (row["indexed_commit"], row["source_head"]) == (COMMIT_B, HEAD_B)
    assert row["pipeline_version"] == "v4"


@pytest.mark.asyncio
async def test_failed_status_records_error_and_keeps_last_success_at(store):
    """The CASE arm: a failed run must not stamp — or wipe — last_success_at."""
    kb_id = uuid.uuid4()

    await store.upsert_watermark(kb_id, "vault", "main", COMMIT_A, "v3")
    succeeded_at = (await _row(store, kb_id))["last_success_at"]

    await store.upsert_watermark(
        kb_id,
        "vault",
        "main",
        COMMIT_A,
        "v3",
        status="failed",
        last_error="boom",
    )

    row = await _row(store, kb_id)
    assert row["status"] == "failed"
    assert row["last_error"] == "boom"
    assert row["last_success_at"] == succeeded_at


@pytest.mark.asyncio
async def test_set_watermark_status_still_works(store):
    """Control: the sibling writer, which never had the defect."""
    kb_id = uuid.uuid4()

    await store.set_watermark_status(kb_id, "indexing")
    assert (await _row(store, kb_id))["status"] == "indexing"

    await store.set_watermark_status(kb_id, "ready", source_head=HEAD_A)
    row = await _row(store, kb_id)
    assert row["status"] == "ready"
    assert row["source_head"] == HEAD_A


# =============================================================================
# Harness canary
# =============================================================================


@pytest.mark.asyncio
async def test_uncast_statement_is_still_rejected_by_postgres(store):
    """The pre-fix statement text, verbatim, must still be rejected.

    Without this, the tests above could go quietly vacuous — a future rewrite of
    the fixture (a laxer stand-in for PostgresDB, a hand-rolled table with TEXT
    columns) would leave them green while losing all power to detect the bug.
    This pins the mechanism itself: two untyped parameters in one COALESCE,
    against the real schema, is a parse error. If this ever passes, the harness
    has stopped being able to fail and the casts above are no longer protected.
    """
    with pytest.raises(asyncpg.exceptions.AmbiguousParameterError) as excinfo:
        await store.db.execute(
            """
            INSERT INTO kb_index_watermark
                (kb_id, repo_name, branch, indexed_commit, pipeline_version,
                 source_head, status, last_attempt_at, last_success_at,
                 last_error, updated_at)
            VALUES ($1, $2, $3, $4, $5, COALESCE($6, $4), $7, NOW(),
                    CASE WHEN $7 = 'ready' THEN NOW() ELSE NULL END, $8, NOW())
            ON CONFLICT (kb_id) DO UPDATE
               SET indexed_commit = $4,
                   source_head = COALESCE($6, $4)
            """,
            uuid.uuid4(),
            "vault",
            "main",
            COMMIT_A,
            "v3",
            None,
            "ready",
            None,
        )

    assert "parameter $4" in str(excinfo.value)
