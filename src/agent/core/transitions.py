"""Phase transition automation for the guardrails system.

DEPRECATION NOTICE:
This module is legacy code for the old graph.py architecture.
The new nested loop graph (graph.py) handles phase transitions
structurally via graph nodes and doesn't need this module.

For new code, use:
- src/agent/managers/plan.py - PlanManager for plan operations
- src/agent/managers/todo.py - TodoManager for todo operations
- src/agent/graph.py - Nested loop graph with built-in transitions

This module is kept for backwards compatibility with graph.py.
It will be removed in a future version.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .todo import TodoManager
    from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class TransitionType(Enum):
    """Type of phase transition."""

    # Normal transition to next phase
    NEXT_PHASE = "next_phase"

    # All phases complete - job finishing
    JOB_COMPLETE = "job_complete"

    # Rewind triggered - need to re-plan
    REWIND = "rewind"


@dataclass
class PhaseInfo:
    """Information about a phase parsed from main_plan.md."""

    number: int
    name: str
    status: str  # "pending", "current", "complete"
    steps: List[str] = field(default_factory=list)

    @property
    def is_current(self) -> bool:
        return self.status == "current"

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"


@dataclass
class TransitionResult:
    """Result of a phase transition check/execution."""

    # Whether a transition should occur
    should_transition: bool

    # Type of transition
    transition_type: Optional[TransitionType] = None

    # Next phase info (if transitioning to next phase)
    next_phase: Optional[PhaseInfo] = None

    # Transition prompt to inject
    transition_prompt: str = ""

    # Whether context summarization should be triggered
    trigger_summarization: bool = False

    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)


# Phase transition prompt templates
PHASE_TRANSITION_PROMPT = """
═══════════════════════════════════════════════════════════════════
                     PHASE TRANSITION
═══════════════════════════════════════════════════════════════════

Phase {current_phase} is complete! Time to transition to the next phase.

Your todos have been archived and a workspace summary generated.

To complete the transition:

1. **Read main_plan.md** - Review the overall execution plan
2. **Read workspace_summary.md** - Understand current workspace state
3. **Update main_plan.md** - Mark Phase {current_phase} as ✓ COMPLETE and Phase {next_phase} as ← CURRENT
4. **Create new todos** - Use todo_write to create tasks for Phase {next_phase}

Phase {next_phase}: {next_phase_name}
{next_phase_steps}

Once you've created the new todos, continue working through them.

═══════════════════════════════════════════════════════════════════
"""

JOB_COMPLETE_PROMPT = """
═══════════════════════════════════════════════════════════════════
                     ALL PHASES COMPLETE
═══════════════════════════════════════════════════════════════════

Congratulations! All phases in your execution plan are complete.

Your todos have been archived and a workspace summary generated.

To finalize the job:

1. **Read main_plan.md** - Verify all phases are marked complete
2. **Read workspace_summary.md** - Review what was accomplished
3. **Create final todos** - Add verification and completion tasks

Suggested final todos:
- Verify all deliverables are present in workspace
- Generate final summary report
- Call job_complete() to signal completion

Use todo_write to add these tasks, then work through them.

═══════════════════════════════════════════════════════════════════
"""

REWIND_TRANSITION_PROMPT = """
═══════════════════════════════════════════════════════════════════
                     REWIND - REPLANNING NEEDED
═══════════════════════════════════════════════════════════════════

A rewind was triggered: {issue}

Your todos have been archived with the failure note.

To recover:

1. **Read main_plan.md** - Review the overall strategy
2. **Read workspace_summary.md** - Understand current state
3. **Analyze the issue** - Consider what went wrong
4. **Update main_plan.md** - Adjust strategy if needed
5. **Create new todos** - Use todo_write for the revised approach

Take time to think through the issue before proceeding.

