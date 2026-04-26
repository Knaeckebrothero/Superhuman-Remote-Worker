#!/usr/bin/env python3
"""Database initialization for the orchestrator.

This module provides database initialization functionality for:
- PostgreSQL App DB (jobs, agents, requirements, datasources, builder)
- PostgreSQL Vector DB (citations, sources, memories, knowledge_index)
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
import hashlib
import logging
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml

# Add project root and orchestrator dir to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
ORCHESTRATOR_DIR = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(ORCHESTRATOR_DIR) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for initialization."""
    if verbose and not os.getenv("DEBUG_ALL"):
        logging.basicConfig(
            level=logging.INFO,
            format="%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        for namespace in ("__main__", "orchestrator", "database"):
            logging.getLogger(namespace).setLevel(logging.DEBUG)
    else:
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
        user = os.getenv("POSTGRES_USER", "srw")
        password = os.getenv("POSTGRES_PASSWORD", "srw_password")
        database = os.getenv("POSTGRES_DB", "srw")
        connection_string = f"postgresql://{user}:{password}@{host}:{port}/{database}"
    return connection_string


def _parse_connection_string(connection_string: str) -> dict:
    """Parse connection string into components for pg_dump/pg_restore."""
    parsed = urlparse(connection_string)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "srw",
        "password": parsed.password or "",
        "database": parsed.path.lstrip("/").split("?")[0] or "srw",
    }


async def ensure_sso_databases() -> bool:
    """Create Keycloak database on the shared PostgreSQL instance.

    For fresh deployments, the compose init script (init_sso_dbs.sh) handles this.
    This function covers existing deployments where the postgres volume already exists.
    Nextcloud uses SQLite (self-contained in its own PVC).

    Connects to the maintenance 'postgres' database to create the SSO databases
    and roles. Idempotent — safe to run repeatedly.
    """
    import psycopg
    from psycopg import sql

    connection_string = get_postgres_connection_string()
    parsed = _parse_connection_string(connection_string)

    # Connect to the 'postgres' maintenance database (not the app database)
    maint_url = f"postgresql://{parsed['user']}:{parsed['password']}@{parsed['host']}:{parsed['port']}/postgres"

    sso_databases = [
        {"name": "keycloak", "user": "keycloak", "password": "keycloak"},
    ]

    try:
        conn = await psycopg.AsyncConnection.connect(maint_url, autocommit=True)
        try:
            for db in sso_databases:
                # Create role if not exists
                result = await conn.execute(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s",
                    (db["user"],),
                )
                if not await result.fetchone():
                    # DDL statements can't use parameterized values — use psycopg.sql
                    await conn.execute(
                        sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                            sql.Identifier(db["user"]),
                            sql.Literal(db["password"]),
                        )
                    )
                    logger.info(f"    Created role: {db['user']}")

                # Create database if not exists
                result = await conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (db["name"],),
                )
                if not await result.fetchone():
                    await conn.execute(
                        sql.SQL("CREATE DATABASE {} OWNER {}").format(
                            sql.Identifier(db["name"]),
                            sql.Identifier(db["user"]),
                        )
                    )
                    logger.info(f"    Created database: {db['name']}")
                else:
                    logger.info(f"    Database exists: {db['name']}")
        finally:
            await conn.close()
        return True
    except Exception as e:
        logger.warning(f"    Could not create SSO databases: {e}")
        logger.warning(
            "    (This is expected if Keycloak/Nextcloud are not being used)"
        )
        return False


async def reset_sso_databases() -> bool:
    """Drop Keycloak database so it re-initializes from config.

    Keycloak imports realm-export.json only on first startup (empty DB).
    Dropping the database forces a clean re-import on next container restart.
    """
    import psycopg
    from psycopg import sql

    connection_string = get_postgres_connection_string()
    parsed = _parse_connection_string(connection_string)
    maint_url = f"postgresql://{parsed['user']}:{parsed['password']}@{parsed['host']}:{parsed['port']}/postgres"

    sso_databases = ["keycloak"]

    try:
        conn = await psycopg.AsyncConnection.connect(maint_url, autocommit=True)
        try:
            for db_name in sso_databases:
                result = await conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (db_name,),
                )
                if await result.fetchone():
                    # Terminate active connections before dropping
                    await conn.execute(
                        sql.SQL(
                            "SELECT pg_terminate_backend(pid) "
                            "FROM pg_stat_activity WHERE datname = {}"
                        ).format(sql.Literal(db_name))
                    )
                    await conn.execute(
                        sql.SQL("DROP DATABASE {}").format(sql.Identifier(db_name))
                    )
                    logger.info(f"    Dropped SSO database: {db_name}")
        finally:
            await conn.close()
        return True
    except Exception as e:
        logger.warning(f"    Could not reset SSO databases: {e}")
        return False


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
    db_name = connection_string.split("/")[-1].split("?")[0]
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

        # Seed default datasources from environment variables
        await _seed_default_datasources(db)

        # Seed system_api_keys from SEED_*_API_KEY env vars (idempotent;
        # mirrors the helm seeder Job)
        await _seed_llm_keys_from_env(db)

        # Seed the codex-proxy system endpoint when CODEX_PROXY_URL is set
        # (idempotent; matched by well-known label)
        await _seed_codex_proxy_endpoint(db)

        # Seed the models catalog from config/models.yaml (idempotent —
        # admin edits via Cockpit are never clobbered)
        await _seed_models_from_yaml(db)

        # One-shot migration: promote system-scoped user_llm_endpoint_models
        # rows to the catalog and drop the legacy table. No-op once the
        # table is gone.
        await _migrate_endpoint_models_to_catalog(db)

        # When OpenRouter is the seeded gateway, insert convenience catalog
        # rows for the auxiliary + embedding routes that would otherwise be
        # empty (idempotent; resolver picks first-enabled-alphabetical at
        # call time)
        await _apply_openrouter_defaults(db)

        # Backfill Nextcloud cloud folders for existing projects
        await _backfill_cloud_folders(db)

        # Seed default projects for users without one
        # (handles JIT-provisioned users who lack a default project)
        await _seed_default_projects(db)

        # Enforce NOT NULL constraint on default_project_id
        # (applied after seeding so existing users have projects first)
        await _enforce_default_project_constraint(db)

        # Migrate orphan jobs to default projects
        await _migrate_orphan_jobs(db)

        # Seed admin MCP token (after users and projects are set up)
        await _seed_admin_mcp_token(db)

        return all_exist

    except Exception as e:
        logger.error(f"  PostgreSQL initialization failed: {e}")
        return False

    finally:
        await db.close()


