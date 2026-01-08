#!/usr/bin/env python3
"""Run the Validator Agent locally for development and debugging.

This script allows you to run the Validator Agent outside of Docker containers,
connecting to databases running in containers (via docker-compose.dbs.yml).

Usage:
    # Start databases first
    podman-compose -f docker-compose.dbs.yml up -d

    # Run validator agent (polls for pending requirements)
    python run_validator.py

    # With verbose output for debugging
    python run_validator.py --verbose

    # Process a single requirement and exit
    python run_validator.py --one-shot

    # Validate a specific requirement by ID
    python run_validator.py --requirement-id <uuid>

    # Use custom polling interval
    python run_validator.py --poll-interval 5

Environment Variables (from .env):
    DATABASE_URL - PostgreSQL connection string
    NEO4J_URI - Neo4j bolt connection string
    NEO4J_USERNAME - Neo4j username
    NEO4J_PASSWORD - Neo4j password
    OPENAI_API_KEY - LLM API key
    LLM_BASE_URL - Custom LLM endpoint (optional)
"""

import asyncio
import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from src.core.postgres_utils import (
    create_postgres_connection,
    get_pending_requirement,
    update_requirement_status,
)
from src.core.neo4j_utils import create_neo4j_connection
from src.core.config import get_validator_max_iterations
from src.agents.validator.validator_agent import ValidatorAgent


def setup_logging(verbose: bool) -> None:
    """Configure logging based on verbosity."""
    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("asyncpg").setLevel(logging.WARNING)
        logging.getLogger("neo4j").setLevel(logging.WARNING)


async def get_requirement_by_id(conn, requirement_id: str) -> dict | None:
    """Get a specific requirement from the cache."""
    row = await conn.fetchrow(
        "SELECT * FROM requirement_cache WHERE id = $1",
        uuid.UUID(requirement_id)
    )
    if row:
        return dict(row)
    return None


