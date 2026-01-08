"""
Database initialization script for the Fessi backend.

This script initializes the PostgreSQL database schema and optionally seeds it
with test data. It supports multiple modes:

1. Default mode: Creates tables if they don't exist (preserves existing data)
2. Force-reset mode: Drops all tables and recreates from scratch
3. Production mode: Migrates schema by adding missing columns (preserves data)

Usage:
    # Default: Create tables if missing, preserve data
    python -m backend.database.db_init

    # Force reset: Drop all tables and recreate
    python -m backend.database.db_init --force-reset

    # With seed data: Insert test users and conversations
    python -m backend.database.db_init --seed

    # Combined: Fresh database with seed data
    python -m backend.database.db_init --force-reset --seed

    # Production migration: Update schema without losing data
    python -m backend.database.db_init --prod

Flags can be combined and execute in this order: --prod -> --force-reset -> --seed
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from sqlalchemy import create_engine, inspect, text
from dotenv import load_dotenv, find_dotenv

# Load environment variables
load_dotenv(find_dotenv())

# Constants
QUERIES_DIR = Path(__file__).parent / "queries"
SCHEMA_FILE = QUERIES_DIR / "schema.sql"
SEED_FILE = QUERIES_DIR / "seed.sql"

# Expected tables for verification
EXPECTED_TABLES = [
    "users",
    "conversations",
    "messages",
    "sessions",
    "guest_usage",
    "user_settings",
    "file_description_cache",
]


def setup_logging() -> logging.Logger:
    """
    Configure logging for database initialization.

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
        description="Initialize the Fessi PostgreSQL database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m backend.database.db_init                       # Create tables if missing
  python -m backend.database.db_init --force-reset         # Drop and recreate all tables
  python -m backend.database.db_init --seed                # Create tables and insert test data
  python -m backend.database.db_init --force-reset --seed  # Fresh database with test data
  python -m backend.database.db_init --prod                # Production migration (add missing columns)

Flags execute in order: --prod -> --force-reset -> --seed
        """,
    )
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="Drop all tables and recreate from schema.sql (WARNING: deletes all data)",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Insert test/example data after initialization",
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Production migration: update schema without losing data or adding test data",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("POSTGRES_HOST", "localhost"),
        help="PostgreSQL host (default: from .env or localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("POSTGRES_PORT", "5432")),
        help="PostgreSQL port (default: from .env or 5432)",
    )
    parser.add_argument(
        "--database",
        default=os.getenv("POSTGRES_DB", "fessi_chat"),
        help="Database name (default: from .env or fessi_chat)",
    )
    parser.add_argument(
        "--user",
        default=os.getenv("POSTGRES_USER", "fessi"),
        help="Database user (default: from .env or fessi)",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("POSTGRES_PASSWORD", ""),
        help="Database password (default: from .env)",
    )
    return parser.parse_args()


def create_database_if_not_exists(
    host: str, port: int, user: str, password: str, db_name: str, logger: logging.Logger
) -> bool:
    """
    Create the database if it doesn't exist.

    Connects to the 'postgres' system database to check for and create the
    target database. Requires appropriate PostgreSQL privileges.

    Args:
        host: PostgreSQL host.
        port: PostgreSQL port.
        user: Database user.
        password: Database password.
        db_name: Name of the database to create.
        logger: Logger instance.

    Returns:
        True if database was created, False if it already existed.

    Raises:
        psycopg2.Error: If connection or creation fails.
    """
    conn = None
    try:
        # Connect to the default 'postgres' database
        conn = psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database="postgres",
        )
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cursor = conn.cursor()

        # Check if database exists
        cursor.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (db_name,),
        )
        exists = cursor.fetchone() is not None

        if exists:
            logger.info(f"  ✓ Database '{db_name}' already exists")
            return False

        # Create the database
        cursor.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name))
        )
        logger.info(f"  ✓ Created database '{db_name}'")
        return True

    finally:
        if conn:
            conn.close()


def get_engine(host: str, port: int, user: str, password: str, db_name: str):
    """
    Create a SQLAlchemy engine for the target database.

    Args:
        host: PostgreSQL host.
        port: PostgreSQL port.
        user: Database user.
        password: Database password.
        db_name: Database name.

    Returns:
        SQLAlchemy Engine instance.
    """
    url = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"
    return create_engine(url, echo=False)


def drop_all_tables(engine, logger: logging.Logger) -> None:
    """
    Drop all tables, triggers, and functions from the database.

    Tables are dropped in dependency order to respect foreign key constraints.

    Args:
        engine: SQLAlchemy engine.
        logger: Logger instance.
    """
    # Drop order respects foreign key dependencies
    drop_order = [
        "messages",
        "sessions",
        "user_settings",
        "guest_usage",
        "conversations",
        "users",
    ]

    with engine.connect() as conn:
        # First drop triggers
        logger.info("  Dropping triggers...")
        triggers = [
            ("update_conversations_timestamp", "conversations"),
            ("update_messages_timestamp", "messages"),
            ("update_user_settings_timestamp", "user_settings"),
        ]
        for trigger_name, table_name in triggers:
            try:
                conn.execute(
                    text(f'DROP TRIGGER IF EXISTS {trigger_name} ON "{table_name}"')
                )
            except Exception:
                pass  # Ignore errors if trigger doesn't exist

        # Drop functions
        logger.info("  Dropping functions...")
        conn.execute(text("DROP FUNCTION IF EXISTS update_timestamp() CASCADE"))
        conn.execute(text("DROP FUNCTION IF EXISTS update_conversation_timestamp() CASCADE"))

        # Drop tables
        logger.info("  Dropping tables...")
        for table in drop_order:
            try:
                conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
                logger.info(f"    ✓ Dropped table '{table}'")
            except Exception as e:
                logger.warning(f"    ⚠ Could not drop table '{table}': {e}")

        conn.commit()

    logger.info("  ✓ All tables dropped")


def execute_schema_sql(engine, schema_path: Path, logger: logging.Logger) -> bool:
    """
    Execute the schema SQL file to create tables, indexes, and triggers.

    The schema uses CREATE TABLE IF NOT EXISTS, making it idempotent.

    Args:
        engine: SQLAlchemy engine.
        schema_path: Path to the schema.sql file.
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    if not schema_path.exists():
        logger.error(f"  ✗ Schema file not found: {schema_path}")
        return False

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()

        # Split by named blocks and execute each
        # The schema uses -- name: comments to separate sections
        with engine.connect() as conn:
            # Execute the entire schema as one script
            # PostgreSQL can handle multiple statements
            conn.execute(text(schema_sql))
            conn.commit()

        logger.info(f"  ✓ Executed schema from {schema_path.name}")
        return True

    except Exception as e:
        logger.error(f"  ✗ Error executing schema: {e}")
        return False