# =============================================================================
# Vector DB Initialization
# =============================================================================


def get_vector_connection_string() -> Optional[str]:
    """Get Vector DB connection string from environment.

    Returns None if VECTOR_DB_URL is not set (single-DB mode).
    """
    return os.getenv("VECTOR_DB_URL")


async def init_vector_db(force_reset: bool = False) -> bool:
    """Initialize the Vector DB (citations, memories + knowledge_index).

    Requires VECTOR_DB_URL to be set.

    Args:
        force_reset: If True, drop all tables and recreate.

    Returns:
        True if successful, False on error.
    """
    vector_url = get_vector_connection_string()
    if not vector_url:
        logger.warning("  VECTOR_DB_URL not set — vector DB will not be initialized")
        return False

    try:
        from orchestrator.database.postgres import PostgresDB, VECTOR_REQUIRED_TABLES
    except ImportError as e:
        logger.error(f"  Could not import PostgresDB: {e}")
        return False

    db_name = vector_url.split("/")[-1].split("?")[0]
    logger.info(f"  Database: {db_name}")

    db = PostgresDB(vector_url)

    try:
        # Create database if it doesn't exist
        try:
            created = await db.create_database_if_not_exists()
            if created:
                logger.info(f"  Created database: {db_name}")
        except Exception as e:
            logger.warning(f"  Could not check/create database: {e}")

        await db.connect()

        if force_reset:
            logger.info("  Resetting vector schema (dropping all tables)...")
            async with db.acquire() as conn:
                await conn.execute("DROP TABLE IF EXISTS source_embeddings CASCADE")
                await conn.execute("DROP TABLE IF EXISTS source_tags CASCADE")
                await conn.execute("DROP TABLE IF EXISTS source_annotations CASCADE")
                await conn.execute("DROP TABLE IF EXISTS citations CASCADE")
                await conn.execute("DROP TABLE IF EXISTS job_sources CASCADE")
                await conn.execute("DROP TABLE IF EXISTS sources CASCADE")
                await conn.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
                await conn.execute(
                    "DROP TABLE IF EXISTS memory_retrieval_messages CASCADE"
                )
                await conn.execute("DROP TABLE IF EXISTS memories CASCADE")
                await conn.execute("DROP TABLE IF EXISTS knowledge_index CASCADE")
                await conn.execute(
                    "DROP FUNCTION IF EXISTS memory_hybrid_search CASCADE"
                )
                await conn.execute(
                    "DROP FUNCTION IF EXISTS memory_project_hybrid_search CASCADE"
                )
                await conn.execute(
                    "DROP FUNCTION IF EXISTS knowledge_hybrid_search CASCADE"
                )
                await conn.execute("DROP TYPE IF EXISTS source_type CASCADE")
                await conn.execute("DROP TYPE IF EXISTS confidence_level CASCADE")
                await conn.execute("DROP TYPE IF EXISTS extraction_method CASCADE")
                await conn.execute("DROP TYPE IF EXISTS verification_status CASCADE")

        # Apply vector schema
        vector_schema_file = Path(__file__).parent / "database" / "vector_schema.sql"
        if vector_schema_file.exists():
            schema_sql = vector_schema_file.read_text()
            async with db.acquire() as conn:
                await conn.execute(schema_sql)
            logger.info("  Applied vector_schema.sql")
        else:
            logger.error(f"  vector_schema.sql not found at {vector_schema_file}")
            return False

        # Verify tables
        logger.info("  Verifying vector tables:")
        async with db.acquire() as conn:
            for table in VECTOR_REQUIRED_TABLES:
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = $1)",
                    table,
                )
                status = "ok" if exists else "MISSING"
                logger.info(f"    {table}: {status}")
                if not exists:
                    return False

        return True

    except Exception as e:
        logger.error(f"  Vector DB initialization failed: {e}")
        return False

    finally:
        await db.close()


async def verify_vector_db() -> dict:
    """Verify Vector DB connectivity and tables.

    Returns:
        Dict with 'configured', 'connected', 'tables', and any error info.
    """
    vector_url = get_vector_connection_string()
    if not vector_url:
        return {"configured": False}

    try:
        from orchestrator.database.postgres import PostgresDB, VECTOR_REQUIRED_TABLES
    except ImportError:
        return {
            "configured": True,
            "connected": False,
            "error": "asyncpg not installed",
        }

    db = PostgresDB(vector_url)

    try:
        await db.connect()
        tables = {}
        async with db.acquire() as conn:
            for table in VECTOR_REQUIRED_TABLES:
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = $1)",
                    table,
                )
                tables[table] = exists
        return {
            "configured": True,
            "connected": True,
            "tables": tables,
            "all_tables_exist": all(tables.values()),
        }
    except Exception as e:
        return {"configured": True, "connected": False, "error": str(e)}
    finally:
        if db.is_connected:
            await db.close()


def backup_vector_db(backup_file: Path) -> bool:
    """Backup Vector DB using pg_dump.

    Args:
        backup_file: Path to write the backup file.

    Returns:
        True if successful, False otherwise.
    """
    vector_url = get_vector_connection_string()
    if not vector_url:
        logger.info("  Vector DB not configured (VECTOR_DB_URL not set)")
        return False

    params = _parse_connection_string(vector_url)

    cmd = [
        "pg_dump",
        "-h",
        params["host"],
        "-p",
        params["port"],
        "-U",
        params["user"],
        "-d",
        params["database"],
        "-F",
        "c",  # Custom format (compressed)
        "-f",
        str(backup_file),
    ]

    env = os.environ.copy()
    env["PGPASSWORD"] = params["password"]

    logger.info(f"  Running pg_dump for vector database: {params['database']}")

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


