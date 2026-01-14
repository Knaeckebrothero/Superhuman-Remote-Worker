"""Database utilities for the Streamlit dashboard.

Enhanced with features salvaged from src/orchestrator/ (monitor.py, reporter.py).
"""

import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor


def get_connection_string() -> str:
    """Get PostgreSQL connection string from environment."""
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://graphrag:graphrag_password@localhost:5432/graphrag"
    )


@contextmanager
def get_connection():
    """Context manager for database connections."""
    conn = psycopg2.connect(get_connection_string())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_job(prompt: str, document_path: str | None = None) -> UUID:
    """Create a new job in the database."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (prompt, document_path, status, creator_status, validator_status)
                VALUES (%s, %s, 'created', 'pending', 'pending')
                RETURNING id
                """,
                (prompt, document_path)
            )
            result = cur.fetchone()
            return result[0]


def list_jobs(status_filter: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """List jobs with optional status filter."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if status_filter:
                cur.execute(
                    """
                    SELECT id, prompt, document_path, status, creator_status, validator_status,
                           created_at, updated_at, completed_at, error_message,
                           total_tokens_used, total_requests
                    FROM jobs
                    WHERE status = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (status_filter, limit)
                )
            else:
                cur.execute(
                    """
                    SELECT id, prompt, document_path, status, creator_status, validator_status,
                           created_at, updated_at, completed_at, error_message,
                           total_tokens_used, total_requests
                    FROM jobs
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,)
                )
            return [dict(row) for row in cur.fetchall()]


def get_job(job_id: UUID | str) -> dict[str, Any] | None:
    """Get a specific job by ID."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, prompt, document_path, status, creator_status, validator_status,
                       created_at, updated_at, completed_at, error_message, error_details,
                       total_tokens_used, total_requests, context
                FROM jobs
                WHERE id = %s
                """,
                (str(job_id),)
            )
            row = cur.fetchone()
            return dict(row) if row else None


def delete_job(job_id: UUID | str) -> bool:
    """Delete a job and its requirements (cascade)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id = %s", (str(job_id),))
            return cur.rowcount > 0


def assign_to_creator(job_id: UUID | str) -> bool:
    """Assign a job to the Creator agent for processing."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'processing',
                    creator_status = 'pending',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status IN ('created', 'failed')
                """,
                (str(job_id),)
            )
            return cur.rowcount > 0


def assign_to_validator(job_id: UUID | str) -> bool:
    """Assign a job to the Validator agent for processing."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'processing',
                    validator_status = 'pending',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND creator_status = 'completed'
                """,
                (str(job_id),)
            )
            return cur.rowcount > 0


def get_requirements(job_id: UUID | str) -> list[dict[str, Any]]:
    """Get all requirements for a job."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, requirement_id, text, name, type, priority,
                       source_document, source_location,
                       gobd_relevant, gdpr_relevant,
                       status, neo4j_id, rejection_reason,
                       confidence, created_at, updated_at
                FROM requirements
                WHERE job_id = %s
                ORDER BY created_at
                """,
                (str(job_id),)
            )
            return [dict(row) for row in cur.fetchall()]


def get_requirement_summary(job_id: UUID | str) -> dict[str, int]:
    """Get requirement counts by status for a job."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending') as pending,
                    COUNT(*) FILTER (WHERE status = 'validating') as validating,
                    COUNT(*) FILTER (WHERE status = 'integrated') as integrated,
                    COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
                    COUNT(*) FILTER (WHERE status = 'failed') as failed,
                    COUNT(*) as total
                FROM requirements
                WHERE job_id = %s
                """,
                (str(job_id),)
            )
            row = cur.fetchone()
            return dict(row) if row else {
                "pending": 0, "validating": 0, "integrated": 0,
                "rejected": 0, "failed": 0, "total": 0
            }


