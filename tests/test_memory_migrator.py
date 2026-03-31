"""Tests for src/tools/knowledge/memory_migrator.py.

Covers section 15 of persistent_agent_tests.md:
  15.1 _MEMORY_TYPE_MAP constant
  15.2 _map_todo_completion()
  15.3 _map_memory_type()
  15.4 _generate_tags()
  15.5 _generate_title()
  15.6 _is_duplicate()
  15.7 migrate_memories()
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.tools.knowledge.memory_migrator import (
    _DUPLICATE_THRESHOLD,
    _MEMORY_TYPE_MAP,
    _generate_tags,
    _generate_title,
    _is_duplicate,
    _map_memory_type,
    _map_todo_completion,
    migrate_memories,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_memory(**overrides):
    """Create a minimal memory row dict."""
    base = {
        "id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "memory_type": "observer",
        "content": "Some memory content here",
        "importance": 0.7,
        "keywords": None,
        "tags": None,
        "phase": None,
        "created_at": None,
    }
    base.update(overrides)
    return base


# =============================================================================
# 15.1: _MEMORY_TYPE_MAP constant
# =============================================================================


class TestMemoryTypeMap:
    """Tests for the _MEMORY_TYPE_MAP constant."""

    def test_observer_maps_to_learning(self):
        assert _MEMORY_TYPE_MAP["observer"] == "learning"

    def test_compaction_summary_maps_to_state(self):
        assert _MEMORY_TYPE_MAP["compaction_summary"] == "state"

    def test_phase_archive_maps_to_retrospective(self):
        assert _MEMORY_TYPE_MAP["phase_archive"] == "retrospective"

    def test_tool_error_maps_to_learning(self):
        assert _MEMORY_TYPE_MAP["tool_error"] == "learning"

    def test_free_maps_to_learning(self):
        assert _MEMORY_TYPE_MAP["free"] == "learning"


# =============================================================================
# 15.2: _map_todo_completion()
# =============================================================================


class TestMapTodoCompletion:
    """Tests for _map_todo_completion()."""

    def test_code_keyword(self):
        assert _map_todo_completion("Wrote code for the feature") == "code"

    def test_implementation_keyword(self):
        assert _map_todo_completion("Finished implementation") == "code"

    def test_module_keyword(self):
        assert _map_todo_completion("Added new module") == "code"

    def test_class_keyword(self):
        assert _map_todo_completion("Created class UserManager") == "code"

    def test_function_keyword(self):
        assert _map_todo_completion("Refactored function parse") == "code"

    def test_method_keyword(self):
        assert _map_todo_completion("Updated method signature") == "code"

    def test_refactor_keyword(self):
        assert _map_todo_completion("Major refactor complete") == "code"

    def test_bug_keyword(self):
        assert _map_todo_completion("Fixed bug in parser") == "code"

    def test_patch_keyword(self):
        assert _map_todo_completion("Applied patch for security") == "code"

    def test_no_code_keywords_returns_learning(self):
        assert _map_todo_completion("Reviewed the documentation") == "learning"

    def test_case_insensitive(self):
        assert _map_todo_completion("IMPLEMENTATION details") == "code"


# =============================================================================
# 15.3: _map_memory_type()
# =============================================================================


class TestMapMemoryType:
    """Tests for _map_memory_type()."""

    def test_todo_completion_delegates_to_map_todo(self):
        assert _map_memory_type("todo_completion", "Wrote code") == "code"

    def test_todo_completion_no_code_keywords(self):
        assert _map_memory_type("todo_completion", "Reviewed docs") == "learning"

    def test_observer_from_map(self):
        assert _map_memory_type("observer", "any content") == "learning"

    def test_compaction_summary_from_map(self):
        assert _map_memory_type("compaction_summary", "any content") == "state"

    def test_phase_archive_from_map(self):
        assert _map_memory_type("phase_archive", "any content") == "retrospective"

    def test_tool_error_from_map(self):
        assert _map_memory_type("tool_error", "any content") == "learning"

    def test_free_from_map(self):
        assert _map_memory_type("free", "any content") == "learning"

    def test_unknown_type_defaults_to_learning(self):
        assert _map_memory_type("unknown_type", "any content") == "learning"

    def test_empty_type_defaults_to_learning(self):
        assert _map_memory_type("", "any content") == "learning"


# =============================================================================
# 15.4: _generate_tags()
# =============================================================================


class TestGenerateTags:
    """Tests for _generate_tags()."""

    def test_uses_keywords_field(self):
        memory = _make_memory(keywords=["python", "async"])
        tags = _generate_tags(memory)
        assert "python" in tags
        assert "async" in tags

    def test_uses_tags_field_fallback(self):
        memory = _make_memory(keywords=None, tags=["react", "frontend"])
        tags = _generate_tags(memory)
        assert "react" in tags
        assert "frontend" in tags

    def test_handles_string_keywords_comma_separated(self):
        memory = _make_memory(keywords="python, async, websocket")
        tags = _generate_tags(memory)
        assert "python" in tags
        assert "async" in tags
        assert "websocket" in tags

    def test_lowercases_tags(self):
        memory = _make_memory(keywords=["Python", "ASYNC"])
        tags = _generate_tags(memory)
        assert "python" in tags
        assert "async" in tags

    def test_strips_whitespace(self):
        memory = _make_memory(keywords="  python , async  ")
        tags = _generate_tags(memory)
        assert "python" in tags
        assert "async" in tags

    def test_no_duplicate_tags(self):
        memory = _make_memory(keywords=["python", "python", "Python"])
        tags = _generate_tags(memory)
        assert tags.count("python") == 1

    def test_appends_migrated_from_memory_tag(self):
        memory = _make_memory()
        tags = _generate_tags(memory)
        assert "migrated-from-memory" in tags

    def test_appends_error_solution_for_tool_error(self):
        memory = _make_memory(memory_type="tool_error")
        tags = _generate_tags(memory)
        assert "error-solution" in tags

    def test_no_error_solution_for_non_tool_error(self):
        memory = _make_memory(memory_type="observer")
        tags = _generate_tags(memory)
        assert "error-solution" not in tags

    def test_appends_memory_type_tag(self):
        memory = _make_memory(memory_type="observer")
        tags = _generate_tags(memory)
        assert "memory-observer" in tags

    def test_memory_type_tag_for_tool_error(self):
        memory = _make_memory(memory_type="tool_error")
        tags = _generate_tags(memory)
        assert "memory-tool_error" in tags

    def test_no_keywords_no_tags(self):
        memory = _make_memory(keywords=None, tags=None)
        tags = _generate_tags(memory)
        # Should still have provenance tags
        assert "migrated-from-memory" in tags


# =============================================================================
# 15.5: _generate_title()
# =============================================================================


class TestGenerateTitle:
    """Tests for _generate_title()."""

    def test_first_nonempty_line(self):
        memory = _make_memory(content="First line\nSecond line")
        assert _generate_title(memory) == "First line"

    def test_skips_empty_lines(self):
        memory = _make_memory(content="\n\n  \nActual title\nBody")
        assert _generate_title(memory) == "Actual title"

    def test_strips_markdown_heading_markers(self):
        memory = _make_memory(content="## My Heading\nBody text")
        assert _generate_title(memory) == "My Heading"

    def test_strips_h1_marker(self):
        memory = _make_memory(content="# Top Level\nBody")
        assert _generate_title(memory) == "Top Level"

    def test_strips_h3_marker(self):
        memory = _make_memory(content="### Sub Heading\nBody")
        assert _generate_title(memory) == "Sub Heading"

    def test_truncates_long_titles(self):
        long_line = "A" * 100
        memory = _make_memory(content=long_line)
        title = _generate_title(memory)
        assert len(title) == 80
        assert title.endswith("...")
        assert title == "A" * 77 + "..."

    def test_no_truncation_at_80(self):
        line = "B" * 80
        memory = _make_memory(content=line)
        title = _generate_title(memory)
        assert len(title) == 80
        assert title == "B" * 80

    def test_no_truncation_under_80(self):
        line = "C" * 50
        memory = _make_memory(content=line)
        assert _generate_title(memory) == line

    def test_fallback_for_empty_content(self):
        mid = uuid.uuid4()
        memory = _make_memory(content="", memory_type="observer", id=mid)
        title = _generate_title(memory)
        assert title == f"observer {str(mid)[:8]}"

    def test_fallback_for_none_content(self):
        mid = uuid.uuid4()
        memory = _make_memory(content=None, memory_type="free", id=mid)
        title = _generate_title(memory)
        assert title == f"free {str(mid)[:8]}"

    def test_fallback_for_whitespace_content(self):
        memory = _make_memory(content="   \n\n  ", memory_type="observer")
        title = _generate_title(memory)
        assert "observer" in title


# =============================================================================
# 15.6: _is_duplicate()
# =============================================================================


class TestIsDuplicate:
    """Tests for _is_duplicate()."""

    @pytest.mark.asyncio
    async def test_false_when_no_results(self):
        ks = AsyncMock()
        ks.hybrid_search.return_value = []
        result = await _is_duplicate("some content", str(uuid.uuid4()), ks)
        assert result is False

    @pytest.mark.asyncio
    async def test_true_when_overlap_above_threshold(self):
        # Create content where overlap is >= 0.92
        words = " ".join(f"word{i}" for i in range(100))
        top_result = MagicMock()
        top_result.content = words  # Exact match
        ks = AsyncMock()
        ks.hybrid_search.return_value = [top_result]
        pid = str(uuid.uuid4())
        result = await _is_duplicate(words, pid, ks)
        assert result is True

    @pytest.mark.asyncio
    async def test_false_when_overlap_below_threshold(self):
        top_result = MagicMock()
        top_result.content = "completely different content here"
        ks = AsyncMock()
        ks.hybrid_search.return_value = [top_result]
        result = await _is_duplicate("original unique text", str(uuid.uuid4()), ks)
        assert result is False

    @pytest.mark.asyncio
    async def test_false_on_exception(self):
        ks = AsyncMock()
        ks.hybrid_search.side_effect = Exception("search failed")
        result = await _is_duplicate("some content", str(uuid.uuid4()), ks)
        assert result is False

    @pytest.mark.asyncio
    async def test_false_when_query_words_empty(self):
        # Empty string → empty word set
        top_result = MagicMock()
        top_result.content = "some result"
        ks = AsyncMock()
        ks.hybrid_search.return_value = [top_result]
        result = await _is_duplicate("", str(uuid.uuid4()), ks)
        assert result is False

    @pytest.mark.asyncio
    async def test_uses_content_truncated_to_500(self):
        long_content = "word " * 200  # 1000 chars
        ks = AsyncMock()
        ks.hybrid_search.return_value = []
        pid = str(uuid.uuid4())
        await _is_duplicate(long_content, pid, ks)
        call_kwargs = ks.hybrid_search.call_args[1]
        query = call_kwargs["query"]
        assert len(query) <= 500

    @pytest.mark.asyncio
    async def test_threshold_value(self):
        assert _DUPLICATE_THRESHOLD == 0.92


# =============================================================================
# 15.7: migrate_memories()
# =============================================================================


class TestMigrateMemories:
    """Tests for migrate_memories()."""

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.fetch.return_value = []
        return db

    @pytest.fixture
    def mock_kg(self):
        kg = MagicMock()
        kg.create_note.return_value = "test-slug"
        return kg

    @pytest.fixture
    def mock_ks(self):
        ks = AsyncMock()
        ks.hybrid_search.return_value = []
        return ks

    @pytest.fixture
    def mock_embed(self):
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_initial_stats(self, mock_db, mock_kg, mock_ks, mock_embed):
        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        assert stats == {"migrated": 0, "skipped": 0, "duplicates": 0, "errors": 0}

    @pytest.mark.asyncio
    async def test_returns_early_with_zeros_when_no_memories(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        mock_db.fetch.return_value = []
        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        assert stats["migrated"] == 0
        mock_kg.create_note.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_empty_content(self, mock_db, mock_kg, mock_ks, mock_embed):
        row = _make_memory(content="   \n  ")
        mock_db.fetch.return_value = [row]
        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        assert stats["skipped"] == 1

    @pytest.mark.asyncio
    async def test_maps_memory_type(self, mock_db, mock_kg, mock_ks, mock_embed):
        row = _make_memory(memory_type="phase_archive", content="Phase review")
        mock_db.fetch.return_value = [row]
        await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["note_type"] == "retrospective"

    @pytest.mark.asyncio
    async def test_importance_high_confidence(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        row = _make_memory(importance=0.9)
        mock_db.fetch.return_value = [row]
        await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["confidence"] == "high"

    @pytest.mark.asyncio
    async def test_importance_medium_confidence(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        row = _make_memory(importance=0.6)
        mock_db.fetch.return_value = [row]
        await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["confidence"] == "medium"

    @pytest.mark.asyncio
    async def test_importance_low_confidence(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        row = _make_memory(importance=0.3)
        mock_db.fetch.return_value = [row]
        await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["confidence"] == "low"

    @pytest.mark.asyncio
    async def test_importance_none_confidence(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        row = _make_memory(importance=None)
        mock_db.fetch.return_value = [row]
        await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["confidence"] is None

    @pytest.mark.asyncio
    async def test_duplicate_increments_count(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        row = _make_memory(content="duplicate content here more words")
        mock_db.fetch.return_value = [row]
        # Make _is_duplicate return True
        top_result = MagicMock()
        top_result.content = "duplicate content here more words"
        mock_ks.hybrid_search.return_value = [top_result]
        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        assert stats["duplicates"] == 1
        mock_kg.create_note.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_check_exception_nonfatal(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        row = _make_memory(content="test content here")
        mock_db.fetch.return_value = [row]
        # hybrid_search raises, but _is_duplicate catches it → returns False
        # The outer try/except in migrate_memories also catches
        mock_ks.hybrid_search.side_effect = Exception("search down")
        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        # Should proceed to write since duplicate check failed non-fatally
        assert stats["migrated"] == 1

    @pytest.mark.asyncio
    async def test_dry_run_does_not_write(self, mock_db, mock_kg, mock_ks, mock_embed):
        row = _make_memory(content="Test memory content")
        mock_db.fetch.return_value = [row]
        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
            dry_run=True,
        )
        assert stats["migrated"] == 1
        mock_kg.create_note.assert_not_called()
        mock_ks.upsert_note.assert_not_called()

    @pytest.mark.asyncio
    async def test_writes_to_neo4j_first(self, mock_db, mock_kg, mock_ks, mock_embed):
        row = _make_memory(content="Test memory content")
        mock_db.fetch.return_value = [row]
        await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        mock_kg.create_note.assert_called_once()

    @pytest.mark.asyncio
    async def test_writes_through_to_pgvector(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        row = _make_memory(content="Test memory content")
        mock_db.fetch.return_value = [row]
        await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        mock_ks.upsert_note.assert_called_once()

    @pytest.mark.asyncio
    async def test_pgvector_failure_nonfatal(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        row = _make_memory(content="Test memory content")
        mock_db.fetch.return_value = [row]
        mock_ks.upsert_note.side_effect = Exception("pgvector down")
        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        assert stats["migrated"] == 1

    @pytest.mark.asyncio
    async def test_neo4j_failure_increments_errors(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        row = _make_memory(content="Test memory content")
        mock_db.fetch.return_value = [row]
        mock_kg.create_note.side_effect = Exception("Neo4j down")
        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        assert stats["errors"] == 1

    @pytest.mark.asyncio
    async def test_returns_final_stats(self, mock_db, mock_kg, mock_ks, mock_embed):
        rows = [
            _make_memory(content="Valid content one"),
            _make_memory(content="   "),  # skipped
            _make_memory(content="Valid content two"),
        ]
        mock_db.fetch.return_value = rows
        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        assert stats["migrated"] == 2
        assert stats["skipped"] == 1

    @pytest.mark.asyncio
    async def test_importance_boundary_08(self, mock_db, mock_kg, mock_ks, mock_embed):
        """Exactly 0.8 should be 'high'."""
        row = _make_memory(importance=0.8)
        mock_db.fetch.return_value = [row]
        await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["confidence"] == "high"

    @pytest.mark.asyncio
    async def test_importance_boundary_05(self, mock_db, mock_kg, mock_ks, mock_embed):
        """Exactly 0.5 should be 'medium'."""
        row = _make_memory(importance=0.5)
        mock_db.fetch.return_value = [row]
        await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["confidence"] == "medium"

    @pytest.mark.asyncio
    async def test_importance_boundary_049(self, mock_db, mock_kg, mock_ks, mock_embed):
        """Below 0.5 should be 'low'."""
        row = _make_memory(importance=0.49)
        mock_db.fetch.return_value = [row]
        await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        call_kwargs = mock_kg.create_note.call_args[1]
        assert call_kwargs["confidence"] == "low"

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_errors(
        self, mock_db, mock_kg, mock_ks, mock_embed
    ):
        mock_db.fetch.side_effect = Exception("DB connection failed")
        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_embed,
        )
        assert stats["errors"] == 1
        assert stats["migrated"] == 0
