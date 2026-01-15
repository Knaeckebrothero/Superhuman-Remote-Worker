#!/usr/bin/env python3
"""Universal Agent Entry Point.

Run the Universal Agent in various modes:
- CLI mode: Process documents (creates job automatically)
- API server mode: Start FastAPI server for HTTP interface
- Polling mode: Continuously poll for jobs

Examples:
    # Process a single document
    python run_universal_agent.py --config creator \
        --document-path ./data/doc.pdf \
        --prompt "Extract GoBD requirements" \
        --verbose

    # Process all documents in a directory
    python run_universal_agent.py --config creator \
        --document-dir ./data/example_data/ \
        --prompt "Extract requirements based on the provided documents" \
        --stream --verbose

    # Combine single document with directory
    python run_universal_agent.py --config creator \
        --document-path ./data/main.pdf \
        --document-dir ./data/context/ \
        --prompt "Extract requirements" \
        --verbose

    # Run as API server (Creator agent on port 8001)
    python run_universal_agent.py --config creator --port 8001

    # Run as Validator on port 8002
    python run_universal_agent.py --config validator --port 8002

    # Process an existing job by ID
    python run_universal_agent.py --config creator --job-id <uuid>

    # Run polling loop (no API server)
    python run_universal_agent.py --config creator --polling-only

    # Resume an existing job
    python run_universal_agent.py --config creator --job-id abc123 --resume
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import uvicorn

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src import UniversalAgent, create_app
from src.core.workspace import get_workspace_base_path
from src.database.postgres_db import PostgresDB


def setup_logging(verbose: bool = False):
    """Configure logging based on verbosity level."""
    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Always suppress noisy third-party libraries - even in verbose mode
    # These produce massive log output that drowns out useful information
    noisy_libraries = [
        "httpx",
        "httpcore",
        "httpcore.http11",
        "openai",
        "openai._base_client",
        "pymongo",
        "pymongo.command",
        "pymongo.connection",
        "pymongo.serverSelection",
        "urllib3",
        "asyncio",
    ]
    for lib in noisy_libraries:
        logging.getLogger(lib).setLevel(logging.WARNING)

    # uvicorn can be INFO level
    logging.getLogger("uvicorn").setLevel(logging.INFO)


def setup_job_file_logging(job_id: str, verbose: bool = False) -> Path:
    """Set up file logging for a specific job.

    Creates a log file alongside the job's workspace directory (not inside it,
    so the agent cannot read its own logs).

    Args:
        job_id: The job identifier
        verbose: Whether to use DEBUG level

    Returns:
        Path to the log file
    """
    workspace_base = get_workspace_base_path()
    workspace_base.mkdir(parents=True, exist_ok=True)

    # Log file is alongside the job folder, not inside it
    log_file = workspace_base / f"job_{job_id}.log"

    # Create file handler
    level = logging.DEBUG if verbose else logging.INFO
    file_handler = logging.FileHandler(log_file, mode='a')
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # Add to root logger so all logs go to the file
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)

    logging.getLogger(__name__).info(f"Logging to file: {log_file}")

    return log_file


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the Universal Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Configuration
    parser.add_argument(
        "--config", "-c",
        default="creator",
        help=(
            "Agent config name or path. Looks in configs/{name}/ first, "
            "then src/agent/config/. (default: creator)"
        ),
    )

    # Server options
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=8001,
        help="Port for API server (default: 8001)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Don't start API server, only run polling or job",
    )

    # Job options
    parser.add_argument(
        "--job-id", "-j",
        help="Process a specific job ID",
    )
    parser.add_argument(
        "--document-path", "-d",
        help="Single document path for Creator agent",
    )
    parser.add_argument(
        "--document-dir",
        help="Directory containing documents (all files will be included)",
    )
    parser.add_argument(
        "--prompt",
        help="Processing prompt for Creator agent",
    )
    parser.add_argument(
        "--context",
        help="Additional context as JSON string (e.g., '{\"domain\": \"car_rental\"}')",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing job (requires --job-id)",
    )

    # Polling options
    parser.add_argument(
        "--polling-only",
        action="store_true",
        help="Run polling loop without API server",
    )

    # Output options
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream state updates (for --job-id)",
    )

    return parser.parse_args()


async def run_single_job(
    config_path: str,
    job_id: str = None,
    document_paths: list = None,
    prompt: str = None,
    context: dict = None,
    stream: bool = False,
    resume: bool = False,
    verbose: bool = False,
):
    """Run a single job and exit.

    If job_id is not provided, creates a new job from document_paths and prompt.
    Logs are written to the workspace directory as job_{job_id}.log.

    Args:
        config_path: Path to agent configuration
        job_id: Existing job ID to process
        document_paths: List of document paths to include
        prompt: Processing prompt
        context: Additional context dictionary
        stream: Enable streaming output
        resume: Resume existing job
        verbose: Enable verbose logging
    """
    logger = logging.getLogger(__name__)
    start_time = datetime.now()

    # For backwards compatibility with database (stores single path)
    primary_document = document_paths[0] if document_paths else None

    # Create job if needed
    if not job_id:
        if not prompt:
            logger.error("Either --job-id or --prompt is required")
            sys.exit(1)

        # Connect to database to create job using new PostgresDB class
        db = PostgresDB()
        await db.connect()
        logger.info("Connected to PostgreSQL")

        try:
            # Use PostgresDB.jobs.create() namespace method
            job_uuid = await db.jobs.create(
                prompt=prompt,
                document_path=primary_document,
                context=context,
            )
            job_id = str(job_uuid)
            logger.info(f"Created job: {job_id}")
        finally:
            # Close connection after job creation - agent will create its own
            await db.close()
            logger.info("Disconnected from PostgreSQL (job created)")

    # Set up file logging for this job
    log_file = setup_job_file_logging(job_id, verbose)

    # Build metadata for the job
    metadata = {}
    if document_paths:
        metadata["document_paths"] = document_paths
    if prompt:
        metadata["prompt"] = prompt
    if context:
        metadata.update(context)

    # Create and initialize agent (it will create its own DB connection)
    agent = UniversalAgent.from_config(config_path)
    await agent.initialize()

    try:
        if stream:
            logger.info(f"Processing job {job_id} with streaming...")
            final_state = None
            # process_job returns an async generator when stream=True
            # We need to await the coroutine first to get the generator
            streaming_gen = await agent.process_job(job_id, metadata, stream=True, resume=resume)
            async for state in streaming_gen:
                final_state = state
                # Print streaming updates
                if isinstance(state, dict):
                    iteration = state.get("iteration", "?")
                    has_error = state.get("error") is not None
                    logger.info(f"[Iteration {iteration}] error={has_error}")
            result = final_state or {}
        else:
            logger.info(f"Processing job {job_id}...")
            result = await agent.process_job(job_id, metadata, resume=resume)

        elapsed = (datetime.now() - start_time).total_seconds()

        # Print result summary
        print("\n" + "=" * 60)
        print("UNIVERSAL AGENT RESULT")
        print("=" * 60)
        print(f"Job ID:       {job_id}")
        print(f"Config:       {config_path}")
        print(f"Iterations:   {result.get('iteration', 0)}")
        print(f"Duration:     {elapsed:.1f}s")
        print(f"Log file:     {log_file}")

        if result.get("error"):
            print(f"Status:       FAILED")
            print(f"Error:        {result['error']}")
            print("=" * 60)
            sys.exit(1)
        else:
            print(f"Status:       COMPLETED")
            print(f"Should stop:  {result.get('should_stop', False)}")
            print("=" * 60)

    finally:
        await agent.shutdown()


async def run_polling_loop(config_path: str):
    """Run the agent's polling loop."""
    logger = logging.getLogger(__name__)

    agent = UniversalAgent.from_config(config_path)
    await agent.initialize()

    try:
        logger.info(f"Starting {agent.display_name} polling loop...")
        await agent.start_polling()
    finally:
        await agent.shutdown()


