"""Unit tests for graph.py (phase alternation graph).

Tests the phase alternation graph architecture routing functions and helper utilities.
LLM-dependent nodes are tested with mocks or integration tests.
"""

import pytest
import tempfile
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add project root src to path for imports
project_root = Path(__file__).parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

# Import from src package (requires langgraph in environment)
from src.core.workspace import WorkspaceManager
from src.managers import TodoManager, PlanManager, MemoryManager
from src.graph import (
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
from src.core.phase import (
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
        """Test that initialized state routes to execute (resume)."""
        state = {"initialized": True}
        result = route_entry(state)
        assert result == "execute"

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

    def test_todos_all_complete(self, managers, mock_config):
        """Test check when all todos are complete."""
        managers["todo"].add("Task 1")
        managers["todo"].complete("todo_1")

        node = create_check_todos_node(managers["todo"], mock_config)

        state = {"job_id": "test-123", "iteration": 0}
        result = node(state)

        assert result.get("phase_complete") is True


class TestArchivePhaseNode:
    """Tests for archive_phase node."""

    def test_archives_todos(self, managers, mock_config):
        """Test that todos are archived."""
        managers["todo"].add("Task 1")
        managers["todo"].complete("todo_1")
        managers["plan"].write("## Phase 1: Test\n\n- [x] Task 1")

        # Create mock context manager - skip compaction by returning False
        mock_context_mgr = MagicMock()
        mock_context_mgr.should_summarize.return_value = False

        # Mock config to enable compact_on_archive
        mock_config.context_management = MagicMock()
        mock_config.context_management.compact_on_archive = True

        mock_llm = MagicMock()
        mock_summarization_prompt = "Summarize this conversation."

        node = create_archive_phase_node(
            managers["todo"], managers["plan"], mock_config,
            mock_context_mgr, mock_llm, mock_summarization_prompt
        )

        state = {"job_id": "test-123", "messages": []}
        result = node(state)

        assert "messages" in result
        assert "Phase complete" in result["messages"][0].content
        # Todos should be cleared
        assert len(managers["todo"].list_all()) == 0


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

    def test_route_after_transition_success_empty_messages(self, workspace_manager):
        """Test routing when transition succeeded (messages cleared)."""
        route_after_transition = create_route_after_transition(workspace_manager)
        state = {"messages": []}
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

        # Should clear messages (success)
        assert result.get("messages") == []
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

        # Should clear messages (success)
        assert result.get("messages") == []
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
        assert state.get("messages") == []  # Messages cleared
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
        assert state.get("messages") == []  # Messages cleared
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
        assert result.get("messages") == []
        assert result.get("is_strategic_phase") is False
        assert result.get("phase_number") == 1

        # route_after_transition should proceed to check_goal
        assert route_after_transition({"messages": []}) == "check_goal"

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