async def run_validator(
    requirement_id: str | None = None,
    one_shot: bool = False,
    poll_interval: int | None = None,
    max_iterations: int | None = None,
    verbose: bool = False,
    stream: bool = False,
) -> dict:
    """Run the Validator Agent.

    Args:
        requirement_id: Specific requirement ID to validate (optional)
        one_shot: Process one requirement and exit
        poll_interval: Seconds between polling for requirements
        max_iterations: Maximum LLM iterations per requirement
        verbose: Enable verbose logging
        stream: Stream state updates (for debugging)

    Returns:
        Summary dictionary
    """
    logger = logging.getLogger("run_validator")

    # Connect to PostgreSQL
    pg_conn = create_postgres_connection()
    await pg_conn.connect()
    logger.info("Connected to PostgreSQL")

    # Connect to Neo4j
    neo4j_conn = create_neo4j_connection()
    if not neo4j_conn.connect():
        raise RuntimeError("Failed to connect to Neo4j")
    logger.info("Connected to Neo4j")

    # Create agent
    agent = ValidatorAgent(
        neo4j_connection=neo4j_conn,
        postgres_connection_string=os.getenv("DATABASE_URL"),
        polling_interval=poll_interval,
    )

    # Stats
    processed = 0
    integrated = 0
    rejected = 0
    failed = 0
    start_time = datetime.now()

    try:
        if requirement_id:
            # Process specific requirement
            requirement = await get_requirement_by_id(pg_conn, requirement_id)
            if not requirement:
                raise ValueError(f"Requirement not found: {requirement_id}")

            logger.info(f"Processing requirement {requirement_id}")
            result = await agent.validate_requirement(
                requirement=requirement,
                job_id=str(requirement.get("job_id", "")),
                requirement_id=requirement_id,
            )

            processed = 1
            if result.get("status") == "completed":
                if result.get("graph_changes"):
                    integrated = 1
                else:
                    rejected = 1
            else:
                failed = 1

            # Update status
            if result.get("status") == "completed":
                graph_changes = result.get("graph_changes", [])
                node_id = None
                for change in graph_changes:
                    if change.get("type") == "create_node":
                        node_id = change.get("node_id")
                        break

                status = "integrated" if graph_changes else "rejected"
                await update_requirement_status(
                    pg_conn,
                    uuid.UUID(requirement_id),
                    status=status,
                    validation_result=result,
                    graph_node_id=node_id,
                    rejection_reason=result.get("rejection_reason") if not graph_changes else None,
                )
            else:
                await update_requirement_status(
                    pg_conn,
                    uuid.UUID(requirement_id),
                    status="failed",
                    error=str(result.get("error", "Unknown error")),
                )

            logger.info(f"Result: {result.get('status')}")

        else:
            # Polling mode
            logger.info(f"Starting polling loop (interval: {poll_interval}s)")
            if one_shot:
                logger.info("One-shot mode: will exit after processing one requirement")

            while True:
                try:
                    # Poll for pending requirement
                    requirement = await get_pending_requirement(pg_conn)

                    if requirement:
                        req_id = str(requirement["id"])
                        job_id = str(requirement["job_id"])
                        logger.info(f"Processing requirement {req_id}")

                        # Validate
                        result = await agent.validate_requirement(
                            requirement=requirement,
                            job_id=job_id,
                            requirement_id=req_id,
                        )

                        processed += 1

                        # Update status
                        if result.get("status") == "completed":
                            graph_changes = result.get("graph_changes", [])
                            node_id = None
                            for change in graph_changes:
                                if change.get("type") == "create_node":
                                    node_id = change.get("node_id")
                                    break

                            if graph_changes:
                                integrated += 1
                                await update_requirement_status(
                                    pg_conn,
                                    uuid.UUID(req_id),
                                    status="integrated",
                                    validation_result=result,
                                    graph_node_id=node_id,
                                )
                            else:
                                rejected += 1
                                await update_requirement_status(
                                    pg_conn,
                                    uuid.UUID(req_id),
                                    status="rejected",
                                    validation_result=result,
                                    rejection_reason=result.get("rejection_reason", "Validation rejected"),
                                )
                        else:
                            failed += 1
                            await update_requirement_status(
                                pg_conn,
                                uuid.UUID(req_id),
                                status="failed",
                                error=str(result.get("error", "Unknown error")),
                            )

                        logger.info(f"Processed: {processed} | Integrated: {integrated} | Rejected: {rejected} | Failed: {failed}")

                        if one_shot:
                            break
                    else:
                        if one_shot:
                            logger.info("No pending requirements found")
                            break

                        logger.debug(f"No pending requirements, waiting {poll_interval}s...")
                        await asyncio.sleep(poll_interval)

                except KeyboardInterrupt:
                    logger.info("Interrupted by user")
                    break

    finally:
        neo4j_conn.close()
        await pg_conn.disconnect()
        logger.info("Disconnected from databases")

    elapsed = (datetime.now() - start_time).total_seconds()

    return {
        "processed": processed,
        "integrated": integrated,
        "rejected": rejected,
        "failed": failed,
        "elapsed_seconds": elapsed,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run Validator Agent locally for development/debugging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start polling for requirements
  %(prog)s

  # Process one requirement and exit
  %(prog)s --one-shot

  # Validate a specific requirement
  %(prog)s --requirement-id 12345678-1234-1234-1234-123456789abc

  # With verbose output
  %(prog)s --verbose

  # Custom poll interval
  %(prog)s --poll-interval 5
        """
    )

    parser.add_argument(
        "--requirement-id",
        help="Specific requirement ID to validate"
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Process one requirement and exit"
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=10,
        help="Seconds between polling (default: 10)"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=f"Maximum LLM iterations per requirement (default: {get_validator_max_iterations()} from VALIDATOR_MAX_ITERATIONS)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose)

    # Run
    try:
        result = asyncio.run(run_validator(
            requirement_id=args.requirement_id,
            one_shot=args.one_shot,
            poll_interval=args.poll_interval,
            max_iterations=args.max_iterations,
            verbose=args.verbose,
        ))

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print("\n" + "=" * 60)
            print("VALIDATOR AGENT RESULT")
            print("=" * 60)
            print(f"Processed:  {result['processed']}")
            print(f"Integrated: {result['integrated']}")
            print(f"Rejected:   {result['rejected']}")
            print(f"Failed:     {result['failed']}")
            print(f"Duration:   {result['elapsed_seconds']:.1f}s")
            print("=" * 60)

        sys.exit(0)

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        logging.getLogger("run_validator").exception(f"Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
