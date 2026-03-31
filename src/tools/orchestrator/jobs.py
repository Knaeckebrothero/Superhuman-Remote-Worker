"""Orchestrator job management tools for persistent agents.

These tools call the orchestrator REST API to create, monitor, and manage
worker jobs. They enable the persistent agent to delegate heavy work to
the autonomous worker pool.

The orchestrator URL is read from the ORCHESTRATOR_URL environment variable
(same as the worker's orchestrator_client.py).
"""

import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)

ORCHESTRATOR_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "create_worker_job": {
        "module": "orchestrator.jobs",
        "function": "create_worker_job",
        "description": (
            "Create a new worker job on the orchestrator. A worker agent will "
            "pick it up and execute it autonomously. Returns the job ID for "
            "monitoring. Use config_name to select an expert (developer, scholar, "
            "critic) or 'defaults' for general-purpose."
        ),
        "category": "orchestrator",
        "short_description": "Delegate work to a worker agent via the orchestrator.",
        "phases": ["strategic", "tactical"],
    },
    "list_worker_jobs": {
        "module": "orchestrator.jobs",
        "function": "list_worker_jobs",
        "description": (
            "List jobs on the orchestrator. Filter by status to find active, "
            "completed, or failed jobs. Returns job IDs, descriptions, statuses, "
            "and assigned agents."
        ),
        "category": "orchestrator",
        "short_description": "List jobs with optional status filter.",
        "phases": ["strategic", "tactical"],
    },
    "get_worker_job": {
        "module": "orchestrator.jobs",
        "function": "get_worker_job",
        "description": (
            "Get detailed status of a specific job including progress, current "
            "phase, assigned agent, and any error messages."
        ),
        "category": "orchestrator",
        "short_description": "Get job details and progress.",
        "phases": ["strategic", "tactical"],
    },
    "get_job_workspace_file": {
        "module": "orchestrator.jobs",
        "function": "get_job_workspace_file",
        "description": (
            "Read a file from a worker job's workspace. Use this to inspect "
            "the worker's output, plan, or workspace notes. Common files: "
            "workspace.md, plan.md, output/*.md"
        ),
        "category": "orchestrator",
        "short_description": "Read a file from a job's workspace.",
        "phases": ["strategic", "tactical"],
    },
    "approve_worker_job": {
        "module": "orchestrator.jobs",
        "function": "approve_worker_job",
        "description": (
            "Approve a frozen job that is pending review. This marks the job "
            "as completed. Use after reviewing the job's deliverables."
        ),
        "category": "orchestrator",
        "short_description": "Approve a frozen job.",
        "phases": ["strategic", "tactical"],
    },
    "resume_worker_job": {
        "module": "orchestrator.jobs",
        "function": "resume_worker_job",
        "description": (
            "Resume a paused or frozen job with optional feedback. The worker "
            "will incorporate your feedback when it resumes execution."
        ),
        "category": "orchestrator",
        "short_description": "Resume a job with optional feedback.",
        "phases": ["strategic", "tactical"],
    },
    "cancel_worker_job": {
        "module": "orchestrator.jobs",
        "function": "cancel_worker_job",
        "description": "Cancel a running or paused job.",
        "category": "orchestrator",
        "short_description": "Cancel a job.",
        "phases": ["strategic", "tactical"],
    },
    "pause_worker_job": {
        "module": "orchestrator.jobs",
        "function": "pause_worker_job",
        "description": (
            "Pause a running job. The worker will stop at the next safe point. "
            "Use resume_worker_job to continue."
        ),
        "category": "orchestrator",
        "short_description": "Pause a running job.",
        "phases": ["strategic", "tactical"],
    },
}


def _get_orchestrator_url() -> str:
    """Get orchestrator base URL from environment."""
    url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")
    return url.rstrip("/")


def _get_client() -> httpx.AsyncClient:
    """Create an httpx client for orchestrator calls."""
    return httpx.AsyncClient(timeout=30.0)


def _format_job_summary(job: Dict[str, Any]) -> str:
    """Format a job dict into a readable summary."""
    lines = [
        f"Job ID: {job.get('id', 'unknown')}",
        f"Status: {job.get('status', 'unknown')}",
        f"Description: {job.get('description', 'N/A')}",
    ]
    if job.get("config_name"):
        lines.append(f"Config: {job['config_name']}")
    if job.get("assigned_agent_id"):
        lines.append(f"Agent: {job['assigned_agent_id']}")
    if job.get("created_at"):
        lines.append(f"Created: {job['created_at']}")
    if job.get("error_message"):
        lines.append(f"Error: {job['error_message']}")
    return "\n".join(lines)


