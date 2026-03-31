"""Tests for src/services/knowledge_store.py.

Covers section 11 of persistent_agent_tests.md:
  11.1  KnowledgeRecord dataclass
  11.2  KnowledgeStore._content_hash()
  11.3  KnowledgeStore._prepare_embedding()
  11.4  upsert_note() — metadata-only update
  11.5  upsert_note() — content changed
  11.6  delete_note()
  11.7  hybrid_search()
  11.8  get_summary()
  11.9  rebuild_from_notes()
  11.10 format_note() (static)
  11.11 assemble_knowledge_block() (classmethod)
"""

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.knowledge_store import KnowledgeRecord, KnowledgeStore


# =============================================================================
# Helpers
# =============================================================================


def _make_store():
    """Create a KnowledgeStore with mocked db and embedding_service."""
    mock_db = AsyncMock()
    mock_embed = AsyncMock()
    mock_embed.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
    store = KnowledgeStore(db=mock_db, embedding_service=mock_embed)
    return store, mock_db, mock_embed


def _make_record(**overrides):
    """Create a KnowledgeRecord with sensible defaults."""
    defaults = {
        "id": uuid.uuid4(),
        "note_id": "test-note",
        "project_id": uuid.uuid4(),
        "title": "Test Note",
        "note_type": "decision",
        "status": "active",
        "content": "Test content",
    }
    defaults.update(overrides)
    return KnowledgeRecord(**defaults)


# =============================================================================
# 11.1: KnowledgeRecord dataclass
# =============================================================================


class TestKnowledgeRecord:
    """Tests for KnowledgeRecord dataclass."""

    def test_can_construct_empty(self):
        r = KnowledgeRecord()
        assert r.note_id == ""
        assert r.title == ""

    def test_status_defaults_to_active(self):
        r = KnowledgeRecord()
        assert r.status == "active"

    def test_tags_defaults_to_empty_list(self):
        r = KnowledgeRecord()
        assert r.tags == []

    def test_keywords_defaults_to_empty_list(self):
        r = KnowledgeRecord()
        assert r.keywords == []

    def test_retrieval_messages_defaults_to_empty_list(self):
        r = KnowledgeRecord()
        assert r.retrieval_messages == []

    def test_from_row_creates_record(self):
        row = {
            "id": uuid.uuid4(),
            "note_id": "my-note",
            "title": "My Note",
            "note_type": "decision",
            "status": "active",
            "content": "body text",
        }
        r = KnowledgeRecord.from_row(row)
        assert r.note_id == "my-note"
        assert r.title == "My Note"

    def test_from_row_handles_missing_keys(self):
        r = KnowledgeRecord.from_row({})
        assert r.note_id == ""
        assert r.status == "active"
        assert r.tags == []

    def test_from_row_none_tags_become_empty_list(self):
        r = KnowledgeRecord.from_row({"tags": None})
        assert r.tags == []

    def test_from_row_none_keywords_become_empty_list(self):
        r = KnowledgeRecord.from_row({"keywords": None})
        assert r.keywords == []

    def test_from_row_none_retrieval_messages_become_empty_list(self):
        r = KnowledgeRecord.from_row({"retrieval_messages": None})
        assert r.retrieval_messages == []


# =============================================================================
# 11.2: KnowledgeStore._content_hash()
# =============================================================================


class TestContentHash:
    """Tests for _content_hash static method."""

    def test_returns_sha256_hex(self):
        expected = hashlib.sha256("test".encode()).hexdigest()
        assert KnowledgeStore._content_hash("test") == expected

    def test_is_static_method(self):
        # Can be called without instance
        result = KnowledgeStore._content_hash("hello")
        assert isinstance(result, str)
        assert len(result) == 64

    def test_consistent_hashing(self):
        h1 = KnowledgeStore._content_hash("same")
        h2 = KnowledgeStore._content_hash("same")
        assert h1 == h2


# =============================================================================
# 11.3: KnowledgeStore._prepare_embedding()
# =============================================================================


