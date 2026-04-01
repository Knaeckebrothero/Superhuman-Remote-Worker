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
            status: Filter by status (created, processing, completed, failed, cancelled, pending_review)
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

    def get_archived_todos(self, job_id: str, filename: str) -> dict[str, Any] | None:
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

    def resume_job(self, job_id: str, feedback: str | None = None) -> dict[str, Any]:
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
        self._scope_headers: dict[str, str] = {}
        self._internal_key = os.environ.get("MCP_INTERNAL_KEY", "")

    def set_scope_headers(self, user_id: str, scope: str) -> None:
        """Set per-request scope headers from authenticated MCP token.

        Updates the httpx client's default headers so all subsequent
        requests include the scope context without modifying each call site.
        """
        self._client.headers["X-MCP-User-Id"] = user_id
        self._client.headers["X-MCP-Scope"] = scope
        if self._internal_key:
            self._client.headers["X-Internal-Key"] = self._internal_key

    def clear_scope_headers(self) -> None:
        """Remove scope headers (unauthenticated or stdio mode)."""
        self._client.headers.pop("X-MCP-User-Id", None)
        self._client.headers.pop("X-MCP-Scope", None)
        self._client.headers.pop("X-Internal-Key", None)

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
            status: Filter by status (created, processing, completed, failed, cancelled, pending_review)
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
    async def get_audit_bulk(
        self,
        job_id: str,
        offset: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Get bulk audit entries for a job (offset/limit based).

        Args:
            job_id: Job UUID
            offset: Number of entries to skip
            limit: Maximum entries to return (up to 500 for MCP)

        Returns:
            Dict with entries, total, offset, limit, hasMore
        """
        resp = await self._client.get(
            f"/api/jobs/{job_id}/audit/bulk",
            params={"offset": offset, "limit": limit},
        )
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

    @_create_retry_decorator()
    async def get_chat_bulk(
        self,
        job_id: str,
        offset: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Get bulk chat history entries for a job (offset/limit based).

        Args:
            job_id: Job UUID
            offset: Number of entries to skip
            limit: Maximum entries to return (up to 500 for MCP)

        Returns:
            Dict with entries, total, offset, limit, hasMore
        """
        resp = await self._client.get(
            f"/api/jobs/{job_id}/chat/bulk",
            params={"offset": offset, "limit": limit},
        )
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
    async def pause_job(self, job_id: str) -> dict[str, Any]:
        """Pause a running job.

        Args:
            job_id: Job UUID

        Returns:
            Pause result with status
        """
        resp = await self._client.put(f"/api/jobs/{job_id}/pause")
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
        parent_job_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new job.

        Args:
            description: Natural language task description
            config_name: Expert/agent config to use
            datasource_ids: Global datasource IDs to clone as job-scoped
            instructions: Additional inline markdown instructions
            config_override: Per-job config overrides
            context: Additional context dictionary
            parent_job_id: Parent job UUID for verification/follow-up jobs
            project_id: Project UUID to associate this job with
            user_id: User UUID who created this job

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
        if parent_job_id:
            body["parent_job_id"] = parent_job_id
        if project_id:
            body["project_id"] = project_id
        if user_id:
            body["user_id"] = user_id
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
        resp = await self._client.get(f"/api/jobs/{job_id}/repo/commits", params=params)
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
        resp = await self._client.get(f"/api/jobs/{job_id}/repo/file", params=params)
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

    @_create_retry_decorator()
    async def get_daily_stats(self, days: int = 7) -> list[dict[str, Any]]:
        """Get daily job statistics for the past N days.

        Args:
            days: Number of days to look back (1-90, default 7)

        Returns:
            List of daily stats with date, jobs_created, jobs_completed,
            jobs_failed, jobs_cancelled
        """
        resp = await self._client.get("/api/stats/daily", params={"days": days})
        resp.raise_for_status()
        return resp.json()

    async def reload_experts(self) -> dict[str, Any]:
        """Force reload of expert configurations from disk.

        Returns:
            Dict with status and count of loaded experts
        """
        resp = await self._client.post("/api/experts/reload")
        resp.raise_for_status()
        return resp.json()

    async def deregister_agent(self, agent_id: str) -> dict[str, str]:
        """Deregister (delete) an agent.

        Args:
            agent_id: Agent UUID

        Returns:
            Status dict
        """
        resp = await self._client.delete(f"/api/agents/{agent_id}")
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
        resp = await self._client.get(f"/api/jobs/{job_id}/citations", params=params)
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
        resp = await self._client.get(f"/api/jobs/{job_id}/sources/{source_id}/tags")
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

    @_create_retry_decorator()
    async def get_memory_stats(self, job_id: str) -> dict[str, Any]:
        """Get memory statistics for a job.

        Args:
            job_id: Job UUID

        Returns:
            Stats with counts by type, source, tokens, accesses, avg importance
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/memory/stats")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Logs & LLM Requests
    # =========================================================================

    @_create_retry_decorator()
    async def get_job_logs(
        self,
        job_id: str,
        lines: int = 100,
        grep: str | None = None,
        level: str | None = None,
    ) -> dict[str, Any]:
        """Get tail of a job's log file with optional filtering.

        Args:
            job_id: Job UUID
            lines: Number of tail lines (1-1000)
            grep: Case-insensitive substring filter
            level: Log level filter (DEBUG, INFO, WARNING, ERROR)

        Returns:
            Dict with lines list, total_lines, filtered flag
        """
        params: dict[str, Any] = {"lines": lines}
        if grep:
            params["grep"] = grep
        if level:
            params["level"] = level
        resp = await self._client.get(f"/api/jobs/{job_id}/logs", params=params)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_llm_requests(
        self,
        job_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List LLM requests for a job with summary fields.

        Args:
            job_id: Job UUID
            limit: Max entries (1-100)
            offset: Pagination offset

        Returns:
            Dict with entries, total, offset, limit, hasMore
        """
        resp = await self._client.get(
            f"/api/jobs/{job_id}/llm-requests",
            params={"limit": limit, "offset": offset},
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_shell_state(self, job_id: str) -> dict[str, Any]:
        """Get shell state for a job's agent.

        Proxied through the orchestrator to the agent's /system/shell-state endpoint.

        Args:
            job_id: Job UUID

        Returns:
            Dict with tabs list, each containing name, type, recent output
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/shell-state")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Agent System Monitoring
    # =========================================================================

    @_create_retry_decorator()
    async def get_agent_system_info(self, agent_id: str) -> dict[str, Any]:
        """Get system information from an agent's container.

        Proxied through the orchestrator to the agent's /system/info endpoint.
        Returns CPU, memory, disk, listening ports, processes, and network connections.

        Args:
            agent_id: Agent UUID

        Returns:
            Dict with cpu, memory, disk, listening_ports, processes,
            network_connections, and agent info
        """
        resp = await self._client.get(f"/api/agents/{agent_id}/system-info")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Knowledge Base
    # =========================================================================

    @_create_retry_decorator()
    async def get_knowledge_summary(self, project_id: str) -> dict[str, Any]:
        """Get knowledge base summary statistics for a project.

        Args:
            project_id: Project UUID

        Returns:
            Dict with total, by_type, by_status, and recent notes
        """
        resp = await self._client.get(f"/api/projects/{project_id}/knowledge/summary")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_knowledge_notes(
        self,
        project_id: str,
        note_type: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        job_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List knowledge notes for a project with optional filters.

        Args:
            project_id: Project UUID
            note_type: Filter by type (insight, decision, pattern, issue, etc.)
            status: Filter by status (active, resolved, superseded, archived)
            tag: Filter by tag
            job_id: Filter by originating job
            limit: Max results (1-200)
            offset: Pagination offset

        Returns:
            Dict with notes list, total, limit, offset
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if note_type:
            params["type"] = note_type
        if status:
            params["status"] = status
        if tag:
            params["tag"] = tag
        if job_id:
            params["job_id"] = job_id
        resp = await self._client.get(
            f"/api/projects/{project_id}/knowledge", params=params
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_knowledge_note(self, project_id: str, note_id: str) -> dict[str, Any]:
        """Get a single knowledge note with full content and relationships.

        Args:
            project_id: Project UUID
            note_id: Note ID

        Returns:
            Full note record with content, metadata, and Neo4j relationships
        """
        resp = await self._client.get(f"/api/projects/{project_id}/knowledge/{note_id}")
        resp.raise_for_status()
        return resp.json()

    async def search_knowledge(
        self,
        project_id: str,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Hybrid search over project knowledge base.

        Uses dense vector + sparse keyword search when embeddings are
        available, falls back to keyword-only search otherwise.

        Args:
            project_id: Project UUID
            query: Search query text
            limit: Max results (1-50)

        Returns:
            Dict with notes list, query, and total count
        """
        resp = await self._client.post(
            f"/api/projects/{project_id}/knowledge/search",
            json={"query": query, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Projects
    # =========================================================================

    @_create_retry_decorator()
    async def list_projects(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """List projects, optionally filtered by user membership.

        Args:
            user_id: Filter to projects this user belongs to

        Returns:
            List of project dicts
        """
        params: dict[str, Any] = {}
        if user_id:
            params["user_id"] = user_id
        resp = await self._client.get("/api/projects", params=params)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_project(self, project_id: str) -> dict[str, Any]:
        """Get a single project by ID.

        Args:
            project_id: Project UUID

        Returns:
            Project dict with name, description, goal, config, timestamps
        """
        resp = await self._client.get(f"/api/projects/{project_id}")
        resp.raise_for_status()
        return resp.json()

    async def create_project(
        self,
        name: str,
        user_id: str,
        description: str | None = None,
        goal: str | None = None,
        default_config_name: str | None = None,
        default_config_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new project.

        Args:
            name: Project name
            user_id: Owner user UUID
            description: Project description
            goal: Project goal statement
            default_config_name: Default agent config for new jobs
            default_config_override: Default config overrides for new jobs

        Returns:
            Created project record with ID
        """
        body: dict[str, Any] = {"name": name, "user_id": user_id}
        if description:
            body["description"] = description
        if goal:
            body["goal"] = goal
        if default_config_name:
            body["default_config_name"] = default_config_name
        if default_config_override:
            body["default_config_override"] = default_config_override
        resp = await self._client.post("/api/projects", json=body)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_project_jobs(
        self,
        project_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List jobs belonging to a project.

        Args:
            project_id: Project UUID
            status: Optional status filter
            limit: Max results (1-500)

        Returns:
            List of job dicts
        """
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        resp = await self._client.get(f"/api/projects/{project_id}/jobs", params=params)
        resp.raise_for_status()
        return resp.json()

    async def create_project_job(
        self,
        project_id: str,
        description: str,
        config_name: str = "default",
        datasource_ids: list[str] | None = None,
        instructions: str | None = None,
        config_override: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a job within a project context.

        Args:
            project_id: Project UUID
            description: Task description
            config_name: Expert/agent config to use
            datasource_ids: Global datasource IDs to clone
            instructions: Additional inline instructions
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
        resp = await self._client.post(f"/api/projects/{project_id}/jobs", json=body)
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Project Management (Extended)
    # =========================================================================

    async def update_project(
        self,
        project_id: str,
        name: str | None = None,
        description: str | None = None,
        goal: str | None = None,
        status: str | None = None,
        default_config_name: str | None = None,
        default_config_override: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Update a project's metadata or defaults.

        Args:
            project_id: Project UUID
            name: New name
            description: New description
            goal: New goal statement
            status: New status
            default_config_name: New default agent config
            default_config_override: New default config overrides

        Returns:
            Status dict
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if goal is not None:
            body["goal"] = goal
        if status is not None:
            body["status"] = status
        if default_config_name is not None:
            body["default_config_name"] = default_config_name
        if default_config_override is not None:
            body["default_config_override"] = default_config_override
        resp = await self._client.patch(f"/api/projects/{project_id}", json=body)
        resp.raise_for_status()
        return resp.json()

    async def delete_project(self, project_id: str) -> dict[str, str]:
        """Delete a project and its associated data.

        Cannot delete default projects.

        Args:
            project_id: Project UUID

        Returns:
            Status dict
        """
        resp = await self._client.delete(f"/api/projects/{project_id}")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_project_members(self, project_id: str) -> list[dict[str, Any]]:
        """List members of a project with their roles.

        Args:
            project_id: Project UUID

        Returns:
            List of member dicts with user_id, role, display_name, etc.
        """
        resp = await self._client.get(f"/api/projects/{project_id}/members")
        resp.raise_for_status()
        return resp.json()

    async def add_project_member(
        self,
        project_id: str,
        user_id: str,
        role: str = "editor",
    ) -> dict[str, Any]:
        """Add a member to a project.

        Args:
            project_id: Project UUID
            user_id: User UUID to add
            role: Member role (owner, editor, viewer)

        Returns:
            Created member record
        """
        body = {"user_id": user_id, "role": role}
        resp = await self._client.post(f"/api/projects/{project_id}/members", json=body)
        resp.raise_for_status()
        return resp.json()

    async def update_project_member(
        self,
        project_id: str,
        user_id: str,
        role: str,
    ) -> dict[str, str]:
        """Update a project member's role.

        Args:
            project_id: Project UUID
            user_id: User UUID
            role: New role (owner, editor, viewer)

        Returns:
            Status dict
        """
        resp = await self._client.patch(
            f"/api/projects/{project_id}/members/{user_id}",
            json={"role": role},
        )
        resp.raise_for_status()
        return resp.json()

    async def remove_project_member(
        self,
        project_id: str,
        user_id: str,
    ) -> dict[str, str]:
        """Remove a member from a project.

        Cannot remove the last owner.

        Args:
            project_id: Project UUID
            user_id: User UUID to remove

        Returns:
            Status dict
        """
        resp = await self._client.delete(
            f"/api/projects/{project_id}/members/{user_id}"
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_project_experts(self, project_id: str) -> list[dict[str, Any]]:
        """List project-specific expert configurations.

        Args:
            project_id: Project UUID

        Returns:
            List of ExpertInfo dicts
        """
        resp = await self._client.get(f"/api/projects/{project_id}/experts")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_project_expert(
        self, project_id: str, expert_name: str
    ) -> dict[str, Any]:
        """Get detailed expert configuration for a project.

        Args:
            project_id: Project UUID
            expert_name: Expert config name

        Returns:
            ExpertInfo with merged config and instructions
        """
        resp = await self._client.get(
            f"/api/projects/{project_id}/experts/{expert_name}"
        )
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Datasource CRUD
    # =========================================================================

    async def create_datasource(
        self,
        name: str,
        ds_type: str,
        connection_url: str,
        description: str | None = None,
        credentials: dict[str, Any] | None = None,
        read_only: bool = True,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new datasource.

        Args:
            name: User-provided label
            ds_type: Type (postgresql, neo4j, mongodb)
            connection_url: Full connection string
            description: What this datasource contains
            credentials: Additional auth details (e.g. username/password for Neo4j)
            read_only: Whether the agent is allowed to write
            job_id: Job UUID for job-scoped, None for global

        Returns:
            Created datasource record with ID
        """
        body: dict[str, Any] = {
            "name": name,
            "type": ds_type,
            "connection_url": connection_url,
            "read_only": read_only,
        }
        if description:
            body["description"] = description
        if credentials:
            body["credentials"] = credentials
        if job_id:
            body["job_id"] = job_id
        resp = await self._client.post("/api/datasources", json=body)
        resp.raise_for_status()
        return resp.json()

    async def update_datasource(
        self,
        datasource_id: str,
        name: str | None = None,
        description: str | None = None,
        connection_url: str | None = None,
        credentials: dict[str, Any] | None = None,
        read_only: bool | None = None,
    ) -> dict[str, str]:
        """Update a datasource.

        Args:
            datasource_id: Datasource UUID
            name: New label
            description: New description
            connection_url: New connection string
            credentials: New auth details
            read_only: New read-only flag

        Returns:
            Status dict
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if connection_url is not None:
            body["connection_url"] = connection_url
        if credentials is not None:
            body["credentials"] = credentials
        if read_only is not None:
            body["read_only"] = read_only
        resp = await self._client.put(f"/api/datasources/{datasource_id}", json=body)
        resp.raise_for_status()
        return resp.json()

    async def delete_datasource(self, datasource_id: str) -> dict[str, str]:
        """Delete a datasource.

        Args:
            datasource_id: Datasource UUID

        Returns:
            Status dict
        """
        resp = await self._client.delete(f"/api/datasources/{datasource_id}")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Knowledge Base Mutations
    # =========================================================================

    async def update_knowledge_note(
        self,
        project_id: str,
        note_id: str,
        status: str | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> dict[str, str]:
        """Update a knowledge note's status or tags.

        Args:
            project_id: Project UUID
            note_id: Note ID
            status: New status (active, resolved, superseded, archived)
            add_tags: Tags to add
            remove_tags: Tags to remove

        Returns:
            Status dict
        """
        body: dict[str, Any] = {}
        if status:
            body["status"] = status
        if add_tags:
            body["add_tags"] = add_tags
        if remove_tags:
            body["remove_tags"] = remove_tags
        resp = await self._client.patch(
            f"/api/projects/{project_id}/knowledge/{note_id}", json=body
        )
        resp.raise_for_status()
        return resp.json()

    async def delete_knowledge_note(
        self, project_id: str, note_id: str
    ) -> dict[str, str]:
        """Hard delete a knowledge note from both stores.

        Args:
            project_id: Project UUID
            note_id: Note ID

        Returns:
            Status dict
        """
        resp = await self._client.delete(
            f"/api/projects/{project_id}/knowledge/{note_id}"
        )
        resp.raise_for_status()
        return resp.json()

    async def export_knowledge(self, project_id: str) -> dict[str, Any]:
        """Export project knowledge base as Obsidian-compatible markdown.

        Args:
            project_id: Project UUID

        Returns:
            Dict with status, path, note_count, project_name
        """
        resp = await self._client.post(f"/api/projects/{project_id}/knowledge/export")
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Job Promotion
    # =========================================================================

    async def promote_job(
        self,
        job_id: str,
        name: str,
        user_id: str,
        description: str | None = None,
        goal: str | None = None,
    ) -> dict[str, Any]:
        """Promote a completed job into a dedicated project.

        Creates a new project, seeds its repo from the job's branch,
        and moves the job to the new project.

        Args:
            job_id: Job UUID (must be completed)
            name: Name for the new project
            user_id: Owner user UUID
            description: Project description
            goal: Project goal

        Returns:
            Dict with status, project_id, project_name, job_id
        """
        body: dict[str, Any] = {"name": name, "user_id": user_id}
        if description:
            body["description"] = description
        if goal:
            body["goal"] = goal
        resp = await self._client.post(f"/api/jobs/{job_id}/promote", json=body)
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Sudo Approval Gate
    # =========================================================================

    @_create_retry_decorator()
    async def list_sudo_requests(
        self,
        job_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List sudo approval requests.

        Args:
            job_id: Optional job ID filter.
            status: Optional status filter (pending, approved, denied, expired).
            limit: Maximum results (default 50).

        Returns:
            List of sudo approval request dicts.
        """
        params: dict[str, Any] = {"limit": limit}
        if job_id:
            params["job_id"] = job_id
        if status:
            params["status"] = status
        resp = await self._client.get("/api/sudo/requests", params=params)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def approve_sudo_request(
        self,
        request_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Approve a pending sudo request.

        Args:
            request_id: UUID of the sudo approval request.
            reason: Optional approval reason.

        Returns:
            Dict with id and status.
        """
        body: dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        resp = await self._client.post(
            f"/api/sudo/requests/{request_id}/approve", json=body
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def deny_sudo_request(
        self,
        request_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Deny a pending sudo request.

        Args:
            request_id: UUID of the sudo approval request.
            reason: Denial reason (required).

        Returns:
            Dict with id and status.
        """
        resp = await self._client.post(
            f"/api/sudo/requests/{request_id}/deny",
            json={"reason": reason},
        )
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Messaging
    # =========================================================================

    @_create_retry_decorator()
    async def list_message_threads(self, job_id: str) -> dict[str, Any]:
        """List message threads for a job.

        Args:
            job_id: Job UUID.

        Returns:
            Dict with ``threads`` list.
        """
        resp = await self._client.get(f"/api/jobs/{job_id}/messages")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def reply_to_message(
        self,
        job_id: str,
        thread_id: str,
        message: str,
        urgent: bool = False,
    ) -> dict[str, Any]:
        """Send a reply to an agent message thread.

        Args:
            job_id: Job UUID.
            thread_id: Thread ID.
            message: Reply body.
            urgent: Whether to deliver as immediate interrupt.

        Returns:
            Dict with delivery strategy and sequence.
        """
        resp = await self._client.post(
            f"/api/jobs/{job_id}/messages/{thread_id}/reply",
            json={"message": message, "urgent": urgent},
        )
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # Persistent Threads
    # =========================================================================

    @_create_retry_decorator()
    async def create_persistent_thread(
        self,
        config_name: str = "defaults",
        title: str = "Untitled Session",
        permission_mode: str = "supervised",
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Create a new persistent thread.

        Returns:
            Dict with ``thread_id`` and ``status``.
        """
        body: dict[str, Any] = {
            "config_name": config_name,
            "title": title,
            "permission_mode": permission_mode,
        }
        if project_id:
            body["project_id"] = project_id
        if project_ids:
            body["project_ids"] = project_ids
        if model:
            body["model"] = model
        if temperature is not None:
            body["temperature"] = temperature
        resp = await self._client.post("/api/persistent/threads", json=body)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def list_persistent_threads(
        self,
        project_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List persistent threads for the authenticated user.

        Returns:
            Dict with ``threads`` list.
        """
        params: dict[str, Any] = {}
        if project_id:
            params["project_id"] = project_id
        if status:
            params["status"] = status
        resp = await self._client.get("/api/persistent/threads", params=params)
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_persistent_thread(self, thread_id: str) -> dict[str, Any]:
        """Get persistent thread details.

        Returns:
            Full thread dict.
        """
        resp = await self._client.get(f"/api/persistent/threads/{thread_id}")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def end_persistent_thread(
        self, thread_id: str, permanent: bool = False
    ) -> dict[str, Any]:
        """End or permanently delete a persistent thread.

        Returns:
            Dict with ``status`` ('ended' or 'deleted').
        """
        resp = await self._client.delete(
            f"/api/persistent/threads/{thread_id}",
            params={"permanent": permanent},
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def resume_persistent_thread(self, thread_id: str) -> dict[str, Any]:
        """Resume an ended or idle persistent thread.

        Returns:
            Dict with ``status`` and ``thread_id``.
        """
        resp = await self._client.post(
            f"/api/persistent/threads/{thread_id}/resume"
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_persistent_thread_messages(
        self,
        thread_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Get message history for a persistent thread.

        Returns:
            Dict with ``messages``, ``total``, and ``thread_id``.
        """
        resp = await self._client.get(
            f"/api/persistent/threads/{thread_id}/messages",
            params={"limit": limit, "offset": offset},
        )
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_persistent_thread_ide(self, thread_id: str) -> dict[str, Any]:
        """Get IDE session status for a persistent thread.

        Returns:
            Dict with ``status``, ``code_server_url``, ``source``, ``gitea_url``.
        """
        resp = await self._client.get(
            f"/api/persistent/threads/{thread_id}/ide"
        )
        resp.raise_for_status()
        return resp.json()
