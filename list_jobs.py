#!/usr/bin/env python3
"""List jobs in the Graph-RAG orchestrator.

Usage:
    python list_jobs.py
    python list_jobs.py --status processing
    python list_jobs.py --limit 10 --json
"""

import asyncio
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from src.database.postgres_utils import create_postgres_connection
from src.orchestrator.job_manager import create_job_manager
from src.orchestrator.reporter import create_reporter


def format_duration(seconds: float) -> str:
    """Format duration in human-readable format."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds/60:.0f}m"
    elif seconds < 86400:
        return f"{seconds/3600:.1f}h"
    else:
        return f"{seconds/86400:.1f}d"


async def main():
    parser = argparse.ArgumentParser(
        description="List jobs in the Graph-RAG orchestrator",
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
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of jobs to show (default: 50)"
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Number of jobs to skip"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON"
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show daily statistics instead of job list"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days for statistics (default: 7)"
    )

    args = parser.parse_args()

    # Connect to database
    conn = create_postgres_connection()
    try:
        await conn.connect()

        job_manager = create_job_manager(conn)
        reporter = create_reporter(conn)

        # Daily statistics
        if args.stats:
            stats = await reporter.get_daily_statistics(args.days)

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
        jobs = await job_manager.list_jobs(
            status=args.status,
            limit=args.limit,
            offset=args.offset
        )

        if args.json:
            # Convert for JSON serialization
            for job in jobs:
                for key, value in job.items():
                    if isinstance(value, datetime):
                        job[key] = value.isoformat()
            print(json.dumps(jobs, indent=2, default=str))
            return

        if not jobs:
            print("No jobs found")
            return

        # Table header
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
            if isinstance(created, datetime):
                created_str = created.strftime('%Y-%m-%d %H:%M')
            else:
                created_str = str(created)[:16]

            # Color status (ANSI codes)
            status_colors = {
                'completed': '\033[92m',  # Green
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

    finally:
        await conn.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
