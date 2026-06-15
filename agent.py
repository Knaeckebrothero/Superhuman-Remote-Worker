"""Universal Agent Entry Point.

Starts the FastAPI server that receives jobs and interactive sessions from
the orchestrator. The agent never runs jobs from its own CLI — job lifecycle
is driven by the orchestrator over HTTP.

Logging is controlled via environment variables:
- LOG_LEVEL: DEBUG, INFO (default), WARNING, ERROR
- DEBUG_ALL: Set to "1" to include third-party library debug output
- DEBUG_LLM_STREAM: Set to "1" for LLM token output to stderr
- DEBUG_LLM_TAIL: Characters to show in LLM debug output (default: 500)

Examples:
    # Run as dual-mode server on port 8001 (default)
    python agent.py --port 8001

    # Worker-only server (no persistent sessions)
    python agent.py --mode worker --port 8001

    # Persistent-only server with a specific thread
    python agent.py --mode persistent --port 8002 --thread-id <uuid>

    # Loop mode: after a job/session completes, return to IDLE instead of
    # exiting. Needed for Docker Compose / bare-metal dev where the process
    # is not respawned automatically.
    python agent.py --port 8001 --loop
"""

import argparse
import logging
import os
import sys

import uvicorn

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from src import create_app  # noqa: E402
from src.core.logging_config import (  # noqa: E402
    configure_logging,
    set_log_context,
)


def setup_logging():
    """Configure logging from LOG_LEVEL / LOG_FORMAT environment variables.

    LOG_FORMAT=json emits structured JSON (cluster); default text (local/dev).
    When LOG_LEVEL=DEBUG, only app loggers (src.*, orchestrator.*) get DEBUG;
    the root stays at INFO to suppress noisy third-party libraries. Set
    DEBUG_ALL=1 to also include third-party debug output.
    See docs/features/centralized_logging.md.
    """
    configure_logging(component="agent", app_namespaces=("src", "orchestrator"))


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the Universal Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Configuration
    parser.add_argument(
        "--config",
        "-c",
        default="defaults",
        help=(
            "Agent config name or path. Looks in config/{name}/config.yaml first, "
            "then config/{name}.yaml. (default: defaults)"
        ),
    )

    # Server options
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=8001,
        help="Port for API server (default: 8001)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )

    # Agent mode
    parser.add_argument(
        "--mode",
        choices=["worker", "persistent", "dual"],
        default="dual",
        help="Agent mode: 'dual' (accepts jobs or sessions, default), 'worker' (jobs only), or 'persistent' (sessions only)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="After completing a job or session, return to IDLE instead of exiting. Required for bare-metal/Docker Compose dev where the process is not respawned.",
    )
    parser.add_argument(
        "--thread-id",
        help="Thread UUID for persistent mode (auto-generated if omitted)",
    )

    return parser.parse_args()


def run_server(config_path: str, host: str, port: int):
    """Run the FastAPI server (worker mode)."""
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
        # Defer to our root JSON handler so uvicorn's own access/error logs are
        # JSON too, not plain text (see docs/features/centralized_logging.md).
        log_config=None,
    )


def run_persistent_server(
    config_path: str, host: str, port: int, thread_id: str | None
):
    """Run the persistent-mode FastAPI server.

    Starts an interactive agent with WebSocket transport.
    Connect via: ws://{host}:{port}/ws/chat
    """
    logger = logging.getLogger(__name__)

    logger.info(f"Starting Persistent Agent on {host}:{port}")
    logger.info(f"Config: {config_path}, Thread: {thread_id or '(auto-create)'}")

    from src.api.persistent_app import create_persistent_app

    app = create_persistent_app(config_path, thread_id)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        # Defer to our root JSON handler so uvicorn's own access/error logs are
        # JSON too, not plain text (see docs/features/centralized_logging.md).
        log_config=None,
    )


def run_dual_server(config_path: str, host: str, port: int):
    """Run the dual-mode FastAPI server.

    Accepts both job dispatch (/job/start) and session attachment
    (/session/attach). Each pod handles one task then exits unless
    --loop is set.
    """
    logger = logging.getLogger(__name__)

    logger.info(f"Starting Dual-Mode Agent on {host}:{port}")
    logger.info(f"Config: {config_path}")

    from src.api.dual_app import create_dual_app

    app = create_dual_app(config_path)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        # Defer to our root JSON handler so uvicorn's own access/error logs are
        # JSON too, not plain text (see docs/features/centralized_logging.md).
        log_config=None,
    )


def main():
    """Main entry point."""
    args = parse_args()
    setup_logging()
    # Tag every log line from this agent process with its identity (correlation).
    # Pods don't set AGENT_ID; the pod name (POD_NAME / HOSTNAME) is the id the
    # orchestrator uses in its own logs. SESSION_BOUND_THREAD_ID is present for
    # dedicated session agents (pool/dual agents bind thread_id per-session at
    # loop start instead).
    set_log_context(
        agent_id=os.getenv("AGENT_ID")
        or os.getenv("POD_NAME")
        or os.getenv("HOSTNAME"),
        thread_id=os.getenv("SESSION_BOUND_THREAD_ID"),
    )

    logger = logging.getLogger(__name__)

    # Loop mode: return to IDLE after task instead of exiting
    if args.loop:
        os.environ["AGENT_LOOP"] = "1"
        logger.info(
            "Loop mode enabled — agent will return to IDLE after task completion"
        )

    config_path = args.config

    if args.mode == "persistent":
        thread_id = args.thread_id  # None → lifespan auto-creates via orchestrator
        run_persistent_server(config_path, args.host, args.port, thread_id)
        return

    if args.mode == "worker":
        run_server(config_path, args.host, args.port)
        return

    # Dual mode (default) — accepts both jobs and sessions
    run_dual_server(config_path, args.host, args.port)


if __name__ == "__main__":
    main()
