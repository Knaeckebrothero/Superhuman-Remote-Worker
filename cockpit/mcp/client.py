"""HTTP client for the Cockpit API.

Provides synchronous methods to interact with the debug cockpit's REST API.
"""

from __future__ import annotations

import os
from typing import Any, Literal

import httpx


FilterCategory = Literal["all", "messages", "tools", "errors"]


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
