#!/usr/bin/env python3
"""Check status of a job in the Graph-RAG orchestrator.

Usage:
    python job_status.py --job-id <uuid>
    python job_status.py --job-id <uuid> --report
    python job_status.py --job-id <uuid> --json
"""

import asyncio
import argparse
import json
import sys
import uuid as uuid_module
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from src.database.postgres_utils import create_postgres_connection
from src.orchestrator.job_manager import create_job_manager
from src.orchestrator.monitor import create_monitor
from src.orchestrator.reporter import create_reporter


async def main():
    parser = argparse.ArgumentParser(
        description="Check status of a job in the Graph-RAG orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000 --report
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000 --json
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000 --progress
        """
    )

    parser.add_argument(
        "--job-id",
        required=True,
        help="Job UUID"
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate full text report"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON"
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Show detailed progress information"
    )
    parser.add_argument(
        "--rejected",
        action="store_true",
        help="Show rejected requirements"
    )
    parser.add_argument(
        "--failed",
        action="store_true",
        help="Show failed requirements"
    )

    args = parser.parse_args()

    # Parse job ID
    try:
        job_id = uuid_module.UUID(args.job_id)
    except ValueError:
        print(f"Error: Invalid job ID: {args.job_id}", file=sys.stderr)
        sys.exit(1)

    # Connect to database
    conn = create_postgres_connection()
    try:
        await conn.connect()

        job_manager = create_job_manager(conn)
        monitor = create_monitor(conn)
        reporter = create_reporter(conn)

        # Get job status
        status = await job_manager.get_job_status(job_id)
        if not status:
            if args.json:
                print(json.dumps({"error": "Job not found"}))
            else:
                print(f"Error: Job {job_id} not found", file=sys.stderr)
            sys.exit(1)

        # Full report
        if args.report:
            report = await reporter.generate_text_report(job_id)
            print(report)
            return

        # JSON output
        if args.json:
            if args.progress:
                progress = await monitor.get_job_progress(job_id)
                print(json.dumps(progress, indent=2, default=str))
            elif args.rejected:
                rejected = await reporter.get_rejected_requirements(job_id)
                print(json.dumps(rejected, indent=2, default=str))
            elif args.failed:
                failed = await reporter.get_failed_requirements(job_id)
                print(json.dumps(failed, indent=2, default=str))
            else:
                json_report = await reporter.generate_json_report(job_id)
                print(json.dumps(json_report, indent=2, default=str))
            return

        # Progress view
        if args.progress:
            progress = await monitor.get_job_progress(job_id)
            print(f"Job: {progress['job_id']}")
            print(f"Status: {progress['status']}")
            print(f"Creator: {progress['creator_status']}")
            print(f"Validator: {progress['validator_status']}")
            print(f"Progress: {progress['progress_percent']:.1f}%")
            print()
            print("Requirements:")
            reqs = progress['requirements']
            for key, value in reqs.items():
                print(f"  {key}: {value}")
            print()
            print(f"Elapsed: {progress['elapsed_seconds']/3600:.1f} hours")
            if progress.get('eta_seconds'):
                print(f"ETA: {progress['eta_seconds']/3600:.1f} hours")
            return

        # Rejected requirements
        if args.rejected:
            rejected = await reporter.get_rejected_requirements(job_id)
            print(f"Rejected Requirements ({len(rejected)}):")
            print("-" * 60)
            for req in rejected:
                print(f"ID: {req['id']}")
                print(f"Name: {req.get('name', 'N/A')}")
                print(f"Reason: {req.get('rejection_reason', 'Unknown')}")
                print(f"Text: {req['text'][:100]}...")
                print("-" * 60)
            return

        # Failed requirements
        if args.failed:
            failed = await reporter.get_failed_requirements(job_id)
            print(f"Failed Requirements ({len(failed)}):")
            print("-" * 60)
            for req in failed:
                print(f"ID: {req['id']}")
                print(f"Name: {req.get('name', 'N/A')}")
                print(f"Error: {req.get('last_error', 'Unknown')}")
                print(f"Retries: {req.get('retry_count', 0)}")
                print(f"Text: {req['text'][:100]}...")
                print("-" * 60)
            return

        # Default: brief status
        print(f"Job ID:           {job_id}")
        print(f"Status:           {status['status']}")
        print(f"Creator Status:   {status['creator_status']}")
        print(f"Validator Status: {status['validator_status']}")
        print(f"Created:          {status['created_at']}")

        req_counts = status.get('requirement_counts', {})
        if req_counts:
            print()
            print("Requirements:")
            print(f"  Total:      {status.get('total_requirements', 0)}")
            print(f"  Integrated: {req_counts.get('integrated', 0)}")
            print(f"  Pending:    {req_counts.get('pending', 0)}")
            print(f"  Rejected:   {req_counts.get('rejected', 0)}")
            print(f"  Failed:     {req_counts.get('failed', 0)}")
            print(f"  Progress:   {status.get('progress_percent', 0):.1f}%")

        if status.get('error_message'):
            print()
            print(f"Error: {status['error_message']}")

    finally:
        await conn.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
