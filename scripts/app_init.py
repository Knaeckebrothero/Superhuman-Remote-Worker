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
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

# Seed profiles directory
SEED_PROFILES_DIR = PROJECT_ROOT / "data" / "seed"


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

Backup/Restore commands:
  python scripts/app_init.py --create-backup                  # Create backup with auto-name
  python scripts/app_init.py --create-backup my_backup        # Create backup with custom name
  python scripts/app_init.py --restore-backup backups/20260117_001  # Restore from backup
        """,
    )
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="Delete all data and recreate databases (WARNING: deletes all data)",
    )
    parser.add_argument(
        "--seed",
        nargs="?",
        const="list",
        metavar="PROFILE",
        help="Load seed data from profile (creator, validator). Use --seed without argument to list profiles.",
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
    parser.add_argument(
        "--create-backup",
        nargs="?",
        const=True,
        metavar="NAME",
        help="Create backup of current state (optional: provide name)",
    )
    parser.add_argument(
        "--restore-backup",
        metavar="PATH",
        help="Restore from backup directory",
    )
    return parser.parse_args()


def list_seed_profiles() -> list[dict]:
    """List available seed profiles with their contents."""
    profiles = []
    if not SEED_PROFILES_DIR.exists():
        return profiles

    for profile_dir in sorted(SEED_PROFILES_DIR.iterdir()):
        if profile_dir.is_dir():
            profile = {
                "name": profile_dir.name,
                "path": profile_dir,
                "has_neo4j": (profile_dir / "neo4j.cypher").exists(),
                "has_postgres": (profile_dir / "postgres.sql").exists(),
            }
            profiles.append(profile)
    return profiles


def get_seed_profile(name: str) -> dict | None:
    """Get a specific seed profile by name."""
    profile_dir = SEED_PROFILES_DIR / name
    if not profile_dir.exists():
        return None
    return {
        "name": name,
        "path": profile_dir,
        "has_neo4j": (profile_dir / "neo4j.cypher").exists(),
        "has_postgres": (profile_dir / "postgres.sql").exists(),
    }


def run_postgres_init(logger: logging.Logger, force_reset: bool = False, seed_profile: dict | None = None) -> bool:
    """
    Run PostgreSQL database initialization.

    Args:
        logger: Logger instance.
        force_reset: If True, drop all tables and recreate.
        seed_profile: Optional seed profile dict with postgres.sql path.

    Returns:
        True if successful, False otherwise.
    """
    try:
        from scripts.init_db import initialize_postgres, seed_postgres
        if not initialize_postgres(logger, force_reset):
            return False

        # Load seed data if profile has postgres.sql
        if seed_profile and seed_profile.get("has_postgres"):
            seed_file = seed_profile["path"] / "postgres.sql"
            logger.info(f"  Loading PostgreSQL seed data from {seed_profile['name']}...")
            if not seed_postgres(seed_file, logger):
                logger.warning("  PostgreSQL seeding had issues")

        return True
    except ImportError as e:
        logger.error(f"  Could not import init_db module: {e}")
        return False
    except Exception as e:
        logger.error(f"  Error running PostgreSQL initialization: {e}")
        return False


def run_neo4j_init(logger: logging.Logger, force_reset: bool = False, seed_profile: dict | None = None) -> bool:
    """
    Run Neo4j knowledge graph initialization.

    Args:
        logger: Logger instance.
        force_reset: If True, clear all data and re-seed.
        seed_profile: Optional seed profile dict with neo4j.cypher path.

    Returns:
        True if successful, False otherwise.
    """
    try:
        from scripts.init_neo4j import initialize_neo4j

        # Determine seed file from profile
        seed_file = None
        if seed_profile and seed_profile.get("has_neo4j"):
            seed_file = seed_profile["path"] / "neo4j.cypher"
            logger.info(f"  Using Neo4j seed data from {seed_profile['name']}...")

        return initialize_neo4j(logger, clear=force_reset, seed=seed_profile is not None, seed_file=seed_file)
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


def clean_workspace(logger: logging.Logger) -> bool:
    """
    Clean up workspace directories.

    Removes all contents from WORKSPACE_PATH.

    Returns:
        True if successful, False otherwise.
    """
    import shutil

    # Get workspace base path
    workspace_path = os.getenv("WORKSPACE_PATH")
    if workspace_path:
        base_path = PROJECT_ROOT / workspace_path
    else:
        base_path = PROJECT_ROOT / "workspace"

    if not base_path.exists():
        logger.info(f"  Workspace directory does not exist: {base_path}")
        return True

    # Find all items in workspace
    items = list(base_path.iterdir())
    if not items:
        logger.info(f"  Workspace directory is already empty: {base_path}")
        return True

    removed = 0
    for item in items:
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed += 1
            logger.debug(f"    Removed: {item.name}")
        except Exception as e:
            logger.warning(f"  Failed to remove {item}: {e}")

    logger.info(f"  Removed {removed} item(s) from {base_path}")
    return True


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


def _get_git_commit() -> str:
    """Get current git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:8]
    except Exception:
        pass
    return "unknown"


