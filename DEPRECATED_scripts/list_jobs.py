#!/usr/bin/env python3
"""List jobs in the Graph-RAG system.

Migrated to use PostgresDB with sync wrappers (Phase 3 of database refactoring).

Usage:
    python scripts/list_jobs.py
    python scripts/list_jobs.py --status processing
    python scripts/list_jobs.py --limit 10 --json
    python scripts/list_jobs.py --stats
"""

import argparse
import json
import os
import sys
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import PostgresDB


def list_jobs(status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """List jobs with optional filtering."""
    db = PostgresDB()
    db.connect_sync()

    try:
        rows = db.fetch_sync(
            """
            SELECT j.id, j.prompt, j.status, j.creator_status, j.validator_status,
                   j.created_at, j.updated_at,
                   COUNT(r.id) FILTER (WHERE r.status = 'integrated') as integrated_requirements,
                   COUNT(r.id) FILTER (WHERE r.status = 'pending') as pending_requirements
            FROM jobs j
            LEFT JOIN requirements r ON j.id = r.job_id
            """ + ("WHERE j.status = $1" if status else "") + """
            GROUP BY j.id
            ORDER BY j.created_at DESC
            LIMIT $""" + ("2" if status else "1") + """ OFFSET $""" + ("3" if status else "2") + """
            """,
            *(([status, limit, offset] if status else [limit, offset]))
        )
        return rows
    finally:
        db.close_sync()


def get_daily_statistics(days: int = 7) -> list[dict]:
    """Get daily statistics for the past N days."""
    db = PostgresDB()
    db.connect_sync()

    try:
        return db.fetch_sync(
            """
            SELECT
                DATE(created_at) as date,
                COUNT(*) as jobs_created,
                COUNT(*) FILTER (WHERE status = 'completed') as jobs_completed,
                COUNT(*) FILTER (WHERE status = 'failed') as jobs_failed,
                COUNT(*) FILTER (WHERE status = 'processing') as jobs_processing
            FROM jobs
            WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '1 day' * $1
            GROUP BY DATE(created_at)
            ORDER BY date DESC
            """,
            days
        )
    finally:
        db.close_sync()


def get_status_counts() -> dict:
    """Get counts of jobs by status."""
    db = PostgresDB()
    db.connect_sync()

    try:
        row = db.fetchrow_sync(
            """
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'created') as created,
                COUNT(*) FILTER (WHERE status = 'processing') as processing,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled
            FROM jobs
            """
        )
        return row
    finally:
        db.close_sync()


def print_jobs_table(jobs: list[dict]):
    """Print jobs in a formatted table."""
    if not jobs:
        print("No jobs found.")
        return

    print(f"\n{'ID':<38} {'Status':<12} {'Creator':<12} {'Validator':<12} {'Reqs':<8} {'Created':<20}")
    print("-" * 120)
    for job in jobs:
        created = job['created_at'].strftime('%Y-%m-%d %H:%M:%S') if job['created_at'] else 'N/A'
        integrated = job.get('integrated_requirements', 0)
        pending = job.get('pending_requirements', 0)
        reqs_str = f"{integrated}i/{pending}p"

        print(f"{job['id']!s:<38} {job['status']:<12} {job['creator_status']:<12} "
              f"{job['validator_status']:<12} {reqs_str:<8} {created:<20}")


def print_statistics(stats: list[dict]):
    """Print daily statistics."""
    if not stats:
        print("No statistics available.")
        return

    print(f"\n{'Date':<12} {'Created':<10} {'Completed':<10} {'Failed':<10} {'Processing':<10}")
    print("-" * 60)
    for day in stats:
        date_str = day['date'].strftime('%Y-%m-%d') if hasattr(day['date'], 'strftime') else str(day['date'])
        print(f"{date_str:<12} {day['jobs_created']:<10} {day['jobs_completed']:<10} "
              f"{day['jobs_failed']:<10} {day.get('jobs_processing', 0):<10}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="List jobs in the Graph-RAG system")
    parser.add_argument("--status", help="Filter by status (e.g., processing, completed)")
    parser.add_argument("--limit", type=int, default=50, help="Maximum number of jobs to show")
    parser.add_argument("--offset", type=int, default=0, help="Number of jobs to skip")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--stats", action="store_true", help="Show daily statistics instead of job list")

    args = parser.parse_args()

    try:
        if args.stats:
            stats = get_daily_statistics()
            if args.json:
                print(json.dumps(stats, default=str, indent=2))
            else:
                print_statistics(stats)
        else:
            jobs = list_jobs(status=args.status, limit=args.limit, offset=args.offset)
            if args.json:
                print(json.dumps(jobs, default=str, indent=2))
            else:
                # Print status summary
                counts = get_status_counts()
                print("\nJob Status Summary:")
                print(f"  Total: {counts['total']}")
                print(f"  Created: {counts['created']}")
                print(f"  Processing: {counts['processing']}")
                print(f"  Completed: {counts['completed']}")
                print(f"  Failed: {counts['failed']}")
                print(f"  Cancelled: {counts['cancelled']}")

                print_jobs_table(jobs)

                if args.limit and len(jobs) == args.limit:
                    print(f"\n(Showing {args.limit} jobs, use --limit to show more)")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
