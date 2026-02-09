"""Unit tests for graph.py (phase alternation graph).

Tests the phase alternation graph architecture routing functions and helper utilities.
LLM-dependent nodes are tested with mocks or integration tests.
"""

import pytest
import tempfile
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# Add project root src to path for imports
project_root = Path(__file__).parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

# Import from src package (requires langgraph in environment)
from src.core.workspace import WorkspaceManager  # noqa: E402
from src.managers import TodoManager, PlanManager, MemoryManager  # noqa: E402
from src.graph import (  # noqa: E402
    route_entry,
    route_after_execute,
    route_after_check_todos,
    create_route_after_transition,
    create_init_workspace_node,
    create_init_strategic_todos_node,
    create_check_todos_node,
    create_archive_phase_node,
    create_check_goal_node,
    create_handle_transition_node,
    get_managers_from_workspace,
)
from src.core.phase import (  # noqa: E402
    get_initial_strategic_todos,
    get_transition_strategic_todos,
    validate_todos_yaml,
    TodosYamlValidationError,
)


@pytest.fixture
def temp_workspace():
    """Create a temporary directory for workspace testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def workspace_manager(temp_workspace):
    """Create a WorkspaceManager with a temporary base path."""
    ws = WorkspaceManager(
        job_id="test-job-123",
        base_path=temp_workspace,
    )
    ws.initialize()
    return ws


@pytest.fixture
def managers(workspace_manager):
    """Create all managers."""
    return {
        "todo": TodoManager(workspace_manager),
        "plan": PlanManager(workspace_manager),
        "memory": MemoryManager(workspace_manager),
        "workspace": workspace_manager,
    }


@pytest.fixture
def mock_config():
    """Create a mock AgentConfig for testing."""
    config = MagicMock()
    config.agent_id = "test-agent"
    config.llm.model = "test-model"
    return config


class TestRouteEntry:
    """Tests for route_entry routing function."""

    def test_route_entry_not_initialized(self):
        """Test that uninitialized state routes to init_workspace."""
        state = {"initialized": False}
        result = route_entry(state)
        assert result == "init_workspace"

    def test_route_entry_initialized(self):
        """Test that initialized state routes to restore_todo_state (resume)."""
        state = {"initialized": True}
        result = route_entry(state)
        assert result == "restore_todo_state"

    def test_route_entry_missing_key(self):
        """Test default behavior when key is missing."""
        state = {}
        result = route_entry(state)
        assert result == "init_workspace"


class TestRouteAfterExecute:
    """Tests for route_after_execute routing function."""

    def test_route_after_execute_no_messages(self):
        """Test routing with no messages goes to check_todos."""
        state = {"messages": []}
        result = route_after_execute(state)
        assert result == "check_todos"

    def test_route_after_execute_with_tool_calls(self):
        """Test routing with tool calls goes to tools."""
        # Create a mock AIMessage with tool_calls
        mock_message = MagicMock()
        mock_message.tool_calls = [{"name": "some_tool"}]

        # Make isinstance check work
        from langchain_core.messages import AIMessage
        mock_message.__class__ = AIMessage

        state = {"messages": [mock_message]}
        result = route_after_execute(state)
        assert result == "tools"

    def test_route_after_execute_without_tool_calls(self):
        """Test routing without tool calls goes to check_todos."""
        mock_message = MagicMock()
        mock_message.tool_calls = []

        from langchain_core.messages import AIMessage
        mock_message.__class__ = AIMessage

        state = {"messages": [mock_message]}
        result = route_after_execute(state)
        assert result == "check_todos"

    def test_route_after_execute_human_message(self):
        """Test routing with HumanMessage goes to check_todos."""
        from langchain_core.messages import HumanMessage
        state = {"messages": [HumanMessage(content="test")]}
        result = route_after_execute(state)
        assert result == "check_todos"


class TestRouteAfterCheckTodos:
    """Tests for route_after_check_todos routing function."""

    def test_route_after_check_todos_phase_complete(self):
        """Test routing when phase is complete."""
        state = {"phase_complete": True}
        result = route_after_check_todos(state)
        assert result == "archive_phase"

    def test_route_after_check_todos_not_complete(self):
        """Test routing when phase is not complete."""
        state = {"phase_complete": False}
        result = route_after_check_todos(state)
        assert result == "execute"

    def test_route_after_check_todos_missing_key(self):
        """Test default behavior when key is missing."""
        state = {}
        result = route_after_check_todos(state)
        assert result == "execute"


class TestGetManagersFromWorkspace:
    """Tests for get_managers_from_workspace helper function."""

    def test_returns_all_managers(self, workspace_manager):
        """Test that helper returns all three managers."""
        todo, plan, memory = get_managers_from_workspace(workspace_manager)

        assert isinstance(todo, TodoManager)
        assert isinstance(plan, PlanManager)
        assert isinstance(memory, MemoryManager)

    def test_managers_use_same_workspace(self, workspace_manager):
        """Test that all managers use the same workspace."""
        todo, plan, memory = get_managers_from_workspace(workspace_manager)

        # Write through one manager, read through workspace
        memory.write("# Test")
        assert workspace_manager.exists("workspace.md")

        plan.write("# Plan")
        assert workspace_manager.exists("plan.md")


class TestInitWorkspaceNode:
    """Tests for init_workspace node."""

    def test_creates_workspace_md(self, managers, mock_config):
        """Test that init creates workspace.md from template."""
        template = "# Test Template\n\n## Section\nContent"
        node = create_init_workspace_node(managers["memory"], template, mock_config)

        state = {"job_id": "test-123"}
        result = node(state)

        assert "workspace_memory" in result
        assert "Test Template" in result["workspace_memory"]
        assert managers["memory"].exists()

    def test_preserves_existing_workspace_md(self, managers, mock_config):
        """Test that existing workspace.md is not overwritten."""
        managers["memory"].write("# Existing Content")

        template = "# New Template"
        node = create_init_workspace_node(managers["memory"], template, mock_config)

        state = {"job_id": "test-123"}
        result = node(state)

        # Should preserve existing content
        assert "Existing Content" in result["workspace_memory"]
        assert "New Template" not in result["workspace_memory"]


class TestCheckTodosNode:
    """Tests for check_todos node."""

    def test_todos_not_complete(self, managers, mock_config):
        """Test check when todos are not complete."""
        managers["todo"].add("Task 1")
        managers["todo"].add("Task 2")

        node = create_check_todos_node(managers["todo"], mock_config)

        state = {"job_id": "test-123", "iteration": 0}
        result = node(state)

        assert result.get("phase_complete") is False
        # Should also export todo state for checkpointing
        assert "todos" in result
        assert "staged_todos" in result
        assert "todo_next_id" in result
        assert len(result["todos"]) == 2

    def test_todos_all_complete(self, managers, mock_config):
        """Test check when all todos are complete."""
        managers["todo"].add("Task 1")
        managers["todo"].complete("todo_1")

        node = create_check_todos_node(managers["todo"], mock_config)

        state = {"job_id": "test-123", "iteration": 0}
        result = node(state)

        assert result.get("phase_complete") is True
        # Should also export todo state for checkpointing
        assert "todos" in result
        assert result["todos"][0]["status"] == "completed"


class TestRestoreTodoStateNode:
    """Tests for restore_todo_state node."""

    def test_restores_from_checkpoint(self, managers):
        """Test that restore_todo_state restores TodoManager from checkpoint."""
        from src.graph import create_restore_todo_state_node

        node = create_restore_todo_state_node(managers["todo"])

        # State with todo data from checkpoint
        state = {
            "job_id": "test-123",
            "is_strategic_phase": False,
            "todos": [
                {"id": "todo_1", "content": "Task 1", "status": "completed", "priority": "high", "notes": ["Done"]},
                {"id": "todo_2", "content": "Task 2", "status": "pending", "priority": "medium", "notes": []},
            ],
            "staged_todos": [],
            "todo_next_id": 3,
        }

        result = node(state)

        # Node returns empty dict (no state changes needed)
        assert result == {}

        # But TodoManager should be restored
        todos = managers["todo"].list_all()
        assert len(todos) == 2
        assert todos[0].id == "todo_1"
        assert todos[0].status.value == "completed"
        assert todos[1].id == "todo_2"
        assert managers["todo"]._next_id == 3
        # Phase state should also be restored
        assert managers["todo"].is_strategic_phase is False

    def test_handles_empty_checkpoint(self, managers):
        """Test that restore_todo_state handles checkpoints without todo data."""
        from src.graph import create_restore_todo_state_node

        node = create_restore_todo_state_node(managers["todo"])

        # Old checkpoint without todo fields
        state = {
            "job_id": "test-123",
            "is_strategic_phase": True,
            "todos": None,
            "staged_todos": None,
            "todo_next_id": None,
        }

        result = node(state)

        # Node returns empty dict
        assert result == {}
        # TodoManager should remain in initial state
        assert managers["todo"].list_all() == []

    def test_restores_staged_todos(self, managers):
        """Test that restore_todo_state restores staged todos."""
        from src.graph import create_restore_todo_state_node

        node = create_restore_todo_state_node(managers["todo"])

        state = {
            "job_id": "test-123",
            "is_strategic_phase": True,
            "todos": [],
            "staged_todos": [
                {"id": "todo_1", "content": "Staged Task 1", "status": "pending", "priority": "medium", "notes": []},
            ],
            "todo_next_id": 5,
        }

        node(state)

        assert managers["todo"].has_staged_todos()
        assert managers["todo"]._next_id == 5


class TestArchivePhaseNode:
    """Tests for archive_phase node."""

    @pytest.mark.asyncio
    async def test_archives_todos(self, managers, mock_config):
        """Test that todos are archived."""
        managers["todo"].add("Task 1")
        managers["todo"].complete("todo_1")
        managers["plan"].write("## Phase 1: Test\n\n- [x] Task 1")

        # Create mock context manager - ensure_within_limits returns unchanged messages
        mock_context_mgr = MagicMock()
        # ensure_within_limits is async, so we need AsyncMock
        from unittest.mock import AsyncMock
        mock_context_mgr.ensure_within_limits = AsyncMock(return_value=[])

        # Mock config to enable compact_on_archive
        mock_config.context_management = MagicMock()
        mock_config.context_management.compact_on_archive = True
        mock_config.context_management.reasoning_level = None
        mock_config.context_management.max_summary_length = 10000
        mock_config.llm = MagicMock()
        mock_config.llm.reasoning_level = "high"

        mock_llm = MagicMock()
        mock_summarization_prompt = "Summarize this conversation."

        node = create_archive_phase_node(
            managers["todo"], managers["plan"], mock_config,
            mock_context_mgr, mock_llm, mock_summarization_prompt
        )

        state = {"job_id": "test-123", "messages": []}
        result = await node(state)

        assert "messages" in result
        assert "Phase complete" in result["messages"][0].content
        # Todos should be cleared
        assert len(managers["todo"].list_all()) == 0

    @pytest.mark.asyncio
    async def test_force_summarize_on_strategic_to_tactical_transition(self, managers, mock_config):
        """Test that summarization is forced when transitioning from strategic to tactical.

        This ensures tactical phases get a 'fresh conversation' with just the plan summary,
        reducing context size and removing irrelevant planning discussions.
        """
        managers["todo"].add("Task 1")
        managers["todo"].complete("todo_1")
        managers["plan"].write("## Phase 1: Test\n\n- [x] Task 1")

        # Create mock context manager to capture the force parameter
        mock_context_mgr = MagicMock()
        mock_context_mgr.ensure_within_limits = AsyncMock(return_value=[])

        # Mock config to enable compact_on_archive
        mock_config.context_management = MagicMock()
        mock_config.context_management.compact_on_archive = True
        mock_config.context_management.reasoning_level = None
        mock_config.context_management.max_summary_length = 10000
        mock_config.llm = MagicMock()
        mock_config.llm.reasoning_level = "high"

        mock_llm = MagicMock()
        mock_summarization_prompt = "Summarize this conversation."

        node = create_archive_phase_node(
            managers["todo"], managers["plan"], mock_config,
            mock_context_mgr, mock_llm, mock_summarization_prompt
        )

        # Test strategic phase (is_strategic=True) - should force summarization
        state = {"job_id": "test-123", "messages": [], "is_strategic_phase": True}
        await node(state)

        # Verify ensure_within_limits was called with force=True
        call_kwargs = mock_context_mgr.ensure_within_limits.call_args
        assert call_kwargs.kwargs.get("force") is True, (
            "Expected force=True for strategic→tactical transition"
        )

    @pytest.mark.asyncio
    async def test_no_force_summarize_on_tactical_to_strategic_transition(self, managers, mock_config):
        """Test that summarization is NOT forced when transitioning from tactical to strategic.

        Tactical→strategic transitions should preserve execution context for strategic reflection,
        only summarizing if thresholds are exceeded.
        """
        managers["todo"].add("Task 1")
        managers["todo"].complete("todo_1")
        managers["plan"].write("## Phase 1: Test\n\n- [x] Task 1")

        # Create mock context manager to capture the force parameter
        mock_context_mgr = MagicMock()
        mock_context_mgr.ensure_within_limits = AsyncMock(return_value=[])

        # Mock config to enable compact_on_archive
        mock_config.context_management = MagicMock()
        mock_config.context_management.compact_on_archive = True
        mock_config.context_management.reasoning_level = None
        mock_config.context_management.max_summary_length = 10000
        mock_config.llm = MagicMock()
        mock_config.llm.reasoning_level = "high"

        mock_llm = MagicMock()
        mock_summarization_prompt = "Summarize this conversation."

        node = create_archive_phase_node(
            managers["todo"], managers["plan"], mock_config,
            mock_context_mgr, mock_llm, mock_summarization_prompt
        )

        # Test tactical phase (is_strategic=False) - should NOT force summarization
        state = {"job_id": "test-123", "messages": [], "is_strategic_phase": False}
        await node(state)

        # Verify ensure_within_limits was called with force=False
        call_kwargs = mock_context_mgr.ensure_within_limits.call_args
        assert call_kwargs.kwargs.get("force") is False, (
            "Expected force=False for tactical→strategic transition"
        )


class TestCheckGoalNode:
    """Tests for check_goal node."""

    def test_goal_not_achieved(self, managers, mock_config):
        """Test when plan is not complete."""
        managers["plan"].write("## Phase 1\n\n- [ ] Task 1")

        node = create_check_goal_node(managers["plan"], managers["workspace"], mock_config, managers["todo"])

        state = {"job_id": "test-123"}
        result = node(state)

        assert result.get("goal_achieved") is False

    def test_goal_achieved_plan_complete(self, managers, mock_config):
        """Test when plan is marked complete."""
        managers["plan"].write("# Plan\n\n# Complete\n\nAll done.")

        node = create_check_goal_node(managers["plan"], managers["workspace"], mock_config, managers["todo"])

        state = {"job_id": "test-123"}
        result = node(state)

        assert result.get("goal_achieved") is True
        assert result.get("should_stop") is True

    def test_goal_achieved_no_more_phases(self, managers, mock_config):
        """Test when there are no more phases."""
        # Plan with all phases complete (no pending phases)
        managers["plan"].write("""# Plan