def _generate_backup_dir_name(name: str | None) -> Path:
    """Generate backup directory name with date and sequence number."""
    backups_dir = PROJECT_ROOT / "backups"
    backups_dir.mkdir(exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d")

    # Find next sequence number for today
    existing = list(backups_dir.glob(f"{date_str}_*"))
    seq_numbers = []
    for d in existing:
        parts = d.name.split("_")
        if len(parts) >= 2:
            try:
                seq_numbers.append(int(parts[1]))
            except ValueError:
                pass

    next_seq = max(seq_numbers, default=0) + 1

    if name and name is not True:
        dir_name = f"{date_str}_{next_seq:03d}_{name}"
    else:
        dir_name = f"{date_str}_{next_seq:03d}"

    return backups_dir / dir_name


def create_backup(logger: logging.Logger, name: str | None = None) -> bool:
    """
    Create a backup of the current application state.

    Args:
        logger: Logger instance.
        name: Optional name to append to backup directory.

    Returns:
        True if successful, False otherwise.
    """
    backup_dir = _generate_backup_dir_name(name)
    backup_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Creating backup: {backup_dir}")
    logger.info("")

    success = True
    backup_info = {
        "timestamp": datetime.now().isoformat(),
        "git_commit": _get_git_commit(),
        "components": {},
    }

    # 1. Backup PostgreSQL
    logger.info("[1/4] Backing up PostgreSQL...")
    try:
        from scripts.init_db import backup_postgres
        postgres_file = backup_dir / "postgres.dump"
        if backup_postgres(postgres_file, logger):
            backup_info["components"]["postgres"] = {"file": "postgres.dump", "success": True}
        else:
            backup_info["components"]["postgres"] = {"success": False}
            success = False
    except ImportError as e:
        logger.warning(f"  Could not import init_db: {e}")
        backup_info["components"]["postgres"] = {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"  PostgreSQL backup failed: {e}")
        backup_info["components"]["postgres"] = {"success": False, "error": str(e)}
        success = False
    logger.info("")

    # 2. Backup Neo4j
    logger.info("[2/4] Backing up Neo4j...")
    try:
        from scripts.init_neo4j import backup_neo4j
        neo4j_file = backup_dir / "neo4j_export.cypher"
        if backup_neo4j(neo4j_file, logger):
            backup_info["components"]["neo4j"] = {"file": "neo4j_export.cypher", "success": True}
        else:
            backup_info["components"]["neo4j"] = {"success": False}
    except ImportError as e:
        logger.warning(f"  Could not import init_neo4j: {e}")
        backup_info["components"]["neo4j"] = {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"  Neo4j backup failed: {e}")
        backup_info["components"]["neo4j"] = {"success": False, "error": str(e)}
    logger.info("")

    # 3. Backup MongoDB
    logger.info("[3/4] Backing up MongoDB...")
    try:
        from scripts.init_mongodb import backup_mongodb
        mongodb_dir = backup_dir / "mongodb"
        mongodb_dir.mkdir(exist_ok=True)
        if backup_mongodb(mongodb_dir, logger):
            backup_info["components"]["mongodb"] = {"directory": "mongodb", "success": True}
        else:
            backup_info["components"]["mongodb"] = {"success": False}
    except ImportError as e:
        logger.info(f"  MongoDB module not available: {e}")
        backup_info["components"]["mongodb"] = {"success": True, "skipped": True}
    except Exception as e:
        logger.warning(f"  MongoDB backup failed: {e}")
        backup_info["components"]["mongodb"] = {"success": False, "error": str(e)}
    logger.info("")

    # 4. Backup workspace
    logger.info("[4/4] Backing up workspace...")
    workspace_path = os.getenv("WORKSPACE_PATH")
    if workspace_path:
        src_workspace = PROJECT_ROOT / workspace_path
    else:
        src_workspace = PROJECT_ROOT / "workspace"

    if src_workspace.exists() and any(src_workspace.iterdir()):
        dest_workspace = backup_dir / "workspace"
        try:
            shutil.copytree(src_workspace, dest_workspace)
            # Count items
            job_count = len(list(dest_workspace.glob("job_*")))
            checkpoint_count = len(list((dest_workspace / "checkpoints").glob("*.db"))) if (dest_workspace / "checkpoints").exists() else 0
            logger.info(f"  Copied workspace: {job_count} jobs, {checkpoint_count} checkpoints")
            backup_info["components"]["workspace"] = {
                "directory": "workspace",
                "success": True,
                "jobs": job_count,
                "checkpoints": checkpoint_count,
            }
        except Exception as e:
            logger.warning(f"  Workspace backup failed: {e}")
            backup_info["components"]["workspace"] = {"success": False, "error": str(e)}
    else:
        logger.info("  Workspace is empty or does not exist")
        backup_info["components"]["workspace"] = {"success": True, "empty": True}
    logger.info("")

    # Write backup info
    info_file = backup_dir / "backup_info.json"
    with open(info_file, "w") as f:
        json.dump(backup_info, f, indent=2)
    logger.info(f"Backup info written to: {info_file}")

    return success


def restore_backup(logger: logging.Logger, backup_path: str) -> bool:
    """
    Restore application state from a backup.

    Args:
        logger: Logger instance.
        backup_path: Path to the backup directory.

    Returns:
        True if successful, False otherwise.
    """
    backup_dir = Path(backup_path)
    if not backup_dir.is_absolute():
        backup_dir = PROJECT_ROOT / backup_path

    if not backup_dir.exists():
        logger.error(f"Backup directory not found: {backup_dir}")
        return False

    # Read backup info
    info_file = backup_dir / "backup_info.json"
    if info_file.exists():
        with open(info_file) as f:
            backup_info = json.load(f)
        logger.info(f"Restoring backup from: {backup_info.get('timestamp', 'unknown')}")
        logger.info(f"Git commit: {backup_info.get('git_commit', 'unknown')}")
    else:
        logger.warning("No backup_info.json found, proceeding anyway")
        backup_info = {}

    logger.info("")
    success = True

    # 1. Restore PostgreSQL
    logger.info("[1/4] Restoring PostgreSQL...")
    postgres_file = backup_dir / "postgres.dump"
    if postgres_file.exists():
        try:
            from scripts.init_db import restore_postgres
            if not restore_postgres(postgres_file, logger):
                logger.warning("  PostgreSQL restore had issues")
        except ImportError as e:
            logger.warning(f"  Could not import init_db: {e}")
        except Exception as e:
            logger.error(f"  PostgreSQL restore failed: {e}")
            success = False
    else:
        logger.info("  No PostgreSQL backup found")
    logger.info("")

    # 2. Restore Neo4j
    logger.info("[2/4] Restoring Neo4j...")
    neo4j_file = backup_dir / "neo4j_export.cypher"
    if neo4j_file.exists():
        try:
            from scripts.init_neo4j import restore_neo4j
            if not restore_neo4j(neo4j_file, logger):
                logger.warning("  Neo4j restore had issues")
        except ImportError as e:
            logger.warning(f"  Could not import init_neo4j: {e}")
        except Exception as e:
            logger.warning(f"  Neo4j restore failed: {e}")
    else:
        logger.info("  No Neo4j backup found")
    logger.info("")

    # 3. Restore MongoDB
    logger.info("[3/4] Restoring MongoDB...")
    mongodb_dir = backup_dir / "mongodb"
    if mongodb_dir.exists() and any(mongodb_dir.iterdir()):
        try:
            from scripts.init_mongodb import restore_mongodb
            if not restore_mongodb(mongodb_dir, logger):
                logger.warning("  MongoDB restore had issues")
        except ImportError as e:
            logger.info(f"  MongoDB module not available: {e}")
        except Exception as e:
            logger.warning(f"  MongoDB restore failed: {e}")
    else:
        logger.info("  No MongoDB backup found")
    logger.info("")

    # 4. Restore workspace
    logger.info("[4/4] Restoring workspace...")
    src_workspace = backup_dir / "workspace"
    if src_workspace.exists():
        workspace_path = os.getenv("WORKSPACE_PATH")
        if workspace_path:
            dest_workspace = PROJECT_ROOT / workspace_path
        else:
            dest_workspace = PROJECT_ROOT / "workspace"

        try:
            # Remove existing workspace contents
            if dest_workspace.exists():
                for item in dest_workspace.iterdir():
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()

            # Copy from backup
            dest_workspace.mkdir(exist_ok=True)
            for item in src_workspace.iterdir():
                dest_item = dest_workspace / item.name
                if item.is_dir():
                    shutil.copytree(item, dest_item)
                else:
                    shutil.copy2(item, dest_item)

            job_count = len(list(dest_workspace.glob("job_*")))
            logger.info(f"  Restored workspace: {job_count} jobs")
        except Exception as e:
            logger.warning(f"  Workspace restore failed: {e}")
    else:
        logger.info("  No workspace backup found")
    logger.info("")

    return success


def main() -> int:
    """Main entry point for application initialization."""
    args = parse_args()
    logger = setup_logging(args.verbose)

    # Handle backup mode
    if args.create_backup is not None:
        logger.info("")
        logger.info("=" * 60)
        logger.info("Graph-RAG Application Backup")
        logger.info("=" * 60)
        logger.info("")

        name = args.create_backup if args.create_backup is not True else None
        success = create_backup(logger, name)

        logger.info("")
        logger.info("=" * 60)
        if success:
            logger.info("Backup Complete!")
        else:
            logger.warning("Backup completed with some issues")
        logger.info("=" * 60)
        return 0 if success else 1

    # Handle restore mode
    if args.restore_backup:
        logger.info("")
        logger.info("=" * 60)
        logger.info("Graph-RAG Application Restore")
        logger.info("=" * 60)
        logger.info("")

        success = restore_backup(logger, args.restore_backup)

        logger.info("")
        logger.info("=" * 60)
        if success:
            logger.info("Restore Complete!")
        else:
            logger.warning("Restore completed with some issues")
        logger.info("=" * 60)
        return 0 if success else 1

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

    # Handle seed profile selection
    seed_profile = None
    if args.seed:
        if args.seed == "list":
            # List available profiles
            logger.info("")
            logger.info("Available seed profiles:")
            logger.info("")
            profiles = list_seed_profiles()
            if not profiles:
                logger.info("  No profiles found in data/seed/")
                logger.info("  Create profiles by adding directories with neo4j.cypher and/or postgres.sql")
            else:
                for p in profiles:
                    components = []
                    if p["has_neo4j"]:
                        components.append("neo4j.cypher")
                    if p["has_postgres"]:
                        components.append("postgres.sql")
                    logger.info(f"  {p['name']:15} [{', '.join(components)}]")
            logger.info("")
            logger.info("Usage: python scripts/app_init.py --seed <profile> [--force-reset]")
            return 0
        else:
            # Get specific profile
            seed_profile = get_seed_profile(args.seed)
            if not seed_profile:
                logger.error(f"Seed profile not found: {args.seed}")
                logger.info("Use --seed without argument to list available profiles")
                return 1

    logger.info("")
    logger.info("=" * 60)
    logger.info("Graph-RAG Application Initialization")
    logger.info("=" * 60)
    if args.force_reset:
        logger.warning("WARNING: Force reset mode - all data will be deleted!")
    if seed_profile:
        logger.info(f"Seed profile: {seed_profile['name']}")
    logger.info("")

    # Calculate steps
    steps = []
    if args.force_reset:
        steps.append(("Workspace", clean_workspace))
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

        if name == "Workspace":
            logger.info(f"[{current_step}/{total_steps}] Cleaning {name}...")
        else:
            logger.info(f"[{current_step}/{total_steps}] Initializing {name}...")

        if name == "Workspace":
            success = init_func(logger)
        elif name == "Neo4j":
            success = init_func(logger, args.force_reset, seed_profile)
        elif name == "PostgreSQL":
            success = init_func(logger, args.force_reset, seed_profile)
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
