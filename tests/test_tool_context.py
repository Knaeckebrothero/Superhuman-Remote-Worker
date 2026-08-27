"""Tests for ToolContext dependency injection container.

Tests construction, validation, availability checks, file read tracking,
instruction enforcement, phase/multimodal config, web content saving,
and async job status updates.
"""

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from src.core.loader import InstructionFileEntry
from src.tools.context import ToolContext


# =============================================================================
# Helpers
# =============================================================================


def _make_workspace_manager(**overrides):
    """Create a mock WorkspaceManager that passes __post_init__ validation."""
    ws = MagicMock()
    ws.is_initialized = True
    ws.job_id = "test-job-123"
    for k, v in overrides.items():
        setattr(ws, k, v)
    return ws


@dataclass
class FakeInstructionEntry:
    """Minimal stand-in for InstructionFileEntry."""

    file: str
    trigger_type: str
    trigger_target: str
    enforce: bool

    @property
    def path(self) -> str:
        """Mirror InstructionFileEntry.path; file-bound entries resolve to file."""
        return self.file


# =============================================================================
# Construction and Validation
# =============================================================================


class TestToolContextConstruction:
    """Tests for ToolContext __init__ and __post_init__."""

    def test_default_construction(self):
        """Default construction: all fields are None/empty."""
        ctx = ToolContext()
        assert ctx.workspace_manager is None
        assert ctx.todo_manager is None
        assert ctx.postgres_db is None
        assert ctx.datasources == {}
        assert ctx.config == {}
        assert ctx._job_id is None
        assert ctx.citation_engine is None

    def test_with_initialized_workspace_manager(self):
        """Should accept initialized workspace_manager."""
        ws = _make_workspace_manager()
        ctx = ToolContext(workspace_manager=ws)
        assert ctx.workspace_manager is ws

    def test_uninitialized_workspace_manager_raises(self):
        """Should raise ValueError when workspace_manager is not initialized."""
        ws = MagicMock()
        ws.is_initialized = False
        with pytest.raises(ValueError, match="must be initialized"):
            ToolContext(workspace_manager=ws)

    def test_none_workspace_manager_ok(self):
        """None workspace_manager is fine (tools that don't need workspace)."""
        ctx = ToolContext(workspace_manager=None)
        assert ctx.workspace_manager is None

    def test_accepts_all_optional_fields(self):
        """All optional fields should accept values."""
        ws = _make_workspace_manager()
        ctx = ToolContext(
            workspace_manager=ws,
            todo_manager=MagicMock(),
            postgres_db=MagicMock(),
            datasources={"neo4j": MagicMock()},
            config={"key": "val"},
            _job_id="override-id",
        )
        assert ctx._job_id == "override-id"
        assert ctx.config["key"] == "val"


# =============================================================================
# job_id property
# =============================================================================


class TestJobIdProperty:
    """Tests for the job_id property getter/setter."""

    def test_returns_job_id_override(self):
        """Should return _job_id when set."""
        ctx = ToolContext(_job_id="direct-id")
        assert ctx.job_id == "direct-id"

    def test_falls_back_to_workspace_manager(self):
        """Should fall back to workspace_manager.job_id."""
        ws = _make_workspace_manager(job_id="ws-job-456")
        ctx = ToolContext(workspace_manager=ws)
        assert ctx.job_id == "ws-job-456"

    def test_returns_none_when_neither(self):
        """Should return None when no job_id source available."""
        ctx = ToolContext()
        assert ctx.job_id is None

    def test_override_takes_priority(self):
        """_job_id should take priority over workspace_manager.job_id."""
        ws = _make_workspace_manager(job_id="ws-id")
        ctx = ToolContext(workspace_manager=ws, _job_id="override-id")
        assert ctx.job_id == "override-id"

    def test_setter_stores_value(self):
        """Setter should store value in _job_id."""
        ctx = ToolContext()
        ctx.job_id = "set-id"
        assert ctx._job_id == "set-id"
        assert ctx.job_id == "set-id"


# =============================================================================
# Availability checks
# =============================================================================


