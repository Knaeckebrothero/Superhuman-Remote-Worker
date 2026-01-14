"""Database utilities for the Streamlit dashboard."""

import os
from contextlib import contextmanager
from datetime import datetime
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
