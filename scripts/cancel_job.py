#!/usr/bin/env python3
"""Cancel a job in the Graph-RAG system.

Standalone CLI tool using direct psycopg2 connection.

Usage:
    python scripts/cancel_job.py --job-id <uuid>
    python scripts/cancel_job.py --job-id <uuid> --force
"""

import argparse
import json
import os
import sys
import uuid as uuid_module
from contextlib import contextmanager

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


def get_job(job_id: str) -> dict | None:
    """Get job by ID."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, prompt, status, creator_status, validator_status
                FROM jobs WHERE id = %s
                """,
                (job_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_requirement_counts(job_id: str) -> dict:
    """Get requirement counts."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'integrated') as integrated,
                    COUNT(*) FILTER (WHERE status = 'pending') as pending
                FROM requirements WHERE job_id = %s
                """,
                (job_id,)
            )
            return dict(cur.fetchone())


def cancel_job(job_id: str) -> bool:
    """Cancel a job."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'cancelled',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status NOT IN ('completed', 'cancelled')
                """,
                (job_id,)
            )
            return cur.rowcount > 0


def main():
    parser = argparse.ArgumentParser(
        description="Cancel a job in the Graph-RAG system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000 --force
        """
    )

    parser.add_argument("--job-id", required=True, help="Job UUID")
    parser.add_argument("--force", action="store_true", help="Skip confirmation")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    # Validate job ID
    try:
        job_id = str(uuid_module.UUID(args.job_id))
    except ValueError:
        if args.json:
            print(json.dumps({"error": f"Invalid job ID: {args.job_id}"}))
        else:
            print(f"Error: Invalid job ID: {args.job_id}", file=sys.stderr)
        sys.exit(1)

    # Get job
    job = get_job(job_id)
    if not job:
        if args.json:
            print(json.dumps({"error": "Job not found"}))
        else:
            print(f"Error: Job {job_id} not found", file=sys.stderr)
        sys.exit(1)

    # Check status
    if job['status'] in ('completed', 'cancelled'):
        if args.json:
            print(json.dumps({
                "error": f"Job already {job['status']}",
                "job_id": job_id,
                "status": job['status']
            }))
        else:
            print(f"Error: Job is already {job['status']}", file=sys.stderr)
        sys.exit(1)

    # Confirm
    if not args.force and not args.json:
        req_counts = get_requirement_counts(job_id)
        print(f"Job ID: {job_id}")
        print(f"Status: {job['status']}")
        prompt = job['prompt'] or ""
        print(f"Prompt: {prompt[:80]}{'...' if len(prompt) > 80 else ''}")
        print()
        print("Requirements:")
        print(f"  Integrated: {req_counts.get('integrated', 0)}")
        print(f"  Pending:    {req_counts.get('pending', 0)}")
        print()
        response = input("Are you sure you want to cancel this job? [y/N] ")
        if response.lower() != 'y':
            print("Cancellation aborted")
            sys.exit(0)

    # Cancel
    success = cancel_job(job_id)

    if not success:
        if args.json:
            print(json.dumps({"error": "Failed to cancel job", "job_id": job_id}))
        else:
            print(f"Error: Failed to cancel job {job_id}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({
            "success": True,
            "job_id": job_id,
            "status": "cancelled"
        }))
    else:
        print(f"Job {job_id} cancelled successfully")


if __name__ == "__main__":
    main()