def insert_seed_data(engine, seed_path: Path, logger: logging.Logger) -> bool:
    """
    Execute the seed SQL file to insert test data.

    The seed data uses ON CONFLICT DO NOTHING, making it safe to run multiple times.

    Args:
        engine: SQLAlchemy engine.
        seed_path: Path to the seed.sql file.
        logger: Logger instance.

    Returns:
        True if successful, False otherwise.
    """
    if not seed_path.exists():
        logger.warning(f"  ⚠ Seed file not found: {seed_path}")
        return False

    try:
        with open(seed_path, "r", encoding="utf-8") as f:
            seed_sql = f.read()

        with engine.connect() as conn:
            conn.execute(text(seed_sql))
            conn.commit()

            # Count inserted seed data
            result = conn.execute(text("SELECT COUNT(*) FROM users"))
            user_count = result.scalar()

            result = conn.execute(text("SELECT COUNT(*) FROM conversations"))
            conv_count = result.scalar()

        logger.info(f"  ✓ Inserted seed data ({user_count} users, {conv_count} conversations)")
        return True

    except Exception as e:
        logger.error(f"  ✗ Error inserting seed data: {e}")
        return False


def parse_schema_columns(schema_path: Path) -> dict[str, list[tuple[str, str]]]:
    """
    Parse schema.sql to extract table columns and their definitions.

    Parses CREATE TABLE blocks to extract column names and their full SQL
    definitions (type, constraints, defaults). This is used by migrate_schema()
    to determine what columns need to be added to existing tables.

    Args:
        schema_path: Path to the schema.sql file.

    Returns:
        Dictionary mapping table names to lists of (column_name, column_definition) tuples.
        Example: {"users": [("id", "SERIAL PRIMARY KEY"), ("email", "TEXT UNIQUE NOT NULL")]}
    """
    import re

    if not schema_path.exists():
        return {}

    content = schema_path.read_text()
    result = {}

    # Find all CREATE TABLE blocks
    # Pattern matches: CREATE TABLE IF NOT EXISTS table_name (...)
    table_pattern = r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*?)\);'
    table_matches = re.findall(table_pattern, content, re.DOTALL | re.IGNORECASE)

    for table_name, columns_block in table_matches:
        columns = []

        # Split by comma, but be careful with commas inside parentheses (e.g., REFERENCES users(id))
        # We'll use a simple approach: split by newlines and process each line
        lines = columns_block.strip().split('\n')

        for line in lines:
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith('--'):
                continue

            # Remove trailing comma if present
            if line.endswith(','):
                line = line[:-1].strip()

            # Skip constraint definitions (PRIMARY KEY, FOREIGN KEY, etc.)
            if any(line.upper().startswith(kw) for kw in ['PRIMARY KEY', 'FOREIGN KEY', 'UNIQUE', 'CHECK', 'CONSTRAINT']):
                continue

            # Extract column name (first word, handling quoted identifiers)
            if line.startswith('"'):
                # Quoted identifier like "userId"
                match = re.match(r'"([^"]+)"\s+(.*)', line)
                if match:
                    col_name = match.group(1)
                    col_def = f'"{col_name}" {match.group(2)}'
                    columns.append((col_name, col_def))
            else:
                # Unquoted identifier
                parts = line.split(None, 1)
                if len(parts) >= 2:
                    col_name = parts[0]
                    col_def = line
                    columns.append((col_name, col_def))

        if columns:
            result[table_name] = columns

    return result


