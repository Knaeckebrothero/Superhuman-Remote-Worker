#!/usr/bin/env python3
"""Database initialization for the orchestrator.

This module provides database initialization functionality for:
- PostgreSQL (jobs, agents, requirements, citations)
- MongoDB (LLM request archiving, optional)

Can be used standalone or imported by the root init.py.

Usage:
    # Initialize databases (idempotent)
    python -m orchestrator.init

    # Force reset (delete all data, recreate)
    python -m orchestrator.init --force-reset

    # Skip specific databases
    python -m orchestrator.init --skip-mongodb
    python -m orchestrator.init --skip-postgres

    # Verify connectivity only
    python -m orchestrator.init --verify

    # Backup/restore
    python -m orchestrator.init --backup /path/to/backup
    python -m orchestrator.init --restore /path/to/backup
"""
import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for initialization."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# =============================================================================
# PostgreSQL Initialization
# =============================================================================

def get_postgres_connection_string() -> str:
    """Get PostgreSQL connection string from environment."""
    connection_string = os.getenv("DATABASE_URL")
    if not connection_string:
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        user = os.getenv("POSTGRES_USER", "graphrag")
        password = os.getenv("POSTGRES_PASSWORD", "graphrag_password")
        database = os.getenv("POSTGRES_DB", "graphrag")
        connection_string = f"postgresql://{user}:{password}@{host}:{port}/{database}"
    return connection_string


def _parse_connection_string(connection_string: str) -> dict:
    """Parse connection string into components for pg_dump/pg_restore."""
    parsed = urlparse(connection_string)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "graphrag",
        "password": parsed.password or "",
        "database": parsed.path.lstrip('/').split('?')[0] or "graphrag",
    }


async def init_postgres(force_reset: bool = False) -> bool:
    """Initialize PostgreSQL database.

    Uses the PostgresDB class from orchestrator.database.postgres for:
    - Database creation (if not exists)
    - Schema application (idempotent)
    - Schema reset (if force_reset=True)
    - Schema verification

    Args:
        force_reset: If True, drop all tables and recreate schema.

    Returns:
        True if initialization successful, False otherwise.
    """
    try:
        from orchestrator.database.postgres import PostgresDB
    except ImportError as e:
        logger.error(f"  Could not import PostgresDB: {e}")
        return False

    connection_string = get_postgres_connection_string()
    db_name = connection_string.split('/')[-1].split('?')[0]
    logger.info(f"  Database: {db_name}")

    db = PostgresDB(connection_string)

    try:
        # Create database if it doesn't exist
        try:
            created = await db.create_database_if_not_exists()
            if created:
                logger.info(f"  Created database: {db_name}")
        except Exception as e:
            logger.warning(f"  Could not check/create database: {e}")

        # Connect to the database
        await db.connect()

        # Reset schema if requested
        if force_reset:
            logger.info("  Resetting schema (dropping all tables)...")
            await db.reset_schema()
        else:
            # Apply schema (idempotent)
            await db.ensure_schema()
            logger.info("  Applied schema.sql")

        # Verify tables exist
        logger.info("  Verifying tables:")
        table_status = await db.verify_schema()
        all_exist = True
        for table, exists in table_status.items():
            status = "ok" if exists else "MISSING"
            logger.info(f"    {table}: {status}")
            if not exists:
                all_exist = False

        return all_exist

    except Exception as e:
        logger.error(f"  PostgreSQL initialization failed: {e}")
        return False

    finally:
        await db.close()


async def verify_postgres() -> dict:
    """Verify PostgreSQL connectivity and schema.

    Returns:
        Dict with 'connected', 'tables', and any error info.
    """
    try:
        from orchestrator.database.postgres import PostgresDB
    except ImportError:
        return {"connected": False, "error": "asyncpg not installed"}

    connection_string = get_postgres_connection_string()
    db = PostgresDB(connection_string)

    try:
        await db.connect()
        tables = await db.verify_schema()
        return {
            "connected": True,
            "tables": tables,
            "all_tables_exist": all(tables.values()),
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}
    finally:
        if db.is_connected:
            await db.close()