class TestPrepareEmbedding:
    """Tests for _prepare_embedding static method."""

    def test_list_unchanged(self):
        data = [0.1, 0.2, 0.3]
        assert KnowledgeStore._prepare_embedding(data) is data

    def test_parses_string_format(self):
        result = KnowledgeStore._prepare_embedding("[0.1,0.2,0.3]")
        assert result == [0.1, 0.2, 0.3]

    def test_empty_string_brackets(self):
        result = KnowledgeStore._prepare_embedding("[]")
        assert result == []

    def test_other_iterable_fallback(self):
        result = KnowledgeStore._prepare_embedding(tuple([1.0, 2.0]))
        assert result == [1.0, 2.0]


# =============================================================================
# 11.4: upsert_note() — metadata-only update
# =============================================================================


class TestUpsertMetadataOnly:
    """Tests for upsert_note when content hash matches (metadata-only)."""

    @pytest.mark.asyncio
    async def test_skips_embedding_when_hash_matches(self):
        store, mock_db, mock_embed = _make_store()
        content = "existing content"
        content_hash = KnowledgeStore._content_hash(content)
        row_id = uuid.uuid4()

        mock_db.fetchval.side_effect = [content_hash, row_id]

        result = await store.upsert_note(
            note_id="n1",
            project_id=uuid.uuid4(),
            title="T",
            note_type="decision",
            content=content,
        )
        assert result == row_id
        mock_embed.embed.assert_not_called()

    @pytest.mark.asyncio
    async def test_updates_metadata_fields(self):
        store, mock_db, mock_embed = _make_store()
        content = "body"
        content_hash = KnowledgeStore._content_hash(content)
        row_id = uuid.uuid4()

        mock_db.fetchval.side_effect = [content_hash, row_id]

        await store.upsert_note(
            note_id="n1",
            project_id=uuid.uuid4(),
            title="Updated Title",
            note_type="learning",
            content=content,
            status="resolved",
            confidence="high",
            tags=["a"],
            keywords=["b"],
            retrieval_messages=["q"],
        )

        # Second fetchval call is the UPDATE
        update_call = mock_db.fetchval.call_args_list[1]
        query = update_call[0][0]
        assert "UPDATE knowledge_index" in query
        assert "RETURNING id" in query

    @pytest.mark.asyncio
    async def test_returns_row_uuid(self):
        store, mock_db, _ = _make_store()
        content = "body"
        row_id = uuid.uuid4()
        mock_db.fetchval.side_effect = [KnowledgeStore._content_hash(content), row_id]

        result = await store.upsert_note(
            note_id="n1",
            project_id=uuid.uuid4(),
            title="T",
            note_type="decision",
            content=content,
        )
        assert result == row_id


# =============================================================================
# 11.5: upsert_note() — content changed
# =============================================================================