def restore_vector_db(backup_file: Path) -> bool:
    """Restore Vector DB from pg_dump backup.

    Args:
        backup_file: Path to the backup file.

    Returns:
        True if successful, False otherwise.
    """
    if not backup_file.exists():
        logger.error(f"  Backup file not found: {backup_file}")
        return False

    vector_url = get_vector_connection_string()
    if not vector_url:
        logger.info("  Vector DB not configured (VECTOR_DB_URL not set)")
        return False

    params = _parse_connection_string(vector_url)

    env = os.environ.copy()
    env["PGPASSWORD"] = params["password"]

    cmd = [
        "pg_restore",
        "-h",
        params["host"],
        "-p",
        params["port"],
        "-U",
        params["user"],
        "-d",
        params["database"],
        "--clean",
        "--if-exists",
        str(backup_file),
    ]

    logger.info(f"  Running pg_restore for vector database: {params['database']}")

    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0 and "error" in result.stderr.lower():
            logger.warning(f"  pg_restore warnings: {result.stderr[:200]}")
        logger.info(f"  Restore completed from: {backup_file}")
        return True
    except FileNotFoundError:
        logger.error("  pg_restore not found. Is PostgreSQL client installed?")
        return False


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
        "-h",
        params["host"],
        "-p",
        params["port"],
        "-U",
        params["user"],
        "-d",
        params["database"],
        "-F",
        "c",  # Custom format (compressed)
        "-f",
        str(backup_file),
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
        "-h",
        params["host"],
        "-p",
        params["port"],
        "-U",
        params["user"],
        "-d",
        params["database"],
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
# Default Datasource Seeding
# =============================================================================


async def _seed_default_datasources(db) -> None:
    """Seed global datasources from DEFAULT_DS_* environment variables.

    Creates or updates global datasources (job_id=NULL) based on env vars.
    See docs/datasources.md for the full design.

    Supported env var patterns:
        DEFAULT_DS_POSTGRESQL_URL, DEFAULT_DS_POSTGRESQL_NAME, DEFAULT_DS_POSTGRESQL_READ_ONLY
        DEFAULT_DS_NEO4J_URL, DEFAULT_DS_NEO4J_USERNAME, DEFAULT_DS_NEO4J_PASSWORD, DEFAULT_DS_NEO4J_NAME, DEFAULT_DS_NEO4J_READ_ONLY
        DEFAULT_DS_MONGODB_URL, DEFAULT_DS_MONGODB_NAME, DEFAULT_DS_MONGODB_READ_ONLY
    """
    seeded = 0

    # PostgreSQL datasource
    pg_url = os.getenv("DEFAULT_DS_POSTGRESQL_URL")
    if pg_url:
        name = os.getenv("DEFAULT_DS_POSTGRESQL_NAME", "Default PostgreSQL")
        await db.upsert_default_datasource(
            name=name,
            ds_type="postgresql",
            connection_url=pg_url,
        )
        logger.info(f"    Seeded default datasource: postgresql ({name})")
        seeded += 1

    # Neo4j datasource
    neo4j_url = os.getenv("DEFAULT_DS_NEO4J_URL")
    if neo4j_url:
        name = os.getenv("DEFAULT_DS_NEO4J_NAME", "Default Neo4j")
        credentials = {}
        username = os.getenv("DEFAULT_DS_NEO4J_USERNAME")
        password = os.getenv("DEFAULT_DS_NEO4J_PASSWORD")
        if username:
            credentials["username"] = username
        if password:
            credentials["password"] = password
        await db.upsert_default_datasource(
            name=name,
            ds_type="neo4j",
            connection_url=neo4j_url,
            credentials=credentials if credentials else None,
        )
        logger.info(f"    Seeded default datasource: neo4j ({name})")
        seeded += 1

    # MongoDB datasource
    mongo_url = os.getenv("DEFAULT_DS_MONGODB_URL")
    if mongo_url:
        name = os.getenv("DEFAULT_DS_MONGODB_NAME", "Default MongoDB")
        await db.upsert_default_datasource(
            name=name,
            ds_type="mongodb",
            connection_url=mongo_url,
        )
        logger.info(f"    Seeded default datasource: mongodb ({name})")
        seeded += 1

    # WebDAV datasource (Nextcloud or any WebDAV server)
    webdav_url = os.getenv("DEFAULT_DS_WEBDAV_URL")
    if webdav_url:
        name = os.getenv("DEFAULT_DS_WEBDAV_NAME", "Default WebDAV")
        credentials = {}
        username = os.getenv("DEFAULT_DS_WEBDAV_USERNAME")
        password = os.getenv("DEFAULT_DS_WEBDAV_PASSWORD")
        if username:
            credentials["username"] = username
        if password:
            credentials["password"] = password
        await db.upsert_default_datasource(
            name=name,
            ds_type="webdav",
            connection_url=webdav_url,
            credentials=credentials if credentials else None,
        )
        logger.info(f"    Seeded default datasource: webdav ({name})")
        seeded += 1

    if seeded > 0:
        logger.info(f"  Seeded {seeded} default datasource(s)")
    else:
        logger.info(
            "  No default datasources configured (DEFAULT_DS_* env vars not set)"
        )


# =============================================================================
# LLM API Key Seeding (mirrors helm/templates/orchestrator/llm-seed-job.yaml)
# =============================================================================


# Providers seeded from SEED_*_API_KEY env vars. Stays in sync with
# orchestrator.main.VALID_SYSTEM_API_KEY_PROVIDERS minus the legacy
# "vision" slot (vision keys ride along on a per-endpoint inline api_key
# now). Inlined here rather than imported from main.py because importing
# main.py at runtime triggers a fresh ``load_dotenv()`` that would undo
# any test-time ``monkeypatch.delenv`` calls.
_SEEDABLE_PROVIDERS = (
    "openai",
    "anthropic",
    "google",
    "groq",
    "openrouter",
    "tavily",
)


