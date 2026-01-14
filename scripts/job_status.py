#!/usr/bin/env python3
"""Check status of a job in the Graph-RAG system.

Standalone CLI tool using direct psycopg2 connection.

Usage:
    python scripts/job_status.py --job-id <uuid>
    python scripts/job_status.py --job-id <uuid> --json
    python scripts/job_status.py --job-id <uuid> --progress
"""

import argparse
import json
import os
import sys
import uuid as uuid_module
from datetime import datetime
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
    finally:
        conn.close()


def get_job(job_id: str) -> dict | None:
    """Get job by ID."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, prompt, document_path, status, creator_status, validator_status,
                       created_at, updated_at, completed_at, error_message
                FROM jobs WHERE id = %s
                """,
                (job_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_requirement_counts(job_id: str) -> dict:
    """Get requirement counts by status."""
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
                    COUNT(*) FILTER (WHERE status = 'failed') as failed
                FROM requirements WHERE job_id = %s
                """,
                (job_id,)
            )
            return dict(cur.fetchone())


def get_job_progress(job_id: str) -> dict:
    """Get detailed progress with ETA."""
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}

    req_counts = get_requirement_counts(job_id)

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
        eta_seconds = (total - processed) * avg_time_per_req

    return {
        "job_id": str(job['id']),
        "status": job['status'],
        "creator_status": job['creator_status'],
        "validator_status": job['validator_status'],
        "requirements": req_counts,
        "progress_percent": round(progress_percent, 1),
        "elapsed_seconds": elapsed_seconds,
        "eta_seconds": eta_seconds,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Check status of a job",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000 --json
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000 --progress
        """
    )

    parser.add_argument("--job-id", required=True, help="Job UUID")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--progress", action="store_true", help="Show detailed progress")

    args = parser.parse_args()

    # Validate job ID
    try:
        job_id = str(uuid_module.UUID(args.job_id))
    except ValueError:
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

    # Progress view
    if args.progress:
        progress = get_job_progress(job_id)
        if args.json:
            print(json.dumps(progress, indent=2, default=str))
        else:
            print(f"Job: {progress['job_id']}")
            print(f"Status: {progress['status']}")
            print(f"Creator: {progress['creator_status']}")
            print(f"Validator: {progress['validator_status']}")
            print(f"Progress: {progress['progress_percent']:.1f}%")
            print()
            print("Requirements:")
            for key, value in progress['requirements'].items():
                print(f"  {key}: {value}")
            print()
            print(f"Elapsed: {progress['elapsed_seconds']/3600:.1f} hours")
            if progress.get('eta_seconds'):
                print(f"ETA: {progress['eta_seconds']/3600:.1f} hours")
        return

    # JSON output
    if args.json:
        req_counts = get_requirement_counts(job_id)
        output = {**job, "requirement_counts": req_counts}
        for key, value in output.items():
            if isinstance(value, datetime):
                output[key] = value.isoformat()
        print(json.dumps(output, indent=2, default=str))
        return

    # Default text output
    req_counts = get_requirement_counts(job_id)

    print(f"Job ID:           {job['id']}")
    print(f"Status:           {job['status']}")
    print(f"Creator Status:   {job['creator_status']}")
    print(f"Validator Status: {job['validator_status']}")
    print(f"Created:          {job['created_at']}")

    if req_counts['total'] > 0:
        print()
        print("Requirements:")
        print(f"  Total:      {req_counts['total']}")
        print(f"  Integrated: {req_counts['integrated']}")
        print(f"  Pending:    {req_counts['pending']}")
        print(f"  Rejected:   {req_counts['rejected']}")
        print(f"  Failed:     {req_counts['failed']}")

        processed = req_counts['integrated'] + req_counts['rejected']
        progress = (processed / req_counts['total'] * 100)
        print(f"  Progress:   {progress:.1f}%")

    if job.get('error_message'):
        print()
        print(f"Error: {job['error_message']}")


if __name__ == "__main__":
    main()