def backup_postgres(backup_file: Path) -> bool:
    """Backup PostgreSQL using pg_dump.

    Args:
        backup_file: Path to write the backup file.

    Returns:
        True if successful, False otherwise.
    """
    connection_string = get_postgres_connection_string()
    params = _parse_connection_string(connection_string)

    cmd = [
        "pg_dump",
        "-h", params["host"],
        "-p", params["port"],
        "-U", params["user"],
        "-d", params["database"],
        "-F", "c",  # Custom format (compressed)
        "-f", str(backup_file),
    ]

    env = os.environ.copy()
    env["PGPASSWORD"] = params["password"]

    logger.info(f"  Running pg_dump for database: {params['database']}")

    try:
        subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
        logger.info(f"  Backup created: {backup_file}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"  pg_dump failed: {e.stderr}")
        return False
    except FileNotFoundError:
        logger.error("  pg_dump not found. Is PostgreSQL client installed?")
        return False


def restore_postgres(backup_file: Path) -> bool:
    """Restore PostgreSQL from pg_dump backup.

    Args:
        backup_file: Path to the backup file.

    Returns:
        True if successful, False otherwise.
    """
    if not backup_file.exists():
        logger.error(f"  Backup file not found: {backup_file}")
        return False

    connection_string = get_postgres_connection_string()
    params = _parse_connection_string(connection_string)

    env = os.environ.copy()
    env["PGPASSWORD"] = params["password"]

    # Clear database first
    logger.info(f"  Clearing database: {params['database']}")
    try:
        asyncio.run(_reset_postgres_schema())
    except Exception as e:
        logger.warning(f"  Could not clear database: {e}")

    cmd = [
        "pg_restore",
        "-h", params["host"],
        "-p", params["port"],
        "-U", params["user"],
        "-d", params["database"],
        "--clean",
        "--if-exists",
        str(backup_file),
    ]

    logger.info(f"  Running pg_restore for database: {params['database']}")

    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0 and "error" in result.stderr.lower():
            logger.warning(f"  pg_restore warnings: {result.stderr[:200]}")
        logger.info(f"  Restore completed from: {backup_file}")
        return True
    except FileNotFoundError:
        logger.error("  pg_restore not found. Is PostgreSQL client installed?")
        return False


async def _reset_postgres_schema():
    """Helper to reset PostgreSQL schema."""
    from orchestrator.database.postgres import PostgresDB
    connection_string = get_postgres_connection_string()
    db = PostgresDB(connection_string)
    try:
        await db.connect()
        await db.reset_schema()
    finally:
        await db.close()


# =============================================================================
# MongoDB Initialization
# =============================================================================

def get_mongodb_url() -> Optional[str]:
    """Get MongoDB URL from environment."""
    return os.getenv("MONGODB_URL")


def _parse_mongodb_url(url: str) -> dict:
    """Parse MongoDB URL into components for mongodump/mongorestore."""
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 27017),
        "database": parsed.path.lstrip('/').split('?')[0] or "graphrag_logs",
        "username": parsed.username,
        "password": parsed.password,
    }


async def init_mongodb(force_reset: bool = False) -> bool:
    """Initialize MongoDB collections and indexes.

    MongoDB is optional - returns True if not configured.

    Args:
        force_reset: If True, clear all collections before reinitializing.

    Returns:
        True if initialization successful or MongoDB not configured.
    """
    mongo_url = get_mongodb_url()
    if not mongo_url:
        logger.info("  MongoDB not configured (MONGODB_URL not set)")
        logger.info("  Skipping MongoDB initialization (optional component)")
        return True

    try:
        from pymongo import MongoClient
    except ImportError:
        logger.info("  pymongo not installed (MongoDB is optional)")
        return True

    try:
        client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
        client.server_info()  # Test connection
        logger.info("  Connected to MongoDB")
    except Exception as e:
        logger.warning(f"  Could not connect to MongoDB: {e}")
        logger.info("  MongoDB is optional - continuing without it")
        return True

    try:
        # Parse database name from URL
        db_name = mongo_url.split('/')[-1].split('?')[0]
        if not db_name:
            db_name = "graphrag_logs"

        db = client[db_name]

        # Show current state
        total_docs = sum(db[c].count_documents({}) for c in db.list_collection_names())
        logger.info(f"  Current state: {total_docs} documents")

        # Clear if requested
        if force_reset and total_docs > 0:
            logger.info("  Clearing database...")
            for collection_name in db.list_collection_names():
                result = db[collection_name].delete_many({})
                logger.info(f"    Deleted {result.deleted_count} documents from {collection_name}")

        # Create collections and indexes
        logger.info("  Configuring collections and indexes...")
        _create_mongodb_indexes(db)

        # Show final state
        collections = db.list_collection_names()
        logger.info(f"  Collections: {list(collections)}")

        return True

    except Exception as e:
        logger.warning(f"  MongoDB initialization error: {e}")
        return True  # Don't fail the whole init

    finally:
        client.close()


