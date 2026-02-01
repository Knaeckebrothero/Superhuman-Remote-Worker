#!/usr/bin/env python3
"""Root initialization script for the Graph-RAG system.

This script orchestrates initialization of all components:
- Orchestrator: PostgreSQL and MongoDB databases
- Agent: Workspace directory structure

This is the recommended entry point for development initialization.

Usage:
    # Initialize everything (databases + workspace)
    python init.py

    # Force reset everything (delete all data, recreate)
    python init.py --force-reset

    # Initialize only specific components
    python init.py --only-orchestrator     # Only databases
    python init.py --only-agent            # Only workspace

    # Skip specific databases
    python init.py --skip-postgres
    python init.py --skip-mongodb

    # Backup/restore
    python init.py --create-backup [name]           # Create full backup
    python init.py --restore-backup <path>          # Restore from backup

Note: The old scripts in scripts/ still work but will show deprecation warnings.
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

# Project root
PROJECT_ROOT = Path(__file__).parent

# Add project root to path for imports
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


def get_git_commit() -> str:
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


def generate_backup_dir_name(name: str | None) -> Path:
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


# =============================================================================
# Initialization Functions
# =============================================================================

async def init_all(
    force_reset: bool = False,
    only_orchestrator: bool = False,
    only_agent: bool = False,
    skip_postgres: bool = False,
    skip_mongodb: bool = False,
) -> bool:
    """Initialize all components.

    Args:
        force_reset: If True, delete all data and recreate.
        only_orchestrator: Only initialize databases.
        only_agent: Only initialize workspace.
        skip_postgres: Skip PostgreSQL initialization.
        skip_mongodb: Skip MongoDB initialization.

    Returns:
        True if all enabled components initialized successfully.
    """
    success = True
    step = 0
    total_steps = 0

    # Calculate total steps
    if not only_agent:
        if not skip_postgres:
            total_steps += 1
        if not skip_mongodb:
            total_steps += 1
    if not only_orchestrator:
        total_steps += 1
    total_steps += 1  # Verification step

    # Initialize orchestrator (databases)
    if not only_agent:
        from orchestrator.init import init_postgres, init_mongodb

        if not skip_postgres:
            step += 1
            logger.info(f"[{step}/{total_steps}] Initializing PostgreSQL...")
            if not await init_postgres(force_reset):
                logger.error("  PostgreSQL initialization failed. Aborting.")
                return False
            logger.info("")

        if not skip_mongodb:
            step += 1
            logger.info(f"[{step}/{total_steps}] Initializing MongoDB...")
            if not await init_mongodb(force_reset):
                # MongoDB failures are non-fatal
                logger.warning("  MongoDB initialization had issues (continuing...)")
            logger.info("")

    # Initialize agent (workspace)
    if not only_orchestrator:
        from src.init import init_workspace, cleanup_workspace

        step += 1
        if force_reset:
            logger.info(f"[{step}/{total_steps}] Cleaning workspace...")
            cleanup_workspace()
            logger.info("")
            step += 1
            total_steps += 1

        logger.info(f"[{step}/{total_steps}] Initializing workspace...")
        if not init_workspace():
            logger.warning("  Workspace initialization had issues")
        logger.info("")

    # Verification
    step += 1
    logger.info(f"[{step}/{total_steps}] Verifying setup...")
    await verify_all(
        skip_orchestrator=only_agent,
        skip_agent=only_orchestrator,
        skip_postgres=skip_postgres,
        skip_mongodb=skip_mongodb,
    )

    return success


async def verify_all(
    skip_orchestrator: bool = False,
    skip_agent: bool = False,
    skip_postgres: bool = False,
    skip_mongodb: bool = False,
) -> None:
    """Verify all components are properly initialized."""
    if not skip_orchestrator:
        from orchestrator.init import verify_postgres, verify_mongodb

        if not skip_postgres:
            pg_result = await verify_postgres()
            if pg_result.get("connected"):
                logger.info("  PostgreSQL: connected")
            else:
                logger.warning(f"  PostgreSQL: not connected ({pg_result.get('error', 'unknown')})")

        if not skip_mongodb:
            mg_result = await verify_mongodb()
            if not mg_result.get("configured"):
                logger.info("  MongoDB: not configured (optional)")
            elif mg_result.get("connected"):
                logger.info("  MongoDB: connected")
            else:
                logger.warning(f"  MongoDB: not connected ({mg_result.get('error', 'unknown')})")

    if not skip_agent:
        from src.init import verify_workspace

        ws_result = verify_workspace()
        if ws_result.get("exists"):
            logger.info(f"  Workspace: {ws_result['job_count']} jobs, {ws_result['checkpoint_count']} checkpoints")
        else:
            logger.warning("  Workspace: not initialized")


# =============================================================================
# Backup/Restore Functions
# =============================================================================

def create_backup(name: str | None = None) -> bool:
    """Create a backup of the current application state.

    Args:
        name: Optional name to append to backup directory.

    Returns:
        True if successful, False otherwise.
    """
    from orchestrator.init import backup_postgres, backup_mongodb
    from src.init import backup_workspace

    backup_dir = generate_backup_dir_name(name)
    backup_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Creating backup: {backup_dir}")
    logger.info("")

    success = True
    backup_info = {
        "timestamp": datetime.now().isoformat(),
        "git_commit": get_git_commit(),
        "components": {},
    }

    # 1. Backup PostgreSQL
    logger.info("[1/3] Backing up PostgreSQL...")
    postgres_file = backup_dir / "postgres.dump"
    if backup_postgres(postgres_file):
        backup_info["components"]["postgres"] = {"file": "postgres.dump", "success": True}
    else:
        backup_info["components"]["postgres"] = {"success": False}
        success = False
    logger.info("")

    # 2. Backup MongoDB
    logger.info("[2/3] Backing up MongoDB...")
    mongodb_dir = backup_dir / "mongodb"
    mongodb_dir.mkdir(exist_ok=True)
    if backup_mongodb(mongodb_dir):
        backup_info["components"]["mongodb"] = {"directory": "mongodb", "success": True}
    else:
        backup_info["components"]["mongodb"] = {"success": False}
    logger.info("")

    # 3. Backup workspace
    logger.info("[3/3] Backing up workspace...")
    if backup_workspace(backup_dir):
        from src.init import verify_workspace
        ws = verify_workspace()
        backup_info["components"]["workspace"] = {
            "directory": "workspace",
            "success": True,
            "jobs": ws.get("job_count", 0),
            "checkpoints": ws.get("checkpoint_count", 0),
        }
    else:
        backup_info["components"]["workspace"] = {"success": False}
    logger.info("")

    # Write backup info
    info_file = backup_dir / "backup_info.json"
    with open(info_file, "w") as f:
        json.dump(backup_info, f, indent=2)
    logger.info(f"Backup info written to: {info_file}")

    return success


def restore_backup(backup_path: str) -> bool:
    """Restore application state from a backup.

    Args:
        backup_path: Path to the backup directory.

    Returns:
        True if successful, False otherwise.
    """
    from orchestrator.init import restore_postgres, restore_mongodb
    from src.init import restore_workspace

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

    logger.info("")
    success = True

    # 1. Restore PostgreSQL
    logger.info("[1/3] Restoring PostgreSQL...")
    postgres_file = backup_dir / "postgres.dump"
    if postgres_file.exists():
        if not restore_postgres(postgres_file):
            logger.warning("  PostgreSQL restore had issues")
    else:
        logger.info("  No PostgreSQL backup found")
    logger.info("")

    # 2. Restore MongoDB
    logger.info("[2/3] Restoring MongoDB...")
    mongodb_dir = backup_dir / "mongodb"
    if mongodb_dir.exists() and any(mongodb_dir.iterdir()):
        if not restore_mongodb(mongodb_dir):
            logger.warning("  MongoDB restore had issues")
    else:
        logger.info("  No MongoDB backup found")
    logger.info("")

    # 3. Restore workspace
    logger.info("[3/3] Restoring workspace...")
    if not restore_workspace(backup_dir):
        logger.warning("  Workspace restore had issues")
    logger.info("")

    return success


# =============================================================================
# CLI
# =============================================================================

def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Initialize the Graph-RAG application.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python init.py                              # Initialize all components
  python init.py --force-reset                # Reset and reinitialize everything
  python init.py --only-orchestrator          # Only initialize databases
  python init.py --only-agent                 # Only initialize workspace
  python init.py --skip-mongodb               # Skip MongoDB (optional component)

Backup/Restore:
  python init.py --create-backup              # Create backup with auto-name
  python init.py --create-backup my_backup    # Create backup with custom name
  python init.py --restore-backup backups/20260117_001  # Restore from backup
        """,
    )
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="Delete all data and recreate (WARNING: deletes all data)",
    )
    parser.add_argument(
        "--only-orchestrator",
        action="store_true",
        help="Only initialize databases (PostgreSQL, MongoDB)",
    )
    parser.add_argument(
        "--only-agent",
        action="store_true",
        help="Only initialize workspace",
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
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    """Async main function."""
    # Handle backup mode
    if args.create_backup is not None:
        logger.info("")
        logger.info("=" * 60)
        logger.info("Graph-RAG Application Backup")
        logger.info("=" * 60)
        logger.info("")

        name = args.create_backup if args.create_backup is not True else None
        success = create_backup(name)

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

        success = restore_backup(args.restore_backup)

        logger.info("")
        logger.info("=" * 60)
        if success:
            logger.info("Restore Complete!")
        else:
            logger.warning("Restore completed with some issues")
        logger.info("=" * 60)
        return 0 if success else 1

    # Handle mutual exclusivity
    if args.only_orchestrator and args.only_agent:
        logger.error("Cannot specify both --only-orchestrator and --only-agent")
        return 1

    # Initialize mode
    logger.info("")
    logger.info("=" * 60)
    logger.info("Graph-RAG Application Initialization")
    logger.info("=" * 60)

    if args.force_reset:
        logger.warning("WARNING: Force reset mode - all data will be deleted!")

    logger.info("")

    success = await init_all(
        force_reset=args.force_reset,
        only_orchestrator=args.only_orchestrator,
        only_agent=args.only_agent,
        skip_postgres=args.skip_postgres,
        skip_mongodb=args.skip_mongodb,
    )

    logger.info("=" * 60)
    if success:
        logger.info("Initialization Complete!")
    else:
        logger.error("Initialization Failed!")
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
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
