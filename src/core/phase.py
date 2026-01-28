"""Phase utilities for strategic/tactical phase alternation.

This module provides utilities for the phase alternation architecture:
- Predefined strategic todos for job start and phase transitions
- todos.yaml schema validation for phase handoffs
- Phase transition logic (strategic -> tactical, tactical -> strategic)

The phase alternation model uses a single ReAct loop that alternates
between strategic (planning) and tactical (execution) phases. Strategic
phases use predefined todos, while tactical phases use agent-created
todos from todos.yaml.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import yaml
from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from ..core.loader import AgentConfig
    from ..core.state import UniversalAgentState
    from ..core.workspace import WorkspaceManager
    from ..managers.todo import TodoManager

logger = logging.getLogger(__name__)


@dataclass
class PredefinedTodo:
    """A predefined todo item for strategic phases.

    Unlike TodoItem in managers/todo.py, this is a lightweight
    structure for the predefined todos loaded at phase start.
    """

    id: int
    content: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for TodoManager compatibility."""
        return {
            "id": f"todo_{self.id}",
            "content": self.content,
            "status": "pending",
            "priority": "medium",
        }


def get_initial_strategic_todos(
    config: Optional["AgentConfig"] = None,
) -> List[PredefinedTodo]:
    """Get todos for the first strategic phase (job start).

    These todos guide the agent through initial workspace setup,
    plan creation, and first phase todo generation.

    Loads from strategic_todos_initial.yaml template with deployment override support.
    Falls back to hardcoded defaults if template not found.

    Args:
        config: Agent configuration for deployment directory. If None, uses
               framework defaults only.

    Returns:
        List of PredefinedTodo items for job initialization
    """
    from ..core.loader import get_initial_strategic_todos_from_config

    # Try to load from template
    todo_list = get_initial_strategic_todos_from_config(config)

    if todo_list:
        # Convert from TodoManager format to PredefinedTodo
        return [
            PredefinedTodo(
                id=int(t["id"].replace("todo_", "")),
                content=t["content"],
            )
            for t in todo_list
        ]

    # Fallback to hardcoded defaults (for backward compatibility)
    logger.warning("Using hardcoded initial strategic todos (template not found)")
    return [
        PredefinedTodo(
            id=1,
            content=(
                "Explore the workspace and populate workspace.md with an overview "
                "of the environment, available tools, and any existing context."
            ),
        ),
        PredefinedTodo(
            id=2,
            content=(
                "Read the instructions.md file and create an execution plan in "
                "plan.md. The plan should outline the phases needed to "
                "complete the task."
            ),
        ),
        PredefinedTodo(
            id=3,
            content=(
                "Divide the plan into phases, where each phase contains 5-20 "
                "concrete, actionable todos."
            ),
        ),
        PredefinedTodo(
            id=4,
            content=(
                "Create todos for the first tactical phase using the "
                "next_phase_todos tool."
            ),
        ),
    ]


def get_transition_strategic_todos(
    config: Optional["AgentConfig"] = None,
) -> List[PredefinedTodo]:
    """Get todos for strategic phases between tactical phases.

    These todos guide the agent through summarizing the previous
    phase, updating memory, and planning the next phase.

    Loads from strategic_todos_transition.yaml template with deployment override support.
    Falls back to hardcoded defaults if template not found.

    Args:
        config: Agent configuration for deployment directory. If None, uses
               framework defaults only.

    Returns:
        List of PredefinedTodo items for phase transitions
    """
    from ..core.loader import get_transition_strategic_todos_from_config

    # Try to load from template
    todo_list = get_transition_strategic_todos_from_config(config)

    if todo_list:
        # Convert from TodoManager format to PredefinedTodo
        return [
            PredefinedTodo(
                id=int(t["id"].replace("todo_", "")),
                content=t["content"],
            )
            for t in todo_list
        ]

    # Fallback to hardcoded defaults (for backward compatibility)
    logger.warning("Using hardcoded transition strategic todos (template not found)")
    return [
        PredefinedTodo(
            id=1,
            content=(
                "Summarize what was accomplished in the previous tactical phase. "
                "Note any issues encountered, decisions made, or discoveries."
            ),
        ),
        PredefinedTodo(
            id=2,
            content=(
                "Update workspace.md with new learnings, patterns discovered, "
                "or important context for future phases."
            ),
        ),
        PredefinedTodo(
            id=3,
            content=(
                "Update plan.md to mark completed phases and adjust "
                "upcoming phases if needed based on learnings."
            ),
        ),
        PredefinedTodo(
            id=4,
            content=(
                "Create todos for the next tactical phase using next_phase_todos, "
                "or call job_complete if the plan is fully executed."
            ),
        ),
    ]