async def _seed_llm_keys_from_env(db) -> None:
    """Seed system_api_keys from SEED_<PROVIDER>_API_KEY env vars on first boot.

    Mirrors the helm seeder Job so local dev with ``python init.py`` and a
    ``.env`` file gets the same insert-only behavior. After first run,
    rotating keys via Admin → Providers is never clobbered — re-runs report
    all entries as "skipped" and exit clean.

    Env var shape: ``SEED_OPENAI_API_KEY``, ``SEED_ANTHROPIC_API_KEY``, …
    one per provider in ``_SEEDABLE_PROVIDERS``.
    """
    try:
        from orchestrator.seed.llm_config import seed as llm_seed
    except ImportError as e:
        logger.warning(f"  Could not import seed.llm_config: {e}")
        return

    entries = []
    for provider in _SEEDABLE_PROVIDERS:
        env_name = f"SEED_{provider.upper()}_API_KEY"
        if os.environ.get(env_name):
            entries.append(
                {
                    "provider": provider,
                    "apiKeyEnv": env_name,
                    "label": f"Seeded from {env_name} at init.",
                }
            )

    if not entries:
        logger.info("  No SEED_*_API_KEY env vars set — skipping LLM key seed")
        return

    report = await llm_seed(db, {"systemApiKeys": entries})
    if report.api_keys_seeded:
        logger.info(f"  Seeded system_api_keys: {', '.join(report.api_keys_seeded)}")
    if report.api_keys_skipped:
        logger.info(
            f"  Skipped existing system_api_keys: {', '.join(report.api_keys_skipped)}"
        )


CODEX_PROXY_ENDPOINT_LABEL = "codex-proxy"


async def _seed_codex_proxy_endpoint(db) -> None:
    """Seed a system-scoped llm_endpoints row for the codex proxy.

    The proxy is an OpenAI-compatible front for ChatGPT subscriptions
    (CLIProxyAPI). When CODEX_PROXY_URL is set in the orchestrator's env,
    we insert one row with the well-known label ``codex-proxy`` so admins
    can attach catalog models to it via Admin → Models.

    Idempotent: re-runs short-circuit on label match. Admins who delete the
    row will not see it recreated unless they wipe the table — matching the
    existing seed-once contract.
    """
    proxy_url = os.environ.get("CODEX_PROXY_URL")
    if not proxy_url:
        logger.info(
            "  CODEX_PROXY_URL not set — skipping codex-proxy endpoint seed"
        )
        return

    try:
        from orchestrator.seed.llm_config import seed as llm_seed
    except ImportError as e:
        logger.warning(f"  Could not import seed.llm_config: {e}")
        return

    # The proxy enforces Bearer auth on /v1/* (same model as /v0/management/*).
    # apiKeyEnv defers secret resolution to seed time so the key never sits
    # in this process between read and DB encryption.
    base_url = proxy_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"

    payload = {
        "systemEndpoints": [
            {
                "label": CODEX_PROXY_ENDPOINT_LABEL,
                "baseUrl": base_url,
                "apiKeyEnv": "CODEX_MANAGEMENT_KEY",
                "models": [],
            }
        ]
    }
    report = await llm_seed(db, payload)
    if CODEX_PROXY_ENDPOINT_LABEL in report.endpoints_seeded:
        logger.info(
            f"  Seeded codex-proxy endpoint at {base_url}"
        )
    elif CODEX_PROXY_ENDPOINT_LABEL in report.endpoints_skipped:
        logger.info(
            "  codex-proxy endpoint already present — leaving untouched"
        )


_YAML_GROUP_ROLE = {
    "auxiliary_models": "auxiliary",
    "vision_models": "vision",
    "embedding_models": "embedding",
}


def _infer_provider_from_model_id(model_id: str) -> str | None:
    """Best-effort provider inference for catalog seed rows lacking an explicit
    ``provider`` field. Mirrors the prefix logic in
    ``orchestrator.main._create_builder_llm``.
    """
    name = model_id.lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("openrouter/"):
        return "openrouter"
    if name.startswith("groq/"):
        return "groq"
    if name.startswith("codex/"):
        return "codex"
    if name.startswith("gemini") or name.startswith("google/"):
        return "google"
    if name.startswith("gpt-") or name.startswith("text-embedding-"):
        return "openai"
    return None


async def _seed_models_from_yaml(db) -> None:
    """Seed the ``models`` catalog from ``config/models.yaml`` on first boot.

    For each entry in the YAML groups + the auxiliary/vision/embedding helper
    lists, construct one catalog row per (model, role) and insert with
    ``ON CONFLICT DO NOTHING`` so admin edits made via the Cockpit are never
    clobbered by subsequent boots.

    Rows whose inferred provider has no ``system_api_keys`` entry yet are
    skipped — they would not be reachable. The ``local`` provider (vLLM via
    a static endpoint) is also skipped: those models surface through the
    ``llm_endpoints`` route, not as system rows.
    """
    catalog_path = Path(__file__).resolve().parent.parent / "config" / "models.yaml"
    if not catalog_path.exists():
        logger.info(f"  Catalog seed skipped — {catalog_path} not found")
        return

    try:
        raw = yaml.safe_load(catalog_path.read_text()) or {}
    except yaml.YAMLError as e:
        logger.warning(f"  Could not parse {catalog_path}: {e}")
        return

    seeded_keys = {k["provider"] for k in await db.list_system_api_keys()}

    rows: list[dict] = []
    skipped_provider_counts: dict[str, int] = {}

    def _enqueue(
        provider: str | None, model_id: str, display: str, role: str, family: str | None
    ) -> None:
        if not provider or provider == "local":
            return
        if provider not in seeded_keys:
            skipped_provider_counts[provider] = (
                skipped_provider_counts.get(provider, 0) + 1
            )
            return
        from src.core.model_registry import family_of  # local import: avoids
        # importing src/* during early startup before sys.path is set up

        rows.append(
            {
                "provider_kind": "system",
                "provider_ref": provider,
                "model_id": model_id,
                "display_label": display or model_id,
                "role": role,
                "family": family or family_of(model_id),
                "seeded_from": "config/models.yaml",
            }
        )

    for group in raw.get("groups", []):
        provider = group.get("provider")
        for entry in group.get("models", []):
            _enqueue(
                provider,
                entry["id"],
                entry.get("display_name") or entry["id"],
                "chat",
                entry.get("family"),
            )

    for yaml_key, role in _YAML_GROUP_ROLE.items():
        for entry in raw.get(yaml_key, []):
            provider = entry.get("provider") or _infer_provider_from_model_id(
                entry["id"]
            )
            _enqueue(
                provider,
                entry["id"],
                entry.get("label") or entry["id"],
                role,
                entry.get("family"),
            )

    inserted = 0
    for row in rows:
        result = await db.create_model(**row, on_conflict_do_nothing=True)
        if result is not None:
            inserted += 1

    if inserted:
        logger.info(f"  Seeded {inserted} catalog rows from config/models.yaml")
    else:
        logger.info("  No new catalog rows from config/models.yaml (all present)")
    for provider, count in sorted(skipped_provider_counts.items()):
        logger.info(
            f"  Skipped {count} {provider} catalog rows — no system_api_keys row yet"
        )