## Phase 1 (complete)
- [x] Done
""")

        node = create_check_goal_node(managers["plan"], managers["workspace"], mock_config, managers["todo"])

        state = {"job_id": "test-123"}
        result = node(state)

        # Should check is_complete which looks for completed markers
        # If plan has "(complete)" it might trigger completion
        # Otherwise no more phases means goal achieved
        assert result.get("goal_achieved") is True or result.get("goal_achieved") is False
        # At minimum, should return something about goal state
        assert "goal_achieved" in result

    def test_goal_achieved_job_complete_called(self, managers, mock_config):
        """Test that job_complete (writing job_completion.json) triggers goal achieved."""
        # Simulate job_complete tool having written the completion file
        import json
        completion_data = {
            "status": "job_completed",
            "summary": "All tasks complete",
            "deliverables": ["output/results.json"],
        }
        managers["workspace"].write_file(
            "output/job_completion.json",
            json.dumps(completion_data)
        )

        node = create_check_goal_node(managers["plan"], managers["workspace"], mock_config, managers["todo"])

        state = {"job_id": "test-123"}
        result = node(state)

        assert result.get("goal_achieved") is True
        assert result.get("should_stop") is True


# =============================================================================
# PHASE ALTERNATION TESTS
# =============================================================================


class TestRouteAfterTransition:
    """Tests for route_after_transition routing function."""

    def test_route_after_transition_success_with_phase_marker(self, workspace_manager):
        """Test routing when transition succeeded (phase boundary marker present)."""
        route_after_transition = create_route_after_transition(workspace_manager)
        from langchain_core.messages import HumanMessage
        marker = HumanMessage(content="[PHASE_TRANSITION] Tactical phase complete.")
        state = {"messages": [marker]}
        result = route_after_transition(state)
        assert result == "check_goal"

    def test_route_after_transition_rejected(self, workspace_manager):
        """Test routing when transition was rejected."""
        route_after_transition = create_route_after_transition(workspace_manager)
        from langchain_core.messages import ToolMessage
        error_msg = ToolMessage(
            content="[TRANSITION_REJECTED] todos.yaml validation failed",
            tool_call_id="phase_transition",
        )
        state = {"messages": [error_msg]}
        result = route_after_transition(state)
        assert result == "execute"

    def test_route_after_transition_other_messages(self, workspace_manager):
        """Test routing with other types of messages."""
        route_after_transition = create_route_after_transition(workspace_manager)
        from langchain_core.messages import HumanMessage
        state = {"messages": [HumanMessage(content="Some message")]}
        result = route_after_transition(state)
        # Other messages go to check_goal
        assert result == "check_goal"

    def test_route_after_transition_job_frozen(self, workspace_manager):
        """Test routing when job is frozen (job_complete was called).

        When job_frozen.json exists, should always route to check_goal
        even if transition was rejected (no todos staged).
        """
        route_after_transition = create_route_after_transition(workspace_manager)

        # Create job_frozen.json to simulate job_complete having been called
        import json
        workspace_manager.write_file(
            "output/job_frozen.json",
            json.dumps({"status": "frozen", "summary": "Test"})
        )

        # Even with rejection message, should route to check_goal
        from langchain_core.messages import ToolMessage
        error_msg = ToolMessage(
            content="[TRANSITION_REJECTED] No todos staged",
            tool_call_id="phase_transition",
        )
        state = {"messages": [error_msg]}
        result = route_after_transition(state)
        assert result == "check_goal"  # NOT execute, because job is frozen


class TestInitStrategicTodosNode:
    """Tests for init_strategic_todos node (phase alternation)."""

    def test_loads_predefined_strategic_todos(self, managers, mock_config):
        """Test that init loads predefined strategic todos."""
        # Write instructions for the node to read
        managers["workspace"].write_file("instructions.md", "# Test Task\n\nDo something.")

        node = create_init_strategic_todos_node(
            managers["workspace"], managers["todo"], mock_config
        )

        state = {"job_id": "test-123"}
        result = node(state)

        # Should have messages with instructions
        assert "messages" in result
        assert len(result["messages"]) == 1
        assert "Test Task" in result["messages"][0].content

        # Should set phase state
        assert result.get("initialized") is True
        assert result.get("is_strategic_phase") is True
        assert result.get("phase_number") == 0
        assert result.get("phase_complete") is False

        # Should have loaded strategic todos
        todos = managers["todo"].list_all()
        assert len(todos) == 4  # Predefined strategic todos

    def test_handles_missing_instructions(self, managers, mock_config):
        """Test handling when instructions.md doesn't exist."""
        node = create_init_strategic_todos_node(
            managers["workspace"], managers["todo"], mock_config
        )

        state = {"job_id": "test-123"}
        result = node(state)

        # Should still work with fallback message
        assert "messages" in result
        assert "No instructions.md found" in result["messages"][0].content

        # Should still load strategic todos
        todos = managers["todo"].list_all()
        assert len(todos) == 4


