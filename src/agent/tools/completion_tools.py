"""Completion signaling tools for the Universal Agent.

This module provides two completion tools:
- mark_complete: Signals that a task/phase is complete (used by phase transitions)
- job_complete: Signals that the entire job is finished (final completion)
"""

import json
import logging
from datetime import datetime
from typing import List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


def create_completion_tools(context: ToolContext) -> List:
    """Create completion signaling tools.

    Args:
        context: Tool context with workspace manager

    Returns:
        List of completion tools
    """
    if not context.has_workspace():
        raise ValueError("ToolContext must have a workspace_manager for completion tools")

    workspace = context.workspace_manager

    @tool
    def mark_complete(
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
    def job_complete(
        summary: str,
        deliverables: List[str],
        confidence: float = 1.0,
        notes: Optional[str] = None,
    ) -> str:
        """Signal that the ENTIRE JOB is complete and the agent should stop.

        This is the final completion signal - call this only when ALL phases
        of the execution plan are complete and all deliverables are ready.

        This differs from mark_complete which signals phase/task completion.
        job_complete signals the end of the entire job.

        Args:
            summary: Brief description of what was accomplished across all phases (2-5 sentences)
            deliverables: List of ALL output files created during the job
            confidence: Overall confidence the job is truly complete (0.0-1.0, default 1.0)
            notes: Optional notes about limitations, edge cases, or recommendations

        Returns:
            Confirmation message indicating job completion
        """
        try:
            # Validate confidence
            confidence = max(0.0, min(1.0, confidence))

            # Build final job completion report
            completion_data = {
                "status": "job_completed",
                "timestamp": datetime.now().isoformat(),
                "summary": summary,
                "deliverables": deliverables,
                "confidence": confidence,
                "job_id": context.job_id,
            }

            if notes:
                completion_data["notes"] = notes

            # Write to output/job_completion.json
            output_path = "output/job_completion.json"
            workspace.write_file(
                output_path,
                json.dumps(completion_data, indent=2, ensure_ascii=False)
            )

            logger.info(f"JOB COMPLETE: {summary}")
            logger.info(f"Final deliverables: {deliverables}")
            logger.info(f"Confidence: {confidence}")

            # TODO: Update job status in PostgreSQL database
            # This should set jobs.status = 'completed' and jobs.completed_at = now()
            # Requires database connection in ToolContext
            # if context.has_database():
            #     context.database.update_job_status(context.job_id, "completed")

            # Return message that triggers final completion detection
            # The agent loop should detect "JOB COMPLETE" and terminate
            return (
                f"JOB COMPLETE - Wrote: output/job_completion.json\n"
                f"Summary: {summary}\n"
                f"Deliverables: {len(deliverables)} files\n"
                f"Confidence: {confidence:.0%}\n"
                f"The job has finished. No further action required."
            )

        except Exception as e:
            logger.error(f"Failed to complete job: {e}")
            return f"Error completing job: {str(e)}"

    return [mark_complete, job_complete]
