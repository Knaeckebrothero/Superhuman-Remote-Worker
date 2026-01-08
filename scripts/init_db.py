#!/usr/bin/env python3
"""Initialize the PostgreSQL database.

This script:
1. Creates the database if it doesn't exist
2. Optionally drops all tables (--force-reset)
3. Creates all tables from schema.sql

Usage:
    python scripts/init_db.py                    # Create tables if not exist
    python scripts/init_db.py --force-reset      # Drop everything, recreate
    python scripts/init_db.py --with-vector      # Include pgvector tables
"""
import logging
import asyncio
import argparse
import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:
    print("Error: asyncpg is required. Install with: pip install asyncpg")
    sys.exit(1)

# Schema files
SCHEMA_FILE = project_root / "src" / "database" / "schema.sql"
SCHEMA_VECTOR_FILE = project_root / "src" / "database" / "schema_vector.sql"


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
    except asyncpg.exceptions.UndefinedObjectError as e:
        if "vector" in str(e).lower():
            print(f"  Skipped {name} (pgvector not installed)")
            return True
        raise
    finally:
        await conn.close()


async def verify_tables(connection_string: str) -> bool:
    """Verify required tables exist."""
    required = ['jobs', 'requirement_cache', 'llm_requests', 'agent_checkpoints', 'candidate_workspace']

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

        # Check optional tables
        vector_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'workspace_embeddings')"
        )
        print(f"    workspace_embeddings: {'ok' if vector_exists else 'not installed (optional)'}")

        return len(missing) == 0
    finally:
        await conn.close()


async def init_database(force_reset: bool = False, with_vector: bool = False) -> bool:
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

    # Apply vector schema if requested
    if with_vector:
        await run_schema(connection_string, SCHEMA_VECTOR_FILE, "schema_vector.sql")

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


async def main():
    parser = argparse.ArgumentParser(description="Initialize Graph-RAG PostgreSQL database")
    parser.add_argument("--force-reset", action="store_true", help="Drop all tables and recreate")
    parser.add_argument("--with-vector", action="store_true", help="Include pgvector tables")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    print("=" * 50)
    print("Graph-RAG Database Initialization")
    print("=" * 50)

    if args.force_reset:
        print("Mode: FORCE RESET (all data will be deleted!)")
    print()

    success = await init_database(
        force_reset=args.force_reset,
        with_vector=args.with_vector
    )

    print()
    if success:
        print("Database ready!")
    else:
        print("Database initialization failed!")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