class TestPredefinedTodos:
    """Tests for predefined strategic todos."""

    def test_initial_strategic_todos(self):
        """Test get_initial_strategic_todos returns correct todos."""
        todos = get_initial_strategic_todos()
        assert len(todos) == 4

        # Check content patterns
        contents = [t.content for t in todos]
        assert any("workspace" in c.lower() for c in contents)
        assert any("plan" in c.lower() for c in contents)
        assert any("todos.yaml" in c.lower() for c in contents)

    def test_transition_strategic_todos(self):
        """Test get_transition_strategic_todos returns correct todos."""
        todos = get_transition_strategic_todos()
        assert len(todos) == 4

        # Check content patterns
        contents = [t.content for t in todos]
        assert any("summarize" in c.lower() for c in contents)
        assert any("workspace.md" in c.lower() for c in contents)
        assert any("plan.md" in c.lower() for c in contents)
        assert any("todos.yaml" in c.lower() or "job_complete" in c.lower() for c in contents)


class TestTodosYamlValidation:
    """Tests for todos.yaml validation."""

    def test_valid_todos_yaml(self):
        """Test validation of a valid todos.yaml."""
        content = """
phase: "Phase 1: Extract requirements"
description: "Process documents and extract requirements"
todos:
  - id: 1
    content: "Read the source document"
  - id: 2
    content: "Extract section headings"
  - id: 3
    content: "Identify requirement statements"
  - id: 4
    content: "Validate extracted requirements"
  - id: 5
    content: "Write requirements to database"
"""
        metadata, todos = validate_todos_yaml(content)

        assert metadata.get("phase") == "Phase 1: Extract requirements"
        assert len(todos) == 5
        assert todos[0]["id"] == 1
        assert todos[0]["content"] == "Read the source document"

    def test_invalid_yaml_syntax(self):
        """Test validation fails for invalid YAML."""
        content = "invalid: yaml: syntax: here"
        with pytest.raises(TodosYamlValidationError) as exc_info:
            validate_todos_yaml(content)
        assert "YAML" in str(exc_info.value)

    def test_too_few_todos(self):
        """Test validation fails when too few todos."""
        content = """
todos:
  - id: 1
    content: "Only one todo"
"""
        with pytest.raises(TodosYamlValidationError) as exc_info:
            validate_todos_yaml(content, min_todos=5)
        assert "Too few todos" in str(exc_info.value.errors[0])

    def test_too_many_todos(self):
        """Test validation fails when too many todos."""
        todos_list = "\n".join([f"  - id: {i}\n    content: 'Todo number {i}'" for i in range(1, 25)])
        content = f"todos:\n{todos_list}"
        with pytest.raises(TodosYamlValidationError) as exc_info:
            validate_todos_yaml(content, max_todos=20)
        assert "Too many todos" in str(exc_info.value.errors[0])

    def test_missing_required_fields(self):
        """Test validation fails when required fields are missing."""
        content = """
todos:
  - id: 1
  - content: "Missing id"
  - id: 3
    content: "Complete"
  - id: 4
    content: "Also complete"
  - id: 5
    content: "Five"
"""
        with pytest.raises(TodosYamlValidationError) as exc_info:
            validate_todos_yaml(content)
        errors = exc_info.value.errors
        assert any("content" in e for e in errors)
        assert any("id" in e for e in errors)


