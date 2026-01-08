#!/usr/bin/env python3
"""
Application initialization script for the Graph-RAG system.

This script initializes the complete application environment including:
- PostgreSQL database (job tracking, requirement cache)
- Neo4j knowledge graph (requirements, business objects, messages)
- MongoDB (optional, for LLM request archiving)

Usage:
    # Initialize everything (PostgreSQL + Neo4j)
    python scripts/app_init.py

    # Force reset everything (delete all data, recreate databases)
    python scripts/app_init.py --force-reset

    # With seed data (load sample requirements into Neo4j)
    python scripts/app_init.py --seed

    # Skip specific databases
    python scripts/app_init.py --skip-postgres
    python scripts/app_init.py --skip-neo4j
    python scripts/app_init.py --skip-mongodb

    # Initialize only specific database
    python scripts/app_init.py --only-neo4j
    python scripts/app_init.py --only-postgres

    # Export current Neo4j data (for creating seed files)
    python scripts/app_init.py --export-neo4j
"""
import argparse
import logging
import os
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure logging for application initialization."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Initialize the Graph-RAG application databases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/app_init.py                    # Initialize all databases
  python scripts/app_init.py --force-reset      # Reset and reinitialize everything
  python scripts/app_init.py --seed             # Initialize with sample data
  python scripts/app_init.py --skip-mongodb     # Skip MongoDB (if not needed)
  python scripts/app_init.py --only-neo4j       # Only initialize Neo4j
  python scripts/app_init.py --export-neo4j     # Export Neo4j data for seeding

Database reset commands (development):
  python scripts/app_init.py --force-reset --seed  # Fresh start with seed data
        """,
    )
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="Delete all data and recreate databases (WARNING: deletes all data)",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Load seed/sample data after initialization",
    )
    parser.add_argument(
        "--skip-postgres",
        action="store_true",
        help="Skip PostgreSQL initialization",
    )
    parser.add_argument(
        "--skip-neo4j",
        action="store_true",
        help="Skip Neo4j initialization",
    )
    parser.add_argument(
        "--skip-mongodb",
        action="store_true",
        help="Skip MongoDB initialization",
    )
    parser.add_argument(
        "--only-postgres",
        action="store_true",
        help="Only initialize PostgreSQL",
    )
    parser.add_argument(
        "--only-neo4j",
        action="store_true",
        help="Only initialize Neo4j",
    )
    parser.add_argument(
        "--only-mongodb",
        action="store_true",
        help="Only initialize MongoDB",
    )
    parser.add_argument(
        "--export-neo4j",
        action="store_true",
        help="Export current Neo4j data to seed file",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )
    return parser.parse_args()


def run_postgres_init(logger: logging.Logger, force_reset: bool = False) -> bool:
    """
    Run PostgreSQL database initialization.

    Args:
        logger: Logger instance.
        force_reset: If True, drop all tables and recreate.

    Returns:
        True if successful, False otherwise.
    """
    try:
        from scripts.init_db import initialize_postgres
        return initialize_postgres(logger, force_reset)
    except ImportError as e:
        logger.error(f"  Could not import init_db module: {e}")
        return False
    except Exception as e:
        logger.error(f"  Error running PostgreSQL initialization: {e}")
        return False


def run_neo4j_init(logger: logging.Logger, force_reset: bool = False, seed: bool = False) -> bool:
    """
    Run Neo4j knowledge graph initialization.

    Args:
        logger: Logger instance.
        force_reset: If True, clear all data and re-seed.
        seed: If True, load seed data.

    Returns:
        True if successful, False otherwise.
    """
    try:
        from scripts.init_neo4j import initialize_neo4j
        return initialize_neo4j(logger, clear=force_reset, seed=seed)
    except ImportError as e:
        logger.warning(f"  Could not import init_neo4j module: {e}")
        return True  # Don't fail if Neo4j module not available
    except Exception as e:
        logger.warning(f"  Error running Neo4j initialization: {e}")
        logger.info("  Hint: Make sure Neo4j is running")
        return True  # Don't fail the whole init


def run_mongodb_init(logger: logging.Logger, force_reset: bool = False) -> bool:
    """
    Run MongoDB initialization.

    Args:
        logger: Logger instance.
        force_reset: If True, drop collections and recreate.

    Returns:
        True if successful, False otherwise.
    """
    try:
        from scripts.init_mongodb import initialize_mongodb
        return initialize_mongodb(logger, force_reset)
    except ImportError as e:
        logger.info(f"  MongoDB module not available (optional): {e}")
        return True  # MongoDB is optional
    except Exception as e:
        logger.warning(f"  Error running MongoDB initialization: {e}")
        return True  # Don't fail the whole init


def run_neo4j_export(logger: logging.Logger) -> bool:
    """
    Export current Neo4j data to seed file.

    Args:
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    try:
        from scripts.init_neo4j import export_neo4j_data
        return export_neo4j_data(logger)
    except ImportError as e:
        logger.error(f"  Could not import init_neo4j module: {e}")
        return False
    except Exception as e:
        logger.error(f"  Error exporting Neo4j data: {e}")
        return False