class TestHasMethods:
    """Tests for has_workspace, has_todo, has_postgres, has_datasource, has_git."""

    def test_has_workspace_true(self):
        ws = _make_workspace_manager()
        ctx = ToolContext(workspace_manager=ws)
        assert ctx.has_workspace() is True

    def test_has_workspace_false(self):
        ctx = ToolContext()
        assert ctx.has_workspace() is False

    def test_has_todo_true(self):
        ctx = ToolContext(todo_manager=MagicMock())
        assert ctx.has_todo() is True

    def test_has_todo_false(self):
        ctx = ToolContext()
        assert ctx.has_todo() is False

    def test_has_postgres_true(self):
        ctx = ToolContext(postgres_db=MagicMock())
        assert ctx.has_postgres() is True

    def test_has_postgres_false(self):
        ctx = ToolContext()
        assert ctx.has_postgres() is False

    def test_has_datasource_true(self):
        ctx = ToolContext(datasources={"neo4j": MagicMock()})
        assert ctx.has_datasource("neo4j") is True

    def test_has_datasource_false_missing_key(self):
        ctx = ToolContext()
        assert ctx.has_datasource("neo4j") is False

    def test_has_datasource_false_none_value(self):
        ctx = ToolContext(datasources={"neo4j": None})
        assert ctx.has_datasource("neo4j") is False

    def test_get_datasource_returns_connection(self):
        conn = MagicMock()
        ctx = ToolContext(datasources={"neo4j": conn})
        assert ctx.get_datasource("neo4j") is conn

    def test_get_datasource_returns_none(self):
        ctx = ToolContext()
        assert ctx.get_datasource("neo4j") is None

    def test_has_git_true(self):
        gm = MagicMock()
        gm.is_active = True
        ws = _make_workspace_manager(git_manager=gm)
        ctx = ToolContext(workspace_manager=ws)
        assert ctx.has_git() is True

    def test_has_git_false_no_workspace(self):
        ctx = ToolContext()
        assert ctx.has_git() is False

    def test_has_git_false_no_git_manager(self):
        ws = _make_workspace_manager(git_manager=None)
        ctx = ToolContext(workspace_manager=ws)
        assert ctx.has_git() is False

    def test_has_git_false_inactive(self):
        gm = MagicMock()
        gm.is_active = False
        ws = _make_workspace_manager(git_manager=gm)
        ctx = ToolContext(workspace_manager=ws)
        assert ctx.has_git() is False

    def test_has_knowledge_true_with_store_only(self):
        # PR4c-3 flip: Neo4j is optional. The pgvector store is the sole
        # requirement — a graph-less deployment must still load KB tools.
        ctx = ToolContext(knowledge_store=MagicMock(), knowledge_graph=None)
        assert ctx.has_knowledge() is True

    def test_has_knowledge_true_with_both(self):
        ctx = ToolContext(knowledge_store=MagicMock(), knowledge_graph=MagicMock())
        assert ctx.has_knowledge() is True

    def test_has_knowledge_false_without_store(self):
        # Graph present but store absent: retrieval is impossible, so the KB
        # is not available regardless of Neo4j.
        ctx = ToolContext(knowledge_store=None, knowledge_graph=MagicMock())
        assert ctx.has_knowledge() is False

    def test_has_knowledge_false_when_nothing(self):
        ctx = ToolContext()
        assert ctx.has_knowledge() is False


# =============================================================================
# db property and get_config
# =============================================================================


class TestDbAndConfig:
    """Tests for db property and get_config."""

    def test_db_returns_postgres(self):
        db = MagicMock()
        ctx = ToolContext(postgres_db=db)
        assert ctx.db is db

    def test_db_returns_none(self):
        ctx = ToolContext()
        assert ctx.db is None

    def test_get_config_existing_key(self):
        ctx = ToolContext(config={"max_size": 1024})
        assert ctx.get_config("max_size") == 1024

    def test_get_config_missing_key_default(self):
        ctx = ToolContext()
        assert ctx.get_config("missing", 42) == 42

    def test_get_config_missing_key_none(self):
        ctx = ToolContext()
        assert ctx.get_config("missing") is None


