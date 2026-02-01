#!/usr/bin/env python3
"""Initialize the PostgreSQL database.

DEPRECATED: This script is deprecated. Use 'python -m orchestrator.init' instead.

This script:
1. Creates the database if it doesn't exist
2. Optionally drops all tables (--force-reset)
3. Creates all tables from schema.sql (idempotent - uses IF NOT EXISTS)

Usage:
    python scripts/init_db.py                    # Create tables if not exist
    python scripts/init_db.py --force-reset      # Drop everything, recreate
"""
import logging
import asyncio
import argparse
import os
import subprocess
import sys
import warnings
from pathlib import Path
from urllib.parse import urlparse

# Emit deprecation warning
warnings.warn(
    "scripts/init_db.py is deprecated. Use 'python -m orchestrator.init' instead.",
    DeprecationWarning,
    stacklevel=2
)

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:
    print("Error: asyncpg is required. Install with: pip install asyncpg")
    sys.exit(1)

# Schema file (canonical location in orchestrator)
SCHEMA_FILE = project_root / "orchestrator" / "database" / "schema.sql"


def get_connection_string() -> str:
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


async def create_database_if_not_exists(connection_string: str, db_name: str) -> bool:
    """Create database if it doesn't exist."""
    base_conn = connection_string.rsplit('/', 1)[0] + '/postgres'
    conn = await asyncpg.connect(base_conn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"  Created database: {db_name}")
            return True
        return False
    finally:
        await conn.close()


async def drop_all_tables(connection_string: str) -> None:
    """Drop all tables by recreating the public schema."""
    conn = await asyncpg.connect(connection_string)
    try:
        # Nuclear option: drop and recreate public schema
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
        await conn.execute("GRANT ALL ON SCHEMA public TO public")
        print("  Dropped all tables (schema reset)")
    finally:
        await conn.close()


async def run_schema(connection_string: str, schema_file: Path, name: str) -> bool:
    """Run a schema file."""
    if not schema_file.exists():
        print(f"  Schema file not found: {schema_file}")
        return False

    with open(schema_file) as f:
        schema_sql = f.read()

    conn = await asyncpg.connect(connection_string)
    try:
        await conn.execute(schema_sql)
        print(f"  Applied {name}")
        return True
    finally:
        await conn.close()


async def verify_tables(connection_string: str) -> bool:
    """Verify required tables exist.

    Tables:
    - jobs: Job tracking and orchestration
    - agents: Registered agent pods for orchestration
    - requirements: Primary storage for extracted requirements
    - sources: Document sources for citations (citation_tool)
    - citations: Citation records (citation_tool)

    Other data is stored elsewhere:
    - LLM logging: MongoDB (via llm_archiver.py)
    - Agent checkpointing: LangGraph's AsyncPostgresSaver (creates its own tables)
    - Agent workspace: Filesystem (workspace_manager.py)
    """
    required = ['jobs', 'agents', 'requirements', 'sources', 'citations']

    conn = await asyncpg.connect(connection_string)
    try:
        missing = []
        for table in required:
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = $1)",
                table
            )
            status = "ok" if exists else "MISSING"
            print(f"    {table}: {status}")
            if not exists:
                missing.append(table)

        return len(missing) == 0
    finally:
        await conn.close()


async def init_database(force_reset: bool = False) -> bool:
    """Initialize the database."""
    connection_string = get_connection_string()
    db_name = connection_string.split('/')[-1].split('?')[0]

    print(f"  Database: {db_name}")

    # Create database if needed
    try:
        await create_database_if_not_exists(connection_string, db_name)
    except Exception as e:
        print(f"  Warning: Could not check/create database: {e}")

    # Drop tables if force reset
    if force_reset:
        await drop_all_tables(connection_string)

    # Apply schema
    if not await run_schema(connection_string, SCHEMA_FILE, "schema.sql"):
        return False

    # Verify
    print("  Verifying tables:")
    return await verify_tables(connection_string)


def initialize_postgres(logger: logging.Logger, force_reset: bool = False) -> bool:
    """Initialize PostgreSQL (called by app_init.py)."""
    try:
        return asyncio.run(init_database(force_reset=force_reset))
    except Exception as e:
        logger.error(f"  PostgreSQL initialization failed: {e}")
        return False


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


def backup_postgres(backup_file: Path, logger: logging.Logger) -> bool:
    """
    Backup PostgreSQL using pg_dump.

    Args:
        backup_file: Path to write the backup file.
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    connection_string = get_connection_string()
    params = _parse_connection_string(connection_string)

    # Build pg_dump command
    cmd = [
        "pg_dump",
        "-h", params["host"],
        "-p", params["port"],
        "-U", params["user"],
        "-d", params["database"],
        "-F", "c",  # Custom format (compressed)
        "-f", str(backup_file),
    ]

    # Set password via environment
    env = os.environ.copy()
    env["PGPASSWORD"] = params["password"]

    logger.info(f"  Running pg_dump for database: {params['database']}")

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info(f"  Backup created: {backup_file}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"  pg_dump failed: {e.stderr}")
        return False
    except FileNotFoundError:
        logger.error("  pg_dump not found. Is PostgreSQL client installed?")
        return False


def restore_postgres(backup_file: Path, logger: logging.Logger) -> bool:
    """
    Restore PostgreSQL from pg_dump backup.

    Args:
        backup_file: Path to the backup file.
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    if not backup_file.exists():
        logger.error(f"  Backup file not found: {backup_file}")
        return False

    connection_string = get_connection_string()
    params = _parse_connection_string(connection_string)

    # Set password via environment
    env = os.environ.copy()
    env["PGPASSWORD"] = params["password"]

    # First, drop and recreate the public schema to clear existing data
    logger.info(f"  Clearing database: {params['database']}")
    try:
        asyncio.run(drop_all_tables(connection_string))
    except Exception as e:
        logger.warning(f"  Could not clear database: {e}")

    # Build pg_restore command
    cmd = [
        "pg_restore",
        "-h", params["host"],
        "-p", params["port"],
        "-U", params["user"],
        "-d", params["database"],
        "--clean",  # Drop objects before recreating
        "--if-exists",  # Don't error if objects don't exist
        str(backup_file),
    ]

    logger.info(f"  Running pg_restore for database: {params['database']}")

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
        )
        # pg_restore may return non-zero for warnings, check stderr for actual errors
        if result.returncode != 0 and "error" in result.stderr.lower():
            logger.warning(f"  pg_restore warnings: {result.stderr[:200]}")

        logger.info(f"  Restore completed from: {backup_file}")
        return True
    except FileNotFoundError:
        logger.error("  pg_restore not found. Is PostgreSQL client installed?")
        return False


async def main():
    parser = argparse.ArgumentParser(description="Initialize Graph-RAG PostgreSQL database")
    parser.add_argument("--force-reset", action="store_true", help="Drop all tables and recreate")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    print("=" * 50)
    print("Graph-RAG Database Initialization")
    print("=" * 50)

    if args.force_reset:
        print("Mode: FORCE RESET (all data will be deleted!)")
    print()

    success = await init_database(force_reset=args.force_reset)

    print()
    if success:
        print("Database ready!")
    else:
        print("Database initialization failed!")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