def get_job_stats() -> dict[str, Any]:
    """Get overall job statistics."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
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
            return dict(cur.fetchone())


# --- Features salvaged from src/orchestrator/monitor.py ---


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

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT j.id, j.prompt, j.status, j.creator_status, j.validator_status,
                       j.created_at, j.updated_at,
                       COUNT(r.id) FILTER (WHERE r.status = 'pending') as pending_requirements,
                       COUNT(r.id) FILTER (WHERE r.status = 'integrated') as integrated_requirements
                FROM jobs j
                LEFT JOIN requirements r ON j.id = r.job_id
                WHERE j.status = 'processing'
                AND j.updated_at < %s
                GROUP BY j.id
                ORDER BY j.updated_at ASC
                """,
                (threshold,)
            )
            rows = cur.fetchall()

    stuck_jobs = []
    for row in rows:
        job = dict(row)
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
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get job info
            cur.execute(
                """
                SELECT id, prompt, status, creator_status, validator_status,
                       created_at, updated_at, completed_at
                FROM jobs WHERE id = %s
                """,
                (str(job_id),)
            )
            job = cur.fetchone()
            if not job:
                return {"error": "Job not found"}
            job = dict(job)

            # Get requirement counts
            cur.execute(
                """
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE status = 'pending') as pending,
                    COUNT(*) FILTER (WHERE status = 'validating') as validating,
                    COUNT(*) FILTER (WHERE status = 'integrated') as integrated,
                    COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
                    COUNT(*) FILTER (WHERE status = 'failed') as failed
                FROM requirements WHERE job_id = %s
                """,
                (str(job_id),)
            )
            req_counts = dict(cur.fetchone())

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


# --- Features salvaged from src/orchestrator/reporter.py ---


def get_daily_statistics(days: int = 7) -> list[dict[str, Any]]:
    """Get daily job statistics for the past N days.

    Args:
        days: Number of days to include

    Returns:
        List of daily statistics dictionaries
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    DATE(created_at) as date,
                    COUNT(*) as jobs_created,
                    COUNT(*) FILTER (WHERE status = 'completed') as jobs_completed,
                    COUNT(*) FILTER (WHERE status = 'failed') as jobs_failed
                FROM jobs
                WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '%s days'
                GROUP BY DATE(created_at)
                ORDER BY date DESC
                """,
                (days,)
            )
            return [dict(row) for row in cur.fetchall()]


def get_requirement_statistics(job_id: UUID | str) -> dict[str, Any]:
    """Get detailed requirement statistics for a job.

    Args:
        job_id: Job UUID

    Returns:
        Dictionary with requirement breakdowns by status, priority, relevance
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
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
                WHERE job_id = %s
                """,
                (str(job_id),)
            )
            row = cur.fetchone()
            return dict(row) if row else {}


def cancel_job(job_id: UUID | str) -> bool:
    """Cancel a job by setting its status to 'cancelled'.

    Args:
        job_id: Job UUID

    Returns:
        True if job was cancelled, False if not found or already completed
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'cancelled',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status NOT IN ('completed', 'cancelled')
                """,
                (str(job_id),)
            )
            return cur.rowcount > 0


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
    import json

    # Validate document count
    if document_paths and len(document_paths) > 10:
        raise ValueError("Maximum 10 documents allowed per job")

    # Store first document path in document_path column, rest in context
    doc_path = document_paths[0] if document_paths else None
    if document_paths and len(document_paths) > 1:
        context = context or {}
        context['additional_documents'] = document_paths[1:]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (prompt, document_path, context, status, creator_status, validator_status)
                VALUES (%s, %s, %s, 'processing', 'pending', 'pending')
                RETURNING id
                """,
                (prompt, doc_path, json.dumps(context) if context else None)
            )
            result = cur.fetchone()
            return result[0]


def create_validator_job(job_id: UUID | str) -> bool:
    """Trigger validation for a job that has completed creator phase.

    Args:
        job_id: Job UUID

    Returns:
        True if validation was triggered, False otherwise
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET validator_status = 'pending',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND creator_status = 'completed'
                """,
                (str(job_id),)
            )
            return cur.rowcount > 0


def get_jobs_ready_for_validation() -> list[dict[str, Any]]:
    """Get jobs that are ready for validation (creator completed).

    Returns:
        List of jobs where creator is done but validator hasn't started
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
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
            return [dict(row) for row in cur.fetchall()]
