#!/usr/bin/env python3
"""Check status of a job in the Graph-RAG system.

Migrated to use PostgresDB with sync wrappers (Phase 3 of database refactoring).

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
    """Get requirement counts by status."""
    db = PostgresDB()
    db.connect_sync()

    try:
        job_uuid = uuid_module.UUID(job_id)
        row = db.fetchrow_sync(
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
            job_uuid
        )
        return row
    finally:
        db.close_sync()


def get_job_progress(job_id: str) -> dict:
    """Get detailed progress with ETA."""
    job = get_job(job_id)
    if not job:
        return {"error": "Job not found"}

    req_counts = get_requirement_counts(job_id)

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
        "job": job,
        "requirements": req_counts,
        "progress_percent": round(progress_percent, 1),
        "elapsed_seconds": elapsed_seconds,
        "eta_seconds": eta_seconds
    }


def print_job_status(job: dict, req_counts: dict):
    """Print job status in a human-readable format."""
    print("\nJob Status")
    print("=" * 60)
    print(f"ID:                {job['id']}")
    print(f"Prompt:            {job.get('prompt', 'N/A')}")
    print(f"Document:          {job.get('document_path', 'N/A')}")
    print(f"Status:            {job['status']}")
    print(f"Creator Status:    {job['creator_status']}")
    print(f"Validator Status:  {job['validator_status']}")
    print(f"Created:           {job['created_at']}")
    print(f"Updated:           {job['updated_at']}")
    if job.get('completed_at'):
        print(f"Completed:         {job['completed_at']}")
    if job.get('error_message'):
        print(f"Error:             {job['error_message']}")

    print("\nRequirements")
    print("-" * 60)
    print(f"Total:             {req_counts['total']}")
    print(f"Pending:           {req_counts['pending']}")
    print(f"Validating:        {req_counts['validating']}")
    print(f"Integrated:        {req_counts['integrated']}")
    print(f"Rejected:          {req_counts['rejected']}")
    print(f"Failed:            {req_counts['failed']}")
    print("=" * 60)


def print_progress(progress: dict):
    """Print progress information with ETA."""
    job = progress['job']
    req_counts = progress['requirements']

    print("\nJob Progress")
    print("=" * 60)
    print(f"ID:                {job['id']}")
    print(f"Status:            {job['status']}")
    print(f"Progress:          {progress['progress_percent']:.1f}%")
    print(f"Elapsed:           {progress['elapsed_seconds']:.0f}s")
    if progress['eta_seconds']:
        eta_minutes = progress['eta_seconds'] / 60
        print(f"ETA:               ~{eta_minutes:.1f} minutes")
    else:
        print(f"ETA:               N/A")

    print("\nRequirements:")
    print(f"  Total:           {req_counts['total']}")
    print(f"  Pending:         {req_counts['pending']}")
    print(f"  Validating:      {req_counts['validating']}")
    print(f"  Integrated:      {req_counts['integrated']}")
    print(f"  Rejected:        {req_counts['rejected']}")
    print(f"  Failed:          {req_counts['failed']}")
    print("=" * 60)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Check job status")
    parser.add_argument("--job-id", "-j", required=True, help="Job ID (UUID)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--progress", action="store_true", help="Show progress with ETA")

    args = parser.parse_args()

    # Validate UUID
    try:
        uuid_module.UUID(args.job_id)
    except ValueError:
        print(f"Error: Invalid job ID format: {args.job_id}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.progress:
            progress = get_job_progress(args.job_id)
            if "error" in progress:
                print(f"Error: {progress['error']}", file=sys.stderr)
                sys.exit(1)

            if args.json:
                # Convert datetime objects to strings for JSON
                output = {
                    "job_id": str(progress['job']['id']),
                    "status": progress['job']['status'],
                    "progress_percent": progress['progress_percent'],
                    "requirements": progress['requirements'],
                    "elapsed_seconds": progress['elapsed_seconds'],
                    "eta_seconds": progress['eta_seconds']
                }
                print(json.dumps(output, indent=2))
            else:
                print_progress(progress)
        else:
            job = get_job(args.job_id)
            if not job:
                print(f"Error: Job not found: {args.job_id}", file=sys.stderr)
                sys.exit(1)

            req_counts = get_requirement_counts(args.job_id)

            if args.json:
                output = {
                    "job": {k: str(v) if isinstance(v, datetime) or isinstance(v, uuid_module.UUID) else v
                            for k, v in job.items()},
                    "requirements": req_counts
                }
                print(json.dumps(output, default=str, indent=2))
            else:
                print_job_status(job, req_counts)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