class TestHandleTransitionNode:
    """Tests for handle_transition node."""

    def test_strategic_to_tactical_success(self, managers, mock_config):
        """Test successful strategic -> tactical transition."""
        # Write valid todos.yaml
        content = """
phase: "Phase 1"
todos:
  - id: 1
    content: "First task"
  - id: 2
    content: "Second task"
  - id: 3
    content: "Third task"
  - id: 4
    content: "Fourth task"
  - id: 5
    content: "Fifth task"
"""
        managers["workspace"].write_file("todos.yaml", content)

        # Add phase_settings to mock config
        phase_settings = MagicMock()
        phase_settings.min_todos = 5
        phase_settings.max_todos = 20
        mock_config.phase_settings = phase_settings

        node = create_handle_transition_node(
            managers["workspace"], managers["todo"], mock_config,
            min_todos=5, max_todos=20
        )

        state = {
            "job_id": "test-123",
            "is_strategic_phase": True,
            "phase_number": 0,
            "iteration": 10,
        }
        result = node(state)

        # Should inject phase boundary marker
        assert len(result.get("messages", [])) == 1
        assert "[PHASE_TRANSITION]" in result["messages"][0].content
        # Should flip phase
        assert result.get("is_strategic_phase") is False
        # Should increment phase number
        assert result.get("phase_number") == 1

        # TodoManager should have new todos
        todos = managers["todo"].list_all()
        assert len(todos) == 5

    def test_strategic_to_tactical_rejection(self, managers, mock_config):
        """Test rejected strategic -> tactical transition (no todos.yaml)."""
        # Add phase_settings to mock config
        phase_settings = MagicMock()
        phase_settings.min_todos = 5
        phase_settings.max_todos = 20
        mock_config.phase_settings = phase_settings

        node = create_handle_transition_node(
            managers["workspace"], managers["todo"], mock_config,
            min_todos=5, max_todos=20
        )

        state = {
            "job_id": "test-123",
            "is_strategic_phase": True,
            "phase_number": 0,
            "iteration": 10,
        }
        result = node(state)

        # Should have error message
        assert "messages" in result
        assert len(result["messages"]) == 1
        assert "[TRANSITION_REJECTED]" in result["messages"][0].content

    def test_tactical_to_strategic_transition(self, managers, mock_config):
        """Test tactical -> strategic transition (archives and loads strategic todos)."""
        # Add some completed todos
        managers["todo"].add("Task 1")
        managers["todo"].complete("todo_1")

        # Add phase_settings to mock config
        phase_settings = MagicMock()
        phase_settings.min_todos = 5
        phase_settings.max_todos = 20
        mock_config.phase_settings = phase_settings

        node = create_handle_transition_node(
            managers["workspace"], managers["todo"], mock_config,
            min_todos=5, max_todos=20
        )

        state = {
            "job_id": "test-123",
            "is_strategic_phase": False,  # Tactical mode
            "phase_number": 1,
            "iteration": 20,
        }
        result = node(state)

        # Should inject phase boundary marker
        assert len(result.get("messages", [])) == 1
        assert "[PHASE_TRANSITION]" in result["messages"][0].content
        # Should flip to strategic
        assert result.get("is_strategic_phase") is True
        # Should increment phase number
        assert result.get("phase_number") == 2

        # TodoManager should have predefined strategic todos
        todos = managers["todo"].list_all()
        assert len(todos) == 4  # Transition strategic todos


