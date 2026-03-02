"""Evaluation verdict tools for the Universal Agent.

Provides tools for critic/reviewer agents to act on target jobs:
- approve_job: Approve a pending_review job via orchestrator API
- return_job_with_feedback: Resume a target job with structured feedback

These tools call the orchestrator REST API. The orchestrator URL is
read from the ORCHESTRATOR_URL environment variable (same as heartbeats).
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
EVALUATION_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "approve_job": {
        "module": "evaluation.evaluation_tools",
        "function": "approve_job",
        "description": "Approve a target job that is pending review",
        "category": "evaluation",
        "short_description": "Approve a pending_review job (transitions to completed).",
        "phases": ["strategic", "tactical"],
    },
    "return_job_with_feedback": {
        "module": "evaluation.evaluation_tools",
        "function": "return_job_with_feedback",
        "description": "Resume a target job with feedback for the original agent to address",
        "category": "evaluation",
        "short_description": "Return a job to the original agent with issues to fix.",
        "phases": ["strategic", "tactical"],
    },
}


def _get_orchestrator_url() -> str:
    """Get orchestrator base URL from environment."""
    return os.getenv("ORCHESTRATOR_URL", "http://localhost:8085").rstrip("/")


def create_evaluation_tools(context: ToolContext) -> List[Any]:
    """Create evaluation verdict tools.

    Args:
        context: ToolContext (orchestrator URL from environment)

    Returns:
        List of LangChain tool functions
    """
    orchestrator_url = _get_orchestrator_url()

    @tool
    async def approve_job(
        job_id: str,
        report: str,
        strengths: Optional[List[str]] = None,
        minor_notes: Optional[List[str]] = None,
    ) -> str:
        """Approve a target job that is pending review.

        Call this when the target job's deliverables meet the requirements
        and the work is acceptable. This transitions the job from
        'pending_review' to 'completed'.

        Args:
            job_id: UUID of the target job to approve
            report: Summary of the review findings (2-5 sentences)
            strengths: Optional list of things the agent did well
            minor_notes: Optional list of minor observations (not blocking)

        Returns:
            Confirmation message or error
        """
        try:
            # Write the verification report to our workspace first
            if context.has_workspace():
                report_data = {
                    "verdict": "approved",
                    "target_job_id": job_id,
                    "report": report,
                }
                if strengths:
                    report_data["strengths"] = strengths
                if minor_notes:
                    report_data["minor_notes"] = minor_notes

                context.workspace_manager.write_file(
                    "output/verification_report.json",
                    json.dumps(report_data, indent=2, ensure_ascii=False),
                )

            # Call orchestrator approve endpoint
            url = f"{orchestrator_url}/api/jobs/{job_id}/approve"
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url)

                if response.status_code == 200:
                    logger.info(f"Approved job {job_id}")
                    return (
                        f"Job {job_id} approved successfully. "
                        f"Status transitioned to 'completed'.\n\n"
                        f"Report: {report}"
                    )
                else:
                    error = response.text
                    logger.error(f"Failed to approve job {job_id}: {response.status_code} - {error}")
                    return f"Error approving job {job_id}: HTTP {response.status_code} - {error}"

        except httpx.RequestError as e:
            logger.error(f"Failed to connect to orchestrator for approve: {e}")
            return f"Error: Could not connect to orchestrator at {orchestrator_url}: {e}"
        except Exception as e:
            logger.error(f"Unexpected error approving job: {e}")
            return f"Error approving job: {e}"

    @tool
    async def return_job_with_feedback(
        job_id: str,
        feedback: str,
        issues: Optional[List[str]] = None,
        severity: str = "medium",
    ) -> str:
        """Return a target job to the original agent with feedback.

        Call this when the target job's deliverables have issues that
        need to be addressed. This resumes the job with your feedback
        injected into the agent's context.

        The original agent will see your feedback and can address the
        issues before calling job_complete again, triggering another
        review cycle.

        Args:
            job_id: UUID of the target job to return
            feedback: Detailed feedback for the agent (what's wrong, what to fix)
            issues: Optional list of specific issues found
            severity: Overall severity: "low", "medium", "high"

        Returns:
            Confirmation message or error
        """
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

            # Write the verification report to our workspace
            if context.has_workspace():
                report_data = {
                    "verdict": "returned",
                    "target_job_id": job_id,
                    "feedback": feedback,
                    "severity": severity,
                }
                if issues:
                    report_data["issues"] = issues

                context.workspace_manager.write_file(
                    "output/verification_report.json",
                    json.dumps(report_data, indent=2, ensure_ascii=False),
                )

            # Call orchestrator resume endpoint with feedback
            url = f"{orchestrator_url}/api/jobs/{job_id}/resume"
            payload = {"feedback": structured_feedback}

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload)

                if response.status_code == 200:
                    logger.info(f"Returned job {job_id} with feedback")
                    issue_count = len(issues) if issues else 0
                    return (
                        f"Job {job_id} returned to original agent with feedback.\n"
                        f"Issues: {issue_count}, Severity: {severity}\n\n"
                        f"The agent will resume and address the feedback."
                    )
                elif response.status_code == 202:
                    logger.info(f"Returned job {job_id} with feedback (dispatched)")
                    return (
                        f"Job {job_id} resume dispatched to agent with feedback.\n"
                        f"The agent will address the issues."
                    )
                else:
                    error = response.text
                    logger.error(
                        f"Failed to resume job {job_id}: {response.status_code} - {error}"
                    )
                    return f"Error resuming job {job_id}: HTTP {response.status_code} - {error}"

        except httpx.RequestError as e:
            logger.error(f"Failed to connect to orchestrator for resume: {e}")
            return f"Error: Could not connect to orchestrator at {orchestrator_url}: {e}"
        except Exception as e:
            logger.error(f"Unexpected error resuming job: {e}")
            return f"Error resuming job: {e}"

    return [approve_job, return_job_with_feedback]
