#!/usr/bin/env python3
"""Workspace initialization for the agent.

This module provides workspace initialization functionality for:
- Workspace directory creation
- Workspace cleanup (for reset)
- Workspace verification
- Workspace backup/restore

Can be used standalone or imported by the root init.py.

Usage:
    # Ensure workspace exists
    python -m src.init

    # Clean all workspaces (force reset)
    python -m src.init --force-reset

    # Verify workspace structure
    python -m src.init --verify

    # Backup/restore workspace
    python -m src.init --backup /path/to/backup
    python -m src.init --restore /path/to/backup
"""
import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
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


def get_workspace_base_path() -> Path:
    """Get the base path for workspaces based on environment.

    Priority:
    1. WORKSPACE_PATH environment variable
    2. /workspace if running in container (detected by existence)
    3. ./workspace in project root for development

    Returns:
        Path to workspace base directory
    """
    # Check environment variable first
    env_path = os.getenv("WORKSPACE_PATH")
    if env_path:
        return Path(env_path)

    # Check if running in container (standard container workspace path)
    container_path = Path("/workspace")
    if container_path.exists() and container_path.is_dir():
        return container_path

    # Development mode: use ./workspace relative to project root
    return PROJECT_ROOT / "workspace"


# =============================================================================
# Workspace Initialization
# =============================================================================

def init_workspace() -> bool:
    """Initialize workspace directory structure.

    Creates the workspace base directory and standard subdirectories:
    - checkpoints/ - LangGraph checkpoint storage
    - logs/ - Job log files

    Returns:
        True if initialization successful.
    """
    base_path = get_workspace_base_path()

    logger.info(f"  Workspace path: {base_path}")

    try:
        # Create base directory
        base_path.mkdir(parents=True, exist_ok=True)
        logger.info("  Created/verified workspace directory")

        # Create standard subdirectories
        subdirs = ["checkpoints", "logs"]
        for subdir in subdirs:
            (base_path / subdir).mkdir(parents=True, exist_ok=True)
            logger.debug(f"    Created/verified: {subdir}/")

        logger.info(f"  Standard directories: {', '.join(subdirs)}")
        return True

    except Exception as e:
        logger.error(f"  Failed to initialize workspace: {e}")
        return False


def cleanup_workspace() -> bool:
    """Clean up all workspace contents.

    Removes all contents from the workspace directory while preserving
    the directory itself.

    Returns:
        True if cleanup successful.
    """
    base_path = get_workspace_base_path()

    if not base_path.exists():
        logger.info(f"  Workspace directory does not exist: {base_path}")
        return True

    items = list(base_path.iterdir())
    if not items:
        logger.info("  Workspace directory is already empty")
        return True

    removed = 0
    errors = 0

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
            errors += 1

    logger.info(f"  Removed {removed} item(s) from workspace")
    if errors > 0:
        logger.warning(f"  Failed to remove {errors} item(s)")

    return errors == 0


def verify_workspace() -> dict:
    """Verify workspace structure.

    Returns:
        Dict with workspace status information.
    """
    base_path = get_workspace_base_path()

    result = {
        "path": str(base_path),
        "exists": base_path.exists(),
        "directories": {},
        "job_count": 0,
        "checkpoint_count": 0,
        "log_count": 0,
        "total_size_bytes": 0,
    }

    if not base_path.exists():
        return result

    # Check standard directories
    for subdir in ["checkpoints", "logs"]:
        dir_path = base_path / subdir
        result["directories"][subdir] = dir_path.exists()

    # Count jobs
    job_dirs = list(base_path.glob("job_*"))
    result["job_count"] = len(job_dirs)

    # Count checkpoints
    checkpoints_dir = base_path / "checkpoints"
    if checkpoints_dir.exists():
        result["checkpoint_count"] = len(list(checkpoints_dir.glob("*.db")))

    # Count logs
    logs_dir = base_path / "logs"
    if logs_dir.exists():
        result["log_count"] = len(list(logs_dir.glob("*.log")))

    # Calculate total size
    total_size = 0
    for item in base_path.rglob("*"):
        if item.is_file():
            try:
                total_size += item.stat().st_size
            except OSError:
                pass
    result["total_size_bytes"] = total_size

    return result