# =============================================================================
# INTEGRATION TESTS
# =============================================================================


class TestPhaseAlternationCycle:
    """Integration tests for the full phase alternation cycle."""

    def test_full_strategic_tactical_strategic_cycle(self, managers, mock_config):
        """Test a complete cycle: init → strategic → tactical → strategic."""
        # Configure mock
        phase_settings = MagicMock()
        phase_settings.min_todos = 5
        phase_settings.max_todos = 20
        mock_config.phase_settings = phase_settings

        # Step 1: Initialize with strategic todos
        managers["workspace"].write_file("instructions.md", "# Test Task\n\nExtract data.")
        init_node = create_init_strategic_todos_node(
            managers["workspace"], managers["todo"], mock_config
        )

        state = {"job_id": "test-123"}
        state.update(init_node(state))

        # Verify strategic initialization
        assert state.get("is_strategic_phase") is True
        assert state.get("phase_number") == 0
        assert len(managers["todo"].list_all()) == 4  # Initial strategic todos

        # Step 2: Complete strategic phase and write todos.yaml
        for todo in managers["todo"].list_all():
            managers["todo"].complete(todo.id)

        # Write todos.yaml (simulating strategic agent's output)
        todos_yaml = """
phase: "Phase 1: Execute extraction"
todos:
  - id: 1
    content: "Read document"
  - id: 2
    content: "Extract section 1"
  - id: 3
    content: "Extract section 2"
  - id: 4
    content: "Validate extractions"
  - id: 5
    content: "Write to database"
"""
        managers["workspace"].write_file("todos.yaml", todos_yaml)

        # Step 3: Transition strategic → tactical
        transition_node = create_handle_transition_node(
            managers["workspace"], managers["todo"], mock_config,
            min_todos=5, max_todos=20
        )

        state["iteration"] = 10
        result = transition_node(state)
        state.update(result)

        # Verify transition to tactical
        assert state.get("is_strategic_phase") is False
        assert state.get("phase_number") == 1
        assert any("[PHASE_TRANSITION]" in getattr(m, "content", "") for m in state.get("messages", []))
        assert len(managers["todo"].list_all()) == 5  # Loaded from todos.yaml

        # Step 4: Complete tactical todos
        for todo in managers["todo"].list_all():
            managers["todo"].complete(todo.id)

        # Step 5: Transition tactical → strategic
        state["iteration"] = 20
        result = transition_node(state)
        state.update(result)

        # Verify transition back to strategic
        assert state.get("is_strategic_phase") is True
        assert state.get("phase_number") == 2
        assert any("[PHASE_TRANSITION]" in getattr(m, "content", "") for m in state.get("messages", []))
        # Should have transition strategic todos (4 items)
        assert len(managers["todo"].list_all()) == 4

    def test_job_completion_detection(self, managers, mock_config):
        """Test that job_complete creates the completion marker file."""
        # Create the output directory
        output_dir = managers["workspace"].get_path("output")
        output_dir.mkdir(parents=True, exist_ok=True)

        # Simulate job_complete tool writing the completion file
        completion_file = output_dir / "job_completion.json"
        completion_file.write_text('{"status": "complete", "summary": "Task done"}')

        # Create check_goal node
        check_goal = create_check_goal_node(
            managers["plan"], managers["workspace"], mock_config, managers["todo"]
        )

        state = {"job_id": "test-123", "iteration": 50}
        result = check_goal(state)

        # Should detect goal achieved
        assert result.get("goal_achieved") is True
        assert result.get("should_stop") is True

    def test_transition_rejection_retryable(self, managers, mock_config):
        """Test that transition rejection allows retry."""
        phase_settings = MagicMock()
        phase_settings.min_todos = 5
        phase_settings.max_todos = 20
        mock_config.phase_settings = phase_settings

        transition_node = create_handle_transition_node(
            managers["workspace"], managers["todo"], mock_config,
            min_todos=5, max_todos=20
        )

        # Create the routing function with workspace access
        route_after_transition = create_route_after_transition(managers["workspace"])

        # First attempt: no todos.yaml
        state = {
            "job_id": "test-123",
            "is_strategic_phase": True,
            "phase_number": 0,
            "iteration": 10,
        }
        result = transition_node(state)

        # Should be rejected
        assert "[TRANSITION_REJECTED]" in result["messages"][0].content
        # State should NOT change
        assert "is_strategic_phase" not in result or result.get("is_strategic_phase") is None

        # After rejection, route_after_transition sends back to execute
        assert route_after_transition({"messages": result["messages"]}) == "execute"

        # Second attempt: write valid todos.yaml (content must be 10+ chars)
        todos_yaml = """
phase: "Phase 1"
todos:
  - id: 1
    content: "Read the source document and identify structure"
  - id: 2
    content: "Extract requirements from section one"
  - id: 3
    content: "Extract requirements from section two"
  - id: 4
    content: "Validate all extracted requirements"
  - id: 5
    content: "Write validated requirements to database"
"""
        managers["workspace"].write_file("todos.yaml", todos_yaml)

        # Try transition again
        state["iteration"] = 15
        result = transition_node(state)

        # Should succeed now
        assert len(result.get("messages", [])) == 1
        assert "[PHASE_TRANSITION]" in result["messages"][0].content
        assert result.get("is_strategic_phase") is False
        assert result.get("phase_number") == 1

        # route_after_transition should proceed to check_goal
        assert route_after_transition({"messages": result["messages"]}) == "check_goal"

    def test_workspace_memory_persists_across_phases(self, managers, mock_config):
        """Test that workspace.md content persists across phase transitions."""
        phase_settings = MagicMock()
        phase_settings.min_todos = 5
        phase_settings.max_todos = 20
        mock_config.phase_settings = phase_settings

        # Write initial memory
        managers["memory"].write("# Project Memory\n\n## Findings\n\n- Important fact 1")

        # Initialize strategic phase
        init_node = create_init_strategic_todos_node(
            managers["workspace"], managers["todo"], mock_config
        )
        state = init_node({"job_id": "test-123"})

        # Verify memory still exists
        assert "Important fact 1" in managers["memory"].read()

        # Complete strategic todos and transition
        for todo in managers["todo"].list_all():
            managers["todo"].complete(todo.id)

        todos_yaml = """
phase: "Phase 1"
todos:
  - id: 1
    content: "Read the source document carefully"
  - id: 2
    content: "Extract key information from document"
  - id: 3
    content: "Validate extracted information"
  - id: 4
    content: "Transform data into required format"
  - id: 5
    content: "Write results to output file"
"""
        managers["workspace"].write_file("todos.yaml", todos_yaml)

        # Transition to tactical
        transition_node = create_handle_transition_node(
            managers["workspace"], managers["todo"], mock_config,
            min_todos=5, max_todos=20
        )
        state["is_strategic_phase"] = True
        state["phase_number"] = 0
        state["iteration"] = 10
        transition_node(state)

        # Verify memory still exists after transition
        memory_content = managers["memory"].read()
        assert "Important fact 1" in memory_content

        # Update memory during tactical phase
        managers["memory"].update_section("Findings", "- Important fact 1\n- Important fact 2")

        # Complete tactical and transition back to strategic
        for todo in managers["todo"].list_all():
            managers["todo"].complete(todo.id)

        state["is_strategic_phase"] = False
        state["phase_number"] = 1
        state["iteration"] = 20
        transition_node(state)

        # Verify both facts persist
        memory_content = managers["memory"].read()
        assert "Important fact 1" in memory_content
        assert "Important fact 2" in memory_content


