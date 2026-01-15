"""Database utilities for the Streamlit dashboard.

Migrated to use PostgresDB with sync wrappers (Phase 3 of database refactoring).
Enhanced with features salvaged from src/orchestrator/ (monitor.py, reporter.py).
"""

import json
import sys
import os
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import PostgresDB

# Create module-level PostgresDB instance for dashboard use
# This uses sync wrappers, so it can be used in Streamlit without async/await
_db_instance = None


def _get_db() -> PostgresDB:
    """Get or create the PostgresDB instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = PostgresDB()
        _db_instance.connect_sync()
    return _db_instance


# ============================================================================
# JOB MANAGEMENT
# ============================================================================


def create_job(prompt: str, document_path: str | None = None) -> UUID:
    """Create a new job in the database."""
    db = _get_db()
    job_id = db.jobs.create_sync(prompt=prompt, document_path=document_path)
    # Update status to 'created' (default from namespace is 'processing')
    db.execute_sync(
        "UPDATE jobs SET status = $1, creator_status = $2, validator_status = $3 WHERE id = $4",
        'created', 'pending', 'pending', job_id
    )
    return job_id


def list_jobs(status_filter: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """List jobs with optional status filter."""
    db = _get_db()
    if status_filter:
        return db.jobs.list_sync(status=status_filter, limit=limit)
    else:
        return db.jobs.list_sync(limit=limit)


def get_job(job_id: UUID | str) -> dict[str, Any] | None:
    """Get a specific job by ID."""
    db = _get_db()
    if isinstance(job_id, str):
        job_id = UUID(job_id)
    return db.jobs.get_sync(job_id)


def delete_job(job_id: UUID | str) -> bool:
    """Delete a job and its requirements (cascade)."""
    db = _get_db()
    if isinstance(job_id, str):
        job_id = UUID(job_id)
    result = db.execute_sync("DELETE FROM jobs WHERE id = $1", job_id)
    return "DELETE" in result


def assign_to_creator(job_id: UUID | str) -> bool:
    """Assign a job to the Creator agent for processing."""
    db = _get_db()
    if isinstance(job_id, str):
        job_id = UUID(job_id)
    result = db.execute_sync(
        """
        UPDATE jobs
        SET status = $1,
            creator_status = $2,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = $3 AND status IN ('created', 'failed')
        """,
        'processing', 'pending', job_id
    )
    return "UPDATE 1" in result


def assign_to_validator(job_id: UUID | str) -> bool:
    """Assign a job to the Validator agent for processing."""
    db = _get_db()
    if isinstance(job_id, str):
        job_id = UUID(job_id)
    result = db.execute_sync(
        """
        UPDATE jobs
        SET status = $1,
            validator_status = $2,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = $3 AND creator_status = 'completed'
        """,
        'processing', 'pending', job_id
    )
    return "UPDATE 1" in result


# ============================================================================
# REQUIREMENTS
# ============================================================================


def get_requirements(job_id: UUID | str) -> list[dict[str, Any]]:
    """Get all requirements for a job."""
    db = _get_db()
    if isinstance(job_id, str):
        job_id = UUID(job_id)
    return db.requirements.list_by_job_sync(job_id, limit=10000)


def get_requirement_summary(job_id: UUID | str) -> dict[str, int]:
    """Get requirement counts by status for a job."""
    db = _get_db()
    if isinstance(job_id, str):
        job_id = UUID(job_id)

    row = db.fetchrow_sync(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'pending') as pending,
            COUNT(*) FILTER (WHERE status = 'validating') as validating,
            COUNT(*) FILTER (WHERE status = 'integrated') as integrated,
            COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
            COUNT(*) FILTER (WHERE status = 'failed') as failed,
            COUNT(*) as total
        FROM requirements
        WHERE job_id = $1
        """,
        job_id
    )
    return row if row else {
        "pending": 0, "validating": 0, "integrated": 0,
        "rejected": 0, "failed": 0, "total": 0
    }


# ============================================================================
# STATISTICS
# ============================================================================


def get_job_stats() -> dict[str, Any]:
    """Get overall job statistics."""
    db = _get_db()
    return db.fetchrow_sync(
        """
        SELECT
            COUNT(*) as total_jobs,
            COUNT(*) FILTER (WHERE status = 'created') as created,
            COUNT(*) FILTER (WHERE status = 'processing') as processing,
            COUNT(*) FILTER (WHERE status = 'completed') as completed,
            COUNT(*) FILTER (WHERE status = 'failed') as failed,
            COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled
        FROM jobs
        """
    )


