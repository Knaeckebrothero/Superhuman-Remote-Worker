#!/usr/bin/env python3
"""Cancel a job in the Graph-RAG system.

Migrated to use PostgresDB with sync wrappers (Phase 3 of database refactoring).

Usage:
    python scripts/cancel_job.py --job-id <uuid>
    python scripts/cancel_job.py --job-id <uuid> --force
"""

import argparse
import json
import os
import sys
import uuid as uuid_module

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import PostgresDB


def get_job(job_id: str) -> dict | None:
    """Get job by ID."""
    db = PostgresDB()
    db.connect_sync()

    try:
        job_uuid = uuid_module.UUID(job_id)
        return db.jobs.get_sync(job_uuid)
    finally:
        db.close_sync()


def get_requirement_counts(job_id: str) -> dict:
    """Get requirement counts."""
    db = PostgresDB()
    db.connect_sync()

    try:
        job_uuid = uuid_module.UUID(job_id)
        row = db.fetchrow_sync(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'integrated') as integrated,
                COUNT(*) FILTER (WHERE status = 'pending') as pending
            FROM requirements WHERE job_id = $1
            """,
            job_uuid
        )
        return row
    finally:
        db.close_sync()


def cancel_job(job_id: str) -> bool:
    """Cancel a job."""
    db = PostgresDB()
    db.connect_sync()

    try:
        job_uuid = uuid_module.UUID(job_id)
        result = db.execute_sync(
            """
            UPDATE jobs
            SET status = $1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $2 AND status NOT IN ('completed', 'cancelled')
            """,
            'cancelled', job_uuid
        )
        return "UPDATE 1" in result
    finally:
        db.close_sync()


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