def migrate_schema(engine, logger: logging.Logger) -> bool:
    """
    Add missing columns and tables (production migration - additive only).

    This is a SAFE, additive-only migration that:
    - Creates missing tables via CREATE TABLE IF NOT EXISTS
    - Adds missing columns via ALTER TABLE ADD COLUMN IF NOT EXISTS
    - NEVER deletes tables, columns, or data
    - Logs warnings for changes that require manual migration

    For complex schema changes (column type changes, constraint modifications,
    column removals), a warning is logged and manual migration is required.

    Args:
        engine: SQLAlchemy engine.
        logger: Logger instance.

    Returns:
        True if migration successful, False otherwise.
    """
    try:
        expected_schema = parse_schema_columns(SCHEMA_FILE)
        if not expected_schema:
            logger.warning("  ⚠ Could not parse schema.sql, skipping column migration")
            return True

        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        columns_added = 0
        warnings_count = 0

        with engine.connect() as conn:
            for table_name, expected_columns in expected_schema.items():
                if table_name not in existing_tables:
                    logger.info(f"  Table '{table_name}' will be created by schema.sql")
                    continue

                # Get existing columns with their types for comparison
                existing_cols_info = {col["name"]: col for col in inspector.get_columns(table_name)}
                existing_col_names = set(existing_cols_info.keys())
                expected_col_names = {col[0] for col in expected_columns}

                # Check for columns that exist in DB but not in schema (orphaned)
                orphaned_cols = existing_col_names - expected_col_names
                if orphaned_cols:
                    for col in orphaned_cols:
                        logger.warning(f"    ⚠ Column '{col}' in '{table_name}' not in schema (orphaned, left untouched)")
                    warnings_count += len(orphaned_cols)

                # Add missing columns
                for col_name, col_def in expected_columns:
                    if col_name not in existing_col_names:
                        # Build ALTER TABLE statement
                        # PostgreSQL 9.6+ supports ADD COLUMN IF NOT EXISTS
                        alter_sql = f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS {col_def}'

                        try:
                            conn.execute(text(alter_sql))
                            logger.info(f"    ✓ Added column '{col_name}' to '{table_name}'")
                            columns_added += 1
                        except Exception as e:
                            logger.warning(f"    ⚠ Could not add column '{col_name}' to '{table_name}': {e}")
                            logger.warning(f"      Manual migration may be required")
                            warnings_count += 1

            conn.commit()

        # Summary
        if columns_added > 0:
            logger.info(f"  ✓ Migration complete: {columns_added} column(s) added")
        else:
            logger.info("  ✓ Schema is up to date, no columns to add")

        if warnings_count > 0:
            logger.warning(f"  ⚠ {warnings_count} warning(s) - some changes may require manual migration")

        return True

    except Exception as e:
        logger.error(f"  ✗ Migration failed: {e}")
        return False


