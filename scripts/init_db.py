#!/usr/bin/env python3
"""Initialize the PostgreSQL database with required schema.

This script:
1. Creates the database if it doesn't exist
2. Runs the initial schema migration
3. Verifies all tables are created

Usage:
    python scripts/init_db.py
    python scripts/init_db.py --connection-string "postgresql://user:pass@host:5432/db"
"""

import asyncio
import argparse
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:
    print("Error: asyncpg is required. Install with: pip install asyncpg")
    sys.exit(1)


async def create_database_if_not_exists(connection_string: str, db_name: str) -> bool:
    """Create database if it doesn't exist.

    Args:
        connection_string: Connection string to postgres (not to target db)
        db_name: Name of database to create

    Returns:
        True if database was created, False if it already existed
    """
    # Connect to default postgres database
    conn = await asyncpg.connect(connection_string)
    try:
        # Check if database exists
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            db_name
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"Created database: {db_name}")
            return True
        else:
            print(f"Database already exists: {db_name}")
            return False
    finally:
        await conn.close()


async def run_migration(connection_string: str) -> None:
    """Run the initial schema migration.

    Args:
        connection_string: Connection string to target database
    """
    migration_file = project_root / "migrations" / "001_initial_schema.sql"

    if not migration_file.exists():
        print(f"Error: Migration file not found: {migration_file}")
        sys.exit(1)

    with open(migration_file) as f:
        schema_sql = f.read()

    conn = await asyncpg.connect(connection_string)
    try:
        # Run migration
        await conn.execute(schema_sql)
        print("Schema migration completed successfully")
    except asyncpg.exceptions.DuplicateObjectError as e:
        print(f"Note: Some objects already exist (this is normal on re-run): {e}")
    except Exception as e:
        print(f"Error running migration: {e}")
        raise
    finally:
        await conn.close()


async def verify_schema(connection_string: str) -> bool:
    """Verify all required tables exist.

    Args:
        connection_string: Connection string to target database

    Returns:
        True if all tables exist
    """
    required_tables = [
        'jobs',
        'requirement_cache',
        'llm_requests',
        'agent_checkpoints',
        'candidate_workspace'
    ]

    conn = await asyncpg.connect(connection_string)
    try:
        missing = []
        for table in required_tables:
            exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = $1
                )
                """,
                table
            )
            if exists:
                print(f"  ✓ {table}")
            else:
                print(f"  ✗ {table}")
                missing.append(table)

        if missing:
            print(f"\nMissing tables: {', '.join(missing)}")
            return False
        else:
            print("\nAll required tables exist!")
            return True
    finally:
        await conn.close()


async def main():
    parser = argparse.ArgumentParser(description="Initialize Graph-RAG PostgreSQL database")
    parser.add_argument(
        "--connection-string",
        help="PostgreSQL connection string",
        default=os.getenv("DATABASE_URL")
    )
    parser.add_argument(
        "--skip-create",
        action="store_true",
        help="Skip database creation (assume it exists)"
    )
    args = parser.parse_args()

    if not args.connection_string:
        # Try to construct from individual env vars
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        user = os.getenv("POSTGRES_USER", "graphrag")
        password = os.getenv("POSTGRES_PASSWORD", "password")
        database = os.getenv("POSTGRES_DB", "graphrag")
        args.connection_string = f"postgresql://{user}:{password}@{host}:{port}/{database}"

    print("=" * 60)
    print("Graph-RAG Database Initialization")
    print("=" * 60)
    print(f"Connection: {args.connection_string.replace(args.connection_string.split(':')[2].split('@')[0], '****')}")
    print()

    # Parse database name from connection string
    db_name = args.connection_string.split('/')[-1].split('?')[0]
    base_conn_string = args.connection_string.rsplit('/', 1)[0] + '/postgres'

    # Step 1: Create database if needed
    if not args.skip_create:
        print("Step 1: Creating database if needed...")
        try:
            await create_database_if_not_exists(base_conn_string, db_name)
        except Exception as e:
            print(f"Warning: Could not check/create database: {e}")
            print("Continuing with migration (database may already exist)...")
    print()

    # Step 2: Run migration
    print("Step 2: Running schema migration...")
    await run_migration(args.connection_string)
    print()

    # Step 3: Verify schema
    print("Step 3: Verifying schema...")
    success = await verify_schema(args.connection_string)

    print()
    if success:
        print("=" * 60)
        print("Database initialization complete!")
        print("=" * 60)
    else:
        print("Database initialization completed with errors.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
