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
import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from datetime import datetime

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
    tool_names: Optional[List[str]] = None,
) -> List[PredefinedTodo]:
    """Get todos for the first strategic phase (job start).

    These todos guide the agent through initial workspace setup,
    plan creation, and first phase todo generation.

    Loads from strategic_todos_initial.yaml template with deployment override support.
    Falls back to hardcoded defaults if template not found.

    Args:
        config: Agent configuration for deployment directory. If None, uses
               framework defaults only.
        tool_names: List of loaded tool names for Jinja2 template rendering.

    Returns:
        List of PredefinedTodo items for job initialization
    """
    from ..core.loader import get_initial_strategic_todos_from_config

    # Try to load from template
    todo_list = get_initial_strategic_todos_from_config(config, tool_names=tool_names)

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
    tool_names: Optional[List[str]] = None,
) -> List[PredefinedTodo]:
    """Get todos for strategic phases between tactical phases.

    These todos guide the agent through summarizing the previous
    phase, updating memory, and planning the next phase.

    Loads from strategic_todos_transition.yaml template with deployment override support.
    Falls back to hardcoded defaults if template not found.

    Args:
        config: Agent configuration for deployment directory. If None, uses
               framework defaults only.
        tool_names: List of loaded tool names for Jinja2 template rendering.

    Returns:
        List of PredefinedTodo items for phase transitions
    """
    from ..core.loader import get_transition_strategic_todos_from_config

    # Try to load from template
    todo_list = get_transition_strategic_todos_from_config(config, tool_names=tool_names)

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


