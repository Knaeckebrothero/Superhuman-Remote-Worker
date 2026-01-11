#!/usr/bin/env python3
"""Cancel a job in the Graph-RAG orchestrator.

Usage:
    python cancel_job.py --job-id <uuid>
    python cancel_job.py --job-id <uuid> --cleanup
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


async def main():
    parser = argparse.ArgumentParser(
        description="Cancel a job in the Graph-RAG orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000 --cleanup
  %(prog)s --job-id 123e4567-e89b-12d3-a456-426614174000 --force
        """
    )

    parser.add_argument(
        "--job-id",
        required=True,
        help="Job UUID"
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Also clean up workspace files"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force cancellation without confirmation"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )

    args = parser.parse_args()

    # Parse job ID
    try:
        job_id = uuid_module.UUID(args.job_id)
    except ValueError:
        if args.json:
            print(json.dumps({"error": f"Invalid job ID: {args.job_id}"}))
        else:
            print(f"Error: Invalid job ID: {args.job_id}", file=sys.stderr)
        sys.exit(1)

    # Connect to database
    conn = create_postgres_connection()
    try:
        await conn.connect()

        job_manager = create_job_manager(conn)

        # Get current status first
        status = await job_manager.get_job_status(job_id)
        if not status:
            if args.json:
                print(json.dumps({"error": "Job not found"}))
            else:
                print(f"Error: Job {job_id} not found", file=sys.stderr)
            sys.exit(1)

        # Check if already completed/cancelled
        if status['status'] in ('completed', 'cancelled'):
            if args.json:
                print(json.dumps({
                    "error": f"Job already {status['status']}",
                    "job_id": str(job_id),
                    "status": status['status']
                }))
            else:
                print(f"Error: Job {job_id} is already {status['status']}", file=sys.stderr)
            sys.exit(1)

        # Confirm unless forced
        if not args.force and not args.json:
            print(f"Job ID: {job_id}")
            print(f"Status: {status['status']}")
            print(f"Prompt: {status['prompt'][:80]}...")
            print()
            print("Requirements:")
            req_counts = status.get('requirement_counts', {})
            print(f"  Integrated: {req_counts.get('integrated', 0)}")
            print(f"  Pending:    {req_counts.get('pending', 0)}")
            print()
            response = input("Are you sure you want to cancel this job? [y/N] ")
            if response.lower() != 'y':
                print("Cancellation aborted")
                sys.exit(0)

        # Cancel the job
        success = await job_manager.cancel_job(job_id)

        if not success:
            if args.json:
                print(json.dumps({
                    "error": "Failed to cancel job",
                    "job_id": str(job_id)
                }))
            else:
                print(f"Error: Failed to cancel job {job_id}", file=sys.stderr)
            sys.exit(1)

        # Cleanup workspace if requested
        if args.cleanup:
            await job_manager.cleanup_workspace(job_id)

        if args.json:
            print(json.dumps({
                "success": True,
                "job_id": str(job_id),
                "status": "cancelled",
                "cleanup": args.cleanup
            }))
        else:
            print(f"Job {job_id} cancelled successfully")
            if args.cleanup:
                print("Workspace files cleaned up")

    finally:
        await conn.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