def get_daily_statistics(days: int = 7) -> list[dict[str, Any]]:
    """Get daily job statistics for the past N days.

    Args:
        days: Number of days to include

    Returns:
        List of daily statistics dictionaries
    """
    db = _get_db()
    return db.fetch_sync(
        """
        SELECT
            DATE(created_at) as date,
            COUNT(*) as jobs_created,
            COUNT(*) FILTER (WHERE status = 'completed') as jobs_completed,
            COUNT(*) FILTER (WHERE status = 'failed') as jobs_failed
        FROM jobs
        WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '1 day' * $1
        GROUP BY DATE(created_at)
        ORDER BY date DESC
        """,
        days
    )


def get_requirement_statistics(job_id: UUID | str) -> dict[str, Any]:
    """Get detailed requirement statistics for a job.

    Args:
        job_id: Job UUID

    Returns:
        Dictionary with requirement breakdowns by status, priority, relevance
    """
    db = _get_db()
    if isinstance(job_id, str):
        job_id = UUID(job_id)

    row = db.fetchrow_sync(
        """
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status = 'pending') as pending,
            COUNT(*) FILTER (WHERE status = 'validating') as validating,
            COUNT(*) FILTER (WHERE status = 'integrated') as integrated,
            COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
            COUNT(*) FILTER (WHERE status = 'failed') as failed,
            COUNT(*) FILTER (WHERE gobd_relevant = true) as gobd_relevant,
            COUNT(*) FILTER (WHERE gdpr_relevant = true) as gdpr_relevant,
            COUNT(*) FILTER (WHERE priority = 'high') as high_priority,
            COUNT(*) FILTER (WHERE priority = 'medium') as medium_priority,
            COUNT(*) FILTER (WHERE priority = 'low') as low_priority
        FROM requirements
        WHERE job_id = $1
        """,
        job_id
    )
    return row if row else {}


# ============================================================================
# MONITORING (salvaged from src/orchestrator/monitor.py)
# ============================================================================


def detect_stuck_jobs(threshold_minutes: int = 60) -> list[dict[str, Any]]:
    """Detect jobs that appear to be stuck.

    A job is considered stuck if it's in 'processing' status but hasn't
    been updated within the threshold period.

    Args:
        threshold_minutes: Minutes without activity to consider stuck

    Returns:
        List of stuck job dictionaries with stuck reason
    """
    threshold = datetime.utcnow() - timedelta(minutes=threshold_minutes)
    db = _get_db()

    rows = db.fetch_sync(
        """
        SELECT j.id, j.prompt, j.status, j.creator_status, j.validator_status,
               j.created_at, j.updated_at,
               COUNT(r.id) FILTER (WHERE r.status = 'pending') as pending_requirements,
               COUNT(r.id) FILTER (WHERE r.status = 'integrated') as integrated_requirements
        FROM jobs j
        LEFT JOIN requirements r ON j.id = r.job_id
        WHERE j.status = 'processing'
        AND j.updated_at < $1
        GROUP BY j.id
        ORDER BY j.updated_at ASC
        """,
        threshold
    )

    stuck_jobs = []
    for job in rows:
        # Determine stuck reason
        if job['creator_status'] == 'processing' and job['integrated_requirements'] == 0:
            job['stuck_reason'] = "Creator not producing requirements"
            job['stuck_component'] = "creator"
        elif job['creator_status'] == 'completed' and job['pending_requirements'] > 0:
            job['stuck_reason'] = f"Validator not processing {job['pending_requirements']} pending requirements"
            job['stuck_component'] = "validator"
        else:
            job['stuck_reason'] = "No recent activity"
            job['stuck_component'] = "unknown"
        stuck_jobs.append(job)

    return stuck_jobs