def _create_mongodb_indexes(db) -> None:
    """Create MongoDB collections and indexes."""
    # llm_requests collection
    llm_requests = db["llm_requests"]
    llm_indexes = [
        ("job_id", {"name": "idx_job_id"}),
        ("agent_type", {"name": "idx_agent_type"}),
        ("timestamp", {"name": "idx_timestamp"}),
        ("model", {"name": "idx_model"}),
        ([("job_id", 1), ("agent_type", 1), ("timestamp", -1)], {"name": "idx_job_agent_time"}),
    ]

    logger.info("  Configuring llm_requests collection...")
    for index_spec, options in llm_indexes:
        try:
            if isinstance(index_spec, list):
                llm_requests.create_index(index_spec, **options)
            else:
                llm_requests.create_index(index_spec, **options)
            logger.info(f"    Created index: {options['name']}")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.info(f"    Index exists: {options['name']}")
            else:
                logger.warning(f"    Failed to create index {options['name']}: {e}")

    # agent_audit collection
    agent_audit = db["agent_audit"]
    audit_indexes = [
        ("job_id", {"name": "idx_audit_job_id"}),
        ("step_type", {"name": "idx_audit_step_type"}),
        ("node_name", {"name": "idx_audit_node_name"}),
        ("timestamp", {"name": "idx_audit_timestamp"}),
        ([("job_id", 1), ("step_number", 1)], {"name": "idx_audit_job_step"}),
        ([("job_id", 1), ("iteration", 1), ("step_number", 1)], {"name": "idx_audit_job_iter_step"}),
        ([("job_id", 1), ("agent_type", 1), ("step_type", 1)], {"name": "idx_audit_job_agent_type"}),
    ]

    logger.info("  Configuring agent_audit collection...")
    for index_spec, options in audit_indexes:
        try:
            if isinstance(index_spec, list):
                agent_audit.create_index(index_spec, **options)
            else:
                agent_audit.create_index(index_spec, **options)
            logger.info(f"    Created index: {options['name']}")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.info(f"    Index exists: {options['name']}")
            else:
                logger.warning(f"    Failed to create index {options['name']}: {e}")


async def verify_mongodb() -> dict:
    """Verify MongoDB connectivity.

    Returns:
        Dict with 'connected', 'collections', and any error info.
    """
    mongo_url = get_mongodb_url()
    if not mongo_url:
        return {"connected": False, "configured": False}

    try:
        from pymongo import MongoClient
    except ImportError:
        return {"connected": False, "error": "pymongo not installed"}

    try:
        client = MongoClient(mongo_url, serverSelectionTimeoutMS=2000)
        client.server_info()

        db_name = mongo_url.split('/')[-1].split('?')[0] or "graphrag_logs"
        db = client[db_name]
        collections = db.list_collection_names()

        client.close()
        return {
            "connected": True,
            "configured": True,
            "collections": collections,
        }
    except Exception as e:
        return {"connected": False, "configured": True, "error": str(e)}


