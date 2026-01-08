"""
Application initialization script for the Fessi backend.

This script initializes the complete application environment including:
- Filesystem directory structure
- Environment configuration (.env file)
- SSL certificates for development
- PostgreSQL database (via db_init.py)
- Neo4j knowledge graph (via neo4j_init.py)

Usage:
    # Initialize everything (filesystem + database + Neo4j)
    python backend/app_init.py

    # Force reset everything (delete .filesystem, recreate databases)
    python backend/app_init.py --force-reset

    # With seed data
    python backend/app_init.py --seed

    # Skip database initialization
    python backend/app_init.py --skip-db

    # Skip Neo4j initialization
    python backend/app_init.py --skip-neo4j

    # Setup filesystem only (no database, no certs)
    python backend/app_init.py --setup-only

    # Production mode: migrate PostgreSQL schema only (skips Neo4j)
    python backend/app_init.py --prod
"""
import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

# Add project root to path so we can import backend modules
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv, find_dotenv

# Load environment variables
load_dotenv(find_dotenv())

# Constants
BASE_DIR = Path(__file__).parent.parent  # Project root
FILESYSTEM_DIR = Path(os.getenv("FILESYSTEM_PATH", "filesystem"))  # TODO: Change this to use the .env
DEVCERTS_DIR = Path("devcerts")
ENV_EXAMPLE = BASE_DIR / ".env.example"
ENV_FILE = BASE_DIR / ".env"


def setup_logging() -> logging.Logger:
    """
    Configure logging for application initialization.

    Returns:
        Logger instance configured for console output.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """
    Parse command line arguments.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description="Initialize the Fessi backend application.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python backend/app_init.py                    # Initialize everything
  python backend/app_init.py --force-reset      # Reset and reinitialize
  python backend/app_init.py --seed             # Initialize with test data
  python backend/app_init.py --skip-db          # Skip database setup
  python backend/app_init.py --setup-only       # Filesystem only
  python backend/app_init.py --prod             # Production migration (PostgreSQL only)

Flags execute in order: --prod -> --force-reset -> --seed
        """,
    )
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="Delete .filesystem/ and reset database (WARNING: deletes all data)",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Insert test/example data into database",
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Skip database initialization",
    )
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="Only create filesystem structure (no database, no certs)",
    )
    parser.add_argument(
        "--no-certs",
        action="store_true",
        help="Skip SSL certificate generation",
    )
    parser.add_argument(
        "--skip-neo4j",
        action="store_true",
        help="Skip Neo4j knowledge graph initialization",
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Production mode: migrate PostgreSQL schema only (skips Neo4j)",
    )
    return parser.parse_args()


def get_filesystem_dirs() -> list[Path]:
    """
    Get list of directories to create for the application.

    Returns:
        List of Path objects for required directories.
    """
    return [
        FILESYSTEM_DIR,
        FILESYSTEM_DIR / "logs",
    ]


def setup_filesystem(logger: logging.Logger, force_reset: bool = False) -> bool:
    """
    Create the required directory structure for the application.

    Args:
        logger: Logger instance.
        force_reset: If True, delete existing .filesystem/ first.

    Returns:
        True if successful, False otherwise.
    """
    try:
        # Force reset if requested
        if force_reset and FILESYSTEM_DIR.exists():
            logger.warning(f"  Deleting existing {FILESYSTEM_DIR}/...")
            shutil.rmtree(FILESYSTEM_DIR)
            logger.info(f"  ✓ Deleted {FILESYSTEM_DIR}/")

        # Create directories
        dirs = get_filesystem_dirs()
        created_count = 0

        for directory in dirs:
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                logger.info(f"  ✓ Created {directory}/")
                created_count += 1
            else:
                logger.info(f"  ✓ {directory}/ already exists")

        return True

    except Exception as e:
        logger.error(f"  ✗ Error setting up filesystem: {e}")
        return False


def setup_env_file(logger: logging.Logger) -> bool:
    """
    Copy .env.example to .env if .env doesn't exist.

    Args:
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    try:
        if ENV_FILE.exists():
            logger.info(f"  ✓ {ENV_FILE.name} already exists")
            return True

        if not ENV_EXAMPLE.exists():
            logger.warning(f"  ⚠ {ENV_EXAMPLE.name} not found, skipping .env creation")
            return True

        shutil.copy(ENV_EXAMPLE, ENV_FILE)
        logger.info(f"  ✓ Created {ENV_FILE.name} from {ENV_EXAMPLE.name}")
        logger.warning("  ⚠ Remember to configure POSTGRES_PASSWORD in .env!")
        return True

    except Exception as e:
        logger.error(f"  ✗ Error setting up .env file: {e}")
        return False