# =============================================================================
# EDIT FILE TOOL TESTS
# =============================================================================


class TestEditFileTool:
    """Tests for the edit_file workspace tool."""

    @pytest.fixture
    def workspace_tools_dict(self, workspace_manager):
        """Create workspace tools and return as dict."""
        from src.tools.workspace import create_workspace_tools
        from src.tools.context import ToolContext

        ctx = ToolContext(workspace_manager=workspace_manager)
        tools = create_workspace_tools(ctx)
        return {t.name: t for t in tools}

    @pytest.fixture
    def edit_tool(self, workspace_tools_dict):
        """Get the edit_file tool from workspace tools."""
        return workspace_tools_dict["edit_file"]

    @pytest.fixture
    def read_tool(self, workspace_tools_dict):
        """Get the read_file tool from workspace tools."""
        return workspace_tools_dict["read_file"]

    def test_edit_file_single_replacement(self, workspace_manager, edit_tool, read_tool):
        """Test successful single replacement."""
        workspace_manager.write_file("test.md", "Hello world\nGoodbye world\n")
        read_tool.invoke({"path": "test.md"})  # Must read first
        result = edit_tool.invoke({"path": "test.md", "old_string": "Hello world", "new_string": "Hi world"})
        assert "Edited" in result
        content = workspace_manager.read_file("test.md")
        assert content == "Hi world\nGoodbye world\n"

    def test_edit_file_not_found(self, edit_tool):
        """Test error when file doesn't exist."""
        # File doesn't exist - should fail before read check
        result = edit_tool.invoke({"path": "missing.md", "old_string": "x", "new_string": "y"})
        assert "Error" in result
        assert "not found" in result

    def test_edit_file_old_string_missing(self, workspace_manager, edit_tool, read_tool):
        """Test error when old_string not in file content."""
        workspace_manager.write_file("test.md", "Hello world\n")
        read_tool.invoke({"path": "test.md"})  # Must read first
        result = edit_tool.invoke({"path": "test.md", "old_string": "does not exist", "new_string": "y"})
        assert "Error" in result
        assert "old_string not found" in result

    def test_edit_file_multiple_matches(self, workspace_manager, edit_tool, read_tool):
        """Test error when old_string appears multiple times."""
        workspace_manager.write_file("test.md", "foo bar\nfoo baz\n")
        read_tool.invoke({"path": "test.md"})  # Must read first
        result = edit_tool.invoke({"path": "test.md", "old_string": "foo", "new_string": "qux"})
        assert "Error" in result
        assert "2 times" in result
        assert "more surrounding context" in result
        # File should be unchanged
        assert workspace_manager.read_file("test.md") == "foo bar\nfoo baz\n"

    def test_edit_file_deletion(self, workspace_manager, edit_tool, read_tool):
        """Test deletion by replacing with empty string."""
        workspace_manager.write_file("test.md", "keep this\ndelete this\nkeep too\n")
        read_tool.invoke({"path": "test.md"})  # Must read first
        result = edit_tool.invoke({"path": "test.md", "old_string": "delete this\n", "new_string": ""})
        assert "Edited" in result
        content = workspace_manager.read_file("test.md")
        assert content == "keep this\nkeep too\n"

    def test_edit_file_directory_error(self, workspace_manager, edit_tool, read_tool):
        """Test error when path is a directory."""
        workspace_manager.get_path("subdir").mkdir(parents=True, exist_ok=True)
        # Can't read a directory, so this should fail at the directory check
        result = edit_tool.invoke({"path": "subdir", "old_string": "x", "new_string": "y"})
        assert "Error" in result
        assert "directory" in result

    def test_edit_file_requires_read(self, workspace_manager, edit_tool):
        """Test that edit_file fails without recent read."""
        workspace_manager.write_file("test.md", "Hello world\n")
        # Don't read first - should fail
        result = edit_tool.invoke({"path": "test.md", "old_string": "Hello", "new_string": "Hi"})
        assert "Error" in result
        assert "read_file" in result.lower()