async def _migrate_endpoint_models_to_catalog(db) -> None:
    """One-shot migration: promote system-scoped ``user_llm_endpoint_models``
    rows into the catalog and drop the legacy table.

    Idempotent — bails out cleanly when the table no longer exists. User-
    scoped rows (``llm_endpoints.user_id IS NOT NULL``) are *not*
    migrated; the dual-scope complexity isn't worth carrying forward, and
    user-side authoring was unused in practice (per design doc §Decisions).
    """
    async with db.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT to_regclass('public.user_llm_endpoint_models') IS NOT NULL"
        )
        if not exists:
            return

        rows = await conn.fetch(
            """
            SELECT m.endpoint_id, m.model_id, m.display_name, m.family,
                   m.context_window, m.reasoning_level, m.enabled, m.capability
              FROM user_llm_endpoint_models m
              JOIN llm_endpoints e ON e.id = m.endpoint_id
             WHERE e.user_id IS NULL
            """
        )

    promoted = 0
    skipped_unsupported_role = 0
    for row in rows:
        capability = (row["capability"] or "chat").lower()
        if capability not in (
            "chat",
            "auxiliary",
            "embedding",
            "vision",
            "whisper",
            "tts",
        ):
            skipped_unsupported_role += 1
            continue
        from src.core.model_registry import family_of  # local import: src/*
        # path may not be on sys.path until runtime is fully wired

        result = await db.create_model(
            provider_kind="endpoint",
            provider_ref=str(row["endpoint_id"]),
            model_id=row["model_id"],
            display_label=row["display_name"],
            role=capability,
            family=row["family"] or family_of(row["model_id"]),
            context_window=row["context_window"],
            reasoning_level=row["reasoning_level"],
            enabled=bool(row["enabled"]),
            seeded_from="migration:user_llm_endpoint_models",
            on_conflict_do_nothing=True,
        )
        if result is not None:
            promoted += 1

    async with db.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS user_llm_endpoint_models CASCADE")

    logger.info(
        "  Migrated user_llm_endpoint_models → models: %d promoted, "
        "%d skipped (unsupported v1 role); table dropped",
        promoted,
        skipped_unsupported_role,
    )


async def _apply_openrouter_defaults(db) -> None:
    """When OpenRouter is the seeded gateway, insert convenience catalog rows
    for the OpenRouter-routed auxiliary and embedding models.

    Rationale: OpenRouter is a single-key gateway for many provider stacks.
    When an admin only seeds an OpenRouter key, the auxiliary and embedding
    slots would otherwise be empty and dependent features (memory
    extraction, recall search) would fail at first use. Inserting these
    rows ensures the catalog has at least one entry per dependent role.

    The default-model resolver's "first-enabled-alphabetical" fallback then
    handles which one gets used. Admin edits/disables survive subsequent
    boots via ``ON CONFLICT DO NOTHING``.
    """
    openrouter_key = await db.get_system_api_key("openrouter")
    if not openrouter_key:
        return

    convenience_rows = [
        {
            "provider_kind": "system",
            "provider_ref": "openrouter",
            "model_id": "openrouter/google/gemini-2.5-flash",
            "display_label": "Gemini 2.5 Flash (OpenRouter)",
            "role": "auxiliary",
            "family": "gemini",
            "seeded_from": "helm:openrouter-defaults",
        },
        {
            "provider_kind": "system",
            "provider_ref": "openrouter",
            "model_id": "openrouter/openai/text-embedding-3-large",
            "display_label": "text-embedding-3-large (OpenRouter)",
            "role": "embedding",
            "family": "default",
            "seeded_from": "helm:openrouter-defaults",
        },
    ]

    inserted = 0
    for row in convenience_rows:
        result = await db.create_model(**row, on_conflict_do_nothing=True)
        if result is not None:
            inserted += 1
            logger.info(
                f"  Inserted openrouter convenience row: {row['model_id']} ({row['role']})"
            )
    if not inserted:
        logger.info("  OpenRouter convenience catalog rows already present — no-op")


# =============================================================================
# Cloud Folder Backfill
# =============================================================================