# =============================================================================
# File read tracking
# =============================================================================


class TestReadTracking:
    """Tests for record_file_read and was_recently_read."""

    def test_record_and_check(self):
        """Recording a read should make it detectable."""
        ctx = ToolContext()
        ctx.record_file_read("foo.md")
        assert ctx.was_recently_read("foo.md") is True

    def test_not_recently_read(self):
        """Unread files should return False."""
        ctx = ToolContext()
        assert ctx.was_recently_read("never_read.md") is False

    def test_leading_slash_normalized(self):
        """Leading slash should be stripped for normalization."""
        ctx = ToolContext()
        ctx.record_file_read("/foo.md")
        assert ctx.was_recently_read("foo.md") is True
        assert ctx.was_recently_read("/foo.md") is True

    def test_whitespace_stripped(self):
        """Whitespace should be stripped."""
        ctx = ToolContext()
        ctx.record_file_read("  foo.md  ")
        assert ctx.was_recently_read("foo.md") is True

    def test_user_edit_invalidation_requires_a_fresh_read(self):
        ctx = ToolContext()
        ctx.record_file_read("/output/report.md", "# Before\n")

        assert ctx.recent_read_matches("output/report.md", "# Before\n") is True
        assert ctx.invalidate_recent_read("output/report.md") is True
        assert ctx.was_recently_read("output/report.md") is False
        assert ctx.recent_read_matches("output/report.md", "# Before\n") is False
        assert ctx.invalidate_recent_read("output/report.md") is False

    def test_versioned_read_detects_changed_full_text(self):
        ctx = ToolContext()
        ctx.record_file_read("output/report.md", "# Before\n")

        assert ctx.recent_read_matches("output/report.md", "# Before\n") is True
        assert ctx.recent_read_matches("output/report.md", "# User edit\n") is False

    def test_path_only_read_preserves_instruction_enforcement_semantics(self):
        ctx = ToolContext()
        ctx.record_file_read("AGENTS.md")

        assert ctx.was_recently_read("AGENTS.md") is True
        assert ctx.recent_read_matches("AGENTS.md", "content not tracked") is False

    def test_deque_eviction(self):
        """Recording 11th file should evict the oldest (maxlen=10)."""
        ctx = ToolContext()
        for i in range(11):
            ctx.record_file_read(f"file_{i}.md")

        # file_0 should be evicted
        assert ctx.was_recently_read("file_0.md") is False
        # file_10 should be present
        assert ctx.was_recently_read("file_10.md") is True

    def test_re_recording_refreshes_position(self):
        """Re-recording a file should move it to the end (not duplicate)."""
        ctx = ToolContext()
        # Fill 10 slots
        for i in range(10):
            ctx.record_file_read(f"file_{i}.md")

        # Re-read file_0 (currently oldest)
        ctx.record_file_read("file_0.md")

        # Now add one more — should evict file_1 (not file_0)
        ctx.record_file_read("new_file.md")

        assert ctx.was_recently_read("file_0.md") is True
        assert ctx.was_recently_read("file_1.md") is False

    def test_get_read_tracking_limit_default(self):
        ctx = ToolContext()
        assert ctx.get_read_tracking_limit() == 10

    def test_get_read_tracking_limit_from_config(self):
        ctx = ToolContext(config={"read_tracking_limit": 20})
        assert ctx.get_read_tracking_limit() == 20


# =============================================================================
# Instruction-file pinning (FIFO eviction exemption)
# =============================================================================