def get_resume_strategic_todos(
    config: Optional["AgentConfig"] = None,
    tool_names: Optional[List[str]] = None,
) -> List[PredefinedTodo]:
    """Get todos for the resume-from-feedback strategic phase.

    These todos guide the agent through processing human feedback,
    evaluating outputs, adapting the plan, and creating corrective todos.

    Loads from strategic_todos_resume.yaml template with deployment override support.
    Falls back to hardcoded defaults if template not found.

    Args:
        config: Agent configuration for deployment directory. If None, uses
               framework defaults only.
        tool_names: List of loaded tool names for Jinja2 template rendering.

    Returns:
        List of PredefinedTodo items for feedback-driven resume
    """
    from ..core.loader import get_resume_strategic_todos_from_config

    # Try to load from template
    todo_list = get_resume_strategic_todos_from_config(config, tool_names=tool_names)

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
    logger.warning("Using hardcoded resume strategic todos (template not found)")
    return [
        PredefinedTodo(
            id=1,
            content=(
                "Process the human feedback: read the feedback message and feedback.md, "
                "categorize each item, and update workspace.md with a feedback summary."
            ),
        ),
        PredefinedTodo(
            id=2,
            content=(
                "Evaluate existing output files against the feedback. "
                "Check which files need minor edits, major rework, or rewrite."
            ),
        ),
        PredefinedTodo(
            id=3,
            content=(
                "Rewrite plan.md with corrective phases ordered by feedback severity. "
                "Each phase must trace to specific feedback items."
            ),
        ),
        PredefinedTodo(
            id=4,
            content=(
                "Create corrective todos using next_phase_todos. Each todo must "
                "reference specific feedback items and files. Do NOT call job_complete "
                "— corrections have not been made yet."
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



def should_freeze_at_boundary(config: "AgentConfig", is_strategic: bool, phase_number: int) -> bool:
    """Determine whether the agent should freeze at a phase boundary.

    Checks the autonomy level from config and the current phase context
    to decide if the agent should pause for human review.

    Args:
        config: Agent configuration with autonomy level
        is_strategic: True if the completing phase is strategic
        phase_number: Current phase number (1-based after first strategic)

    Returns:
        True if the agent should freeze at this boundary
    """
    autonomy = getattr(config, "autonomy", "partial")
    if autonomy == "full":
        return False
    if autonomy == "review":
        return False
    if autonomy == "partial":
        return is_strategic and phase_number <= 1
    if autonomy == "guided":
        return is_strategic
    if autonomy == "dependent":
        return True
    return False


def freeze_for_review(
    state: "UniversalAgentState",
    workspace: "WorkspaceManager",
    todo_manager: "TodoManager",
    phase_type: str,
    phase_number: int,
) -> "TransitionResult":
    """Freeze the job at a phase boundary for human review.

    This is a lighter version of finalize_job() — it pauses execution
    without requiring job_complete data (summary/deliverables/confidence).

    Actions:
    - Write output/job_frozen.json with freeze_type="phase_boundary"
    - Git commit + push
    - Return TransitionResult with should_stop=True but NOT goal_achieved=True

    Args:
        state: Current agent state
        workspace: WorkspaceManager for file access
        todo_manager: TodoManager (for archiving)
        phase_type: "strategic" or "tactical"
        phase_number: Current phase number

    Returns:
        TransitionResult with should_stop=True to pause the agent loop
    """
    job_id = state.get("job_id", "unknown")

    freeze_data = {
        "status": "pending_review",
        "freeze_type": "phase_boundary",
        "timestamp": datetime.now().isoformat(),
        "phase_type": phase_type,
        "phase_number": phase_number,
        "job_id": job_id,
    }

    # Write to output/job_frozen.json
    output_path = "output/job_frozen.json"
    workspace.write_file(
        output_path,
        json.dumps(freeze_data, indent=2, ensure_ascii=False)
    )

    logger.info(
        f"[{job_id}] JOB FROZEN at phase boundary: "
        f"{phase_type} phase {phase_number}"
    )

    # Git commit and push
    git_mgr = workspace.git_manager
    if git_mgr and git_mgr.is_active:
        try:
            git_mgr.commit(
                f"Frozen at {phase_type} phase {phase_number} boundary",
                allow_empty=True,
            )
            git_mgr.push()
        except Exception as e:
            logger.warning(f"[{job_id}] Git push failed at freeze: {e}")

    # Export todo state BEFORE archiving so staged todos survive in checkpoint.
    # archive() clears _todos but preserves _staged_todos — we need both in the
    # checkpoint so that on resume the agent can apply staged tactical todos
    # instead of falling into the recovery/re-planning path.
    todo_state = todo_manager.export_state() if todo_manager else {}

    # Archive todos if any remain
    if todo_manager:
        todo_manager.archive(f"phase_{phase_number}_{phase_type}")

    # Create freeze message
    freeze_msg = HumanMessage(
        content=(
            f"[JOB_FROZEN] Job paused for human review at {phase_type} phase "
            f"{phase_number} boundary.\n"
            f"Wrote: {output_path}\n\n"
            f"The job has been paused for human review. A human operator can:\n"
            f"  - Approve: python agent.py --config <config> --job-id {job_id} --approve\n"
            f"  - Resume:  python agent.py --config <config> --job-id {job_id} --resume --feedback '...'"
        )
    )

    return TransitionResult(
        success=True,
        state_updates={
            "messages": [freeze_msg],
            "goal_achieved": False,
            "should_stop": True,
            "todos": todo_state.get("todos", []),
            "staged_todos": todo_state.get("staged_todos", []),
            "todo_next_id": todo_state.get("next_id", 1),
        },
        freeze_data=freeze_data,
    )


@dataclass
class TransitionResult:
    """Result of a phase transition attempt.

    Attributes:
        success: Whether the transition was successful
        state_updates: Dictionary of state updates to apply
        error_message: Error message if transition failed (None if success)
        freeze_data: JSON-serializable freeze/completion data for DB storage
    """

    success: bool
    state_updates: Dict[str, Any]
    error_message: Optional[str] = None
    freeze_data: Optional[Dict[str, Any]] = None


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
    from langchain_core.messages import HumanMessage

    logger.warning(f"Phase transition rejected: {reason}")

    # Create error message for the conversation as HumanMessage.
    # Previously this was a ToolMessage with a synthetic tool_call_id, but
    # sanitize_message_history() would remove it as an orphan (no parent
    # AIMessage), which could leave consecutive AIMessages in the history
    # and trigger "Cannot have 2+ assistant messages at the end" errors
    # from strict LLM APIs (e.g., vLLM).
    error_msg = HumanMessage(
        content=f"[TRANSITION_REJECTED] Phase transition rejected: {reason}\n\n"
        "Please fix the issue and try again.",
    )

    return TransitionResult(
        success=False,
        state_updates={
            "messages": [error_msg],
            # Don't clear messages or change phase on rejection
        },
        error_message=reason,
    )


def _finalize_with_verdict(
    state: "UniversalAgentState",
    workspace: "WorkspaceManager",
    todo_manager: "TodoManager",
    verdict: Dict[str, Any],
    job_id: str,
) -> TransitionResult:
    """Finalize a critic job based on verdict data from evaluation tools.

    This handles the deferred verdict pattern: evaluation tools store
    verdict intent, and this function produces the correct freeze_data
    and TransitionResult for handle_transition to process.

    Args:
        state: Current agent state
        workspace: WorkspaceManager for file access
        todo_manager: TodoManager (for archiving)
        verdict: Verdict data from evaluation tools
        job_id: Critic job UUID

    Returns:
        TransitionResult with verdict-specific freeze_data
    """
    verdict_type = verdict.get("_verdict")
    target_job_id = verdict.get("_target_job_id", "unknown")

    if verdict_type == "approved":
        freeze_data = {
            "status": "completed",
            "freeze_type": "verdict",
            "verdict": "approved",
            "target_job_id": target_job_id,
            "report": verdict.get("report", ""),
            "strengths": verdict.get("strengths", []),
            "minor_notes": verdict.get("minor_notes", []),
            "timestamp": datetime.now().isoformat(),
            "job_id": job_id,
        }
        goal_achieved = True
        log_msg = f"[{job_id}] Critic verdict: APPROVED target job {target_job_id}"

    elif verdict_type == "returned":
        freeze_data = {
            "status": "waiting",
            "freeze_type": "verdict",
            "verdict": "returned",
            "target_job_id": target_job_id,
            "feedback": verdict.get("_feedback", ""),
            "feedback_raw": verdict.get("feedback_raw", ""),
            "issues": verdict.get("issues", []),
            "severity": verdict.get("severity", "medium"),
            "timestamp": datetime.now().isoformat(),
            "job_id": job_id,
        }
        goal_achieved = False
        log_msg = f"[{job_id}] Critic verdict: RETURNED target job {target_job_id} with feedback"

    else:
        # Unknown verdict type — warn and fall through to normal completion
        logger.warning(
            f"[{job_id}] Unknown verdict type '{verdict_type}', "
            f"treating as normal completion"
        )
        freeze_data = {
            "status": "completed",
            "freeze_type": "verdict",
            "verdict": verdict_type,
            "target_job_id": target_job_id,
            "timestamp": datetime.now().isoformat(),
            "job_id": job_id,
        }
        goal_achieved = True
        log_msg = f"[{job_id}] Critic verdict: {verdict_type} (unknown, defaulting to completed)"

    logger.info(log_msg)

    # Write verdict file to workspace
    if workspace:
        workspace.write_file(
            "output/critic_verdict.json",
            json.dumps(freeze_data, indent=2, ensure_ascii=False),
        )

    # Git commit
    git_mgr = workspace.git_manager if workspace else None
    if git_mgr and git_mgr.is_active:
        try:
            git_mgr.commit(
                f"Critic verdict: {verdict_type} for target {target_job_id}",
                allow_empty=True,
            )
            git_mgr.push()
        except Exception as e:
            logger.warning(f"[{job_id}] Git push failed at verdict: {e}")

    # Archive todos
    if todo_manager:
        todo_manager.archive("final")

    verdict_msg = HumanMessage(
        content=(
            f"[CRITIC_VERDICT] Verdict: {verdict_type} for target job {target_job_id}.\n"
            f"Wrote: output/critic_verdict.json"
        )
    )

    return TransitionResult(
        success=True,
        state_updates={
            "messages": [verdict_msg],
            "goal_achieved": goal_achieved,
            "should_stop": True,
            "is_final_phase": False,
        },
        freeze_data=freeze_data,
    )


def finalize_job(
    state: "UniversalAgentState",
    workspace: "WorkspaceManager",
    todo_manager: "TodoManager",
    config: Optional["AgentConfig"] = None,
) -> TransitionResult:
    """Finalize the job after job_complete is called.

    Behavior depends on autonomy level:
    - full: Write job_completion.json directly, auto-complete (no freeze)
    - All others: Write job_frozen.json with freeze_type="job_complete"

    Actions:
    - Write output file (job_completion.json or job_frozen.json)
    - Return TransitionResult with should_stop=True

    Note: The database status update is handled by the async
    handle_transition node in graph.py.

    Args:
        state: Current agent state
        workspace: WorkspaceManager for file access
        todo_manager: TodoManager (for archiving)
        config: Agent configuration (for autonomy level)

    Returns:
        TransitionResult with should_stop=True to end the agent loop
    """
    from ..tools.core.job import get_final_phase_data, clear_final_phase_data
    from ..tools.evaluation.evaluation_tools import get_verdict_data, clear_verdict_data

    job_id = state.get("job_id", "unknown")
    autonomy = getattr(config, "autonomy", "partial") if config else "partial"

    # Check for deferred verdict data (from critic evaluation tools)
    verdict = get_verdict_data(job_id)
    if verdict:
        clear_verdict_data(job_id)
        # Also clear final_phase_data if set (evaluation tools set it to trigger finalize_job)
        clear_final_phase_data(job_id)
        return _finalize_with_verdict(state, workspace, todo_manager, verdict, job_id)

    # Edge case: critic job reached finalize_job without verdict data.
    # This means the critic called job_complete instead of an evaluation tool.
    # Treat as implicit approval so the target job doesn't get stuck in "reviewing".
    metadata = state.get("metadata") or {}
    if metadata.get("verification_target"):
        target_job_id = metadata["verification_target"]
        logger.warning(
            f"[{job_id}] Critic job finalizing WITHOUT verdict data. "
            f"The critic called job_complete instead of approve_job/return_job_with_feedback. "
            f"Synthesizing implicit approval for target job {target_job_id}."
        )
        final_data = get_final_phase_data(job_id)
        clear_final_phase_data(job_id)
        implicit_verdict = {
            "_verdict": "approved",
            "_target_job_id": target_job_id,
            "report": (final_data or {}).get("summary", "Critic completed without explicit verdict — implicit approval."),
            "strengths": [],
            "minor_notes": ["Critic used job_complete instead of approve_job — treated as implicit approval."],
        }
        return _finalize_with_verdict(state, workspace, todo_manager, implicit_verdict, job_id)

    # Get the final phase data (set by job_complete tool)
    final_data = get_final_phase_data(job_id)

    if not final_data:
        # Fallback data if not found (shouldn't happen)
        logger.warning(f"[{job_id}] No final phase data found, using defaults")
        final_data = {
            "summary": "Job completed",
            "deliverables": [],
            "confidence": 1.0,
            "job_id": job_id,
        }

    # Clear the final phase data
    clear_final_phase_data(job_id)

    if autonomy == "full":
        # Full autonomy: auto-complete without freezing
        completion_data = {
            "status": "job_completed",
            "timestamp": datetime.now().isoformat(),
            "summary": final_data.get("summary", "Job completed"),
            "deliverables": final_data.get("deliverables", []),
            "confidence": final_data.get("confidence", 1.0),
            "job_id": job_id,
        }
        if "notes" in final_data:
            completion_data["notes"] = final_data["notes"]

        output_path = "output/job_completion.json"
        workspace.write_file(
            output_path,
            json.dumps(completion_data, indent=2, ensure_ascii=False)
        )

        logger.info(f"[{job_id}] JOB AUTO-COMPLETED (autonomy=full): {completion_data['summary']}")
        logger.info(f"[{job_id}] Deliverables: {completion_data['deliverables']}")

        # Final git commit and push
        git_mgr = workspace.git_manager
        if git_mgr and git_mgr.is_active:
            try:
                git_mgr.commit("Job completed (autonomy=full)", allow_empty=True)
                phase_num = state.get("phase_number", 0)
                short_id = job_id[:8]
                git_mgr.tag(f"{short_id}-job-completed-phase-{phase_num}", "Job auto-completed (full autonomy)")
                git_mgr.push()
            except Exception as e:
                logger.warning(f"[{job_id}] Final git push failed: {e}")

        # Archive todos
        if todo_manager:
            todo_manager.archive("final")

        completion_msg = HumanMessage(
            content=(
                f"[JOB_COMPLETED] Job auto-completed (autonomy=full).\n"
                f"Wrote: {output_path}\n"
                f"Summary: {completion_data['summary']}\n"
                f"Deliverables: {len(completion_data['deliverables'])} files\n"
                f"Confidence: {completion_data['confidence']:.0%}"
            )
        )

        return TransitionResult(
            success=True,
            state_updates={
                "messages": [completion_msg],
                "goal_achieved": True,
                "should_stop": True,
                "is_final_phase": False,
            },
            freeze_data=completion_data,
        )

    # All other autonomy levels: freeze for human review
    freeze_data = {
        "status": "pending_review",
        "freeze_type": "job_complete",
        "timestamp": datetime.now().isoformat(),
        "summary": final_data.get("summary", "Job completed"),
        "deliverables": final_data.get("deliverables", []),
        "confidence": final_data.get("confidence", 1.0),
        "job_id": job_id,
    }

    if "notes" in final_data:
        freeze_data["notes"] = final_data["notes"]

    # Write to output/job_frozen.json
    output_path = "output/job_frozen.json"
    workspace.write_file(
        output_path,
        json.dumps(freeze_data, indent=2, ensure_ascii=False)
    )

    logger.info(f"[{job_id}] JOB FROZEN for review: {freeze_data['summary']}")
    logger.info(f"[{job_id}] Deliverables: {freeze_data['deliverables']}")

    # NOTE: Database status update to 'pending_review' is handled by the
    # async handle_transition node in graph.py, which can properly await
    # the asyncpg pool on the correct event loop.

    # Final git commit and push for workspace delivery
    git_mgr = workspace.git_manager
    if git_mgr and git_mgr.is_active:
        try:
            git_mgr.commit("Job frozen for review", allow_empty=True)
            phase_num = state.get("phase_number", 0)
            short_id = job_id[:8]
            git_mgr.tag(f"{short_id}-job-frozen-phase-{phase_num}", "Job frozen for human review")
            git_mgr.push()
        except Exception as e:
            logger.warning(f"[{job_id}] Final git push failed: {e}")

    # Archive todos if any remain
    if todo_manager:
        todo_manager.archive("final")

    # Create completion message
    completion_msg = HumanMessage(
        content=(
            f"[JOB_FROZEN] Job frozen for human review.\n"
            f"Wrote: {output_path}\n"
            f"Summary: {freeze_data['summary']}\n"
            f"Deliverables: {len(freeze_data['deliverables'])} files\n"
            f"Confidence: {freeze_data['confidence']:.0%}\n\n"
            f"The job has been paused for human review. A human operator can:\n"
            f"  - Approve: python agent.py --config <config> --job-id {job_id} --approve\n"
            f"  - Resume:  python agent.py --config <config> --job-id {job_id} --resume --feedback '...'"
        )
    )

    return TransitionResult(
        success=True,
        state_updates={
            "messages": [completion_msg],
            "goal_achieved": False,  # False = DB gets pending_review, not completed
            "should_stop": True,
            "is_final_phase": False,  # Reset for cleanliness
        },
        freeze_data=freeze_data,
    )


def _complete_phase_with_git(
    workspace: "WorkspaceManager",
    phase_number: int,
    phase_type: str,
    todos_archived: int = 0,
    job_id: str = "unknown",
) -> None:
    """Complete a phase with git operations.

    Creates a git tag for the completed phase and commits any pending changes.

    Args:
        workspace: WorkspaceManager with git_manager
        phase_number: Current phase number
        phase_type: Completed phase type ("strategic" or "tactical")
        todos_archived: Number of todos archived in this phase
        job_id: Job UUID (used to namespace tags in shared repos)
    """
    git_mgr = workspace.git_manager
    if not git_mgr or not git_mgr.is_active:
        return

    try:
        # Create tag for completed phase (namespaced by job short ID)
        short_id = job_id[:8]
        tag_name = f"{short_id}-phase-{phase_number}-{phase_type}-complete"
        git_mgr.tag(tag_name, f"Phase {phase_number} {phase_type} complete")
        logger.debug(f"Created git tag: {tag_name}")

        # Commit any pending changes from this phase
        commit_msg = (
            f"[Phase {phase_number} {phase_type.title()}] Complete - "
            f"archived {todos_archived} todos"
        )
        git_mgr.commit(commit_msg, allow_empty=True)
        logger.debug(f"Committed phase completion: {commit_msg}")

        # Push to remote for workspace delivery
        branch = git_mgr.current_branch()
        logger.debug(f"Pushing phase completion to branch: {branch}")
        git_mgr.push()

    except Exception as e:
        logger.warning(f"Git operations failed during phase transition: {e}")
        # Don't fail the transition - git is optional


def on_strategic_phase_complete(
    state: "UniversalAgentState",
    workspace: "WorkspaceManager",
    todo_manager: "TodoManager",
    min_todos: int = 5,
    max_todos: int = 20,
    config: Optional["AgentConfig"] = None,
) -> TransitionResult:
    """Handle transition from strategic phase to tactical phase.

    This function is called when the last strategic todo is completed.
    It checks for staged todos and, if present, transitions to tactical phase.

    If the phase is marked as final (via job_complete), it finalizes the job
    instead of transitioning to another phase.

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
    from ..tools.core.job import get_final_phase_data

    job_id = state.get("job_id", "unknown")
    phase_number = state.get("phase_number", 0)

    # Check if this is the final phase (job_complete was called)
    is_final = state.get("is_final_phase", False)
    final_data = get_final_phase_data(job_id)

    if is_final or final_data:
        logger.info(f"[{job_id}] Final phase detected, completing job")
        return finalize_job(state, workspace, todo_manager, config=config)

    # Check for deferred verdict data (critic evaluation tools)
    from ..tools.evaluation.evaluation_tools import get_verdict_data
    verdict_data = get_verdict_data(job_id)
    if verdict_data:
        logger.info(f"[{job_id}] Verdict detected, finalizing critic job")
        return finalize_job(state, workspace, todo_manager, config=config)

    # Check autonomy level for phase boundary freeze
    if config and should_freeze_at_boundary(config, is_strategic=True, phase_number=phase_number):
        logger.info(f"[{job_id}] Autonomy freeze at strategic phase {phase_number} boundary")
        return freeze_for_review(state, workspace, todo_manager, "strategic", phase_number)

    logger.info(f"[{job_id}] Strategic phase complete, checking for staged todos")

    # Check if there are staged todos
    if not todo_manager.has_staged_todos():
        return reject_transition(
            state,
            "No todos staged for the next phase. "
            "Use next_phase_todos tool to create 5-20 tasks first, "
            "or call job_complete if the plan is fully executed.",
        )

    # Get phase name from staged todos
    phase_name = todo_manager.get_staged_phase_name() or f"Phase {phase_number + 1}"

    # Get count of completed todos before applying new ones
    completed_todos = len([t for t in todo_manager.list_all() if t.status.value == "completed"])

    # Apply staged todos to the active todo list
    todo_manager.apply_staged_todos()
    todo_count = len(todo_manager.list_all())

    # Export todo state for checkpointing
    todo_state = todo_manager.export_state()

    # Git operations: tag completed phase, commit
    _complete_phase_with_git(
        workspace=workspace,
        phase_number=phase_number,
        phase_type="strategic",
        todos_archived=completed_todos,
        job_id=job_id,
    )

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
            "todos": todo_state["todos"],
            "staged_todos": todo_state["staged_todos"],
            "todo_next_id": todo_state["next_id"],
        },
    )


def on_tactical_phase_complete(
    state: "UniversalAgentState",
    workspace: "WorkspaceManager",
    todo_manager: "TodoManager",
    config: Optional["AgentConfig"] = None,
    tool_names: Optional[List[str]] = None,
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

    # Get count of completed todos before loading new ones
    completed_todos = len([t for t in todo_manager.list_all() if t.status.value == "completed"])

    # Git operations: tag completed phase, commit
    _complete_phase_with_git(
        workspace=workspace,
        phase_number=phase_number,
        phase_type="tactical",
        todos_archived=completed_todos,
        job_id=job_id,
    )

    # Check autonomy level for phase boundary freeze (before loading new todos)
    if config and should_freeze_at_boundary(config, is_strategic=False, phase_number=phase_number):
        logger.info(f"[{job_id}] Autonomy freeze at tactical phase {phase_number} boundary")
        return freeze_for_review(state, workspace, todo_manager, "tactical", phase_number)

    # Load predefined strategic todos (from config template or defaults)
    strategic_todos = get_transition_strategic_todos(config, tool_names=tool_names)
    todo_list = [todo.to_dict() for todo in strategic_todos]
    todo_manager.set_todos_from_list(todo_list)

    # Export todo state for checkpointing
    todo_state = todo_manager.export_state()

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
            "todos": todo_state["todos"],
            "staged_todos": todo_state["staged_todos"],
            "todo_next_id": todo_state["next_id"],
        },
    )


def handle_phase_transition(
    state: "UniversalAgentState",
    workspace: "WorkspaceManager",
    todo_manager: "TodoManager",
    min_todos: int = 5,
    max_todos: int = 20,
    config: Optional["AgentConfig"] = None,
    tool_names: Optional[List[str]] = None,
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
            state, workspace, todo_manager, min_todos, max_todos, config=config
        )
    else:
        return on_tactical_phase_complete(state, workspace, todo_manager, config, tool_names=tool_names)
