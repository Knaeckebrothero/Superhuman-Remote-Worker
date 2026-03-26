"""Universal Agent Entry Point.

Run the Universal Agent in various modes:
- CLI mode: Process documents (creates job automatically)
- API server mode: Start FastAPI server for HTTP interface (receives jobs from orchestrator)

Logging is controlled via environment variables:
- LOG_LEVEL: DEBUG, INFO (default), WARNING, ERROR
- DEBUG_ALL: Set to "1" to include third-party library debug output
- DEBUG_LLM_STREAM: Set to "1" for LLM token output to stderr
- DEBUG_LLM_TAIL: Characters to show in LLM debug output (default: 500)

Examples:
    # Process a single document
    python agent.py --config developer \
        --document-path ./data/doc.pdf \
        --description "Analyze and summarize this document"

    # Process all documents in a directory (with debug logging)
    LOG_LEVEL=DEBUG python agent.py --config scholar \
        --document-dir ./data/example_data/ \
        --description "Research based on the provided documents"

    # Run as API server on port 8001
    python agent.py --config developer --port 8001

    # Process an existing job by ID
    python agent.py --config developer --job-id <uuid>

    # Resume an existing job
    python agent.py --config developer --job-id abc123 --resume

    # Resume a frozen job with feedback
    python agent.py --config critic --job-id abc123 --resume --feedback "Please also check X"

    # Approve a frozen job (marks as completed)
    python agent.py --config critic --job-id abc123 --approve

    # List available phase snapshots for recovery
    python agent.py --job-id abc123 --list-phases

    # Recover to a specific phase and resume
    python agent.py --job-id abc123 --recover-phase 2 --resume
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src import UniversalAgent, create_app
from src.core.workspace import get_workspace_base_path, get_logs_path
from src.core.phase_snapshot import PhaseSnapshotManager, format_snapshots_table
from src.database.postgres_db import PostgresDB


def setup_logging():
    """Configure logging from LOG_LEVEL environment variable.

    Reads LOG_LEVEL env var (default: INFO). Valid values: DEBUG, INFO, WARNING, ERROR.

    When LOG_LEVEL=DEBUG, only app loggers (src.*, orchestrator.*) get DEBUG;
    the root logger stays at INFO to suppress noisy third-party libraries.
    Set DEBUG_ALL=1 to also include third-party debug output.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    if level <= logging.DEBUG and not os.getenv("DEBUG_ALL"):
        # Root stays at INFO — silences all third-party DEBUG noise automatically
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # Only app namespaces get DEBUG
        for namespace in ("src", "orchestrator"):
            logging.getLogger(namespace).setLevel(logging.DEBUG)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


class FlushingFileHandler(logging.FileHandler):
    """File handler that flushes after every emit for crash safety."""

    def emit(self, record):
        super().emit(record)
        self.flush()


