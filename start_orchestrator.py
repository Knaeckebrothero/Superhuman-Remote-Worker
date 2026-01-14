#!/usr/bin/env python3
"""Start a new job in the Graph-RAG orchestrator.

Usage:
    python start_orchestrator.py --prompt "Analyze GDPR requirements" --document-path ./data/gdpr.pdf
    python start_orchestrator.py --prompt "Review GoBD compliance" --document-path ./data/gobd.pdf --context '{"domain": "car_rental"}'
"""

import asyncio
import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from src.database.postgres_utils import create_postgres_connection
from src.orchestrator.job_manager import create_job_manager
from src.orchestrator.monitor import create_monitor


async def main():
    parser = argparse.ArgumentParser(
        description="Start a new job in the Graph-RAG orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --prompt "Analyze GDPR requirements" --document-path ./data/gdpr.pdf
  %(prog)s --prompt "Review GoBD compliance" --document-path ./data/gobd.pdf --context '{"domain": "car_rental", "region": "EU"}'
  %(prog)s --prompt "Extract requirements from contract" --document-path ./contract.docx --wait
        """
    )

    parser.add_argument(
        "--prompt",
        required=True,
        help="User prompt describing the requirement analysis task"
    )
    parser.add_argument(
        "--document-path",
        help="Path to source document (PDF, DOCX, TXT)"
    )
    parser.add_argument(
        "--context",
        help="Additional context as JSON string (e.g., '{\"domain\": \"car_rental\"}')"
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="Don't copy document to workspace (use original path)"
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for job completion and show progress"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )

    args = parser.parse_args()

    # Parse context if provided
    context = None
    if args.context:
        try:
            context = json.loads(args.context)
        except json.JSONDecodeError as e:
            print(f"Error parsing context JSON: {e}", file=sys.stderr)
            sys.exit(1)

    # Validate document path if provided
    if args.document_path and not Path(args.document_path).exists():
        print(f"Error: Document not found: {args.document_path}", file=sys.stderr)
        sys.exit(1)

    # Connect to database
    conn = create_postgres_connection()
    try:
        await conn.connect()

        # Create job manager
        job_manager = create_job_manager(conn)

        # Create job
        try:
            job_id = await job_manager.create_new_job(
                prompt=args.prompt,
                document_path=args.document_path,
                context=context,
                store_document=not args.no_store
            )
        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e)}))
            else:
                print(f"Error creating job: {e}", file=sys.stderr)
            sys.exit(1)

        if args.json:
            output = {
                "job_id": str(job_id),
                "status": "created",
                "prompt": args.prompt,
                "document_path": args.document_path
            }
            print(json.dumps(output, indent=2))
        else:
            print(f"Created job: {job_id}")
            print(f"Prompt: {args.prompt[:80]}{'...' if len(args.prompt) > 80 else ''}")
            if args.document_path:
                print(f"Document: {args.document_path}")

        # Wait for completion if requested
        if args.wait:
            monitor = create_monitor(conn)

            async def progress_callback(progress):
                if not args.json:
                    pct = progress.get('progress_percent', 0)
                    reqs = progress.get('requirements', {})
                    print(f"\rProgress: {pct:.1f}% | "
                          f"Integrated: {reqs.get('integrated', 0)} | "
                          f"Pending: {reqs.get('pending', 0)} | "
                          f"Rejected: {reqs.get('rejected', 0)}", end="")

            if not args.json:
                print("\nWaiting for completion...")

            final_status = await monitor.wait_for_completion(
                job_id,
                callback=progress_callback if not args.json else None
            )

            if not args.json:
                print(f"\nJob completed with status: {final_status.value}")
            else:
                final_progress = await monitor.get_job_progress(job_id)
                print(json.dumps({
                    "job_id": str(job_id),
                    "final_status": final_status.value,
                    "progress": final_progress
                }, indent=2))

    finally:
        await conn.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