def run_server(config_path: str, host: str, port: int):
    """Run the FastAPI server."""
    logger = logging.getLogger(__name__)

    logger.info(f"Starting Universal Agent API server on {host}:{port}")
    logger.info(f"Using config: {config_path}")

    # Create and run app
    app = create_app(config_path)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
    )


def main():
    """Main entry point."""
    args = parse_args()
    setup_logging(args.verbose)

    logger = logging.getLogger(__name__)

    # Determine config path
    config_path = args.config

    # Validate document path if provided
    if args.document_path and not Path(args.document_path).exists():
        logger.error(f"Document not found: {args.document_path}")
        sys.exit(1)

    # Validate document directory if provided
    if args.document_dir and not Path(args.document_dir).is_dir():
        logger.error(f"Document directory not found: {args.document_dir}")
        sys.exit(1)

    # Collect document paths
    document_paths = []
    if args.document_path:
        document_paths.append(str(Path(args.document_path).resolve()))
    if args.document_dir:
        doc_dir = Path(args.document_dir)
        for file_path in sorted(doc_dir.iterdir()):
            if file_path.is_file():
                document_paths.append(str(file_path.resolve()))
        logger.info(f"Found {len(document_paths)} files in {args.document_dir}")

    # Parse context if provided
    context = None
    if args.context:
        try:
            context = json.loads(args.context)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in --context: {e}")
            sys.exit(1)

    # Single job mode (either by job_id or by document+prompt)
    if args.job_id or args.prompt:
        asyncio.run(run_single_job(
            config_path=config_path,
            job_id=args.job_id,
            document_paths=document_paths if document_paths else None,
            prompt=args.prompt,
            context=context,
            stream=args.stream,
            resume=args.resume,
            verbose=args.verbose,
        ))
        return

    # Polling-only mode
    if args.polling_only:
        asyncio.run(run_polling_loop(config_path))
        return

    # API server mode (default)
    if args.no_server:
        logger.error("Specify --job-id, --prompt, or --polling-only with --no-server")
        sys.exit(1)

    run_server(config_path, args.host, args.port)


if __name__ == "__main__":
    main()