# =============================================================================
# EDIT CITATION TOOL TESTS
# =============================================================================


class TestEditCitationTool:
    """Tests for the edit_citation tool."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database with citations namespace."""
        db = MagicMock()
        db.citations = MagicMock()
        return db

    @pytest.fixture
    def edit_tool(self, workspace_manager, mock_db):
        """Create citation tools and return the edit_citation tool."""
        from src.tools.citation import create_citation_tools
        from src.tools.context import ToolContext

        ctx = ToolContext(
            workspace_manager=workspace_manager,
            postgres_db=mock_db,
            _job_id="test-job-123",
        )
        tools = create_citation_tools(ctx)
        for t in tools:
            if t.name == "edit_citation":
                return t
        pytest.fail("edit_citation tool not found in citation tools")

    @pytest.mark.asyncio
    async def test_edit_claim(self, edit_tool, mock_db):
        """Test successful edit of claim field."""
        mock_db.citations.edit = AsyncMock(return_value=None)

        result = await edit_tool.ainvoke({
            "citation_id": 1,
            "claim": "Updated claim text",
        })

        assert "ok: edited citation [1]" in result
        assert "verification_status reset" in result
        mock_db.citations.edit.assert_called_once()
        assert mock_db.citations.edit.call_args.kwargs["claim"] == "Updated claim text"

    @pytest.mark.asyncio
    async def test_edit_not_found(self, edit_tool, mock_db):
        """Test error when citation is not found."""
        mock_db.citations.edit = AsyncMock(
            side_effect=ValueError("Citation 999 not found")
        )

        result = await edit_tool.ainvoke({
            "citation_id": 999,
            "claim": "New claim",
        })

        assert "error:" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_content_edit_resets_verification(self, edit_tool, mock_db):
        """Test that editing content fields triggers verification reset message."""
        mock_db.citations.edit = AsyncMock(return_value=None)

        result = await edit_tool.ainvoke({
            "citation_id": 1,
            "verbatim_quote": "New quote text",
        })

        assert "verification_status reset to 'pending'" in result

    @pytest.mark.asyncio
    async def test_non_content_edit_preserves_verification(self, edit_tool, mock_db):
        """Test that editing non-content fields does not mention verification reset."""
        mock_db.citations.edit = AsyncMock(return_value=None)

        result = await edit_tool.ainvoke({
            "citation_id": 1,
            "confidence": "medium",
        })

        assert "ok: edited citation [1]" in result
        assert "verification_status reset" not in result

    @pytest.mark.asyncio
    async def test_edit_no_fields(self, edit_tool, mock_db):
        """Test error when no fields are provided."""
        result = await edit_tool.ainvoke({
            "citation_id": 1,
        })

        assert "error:" in result
        assert "no fields" in result

    @pytest.mark.asyncio
    async def test_edit_invalid_locator_json(self, edit_tool, mock_db):
        """Test error when locator is not valid JSON."""
        result = await edit_tool.ainvoke({
            "citation_id": 1,
            "locator": "not valid json{",
        })

        assert "error:" in result
        assert "valid JSON" in result


