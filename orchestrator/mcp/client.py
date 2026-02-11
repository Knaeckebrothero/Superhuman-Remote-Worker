"""HTTP client for the Cockpit API.

Provides synchronous and asynchronous methods to interact with the debug cockpit's REST API.
"""

from __future__ import annotations

import os
from typing import Any, Literal

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)


FilterCategory = Literal["all", "messages", "tools", "errors"]


def _create_retry_decorator():
    """Create a retry decorator with exponential backoff."""
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
        reraise=True,
    )


class CockpitClient:
    """HTTP client for the cockpit API."""

    def __init__(self, base_url: str | None = None):
        """Initialize the client.

        Args:
            base_url: Cockpit API URL. Defaults to COCKPIT_API_URL env var
                      or http://localhost:8085.
        """
        self.base_url = base_url or os.environ.get(
            "COCKPIT_API_URL", "http://localhost:8085"
        )
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0)

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self) -> CockpitClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # =========================================================================
    # Health
    # =========================================================================

    def health_check(self) -> dict[str, str]:
        """Check API health status."""
        resp = self._client.get("/api/health")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Jobs
    # =========================================================================

    def list_jobs(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List jobs with optional status filter.

        Args:
            status: Filter by status (pending, running, completed, failed)
            limit: Maximum number of jobs to return (1-500)

        Returns:
            List of job dicts with id, status, config, timestamps, audit_count
        """
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        resp = self._client.get("/api/jobs", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_job(self, job_id: str) -> dict[str, Any]:
        """Get a single job by ID.

        Args:
            job_id: Job UUID

        Returns:
            Job dict with full details
        """
        resp = self._client.get(f"/api/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Audit Trail
    # =========================================================================

    def get_audit_trail(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
        filter_category: FilterCategory = "all",
    ) -> dict[str, Any]:
        """Get paginated audit entries for a job.

        Args:
            job_id: Job UUID
            page: Page number (1-indexed, -1 for last page)
            page_size: Entries per page (1-200)
            filter_category: Filter type (all, messages, tools, errors)

        Returns:
            Dict with entries, total, page, pageSize, hasMore
        """
        params = {
            "page": page,
            "pageSize": page_size,
            "filter": filter_category,
        }
        resp = self._client.get(f"/api/jobs/{job_id}/audit", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_audit_time_range(self, job_id: str) -> dict[str, str] | None:
        """Get first and last timestamps for job audit entries.

        Args:
            job_id: Job UUID

        Returns:
            Dict with start and end ISO timestamps, or None
        """
        resp = self._client.get(f"/api/jobs/{job_id}/audit/timerange")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Chat History
    # =========================================================================

    def get_chat_history(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Get paginated chat history for a job.

        Args:
            job_id: Job UUID
            page: Page number (1-indexed, -1 for last page)
            page_size: Entries per page (1-200)

        Returns:
            Dict with entries, total, page, pageSize, hasMore
        """
        params = {
            "page": page,
            "pageSize": page_size,
        }
        resp = self._client.get(f"/api/jobs/{job_id}/chat", params=params)
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Todos
    # =========================================================================

    def get_todos(self, job_id: str) -> dict[str, Any]:
        """Get all todos for a job (current + archives).

        Args:
            job_id: Job UUID

        Returns:
            Dict with job_id, current, archives, has_workspace
        """
        resp = self._client.get(f"/api/jobs/{job_id}/todos")
        resp.raise_for_status()
        return resp.json()

    def get_current_todos(self, job_id: str) -> dict[str, Any] | None:
        """Get current active todos from todos.yaml.

        Args:
            job_id: Job UUID

        Returns:
            Dict with todos list and metadata, or None if not found
        """
        resp = self._client.get(f"/api/jobs/{job_id}/todos/current")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def list_archived_todos(self, job_id: str) -> list[dict[str, Any]]:
        """List all archived todo files for a job.

        Args:
            job_id: Job UUID

        Returns:
            List of archive metadata (filename, phase_name, timestamp)
        """
        resp = self._client.get(f"/api/jobs/{job_id}/todos/archives")
        resp.raise_for_status()
        return resp.json()

    def get_archived_todos(
        self, job_id: str, filename: str
    ) -> dict[str, Any] | None:
        """Get parsed content of an archived todo file.

        Args:
            job_id: Job UUID
            filename: Archive filename (e.g., todos_phase1_20260124_183618.md)

        Returns:
            Dict with parsed todos, summary, and metadata, or None if not found
        """
        resp = self._client.get(f"/api/jobs/{job_id}/todos/archives/{filename}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Graph Changes
    # =========================================================================

    def get_graph_changes(self, job_id: str) -> dict[str, Any]:
        """Get parsed graph changes for timeline visualization.

        Args:
            job_id: Job UUID

        Returns:
            Dict with jobId, timeRange, summary, snapshots, deltas
        """
        resp = self._client.get(f"/api/graph/changes/{job_id}")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # LLM Requests
    # =========================================================================

    def get_llm_request(self, doc_id: str) -> dict[str, Any]:
        """Get a single LLM request by MongoDB document ID.

        Args:
            doc_id: MongoDB ObjectId as string (24 hex characters)

        Returns:
            Full LLM request document with messages and response
        """
        resp = self._client.get(f"/api/requests/{doc_id}")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Tables (for debugging)
    # =========================================================================

    # =========================================================================
    # Job Actions (mutations)
    # =========================================================================

    def approve_job(self, job_id: str) -> dict[str, Any]:
        """Approve a frozen job, marking it as completed."""
        resp = self._client.post(f"/api/jobs/{job_id}/approve")
        resp.raise_for_status()
        return resp.json()

    def resume_job(
        self, job_id: str, feedback: str | None = None
    ) -> dict[str, Any]:
        """Resume a job from its checkpoint with optional feedback."""
        body: dict[str, Any] = {}
        if feedback:
            body["feedback"] = feedback
        resp = self._client.post(
            f"/api/jobs/{job_id}/resume",
            json=body if body else None,
        )
        resp.raise_for_status()
        return resp.json()

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        """Cancel a running job."""
        resp = self._client.put(f"/api/jobs/{job_id}/cancel")
        resp.raise_for_status()
        return resp.json()

    def create_job(
        self,
        description: str,
        config_name: str = "default",
        datasource_ids: list[str] | None = None,
        instructions: str | None = None,
        config_override: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new job."""
        body: dict[str, Any] = {
            "description": description,
            "config_name": config_name,
        }
        if datasource_ids:
            body["datasource_ids"] = datasource_ids
        if instructions:
            body["instructions"] = instructions
        if config_override:
            body["config_override"] = config_override
        if context:
            body["context"] = context
        resp = self._client.post("/api/jobs", json=body)
        resp.raise_for_status()
        return resp.json()

    def delete_job(self, job_id: str) -> dict[str, Any]:
        """Delete a job and its associated data."""
        resp = self._client.delete(f"/api/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    def assign_job(self, job_id: str, agent_id: str) -> dict[str, Any]:
        """Assign a job to an agent."""
        resp = self._client.post(f"/api/jobs/{job_id}/assign/{agent_id}")
        resp.raise_for_status()
        return resp.json()

    def test_datasource(self, datasource_id: str) -> dict[str, Any]:
        """Test connectivity to a datasource."""
        resp = self._client.post(f"/api/datasources/{datasource_id}/test")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Tables (for debugging)
    # =========================================================================

    def list_tables(self) -> list[dict[str, Any]]:
        """List available database tables with row counts."""
        resp = self._client.get("/api/tables")
        resp.raise_for_status()
        return resp.json()

    def get_table_data(
        self,
        table_name: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Get paginated table data.

        Args:
            table_name: Table name (jobs, requirements, citations, etc.)
            page: Page number (1-indexed, -1 for last page)
            page_size: Rows per page (1-500)

        Returns:
            Dict with data, total, page, pageSize, hasMore
        """
        params = {
            "page": page,
            "pageSize": page_size,
        }
        resp = self._client.get(f"/api/tables/{table_name}", params=params)
        resp.raise_for_status()
        return resp.json()


# =============================================================================
# Async Client
# =============================================================================


class AsyncCockpitClient:
    """Async HTTP client for the cockpit API with retry logic."""

    def __init__(self, base_url: str | None = None):
        """Initialize the async client.

        Args:
            base_url: Cockpit API URL. Defaults to COCKPIT_API_URL env var
                      or http://localhost:8085.
        """
        self.base_url = base_url or os.environ.get(
            "COCKPIT_API_URL", "http://localhost:8085"
        )
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncCockpitClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # =========================================================================
    # Health
    # =========================================================================

    @_create_retry_decorator()
    async def health_check(self) -> dict[str, str]:
        """Check API health status."""
        resp = await self._client.get("/api/health")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Jobs
    # =========================================================================

    @_create_retry_decorator()
    async def list_jobs(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List jobs with optional status filter.

        Args:
            status: Filter by status (pending, running, completed, failed)
            limit: Maximum number of jobs to return (1-500)

        Returns:
            List of job dicts with id, status, config, timestamps, audit_count
        """
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        resp = await self._client.get("/api/jobs", params=params)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_job(self, job_id: str) -> dict[str, Any]:
        """Get a single job by ID.

        Args:
            job_id: Job UUID

        Returns:
            Job dict with full details
        """
        resp = await self._client.get(f"/api/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Audit Trail
    # =========================================================================

    @_create_retry_decorator()
    async def get_audit_trail(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
        filter_category: FilterCategory = "all",
    ) -> dict[str, Any]:
        """Get paginated audit entries for a job.

        Args:
            job_id: Job UUID
            page: Page number (1-indexed, -1 for last page)
            page_size: Entries per page (1-200)
            filter_category: Filter type (all, messages, tools, errors)

        Returns:
            Dict with entries, total, page, pageSize, hasMore
        """
        params = {
            "page": page,
            "pageSize": page_size,
            "filter": filter_category,
        }
        resp = await self._client.get(f"/api/jobs/{job_id}/audit", params=params)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_audit_time_range(self, job_id: str) -> dict[str, str] | None:
        """Get first and last timestamps for job audit entries.

        Args:
            job_id: Job UUID

        Returns:
            Dict with start and end ISO timestamps, or None
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/audit/timerange")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Chat History
    # =========================================================================

    @_create_retry_decorator()
    async def get_chat_history(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Get paginated chat history for a job.

        Args:
            job_id: Job UUID
            page: Page number (1-indexed, -1 for last page)
            page_size: Entries per page (1-200)

        Returns:
            Dict with entries, total, page, pageSize, hasMore
        """
        params = {
            "page": page,
            "pageSize": page_size,
        }
        resp = await self._client.get(f"/api/jobs/{job_id}/chat", params=params)
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Todos
    # =========================================================================

    @_create_retry_decorator()
    async def get_todos(self, job_id: str) -> dict[str, Any]:
        """Get all todos for a job (current + archives).

        Args:
            job_id: Job UUID

        Returns:
            Dict with job_id, current, archives, has_workspace
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/todos")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_current_todos(self, job_id: str) -> dict[str, Any] | None:
        """Get current active todos from todos.yaml.

        Args:
            job_id: Job UUID

        Returns:
            Dict with todos list and metadata, or None if not found
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/todos/current")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_archived_todos(self, job_id: str) -> list[dict[str, Any]]:
        """List all archived todo files for a job.

        Args:
            job_id: Job UUID

        Returns:
            List of archive metadata (filename, phase_name, timestamp)
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/todos/archives")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_archived_todos(
        self, job_id: str, filename: str
    ) -> dict[str, Any] | None:
        """Get parsed content of an archived todo file.

        Args:
            job_id: Job UUID
            filename: Archive filename (e.g., todos_phase1_20260124_183618.md)

        Returns:
            Dict with parsed todos, summary, and metadata, or None if not found
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/todos/archives/{filename}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Graph Changes
    # =========================================================================

    @_create_retry_decorator()
    async def get_graph_changes(self, job_id: str) -> dict[str, Any]:
        """Get parsed graph changes for timeline visualization.

        Args:
            job_id: Job UUID

        Returns:
            Dict with jobId, timeRange, summary, snapshots, deltas
        """
        resp = await self._client.get(f"/api/graph/changes/{job_id}")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # LLM Requests
    # =========================================================================

    @_create_retry_decorator()
    async def get_llm_request(self, doc_id: str) -> dict[str, Any]:
        """Get a single LLM request by MongoDB document ID.

        Args:
            doc_id: MongoDB ObjectId as string (24 hex characters)

        Returns:
            Full LLM request document with messages and response
        """
        resp = await self._client.get(f"/api/requests/{doc_id}")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Job Actions (mutations)
    # =========================================================================

    @_create_retry_decorator()
    async def approve_job(self, job_id: str) -> dict[str, Any]:
        """Approve a frozen job, marking it as completed.

        Args:
            job_id: Job UUID

        Returns:
            Approval result with status and completion data
        """
        resp = await self._client.post(f"/api/jobs/{job_id}/approve")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def resume_job(
        self,
        job_id: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        """Resume a job from its checkpoint with optional feedback.

        Args:
            job_id: Job UUID
            feedback: Optional feedback to inject before resuming

        Returns:
            Resume result with status
        """
        body: dict[str, Any] = {}
        if feedback:
            body["feedback"] = feedback
        resp = await self._client.post(
            f"/api/jobs/{job_id}/resume",
            json=body if body else None,
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        """Cancel a running job.

        Args:
            job_id: Job UUID

        Returns:
            Cancellation result with status
        """
        resp = await self._client.put(f"/api/jobs/{job_id}/cancel")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def create_job(
        self,
        description: str,
        config_name: str = "default",
        datasource_ids: list[str] | None = None,
        instructions: str | None = None,
        config_override: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new job.

        Args:
            description: Natural language task description
            config_name: Expert/agent config to use
            datasource_ids: Global datasource IDs to clone as job-scoped
            instructions: Additional inline markdown instructions
            config_override: Per-job config overrides
            context: Additional context dictionary

        Returns:
            Created job record with ID
        """
        body: dict[str, Any] = {
            "description": description,
            "config_name": config_name,
        }
        if datasource_ids:
            body["datasource_ids"] = datasource_ids
        if instructions:
            body["instructions"] = instructions
        if config_override:
            body["config_override"] = config_override
        if context:
            body["context"] = context
        resp = await self._client.post("/api/jobs", json=body)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def delete_job(self, job_id: str) -> dict[str, Any]:
        """Delete a job and its associated data.

        Args:
            job_id: Job UUID

        Returns:
            Deletion result with status
        """
        resp = await self._client.delete(f"/api/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def assign_job(self, job_id: str, agent_id: str) -> dict[str, Any]:
        """Assign a job to an agent.

        Args:
            job_id: Job UUID
            agent_id: Agent UUID

        Returns:
            Assignment result with status
        """
        resp = await self._client.post(f"/api/jobs/{job_id}/assign/{agent_id}")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def test_datasource(self, datasource_id: str) -> dict[str, Any]:
        """Test connectivity to a datasource.

        Args:
            datasource_id: Datasource UUID

        Returns:
            Test result with status and message
        """
        resp = await self._client.post(f"/api/datasources/{datasource_id}/test")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Git History (Gitea proxy)
    # =========================================================================

    @_create_retry_decorator()
    async def list_job_commits(
        self,
        job_id: str,
        sha: str = "main",
        since_ref: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List git commits for a job's repository.

        Args:
            job_id: Job UUID
            sha: Branch, tag, or SHA to list from
            since_ref: Only show commits after this ref
            page: Page number
            limit: Max commits

        Returns:
            Dict with total_commits and commits list
        """
        params: dict[str, Any] = {"sha": sha, "page": page, "limit": limit}
        if since_ref:
            params["since_ref"] = since_ref
        resp = await self._client.get(
            f"/api/jobs/{job_id}/repo/commits", params=params
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_job_diff(
        self,
        job_id: str,
        base: str,
        head: str = "HEAD",
    ) -> dict[str, Any]:
        """Get unified diff between two refs in a job's repository.

        Args:
            job_id: Job UUID
            base: Base ref (commit SHA, tag, or branch)
            head: Head ref

        Returns:
            Dict with base, head, and diff text
        """
        resp = await self._client.get(
            f"/api/jobs/{job_id}/repo/diff",
            params={"base": base, "head": head},
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_job_file(
        self,
        job_id: str,
        path: str,
        ref: str | None = None,
    ) -> dict[str, Any]:
        """Read a file from a job's Gitea repository at any ref.

        Args:
            job_id: Job UUID
            path: File path within the repo
            ref: Branch, tag, or commit SHA

        Returns:
            Dict with path, content, and size
        """
        params: dict[str, Any] = {"path": path}
        if ref:
            params["ref"] = ref
        resp = await self._client.get(
            f"/api/jobs/{job_id}/repo/file", params=params
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_job_files(
        self,
        job_id: str,
        path: str = "",
        ref: str | None = None,
    ) -> list[dict[str, Any]]:
        """List directory contents from a job's Gitea repository.

        Args:
            job_id: Job UUID
            path: Directory path
            ref: Branch, tag, or commit SHA

        Returns:
            List of entries with name, path, type, size
        """
        params: dict[str, Any] = {"path": path}
        if ref:
            params["ref"] = ref
        resp = await self._client.get(
            f"/api/jobs/{job_id}/repo/contents", params=params
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_job_tags(self, job_id: str) -> list[dict[str, Any]]:
        """List tags in a job's repository.

        Args:
            job_id: Job UUID

        Returns:
            List of tags with name, sha, and message
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/repo/tags")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Workspace & Job Context
    # =========================================================================

    @_create_retry_decorator()
    async def get_frozen_job(self, job_id: str) -> dict[str, Any]:
        """Get frozen job review data.

        Args:
            job_id: Job UUID

        Returns:
            Frozen job data (summary, confidence, deliverables, notes)
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/frozen")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_workspace_file(self, job_id: str, path: str) -> dict[str, Any]:
        """Read a file from the job's local workspace.

        Args:
            job_id: Job UUID
            path: Relative path within the workspace

        Returns:
            Dict with path and content
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/workspace/{path}")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_workspace_overview(self, job_id: str) -> dict[str, Any]:
        """Get workspace overview with file listing and content previews.

        Args:
            job_id: Job UUID

        Returns:
            Workspace overview dict
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/workspace")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_job_progress(self, job_id: str) -> dict[str, Any]:
        """Get job progress including phase info and ETA.

        Args:
            job_id: Job UUID

        Returns:
            Progress data dict
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/progress")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_job_requirements(
        self,
        job_id: str,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Get extracted requirements for a job.

        Args:
            job_id: Job UUID
            status: Filter by validation status
            limit: Max results
            offset: Pagination offset

        Returns:
            Dict with requirements list and total count
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        resp = await self._client.get(
            f"/api/jobs/{job_id}/requirements", params=params
        )
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # System Monitoring
    # =========================================================================

    @_create_retry_decorator()
    async def get_job_stats(self) -> dict[str, Any]:
        """Get job queue statistics."""
        resp = await self._client.get("/api/stats/jobs")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_agent_stats(self) -> dict[str, Any]:
        """Get agent workforce summary."""
        resp = await self._client.get("/api/stats/agents")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_stuck_jobs(self, threshold_minutes: int = 30) -> list[dict[str, Any]]:
        """Get jobs stuck in processing beyond a threshold."""
        resp = await self._client.get(
            "/api/stats/stuck",
            params={"threshold_minutes": threshold_minutes},
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_agents(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List registered agents."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        resp = await self._client.get("/api/agents", params=params)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_experts(self) -> list[dict[str, Any]]:
        """List available expert configurations."""
        resp = await self._client.get("/api/experts")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_expert(self, expert_id: str) -> dict[str, Any]:
        """Get full expert config detail."""
        resp = await self._client.get(f"/api/experts/{expert_id}")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_datasources(
        self, ds_type: str | None = None
    ) -> list[dict[str, Any]]:
        """List configured datasources."""
        params: dict[str, Any] = {}
        if ds_type:
            params["type"] = ds_type
        resp = await self._client.get("/api/datasources", params=params)
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Tables (for debugging)
    # =========================================================================

    @_create_retry_decorator()
    async def list_tables(self) -> list[dict[str, Any]]:
        """List available database tables with row counts."""
        resp = await self._client.get("/api/tables")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_table_data(
        self,
        table_name: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Get paginated table data.

        Args:
            table_name: Table name (jobs, requirements, citations, etc.)
            page: Page number (1-indexed, -1 for last page)
            page_size: Rows per page (1-500)

        Returns:
            Dict with data, total, page, pageSize, hasMore
        """
        params = {
            "page": page,
            "pageSize": page_size,
        }
        resp = await self._client.get(f"/api/tables/{table_name}", params=params)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_table_schema(self, table_name: str) -> list[dict[str, Any]]:
        """Get column definitions for a table.

        Args:
            table_name: Table name

        Returns:
            List of column definitions with name, type, nullable, default
        """
        resp = await self._client.get(f"/api/tables/{table_name}/schema")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Citation & Source Library
    # =========================================================================

    @_create_retry_decorator()
    async def list_job_sources(
        self,
        job_id: str | None = None,
        source_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List sources, optionally filtered by job and/or type.

        Args:
            job_id: Filter by job UUID (omit for all sources)
            source_type: Filter by type (document, website, database, custom)
            limit: Max results
            offset: Pagination offset

        Returns:
            Dict with sources list and total count
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if job_id:
            params["job_id"] = job_id
        if source_type:
            params["type"] = source_type
        resp = await self._client.get("/api/sources", params=params)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_source_detail(
        self,
        source_id: int,
        content_limit: int = 2000,
    ) -> dict[str, Any]:
        """Get full detail for a single source.

        Args:
            source_id: Source ID (integer)
            content_limit: Max characters of content to return

        Returns:
            Source record with type, identifier, name, content, metadata
        """
        params: dict[str, Any] = {"content_limit": content_limit}
        resp = await self._client.get(f"/api/sources/{source_id}", params=params)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_job_citations(
        self,
        job_id: str,
        source_id: int | None = None,
        verification_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List citations for a job with optional filters.

        Args:
            job_id: Job UUID
            source_id: Filter by source ID
            verification_status: Filter by status (pending, verified, failed, unverified)
            limit: Max results
            offset: Pagination offset

        Returns:
            Dict with citations list and total count
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if source_id is not None:
            params["source_id"] = source_id
        if verification_status:
            params["status"] = verification_status
        resp = await self._client.get(
            f"/api/jobs/{job_id}/citations", params=params
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_citation_detail(self, citation_id: int) -> dict[str, Any]:
        """Get full citation record with source info and verification details.

        Args:
            citation_id: Citation ID (integer)

        Returns:
            Full citation with claim, quote, source, verification data
        """
        resp = await self._client.get(f"/api/citations/{citation_id}")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def search_job_sources(
        self,
        job_id: str,
        query: str,
        mode: str = "keyword",
        source_type: str | None = None,
        tags: str | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        """Search a job's source library.

        Args:
            job_id: Job UUID
            query: Search query text
            mode: Search mode (keyword, semantic, hybrid)
            source_type: Filter by source type
            tags: Comma-separated tags (AND logic)
            top_k: Max results

        Returns:
            Search results with evidence labels and snippets
        """
        params: dict[str, Any] = {"query": query, "mode": mode, "top_k": top_k}
        if source_type:
            params["source_type"] = source_type
        if tags:
            params["tags"] = tags
        resp = await self._client.get(
            f"/api/jobs/{job_id}/sources/search", params=params
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_source_annotations(
        self,
        job_id: str,
        source_id: int,
        annotation_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get annotations for a source within a job.

        Args:
            job_id: Job UUID
            source_id: Source ID
            annotation_type: Filter by type (note, highlight, summary, question, critique)

        Returns:
            List of annotations with type, content, page reference
        """
        params: dict[str, Any] = {}
        if annotation_type:
            params["type"] = annotation_type
        resp = await self._client.get(
            f"/api/jobs/{job_id}/sources/{source_id}/annotations", params=params
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_source_tags(self, job_id: str, source_id: int) -> list[str]:
        """Get tags for a source within a job.

        Args:
            job_id: Job UUID
            source_id: Source ID

        Returns:
            List of tag strings
        """
        resp = await self._client.get(
            f"/api/jobs/{job_id}/sources/{source_id}/tags"
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_citation_stats(self, job_id: str) -> dict[str, Any]:
        """Get citation statistics for a job.

        Args:
            job_id: Job UUID

        Returns:
            Stats with source/citation counts by type, status, confidence, method
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/citations/stats")
        resp.raise_for_status()
        return resp.json()
