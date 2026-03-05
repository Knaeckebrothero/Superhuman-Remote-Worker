"""Evaluation verdict tools for the Universal Agent.

Provides tools for critic/reviewer agents to act on target jobs:
- approve_job: Record approval verdict for a target job
- return_job_with_feedback: Record feedback verdict for a target job

These tools use a deferred verdict pattern: they store the verdict intent
in module-level state, and the actual orchestrator API calls happen after
the critic's graph ends (in finalize_job / handle_transition).
"""

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deferred verdict storage
# ---------------------------------------------------------------------------
# Verdict data is stored here by the tools, then consumed by finalize_job
# in src/core/phase.py after the critic's graph loop ends.
_verdict_data: Dict[str, Dict[str, Any]] = {}


def get_verdict_data(job_id: str) -> Optional[Dict[str, Any]]:
    """Get stored verdict data for a job (used by phase.py).

    Args:
        job_id: The critic job's UUID

    Returns:
        Verdict dict if present, None otherwise
    """
    return _verdict_data.get(job_id)


def clear_verdict_data(job_id: str) -> None:
    """Clear stored verdict data for a job (used by phase.py after processing).

    Args:
        job_id: The critic job's UUID
    """
    if job_id in _verdict_data:
        del _verdict_data[job_id]


# Tool metadata for registry
EVALUATION_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "approve_job": {
        "module": "evaluation.evaluation_tools",
        "function": "approve_job",
        "description": "Approve a target job that is pending review",
        "category": "evaluation",
        "short_description": "Approve a pending_review job (transitions to completed).",
        "phases": ["strategic"],
    },
    "return_job_with_feedback": {
        "module": "evaluation.evaluation_tools",
        "function": "return_job_with_feedback",
        "description": "Resume a target job with feedback for the original agent to address",
        "category": "evaluation",
        "short_description": "Return a job to the original agent with issues to fix.",
        "phases": ["strategic"],
    },
}


def create_evaluation_tools(context: ToolContext) -> List[Any]:
    """Create evaluation verdict tools.

    Args:
        context: ToolContext (orchestrator URL from environment)

    Returns:
        List of LangChain tool functions
    """

    @tool
    async def approve_job(
        job_id: str,
        report: str,
        strengths: Optional[List[str]] = None,
        minor_notes: Optional[List[str]] = None,
    ) -> str:
        """Approve a target job that is pending review.

        Call this when the target job's deliverables meet the requirements
        and the work is acceptable. The verdict is recorded and executed
        when you complete your remaining todos.

        Args:
            job_id: UUID of the target job to approve
            report: Summary of the review findings (2-5 sentences)
            strengths: Optional list of things the agent did well
            minor_notes: Optional list of minor observations (not blocking)

        Returns:
            Confirmation message
        """
        from ..core.job import _final_phase_data

        try:
            # Build the report data
            report_data: Dict[str, Any] = {
                "verdict": "approved",
                "target_job_id": job_id,
                "report": report,
            }
            if strengths:
                report_data["strengths"] = strengths
            if minor_notes:
                report_data["minor_notes"] = minor_notes

            # Write the verification report to workspace
            if context.has_workspace():
                context.workspace_manager.write_file(
                    "output/verification_report.json",
                    json.dumps(report_data, indent=2, ensure_ascii=False),
                )

            # Store verdict for deferred execution
            _verdict_data[context.job_id] = {
                "_verdict": "approved",
                "_target_job_id": job_id,
                "report": report,
                "strengths": strengths or [],
                "minor_notes": minor_notes or [],
            }

            # Also set _final_phase_data so on_strategic_phase_complete
            # triggers finalize_job
            _final_phase_data[context.job_id] = {
                "summary": f"Verification complete: approved job {job_id}",
                "deliverables": ["output/verification_report.json"],
                "confidence": 1.0,
                "job_id": context.job_id,
            }

            logger.info(f"Verdict recorded: approved job {job_id}")
            return (
                f"Verdict recorded: APPROVED job {job_id}.\n"
                f"Report: {report}\n\n"
                f"Complete your remaining todos to finalize the verdict."
            )

        except Exception as e:
            logger.error(f"Error recording approval verdict: {e}")
            return f"Error recording verdict: {e}"

    @tool
    async def return_job_with_feedback(
        job_id: str,
        feedback: str,
        issues: Optional[List[str]] = None,
        severity: str = "medium",
    ) -> str:
        """Return a target job to the original agent with feedback.

        Call this when the target job's deliverables have issues that
        need to be addressed. The verdict is recorded and executed
        when you complete your remaining todos.

        The original agent will see your feedback and can address the
        issues before calling job_complete again, triggering another
        review cycle.

        Args:
            job_id: UUID of the target job to return
            feedback: Detailed feedback for the agent (what's wrong, what to fix)
            issues: Optional list of specific issues found
            severity: Overall severity: "low", "medium", "high"

        Returns:
            Confirmation message
        """
        from ..core.job import _final_phase_data

        try:
            # Build structured feedback message
            feedback_parts = [f"## Verification Feedback\n\n{feedback}"]

            if issues:
                feedback_parts.append("\n### Issues Found\n")
                for i, issue in enumerate(issues, 1):
                    feedback_parts.append(f"{i}. {issue}")

            feedback_parts.append(f"\n**Severity**: {severity}")
            feedback_parts.append(
                "\nPlease address these issues and call job_complete again when done."
            )

            structured_feedback = "\n".join(feedback_parts)

            # Build the report data
            report_data: Dict[str, Any] = {
                "verdict": "returned",
                "target_job_id": job_id,
                "feedback": feedback,
                "severity": severity,
            }
            if issues:
                report_data["issues"] = issues

            # Write the verification report to workspace
            if context.has_workspace():
                context.workspace_manager.write_file(
                    "output/verification_report.json",
                    json.dumps(report_data, indent=2, ensure_ascii=False),
                )

            # Store verdict for deferred execution
            _verdict_data[context.job_id] = {
                "_verdict": "returned",
                "_target_job_id": job_id,
                "_feedback": structured_feedback,
                "feedback_raw": feedback,
                "issues": issues or [],
                "severity": severity,
            }

            # Also set _final_phase_data so on_strategic_phase_complete
            # triggers finalize_job
            _final_phase_data[context.job_id] = {
                "summary": f"Verification complete: returned job {job_id} with feedback",
                "deliverables": ["output/verification_report.json"],
                "confidence": 1.0,
                "job_id": context.job_id,
            }

            issue_count = len(issues) if issues else 0
            logger.info(
                f"Verdict recorded: returned job {job_id} "
                f"({issue_count} issues, severity={severity})"
            )
            return (
                f"Verdict recorded: RETURNED job {job_id} with feedback.\n"
                f"Issues: {issue_count}, Severity: {severity}\n\n"
                f"Complete your remaining todos to finalize the verdict."
            )

        except Exception as e:
            logger.error(f"Error recording return verdict: {e}")
            return f"Error recording verdict: {e}"

    return [approve_job, return_job_with_feedback]
