"""Attributable knowledge history — vector migration 0016 + KnowledgeStore.get_note_revisions.

Guards knowledge-base/knowledge/features/workspace_and_change_records.md §6.2: ``knowledge_index``
is one row per note and both write paths overwrite the body in place
(``upsert_note``'s ``ON CONFLICT ... content = EXCLUDED.content`` and
``upsert_kb_note``'s ``(kb_id, path)`` branch), so prior content was
unrecoverable and "which job changed what note when" unanswerable. Migration
0016 adds ``knowledge_note_revisions`` plus BEFORE UPDATE / BEFORE DELETE
capture triggers on ``knowledge_index``; the trigger form is the point — it
sees every write path (both existing overwrite sites and any added later)
with zero application changes.

Three layers, mirroring the neighbouring idioms:

1. Structural assertions on the migration file text
   (test_experts_migration.py idiom) — no infrastructure needed.
2. Mocked-db unit tests for ``get_note_revisions``
   (test_knowledge_store.py idiom).
3. Real-pgvector integration: the full migration chain applied from zero via
   the real runner, then the triggers exercised through the REAL overwrite
   sites (test_kb_watermark_bindings.py idiom — skips when testcontainers or
   a container runtime is unavailable).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.services.knowledge_store import KnowledgeStore

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "orchestrator/database/migrations/vector/0016_knowledge_note_revisions.sql"
)


# =============================================================================
# 1. Structural assertions on the migration file
# =============================================================================


class TestMigrationShape:
    """DDL-shape assertions on 0016 (the repo's migration-test idiom)."""

    def test_migration_file_exists(self):
        assert MIGRATION.is_file(), "0016_knowledge_note_revisions.sql must exist"

    def test_revisions_table_and_envelope_columns(self):
        sql = MIGRATION.read_text()
        assert "CREATE TABLE IF NOT EXISTS knowledge_note_revisions" in sql
        for col in (
            "project_id UUID NOT NULL",
            "note_id VARCHAR(100) NOT NULL",
            "title TEXT NOT NULL",
            "note_type VARCHAR(50) NOT NULL",
            "content TEXT NOT NULL",
            "action VARCHAR(10) NOT NULL",
            "replaced_by_job_id UUID",
            "changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        ):
            assert col in sql, f"missing column DDL: {col}"
        assert "CHECK (action IN ('update', 'delete'))" in sql

    def test_history_is_cheap_no_embedding_or_search_doc(self):
        """§6.2: omit the embedding from the revision (regenerate on restore)."""
        sql = MIGRATION.read_text()
        table_block = sql.split("CREATE TABLE IF NOT EXISTS knowledge_note_revisions")[
            1
        ].split(");")[0]
        assert "embedding" not in table_block
        assert "search_doc" not in table_block
        assert "vector(" not in table_block

    def test_update_trigger_gated_on_content_or_title(self):
        """Status-only gardening flips must NOT copy the full body (bloat)."""
        sql = MIGRATION.read_text()
        assert "BEFORE UPDATE ON knowledge_index" in sql
        assert "FOR EACH ROW" in sql
        assert "OLD.content IS DISTINCT FROM NEW.content" in sql
        assert "OLD.title IS DISTINCT FROM NEW.title" in sql

    def test_delete_trigger_unconditional(self):
        sql = MIGRATION.read_text()
        assert "BEFORE DELETE ON knowledge_index" in sql
        # The DELETE trigger must not carry a WHEN gate — the final version is
        # always preserved. (WHEN only appears between the UPDATE trigger's
        # header and its EXECUTE line.)
        delete_block = sql.split("BEFORE DELETE ON knowledge_index")[1]
        assert "WHEN" not in delete_block.split("EXECUTE FUNCTION")[0]

    def test_per_note_history_index(self):
        sql = MIGRATION.read_text()
        assert re.search(
            r"CREATE INDEX IF NOT EXISTS idx_knowledge_note_revisions_note\s*\n?"
            r"\s*ON knowledge_note_revisions \(project_id, note_id, changed_at DESC\)",
            sql,
        )

    def test_transactional_header(self):
        sql = MIGRATION.read_text()
        assert sql.strip().startswith("-- migration:")
        assert "-- transactional: YES." in sql
        assert not MIGRATION.name.endswith(".notx.sql")


# =============================================================================
# 2. get_note_revisions() — mocked db (test_knowledge_store.py idiom)
# =============================================================================


def _make_store():
    """A KnowledgeStore with mocked db and embedding_service."""
    mock_db = AsyncMock()
    mock_embed = AsyncMock()
    store = KnowledgeStore(db=mock_db, embedding_service=mock_embed)
    return store, mock_db


def _revision_row(**overrides):
    defaults = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "note_id": "my-note",
        "title": "Old Title",
        "note_type": "decision",
        "status": "active",
        "confidence": None,
        "tags": ["a"],
        "keywords": ["b"],
        "job_id": uuid.uuid4(),
        "phase": 2,
        "content": "old body",
        "created_at": None,
        "modified_at": None,
        "action": "update",
        "replaced_by_job_id": uuid.uuid4(),
        "changed_at": None,
    }
    defaults.update(overrides)
    return defaults


class TestGetNoteRevisions:
    """Tests for KnowledgeStore.get_note_revisions()."""

    @pytest.mark.asyncio
    async def test_queries_newest_first_with_params(self):
        store, mock_db = _make_store()
        mock_db.fetch.return_value = []
        project_id = uuid.uuid4()

        await store.get_note_revisions(project_id, "my-note", limit=5)

        sql = mock_db.fetch.call_args[0][0]
        assert "FROM knowledge_note_revisions" in sql
        assert "ORDER BY changed_at DESC" in sql
        assert mock_db.fetch.call_args[0][1] == project_id
        assert mock_db.fetch.call_args[0][2] == "my-note"
        assert mock_db.fetch.call_args[0][3] == 5

    @pytest.mark.asyncio
    async def test_default_limit_is_20(self):
        store, mock_db = _make_store()
        mock_db.fetch.return_value = []

        await store.get_note_revisions(uuid.uuid4(), "my-note")

        assert mock_db.fetch.call_args[0][3] == 20

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_history(self):
        store, mock_db = _make_store()
        mock_db.fetch.return_value = []

        result = await store.get_note_revisions(uuid.uuid4(), "my-note")

        assert result == []

    @pytest.mark.asyncio
    async def test_maps_rows_to_dicts(self):
        store, mock_db = _make_store()
        row = _revision_row()
        mock_db.fetch.return_value = [row]

        result = await store.get_note_revisions(row["project_id"], "my-note")

        assert len(result) == 1
        rev = result[0]
        assert rev["title"] == "Old Title"
        assert rev["content"] == "old body"
        assert rev["action"] == "update"
        assert rev["replaced_by_job_id"] == row["replaced_by_job_id"]
        assert rev["job_id"] == row["job_id"]

    @pytest.mark.asyncio
    async def test_none_arrays_become_empty_lists(self):
        store, mock_db = _make_store()
        mock_db.fetch.return_value = [_revision_row(tags=None, keywords=None)]

        result = await store.get_note_revisions(uuid.uuid4(), "my-note")

        assert result[0]["tags"] == []
        assert result[0]["keywords"] == []


# =============================================================================
# 3. Real-pgvector integration — migration applies, triggers fire
#    (test_kb_watermark_bindings.py idiom)
# =============================================================================

pytest.importorskip("testcontainers.postgres")

from orchestrator.database.migrate import run_migrations  # noqa: E402
from orchestrator.database.postgres import PostgresDB  # noqa: E402

# pgvector, not plain postgres: the vector migrations CREATE EXTENSION vector.
PG_IMAGE = "pgvector/pgvector:pg15"
VECTOR_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "vector"
)