def backup_workspace(backup_dir: Path) -> bool:
    """Backup workspace directory.

    Args:
        backup_dir: Directory to store the workspace backup.

    Returns:
        True if successful, False otherwise.
    """
    base_path = get_workspace_base_path()

    if not base_path.exists():
        logger.info("  Workspace does not exist, nothing to backup")
        return True

    items = list(base_path.iterdir())
    if not items:
        logger.info("  Workspace is empty, nothing to backup")
        return True

    dest_workspace = backup_dir / "workspace"

    try:
        shutil.copytree(base_path, dest_workspace)

        # Count what was backed up
        job_count = len(list(dest_workspace.glob("job_*")))
        checkpoint_count = 0
        if (dest_workspace / "checkpoints").exists():
            checkpoint_count = len(list((dest_workspace / "checkpoints").glob("*.db")))

        logger.info(f"  Backed up workspace: {job_count} jobs, {checkpoint_count} checkpoints")
        return True

    except Exception as e:
        logger.error(f"  Workspace backup failed: {e}")
        return False


def restore_workspace(backup_dir: Path) -> bool:
    """Restore workspace from backup.

    Args:
        backup_dir: Directory containing the workspace backup.

    Returns:
        True if successful, False otherwise.
    """
    src_workspace = backup_dir / "workspace"

    if not src_workspace.exists():
        logger.info("  No workspace backup found")
        return True

    base_path = get_workspace_base_path()

    try:
        # Clear existing workspace
        if base_path.exists():
            for item in base_path.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

        # Copy from backup
        base_path.mkdir(exist_ok=True)
        for item in src_workspace.iterdir():
            dest_item = base_path / item.name
            if item.is_dir():
                shutil.copytree(item, dest_item)
            else:
                shutil.copy2(item, dest_item)

        # Count what was restored
        job_count = len(list(base_path.glob("job_*")))
        logger.info(f"  Restored workspace: {job_count} jobs")
        return True

    except Exception as e:
        logger.error(f"  Workspace restore failed: {e}")
        return False


# =============================================================================
# CLI
# =============================================================================

def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Initialize agent workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.init                      # Ensure workspace exists
  python -m src.init --force-reset        # Clean all workspaces
  python -m src.init --verify             # Verify workspace structure
  python -m src.init --backup ./backup    # Create backup
  python -m src.init --restore ./backup   # Restore from backup
        """,
    )
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="Clean all workspace contents (WARNING: deletes all job data)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Only verify workspace structure, don't initialize",
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


def main() -> int:
    """Main entry point."""
    args = parse_args()
    setup_logging(args.verbose)

    # Verify mode
    if args.verify:
        logger.info("")
        logger.info("=" * 60)
        logger.info("Workspace Verification")
        logger.info("=" * 60)
        logger.info("")

        result = verify_workspace()

        logger.info(f"Workspace path: {result['path']}")
        if result["exists"]:
            logger.info("  Status: exists")
            logger.info(f"  Jobs: {result['job_count']}")
            logger.info(f"  Checkpoints: {result['checkpoint_count']}")
            logger.info(f"  Logs: {result['log_count']}")

            size_mb = result['total_size_bytes'] / (1024 * 1024)
            logger.info(f"  Total size: {size_mb:.2f} MB")

            # Check standard directories
            for subdir, exists in result["directories"].items():
                status = "ok" if exists else "MISSING"
                logger.info(f"  {subdir}/: {status}")
        else:
            logger.info("  Status: does not exist")

        return 0

    # Backup mode
    if args.backup:
        backup_dir = Path(args.backup)
        logger.info("")
        logger.info("=" * 60)
        logger.info("Workspace Backup")
        logger.info("=" * 60)
        logger.info(f"Backup directory: {backup_dir}")
        logger.info("")

        backup_dir.mkdir(parents=True, exist_ok=True)
        success = backup_workspace(backup_dir)
        return 0 if success else 1

    # Restore mode
    if args.restore:
        backup_dir = Path(args.restore)
        if not backup_dir.exists():
            logger.error(f"Backup directory not found: {backup_dir}")
            return 1

        logger.info("")
        logger.info("=" * 60)
        logger.info("Workspace Restore")
        logger.info("=" * 60)
        logger.info(f"Restoring from: {backup_dir}")
        logger.info("")

        success = restore_workspace(backup_dir)
        return 0 if success else 1

    # Initialize/cleanup mode
    logger.info("")
    logger.info("=" * 60)
    logger.info("Agent Workspace Initialization")
    logger.info("=" * 60)

    if args.force_reset:
        logger.warning("WARNING: Force reset mode - all workspace data will be deleted!")
        logger.info("")
        logger.info("Cleaning workspace...")
        if not cleanup_workspace():
            logger.error("Workspace cleanup failed")
            return 1

    logger.info("")
    logger.info("Initializing workspace...")
    success = init_workspace()

    logger.info("")
    logger.info("=" * 60)
    if success:
        logger.info("Workspace Initialization Complete!")
    else:
        logger.error("Workspace Initialization Failed!")
    logger.info("=" * 60)

    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInitialization cancelled by user.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