class TestInstructionPinning:
    """Instruction-file paths are exempt from FIFO eviction once read.

    Any 10 reads used to evict the todo-guide skill and re-arm the
    enforce-gate — one forced guide re-read per strategic phase. Pinned
    paths stay "recently read"; the write-authorization path
    (recent_read_matches) deliberately ignores the pin.
    """

    def _ctx_with_guide(self):
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry(
                "skills/todo-guide/SKILL.md", "before_tool", "next_phase_todos", True
            ),
        ]
        return ctx

    def test_instruction_file_survives_many_subsequent_reads(self):
        ctx = self._ctx_with_guide()
        ctx.record_file_read("skills/todo-guide/SKILL.md")
        for i in range(25):
            ctx.record_file_read(f"file_{i}.md")

        assert ctx.was_recently_read("skills/todo-guide/SKILL.md") is True
        # Enforce-gate stays satisfied — no forced re-read
        assert ctx.check_tool_enforcement("next_phase_todos") is None

    def test_normal_files_still_evict(self):
        ctx = self._ctx_with_guide()
        ctx.record_file_read("skills/todo-guide/SKILL.md")
        ctx.record_file_read("notes/facts.md")
        for i in range(10):
            ctx.record_file_read(f"file_{i}.md")

        assert ctx.was_recently_read("notes/facts.md") is False
        assert ctx.was_recently_read("skills/todo-guide/SKILL.md") is True

    def test_invalidate_clears_pin(self):
        ctx = self._ctx_with_guide()
        ctx.record_file_read("skills/todo-guide/SKILL.md")
        for i in range(10):
            ctx.record_file_read(f"file_{i}.md")  # evicted from deque, pin holds

        assert ctx.invalidate_recent_read("skills/todo-guide/SKILL.md") is True
        assert ctx.was_recently_read("skills/todo-guide/SKILL.md") is False

    def test_pin_does_not_authorize_writes(self):
        """recent_read_matches (write authorization) ignores the pin."""
        ctx = self._ctx_with_guide()
        ctx.record_file_read("skills/todo-guide/SKILL.md", "guide body")
        for i in range(10):
            ctx.record_file_read(f"file_{i}.md")

        assert ctx.was_recently_read("skills/todo-guide/SKILL.md") is True
        assert (
            ctx.recent_read_matches("skills/todo-guide/SKILL.md", "guide body") is False
        )

    def test_unconfigured_paths_are_not_pinned(self):
        ctx = ToolContext()
        ctx.record_file_read("skills/todo-guide/SKILL.md")
        for i in range(10):
            ctx.record_file_read(f"file_{i}.md")

        assert ctx.was_recently_read("skills/todo-guide/SKILL.md") is False

    def test_worker_checkpoint_receipt_restores_enforcement_not_write_authority(self):
        entry = InstructionFileEntry(
            trigger="before_tool:todo_complete",
            skill="verify-before-done",
            phases=["tactical"],
            read_scope="phase",
            max_read_age_turns=20,
        )
        source_ws = _make_workspace_manager()
        source = ToolContext(workspace_manager=source_ws)
        source._instruction_files = [entry]
        source.set_current_phase("tactical", phase_number=2, turn_count=10)
        source.record_file_read(entry.path, "guide version one")

        receipts = source.export_instruction_read_receipts()

        successor_ws = _make_workspace_manager()
        successor_ws.read_file.return_value = "guide version one"
        successor = ToolContext(workspace_manager=successor_ws)
        successor._instruction_files = [entry]
        assert successor.restore_instruction_read_receipts(receipts) == 1
        successor.set_current_phase("tactical", phase_number=2, turn_count=11)
        assert successor.check_tool_enforcement("todo_complete") is None
        # The durable receipt can satisfy instruction enforcement, but cannot
        # authorize a mutation of that file on a new lease.
        assert not successor.recent_read_matches(entry.path, "guide version one")

        successor.set_current_phase("tactical", phase_number=4, turn_count=12)
        assert successor.check_tool_enforcement("todo_complete") is not None

    def test_worker_checkpoint_receipt_fails_closed_when_instruction_changed(self):
        entry = InstructionFileEntry(
            trigger="before_tool:todo_complete",
            skill="verify-before-done",
            phases=["tactical"],
            read_scope="phase",
            max_read_age_turns=20,
        )
        source = ToolContext(workspace_manager=_make_workspace_manager())
        source._instruction_files = [entry]
        source.set_current_phase("tactical", phase_number=2, turn_count=10)
        source.record_file_read(entry.path, "old guide")

        successor_ws = _make_workspace_manager()
        successor_ws.read_file.return_value = "new guide"
        successor = ToolContext(workspace_manager=successor_ws)
        successor._instruction_files = [entry]
        assert (
            successor.restore_instruction_read_receipts(
                source.export_instruction_read_receipts()
            )
            == 0
        )
        successor.set_current_phase("tactical", phase_number=2, turn_count=11)
        assert successor.check_tool_enforcement("todo_complete") is not None

    def test_worker_checkpoint_receipt_keeps_version_across_handoffs_and_fifo(self):
        entry = InstructionFileEntry(
            trigger="before_tool:todo_complete",
            skill="verify-before-done",
            phases=["tactical"],
            read_scope="phase",
            max_read_age_turns=20,
        )
        source = ToolContext(workspace_manager=_make_workspace_manager())
        source._instruction_files = [entry]
        source.set_current_phase("tactical", phase_number=2, turn_count=10)
        source.record_file_read(entry.path, "guide version one")
        for index in range(25):
            source.record_file_read(f"notes/read-{index}.md", str(index))

        first_receipts = source.export_instruction_read_receipts()
        expected_version = first_receipts[entry.path]["content_version"]

        second_ws = _make_workspace_manager()
        second_ws.read_file.return_value = "guide version one"
        second = ToolContext(workspace_manager=second_ws)
        second._instruction_files = [entry]
        assert second.restore_instruction_read_receipts(first_receipts) == 1
        # A restored enforcement receipt must never become write authority.
        assert entry.path not in second._recent_read_versions
        assert not second.recent_read_matches(entry.path, "guide version one")

        second_receipts = second.export_instruction_read_receipts()
        assert second_receipts[entry.path]["content_version"] == expected_version

        third_ws = _make_workspace_manager()
        third_ws.read_file.return_value = "guide version two"
        third = ToolContext(workspace_manager=third_ws)
        third._instruction_files = [entry]
        assert third.restore_instruction_read_receipts(second_receipts) == 0
        third.set_current_phase("tactical", phase_number=2, turn_count=11)
        assert third.check_tool_enforcement("todo_complete") is not None