class _FixedEmbedding:
    """Embedding stub matching knowledge_index's vector(4096) column."""

    async def embed(self, text: str) -> List[float]:
        return [0.0] * 4096


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


@pytest.fixture(scope="module")
def migrated_dsn(pg_dsn):
    """Apply the real vector migration chain from zero — 0016 included.

    A failing 0016 fails right here, so "the migration applies" is itself
    under test, via the same runner production uses.
    """
    import asyncpg

    async def _migrate():
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
        try:
            await run_migrations(pool, VECTOR_MIGRATIONS)
        finally:
            await pool.close()

    asyncio.run(_migrate())
    return pg_dsn


@pytest_asyncio.fixture
async def store(migrated_dsn):
    """A KnowledgeStore over a real pool, embeddings stubbed to 4096 zeros."""
    db = PostgresDB(connection_string=migrated_dsn)
    await db.connect()
    try:
        yield KnowledgeStore(db=db, embedding_service=_FixedEmbedding())
    finally:
        await db.close()


async def _revisions(store, project_id, note_id):
    return await store.db.fetch(
        """
        SELECT * FROM knowledge_note_revisions
        WHERE project_id = $1 AND note_id = $2
        ORDER BY changed_at DESC
        """,
        project_id,
        note_id,
    )


@pytest.mark.asyncio
async def test_content_overwrite_cuts_revision_with_old_body(store):
    """The §6.2 headline: upsert_note's ON CONFLICT overwrite preserves OLD."""
    project_id = uuid.uuid4()
    job_a, job_b = uuid.uuid4(), uuid.uuid4()

    await store.upsert_note(
        note_id="chose-jwt",
        project_id=project_id,
        title="Chose JWT",
        note_type="decision",
        content="version one",
        job_id=job_a,
    )
    # Fresh INSERT — no UPDATE fired, so no revision yet.
    assert await _revisions(store, project_id, "chose-jwt") == []

    await store.upsert_note(
        note_id="chose-jwt",
        project_id=project_id,
        title="Chose JWT",
        note_type="decision",
        content="version two",
        job_id=job_b,
    )

    revs = await _revisions(store, project_id, "chose-jwt")
    assert len(revs) == 1
    assert revs[0]["action"] == "update"
    assert revs[0]["content"] == "version one"
    assert revs[0]["job_id"] == job_a
    assert revs[0]["replaced_by_job_id"] == job_b
    # History stays cheap: the live row keeps its embedding, the revision has
    # no such column at all.
    assert "embedding" not in dict(revs[0])
    assert "search_doc" not in dict(revs[0])