def verify_setup(logger: logging.Logger, skip_postgres: bool, skip_neo4j: bool, skip_mongodb: bool) -> bool:
    """
    Verify that all components are properly initialized.

    Returns:
        True if verification passes, False otherwise.
    """
    all_ok = True

    # Check PostgreSQL
    if not skip_postgres:
        try:
            import asyncpg
            import asyncio

            async def check_pg():
                conn_str = os.getenv("DATABASE_URL")
                if not conn_str:
                    return False
                try:
                    conn = await asyncpg.connect(conn_str)
                    await conn.close()
                    return True
                except Exception:
                    return False

            if asyncio.run(check_pg()):
                logger.info("  PostgreSQL: connected")
            else:
                logger.warning("  PostgreSQL: not connected")
                all_ok = False
        except ImportError:
            logger.warning("  PostgreSQL: asyncpg not installed")

    # Check Neo4j
    if not skip_neo4j:
        try:
            from neo4j import GraphDatabase
            uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
            user = os.getenv("NEO4J_USERNAME", "neo4j")
            password = os.getenv("NEO4J_PASSWORD", "neo4j_password")
            driver = GraphDatabase.driver(uri, auth=(user, password))
            driver.verify_connectivity()
            driver.close()
            logger.info("  Neo4j: connected")
        except Exception as e:
            logger.warning(f"  Neo4j: not connected ({e})")

    # Check MongoDB
    if not skip_mongodb:
        mongo_url = os.getenv("MONGODB_URL")
        if mongo_url:
            try:
                from pymongo import MongoClient
                client = MongoClient(mongo_url, serverSelectionTimeoutMS=2000)
                client.server_info()
                client.close()
                logger.info("  MongoDB: connected")
            except Exception as e:
                logger.warning(f"  MongoDB: not connected ({e})")
        else:
            logger.info("  MongoDB: not configured (optional)")

    return all_ok


def main() -> int:
    """Main entry point for application initialization."""
    args = parse_args()
    logger = setup_logging(args.verbose)

    # Handle --only-* flags
    if args.only_postgres:
        args.skip_neo4j = True
        args.skip_mongodb = True
    elif args.only_neo4j:
        args.skip_postgres = True
        args.skip_mongodb = True
    elif args.only_mongodb:
        args.skip_postgres = True
        args.skip_neo4j = True

    # Handle export mode
    if args.export_neo4j:
        logger.info("")
        logger.info("=== Neo4j Data Export ===")
        logger.info("")
        success = run_neo4j_export(logger)
        return 0 if success else 1

    logger.info("")
    logger.info("=" * 60)
    logger.info("Graph-RAG Application Initialization")
    logger.info("=" * 60)
    if args.force_reset:
        logger.warning("WARNING: Force reset mode - all data will be deleted!")
    logger.info("")

    # Calculate steps
    steps = []
    if not args.skip_postgres:
        steps.append(("PostgreSQL", run_postgres_init))
    if not args.skip_neo4j:
        steps.append(("Neo4j", run_neo4j_init))
    if not args.skip_mongodb:
        steps.append(("MongoDB", run_mongodb_init))
    steps.append(("Verify", None))

    total_steps = len(steps)
    current_step = 0

    # Run initialization steps
    for name, init_func in steps:
        current_step += 1

        if name == "Verify":
            logger.info("")
            logger.info(f"[{current_step}/{total_steps}] Verifying setup...")
            verify_setup(logger, args.skip_postgres, args.skip_neo4j, args.skip_mongodb)
            continue

        logger.info(f"[{current_step}/{total_steps}] Initializing {name}...")

        if name == "Neo4j":
            success = init_func(logger, args.force_reset, args.seed)
        else:
            success = init_func(logger, args.force_reset)

        if not success:
            if name == "PostgreSQL":
                logger.error("")
                logger.error(f"{name} initialization failed. Aborting.")
                return 1
            else:
                logger.warning(f"  {name} initialization had issues (continuing...)")
        logger.info("")

    # Success message
    logger.info("=" * 60)
    logger.info("Initialization Complete!")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInitialization cancelled by user.")
        sys.exit(1)
    except Exception as e:
        logging.getLogger(__name__).error(f"Unexpected error: {e}")
        sys.exit(1)