# =============================================================================
# Instruction enforcement
# =============================================================================


class TestInstructionEnforcement:
    """Tests for get_enforcement_files, check_tool_enforcement, get_phase_instruction_files."""

    def test_get_enforcement_files_matching(self):
        """Should return file paths for matching entries."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry("guide.md", "before_tool", "next_phase_todos", True),
        ]
        result = ctx.get_enforcement_files("next_phase_todos")
        assert result == ["guide.md"]

    def test_get_enforcement_files_no_match(self):
        """Should return empty when no entries match."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry("guide.md", "before_tool", "next_phase_todos", True),
        ]
        result = ctx.get_enforcement_files("read_file")
        assert result == []

    def test_get_enforcement_files_only_enforce_true(self):
        """Should only match entries with enforce=True."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry("guide.md", "before_tool", "my_tool", False),
        ]
        result = ctx.get_enforcement_files("my_tool")
        assert result == []

    def test_get_enforcement_files_only_before_tool(self):
        """Should only match trigger_type='before_tool'."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry("guide.md", "phase", "my_tool", True),
        ]
        result = ctx.get_enforcement_files("my_tool")
        assert result == []

    def test_check_enforcement_passes(self):
        """Should return None when all files were recently read."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry("guide.md", "before_tool", "my_tool", True),
        ]
        ctx.record_file_read("guide.md")
        assert ctx.check_tool_enforcement("my_tool") is None

    def test_check_enforcement_fails(self):
        """Should return error string when file not read."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry("guide.md", "before_tool", "my_tool", True),
        ]
        result = ctx.check_tool_enforcement("my_tool")
        assert result is not None
        assert "guide.md" in result
        assert "my_tool" in result

    def test_check_enforcement_no_entries(self):
        """Should return None when no enforcement entries for tool."""
        ctx = ToolContext()
        assert ctx.check_tool_enforcement("read_file") is None

    def test_phase_filtered_gate_only_applies_in_selected_phase(self):
        ctx = ToolContext()
        ctx._instruction_files = [
            InstructionFileEntry(
                trigger="before_tool:todo_complete",
                skill="verify-before-done",
                phases=["tactical"],
                read_scope="phase",
                max_read_age_turns=20,
            )
        ]

        ctx.set_current_phase("strategic", phase_number=1, turn_count=3)
        assert ctx.check_tool_enforcement("todo_complete") is None

        ctx.set_current_phase("tactical", phase_number=2, turn_count=4)
        assert ctx.check_tool_enforcement("todo_complete") is not None

    def test_phase_scoped_read_does_not_unlock_a_later_phase(self):
        entry = InstructionFileEntry(
            trigger="before_tool:todo_complete",
            skill="verify-before-done",
            phases=["tactical"],
            read_scope="phase",
            max_read_age_turns=20,
        )
        ctx = ToolContext()
        ctx._instruction_files = [entry]
        path = entry.path

        ctx.set_current_phase("tactical", phase_number=2, turn_count=5)
        ctx.record_file_read(path)
        assert ctx.check_tool_enforcement("todo_complete") is None

        ctx.set_current_phase("tactical", phase_number=4, turn_count=9)
        assert ctx.check_tool_enforcement("todo_complete") is not None

    def test_instruction_read_expires_after_configured_llm_turns(self):
        entry = InstructionFileEntry(
            trigger="before_tool:todo_complete",
            skill="verify-before-done",
            phases=["tactical"],
            read_scope="phase",
            max_read_age_turns=20,
        )
        ctx = ToolContext()
        ctx._instruction_files = [entry]
        ctx.set_current_phase("tactical", phase_number=2, turn_count=10)
        ctx.record_file_read(entry.path)

        ctx.set_current_phase("tactical", phase_number=2, turn_count=30)
        assert ctx.check_tool_enforcement("todo_complete") is None
        ctx.set_current_phase("tactical", phase_number=2, turn_count=31)
        assert ctx.check_tool_enforcement("todo_complete") is not None

    def test_get_phase_instruction_files_strategic(self):
        """Should return phase_start entries and the legacy phase alias."""
        entry = FakeInstructionEntry("strat.md", "phase_start", "strategic", False)
        ctx = ToolContext()
        ctx._instruction_files = [
            entry,
            FakeInstructionEntry("tact.md", "phase", "tactical", False),
        ]
        result = ctx.get_phase_instruction_files("strategic")
        assert len(result) == 1
        assert result[0].file == "strat.md"

    def test_get_phase_instruction_files_empty(self):
        """Should return empty when no entries match."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry("guide.md", "before_tool", "my_tool", True),
        ]
        result = ctx.get_phase_instruction_files("strategic")
        assert result == []


# =============================================================================
# Phase and multimodal
# =============================================================================


class TestPhaseAndMultimodal:
    """Tests for set_current_phase and get_phase_multimodal."""

    def test_set_current_phase(self):
        ctx = ToolContext()
        ctx.set_current_phase("strategic")
        assert ctx._current_phase == "strategic"

    def test_get_phase_multimodal_with_llm_config(self):
        """With llm_config and phase, should use phase-specific config."""
        llm_config = MagicMock()
        phase_cfg = MagicMock()
        phase_cfg.multimodal = True
        llm_config.get_phase_config.return_value = phase_cfg

        ctx = ToolContext()
        ctx._llm_config = llm_config
        ctx.set_current_phase("tactical")
        assert ctx.get_phase_multimodal() is True
        llm_config.get_phase_config.assert_called_once_with("tactical")

    def test_get_phase_multimodal_fallback_config(self):
        """Without llm_config, should fall back to config['multimodal']."""
        ctx = ToolContext(config={"multimodal": True})
        assert ctx.get_phase_multimodal() is True

    def test_get_phase_multimodal_default_false(self):
        """Without any config, should return False."""
        ctx = ToolContext()
        assert ctx.get_phase_multimodal() is False

    def test_get_phase_multimodal_no_phase_set(self):
        """With llm_config but no phase set, should fall back to config."""
        ctx = ToolContext(config={"multimodal": True})
        ctx._llm_config = MagicMock()  # has llm_config but _current_phase is None
        assert ctx.get_phase_multimodal() is True


# =============================================================================
# Web content saving
# =============================================================================


class TestSaveWebContent:
    """Tests for save_web_content_to_disk."""

    def test_returns_none_without_workspace(self):
        """Should return None when no workspace available."""
        ctx = ToolContext()
        result = ctx.save_web_content_to_disk("https://example.com", "content")
        assert result is None

    def test_generates_deterministic_filename(self):
        """Same URL should produce same filename."""
        ws = _make_workspace_manager()
        path_mock = MagicMock()
        path_mock.exists.return_value = False
        path_mock.parent.mkdir = MagicMock()
        ws.get_path.return_value = path_mock
        ws.write_file = MagicMock()

        ctx = ToolContext(workspace_manager=ws)
        result1 = ctx.save_web_content_to_disk("https://example.com/page", "content")
        result2 = ctx.save_web_content_to_disk("https://example.com/page", "content2")
        assert result1 == result2

    def test_returns_relative_path(self):
        """Should return workspace-relative path starting with documents/external/."""
        ws = _make_workspace_manager()
        path_mock = MagicMock()
        path_mock.exists.return_value = False
        path_mock.parent.mkdir = MagicMock()
        ws.get_path.return_value = path_mock
        ws.write_file = MagicMock()

        ctx = ToolContext(workspace_manager=ws)
        result = ctx.save_web_content_to_disk("https://example.com", "content")
        assert result.startswith("documents/external/")
        assert result.endswith(".md")

    def test_skips_existing_file(self):
        """Should return existing path without rewriting."""
        ws = _make_workspace_manager()
        path_mock = MagicMock()
        path_mock.exists.return_value = True  # File already exists
        ws.get_path.return_value = path_mock
        ws.write_file = MagicMock()

        ctx = ToolContext(workspace_manager=ws)
        result = ctx.save_web_content_to_disk("https://example.com", "content")
        assert result is not None
        ws.write_file.assert_not_called()

    def test_returns_none_on_write_failure(self):
        """Should return None when write_file fails."""
        ws = _make_workspace_manager()
        ws.exists.return_value = False
        ws.write_file.side_effect = Exception("disk full")

        ctx = ToolContext(workspace_manager=ws)
        result = ctx.save_web_content_to_disk("https://example.com", "content")
        assert result is None

    def test_writes_yaml_front_matter(self):
        """Written content should include YAML front-matter."""
        ws = _make_workspace_manager()
        ws.exists.return_value = False
        ws.write_file = MagicMock()

        ctx = ToolContext(workspace_manager=ws)
        ctx.save_web_content_to_disk(
            "https://example.com",
            "Hello world",
            title="Test Page",
            source_id=42,
        )

        ws.write_file.assert_called_once()
        written = ws.write_file.call_args[0][1]
        assert written.startswith("---\n")
        assert "url: https://example.com" in written
        assert 'title: "Test Page"' in written
        assert "source_id: 42" in written
        assert "Hello world" in written


# =============================================================================
# Citation engine lifecycle
# =============================================================================


class TestCitationEngine:
    """Tests for get_citation_engine and close_citation_engine."""

    def test_close_when_none_is_noop(self):
        """close_citation_engine should be safe when engine is None."""
        ctx = ToolContext()
        ctx.close_citation_engine()  # Should not raise

    def test_close_clears_state(self):
        """close_citation_engine should clear engine and registries.

        It must NOT call engine.close() — the engine borrows the agent's
        shared vector pool, which the agent closes on shutdown (see c420f066).
        """
        engine = MagicMock()
        ctx = ToolContext(citation_engine=engine)
        ctx._source_registry = {"a": 1, "b": 2}
        ctx.close_citation_engine()

        assert ctx.citation_engine is None
        assert ctx._source_registry == {}
        engine.close.assert_not_called()