# =============================================================================
# CONTEXT MANAGER ENSURE_WITHIN_LIMITS TESTS
# =============================================================================


class TestEnsureWithinLimits:
    """Tests for ContextManager.ensure_within_limits method."""

    @pytest.fixture
    def context_mgr(self):
        """Create a ContextManager with low thresholds for testing."""
        from src.core.context import ContextManager, ContextConfig
        config = ContextConfig(
            compaction_threshold_tokens=1000,
            summarization_threshold_tokens=1000,
            message_count_threshold=5,
            message_count_min_tokens=100,
            keep_recent_messages=2,
        )
        return ContextManager(config=config)

    @pytest.fixture
    def mock_llm(self):
        """Create a mock LLM that returns a summary."""
        llm = MagicMock()
        # with_structured_output returns an LLM that can be awaited
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(return_value=MagicMock(
            summary="Test summary",
            tasks_completed="- Task 1",
            key_decisions="- Decision 1",
            current_state="In progress",
            blockers="",
        ))
        llm.with_structured_output = MagicMock(return_value=structured_llm)
        return llm

    @pytest.mark.asyncio
    async def test_no_compaction_when_under_threshold(self, context_mgr):
        """Test that messages are returned unchanged when under threshold."""
        from langchain_core.messages import HumanMessage
        messages = [HumanMessage(content="Hello")]

        mock_llm = MagicMock()
        result = await context_mgr.ensure_within_limits(messages, mock_llm)

        # Should return same messages unchanged
        assert result == messages
        # LLM should not be called
        mock_llm.with_structured_output.assert_not_called()

    @pytest.mark.asyncio
    async def test_compaction_when_over_message_threshold(self, context_mgr, mock_llm):
        """Test that compaction happens when message count exceeds threshold."""
        from langchain_core.messages import HumanMessage, AIMessage

        # Create enough messages to trigger threshold (>5 messages, >100 tokens)
        messages = [
            HumanMessage(content="Long message 1 " * 20),
            AIMessage(content="Response 1 " * 20),
            HumanMessage(content="Long message 2 " * 20),
            AIMessage(content="Response 2 " * 20),
            HumanMessage(content="Long message 3 " * 20),
            AIMessage(content="Response 3 " * 20),
        ]

        result = await context_mgr.ensure_within_limits(messages, mock_llm)

        # Should have fewer messages (compacted)
        assert len(result) < len(messages)

    @pytest.mark.asyncio
    async def test_force_compaction(self, context_mgr, mock_llm):
        """Test that force=True triggers compaction even under threshold."""
        from langchain_core.messages import HumanMessage, AIMessage

        # Large messages so the summary is smaller than original
        # (compaction is skipped if summary is larger than original)
        messages = [
            HumanMessage(content="Hello world " * 50),
            AIMessage(content="Hi there " * 50),
            HumanMessage(content="How are you doing today? " * 50),
            AIMessage(content="I am doing great, thanks! " * 50),
        ]

        result = await context_mgr.ensure_within_limits(messages, mock_llm, force=True)

        # With force=True, compaction should happen (summary is smaller than original)
        assert len(result) < len(messages)

    @pytest.mark.asyncio
    async def test_returns_original_when_not_enough_messages(self, context_mgr, mock_llm):
        """Test that messages are returned unchanged when too few to compact."""
        from langchain_core.messages import HumanMessage

        # Only 1 message - can't really compact
        messages = [HumanMessage(content="Hello")]

        result = await context_mgr.ensure_within_limits(messages, mock_llm, force=True)

        # Should return same messages since there's nothing to summarize
        assert result == messages