def create_orchestrator_tools(context: ToolContext) -> List[Any]:
    """Create all orchestrator tools with injected context."""
    base_url = _get_orchestrator_url()

    @tool
    async def create_worker_job(
            description: str,
            config_name: str = "defaults",
            instructions: Optional[str] = None,
            priority: int = 5,
            project_id: Optional[str] = None,
    ) -> str:
        """Create a new worker job on the orchestrator.

        Args:
            description: What the worker should accomplish
            config_name: Expert config to use (defaults, developer, scholar, critic)
            instructions: Additional instructions for the worker
            priority: Job priority 1-10, higher = more urgent (default: 5)
            project_id: Optional project to scope the job to

        Returns:
            Job creation result with job ID
        """
        payload: Dict[str, Any] = {
            "description": description,
            "config_name": config_name,
            "priority": priority,
        }
        if instructions:
            payload["instructions"] = instructions
        if project_id:
            payload["project_id"] = project_id

        async with _get_client() as client:
            try:
                resp = await client.post(f"{base_url}/api/jobs", json=payload)
                resp.raise_for_status()
                data = resp.json()
                job_id = data.get("id") or data.get("job_id", "unknown")
                return (
                    f"Job created successfully.\n"
                    f"Job ID: {job_id}\n"
                    f"Config: {config_name}\n"
                    f"Priority: {priority}\n"
                    f"Description: {description}\n\n"
                    f"A worker agent will pick this up from the dispatch queue. "
                    f"Use get_worker_job('{job_id}') to check progress."
                )
            except httpx.HTTPStatusError as e:
                return f"Failed to create job: HTTP {e.response.status_code} — {e.response.text}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def list_worker_jobs(
            status: Optional[str] = None,
            limit: int = 20,
    ) -> str:
        """List jobs on the orchestrator.

        Args:
            status: Filter by status (created, processing, completed, failed, cancelled, paused, pending_review)
            limit: Maximum jobs to return (default: 20)

        Returns:
            Formatted list of jobs
        """
        params: Dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status

        async with _get_client() as client:
            try:
                resp = await client.get(f"{base_url}/api/jobs", params=params)
                resp.raise_for_status()
                data = resp.json()
                jobs = data if isinstance(data, list) else data.get("jobs", [])

                if not jobs:
                    filter_msg = f" with status='{status}'" if status else ""
                    return f"No jobs found{filter_msg}."

                lines = [f"Found {len(jobs)} job(s):\n"]
                for job in jobs:
                    lines.append(f"--- {job.get('id', '?')[:8]}... ---")
                    lines.append(f"  Status: {job.get('status', '?')}")
                    lines.append(
                        f"  Description: {(job.get('description') or 'N/A')[:100]}"
                    )
                    if job.get("config_name"):
                        lines.append(f"  Config: {job['config_name']}")
                    lines.append("")

                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                return f"Failed to list jobs: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def get_worker_job(job_id: str) -> str:
        """Get detailed status of a worker job.

        Args:
            job_id: The job UUID

        Returns:
            Job details including status, progress, and any errors
        """
        async with _get_client() as client:
            try:
                resp = await client.get(f"{base_url}/api/jobs/{job_id}")
                resp.raise_for_status()
                job = resp.json()
                return _format_job_summary(job)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"Job '{job_id}' not found."
                return f"Failed to get job: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def get_job_workspace_file(job_id: str, path: str) -> str:
        """Read a file from a worker job's workspace.

        Args:
            job_id: The job UUID
            path: Relative file path (e.g., workspace.md, plan.md, output/result.md)

        Returns:
            File contents or error message
        """
        async with _get_client() as client:
            try:
                resp = await client.get(
                    f"{base_url}/api/jobs/{job_id}/workspace/file",
                    params={"path": path},
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("content", "")
                if not content:
                    return f"File '{path}' is empty or not found."
                return f"=== {path} ===\n{content}"
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"File '{path}' not found in job {job_id}."
                return f"Failed to read file: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def approve_worker_job(job_id: str) -> str:
        """Approve a frozen job that is pending review.

        Args:
            job_id: The job UUID to approve

        Returns:
            Approval result
        """
        async with _get_client() as client:
            try:
                resp = await client.post(f"{base_url}/api/jobs/{job_id}/approve")
                resp.raise_for_status()
                return f"Job {job_id} approved and marked as completed."
            except httpx.HTTPStatusError as e:
                return f"Failed to approve job: HTTP {e.response.status_code} — {e.response.text}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def resume_worker_job(
            job_id: str,
            feedback: Optional[str] = None,
    ) -> str:
        """Resume a paused or frozen job with optional feedback.

        Args:
            job_id: The job UUID to resume
            feedback: Optional feedback message for the worker to incorporate

        Returns:
            Resume result
        """
        payload: Dict[str, Any] = {}
        if feedback:
            payload["feedback"] = feedback

        async with _get_client() as client:
            try:
                resp = await client.post(
                    f"{base_url}/api/jobs/{job_id}/resume",
                    json=payload,
                )
                resp.raise_for_status()
                msg = f"Job {job_id} resumed."
                if feedback:
                    msg += f" Feedback sent: {feedback[:100]}"
                return msg
            except httpx.HTTPStatusError as e:
                return f"Failed to resume job: HTTP {e.response.status_code} — {e.response.text}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def cancel_worker_job(job_id: str) -> str:
        """Cancel a running or paused job.

        Args:
            job_id: The job UUID to cancel

        Returns:
            Cancellation result
        """
        async with _get_client() as client:
            try:
                resp = await client.post(f"{base_url}/api/jobs/{job_id}/cancel")
                resp.raise_for_status()
                return f"Job {job_id} cancelled."
            except httpx.HTTPStatusError as e:
                return f"Failed to cancel job: HTTP {e.response.status_code} — {e.response.text}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def pause_worker_job(job_id: str) -> str:
        """Pause a running job. The worker stops at the next safe point.

        Args:
            job_id: The job UUID to pause

        Returns:
            Pause result
        """
        async with _get_client() as client:
            try:
                resp = await client.post(f"{base_url}/api/jobs/{job_id}/pause")
                resp.raise_for_status()
                return f"Job {job_id} pause requested. Worker will stop at next safe point."
            except httpx.HTTPStatusError as e:
                return f"Failed to pause job: HTTP {e.response.status_code} — {e.response.text}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    return [
        create_worker_job,
        list_worker_jobs,
        get_worker_job,
        get_job_workspace_file,
        approve_worker_job,
        resume_worker_job,
        cancel_worker_job,
        pause_worker_job,
    ]