def verify_schema(engine, logger: logging.Logger) -> bool:
    """
    Verify that all expected tables exist in the database.

    Args:
        engine: SQLAlchemy engine.
        logger: Logger instance.

    Returns:
        True if all tables exist, False otherwise.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    all_present = True
    for table in EXPECTED_TABLES:
        if table in existing_tables:
            logger.info(f"    ✓ Table '{table}' exists")
        else:
            logger.error(f"    ✗ Table '{table}' missing")
            all_present = False

    return all_present


def initialize_database(args: argparse.Namespace, logger: logging.Logger) -> bool:
    """
    Main database initialization logic.

    Performs the following steps (in this order, regardless of CLI argument order):
    1. Create database if it doesn't exist
    2. If --prod: run production migration (add missing columns)
    3. If --force-reset: drop all existing tables
    4. Execute schema.sql to create tables
    5. If --seed: insert test data
    6. Verify schema

    Args:
        args: Parsed command line arguments.
        logger: Logger instance.

    Returns:
        True if initialization successful, False otherwise.
    """
    # Determine total steps based on flags
    prod_mode = getattr(args, 'prod', False)
    total_steps = 4  # Base: check db, schema, seed check, verify
    if prod_mode:
        total_steps += 1
    if args.force_reset:
        total_steps += 1

    current_step = 0

    logger.info("")
    logger.info("=== Database Initialization ===")
    if prod_mode:
        logger.info("    (Production migration mode)")
    logger.info("")

    # Step 1: Create database if not exists
    current_step += 1
    logger.info(f"[{current_step}/{total_steps}] Checking database...")
    try:
        create_database_if_not_exists(
            args.host, args.port, args.user, args.password, args.database, logger
        )
    except Exception as e:
        logger.error(f"  ✗ Failed to check/create database: {e}")
        logger.info("")
        logger.info("  Hint: Make sure PostgreSQL is running and credentials are correct.")
        logger.info(f"  Tried to connect to: {args.host}:{args.port} as '{args.user}'")
        return False

    # Get engine for the target database
    engine = get_engine(args.host, args.port, args.user, args.password, args.database)

    # Step 2: Production migration (runs FIRST if --prod is set)
    if prod_mode:
        current_step += 1
        logger.info("")
        logger.info(f"[{current_step}/{total_steps}] Running production migration...")
        if not migrate_schema(engine, logger):
            logger.error("")
            logger.error("  ✗ Production migration failed!")
            return False

    # Step 3: Force reset if requested (runs AFTER prod migration)
    if args.force_reset:
        current_step += 1
        logger.info("")
        logger.info(f"[{current_step}/{total_steps}] Force reset - dropping all tables...")
        logger.warning("  ⚠ WARNING: All existing data will be deleted!")
        drop_all_tables(engine, logger)
    elif not prod_mode:
        logger.info("")
        logger.info("  Preserving existing data (use --force-reset to drop tables)")

    # Step 4: Execute schema (creates tables, indexes, triggers)
    current_step += 1
    logger.info("")
    logger.info(f"[{current_step}/{total_steps}] Creating/updating tables from schema...")
    if not execute_schema_sql(engine, SCHEMA_FILE, logger):
        return False

    # Step 5: Insert seed data if requested (runs LAST)
    current_step += 1
    logger.info("")
    if args.seed:
        logger.info(f"[{current_step}/{total_steps}] Inserting seed data...")
        if not insert_seed_data(engine, SEED_FILE, logger):
            logger.warning("  ⚠ Seed data insertion had issues, but continuing...")
    else:
        logger.info(f"[{current_step}/{total_steps}] Skipping seed data (use --seed to insert test data)")

    # Verify schema
    logger.info("")
    logger.info("Verifying schema...")
    if not verify_schema(engine, logger):
        logger.error("")
        logger.error("  ✗ Schema verification failed!")
        return False

    logger.info("")
    logger.info("=== Database Initialization Complete ===")
    logger.info("")

    if args.seed:
        logger.info("Test credentials:")
        logger.info("  - test@example.com")
        logger.info("  - admin@example.com")
        logger.info("  - demo@example.com")
        logger.info("")

    return True


def main() -> int:
    """
    Main entry point for database initialization.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    logger = setup_logging()
    args = parse_args()

    try:
        success = initialize_database(args, logger)
        return 0 if success else 1
    except KeyboardInterrupt:
        logger.info("")
        logger.info("Initialization cancelled by user.")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