═══════════════════════════════════════════════════════════════════
"""


class PhaseTransitionManager:
    """Manages phase transitions for the guardrails system.

    This class handles the detection and execution of phase transitions.
    It parses main_plan.md to understand the plan structure and determines
    what type of transition should occur.

    Usage:
        manager = PhaseTransitionManager(workspace_manager, todo_manager)

        # Check if transition should occur after task completion
        result = manager.check_transition()

        if result.should_transition:
            # Execute the transition
            manager.execute_transition(result)
    """

    def __init__(
        self,
        workspace_manager: Optional["WorkspaceManager"] = None,
        todo_manager: Optional["TodoManager"] = None,
    ):
        """Initialize the phase transition manager.

        Args:
            workspace_manager: Workspace manager for file operations
            todo_manager: Todo manager for task tracking
        """
        self._workspace = workspace_manager
        self._todo_manager = todo_manager

    def set_workspace_manager(self, workspace_manager: "WorkspaceManager") -> None:
        """Set the workspace manager."""
        self._workspace = workspace_manager

    def set_todo_manager(self, todo_manager: "TodoManager") -> None:
        """Set the todo manager."""
        self._todo_manager = todo_manager

    def parse_main_plan(self) -> Tuple[List[PhaseInfo], Optional[PhaseInfo]]:
        """Parse main_plan.md to extract phase information.

        Returns:
            Tuple of (all_phases, current_phase)
        """
        if not self._workspace:
            logger.warning("No workspace manager available for parsing main_plan.md")
            return [], None

        if not self._workspace.exists("main_plan.md"):
            logger.debug("main_plan.md not found in workspace")
            return [], None

        try:
            content = self._workspace.read_file("main_plan.md")
            return self._parse_plan_content(content)
        except Exception as e:
            logger.error(f"Error parsing main_plan.md: {e}")
            return [], None

    def _parse_plan_content(self, content: str) -> Tuple[List[PhaseInfo], Optional[PhaseInfo]]:
        """Parse plan content to extract phases.

        Expected format:
            ## Phase 1: Document Analysis ✓ COMPLETE
            - [x] Extract document structure
            - [x] Identify key sections

            ## Phase 2: Requirement Extraction ← CURRENT
            - [ ] Process section 1-3
            - [ ] Process section 4-6

        Args:
            content: Content of main_plan.md

        Returns:
            Tuple of (all_phases, current_phase)
        """
        phases = []
        current_phase = None

        # Pattern for phase headers
        # Matches: ## Phase 1: Name, ## Phase 2: Name ✓ COMPLETE, ## Phase 3: Name ← CURRENT
        phase_pattern = re.compile(
            r'^##\s*Phase\s*(\d+)[:\s]*([^\n✓←]*?)(?:\s*(✓\s*COMPLETE|←\s*CURRENT))?\s*$',
            re.MULTILINE | re.IGNORECASE
        )

        # Pattern for step items (checkbox items)
        step_pattern = re.compile(r'^\s*-\s*\[[x ]\]\s*(.+)$', re.MULTILINE | re.IGNORECASE)

        # Find all phase headers with their positions
        matches = list(phase_pattern.finditer(content))

        for i, match in enumerate(matches):
            phase_num = int(match.group(1))
            phase_name = match.group(2).strip()
            status_marker = match.group(3) or ""

            # Determine status
            if "COMPLETE" in status_marker.upper():
                status = "complete"
            elif "CURRENT" in status_marker.upper():
                status = "current"
            else:
                status = "pending"

            # Extract steps between this phase header and the next
            start_pos = match.end()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            phase_content = content[start_pos:end_pos]

            # Find all steps
            steps = [m.group(1).strip() for m in step_pattern.finditer(phase_content)]

            phase_info = PhaseInfo(
                number=phase_num,
                name=phase_name,
                status=status,
                steps=steps,
            )
            phases.append(phase_info)

            if status == "current":
                current_phase = phase_info

        # Sort by phase number
        phases.sort(key=lambda p: p.number)

        return phases, current_phase

    def get_next_phase(self, phases: List[PhaseInfo], current: Optional[PhaseInfo]) -> Optional[PhaseInfo]:
        """Determine the next phase to execute.

        Args:
            phases: List of all phases
            current: Current phase (if any)

        Returns:
            Next phase to execute, or None if all complete
        """
        if not phases:
            return None

        if current is None:
            # Find first non-complete phase
            for phase in phases:
                if not phase.is_complete:
                    return phase
            return None

        # Find the next phase after current
        current_idx = next(
            (i for i, p in enumerate(phases) if p.number == current.number),
            -1
        )

        if current_idx < 0 or current_idx >= len(phases) - 1:
            return None

        # Return next phase (should be pending)
        next_phase = phases[current_idx + 1]
        return next_phase if not next_phase.is_complete else None

    def check_transition(self, is_last_task: bool = False) -> TransitionResult:
        """Check if a phase transition should occur.

        This should be called after todo_complete() returns is_last_task=True.

        Args:
            is_last_task: Whether the last task in the phase was just completed

        Returns:
            TransitionResult indicating whether/how to transition
        """
        if not is_last_task:
            return TransitionResult(should_transition=False)

        # Parse the current plan to understand phase structure
        phases, current_phase = self.parse_main_plan()

        if not phases:
            # No plan found - this might be bootstrap or ad-hoc work
            # During bootstrap, the agent hasn't created main_plan.md yet
            # Don't trigger job completion - let the agent continue working
            logger.debug("No phases found in main_plan.md, skipping transition")
            return TransitionResult(should_transition=False)

        # Find next phase
        next_phase = self.get_next_phase(phases, current_phase)

        if next_phase is None:
            # All phases complete - job finishing
            logger.info("All phases complete, triggering job completion")
            return TransitionResult(
                should_transition=True,
                transition_type=TransitionType.JOB_COMPLETE,
                transition_prompt=JOB_COMPLETE_PROMPT,
                trigger_summarization=True,
                metadata={
                    "completed_phases": len([p for p in phases if p.is_complete]),
                    "total_phases": len(phases),
                },
            )

        # Normal transition to next phase
        logger.info(f"Transitioning to Phase {next_phase.number}: {next_phase.name}")

        # Format the steps for the prompt
        steps_text = ""
        if next_phase.steps:
            steps_text = "Steps:\n" + "\n".join(f"  - {step}" for step in next_phase.steps)

        prompt = PHASE_TRANSITION_PROMPT.format(
            current_phase=current_phase.number if current_phase else "?",
            next_phase=next_phase.number,
            next_phase_name=next_phase.name,
            next_phase_steps=steps_text,
        )

        return TransitionResult(
            should_transition=True,
            transition_type=TransitionType.NEXT_PHASE,
            next_phase=next_phase,
            transition_prompt=prompt,
            trigger_summarization=True,
            metadata={
                "from_phase": current_phase.number if current_phase else 0,
                "to_phase": next_phase.number,
            },
        )

    def check_rewind_transition(self, issue: str) -> TransitionResult:
        """Create a transition result for a rewind operation.

        Args:
            issue: The issue that caused the rewind

        Returns:
            TransitionResult for rewind recovery
        """
        prompt = REWIND_TRANSITION_PROMPT.format(issue=issue)

        return TransitionResult(
            should_transition=True,
            transition_type=TransitionType.REWIND,
            transition_prompt=prompt,
            trigger_summarization=True,
            metadata={"rewind_issue": issue},
        )

    def execute_transition(self, result: TransitionResult) -> Dict[str, Any]:
        """Execute a phase transition.

        This method:
        1. Archives completed todos
        2. Generates workspace summary
        3. Updates todo manager phase info

        Args:
            result: TransitionResult from check_transition()

        Returns:
            Dictionary with execution details
        """
        execution_result = {
            "success": False,
            "archived": False,
            "summary_generated": False,
            "phase_updated": False,
        }

        if not result.should_transition:
            return execution_result

        try:
            # 1. Archive completed todos
            if self._todo_manager and self._workspace:
                try:
                    phase_info = self._todo_manager.get_phase_info()
                    phase_name = phase_info.get("phase_name", "")
                    archive_result = self._todo_manager.archive_and_reset(
                        phase_name=phase_name,
                        workspace_manager=self._workspace,
                    )
                    execution_result["archived"] = True
                    logger.info(f"Archived todos: {archive_result}")
                except Exception as e:
                    logger.error(f"Failed to archive todos: {e}")

            # 2. Note: workspace.md is now persistent and injected into system prompt
            # Agent should update it manually when needed (like CLAUDE.md pattern)
            execution_result["workspace_md_available"] = True

            # 3. Update todo manager phase info for next phase
            if result.next_phase and self._todo_manager:
                try:
                    # Get total phases from parsing
                    phases, _ = self.parse_main_plan()
                    total_phases = len(phases)

                    self._todo_manager.set_phase_info(
                        phase_number=result.next_phase.number,
                        total_phases=total_phases,
                        phase_name=result.next_phase.name,
                    )
                    execution_result["phase_updated"] = True
                except Exception as e:
                    logger.error(f"Failed to update phase info: {e}")

            execution_result["success"] = True

        except Exception as e:
            logger.error(f"Phase transition execution failed: {e}")

        return execution_result


def get_job_completion_todos() -> List[Dict[str, str]]:
    """Get the standard job completion todos.

    These are injected when all phases are complete.

    Returns:
        List of todo dictionaries for job completion
    """
    return [
        {
            "content": "Verify all deliverables are present in workspace",
            "status": "pending",
            "priority": "high",
        },
        {
            "content": "Read workspace_summary.md to review accomplishments",
            "status": "pending",
            "priority": "medium",
        },
        {
            "content": "Generate final summary report if not already done",
            "status": "pending",
            "priority": "medium",
        },
        {
            "content": "Call job_complete() to signal job completion",
            "status": "pending",
            "priority": "high",
        },
    ]


def get_bootstrap_todos() -> List[Dict[str, str]]:
    """Get the standard bootstrap todos for job initialization.

    These are pre-populated at job start to guide the agent through
    the initial planning phase. This sequence ensures the agent:
    1. Understands the workspace state
    2. Reads their instructions
    3. Creates a comprehensive plan
    4. Divides the plan into executable phases

    Returns:
        List of todo dictionaries for bootstrap
    """
    return [
        {
            "content": "Read instructions.md to understand task requirements",
            "status": "pending",
            "priority": "high",
        },
        {
            "content": "Create comprehensive execution plan in main_plan.md",
            "status": "pending",
            "priority": "high",
        },
        {
            "content": "Divide plan into executable phases (5-20 steps each)",
            "status": "pending",
            "priority": "high",
        },
    ]


# Bootstrap prompt template - shown when agent starts with bootstrap todos
BOOTSTRAP_PROMPT = """
═══════════════════════════════════════════════════════════════════
                     JOB INITIALIZATION
═══════════════════════════════════════════════════════════════════

Welcome! You are starting a new job. Your todo list has been pre-populated
with bootstrap tasks to guide you through the initialization process.

YOUR ONLY JOB: Complete the tasks in your todo list, one at a time.

After completing each task, call todo_complete(). The system will:
- Mark the task done
- Show you the next task
- Handle everything else automatically

Do not try to manage phases yourself. Just focus on the current task.
When you complete all bootstrap tasks, phase transitions will begin
automatically based on your plan.

Start by working on the first task in your todo list.

═══════════════════════════════════════════════════════════════════
"""
