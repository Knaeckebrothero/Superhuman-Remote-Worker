"""Unit tests for project knowledge base Phase 3 deliverables.

Tests the orchestrator API endpoints (via direct function calls),
workspace converter, memory migrator, and Angular model contracts.
"""

import asyncio
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


# =========================================================================
# Workspace Converter Tests
# =========================================================================

class TestWorkspaceConverter:
    """Tests for workspace.md → knowledge notes conversion."""

    def test_convert_empty_content(self):
        from src.tools.knowledge.workspace_converter import convert_workspace
        assert convert_workspace("") == []
        assert convert_workspace("   \n  ") == []

    def test_convert_no_headings(self):
        from src.tools.knowledge.workspace_converter import convert_workspace
        notes = convert_workspace("Some text without any headings at all.")
        assert len(notes) == 1
        assert notes[0]["title"] == "Workspace Notes"
        assert "Some text without any headings" in notes[0]["content"]

    def test_convert_single_section(self):
        from src.tools.knowledge.workspace_converter import convert_workspace

        content = "## Authentication Decision\nWe chose JWT over OAuth for simplicity."
        notes = convert_workspace(content)
        assert len(notes) == 1
        assert notes[0]["title"] == "Authentication Decision"
        assert notes[0]["note_type"] == "decision"
        assert notes[0]["slug"] == "authentication-decision"
        assert "JWT" in notes[0]["content"] or "chose" in notes[0]["content"]

    def test_convert_multiple_sections(self):
        from src.tools.knowledge.workspace_converter import convert_workspace

        content = """## Architecture Decision
We chose PostgreSQL for the database.

## Current Status
The migration is in progress. Everything is on track.

## Open Questions
We need to investigate caching strategy.
"""
        notes = convert_workspace(content)
        assert len(notes) == 3

        types = {n["note_type"] for n in notes}
        assert "decision" in types
        assert "state" in types
        assert "question" in types

    def test_convert_with_job_id(self):
        from src.tools.knowledge.workspace_converter import convert_workspace

        content = "## Some Finding\nWe discovered a performance issue."
        notes = convert_workspace(content, job_id="test-job-123", config_name="developer")
        assert len(notes) == 1
        assert "migrated-from-workspace" in notes[0]["tags"]
        assert "agent-developer" in notes[0]["tags"]

    def test_cross_references_detected(self):
        from src.tools.knowledge.workspace_converter import convert_workspace

        content = """## Database Choice
We selected PostgreSQL for ACID compliance.

## Performance Analysis
Based on the Database Choice, we benchmarked queries.
"""
        notes = convert_workspace(content)
        assert len(notes) == 2

        # The "Performance Analysis" note should reference "Database Choice"
        perf_note = next(n for n in notes if n["slug"] == "performance-analysis")
        assert len(perf_note["links"]) > 0
        assert perf_note["links"][0]["target"] == "database-choice"
        assert perf_note["links"][0]["type"] == "REFERENCES"

    def test_slug_collision_handling(self):
        from src.tools.knowledge.workspace_converter import convert_workspace

        content = """## Status
First status section.

## Status
Second status section with same heading.
"""
        notes = convert_workspace(content)
        assert len(notes) == 2
        slugs = {n["slug"] for n in notes}
        assert len(slugs) == 2  # Should have unique slugs

    def test_tag_extraction(self):
        from src.tools.knowledge.workspace_converter import convert_workspace

        content = "## Code Module\nImplemented `AuthController` using JWT tokens via Express."
        notes = convert_workspace(content)
        assert len(notes) == 1
        tags = notes[0]["tags"]
        # Should extract code references and significant words
        assert any("authcontroller" in t for t in tags)

    @pytest.mark.asyncio
    async def test_convert_and_write(self):
        from src.tools.knowledge.workspace_converter import convert_and_write

        mock_kg = MagicMock()
        mock_kg.create_note = MagicMock(return_value="test-slug")
        mock_ks = AsyncMock()
        mock_ks.upsert_note = AsyncMock()

        content = "## Test Decision\nWe chose option A."
        count = await convert_and_write(
            content=content,
            project_id=str(uuid.uuid4()),
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            job_id=str(uuid.uuid4()),
        )

        assert count == 1
        mock_kg.create_note.assert_called_once()
        mock_ks.upsert_note.assert_called_once()


# =========================================================================
# Memory Migrator Tests
# =========================================================================