def setup_job_file_logging(job_id: str) -> Path:
    """Set up file logging for a specific job.

    Creates a log file in the logs directory (not inside the job workspace,
    so the agent cannot read its own logs). Uses FlushingFileHandler to ensure
    logs are written immediately for crash safety.

    Log level is read from LOG_LEVEL environment variable (default: INFO).

    Args:
        job_id: The job identifier

    Returns:
        Path to the log file
    """
    logs_dir = get_logs_path()  # Creates workspace/logs/ if needed

    # Log file goes in the logs directory
    log_file = logs_dir / f"job_{job_id}.log"

    # Get level from env var (same as setup_logging)
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    # Create flushing file handler for crash safety
    file_handler = FlushingFileHandler(log_file, mode='a')
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
        default="defaults",
        help=(
            "Agent config name or path. Looks in config/{name}/config.yaml first, "
            "then config/{name}.yaml. (default: defaults)"
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
        help="Don't start API server, only run job",
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
        "--description",
        help="Job description - what the agent should accomplish",
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
    parser.add_argument(
        "--feedback",
        help="Feedback message to inject when resuming a frozen job",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Approve a frozen job (marks as completed, requires --job-id)",
    )

    # Coding agent options
    parser.add_argument(
        "--git-url",
        help="Git repository URL to clone into workspace/repo/ (for coding agents)",
    )
    parser.add_argument(
        "--git-branch",
        default="main",
        help="Git branch to checkout after cloning (default: main)",
    )

    # Phase recovery options
    parser.add_argument(
        "--list-phases",
        action="store_true",
        help="List available phase snapshots for recovery (requires --job-id)",
    )
    parser.add_argument(
        "--recover-phase",
        type=int,
        metavar="N",
        help="Recover to phase N before resuming (requires --job-id and --resume)",
    )

    return parser.parse_args()


async def run_single_job(
    config_path: str,
    job_id: str = None,
    document_paths: Optional[List[str]] = None,
    description: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    resume: bool = False,
    feedback: Optional[str] = None,
):
    """Run a single job and exit.

    If job_id is not provided, creates a new job from document_paths and description.
    Logs are written to the workspace directory as job_{job_id}.log.
    Always uses streaming mode with iteration logging.

    Args:
        config_path: Path to agent configuration
        job_id: Existing job ID to process
        document_paths: List of document paths to include
        description: Job description - what the agent should accomplish
        context: Additional context dictionary
        resume: Resume existing job
        feedback: Feedback message to inject when resuming a frozen job
    """
    logger = logging.getLogger(__name__)
    start_time = datetime.now()

    # For backwards compatibility with database (stores single path)
    primary_document = document_paths[0] if document_paths else None

    # Create job if needed
    if not job_id:
        if not description:
            logger.error("Either --job-id or --description is required")
            sys.exit(1)

        # Connect to database to create job using new PostgresDB class
        db = PostgresDB()
        await db.connect()
        logger.info("Connected to PostgreSQL")

        try:
            # Use PostgresDB.jobs.create() namespace method
            job_uuid = await db.jobs.create(
                description=description,
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
    log_file = setup_job_file_logging(job_id)

    # Build metadata for the job
    metadata = {}
    if document_paths:
        metadata["document_paths"] = document_paths
    if description:
        metadata["description"] = description
    if context:
        metadata.update(context)
    # Query job status before resume (so process_job knows if this was a graceful stop)
    previous_status = None
    if resume and job_id:
        try:
            import uuid as _uuid
            db = PostgresDB()
            await db.connect()
            job_record = await db.jobs.get(_uuid.UUID(job_id))
            if job_record:
                previous_status = job_record.get("status")
                logger.info(f"Job {job_id} previous status: {previous_status}")
            await db.close()
        except Exception as e:
            logger.warning(f"Could not query job status for resume (will use snapshot recovery): {e}")

    # Create and initialize agent (it will create its own DB connection)
    agent = UniversalAgent.from_config(config_path)
    await agent.initialize()

    try:
        logger.info(f"Processing job {job_id} with streaming...")
        final_state = None
        # process_job returns an async generator when stream=True
        # We need to await the coroutine first to get the generator
        streaming_gen = await agent.process_job(
            job_id, metadata, stream=True, resume=resume, feedback=feedback,
            previous_status=previous_status,
        )
        async for state in streaming_gen:
            final_state = state
            # Print streaming updates
            if isinstance(state, dict):
                iteration = state.get("iteration", "?")
                has_error = state.get("error") is not None
                logger.info(f"[Iteration {iteration}] error={has_error}")
        result = final_state or {}

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


async def approve_frozen_job(config_path: str, job_id: str):
    """Approve a frozen job, marking it as truly completed.

    Args:
        config_path: Path to agent configuration
        job_id: The job ID to approve
    """
    logger = logging.getLogger(__name__)

    # Create and initialize agent
    agent = UniversalAgent.from_config(config_path)
    await agent.initialize()

    try:
        result = await agent.approve_frozen_job(job_id)

        print("\n" + "=" * 60)
        print("JOB APPROVED")
        print("=" * 60)
        print(f"Job ID:       {result['job_id']}")
        print(f"Status:       {result['status']}")
        print("=" * 60)

    except ValueError as e:
        logger.error(str(e))
        print(f"\nError: {e}")
        sys.exit(1)
    finally:
        await agent.shutdown()


def list_phase_snapshots(job_id: str):
    """List available phase snapshots for a job.

    Args:
        job_id: Job identifier
    """
    snapshot_mgr = PhaseSnapshotManager(job_id)
    snapshots = snapshot_mgr.list_snapshots()

    print(format_snapshots_table(snapshots))

    if snapshots:
        print(f"To recover: python agent.py --config <cfg> --job-id {job_id} --recover-phase N --resume")
        print("")


async def recover_and_resume(
    config_path: str,
    job_id: str,
    phase_number: int,
    document_paths: Optional[List[str]] = None,
    description: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
):
    """Recover to a specific phase and resume execution.

    Args:
        config_path: Path to agent configuration
        job_id: Job identifier
        phase_number: Phase number to recover to
        document_paths: List of document paths (for metadata)
        description: Job description (for metadata)
        context: Additional context dictionary
    """
    logger = logging.getLogger(__name__)

    # Get snapshot info first
    snapshot_mgr = PhaseSnapshotManager(job_id)
    snapshot = snapshot_mgr.get_snapshot(phase_number)

    if not snapshot:
        print(f"\nError: No snapshot found for phase {phase_number}")
        print("Use --list-phases to see available snapshots.")
        sys.exit(1)

    # Show recovery info
    print("\n" + "=" * 60)
    print(f"RECOVERING TO PHASE {phase_number}")
    print("=" * 60)
    print(f"Type:         {'strategic' if snapshot.is_strategic_phase else 'tactical'}")
    print(f"Iteration:    {snapshot.iteration}")
    print(f"Messages:     {snapshot.message_count}")
    print(f"Timestamp:    {snapshot.timestamp[:19].replace('T', ' ')}")
    print("=" * 60)

    # Perform recovery
    if not snapshot_mgr.recover_to_phase(phase_number):
        print("\nError: Recovery failed")
        sys.exit(1)

    # Delete stale snapshots after the recovery point
    deleted = snapshot_mgr.delete_snapshots_after(phase_number)
    if deleted > 0:
        print(f"Deleted {deleted} stale snapshot(s) from phases after {phase_number}")

    print("Recovery complete. Resuming execution...\n")

    # Resume execution
    await run_single_job(
        config_path=config_path,
        job_id=job_id,
        document_paths=document_paths,
        description=description,
        context=context,
        resume=True,  # Always resume after recovery
        feedback=None,
    )


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
    setup_logging()

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

    # Add git repo info to context for coding agents
    if args.git_url:
        if context is None:
            context = {}
        context["git_url"] = args.git_url
        context["git_branch"] = args.git_branch

    # Validate git-url if provided
    if args.git_url:
        if not args.description:
            logger.error("--git-url requires --description")
            sys.exit(1)

    # Approve frozen job mode
    if args.approve:
        if not args.job_id:
            logger.error("--approve requires --job-id")
            sys.exit(1)
        asyncio.run(approve_frozen_job(config_path, args.job_id))
        return

    # List phase snapshots mode
    if args.list_phases:
        if not args.job_id:
            logger.error("--list-phases requires --job-id")
            sys.exit(1)
        list_phase_snapshots(args.job_id)
        return

    # Recover to phase mode
    if args.recover_phase is not None:
        if not args.job_id:
            logger.error("--recover-phase requires --job-id")
            sys.exit(1)
        if not args.resume:
            logger.error("--recover-phase requires --resume")
            sys.exit(1)
        asyncio.run(recover_and_resume(
            config_path=config_path,
            job_id=args.job_id,
            phase_number=args.recover_phase,
            document_paths=document_paths if document_paths else None,
            description=args.description,
            context=context,
        ))
        return

    # Single job mode
    if args.job_id or args.description:
        asyncio.run(run_single_job(
            config_path=config_path,
            job_id=args.job_id,
            document_paths=document_paths if document_paths else None,
            description=args.description,
            context=context,
            resume=args.resume,
            feedback=args.feedback,
        ))
        return

    # API server mode (default)
    if args.no_server:
        logger.error("Specify --job-id or --description with --no-server")
        sys.exit(1)

    run_server(config_path, args.host, args.port)


if __name__ == "__main__":
    main()
