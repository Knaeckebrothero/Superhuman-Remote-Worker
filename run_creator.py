#!/usr/bin/env python3
"""Run the Creator Agent locally for development and debugging.

This script allows you to run the Creator Agent outside of Docker containers,
connecting to databases running in containers (via docker-compose.dbs.yml).

Usage:
    # Start databases first
    podman-compose -f docker-compose.dbs.yml up -d

    # Run creator agent on a document
    python run_creator.py --document-path ./data/document.pdf --prompt "Extract requirements"

    # With verbose output for debugging
    python run_creator.py --document-path ./data/document.pdf --prompt "..." --verbose

    # Process an existing job by ID
    python run_creator.py --job-id <uuid>

    # Set max iterations (default: 50)
    python run_creator.py --document-path ./data/doc.pdf --prompt "..." --max-iterations 100

Environment Variables (from .env):
    DATABASE_URL - PostgreSQL connection string
    OPENAI_API_KEY - LLM API key
    LLM_BASE_URL - Custom LLM endpoint (optional)
    TAVILY_API_KEY - Web search API key (optional)
"""

import asyncio
import argparse
import json
import logging
import sys
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from src.core.postgres_utils import create_postgres_connection, create_job
from src.core.config import get_creator_max_iterations
from src.agents.creator.creator_agent import CreatorAgent


def setup_logging(verbose: bool) -> None:
    """Configure logging based on verbosity."""
    level = logging.DEBUG if verbose else logging.INFO

    # Configure root logger
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Reduce noise from some libraries unless in verbose mode
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("asyncpg").setLevel(logging.WARNING)


async def create_new_job(conn, prompt: str, document_path: str | None, context: dict | None) -> str:
    """Create a new job in the database."""
    job_id = await create_job(
        conn=conn,
        prompt=prompt,
        document_path=document_path,
        context=context,
    )
    return str(job_id)


async def run_creator(
    job_id: str | None = None,
    document_path: str | None = None,
    prompt: str | None = None,
    context: dict | None = None,
    max_iterations: int | None = None,
    verbose: bool = False,
    stream: bool = False,
) -> dict:
    """Run the Creator Agent.

    Args:
        job_id: Existing job ID to process (optional)
        document_path: Path to document file
        prompt: Processing prompt
        context: Additional context dict
        max_iterations: Maximum LLM iterations
        verbose: Enable verbose logging
        stream: Stream state updates (for debugging)

    Returns:
        Final state dictionary
    """
    logger = logging.getLogger("run_creator")

    # Connect to PostgreSQL
    conn = create_postgres_connection()
    await conn.connect()
    logger.info("Connected to PostgreSQL")

    try:
        # Create job if not provided
        if not job_id:
            if not prompt:
                raise ValueError("Either --job-id or --prompt is required")

            job_id = await create_new_job(conn, prompt, document_path, context)
            logger.info(f"Created job: {job_id}")

        # Create and run agent
        agent = CreatorAgent(postgres_conn=conn)
        logger.info(f"Processing job {job_id}")

        start_time = datetime.now()

        if stream:
            # Stream mode - show state updates
            final_state = None
            async for state_update in agent.process_job_stream(job_id, max_iterations):
                final_state = state_update
                # Print progress
                node_name = list(state_update.keys())[0] if state_update else "unknown"
                if verbose:
                    iteration = state_update.get(node_name, {}).get("iteration", "?")
                    phase = state_update.get(node_name, {}).get("current_phase", "?")
                    logger.debug(f"Node: {node_name} | Phase: {phase} | Iteration: {iteration}")
        else:
            # Normal mode
            final_state = await agent.process_job(job_id, max_iterations)

        elapsed = (datetime.now() - start_time).total_seconds()

        # Extract results
        requirements_created = final_state.get("requirements_created", [])
        iterations = final_state.get("iteration", 0)
        error = final_state.get("error")

        logger.info(f"Completed in {elapsed:.1f}s")
        logger.info(f"Iterations: {iterations}")
        logger.info(f"Requirements created: {len(requirements_created)}")

        if error:
            logger.error(f"Error: {error}")

        return {
            "job_id": job_id,
            "status": "failed" if error else "completed",
            "requirements_created": requirements_created,
            "iterations": iterations,
            "elapsed_seconds": elapsed,
            "error": error,
        }

    finally:
        await conn.disconnect()
        logger.info("Disconnected from PostgreSQL")


def main():
    parser = argparse.ArgumentParser(
        description="Run Creator Agent locally for development/debugging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process a document
  %(prog)s --document-path ./data/spec.pdf --prompt "Extract GoBD requirements"

  # With context and verbose output
  %(prog)s --document-path ./doc.pdf --prompt "..." --context '{"domain": "car_rental"}' --verbose

  # Process existing job
  %(prog)s --job-id 12345678-1234-1234-1234-123456789abc

  # Stream mode for debugging state transitions
  %(prog)s --document-path ./doc.pdf --prompt "..." --stream --verbose
        """
    )

    parser.add_argument(
        "--job-id",
        help="Existing job ID to process (skips job creation)"
    )
    parser.add_argument(
        "--document-path",
        help="Path to document file (PDF, DOCX, TXT, HTML)"
    )
    parser.add_argument(
        "--prompt",
        help="Processing prompt for requirement extraction"
    )
    parser.add_argument(
        "--context",
        help="Additional context as JSON string"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=f"Maximum LLM iterations (default: {get_creator_max_iterations()} from CREATOR_MAX_ITERATIONS)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging"
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream state updates (useful for debugging)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )

    args = parser.parse_args()

    # Validate arguments
    if not args.job_id and not args.prompt:
        parser.error("Either --job-id or --prompt is required")

    if args.document_path and not Path(args.document_path).exists():
        parser.error(f"Document not found: {args.document_path}")

    # Parse context
    context = None
    if args.context:
        try:
            context = json.loads(args.context)
        except json.JSONDecodeError as e:
            parser.error(f"Invalid JSON in --context: {e}")

    # Setup logging
    setup_logging(args.verbose)

    # Run
    try:
        result = asyncio.run(run_creator(
            job_id=args.job_id,
            document_path=args.document_path,
            prompt=args.prompt,
            context=context,
            max_iterations=args.max_iterations,
            verbose=args.verbose,
            stream=args.stream,
        ))

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print("\n" + "=" * 60)
            print("CREATOR AGENT RESULT")
            print("=" * 60)
            print(f"Job ID:       {result['job_id']}")
            print(f"Status:       {result['status']}")
            print(f"Iterations:   {result['iterations']}")
            print(f"Requirements: {len(result['requirements_created'])}")
            print(f"Duration:     {result['elapsed_seconds']:.1f}s")
            if result['error']:
                print(f"Error:        {result['error']}")
            print("=" * 60)

        sys.exit(0 if result['status'] == 'completed' else 1)

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        logging.getLogger("run_creator").exception(f"Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