class TestUpsertContentChanged:
    """Tests for upsert_note when content hash differs."""

    @pytest.mark.asyncio
    async def test_generates_embedding_when_hash_differs(self):
        store, mock_db, mock_embed = _make_store()
        mock_db.fetchval.side_effect = ["old-hash", uuid.uuid4()]

        await store.upsert_note(
            note_id="n1",
            project_id=uuid.uuid4(),
            title="T",
            note_type="decision",
            content="new content",
        )
        mock_embed.embed.assert_called_once()

    @pytest.mark.asyncio
    async def test_generates_embedding_when_no_existing(self):
        store, mock_db, mock_embed = _make_store()
        mock_db.fetchval.side_effect = [None, uuid.uuid4()]

        await store.upsert_note(
            note_id="n1",
            project_id=uuid.uuid4(),
            title="T",
            note_type="decision",
            content="content",
        )
        mock_embed.embed.assert_called_once()

    @pytest.mark.asyncio
    async def test_embed_text_includes_retrieval_messages(self):
        store, mock_db, mock_embed = _make_store()
        mock_db.fetchval.side_effect = [None, uuid.uuid4()]

        await store.upsert_note(
            note_id="n1",
            project_id=uuid.uuid4(),
            title="T",
            note_type="decision",
            content="body",
            retrieval_messages=["What is X?", "How does Y work?"],
        )
        embed_text = mock_embed.embed.call_args[0][0]
        assert "body" in embed_text
        assert "What is X?" in embed_text
        assert "How does Y work?" in embed_text

    @pytest.mark.asyncio
    async def test_embed_text_without_retrieval_messages(self):
        store, mock_db, mock_embed = _make_store()
        mock_db.fetchval.side_effect = [None, uuid.uuid4()]

        await store.upsert_note(
            note_id="n1",
            project_id=uuid.uuid4(),
            title="T",
            note_type="decision",
            content="just content",
        )
        embed_text = mock_embed.embed.call_args[0][0]
        assert embed_text == "just content"

    @pytest.mark.asyncio
    async def test_uses_on_conflict_upsert(self):
        store, mock_db, mock_embed = _make_store()
        mock_db.fetchval.side_effect = [None, uuid.uuid4()]

        await store.upsert_note(
            note_id="n1",
            project_id=uuid.uuid4(),
            title="T",
            note_type="decision",
            content="body",
        )
        # Second fetchval is the INSERT ... ON CONFLICT
        query = mock_db.fetchval.call_args_list[1][0][0]
        assert "ON CONFLICT (project_id, note_id) DO UPDATE" in query

    @pytest.mark.asyncio
    async def test_returns_row_uuid(self):
        store, mock_db, _ = _make_store()
        row_id = uuid.uuid4()
        mock_db.fetchval.side_effect = [None, row_id]

        result = await store.upsert_note(
            note_id="n1",
            project_id=uuid.uuid4(),
            title="T",
            note_type="decision",
            content="body",
        )
        assert result == row_id

    @pytest.mark.asyncio
    async def test_none_tags_normalized_to_empty_list(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.side_effect = [None, uuid.uuid4()]

        await store.upsert_note(
            note_id="n1",
            project_id=uuid.uuid4(),
            title="T",
            note_type="decision",
            content="body",
            tags=None,
            keywords=None,
            retrieval_messages=None,
        )
        # Should not raise — None normalized to []
        mock_db.fetchval.assert_called()


# =============================================================================
# 11.6: delete_note()
# =============================================================================


class TestDeleteNote:
    """Tests for delete_note() method."""

    @pytest.mark.asyncio
    async def test_returns_true_when_deleted(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = uuid.uuid4()
        assert await store.delete_note(uuid.uuid4(), "n1") is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = None
        assert await store.delete_note(uuid.uuid4(), "n1") is False


# =============================================================================
# 11.7: hybrid_search()
# =============================================================================


class TestHybridSearch:
    """Tests for hybrid_search() method."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_project(self):
        store, mock_db, _ = _make_store()
        result = await store.hybrid_search(query="test")
        assert result == []
        mock_db.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_project_uses_single_function(self):
        store, mock_db, _ = _make_store()
        mock_db.fetch.return_value = []

        await store.hybrid_search(project_id=uuid.uuid4(), query="test")
        query = mock_db.fetch.call_args[0][0]
        assert "knowledge_hybrid_search" in query

    @pytest.mark.asyncio
    async def test_multi_project_uses_multi_function(self):
        store, mock_db, _ = _make_store()
        mock_db.fetch.return_value = []

        await store.hybrid_search(
            project_ids=[uuid.uuid4(), uuid.uuid4()], query="test"
        )
        query = mock_db.fetch.call_args[0][0]
        assert "knowledge_multi_project_hybrid_search" in query

    @pytest.mark.asyncio
    async def test_project_ids_takes_priority(self):
        store, mock_db, _ = _make_store()
        mock_db.fetch.return_value = []

        pid = uuid.uuid4()
        pids = [uuid.uuid4(), uuid.uuid4()]
        await store.hybrid_search(project_id=pid, project_ids=pids, query="test")
        query = mock_db.fetch.call_args[0][0]
        assert "multi_project" in query

    @pytest.mark.asyncio
    async def test_calls_embed_for_query(self):
        store, mock_db, mock_embed = _make_store()
        mock_db.fetch.return_value = []

        await store.hybrid_search(project_id=uuid.uuid4(), query="search term")
        mock_embed.embed.assert_called_once_with("search term")

    @pytest.mark.asyncio
    async def test_returns_knowledge_records(self):
        store, mock_db, _ = _make_store()
        mock_row = MagicMock()
        mock_row.__iter__ = MagicMock(return_value=iter([]))
        # dict(row) must work
        mock_row_dict = {
            "note_id": "n1",
            "title": "A",
            "note_type": "decision",
            "status": "active",
            "content": "body",
        }
        mock_db.fetch.return_value = [mock_row_dict]

        result = await store.hybrid_search(project_id=uuid.uuid4(), query="q")
        assert len(result) == 1
        assert isinstance(result[0], KnowledgeRecord)
        assert result[0].note_id == "n1"

    @pytest.mark.asyncio
    async def test_default_weights(self):
        store, mock_db, _ = _make_store()
        mock_db.fetch.return_value = []

        await store.hybrid_search(project_id=uuid.uuid4(), query="q")
        call_args = mock_db.fetch.call_args[0]
        # Args: sql, query, embedding, scope, match_count, dense, sparse, recency
        assert call_args[5] == 0.6  # dense
        assert call_args[6] == 0.3  # sparse
        assert call_args[7] == 0.1  # recency


# =============================================================================
# 11.8: get_summary()
# =============================================================================


class TestGetSummary:
    """Tests for get_summary() method."""

    @pytest.mark.asyncio
    async def test_returns_zero_total_when_no_project(self):
        store, mock_db, _ = _make_store()
        result = await store.get_summary()
        assert result == {"total": 0}

    @pytest.mark.asyncio
    async def test_single_project_uses_equals_clause(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchrow.return_value = {"total": 5}
        mock_db.fetch.return_value = []

        await store.get_summary(project_id=uuid.uuid4())
        query = mock_db.fetchrow.call_args[0][0]
        assert "project_id = $1" in query

    @pytest.mark.asyncio
    async def test_multi_project_uses_any_clause(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchrow.return_value = {"total": 10}
        mock_db.fetch.return_value = []

        await store.get_summary(project_ids=[uuid.uuid4(), uuid.uuid4()])
        query = mock_db.fetchrow.call_args[0][0]
        assert "project_id = ANY($1)" in query

    @pytest.mark.asyncio
    async def test_returns_expected_count_fields(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchrow.return_value = {
            "total": 10,
            "active": 8,
            "decisions": 3,
            "learnings": 2,
            "open_questions": 1,
            "goals": 1,
            "code_notes": 2,
            "state_notes": 1,
            "last_modified": None,
        }
        mock_db.fetch.return_value = []

        result = await store.get_summary(project_id=uuid.uuid4())
        assert result["total"] == 10
        assert result["active"] == 8
        assert result["decisions"] == 3

    @pytest.mark.asyncio
    async def test_includes_recent_notes(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchrow.return_value = {"total": 2}
        mock_db.fetch.return_value = [
            {
                "note_id": "n1",
                "title": "A",
                "note_type": "decision",
                "status": "active",
                "modified_at": None,
            },
        ]

        result = await store.get_summary(project_id=uuid.uuid4())
        assert "recent_notes" in result
        assert len(result["recent_notes"]) == 1


# =============================================================================
# 11.9: rebuild_from_notes()
# =============================================================================


class TestRebuildFromNotes:
    """Tests for rebuild_from_notes() method."""

    @pytest.mark.asyncio
    async def test_deletes_existing_entries_first(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = uuid.uuid4()
        pid = uuid.uuid4()

        await store.rebuild_from_notes(pid, [])
        mock_db.execute.assert_called_once()
        query = mock_db.execute.call_args[0][0]
        assert "DELETE FROM knowledge_index" in query

    @pytest.mark.asyncio
    async def test_upserts_each_note(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = uuid.uuid4()
        pid = uuid.uuid4()

        notes = [
            {"id": "n1", "title": "A", "type": "decision", "content": "body1"},
            {"id": "n2", "title": "B", "type": "learning", "content": "body2"},
        ]
        count = await store.rebuild_from_notes(pid, notes)
        assert count == 2

    @pytest.mark.asyncio
    async def test_handles_neo4j_datetime(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = uuid.uuid4()

        mock_dt = MagicMock()
        mock_dt.to_native.return_value = "2026-01-01T00:00:00"

        notes = [
            {
                "id": "n1",
                "type": "decision",
                "content": "body",
                "created": mock_dt,
                "modified": mock_dt,
            }
        ]
        count = await store.rebuild_from_notes(uuid.uuid4(), notes)
        assert count == 1
        mock_dt.to_native.assert_called()

    @pytest.mark.asyncio
    async def test_converts_string_job_id(self):
        store, mock_db, _ = _make_store()
        mock_db.fetchval.return_value = uuid.uuid4()
        job_uuid = str(uuid.uuid4())

        notes = [{"id": "n1", "type": "decision", "content": "x", "job_id": job_uuid}]
        count = await store.rebuild_from_notes(uuid.uuid4(), notes)
        assert count == 1

    @pytest.mark.asyncio
    async def test_individual_failure_is_non_fatal(self):
        store, mock_db, _ = _make_store()
        # First note succeeds, second fails, third succeeds
        mock_db.fetchval.side_effect = [
            uuid.uuid4(),  # hash check for n1 (content changed)
            uuid.uuid4(),  # upsert result for n1
            Exception("bad data"),  # n2 fails
            uuid.uuid4(),  # hash check for n3
            uuid.uuid4(),  # upsert result for n3
        ]

        notes = [
            {"id": "n1", "type": "decision", "content": "ok1"},
            {"id": "n2", "type": "decision", "content": "bad"},
            {"id": "n3", "type": "decision", "content": "ok3"},
        ]
        count = await store.rebuild_from_notes(uuid.uuid4(), notes)
        assert count == 2  # n1 + n3

    @pytest.mark.asyncio
    async def test_returns_zero_for_empty_notes(self):
        store, mock_db, _ = _make_store()
        count = await store.rebuild_from_notes(uuid.uuid4(), [])
        assert count == 0


# =============================================================================
# 11.10: format_note() (static)
# =============================================================================


class TestFormatNote:
    """Tests for format_note() static method."""

    def test_includes_note_type(self):
        note = _make_record(note_type="decision")
        result = KnowledgeStore.format_note(note, 1)
        assert "decision" in result

    def test_includes_confidence_when_present(self):
        note = _make_record(confidence="high")
        result = KnowledgeStore.format_note(note, 1)
        assert "high confidence" in result

    def test_excludes_confidence_when_absent(self):
        note = _make_record(confidence=None)
        result = KnowledgeStore.format_note(note, 1)
        assert "confidence" not in result

    def test_includes_phase_when_not_none(self):
        note = _make_record(phase=3)
        result = KnowledgeStore.format_note(note, 1)
        assert "phase 3" in result

    def test_excludes_phase_when_none(self):
        note = _make_record(phase=None)
        result = KnowledgeStore.format_note(note, 1)
        assert "phase" not in result

    def test_includes_tags_when_present(self):
        note = _make_record(tags=["auth", "security"])
        result = KnowledgeStore.format_note(note, 1)
        assert "Tags: auth, security" in result

    def test_excludes_tags_when_empty(self):
        note = _make_record(tags=[])
        result = KnowledgeStore.format_note(note, 1)
        assert "Tags:" not in result

    def test_truncates_long_content(self):
        note = _make_record(content="x" * 600)
        result = KnowledgeStore.format_note(note, 1)
        assert result.endswith("...")
        # Content part should be 497 + 3 = 500 chars
        content_line = result.split("\n", 1)[1]
        assert len(content_line) == 500

    def test_does_not_truncate_short_content(self):
        note = _make_record(content="short")
        result = KnowledgeStore.format_note(note, 1)
        assert "..." not in result

    def test_uses_1_based_index(self):
        note = _make_record()
        result = KnowledgeStore.format_note(note, 5)
        assert result.startswith("[5]")


# =============================================================================
# 11.11: assemble_knowledge_block() (classmethod)
# =============================================================================


class TestAssembleKnowledgeBlock:
    """Tests for assemble_knowledge_block() classmethod."""

    def test_returns_empty_string_for_empty_list(self):
        assert KnowledgeStore.assemble_knowledge_block([]) == ""

    def test_wraps_with_header_and_footer(self):
        notes = [_make_record(content="body")]
        result = KnowledgeStore.assemble_knowledge_block(notes)
        assert result.startswith("--- Project Knowledge ---")
        assert "--- End Knowledge" in result

    def test_footer_includes_note_count(self):
        notes = [_make_record(content="a"), _make_record(content="b")]
        result = KnowledgeStore.assemble_knowledge_block(notes)
        assert "2 notes" in result

    def test_footer_includes_token_estimate(self):
        notes = [_make_record(content="a" * 400)]
        result = KnowledgeStore.assemble_knowledge_block(notes)
        # 400 // 4 = 100 tokens
        assert "100 tokens" in result

    def test_each_note_formatted_with_1_based_index(self):
        notes = [_make_record(content="first"), _make_record(content="second")]
        result = KnowledgeStore.assemble_knowledge_block(notes)
        assert "[1]" in result
        assert "[2]" in result
