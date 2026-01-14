#!/usr/bin/env python3
"""List jobs in the Graph-RAG system.

Standalone CLI tool using direct psycopg2 connection.

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
from contextlib import contextmanager
from datetime import datetime

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


def list_jobs(status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """List jobs with optional filtering."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if status:
                cur.execute(
                    """
                    SELECT j.id, j.prompt, j.status, j.creator_status, j.validator_status,
                           j.created_at, j.updated_at,
                           COUNT(r.id) FILTER (WHERE r.status = 'integrated') as integrated_requirements,
                           COUNT(r.id) FILTER (WHERE r.status = 'pending') as pending_requirements
                    FROM jobs j
                    LEFT JOIN requirements r ON j.id = r.job_id
                    WHERE j.status = %s
                    GROUP BY j.id
                    ORDER BY j.created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (status, limit, offset)
                )
            else:
                cur.execute(
                    """
                    SELECT j.id, j.prompt, j.status, j.creator_status, j.validator_status,
                           j.created_at, j.updated_at,
                           COUNT(r.id) FILTER (WHERE r.status = 'integrated') as integrated_requirements,
                           COUNT(r.id) FILTER (WHERE r.status = 'pending') as pending_requirements
                    FROM jobs j
                    LEFT JOIN requirements r ON j.id = r.job_id
                    GROUP BY j.id
                    ORDER BY j.created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset)
                )
            return [dict(row) for row in cur.fetchall()]


def get_daily_statistics(days: int = 7) -> list[dict]:
    """Get daily job statistics."""
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


def main():
    parser = argparse.ArgumentParser(
        description="List jobs in the Graph-RAG system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           # List all jobs
  %(prog)s --status processing       # Filter by status
  %(prog)s --limit 10                # Limit results
  %(prog)s --json                    # JSON output
  %(prog)s --stats                   # Show daily statistics
        """
    )

    parser.add_argument(
        "--status",
        choices=["created", "processing", "completed", "failed", "cancelled"],
        help="Filter by job status"
    )
    parser.add_argument("--limit", type=int, default=50, help="Maximum jobs to show")
    parser.add_argument("--offset", type=int, default=0, help="Number of jobs to skip")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--stats", action="store_true", help="Show daily statistics")
    parser.add_argument("--days", type=int, default=7, help="Days for statistics")

    args = parser.parse_args()

    # Daily statistics
    if args.stats:
        stats = get_daily_statistics(args.days)
        if args.json:
            print(json.dumps(stats, indent=2, default=str))
        else:
            print(f"Daily Statistics (last {args.days} days)")
            print("=" * 60)
            print(f"{'Date':<12} {'Created':<10} {'Completed':<12} {'Failed':<10}")
            print("-" * 60)
            for day in stats:
                print(f"{str(day['date']):<12} "
                      f"{day['jobs_created']:<10} "
                      f"{day['jobs_completed']:<12} "
                      f"{day['jobs_failed']:<10}")
        return

    # List jobs
    jobs = list_jobs(status=args.status, limit=args.limit, offset=args.offset)

    if args.json:
        for job in jobs:
            for key, value in job.items():
                if isinstance(value, datetime):
                    job[key] = value.isoformat()
        print(json.dumps(jobs, indent=2, default=str))
        return

    if not jobs:
        print("No jobs found")
        return

    # Table output
    print(f"{'Job ID':<36} {'Status':<12} {'Creator':<10} {'Validator':<10} "
          f"{'Integrated':<10} {'Pending':<8} {'Created':<20}")
    print("=" * 120)

    for job in jobs:
        job_id = str(job['id'])
        status = job['status']
        creator = job.get('creator_status', '-')
        validator = job.get('validator_status', '-')
        integrated = job.get('integrated_requirements', 0)
        pending = job.get('pending_requirements', 0)

        created = job['created_at']
        created_str = created.strftime('%Y-%m-%d %H:%M') if isinstance(created, datetime) else str(created)[:16]

        # ANSI colors
        status_colors = {
            'completed': '\033[92m',   # Green
            'processing': '\033[93m',  # Yellow
            'failed': '\033[91m',      # Red
            'cancelled': '\033[90m',   # Gray
        }
        reset = '\033[0m'
        color = status_colors.get(status, '')
        status_display = f"{color}{status:<12}{reset}" if color else f"{status:<12}"

        print(f"{job_id:<36} {status_display} {creator:<10} {validator:<10} "
              f"{integrated:<10} {pending:<8} {created_str:<20}")

    print()
    print(f"Showing {len(jobs)} jobs (offset: {args.offset}, limit: {args.limit})")


if __name__ == "__main__":
    main()
