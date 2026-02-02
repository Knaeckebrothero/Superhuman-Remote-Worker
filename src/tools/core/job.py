"""Job lifecycle tools for the Universal Agent.

This module provides completion signaling tools:
- mark_complete: Signals that a task/phase is complete (used by phase transitions)
- job_complete: Marks the phase as final, job completes after remaining todos are done

The job_complete tool implements a final phase pattern:
1. Rejects if called from tactical phase (must be in strategic phase)
2. Sets is_final_phase=True in state
3. Agent completes remaining strategic todos (summarize, update workspace/plan)
4. When all todos complete, on_strategic_phase_complete detects final phase and freezes job
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


# Global storage for final phase data (set by job_complete, used by finalize_job)
# This is necessary because tools can't directly modify state, but phase.py can read this
_final_phase_data: Dict[str, Dict[str, Any]] = {}


# Tool metadata for registry
# Phase availability:
#   - "strategic": Only in strategic mode (planning)
#   - "tactical": Only in tactical mode (execution)
#   - Both: Available in both modes (default if not specified)
JOB_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "mark_complete": {
        "module": "core.job",
        "function": "mark_complete",
        "description": "Signal task/phase completion with structured report",
        "category": "completion",
        "phases": ["strategic", "tactical"],  # Both modes
    },
    "job_complete": {
        "module": "core.job",
        "function": "job_complete",
        "description": "Signal FINAL job completion - call when all phases are done",
        "category": "completion",
        "phases": ["strategic"],  # Strategic-only: prevents premature termination
    },
}


def create_job_tools(context: ToolContext) -> List[Any]:
    """Create job lifecycle tools.

    Args:
        context: Tool context with workspace manager

    Returns:
        List of job tools

    Raises:
        ValueError: If context doesn't have a workspace_manager
    """
    if not context.has_workspace():
        raise ValueError("ToolContext must have a workspace_manager for job tools")

    workspace = context.workspace_manager

    @tool
    async def mark_complete(
        summary: str,
        deliverables: List[str],
        confidence: float = 1.0,
        notes: Optional[str] = None,
    ) -> str:
        """Signal that the assigned task is complete.

        Call this tool ONLY when you have finished all work on the task.
        This will write a completion report and end the agent loop.

        Args:
            summary: Brief description of what was accomplished (1-3 sentences)
            deliverables: List of output files or artifacts created (e.g., ["output/requirements.json", "notes/analysis.md"])
            confidence: Your confidence the task is truly complete (0.0-1.0, default 1.0)
            notes: Optional notes about limitations, assumptions, or follow-up suggestions

        Returns:
            Confirmation message
        """
        try:
            # Validate confidence
            confidence = max(0.0, min(1.0, confidence))

            # Build completion report
            completion_data = {
                "status": "completed",
                "timestamp": datetime.now().isoformat(),
                "summary": summary,
                "deliverables": deliverables,
                "confidence": confidence,
            }

            if notes:
                completion_data["notes"] = notes

            # Write to output/completion.json
            output_path = "output/completion.json"
            workspace.write_file(
                output_path,
                json.dumps(completion_data, indent=2, ensure_ascii=False)
            )

            logger.info(f"Task marked complete: {summary}")
            logger.info(f"Deliverables: {deliverables}")

            # Return message that triggers completion detection
            return f"Wrote file: output/completion.json - Task complete. Summary: {summary}"

        except Exception as e:
            logger.error(f"Failed to mark complete: {e}")
            return f"Error marking complete: {str(e)}"

    @tool
    async def job_complete(
        summary: str,
        deliverables: List[str],
        confidence: float = 1.0,
        notes: Optional[str] = None,
    ) -> str:
        """Signal that the job is complete and ready for human review.

        This tool marks the current strategic phase as "final". The job will
        complete after all remaining strategic todos are done (summarize,
        update workspace.md, update plan.md).

        IMPORTANT: This tool can only be called during a strategic phase.
        If called from tactical phase, it will be rejected.

        Args:
            summary: Brief description of what was accomplished across all phases (2-5 sentences)
            deliverables: List of ALL output files created during the job
            confidence: Overall confidence the job is truly complete (0.0-1.0, default 1.0)
            notes: Optional notes about limitations, edge cases, or recommendations

        Returns:
            Confirmation that phase is marked as final, or error if in tactical phase
        """
        try:
            # Check if we're in a strategic phase
            is_strategic = True
            if context.has_todo():
                is_strategic = context.todo_manager.is_strategic_phase

            if not is_strategic:
                logger.warning("job_complete rejected: called from tactical phase")
                return (
                    "ERROR: job_complete can only be called during a strategic phase.\n\n"
                    "Complete all tactical todos first. When all tactical todos are done,\n"
                    "you will automatically transition to a strategic phase where you can\n"
                    "call job_complete."
                )

            # Check if already in final phase
            if context.job_id in _final_phase_data:
                logger.info(f"job_complete called again for job {context.job_id} - already marked as final")
                return (
                    "Phase is already marked as final. Complete your remaining todos\n"
                    "to finish the job."
                )

            # Check if there are staged todos (shouldn't call job_complete if planning more work)
            if context.has_todo() and context.todo_manager.has_staged_todos():
                logger.warning("job_complete rejected: staged todos exist")
                return (
                    "ERROR: Cannot mark job as complete - you have staged todos for the next phase.\n\n"
                    "Either:\n"
                    "1. Clear the staged todos and call job_complete again, or\n"
                    "2. Continue with next_phase_todos to execute the staged work"
                )

            # Validate confidence
            confidence = max(0.0, min(1.0, confidence))

            # Store final phase data for later use by finalize_job
            final_data = {
                "summary": summary,
                "deliverables": deliverables,
                "confidence": confidence,
                "job_id": context.job_id,
            }
            if notes:
                final_data["notes"] = notes

            _final_phase_data[context.job_id] = final_data

            logger.info(f"Job {context.job_id} marked as final phase")
            logger.info(f"Summary: {summary}")
            logger.info(f"Deliverables: {deliverables}")

            # Check if there are no pending todos - if so, job will complete immediately
            # after this tool returns (on_strategic_phase_complete will be called)
            pending_count = 0
            if context.has_todo():
                pending = context.todo_manager.list_pending()
                pending_count = len(pending)

            if pending_count == 0:
                return (
                    "Phase marked as final. No remaining todos - job will complete now.\n\n"
                    f"Summary: {summary}\n"
                    f"Deliverables: {len(deliverables)} files\n"
                    f"Confidence: {confidence:.0%}"
                )
            else:
                return (
                    f"Phase marked as final. Complete your {pending_count} remaining todo(s) to finish the job.\n\n"
                    f"Summary: {summary}\n"
                    f"Deliverables: {len(deliverables)} files\n"
                    f"Confidence: {confidence:.0%}\n\n"
                    "Once all todos are complete, the job will be frozen for human review."
                )

        except Exception as e:
            logger.error(f"Failed to mark job as final: {e}")
            return f"Error marking job as final: {str(e)}"

    return [mark_complete, job_complete]


def get_final_phase_data(job_id: str) -> Optional[Dict[str, Any]]:
    """Get the final phase data for a job, if it exists.

    Called by phase.py to check if a job has been marked as final.

    Args:
        job_id: The job ID to check

    Returns:
        Final phase data dict if job is marked as final, None otherwise
    """
    return _final_phase_data.get(job_id)


def clear_final_phase_data(job_id: str) -> None:
    """Clear the final phase data for a job.

    Called by phase.py after job finalization is complete.

    Args:
        job_id: The job ID to clear
    """
    if job_id in _final_phase_data:
        del _final_phase_data[job_id]
        logger.debug(f"Cleared final phase data for job {job_id}")