def backup_mongodb(backup_dir: Path) -> bool:
    """Backup MongoDB using mongodump.

    Args:
        backup_dir: Path to write the backup directory.

    Returns:
        True if successful, False otherwise.
    """
    mongo_url = get_mongodb_url()
    if not mongo_url:
        logger.info("  MongoDB not configured (MONGODB_URL not set)")
        return True

    params = _parse_mongodb_url(mongo_url)

    cmd = [
        "mongodump",
        "--host", params["host"],
        "--port", params["port"],
        "--db", params["database"],
        "--out", str(backup_dir),
    ]

    if params["username"] and params["password"]:
        cmd.extend(["--username", params["username"]])
        cmd.extend(["--password", params["password"]])
        cmd.extend(["--authenticationDatabase", "admin"])

    logger.info(f"  Running mongodump for database: {params['database']}")

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info(f"  Backup created: {backup_dir}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"  mongodump failed: {e.stderr}")
        return False
    except FileNotFoundError:
        logger.error("  mongodump not found. Is MongoDB tools installed?")
        return False


def restore_mongodb(backup_dir: Path) -> bool:
    """Restore MongoDB from mongodump backup.

    Args:
        backup_dir: Path to the backup directory.

    Returns:
        True if successful, False otherwise.
    """
    mongo_url = get_mongodb_url()
    if not mongo_url:
        logger.info("  MongoDB not configured (MONGODB_URL not set)")
        return True

    params = _parse_mongodb_url(mongo_url)

    # Find the database directory in backup
    db_backup_dir = backup_dir / params["database"]
    if not db_backup_dir.exists():
        subdirs = [d for d in backup_dir.iterdir() if d.is_dir()]
        if subdirs:
            db_backup_dir = subdirs[0]
        else:
            logger.warning(f"  No MongoDB backup data found in: {backup_dir}")
            return True

    # Clear existing data
    logger.info(f"  Clearing database: {params['database']}")
    try:
        from pymongo import MongoClient
        client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
        db = client[params["database"]]
        for collection_name in db.list_collection_names():
            db[collection_name].delete_many({})
        client.close()
    except Exception as e:
        logger.warning(f"  Could not clear database: {e}")

    cmd = [
        "mongorestore",
        "--host", params["host"],
        "--port", params["port"],
        "--db", params["database"],
        "--drop",
        str(db_backup_dir),
    ]

    if params["username"] and params["password"]:
        cmd.extend(["--username", params["username"]])
        cmd.extend(["--password", params["password"]])
        cmd.extend(["--authenticationDatabase", "admin"])

    logger.info(f"  Running mongorestore for database: {params['database']}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 and "error" in result.stderr.lower():
            logger.warning(f"  mongorestore warnings: {result.stderr[:200]}")
        logger.info(f"  Restore completed from: {backup_dir}")
        return True
    except FileNotFoundError:
        logger.error("  mongorestore not found. Is MongoDB tools installed?")
        return False


# =============================================================================
# Combined Operations
# =============================================================================

async def init_databases(
    force_reset: bool = False,
    skip_postgres: bool = False,
    skip_mongodb: bool = False,
) -> bool:
    """Initialize all databases.

    Args:
        force_reset: If True, drop and recreate all data.
        skip_postgres: Skip PostgreSQL initialization.
        skip_mongodb: Skip MongoDB initialization.

    Returns:
        True if all enabled databases initialized successfully.
    """
    success = True

    if not skip_postgres:
        logger.info("")
        logger.info("Initializing PostgreSQL...")
        if not await init_postgres(force_reset):
            success = False
            logger.error("PostgreSQL initialization failed")

    if not skip_mongodb:
        logger.info("")
        logger.info("Initializing MongoDB...")
        if not await init_mongodb(force_reset):
            # MongoDB failures are non-fatal
            logger.warning("MongoDB initialization had issues")

    return success


async def verify_databases(
    skip_postgres: bool = False,
    skip_mongodb: bool = False,
) -> dict:
    """Verify all database connections.

    Args:
        skip_postgres: Skip PostgreSQL verification.
        skip_mongodb: Skip MongoDB verification.

    Returns:
        Dict with status for each database.
    """
    result = {}

    if not skip_postgres:
        result["postgres"] = await verify_postgres()

    if not skip_mongodb:
        result["mongodb"] = await verify_mongodb()

    return result


def backup_databases(backup_dir: Path) -> dict:
    """Backup all databases.

    Args:
        backup_dir: Directory to store backups.

    Returns:
        Dict with backup status for each database.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    result = {}

    # Backup PostgreSQL
    postgres_file = backup_dir / "postgres.dump"
    result["postgres"] = {
        "file": "postgres.dump",
        "success": backup_postgres(postgres_file),
    }

    # Backup MongoDB
    mongodb_dir = backup_dir / "mongodb"
    mongodb_dir.mkdir(exist_ok=True)
    result["mongodb"] = {
        "directory": "mongodb",
        "success": backup_mongodb(mongodb_dir),
    }

    return result


def restore_databases(backup_dir: Path) -> dict:
    """Restore all databases from backup.

    Args:
        backup_dir: Directory containing backups.

    Returns:
        Dict with restore status for each database.
    """
    result = {}

    # Restore PostgreSQL
    postgres_file = backup_dir / "postgres.dump"
    if postgres_file.exists():
        result["postgres"] = {
            "file": "postgres.dump",
            "success": restore_postgres(postgres_file),
        }
    else:
        logger.info("  No PostgreSQL backup found")
        result["postgres"] = {"success": True, "skipped": True}

    # Restore MongoDB
    mongodb_dir = backup_dir / "mongodb"
    if mongodb_dir.exists() and any(mongodb_dir.iterdir()):
        result["mongodb"] = {
            "directory": "mongodb",
            "success": restore_mongodb(mongodb_dir),
        }
    else:
        logger.info("  No MongoDB backup found")
        result["mongodb"] = {"success": True, "skipped": True}

    return result


# =============================================================================
# CLI
# =============================================================================

def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Initialize orchestrator databases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m orchestrator.init                     # Initialize databases
  python -m orchestrator.init --force-reset       # Reset and reinitialize
  python -m orchestrator.init --skip-mongodb      # Skip MongoDB
  python -m orchestrator.init --verify            # Just verify connectivity
  python -m orchestrator.init --backup ./backup   # Create backup
  python -m orchestrator.init --restore ./backup  # Restore from backup
        """,
    )
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="Delete all data and recreate databases (WARNING: deletes all data)",
    )
    parser.add_argument(
        "--skip-postgres",
        action="store_true",
        help="Skip PostgreSQL initialization",
    )
    parser.add_argument(
        "--skip-mongodb",
        action="store_true",
        help="Skip MongoDB initialization",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Only verify database connectivity, don't initialize",
    )
    parser.add_argument(
        "--backup",
        metavar="PATH",
        help="Create backup to specified directory",
    )
    parser.add_argument(
        "--restore",
        metavar="PATH",
        help="Restore from backup directory",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    """Async main function."""
    # Verify mode
    if args.verify:
        logger.info("")
        logger.info("=" * 60)
        logger.info("Database Connectivity Verification")
        logger.info("=" * 60)
        logger.info("")

        result = await verify_databases(args.skip_postgres, args.skip_mongodb)

        if "postgres" in result:
            pg = result["postgres"]
            if pg.get("connected"):
                logger.info("PostgreSQL: connected")
                if not pg.get("all_tables_exist"):
                    logger.warning("  Some tables missing - run init to create them")
            else:
                logger.warning(f"PostgreSQL: not connected ({pg.get('error', 'unknown error')})")

        if "mongodb" in result:
            mg = result["mongodb"]
            if not mg.get("configured"):
                logger.info("MongoDB: not configured (optional)")
            elif mg.get("connected"):
                logger.info(f"MongoDB: connected (collections: {mg.get('collections', [])})")
            else:
                logger.warning(f"MongoDB: not connected ({mg.get('error', 'unknown error')})")

        return 0

    # Backup mode
    if args.backup:
        backup_dir = Path(args.backup)
        logger.info("")
        logger.info("=" * 60)
        logger.info("Database Backup")
        logger.info("=" * 60)
        logger.info(f"Backup directory: {backup_dir}")
        logger.info("")

        result = backup_databases(backup_dir)

        success = all(r.get("success", False) for r in result.values())
        return 0 if success else 1

    # Restore mode
    if args.restore:
        backup_dir = Path(args.restore)
        if not backup_dir.exists():
            logger.error(f"Backup directory not found: {backup_dir}")
            return 1

        logger.info("")
        logger.info("=" * 60)
        logger.info("Database Restore")
        logger.info("=" * 60)
        logger.info(f"Restoring from: {backup_dir}")
        logger.info("")

        result = restore_databases(backup_dir)

        success = all(r.get("success", False) for r in result.values())
        return 0 if success else 1

    # Initialize mode
    logger.info("")
    logger.info("=" * 60)
    logger.info("Orchestrator Database Initialization")
    logger.info("=" * 60)

    if args.force_reset:
        logger.warning("WARNING: Force reset mode - all data will be deleted!")

    success = await init_databases(
        force_reset=args.force_reset,
        skip_postgres=args.skip_postgres,
        skip_mongodb=args.skip_mongodb,
    )

    logger.info("")
    logger.info("=" * 60)
    if success:
        logger.info("Database Initialization Complete!")
    else:
        logger.error("Database Initialization Failed!")
    logger.info("=" * 60)

    return 0 if success else 1


def main() -> int:
    """Main entry point."""
    args = parse_args()
    setup_logging(args.verbose)

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nInitialization cancelled by user.")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