class TestMemoryMigrator:
    """Tests for Memory Light → knowledge notes migration."""

    def test_map_memory_types(self):
        from src.tools.knowledge.memory_migrator import _map_memory_type

        assert _map_memory_type("observer", "some content") == "learning"
        assert _map_memory_type("compaction_summary", "some content") == "state"
        assert _map_memory_type("phase_archive", "some content") == "retrospective"
        assert _map_memory_type("tool_error", "some content") == "learning"
        assert _map_memory_type("free", "some content") == "learning"
        assert _map_memory_type("unknown_type", "some content") == "learning"

    def test_map_todo_completion_code(self):
        from src.tools.knowledge.memory_migrator import _map_memory_type

        assert _map_memory_type("todo_completion", "Implemented the AuthController class") == "code"
        assert _map_memory_type("todo_completion", "Wrote documentation for users") == "learning"

    def test_generate_tags(self):
        from src.tools.knowledge.memory_migrator import _generate_tags

        memory = {
            "memory_type": "tool_error",
            "keywords": ["jwt", "auth"],
        }
        tags = _generate_tags(memory)
        assert "jwt" in tags
        assert "auth" in tags
        assert "migrated-from-memory" in tags
        assert "error-solution" in tags
        assert "memory-tool_error" in tags

    def test_generate_title(self):
        from src.tools.knowledge.memory_migrator import _generate_title

        memory = {"content": "JWT tokens expire after 24 hours\nMore details here."}
        title = _generate_title(memory)
        assert title == "JWT tokens expire after 24 hours"

    def test_generate_title_long_content(self):
        from src.tools.knowledge.memory_migrator import _generate_title

        memory = {"content": "A" * 100}
        title = _generate_title(memory)
        assert len(title) <= 80
        assert title.endswith("...")

    def test_generate_title_empty(self):
        from src.tools.knowledge.memory_migrator import _generate_title

        memory = {"content": "", "memory_type": "observer", "id": "abc12345-xxx"}
        title = _generate_title(memory)
        assert "observer" in title

    def test_slugify(self):
        from src.tools.knowledge.memory_migrator import _slugify

        assert _slugify("Hello World!") == "hello-world"
        assert _slugify("  Multiple   Spaces  ") == "multiple-spaces"
        assert _slugify("Special @#$ Characters") == "special-characters"

    @pytest.mark.asyncio
    async def test_migrate_memories_dry_run(self):
        from src.tools.knowledge.memory_migrator import migrate_memories

        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[
            {
                "id": uuid.uuid4(),
                "job_id": uuid.uuid4(),
                "content": "Important finding about caching.",
                "memory_type": "observer",
                "importance": 0.8,
                "keywords": ["caching"],
                "created_at": None,
                "phase": 2,
            },
            {
                "id": uuid.uuid4(),
                "job_id": uuid.uuid4(),
                "content": "",  # Empty — should be skipped
                "memory_type": "free",
                "importance": 0.5,
                "keywords": [],
                "created_at": None,
                "phase": None,
            },
        ])

        mock_kg = MagicMock()
        mock_ks = AsyncMock()
        mock_ks.hybrid_search = AsyncMock(return_value=[])  # No duplicates
        mock_es = AsyncMock()

        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_es,
            dry_run=True,
        )

        assert stats["migrated"] == 1  # One non-empty memory
        assert stats["skipped"] == 1   # One empty content
        mock_kg.create_note.assert_not_called()  # Dry run

    @pytest.mark.asyncio
    async def test_migrate_memories_write(self):
        from src.tools.knowledge.memory_migrator import migrate_memories

        job_id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[
            {
                "id": uuid.uuid4(),
                "job_id": job_id,
                "content": "Discovered that GIN indexes improve JSONB query performance by 60x.",
                "memory_type": "observer",
                "importance": 0.9,
                "keywords": ["postgresql", "gin", "performance"],
                "created_at": None,
                "phase": 3,
            },
        ])

        mock_kg = MagicMock()
        mock_kg.create_note = MagicMock(return_value="gin-indexes-improve-jsonb")
        mock_ks = AsyncMock()
        mock_ks.hybrid_search = AsyncMock(return_value=[])
        mock_ks.upsert_note = AsyncMock()
        mock_es = AsyncMock()

        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=mock_kg,
            knowledge_store=mock_ks,
            embedding_service=mock_es,
            dry_run=False,
        )

        assert stats["migrated"] == 1
        assert stats["errors"] == 0
        mock_kg.create_note.assert_called_once()
        mock_ks.upsert_note.assert_called_once()

        # Verify the note was created with correct type and confidence
        call_kwargs = mock_kg.create_note.call_args
        assert call_kwargs.kwargs.get("note_type") == "learning"
        assert call_kwargs.kwargs.get("confidence") == "high"  # importance 0.9

    @pytest.mark.asyncio
    async def test_migrate_no_memories(self):
        from src.tools.knowledge.memory_migrator import migrate_memories

        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[])

        stats = await migrate_memories(
            project_id=str(uuid.uuid4()),
            db=mock_db,
            knowledge_graph=MagicMock(),
            knowledge_store=AsyncMock(),
            embedding_service=AsyncMock(),
        )

        assert stats["migrated"] == 0
        assert stats["skipped"] == 0


# =========================================================================
# Orchestrator Knowledge API Tests (model/contract tests)
# =========================================================================