async def _backfill_cloud_folders(db) -> None:
    """Create main-cloud project folders for projects that predate the feature.

    Idempotent — skips projects whose main_cloud_folder_handle (or the legacy
    nextcloud_folder_id) is already populated.
    """
    from services.cloud import build_backend

    backend = build_backend()
    if not await backend.ensure_initialized():
        logger.info("  Cloud folder backfill skipped — main cloud not available")
        return

    backfilled = 0
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name FROM projects
            WHERE main_cloud_folder_handle IS NULL
              AND nextcloud_folder_id IS NULL
              AND is_default = FALSE
              AND status = 'active'
            """
        )

    for row in rows:
        project_id = str(row["id"])
        project_name = row["name"]
        group_name = f"project-{project_id}"

        try:
            await backend.ensure_group(group_name)
            folder_handle = await backend.ensure_project_folder(
                project_name=project_name,
                group_id=group_name,
            )
            if folder_handle is not None:
                legacy_folder_id: int | None = None
                if backend.backend_id == "nextcloud":
                    try:
                        legacy_folder_id = int(folder_handle.native_id)
                    except ValueError:
                        legacy_folder_id = None
                await db.update_project(
                    project_id,
                    main_cloud_backend=backend.backend_id,
                    main_cloud_folder_handle=folder_handle.to_db(),
                    nextcloud_folder_id=legacy_folder_id,
                )

                # Create a project-scoped WebDAV datasource if none exists
                existing_ds = await db.list_project_datasources(project_id)
                has_webdav = any(ds["type"] == "webdav" for ds in existing_ds)
                if not has_webdav:
                    webdav_url = backend.get_project_folder_webdav_url(folder_handle)
                    if webdav_url:
                        ds = await db.create_datasource(
                            name=f"Cloud Storage ({project_name})",
                            ds_type="webdav",
                            connection_url=webdav_url,
                            description=(
                                f"Shared file storage for project '{project_name}'"
                            ),
                            credentials=backend.webdav_credentials,
                        )
                        await db.link_datasource_to_project(
                            project_id=project_id,
                            datasource_id=str(ds["id"]),
                            read_only=False,
                        )

                backfilled += 1
                logger.info(
                    f"    Backfilled cloud folder for project '{project_name}' "
                    f"(handle={folder_handle.native_id})"
                )
        except Exception as e:
            logger.warning(
                f"    Failed to backfill cloud folder for project '{project_name}': {e}"
            )

    await backend.close()

    if backfilled > 0:
        logger.info(f"  Backfilled {backfilled} project cloud folder(s)")
    else:
        logger.info("  No projects need cloud folder backfill")


# =============================================================================
# Default Project Seeding
# =============================================================================


async def _seed_default_projects(db) -> None:
    """Create default projects for users that don't have one.

    Queries users where default_project_id IS NULL, then creates a personal
    default project for each with owner membership. No Gitea access here —
    jobs repos are created lazily by the orchestrator at startup.
    """
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, display_name FROM users WHERE default_project_id IS NULL"
            )

        if not rows:
            logger.info("  All users have default projects")
            return

        seeded = 0
        for row in rows:
            try:
                await db.create_default_project_for_user(
                    str(row["id"]), row["display_name"]
                )
                seeded += 1
            except Exception as e:
                logger.warning(
                    f"  Failed to create default project for user {row['display_name']}: {e}"
                )

        if seeded > 0:
            logger.info(f"  Seeded {seeded} default project(s)")

    except Exception as e:
        logger.warning(f"  Default project seeding failed: {e}")


async def _enforce_default_project_constraint(db) -> None:
    """Add CHECK constraint ensuring every user has a default project.

    Uses ADD CONSTRAINT ... CHECK (default_project_id IS NOT NULL).
    Idempotent — skips if the constraint already exists.
    Must run after _seed_default_projects to avoid constraint violations.
    """
    try:
        async with db.acquire() as conn:
            await conn.execute("""
                DO $$ BEGIN
                    ALTER TABLE users ADD CONSTRAINT users_default_project_required
                        CHECK (default_project_id IS NOT NULL);
                EXCEPTION WHEN duplicate_object THEN null;
                END $$;
            """)
        logger.info("  Default project constraint enforced on users table")
    except Exception as e:
        logger.warning(f"  Failed to enforce default project constraint: {e}")


async def _seed_admin_mcp_token(db) -> None:
    """Generate a root MCP token for the admin user if none exists.

    When MCP_DEV_TOKEN is set in the environment, uses that fixed value
    instead of generating a random token. This keeps the token stable
    across database resets so .mcp.json never goes stale during local
    development.

    Env vars:
        MCP_DEV_TOKEN: Fixed token value (must start with "srw_").
                       When set, this exact token is inserted on every init.
                       When unset, a random token is generated once.
    """
    try:
        admin = await db.get_admin_user()
        if not admin:
            return

        dev_token = os.environ.get("MCP_DEV_TOKEN", "").strip()

        # Check if admin already has an MCP token
        existing = await db.list_mcp_tokens(str(admin["id"]))
        if existing:
            if dev_token:
                # Dev token configured — ensure it matches what's in the DB.
                # If the hash differs (e.g. env var changed), replace it.
                expected_hash = hashlib.sha256(dev_token.encode()).hexdigest()
                current_hash_matches = False
                for tok in existing:
                    stored = await db.get_mcp_token_by_hash(expected_hash)
                    if stored:
                        current_hash_matches = True
                        break
                if current_hash_matches:
                    logger.info("  Admin MCP dev token already exists and matches")
                    return
                # Hash mismatch — revoke old tokens and insert the new dev token
                for tok in existing:
                    await db.revoke_mcp_token(str(tok["id"]), str(admin["id"]))
                logger.info("  Replaced admin MCP token with updated dev token")
            else:
                logger.info("  Admin MCP token already exists")
                return

        # Use fixed dev token or generate a random one
        if dev_token:
            if not dev_token.startswith("srw_"):
                logger.warning("  MCP_DEV_TOKEN must start with 'srw_' — ignoring")
                return
            token = dev_token
        else:
            token = "srw_" + secrets.token_urlsafe(32)

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        token_prefix = token[:12]

        await db.create_mcp_token(
            user_id=str(admin["id"]),
            name="Root (auto-generated)",
            token_hash=token_hash,
            token_prefix=token_prefix,
            scope="all",
        )

        if dev_token:
            logger.info("  Admin MCP token seeded from MCP_DEV_TOKEN (scope: all)")
        else:
            logger.info("  ============================================")
            logger.info("  Admin MCP Token (scope: all):")
            logger.info(f"  {token}")
            logger.info("  Save this token — it will not be shown again.")
            logger.info("  ============================================")
    except Exception as e:
        logger.warning(f"  Admin MCP token seeding failed: {e}")


async def _migrate_orphan_jobs(db) -> None:
    """Assign orphan jobs (project_id IS NULL) to their user's default project.

    For jobs with a user_id, uses the user's default_project_id.
    For jobs without a user_id, assigns to the first available default project
    as a fallback.
    """
    try:
        async with db.acquire() as conn:
            # Jobs with a user_id: assign to that user's default project
            result = await conn.execute("""
                UPDATE jobs j
                SET project_id = u.default_project_id
                FROM users u
                WHERE j.user_id = u.id
                  AND j.project_id IS NULL
                  AND u.default_project_id IS NOT NULL
            """)
            user_count = int(result.split()[-1]) if result else 0

            # Jobs without user_id: assign to first default project as fallback
            fallback_count = 0
            orphan_count = await conn.fetchval(
                "SELECT COUNT(*) FROM jobs WHERE project_id IS NULL"
            )
            if orphan_count and orphan_count > 0:
                fallback_project = await conn.fetchval(
                    "SELECT id FROM projects WHERE is_default = TRUE LIMIT 1"
                )
                if fallback_project:
                    result = await conn.execute(
                        "UPDATE jobs SET project_id = $1 WHERE project_id IS NULL",
                        fallback_project,
                    )
                    fallback_count = int(result.split()[-1]) if result else 0

        total = user_count + fallback_count
        if total > 0:
            logger.info(
                f"  Migrated {total} orphan job(s) to default projects "
                f"({user_count} by user, {fallback_count} fallback)"
            )
        else:
            logger.info("  No orphan jobs to migrate")

    except Exception as e:
        logger.warning(f"  Orphan job migration failed: {e}")


# =============================================================================
# Neo4j Initialization (System Knowledge Base)
# =============================================================================

NEO4J_SCHEMA_QUERIES = [
    # Unique constraint: note slug per project
    "CREATE CONSTRAINT note_id_unique IF NOT EXISTS FOR (n:Note) REQUIRE (n.project_id, n.id) IS UNIQUE",
    # Indexes for common lookups
    "CREATE INDEX note_project IF NOT EXISTS FOR (n:Note) ON (n.project_id)",
    "CREATE INDEX note_type IF NOT EXISTS FOR (n:Note) ON (n.project_id, n.type)",
    "CREATE INDEX note_status IF NOT EXISTS FOR (n:Note) ON (n.project_id, n.status)",
    "CREATE INDEX tag_project IF NOT EXISTS FOR (t:Tag) ON (t.project_id)",
    "CREATE INDEX keyword_project IF NOT EXISTS FOR (k:Keyword) ON (k.project_id)",
]


def get_neo4j_config() -> Optional[dict]:
    """Get system Neo4j connection config from environment.

    Returns:
        Dict with uri, username, password if configured, else None.
    """
    uri = os.getenv("NEO4J_URL")
    if not uri:
        return None
    return {
        "uri": uri,
        "username": os.getenv("NEO4J_USERNAME", "neo4j"),
        "password": os.getenv("NEO4J_PASSWORD", ""),
    }


def init_neo4j(force_reset: bool = False) -> bool:
    """Initialize Neo4j schema for the knowledge base.

    Creates constraints and indexes for Note, Tag, and Keyword nodes.
    Idempotent — all statements use IF NOT EXISTS.

    Args:
        force_reset: If True, delete all knowledge nodes before recreating schema.

    Returns:
        True if successful or Neo4j not configured, False on error.
    """
    config = get_neo4j_config()
    if not config:
        logger.info("  Neo4j not configured (NEO4J_URL not set)")
        logger.info("  Skipping Neo4j initialization (optional for knowledge base)")
        return True

    try:
        from src.database.neo4j_db import Neo4jDB
    except ImportError as e:
        logger.warning(f"  Could not import Neo4jDB: {e}")
        return True  # Non-fatal — KB is optional

    db = Neo4jDB(**config)
    if not db.connect():
        logger.warning("  Could not connect to Neo4j — knowledge base unavailable")
        return True  # Non-fatal

    try:
        if force_reset:
            logger.info("  Resetting Neo4j knowledge data...")
            db.execute_write("MATCH (n:Note) DETACH DELETE n")
            db.execute_write("MATCH (t:Tag) DETACH DELETE t")
            db.execute_write("MATCH (k:Keyword) DETACH DELETE k")
            logger.info("  Cleared all knowledge nodes")

        logger.info("  Applying Neo4j schema (constraints + indexes)...")
        for query in NEO4J_SCHEMA_QUERIES:
            try:
                db.execute_write(query)
            except Exception as e:
                # Some Neo4j versions handle IF NOT EXISTS differently
                logger.debug(f"  Schema query note: {e}")

        logger.info("  Neo4j schema applied successfully")
        return True

    except Exception as e:
        logger.warning(f"  Neo4j initialization error: {e}")
        return True  # Non-fatal

    finally:
        db.close()


def verify_neo4j() -> dict:
    """Verify Neo4j connectivity and schema.

    Returns:
        Dict with connection status and schema info.
    """
    config = get_neo4j_config()
    if not config:
        return {"connected": False, "configured": False}

    try:
        from src.database.neo4j_db import Neo4jDB
    except ImportError:
        return {
            "connected": False,
            "configured": True,
            "error": "neo4j driver not installed",
        }

    db = Neo4jDB(**config)
    if not db.connect():
        return {"connected": False, "configured": True, "error": "connection failed"}

    try:
        schema = db.get_schema()
        return {
            "connected": True,
            "configured": True,
            "node_labels": schema.get("node_labels", []),
            "relationship_types": schema.get("relationship_types", []),
        }
    except Exception as e:
        return {"connected": False, "configured": True, "error": str(e)}
    finally:
        db.close()


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
        "database": parsed.path.lstrip("/").split("?")[0] or "srw_logs",
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
        db_name = mongo_url.split("/")[-1].split("?")[0]
        if not db_name:
            db_name = "srw_logs"

        db = client[db_name]

        # Show current state
        total_docs = sum(db[c].count_documents({}) for c in db.list_collection_names())
        logger.info(f"  Current state: {total_docs} documents")

        # Clear if requested
        if force_reset and total_docs > 0:
            logger.info("  Clearing database...")
            for collection_name in db.list_collection_names():
                result = db[collection_name].delete_many({})
                logger.info(
                    f"    Deleted {result.deleted_count} documents from {collection_name}"
                )

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
        (
            [("job_id", 1), ("agent_type", 1), ("timestamp", -1)],
            {"name": "idx_job_agent_time"},
        ),
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
        (
            [("job_id", 1), ("iteration", 1), ("step_number", 1)],
            {"name": "idx_audit_job_iter_step"},
        ),
        (
            [("job_id", 1), ("agent_type", 1), ("step_type", 1)],
            {"name": "idx_audit_job_agent_type"},
        ),
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

    # chat_history collection
    chat_history = db["chat_history"]
    chat_indexes = [
        ("job_id", {"name": "idx_chat_job_id"}),
        ([("job_id", 1), ("timestamp", 1)], {"name": "idx_chat_job_timestamp"}),
    ]

    logger.info("  Configuring chat_history collection...")
    for index_spec, options in chat_indexes:
        try:
            if isinstance(index_spec, list):
                chat_history.create_index(index_spec, **options)
            else:
                chat_history.create_index(index_spec, **options)
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

        db_name = mongo_url.split("/")[-1].split("?")[0] or "srw_logs"
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
        "--host",
        params["host"],
        "--port",
        params["port"],
        "--db",
        params["database"],
        "--out",
        str(backup_dir),
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
        "--host",
        params["host"],
        "--port",
        params["port"],
        "--db",
        params["database"],
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
# Uploads Directory Management
# =============================================================================


def _resolve_uploads_dir() -> Path:
    """Resolve uploads directory, respecting WORKSPACE_PATH env var."""
    env_path = os.getenv("WORKSPACE_PATH")
    if env_path:
        return Path(env_path) / "uploads"
    return Path(__file__).resolve().parent.parent / "workspace" / "uploads"


UPLOADS_DIR = _resolve_uploads_dir()


def get_uploads_path() -> Path:
    """Get the uploads directory path."""
    return UPLOADS_DIR


def init_uploads() -> bool:
    """Initialize uploads directory."""
    uploads_path = get_uploads_path()
    try:
        uploads_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"  Uploads path: {uploads_path}")
        return True
    except Exception as e:
        logger.error(f"  Failed to initialize uploads: {e}")
        return False


def cleanup_uploads() -> bool:
    """Clean up all uploads."""
    uploads_path = get_uploads_path()
    if not uploads_path.exists():
        return True

    removed = 0
    for item in uploads_path.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed += 1
        except Exception as e:
            logger.warning(f"  Failed to remove {item}: {e}")

    logger.info(f"  Removed {removed} upload(s)")
    return True


def verify_uploads() -> dict:
    """Verify uploads directory and return stats."""
    uploads_path = get_uploads_path()
    result = {
        "path": str(uploads_path),
        "exists": uploads_path.exists(),
        "upload_count": 0,
        "total_files": 0,
        "total_size_bytes": 0,
    }

    if not uploads_path.exists():
        return result

    # Count uploads (directories with metadata.json)
    for upload_dir in uploads_path.iterdir():
        if upload_dir.is_dir() and (upload_dir / "metadata.json").exists():
            result["upload_count"] += 1
            for f in upload_dir.iterdir():
                if f.is_file() and f.name != "metadata.json":
                    result["total_files"] += 1
                    result["total_size_bytes"] += f.stat().st_size

    return result


def backup_uploads(backup_dir: Path) -> bool:
    """Backup uploads directory."""
    uploads_path = get_uploads_path()
    if not uploads_path.exists() or not any(uploads_path.iterdir()):
        logger.info("  No uploads to backup")
        return True

    dest = backup_dir / "uploads"
    try:
        shutil.copytree(uploads_path, dest)
        upload_count = len([d for d in dest.iterdir() if d.is_dir()])
        logger.info(f"  Backed up {upload_count} upload(s)")
        return True
    except Exception as e:
        logger.error(f"  Uploads backup failed: {e}")
        return False


def restore_uploads(backup_dir: Path) -> bool:
    """Restore uploads from backup."""
    src = backup_dir / "uploads"
    if not src.exists():
        logger.info("  No uploads backup found")
        return True

    uploads_path = get_uploads_path()
    try:
        # Clear existing
        if uploads_path.exists():
            shutil.rmtree(uploads_path)

        shutil.copytree(src, uploads_path)
        upload_count = len([d for d in uploads_path.iterdir() if d.is_dir()])
        logger.info(f"  Restored {upload_count} upload(s)")
        return True
    except Exception as e:
        logger.error(f"  Uploads restore failed: {e}")
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

    # Vector DB (memories + knowledge_index) — skip if not configured
    if not skip_postgres:
        logger.info("")
        logger.info("Initializing Vector DB...")
        if not await init_vector_db(force_reset):
            logger.warning(
                "Vector DB initialization had issues (non-fatal in single-DB mode)"
            )

    # Neo4j (system knowledge base) — non-fatal if not configured
    logger.info("")
    logger.info("Initializing Neo4j (knowledge base)...")
    init_neo4j(force_reset)

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
        result["vector_db"] = await verify_vector_db()

    if not skip_mongodb:
        result["mongodb"] = await verify_mongodb()

    result["neo4j"] = verify_neo4j()

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
        "--verbose",
        "-v",
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
                logger.warning(
                    f"PostgreSQL: not connected ({pg.get('error', 'unknown error')})"
                )

        if "mongodb" in result:
            mg = result["mongodb"]
            if not mg.get("configured"):
                logger.info("MongoDB: not configured (optional)")
            elif mg.get("connected"):
                logger.info(
                    f"MongoDB: connected (collections: {mg.get('collections', [])})"
                )
            else:
                logger.warning(
                    f"MongoDB: not connected ({mg.get('error', 'unknown error')})"
                )

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