def get_job_progress(job_id: UUID | str) -> dict[str, Any]:
    """Get detailed progress information for a job including ETA.

    Args:
        job_id: Job UUID

    Returns:
        Dictionary with progress details and estimated time remaining
    """
    db = _get_db()
    if isinstance(job_id, str):
        job_id = UUID(job_id)

    # Get job info
    job = db.fetchrow_sync(
        """
        SELECT id, prompt, status, creator_status, validator_status,
               created_at, updated_at, completed_at
        FROM jobs WHERE id = $1
        """,
        job_id
    )
    if not job:
        return {"error": "Job not found"}

    # Get requirement counts
    req_counts = db.fetchrow_sync(
        """
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status = 'pending') as pending,
            COUNT(*) FILTER (WHERE status = 'validating') as validating,
            COUNT(*) FILTER (WHERE status = 'integrated') as integrated,
            COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
            COUNT(*) FILTER (WHERE status = 'failed') as failed
        FROM requirements WHERE job_id = $1
        """,
        job_id
    )

    # Calculate progress
    total = req_counts['total']
    processed = req_counts['integrated'] + req_counts['rejected']
    progress_percent = (processed / total * 100) if total > 0 else 0

    # Calculate ETA
    created_at = job['created_at']
    if created_at.tzinfo:
        created_at = created_at.replace(tzinfo=None)
    elapsed = datetime.utcnow() - created_at
    elapsed_seconds = elapsed.total_seconds()

    eta_seconds = None
    if processed > 0 and total > processed:
        avg_time_per_req = elapsed_seconds / processed
        remaining = (total - processed) * avg_time_per_req
        eta_seconds = remaining

    return {
        "job_id": str(job['id']),
        "status": job['status'],
        "creator_status": job['creator_status'],
        "validator_status": job['validator_status'],
        "requirements": req_counts,
        "progress_percent": round(progress_percent, 1),
        "elapsed_seconds": elapsed_seconds,
        "eta_seconds": eta_seconds,
        "created_at": job['created_at'].isoformat() if job['created_at'] else None,
    }


# ============================================================================
# JOB ACTIONS
# ============================================================================


def cancel_job(job_id: UUID | str) -> bool:
    """Cancel a job by setting its status to 'cancelled'.

    Args:
        job_id: Job UUID

    Returns:
        True if job was cancelled, False if not found or already completed
    """
    db = _get_db()
    if isinstance(job_id, str):
        job_id = UUID(job_id)

    result = db.execute_sync(
        """
        UPDATE jobs
        SET status = $1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = $2 AND status NOT IN ('completed', 'cancelled')
        """,
        'cancelled', job_id
    )
    return "UPDATE 1" in result


def create_creator_job(
    prompt: str,
    document_paths: list[str] | None = None,
    context: dict[str, Any] | None = None
) -> UUID:
    """Create a new job for the Creator agent.

    Args:
        prompt: Task prompt for the agent
        document_paths: List of document paths (up to 10)
        context: Optional context dictionary

    Returns:
        UUID of the created job
    """
    # Validate document count
    if document_paths and len(document_paths) > 10:
        raise ValueError("Maximum 10 documents allowed per job")

    # Store first document path in document_path column, rest in context
    doc_path = document_paths[0] if document_paths else None
    if document_paths and len(document_paths) > 1:
        context = context or {}
        context['additional_documents'] = document_paths[1:]

    db = _get_db()
    job_id = db.fetchval_sync(
        """
        INSERT INTO jobs (prompt, document_path, context, status, creator_status, validator_status)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        prompt, doc_path, json.dumps(context) if context else None,
        'processing', 'pending', 'pending'
    )
    return job_id


def create_validator_job(job_id: UUID | str) -> bool:
    """Trigger validation for a job that has completed creator phase.

    Args:
        job_id: Job UUID

    Returns:
        True if validation was triggered, False otherwise
    """
    db = _get_db()
    if isinstance(job_id, str):
        job_id = UUID(job_id)

    result = db.execute_sync(
        """
        UPDATE jobs
        SET validator_status = $1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = $2 AND creator_status = 'completed'
        """,
        'pending', job_id
    )
    return "UPDATE 1" in result


def get_jobs_ready_for_validation() -> list[dict[str, Any]]:
    """Get jobs that are ready for validation (creator completed).

    Returns:
        List of jobs where creator is done but validator hasn't started
    """
    db = _get_db()
    return db.fetch_sync(
        """
        SELECT j.id, j.prompt, j.document_path, j.created_at, j.updated_at,
               COUNT(r.id) as requirement_count
        FROM jobs j
        LEFT JOIN requirements r ON j.id = r.job_id
        WHERE j.creator_status = 'completed'
        AND j.validator_status IN ('pending', 'failed')
        AND j.status != 'cancelled'
        GROUP BY j.id
        ORDER BY j.updated_at DESC
        """
    )