def setup_ssl_certificates(logger: logging.Logger) -> bool:
    """
    Generate development SSL certificates using trustme.

    Args:
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    try:
        # Check if certificates already exist
        cert_file = DEVCERTS_DIR / "server.pem"
        key_file = DEVCERTS_DIR / "server.key"

        if cert_file.exists() and key_file.exists():
            logger.info(f"  ✓ SSL certificates already exist in {DEVCERTS_DIR}/")
            return True

        # Import and run the certificate setup
        from backend.utils.certificates import setup_development_certificates

        cert_path, key_path = setup_development_certificates()
        logger.info(f"  ✓ Generated SSL certificates in {DEVCERTS_DIR}/")
        logger.info(f"    - {Path(cert_path).name}")
        logger.info(f"    - {Path(key_path).name}")
        logger.info(f"    - ca.pem")
        return True

    except ImportError as e:
        logger.error(f"  ✗ Could not import certificates module: {e}")
        logger.info("  Hint: Make sure 'trustme' is installed: pip install trustme")
        return False
    except Exception as e:
        logger.error(f"  ✗ Error generating SSL certificates: {e}")
        return False


def run_db_init(logger: logging.Logger, force_reset: bool = False, seed: bool = False, prod: bool = False) -> bool:
    """
    Run the database initialization script.

    Args:
        logger: Logger instance.
        force_reset: If True, drop all tables and recreate.
        seed: If True, insert test data.
        prod: If True, run production migration (add missing columns).

    Returns:
        True if successful, False otherwise.
    """
    try:
        # Import db_init module
        from backend.database import db_init

        # Create args namespace to match db_init expectations
        class DbInitArgs:
            pass

        args = DbInitArgs()
        args.host = os.getenv("POSTGRES_HOST", "localhost")
        args.port = int(os.getenv("POSTGRES_PORT", "5432"))
        args.database = os.getenv("POSTGRES_DB", "fessi_chat")
        args.user = os.getenv("POSTGRES_USER", "fessi")
        args.password = os.getenv("POSTGRES_PASSWORD", "")
        args.force_reset = force_reset
        args.seed = seed
        args.prod = prod

        # Create a sub-logger for db_init
        db_logger = logging.getLogger("db_init")
        db_logger.setLevel(logging.INFO)

        # Run initialization
        success = db_init.initialize_database(args, db_logger)
        return success

    except ImportError as e:
        logger.error(f"  ✗ Could not import db_init module: {e}")
        return False
    except Exception as e:
        logger.error(f"  ✗ Error running database initialization: {e}")
        return False


def run_neo4j_init(logger: logging.Logger, force_reset: bool = False, seed: bool = True) -> bool:
    """
    Run the Neo4j knowledge graph initialization script.

    Args:
        logger: Logger instance.
        force_reset: If True, clear all data and re-seed.
        seed: If True, insert knowledge graph data.

    Returns:
        True if successful, False otherwise.
    """
    try:
        # Import neo4j_init module
        from backend.database import neo4j_init

        # Create args namespace to match neo4j_init expectations
        class Neo4jInitArgs:
            pass

        args = Neo4jInitArgs()
        args.uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        args.user = os.getenv("NEO4J_USER", "neo4j")
        args.password = os.getenv("NEO4J_PASSWORD", "fessi_neo4j_dev")
        args.force_reset = force_reset
        args.no_seed = not seed

        # Create a sub-logger for neo4j_init
        neo4j_logger = logging.getLogger("neo4j_init")
        neo4j_logger.setLevel(logging.INFO)

        # Run initialization
        success = neo4j_init.initialize_neo4j(args, neo4j_logger)
        return success

    except ImportError as e:
        logger.warning(f"  ⚠ Could not import neo4j_init module: {e}")
        logger.info("  Hint: Neo4j may not be required for basic functionality")
        return True  # Don't fail the whole init if Neo4j is not available
    except Exception as e:
        logger.warning(f"  ⚠ Error running Neo4j initialization: {e}")
        logger.info("  Hint: Make sure Neo4j is running: cd docker && docker-compose up -d neo4j")
        return True  # Don't fail the whole init if Neo4j is not available


def verify_setup(logger: logging.Logger, skip_db: bool = False, skip_certs: bool = False, skip_neo4j: bool = False) -> bool:
    """
    Verify that all components are properly initialized.

    Args:
        logger: Logger instance.
        skip_db: Skip database verification.
        skip_certs: Skip certificate verification.
        skip_neo4j: Skip Neo4j verification.

    Returns:
        True if verification passes, False otherwise.
    """
    all_ok = True

    # Check directories
    for directory in get_filesystem_dirs():
        if directory.exists():
            logger.info(f"  ✓ Directory {directory}/ exists")
        else:
            logger.error(f"  ✗ Directory {directory}/ missing")
            all_ok = False

    # Check .env file
    if ENV_FILE.exists():
        logger.info(f"  ✓ Environment file {ENV_FILE.name} exists")
    else:
        logger.warning(f"  ⚠ Environment file {ENV_FILE.name} missing")

    # Check SSL certificates
    if not skip_certs:
        cert_file = DEVCERTS_DIR / "server.pem"
        key_file = DEVCERTS_DIR / "server.key"
        if cert_file.exists() and key_file.exists():
            logger.info(f"  ✓ SSL certificates exist in {DEVCERTS_DIR}/")
        else:
            logger.warning(f"  ⚠ SSL certificates missing in {DEVCERTS_DIR}/")

    # Check database connection
    if not skip_db:
        try:
            from backend.database import db
            # Try to get engine - this verifies connection config is valid
            if db.engine:
                logger.info("  ✓ PostgreSQL connection configured")
            else:
                logger.warning("  ⚠ PostgreSQL connection not configured")
        except Exception as e:
            logger.warning(f"  ⚠ Could not verify PostgreSQL: {e}")

    # Check Neo4j connection
    if not skip_neo4j:
        try:
            from backend.database.neo4j_db import neo4j_db
            if neo4j_db.is_connected():
                logger.info("  ✓ Neo4j connection verified")
            else:
                logger.warning("  ⚠ Neo4j not connected (knowledge graph features unavailable)")
        except Exception as e:
            logger.warning(f"  ⚠ Could not verify Neo4j: {e}")

    return all_ok


def main() -> int:
    """
    Main entry point for application initialization.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    logger = setup_logging()
    args = parse_args()

    # Production mode automatically skips Neo4j
    if args.prod:
        args.skip_neo4j = True

    logger.info("")
    logger.info("=== Fessi Backend Initialization ===")
    if args.prod:
        logger.info("    (Production migration mode - Neo4j skipped)")
    logger.info("")

    # Calculate total steps based on flags
    total_steps = 6  # Base: filesystem, env, certs, postgres, neo4j, verify
    if args.setup_only:
        total_steps = 2
    else:
        if args.no_certs:
            total_steps -= 1
        if args.skip_db:
            total_steps -= 1
        if args.skip_neo4j:
            total_steps -= 1

    current_step = 0

    # Step 1: Setup filesystem
    current_step += 1
    logger.info(f"[{current_step}/{total_steps}] Setting up filesystem...")
    if not setup_filesystem(logger, args.force_reset):
        logger.error("")
        logger.error("Failed to setup filesystem. Aborting.")
        return 1

    # Step 2: Setup .env file
    current_step += 1
    logger.info("")
    logger.info(f"[{current_step}/{total_steps}] Setting up environment...")
    if not setup_env_file(logger):
        logger.error("")
        logger.error("Failed to setup environment file. Aborting.")
        return 1

    # Exit early if setup-only
    if args.setup_only:
        logger.info("")
        logger.info("=== Setup Complete (filesystem only) ===")
        logger.info("")
        logger.info("Next steps:")
        logger.info("  1. Configure .env file with your PostgreSQL and Neo4j credentials")
        logger.info("  2. Run: python backend/app_init.py")
        return 0

    # Step 3: Setup SSL certificates
    if not args.no_certs:
        current_step += 1
        logger.info("")
        logger.info(f"[{current_step}/{total_steps}] Setting up SSL certificates...")
        if not setup_ssl_certificates(logger):
            logger.warning("")
            logger.warning("SSL certificate setup failed, but continuing...")

    # Step 4: Initialize PostgreSQL database
    if not args.skip_db:
        current_step += 1
        logger.info("")
        logger.info(f"[{current_step}/{total_steps}] Initializing PostgreSQL database...")
        if not run_db_init(logger, args.force_reset, args.seed, args.prod):
            logger.error("")
            logger.error("PostgreSQL initialization failed. Aborting.")
            return 1

    # Step 5: Initialize Neo4j knowledge graph
    if not args.skip_neo4j:
        current_step += 1
        logger.info("")
        logger.info(f"[{current_step}/{total_steps}] Initializing Neo4j knowledge graph...")
        # Neo4j init doesn't fail the whole process if unavailable
        run_neo4j_init(logger, args.force_reset, args.seed)

    # Step 6: Verify setup
    current_step += 1
    logger.info("")
    logger.info(f"[{current_step}/{total_steps}] Verifying setup...")
    verify_setup(logger, args.skip_db, args.no_certs, args.skip_neo4j)

    # Success message
    logger.info("")
    logger.info("=== Initialization Complete ===")
    logger.info("")
    logger.info("To start the backend:")
    logger.info("  python start_backend.py")
    logger.info("")

    if args.seed:
        logger.info("Test credentials (from seed data):")
        logger.info("  - test@example.com")
        logger.info("  - admin@example.com")
        logger.info("  - demo@example.com")
        logger.info("")

    return 0


if __name__ == "__main__":
    # Set up basic logging for early errors before main() configures it
    init_logger = logging.getLogger(__name__)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        init_logger.info("")
        init_logger.info("Initialization cancelled by user.")
        sys.exit(1)
    except Exception as e:
        init_logger.error(f"Unexpected error: {e}")
        sys.exit(1)
