"""Job Manager for the Graph-RAG Autonomous Agent System.

This module provides job lifecycle management including creation, status tracking,
cancellation, and document storage.
"""

import os
import uuid
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from src.database.postgres_utils import (
    PostgresConnection,
    create_job,
    get_job,
    update_job_status,
    count_requirements_by_status,
)

logger = logging.getLogger(__name__)


class JobManager:
    """Manages job lifecycle for the orchestrator.

    Handles job creation, status queries, cancellation, and document storage.
    Works with both Creator and Validator agents through the shared PostgreSQL database.

    Example:
        ```python
        manager = JobManager(postgres_conn, workspace_path="/app/workspace")

        # Create a new job
        job_id = await manager.create_job(
            prompt="Analyze GDPR requirements",
            document_path="/path/to/document.pdf",
            context={"domain": "car_rental"}
        )

        # Check job status
        status = await manager.get_job_status(job_id)

        # Cancel job
        await manager.cancel_job(job_id)
        ```
    """

    def __init__(
        self,
        conn: PostgresConnection,
        workspace_path: Optional[str] = None
    ):
        """Initialize the JobManager.

        Args:
            conn: PostgreSQL connection instance
            workspace_path: Path to workspace directory for document storage.
                           Defaults to ./workspace
        """
        self.conn = conn
        self.workspace_path = Path(workspace_path or os.getenv("WORKSPACE_PATH", "./workspace"))
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"JobManager initialized with workspace: {self.workspace_path}")

    async def create_new_job(
        self,
        prompt: str,
        document_path: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        store_document: bool = True
    ) -> uuid.UUID:
        """Create a new job with optional document storage.

        Args:
            prompt: User prompt describing the requirement analysis task
            document_path: Path to source document (PDF, DOCX, etc.)
            context: Additional context dictionary (domain, region, etc.)
            store_document: Whether to copy document to workspace

        Returns:
            UUID of the created job

        Raises:
            FileNotFoundError: If document_path is provided but file doesn't exist
            ValueError: If prompt is empty
        """
        if not prompt or not prompt.strip():
            raise ValueError("Prompt cannot be empty")

        # Validate and store document
        stored_path = None
        if document_path:
            source_path = Path(document_path)
            if not source_path.exists():
                raise FileNotFoundError(f"Document not found: {document_path}")

            if store_document:
                stored_path = await self._store_document(source_path)
            else:
                stored_path = str(source_path.absolute())

        # Create job in database
        job_id = await create_job(
            self.conn,
            prompt=prompt.strip(),
            document_path=stored_path,
            context=context
        )

        # Update status to processing
        await update_job_status(self.conn, job_id, status="processing")

        logger.info(f"Created job {job_id} with document: {stored_path}")
        return job_id

    async def _store_document(self, source_path: Path) -> str:
        """Store document in workspace directory.

        Args:
            source_path: Path to source document

        Returns:
            Path to stored document
        """
        # Create job-specific directory
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        doc_dir = self.workspace_path / f"documents_{timestamp}"
        doc_dir.mkdir(parents=True, exist_ok=True)

        # Copy document
        dest_path = doc_dir / source_path.name
        shutil.copy2(source_path, dest_path)

        logger.debug(f"Stored document: {source_path} -> {dest_path}")
        return str(dest_path)

    async def get_job_status(self, job_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Get detailed job status including requirement counts.

        Args:
            job_id: Job UUID

        Returns:
            Dictionary with job details and requirement statistics, or None if not found
        """
        job = await get_job(self.conn, job_id)
        if not job:
            return None

        # Get requirement counts
        req_counts = await count_requirements_by_status(self.conn, job_id)

        # Calculate progress
        total_reqs = sum(req_counts.values())
        processed_reqs = req_counts.get('integrated', 0) + req_counts.get('rejected', 0)

        return {
            **job,
            "requirement_counts": req_counts,
            "total_requirements": total_reqs,
            "processed_requirements": processed_reqs,
            "progress_percent": (processed_reqs / total_reqs * 100) if total_reqs > 0 else 0,
        }

    async def list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """List jobs with optional filtering.

        Args:
            status: Filter by job status (optional)
            limit: Maximum number of jobs to return
            offset: Number of jobs to skip

        Returns:
            List of job dictionaries
        """
        if status:
            rows = await self.conn.fetch(
                """
                SELECT * FROM job_summary
                WHERE status = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                """,
                status, limit, offset
            )
        else:
            rows = await self.conn.fetch(
                """
                SELECT * FROM job_summary
                ORDER BY created_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit, offset
            )

        return [dict(row) for row in rows]

    async def cancel_job(self, job_id: uuid.UUID) -> bool:
        """Cancel a job.

        Sets job status to 'cancelled'. Does not affect already integrated requirements.

        Args:
            job_id: Job UUID

        Returns:
            True if job was cancelled, False if job not found or already completed
        """
        job = await get_job(self.conn, job_id)
        if not job:
            logger.warning(f"Job {job_id} not found for cancellation")
            return False

        if job['status'] in ('completed', 'cancelled'):
            logger.warning(f"Job {job_id} already {job['status']}, cannot cancel")
            return False

        await update_job_status(
            self.conn,
            job_id,
            status="cancelled",
            creator_status="failed" if job['creator_status'] != 'completed' else None,
            validator_status="failed" if job['validator_status'] != 'completed' else None
        )

        logger.info(f"Cancelled job {job_id}")
        return True

    async def update_creator_status(
        self,
        job_id: uuid.UUID,
        status: str,
        error_message: Optional[str] = None
    ) -> None:
        """Update Creator Agent status for a job.

        Args:
            job_id: Job UUID
            status: New status (pending, processing, completed, failed)
            error_message: Error message if failed
        """
        await update_job_status(
            self.conn,
            job_id,
            creator_status=status,
            error_message=error_message if status == 'failed' else None
        )
        logger.info(f"Updated job {job_id} creator_status to {status}")

    async def update_validator_status(
        self,
        job_id: uuid.UUID,
        status: str,
        error_message: Optional[str] = None
    ) -> None:
        """Update Validator Agent status for a job.

        Args:
            job_id: Job UUID
            status: New status (pending, processing, completed, failed)
            error_message: Error message if failed
        """
        await update_job_status(
            self.conn,
            job_id,
            validator_status=status,
            error_message=error_message if status == 'failed' else None
        )
        logger.info(f"Updated job {job_id} validator_status to {status}")

    async def mark_job_completed(self, job_id: uuid.UUID) -> None:
        """Mark a job as completed.

        Args:
            job_id: Job UUID
        """
        await update_job_status(self.conn, job_id, status="completed")
        logger.info(f"Marked job {job_id} as completed")

    async def mark_job_failed(
        self,
        job_id: uuid.UUID,
        error_message: str
    ) -> None:
        """Mark a job as failed.

        Args:
            job_id: Job UUID
            error_message: Error description
        """
        await update_job_status(
            self.conn,
            job_id,
            status="failed",
            error_message=error_message
        )
        logger.info(f"Marked job {job_id} as failed: {error_message}")

    async def get_pending_jobs(self) -> List[Dict[str, Any]]:
        """Get jobs that need processing.

        Returns jobs where creator_status is 'pending' or 'processing'.

        Returns:
            List of pending job dictionaries
        """
        rows = await self.conn.fetch(
            """
            SELECT * FROM jobs
            WHERE status = 'processing'
            AND (creator_status IN ('pending', 'processing')
                 OR validator_status IN ('pending', 'processing'))
            ORDER BY created_at ASC
            """
        )
        return [dict(row) for row in rows]

    async def get_job_document_path(self, job_id: uuid.UUID) -> Optional[str]:
        """Get the document path for a job.

        Args:
            job_id: Job UUID

        Returns:
            Document path or None if no document
        """
        job = await get_job(self.conn, job_id)
        if job:
            return job.get('document_path')
        return None

    async def cleanup_workspace(self, job_id: uuid.UUID) -> None:
        """Clean up workspace files for a completed/cancelled job.

        Args:
            job_id: Job UUID
        """
        job = await get_job(self.conn, job_id)
        if not job:
            return

        doc_path = job.get('document_path')
        if doc_path and doc_path.startswith(str(self.workspace_path)):
            doc_dir = Path(doc_path).parent
            if doc_dir.exists():
                shutil.rmtree(doc_dir)
                logger.info(f"Cleaned up workspace for job {job_id}")


def create_job_manager(
    conn: PostgresConnection,
    workspace_path: Optional[str] = None
) -> JobManager:
    """Factory function to create a JobManager instance.

    Args:
        conn: PostgreSQL connection instance
        workspace_path: Path to workspace directory

    Returns:
        JobManager instance
    """
    return JobManager(conn, workspace_path)