# =============================================================================
# todos.yaml Schema and Validation
# =============================================================================


class TodosYamlValidationError(Exception):
    """Raised when todos.yaml validation fails."""

    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or [message]


def validate_todos_yaml(
    content: str,
    min_todos: int = 5,
    max_todos: int = 20,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Validate todos.yaml content and extract todos.

    Expected schema:
    ```yaml
    phase: "Phase 1: Description"  # Optional
    description: "What this phase does"  # Optional
    todos:
      - id: 1
        content: "First task description"
      - id: 2
        content: "Second task description"
    ```

    Args:
        content: Raw YAML content string
        min_todos: Minimum number of todos required (default: 5)
        max_todos: Maximum number of todos allowed (default: 20)

    Returns:
        Tuple of (metadata dict, list of todo dicts)
        - metadata: {phase, description} if present
        - todos: [{id, content}, ...] validated todo items

    Raises:
        TodosYamlValidationError: If validation fails
    """
    errors: List[str] = []

    # Parse YAML
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise TodosYamlValidationError(
            f"Invalid YAML syntax: {e}",
            [f"YAML parse error: {e}"],
        )

    if data is None:
        raise TodosYamlValidationError(
            "Empty todos.yaml file",
            ["File is empty or contains only whitespace"],
        )

    if not isinstance(data, dict):
        raise TodosYamlValidationError(
            "todos.yaml must be a YAML mapping",
            [f"Expected mapping, got {type(data).__name__}"],
        )

    # Check required 'todos' key
    if "todos" not in data:
        raise TodosYamlValidationError(
            "Missing required 'todos' key",
            ["todos.yaml must have a 'todos' key with a list of todo items"],
        )

    todos_raw = data["todos"]
    if not isinstance(todos_raw, list):
        raise TodosYamlValidationError(
            "'todos' must be a list",
            [f"Expected list for 'todos', got {type(todos_raw).__name__}"],
        )

    # Validate todo count
    todo_count = len(todos_raw)
    if todo_count < min_todos:
        errors.append(
            f"Too few todos: {todo_count} < {min_todos}. "
            f"Create more detailed, actionable tasks."
        )
    if todo_count > max_todos:
        errors.append(
            f"Too many todos: {todo_count} > {max_todos}. "
            f"Group related tasks or split into multiple phases."
        )

    # Validate each todo item
    validated_todos: List[Dict[str, Any]] = []
    seen_ids: set = set()

    for i, item in enumerate(todos_raw):
        if not isinstance(item, dict):
            errors.append(f"Todo #{i + 1}: Expected mapping, got {type(item).__name__}")
            continue

        # Validate 'id'
        todo_id = item.get("id")
        if todo_id is None:
            errors.append(f"Todo #{i + 1}: Missing required 'id' field")
        elif not isinstance(todo_id, int):
            errors.append(
                f"Todo #{i + 1}: 'id' must be an integer, got {type(todo_id).__name__}"
            )
        elif todo_id in seen_ids:
            errors.append(f"Todo #{i + 1}: Duplicate id '{todo_id}'")
        else:
            seen_ids.add(todo_id)

        # Validate 'content'
        content_val = item.get("content")
        if content_val is None:
            errors.append(f"Todo #{i + 1}: Missing required 'content' field")
        elif not isinstance(content_val, str):
            errors.append(
                f"Todo #{i + 1}: 'content' must be a string, "
                f"got {type(content_val).__name__}"
            )
        elif len(content_val.strip()) < 10:
            errors.append(
                f"Todo #{i + 1}: 'content' too short ({len(content_val.strip())} chars). "
                f"Provide a meaningful task description."
            )

        # If valid so far, add to validated list
        if todo_id is not None and content_val is not None:
            validated_todos.append({
                "id": todo_id,
                "content": content_val.strip(),
            })

    if errors:
        raise TodosYamlValidationError(
            f"todos.yaml validation failed with {len(errors)} error(s)",
            errors,
        )

    # Extract optional metadata
    metadata = {}
    if "phase" in data:
        metadata["phase"] = str(data["phase"])
    if "description" in data:
        metadata["description"] = str(data["description"])

    logger.info(f"Validated todos.yaml: {len(validated_todos)} todos")
    return metadata, validated_todos


def load_todos_from_yaml(
    workspace_path: Path,
    min_todos: int = 5,
    max_todos: int = 20,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Load and validate todos from workspace/todos.yaml.

    Convenience function that reads the file and validates it.

    Args:
        workspace_path: Path to workspace directory
        min_todos: Minimum todos required
        max_todos: Maximum todos allowed

    Returns:
        Tuple of (metadata dict, list of todo dicts)

    Raises:
        TodosYamlValidationError: If file doesn't exist or validation fails
    """
    todos_path = workspace_path / "todos.yaml"

    if not todos_path.exists():
        raise TodosYamlValidationError(
            "todos.yaml not found",
            [f"Expected file at: {todos_path}"],
        )

    content = todos_path.read_text()
    return validate_todos_yaml(content, min_todos, max_todos)


# =============================================================================
# Phase Transition Logic
# =============================================================================



@dataclass
class TransitionResult:
    """Result of a phase transition attempt.

    Attributes:
        success: Whether the transition was successful
        state_updates: Dictionary of state updates to apply
        error_message: Error message if transition failed (None if success)
    """

    success: bool
    state_updates: Dict[str, Any]
    error_message: Optional[str] = None


def reject_transition(
    state: "UniversalAgentState",
    reason: str,
) -> TransitionResult:
    """Reject a phase transition and return an error result.

    This is called when validation fails during a transition attempt.
    The last todo is kept incomplete so the agent can fix the issue.

    Args:
        state: Current agent state
        reason: Human-readable explanation of why transition was rejected

    Returns:
        TransitionResult with success=False and error message
    """
    from langchain_core.messages import ToolMessage

    logger.warning(f"Phase transition rejected: {reason}")

    # Create error message for the conversation
    error_msg = ToolMessage(
        content=f"[TRANSITION_REJECTED] Phase transition rejected: {reason}\n\n"
        "Please fix the issue and try again.",
        tool_call_id="phase_transition",
    )

    return TransitionResult(
        success=False,
        state_updates={
            "messages": [error_msg],
            # Don't clear messages or change phase on rejection
        },
        error_message=reason,
    )


def on_strategic_phase_complete(
    state: "UniversalAgentState",
    workspace: "WorkspaceManager",
    todo_manager: "TodoManager",
    min_todos: int = 5,
    max_todos: int = 20,
) -> TransitionResult:
    """Handle transition from strategic phase to tactical phase.

    This function is called when the last strategic todo is completed.
    It checks for staged todos and, if present, transitions to tactical phase.

    The new flow uses staged todos instead of todos.yaml:
    1. Agent calls next_phase_todos() to stage todos
    2. Agent calls todo_complete() to finish strategic phase
    3. This function checks for staged todos and applies them

    On success:
    - Injects phase boundary marker message
    - Applies staged todos to TodoManager
    - Flips to tactical phase
    - Increments phase_number

    On failure:
    - Returns error message for agent to fix
    - Does not change phase

    Args:
        state: Current agent state
        workspace: WorkspaceManager for file access
        todo_manager: TodoManager for loading todos
        min_todos: Minimum todos required (default: 5, used by staging)
        max_todos: Maximum todos allowed (default: 20, used by staging)

    Returns:
        TransitionResult indicating success/failure with state updates
    """
    job_id = state.get("job_id", "unknown")
    phase_number = state.get("phase_number", 0)

    logger.info(f"[{job_id}] Strategic phase complete, checking for staged todos")

    # Check if there are staged todos
    if not todo_manager.has_staged_todos():
        return reject_transition(
            state,
            "No todos staged for the next phase. "
            "Use next_phase_todos tool to create 5-20 tasks first.",
        )

    # Get phase name from staged todos
    phase_name = todo_manager.get_staged_phase_name() or f"Phase {phase_number + 1}"

    # Apply staged todos to the active todo list
    todo_manager.apply_staged_todos()
    todo_count = len(todo_manager.list_all())

    logger.info(
        f"[{job_id}] Transitioning to tactical phase: {phase_name} "
        f"({todo_count} todos)"
    )

    phase_marker = HumanMessage(
        content=(
            f"[PHASE_TRANSITION] Strategic phase complete. "
            f"Entering tactical phase {phase_number + 1}: {phase_name} "
            f"({todo_count} todos). Work through the todos using your tools."
        )
    )

    return TransitionResult(
        success=True,
        state_updates={
            "messages": [phase_marker],
            "is_strategic_phase": False,
            "phase_number": phase_number + 1,
            "phase_complete": False,
        },
    )


def on_tactical_phase_complete(
    state: "UniversalAgentState",
    workspace: "WorkspaceManager",
    todo_manager: "TodoManager",
    config: Optional["AgentConfig"] = None,
) -> TransitionResult:
    """Handle transition from tactical phase to strategic phase.

    This function is called when all tactical todos are completed.
    It transitions to strategic phase with predefined todos.

    On success:
    - Injects phase boundary marker message
    - Loads predefined strategic todos
    - Flips to strategic phase
    - Increments phase_number

    Args:
        state: Current agent state
        workspace: WorkspaceManager for file access
        todo_manager: TodoManager for archiving and loading todos
        config: Agent configuration for loading strategic todos from template

    Returns:
        TransitionResult with state updates for strategic phase
    """
    job_id = state.get("job_id", "unknown")
    phase_number = state.get("phase_number", 0)

    logger.info(f"[{job_id}] Tactical phase complete, transitioning to strategic")

    # Load predefined strategic todos (from config template or defaults)
    strategic_todos = get_transition_strategic_todos(config)
    todo_list = [todo.to_dict() for todo in strategic_todos]
    todo_manager.set_todos_from_list(todo_list)

    logger.info(
        f"[{job_id}] Transitioning to strategic phase "
        f"({len(strategic_todos)} predefined todos)"
    )

    phase_marker = HumanMessage(
        content=(
            f"[PHASE_TRANSITION] Tactical phase complete. "
            f"Entering strategic phase {phase_number + 1}. "
            f"Review what was accomplished, update workspace.md and plan.md, "
            f"then create todos for the next tactical phase or call job_complete."
        )
    )

    return TransitionResult(
        success=True,
        state_updates={
            "messages": [phase_marker],
            "is_strategic_phase": True,
            "phase_number": phase_number + 1,
            "phase_complete": False,
        },
    )


def handle_phase_transition(
    state: "UniversalAgentState",
    workspace: "WorkspaceManager",
    todo_manager: "TodoManager",
    min_todos: int = 5,
    max_todos: int = 20,
    config: Optional["AgentConfig"] = None,
) -> TransitionResult:
    """Route to the appropriate phase transition handler.

    This is the main entry point for phase transitions. It checks
    the current phase and delegates to the appropriate handler.

    Args:
        state: Current agent state
        workspace: WorkspaceManager for file access
        todo_manager: TodoManager for todo operations
        min_todos: Minimum todos for strategic->tactical transition
        max_todos: Maximum todos for strategic->tactical transition
        config: Agent configuration for loading strategic todos from template

    Returns:
        TransitionResult from the appropriate handler
    """
    is_strategic = state.get("is_strategic_phase", True)

    if is_strategic:
        return on_strategic_phase_complete(
            state, workspace, todo_manager, min_todos, max_todos
        )
    else:
        return on_tactical_phase_complete(state, workspace, todo_manager, config)