@pytest.mark.asyncio
async def test_status_only_flip_cuts_no_revision(store):
    """Supersede/archive gardening is frequent and already expressed in-row."""
    project_id = uuid.uuid4()

    await store.upsert_note(
        note_id="stale-goal",
        project_id=project_id,
        title="Goal",
        note_type="goal",
        content="unchanged body",
    )
    # Same content hash -> metadata-only UPDATE path; content/title unchanged.
    await store.upsert_note(
        note_id="stale-goal",
        project_id=project_id,
        title="Goal",
        note_type="goal",
        content="unchanged body",
        status="superseded",
    )

    row = await store.db.fetchrow(
        "SELECT status FROM knowledge_index WHERE project_id = $1 AND note_id = $2",
        project_id,
        "stale-goal",
    )
    assert row["status"] == "superseded"
    assert await _revisions(store, project_id, "stale-goal") == []


@pytest.mark.asyncio
async def test_title_only_change_cuts_revision(store):
    """The WHEN clause's second arm — a retitle is a real revision."""
    project_id = uuid.uuid4()

    await store.upsert_note(
        note_id="renamed",
        project_id=project_id,
        title="Old Title",
        note_type="learning",
        content="same body",
    )
    # Same content -> metadata-only UPDATE path, but the title differs.
    await store.upsert_note(
        note_id="renamed",
        project_id=project_id,
        title="New Title",
        note_type="learning",
        content="same body",
    )

    revs = await _revisions(store, project_id, "renamed")
    assert len(revs) == 1
    assert revs[0]["action"] == "update"
    assert revs[0]["title"] == "Old Title"


@pytest.mark.asyncio
async def test_kb_note_path_overwrite_cuts_revision(store):
    """The second overwrite site: upsert_kb_note's (kb_id, path) branch."""
    kb_id = uuid.uuid4()

    common = dict(
        kb_id=kb_id,
        note_id="vault-note",
        path="notes/vault-note.md",
        title="Vault Note",
        note_type="learning",
        blob_sha=None,
        embedding_version=None,
    )
    await store.upsert_kb_note(content="reindex pass one", **common)
    await store.upsert_kb_note(content="reindex pass two", **common)

    # upsert_kb_note writes project_id = kb_id (legacy-compat), so the
    # revision's project_id carries the same value.
    revs = await _revisions(store, kb_id, "vault-note")
    assert len(revs) == 1
    assert revs[0]["action"] == "update"
    assert revs[0]["content"] == "reindex pass one"


@pytest.mark.asyncio
async def test_delete_cuts_delete_revision(store):
    """kb note deletion exists via the API — the final version is preserved."""
    project_id = uuid.uuid4()
    job_a = uuid.uuid4()

    await store.upsert_note(
        note_id="doomed",
        project_id=project_id,
        title="Doomed",
        note_type="state",
        content="final words",
        job_id=job_a,
    )
    assert await store.delete_note(project_id, "doomed") is True

    revs = await _revisions(store, project_id, "doomed")
    assert len(revs) == 1
    assert revs[0]["action"] == "delete"
    assert revs[0]["content"] == "final words"
    assert revs[0]["job_id"] == job_a
    assert revs[0]["replaced_by_job_id"] is None


@pytest.mark.asyncio
async def test_get_note_revisions_newest_first_with_limit(store):
    """The read method over real trigger-written rows."""
    project_id = uuid.uuid4()

    for i, body in enumerate(["v1", "v2", "v3"]):
        await store.upsert_note(
            note_id="evolving",
            project_id=project_id,
            title="Evolving",
            note_type="decision",
            content=body,
        )
        # changed_at is statement-transaction time; keep successive writes on
        # distinct timestamps so the DESC ordering assertion is deterministic.
        await asyncio.sleep(0.01)
    await store.delete_note(project_id, "evolving")

    revs = await store.get_note_revisions(project_id, "evolving")
    # v1->v2 and v2->v3 overwrites plus the delete of v3.
    assert [r["action"] for r in revs] == ["delete", "update", "update"]
    assert [r["content"] for r in revs] == ["v3", "v2", "v1"]

    limited = await store.get_note_revisions(project_id, "evolving", limit=2)
    assert [r["content"] for r in limited] == ["v3", "v2"]
