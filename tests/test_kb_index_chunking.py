"""Tests for OKF KB slice 3 PR1 — chunk-granular index store surface.

Design: docs/features/okf_knowledge_base.md §5.1 / §11 slice-3 PR1.

PR1 is the SCHEMA + STORE surface, inert: it adds the persistence primitives the
git-tree reindexer (PR3) writes and the retrieval cutover (PR4) reads, but nothing
in the running system calls them yet. These tests pin the SQL each primitive
composes and the shapes it returns, against the 0008_kb_index_chunking.sql schema.

Covers:
  - KbWatermark / KnowledgeChunk dataclasses (from_row mapping)
  - KnowledgeStore watermark CRUD: get_watermark, upsert_watermark
  - KnowledgeStore.get_indexed_blob_shas — {path: blob_sha} for tree-diff self-heal
  - KnowledgeStore.upsert_kb_note — note-level row keyed by (kb_id, path)
  - KnowledgeStore.replace_note_chunks — atomic delete+insert of a note's chunks
  - KnowledgeStore.delete_kb_note — remove a note (cascade reaps its chunks)
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from src.services.knowledge_store import (
    KbWatermark,
    KnowledgeChunk,
    KnowledgeStore,
)


def _make_store():
    """KnowledgeStore with mocked async db + embedding service."""
    mock_db = AsyncMock()
    mock_embed = AsyncMock()
    mock_embed.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
    return KnowledgeStore(db=mock_db, embedding_service=mock_embed), mock_db, mock_embed


# =============================================================================
# KbWatermark dataclass
# =============================================================================


class TestKbWatermarkFromRow:
    def test_maps_all_fields(self):
        kb = uuid.uuid4()
        wm = KbWatermark.from_row(
            {
                "kb_id": kb,
                "repo_name": "project-68137e29-jobs",
                "branch": "main",
                "indexed_commit": "abc123",
                "pipeline_version": "qwen3-8b:4096:v1",
                "updated_at": "2026-07-05T00:00:00Z",
            }
        )
        assert wm.kb_id == kb
        assert wm.repo_name == "project-68137e29-jobs"
        assert wm.branch == "main"
        assert wm.indexed_commit == "abc123"
        assert wm.pipeline_version == "qwen3-8b:4096:v1"
        assert wm.updated_at == "2026-07-05T00:00:00Z"

    def test_missing_keys_default_to_none(self):
        wm = KbWatermark.from_row({"kb_id": uuid.uuid4()})
        assert wm.repo_name is None
        assert wm.branch is None
        assert wm.indexed_commit is None
        assert wm.pipeline_version is None
        assert wm.updated_at is None


# =============================================================================
# get_watermark
# =============================================================================


class TestGetWatermark:
    @pytest.mark.asyncio
    async def test_selects_by_kb_id_and_parses(self):
        store, mock_db, _ = _make_store()
        kb = uuid.uuid4()
        mock_db.fetchrow.return_value = {
            "kb_id": kb,
            "repo_name": "r",
            "branch": "main",
            "indexed_commit": "deadbeef",
            "pipeline_version": "v1",
            "updated_at": None,
        }
        wm = await store.get_watermark(kb)
        query, *params = mock_db.fetchrow.call_args[0]
        assert "kb_index_watermark" in query
        assert "kb_id = $1" in query
        assert params == [kb]
        assert wm.indexed_commit == "deadbeef"

    @pytest.mark.asyncio
    async def test_returns_none_when_absent(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchrow.return_value = None
        assert await store.get_watermark(uuid.uuid4()) is None


# =============================================================================
# upsert_watermark
# =============================================================================


class TestUpsertWatermark:
    @pytest.mark.asyncio
    async def test_inserts_on_conflict_updates(self):
        store, mock_db, _ = _make_store()
        kb = uuid.uuid4()
        await store.upsert_watermark(
            kb_id=kb,
            repo_name="project-68137e29-jobs",
            branch="main",
            indexed_commit="abc123",
            pipeline_version="qwen3-8b:4096:v1",
        )
        query, *params = mock_db.execute.call_args[0]
        assert "INSERT INTO kb_index_watermark" in query
        assert "ON CONFLICT (kb_id) DO UPDATE" in query
        # indexed_commit + pipeline_version must be in both the VALUES and the SET
        assert query.count("indexed_commit") >= 2
        assert query.count("pipeline_version") >= 2
        assert params[0] == kb
        assert "abc123" in params
        assert "qwen3-8b:4096:v1" in params

    @pytest.mark.asyncio
    async def test_stamps_updated_at_via_now(self):
        store, mock_db, _ = _make_store()
        await store.upsert_watermark(
            kb_id=uuid.uuid4(),
            repo_name="r",
            branch="main",
            indexed_commit="c",
            pipeline_version="v",
        )
        query = mock_db.execute.call_args[0][0]
        assert "NOW()" in query


# =============================================================================
# get_indexed_blob_shas
# =============================================================================


class TestGetIndexedBlobShas:
    @pytest.mark.asyncio
    async def test_returns_path_to_blob_sha_map(self):
        store, mock_db, _ = _make_store()
        kb = uuid.uuid4()
        mock_db.fetch.return_value = [
            {"path": "knowledge/a.md", "blob_sha": "sha_a"},
            {"path": "knowledge/b.md", "blob_sha": "sha_b"},
        ]
        result = await store.get_indexed_blob_shas(kb)
        assert result == {"knowledge/a.md": "sha_a", "knowledge/b.md": "sha_b"}
        query, *params = mock_db.fetch.call_args[0]
        assert "knowledge_index" in query
        assert "kb_id = $1" in query
        assert "path IS NOT NULL" in query
        assert params == [kb]

    @pytest.mark.asyncio
    async def test_empty_when_nothing_indexed(self):
        store, mock_db, _ = _make_store()
        mock_db.fetch.return_value = []
        assert await store.get_indexed_blob_shas(uuid.uuid4()) == {}


# =============================================================================
# KnowledgeChunk dataclass
# =============================================================================


class TestKnowledgeChunkFromRow:
    def test_maps_all_fields(self):
        note_row = uuid.uuid4()
        kb = uuid.uuid4()
        ch = KnowledgeChunk.from_row(
            {
                "id": None,
                "note_row": note_row,
                "kb_id": kb,
                "chunk_ix": 3,
                "heading_path": "Design > Storage",
                "content": "chunk body",
                "embedding_version": "qwen3-8b:4096:v1",
                "created_at": None,
            }
        )
        assert ch.note_row == note_row
        assert ch.kb_id == kb
        assert ch.chunk_ix == 3
        assert ch.heading_path == "Design > Storage"
        assert ch.content == "chunk body"
        assert ch.embedding_version == "qwen3-8b:4096:v1"

    def test_defaults_on_missing(self):
        ch = KnowledgeChunk.from_row({})
        assert ch.chunk_ix == 0
        assert ch.content == ""
        assert ch.heading_path is None
        assert ch.embedding_version is None


# =============================================================================
# upsert_kb_note — note-level row keyed by (kb_id, path)
# =============================================================================


class TestUpsertKbNote:
    @pytest.mark.asyncio
    async def test_inserts_keyed_by_kb_path_returning_id(self):
        store, mock_db, _ = _make_store()
        row_id = uuid.uuid4()
        mock_db.fetchval.return_value = row_id
        kb = uuid.uuid4()
        result = await store.upsert_kb_note(
            kb_id=kb,
            note_id="chose-jwt-over-oauth",
            path="knowledge/chose-jwt.md",
            title="Chose JWT",
            note_type="decision",
            content="body",
            blob_sha="blob123",
            embedding_version="qwen3-8b:4096:v1",
        )
        assert result == row_id
        query, *params = mock_db.fetchval.call_args[0]
        assert "INSERT INTO knowledge_index" in query
        assert "ON CONFLICT (kb_id, path)" in query
        assert "DO UPDATE" in query
        assert "RETURNING id" in query
        # the reindex-critical columns must be written
        assert kb in params
        assert "knowledge/chose-jwt.md" in params
        assert "blob123" in params
        assert "qwen3-8b:4096:v1" in params

    @pytest.mark.asyncio
    async def test_note_row_carries_no_dense_vector(self):
        # In the chunk model the embedding lives on knowledge_chunks, never the
        # note row — the note INSERT must not touch the `embedding` column.
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = uuid.uuid4()
        await store.upsert_kb_note(
            kb_id=uuid.uuid4(),
            note_id="n",
            path="knowledge/n.md",
            title="T",
            note_type="learning",
            content="body",
            blob_sha="b",
            embedding_version="v1",
        )
        query = mock_db.fetchval.call_args[0][0]
        # `embedding_version` is fine; a bare `embedding` column is not.
        assert "embedding" not in query.replace("embedding_version", "")

    @pytest.mark.asyncio
    async def test_backfills_project_id_from_kb_for_legacy_filters(self):
        # Legacy queries (get_summary, hybrid_search) still filter project_id;
        # a project-scoped KB's id IS its project_id, so the new row carries it.
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = uuid.uuid4()
        kb = uuid.uuid4()
        await store.upsert_kb_note(
            kb_id=kb,
            note_id="n",
            path="knowledge/n.md",
            title="T",
            note_type="learning",
            content="body",
            blob_sha="b",
            embedding_version="v1",
        )
        query, *params = mock_db.fetchval.call_args[0]
        # Both columns are written, and both bind to the same $1 (the kb id) —
        # project_id = kb_id, so the legacy project-scoped filters see the row.
        col_list = query.split("VALUES")[0]
        assert "kb_id" in col_list and "project_id" in col_list
        assert "($1, $1," in query
        assert kb in params

    @pytest.mark.asyncio
    async def test_conflict_update_preserves_created_at(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = uuid.uuid4()
        await store.upsert_kb_note(
            kb_id=uuid.uuid4(),
            note_id="n",
            path="knowledge/n.md",
            title="T",
            note_type="learning",
            content="body",
            blob_sha="b",
            embedding_version="v1",
        )
        query = mock_db.fetchval.call_args[0][0]
        assert "created_at" not in query.split("DO UPDATE")[1]


# =============================================================================
# replace_note_chunks — atomic delete + insert of a note's chunks
# =============================================================================


class TestReplaceNoteChunks:
    @pytest.mark.asyncio
    async def test_deletes_then_inserts_each_chunk(self):
        store, mock_db, _ = _make_store()
        note_row = uuid.uuid4()
        kb = uuid.uuid4()
        chunks = [
            {
                "chunk_ix": 0,
                "heading_path": "Intro",
                "content": "first",
                "embedding": [0.1, 0.2],
            },
            {
                "chunk_ix": 1,
                "heading_path": "Design > Storage",
                "content": "second",
                "embedding": [0.3, 0.4],
            },
        ]
        n = await store.replace_note_chunks(
            note_row=note_row,
            kb_id=kb,
            chunks=chunks,
            embedding_version="v1",
        )
        assert n == 2
        calls = mock_db.execute.call_args_list
        # first call clears the note's existing chunks
        assert "DELETE FROM knowledge_chunks" in calls[0][0][0]
        assert calls[0][0][1] == note_row
        # then one INSERT per chunk
        insert_calls = [c for c in calls if "INSERT INTO knowledge_chunks" in c[0][0]]
        assert len(insert_calls) == 2
        first_insert = insert_calls[0]
        assert "to_tsvector" in first_insert[0][0]  # search_doc populated at write
        assert note_row in first_insert[0]
        assert kb in first_insert[0]
        assert "first" in first_insert[0]
        assert "v1" in first_insert[0]

    @pytest.mark.asyncio
    async def test_empty_chunks_clears_and_returns_zero(self):
        store, mock_db, _ = _make_store()
        note_row = uuid.uuid4()
        n = await store.replace_note_chunks(
            note_row=note_row,
            kb_id=uuid.uuid4(),
            chunks=[],
            embedding_version="v1",
        )
        assert n == 0
        calls = mock_db.execute.call_args_list
        assert len(calls) == 1
        assert "DELETE FROM knowledge_chunks" in calls[0][0][0]

    @pytest.mark.asyncio
    async def test_embedding_passed_through_prepare(self):
        # A legacy string embedding must be normalized to a list before the codec.
        store, mock_db, _ = _make_store()
        await store.replace_note_chunks(
            note_row=uuid.uuid4(),
            kb_id=uuid.uuid4(),
            chunks=[{"chunk_ix": 0, "content": "c", "embedding": "[0.1,0.2]"}],
            embedding_version="v1",
        )
        insert_call = [
            c
            for c in mock_db.execute.call_args_list
            if "INSERT INTO knowledge_chunks" in c[0][0]
        ][0]
        assert [0.1, 0.2] in insert_call[0]


# =============================================================================
# adopt_legacy_row — claim a pre-slice-3 row by (kb_id, note_id), set its path
# =============================================================================


class TestAdoptLegacyRow:
    @pytest.mark.asyncio
    async def test_claims_pathless_row_and_returns_id(self):
        # A legacy slice-1/2 row has (project_id, note_id) but path IS NULL.
        # Adopting it sets path (and kb_id) so upsert_kb_note's (kb_id, path)
        # INSERT can't unique-violate on uq_knowledge_project_note.
        store, mock_db, _ = _make_store()
        row_id = uuid.uuid4()
        mock_db.fetchval.return_value = row_id
        kb = uuid.uuid4()
        result = await store.adopt_legacy_row(kb, "chose-jwt", "knowledge/chose-jwt.md")
        assert result == row_id
        query, *params = mock_db.fetchval.call_args[0]
        assert "UPDATE knowledge_index" in query
        assert "path IS NULL" in query  # never steals an already-migrated row
        assert "project_id = $1" in query
        assert "note_id = $2" in query
        assert "RETURNING id" in query
        # sets BOTH kb_id and path on the claimed row
        set_clause = query.split("SET")[1].split("WHERE")[0]
        assert "kb_id" in set_clause and "path" in set_clause
        assert params == [kb, "chose-jwt", "knowledge/chose-jwt.md"]

    @pytest.mark.asyncio
    async def test_returns_none_when_no_legacy_row(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = None
        assert await store.adopt_legacy_row(uuid.uuid4(), "n", "knowledge/n.md") is None


# =============================================================================
# delete_kb_note — remove a note (cascade reaps its chunks)
# =============================================================================


class TestDeleteKbNote:
    @pytest.mark.asyncio
    async def test_deletes_by_kb_and_path(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = uuid.uuid4()
        kb = uuid.uuid4()
        assert await store.delete_kb_note(kb, "knowledge/n.md") is True
        query, *params = mock_db.fetchval.call_args[0]
        assert "DELETE FROM knowledge_index" in query
        assert "kb_id = $1" in query
        assert "path = $2" in query
        assert "RETURNING id" in query
        assert params == [kb, "knowledge/n.md"]

    @pytest.mark.asyncio
    async def test_returns_false_when_absent(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = None
        assert await store.delete_kb_note(uuid.uuid4(), "knowledge/x.md") is False


# =============================================================================
# KnowledgeStore.stamp_note_indexed — the chunks-are-durable stamp (PR3.1)
# =============================================================================


class TestStampNoteIndexed:
    """blob_sha/embedding_version mean "this note's chunks are fully written".
    The reindexer upserts the note UNSTAMPED, writes chunks, then stamps — so a
    chunk-write failure leaves blob_sha NULL and the next diff retries the note
    (live gap 2026-07-05: notes stamped, zero chunks, self-heal defeated)."""

    @pytest.mark.asyncio
    async def test_stamps_blob_sha_and_version_by_row_id(self):
        store, db, _ = _make_store()
        row_id = uuid.uuid4()
        await store.stamp_note_indexed(row_id, "abc123", "m:4096:c1")
        query = db.execute.await_args[0][0]
        params = db.execute.await_args[0][1:]
        assert "UPDATE knowledge_index" in query
        assert "blob_sha" in query
        assert "embedding_version" in query
        assert "WHERE id = $1" in query
        assert params == (row_id, "abc123", "m:4096:c1")
