"""Completion signaling tools for the Universal Agent.

This module provides two completion tools:
- mark_complete: Signals that a task/phase is complete (used by phase transitions)
- job_complete: Freezes the job for human review (requires approval to complete)

The job_complete tool implements a human-in-the-loop checkpoint:
1. Rejects the call if there are pending todos (agent must complete all work first)
2. Freezes the job with status 'pending_review' instead of completing it
3. Human operator can then approve (mark completed) or resume with feedback
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

        This tool freezes the job for human review rather than completing it directly.
        A human operator must approve the job before it is marked as truly complete.

        IMPORTANT: All todos must be completed before calling this tool.
        If there are pending todos, this tool will return an error.

        Args:
            summary: Brief description of what was accomplished across all phases (2-5 sentences)
            deliverables: List of ALL output files created during the job
            confidence: Overall confidence the job is truly complete (0.0-1.0, default 1.0)
            notes: Optional notes about limitations, edge cases, or recommendations

        Returns:
            Confirmation that job is frozen for review, or error if todos remain
        """
        try:
            # Check for pending todos with special handling for strategic phase
            if context.has_todo():
                pending = context.todo_manager.list_pending()
                if pending:
                    pending_count = len(pending)
                    is_strategic = context.todo_manager.is_strategic_phase

                    # Special case: In strategic phase with only todo_4 pending
                    # This is the "call job_complete OR next_phase_todos" todo
                    # Auto-complete it since we're completing the job
                    if is_strategic and pending_count == 1 and pending[0].id == "todo_4":
                        context.todo_manager.complete(
                            "todo_4",
                            notes=["Auto-completed by job_complete: plan fully executed"]
                        )
                        logger.info("Auto-completed todo_4 (last strategic todo) for job completion")
                    else:
                        # Standard rejection
                        pending_list = "\n".join(
                            f"  - [{t.id}] {t.content[:80]}{'...' if len(t.content) > 80 else ''}"
                            for t in pending[:5]
                        )
                        more_msg = f"\n  ... and {pending_count - 5} more" if pending_count > 5 else ""

                        if not is_strategic:
                            error_detail = (
                                "job_complete can only be called during a strategic phase.\n"
                                "Complete all tactical todos first, then wait for the strategic phase."
                            )
                        else:
                            error_detail = (
                                "Please complete all todos using `todo_complete` before calling `job_complete`.\n"
                                "If a todo is no longer needed, you can use `todo_rewind` to reconsider it."
                            )

                        logger.warning(f"job_complete rejected: {pending_count} pending todos remain")
                        return (
                            f"ERROR: Cannot complete job - {pending_count} todo(s) still pending.\n\n"
                            f"Pending todos:\n{pending_list}{more_msg}\n\n"
                            f"{error_detail}"
                        )

            # Validate confidence
            confidence = max(0.0, min(1.0, confidence))

            # Build freeze report (job awaiting human review)
            freeze_data = {
                "status": "pending_review",
                "timestamp": datetime.now().isoformat(),
                "summary": summary,
                "deliverables": deliverables,
                "confidence": confidence,
                "job_id": context.job_id,
            }

            if notes:
                freeze_data["notes"] = notes

            # Write to output/job_frozen.json (NOT job_completion.json)
            output_path = "output/job_frozen.json"
            workspace.write_file(
                output_path,
                json.dumps(freeze_data, indent=2, ensure_ascii=False)
            )

            logger.info(f"JOB FROZEN for review: {summary}")
            logger.info(f"Deliverables: {deliverables}")
            logger.info(f"Confidence: {confidence}")

            # Update job status to pending_review (not completed)
            db_updated = False
            if context.has_postgres():
                try:
                    db_updated = await context.update_job_status(
                        status="pending_review",
                        completed_at=False,
                    )
                    if db_updated:
                        logger.info(f"Updated job {context.job_id} status to 'pending_review' in database")
                    else:
                        logger.warning("Failed to update job status in database")
                except Exception as e:
                    logger.error(f"Error updating job status in database: {e}")
            else:
                logger.debug("No PostgreSQL connection available, skipping database status update")

            db_status = " Database updated to 'pending_review'." if db_updated else ""
            return (
                f"JOB FROZEN - Awaiting human review.{db_status}\n"
                f"Wrote: output/job_frozen.json\n"
                f"Summary: {summary}\n"
                f"Deliverables: {len(deliverables)} files\n"
                f"Confidence: {confidence:.0%}\n\n"
                f"The job has been paused for human review. A human operator can:\n"
                f"  - Approve: python agent.py --config <config> --job-id {context.job_id} --approve\n"
                f"  - Resume:  python agent.py --config <config> --job-id {context.job_id} --resume --feedback '...'\n"
            )

        except Exception as e:
            logger.error(f"Failed to freeze job: {e}")
            return f"Error freezing job: {str(e)}"

    return [mark_complete, job_complete]