class TestKnowledgeAPIModels:
    """Tests for the Pydantic models used by knowledge API endpoints."""

    def test_search_request_model(self):
        # Import from orchestrator
        sys.path.insert(0, str(project_root / "orchestrator"))
        try:
            from main import KnowledgeSearchRequest
            req = KnowledgeSearchRequest(query="test query", limit=5)
            assert req.query == "test query"
            assert req.limit == 5
        finally:
            sys.path.pop(0)

    def test_note_update_model(self):
        sys.path.insert(0, str(project_root / "orchestrator"))
        try:
            from main import KnowledgeNoteUpdate
            update = KnowledgeNoteUpdate(status="superseded", add_tags=["deprecated"])
            assert update.status == "superseded"
            assert update.add_tags == ["deprecated"]
            assert update.remove_tags is None
        finally:
            sys.path.pop(0)


# =========================================================================
# Phase 3 Integration Smoke Tests
# =========================================================================

class TestPhase3Integration:
    """Smoke tests verifying the Phase 3 components wire together."""

    def test_workspace_converter_importable(self):
        from src.tools.knowledge.workspace_converter import convert_workspace, convert_and_write
        assert callable(convert_workspace)
        assert callable(convert_and_write)

    def test_memory_migrator_importable(self):
        from src.tools.knowledge.memory_migrator import migrate_memories
        assert callable(migrate_memories)

    def test_knowledge_note_type_coverage(self):
        """Verify converter can classify into all expected note types."""
        from src.tools.knowledge.workspace_converter import convert_workspace

        test_cases = [
            ("## Goal Setting\nOur objective is to ship by Q2.", "goal"),
            ("## Plan\nThe roadmap has three phases.", "plan"),
            ("## Decision Log\nWe chose React over Vue.", "decision"),
            ("## Lessons Learned\nWe discovered that caching helps.", "learning"),
            ("## Code Architecture\nThe implementation uses a module pattern.", "code"),
            ("## Open Questions\nNeed to investigate scaling.", "question"),
            ("## Current Status\nProgress is on track.", "state"),
        ]

        for content, expected_type in test_cases:
            notes = convert_workspace(content)
            assert len(notes) == 1, f"Expected 1 note for: {content}"
            assert notes[0]["note_type"] == expected_type, \
                f"Expected type '{expected_type}' for: {content}, got '{notes[0]['note_type']}'"

    def test_memory_type_coverage(self):
        """Verify migrator maps all memory types correctly."""
        from src.tools.knowledge.memory_migrator import _map_memory_type

        expected = {
            "observer": "learning",
            "compaction_summary": "state",
            "phase_archive": "retrospective",
            "tool_error": "learning",
            "free": "learning",
        }

        for mem_type, expected_note_type in expected.items():
            result = _map_memory_type(mem_type, "generic content")
            assert result == expected_note_type, \
                f"Expected '{expected_note_type}' for memory type '{mem_type}', got '{result}'"

    def test_angular_model_exports(self):
        """Verify the Angular model file contains the expected KB interfaces."""
        model_path = project_root / "cockpit/src/app/core/models/api.model.ts"
        content = model_path.read_text()

        expected_interfaces = [
            "KnowledgeNote",
            "KnowledgeNoteDetail",
            "KnowledgeSummary",
            "KnowledgeListResponse",
            "KnowledgeSearchResponse",
            "KnowledgeRelationship",
            "KnowledgeNoteType",
            "KnowledgeNoteStatus",
        ]

        for iface in expected_interfaces:
            assert iface in content, f"Missing interface/type: {iface}"

    def test_api_service_has_kb_methods(self):
        """Verify the Angular API service has knowledge base methods."""
        service_path = project_root / "cockpit/src/app/core/services/api.service.ts"
        content = service_path.read_text()

        expected_methods = [
            "getKnowledgeSummary",
            "getKnowledgeNotes",
            "getKnowledgeNote",
            "searchKnowledge",
            "updateKnowledgeNote",
            "deleteKnowledgeNote",
            "exportKnowledge",
        ]

        for method in expected_methods:
            assert method in content, f"Missing API method: {method}"

    def test_project_detail_has_knowledge_tab(self):
        """Verify the project detail component has the Knowledge tab."""
        component_path = project_root / "cockpit/src/app/pages/project-detail/project-detail.component.ts"
        content = component_path.read_text()

        assert "'knowledge'" in content, "Missing 'knowledge' tab type"
        assert "Knowledge" in content, "Missing 'Knowledge' tab label"
        assert "kb-section" in content, "Missing kb-section CSS class"
        assert "loadKBNotes" in content, "Missing loadKBNotes method"
        assert "searchKB" in content, "Missing searchKB method"
        assert "openNote" in content, "Missing openNote method"
        assert "deleteNote" in content, "Missing deleteNote method"
        assert "exportKB" in content, "Missing exportKB method"
