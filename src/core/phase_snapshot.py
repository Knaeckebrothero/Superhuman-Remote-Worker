"""Phase snapshot management for agent recovery.

This module provides snapshot creation and recovery at phase boundaries,
allowing agents to roll back to a previous phase if corruption occurs.

Snapshots are stored in: workspace/phase_snapshots/job_<id>/
Each snapshot includes:
- phase_<n>/checkpoint.db: Copy of the LangGraph checkpoint at phase end
- phase_<n>/metadata.json: Snapshot metadata (iteration, timestamp, etc.)
- phase_<n>/workspace.md: Copy of workspace.md at phase end
- phase_<n>/plan.md: Copy of plan.md at phase end
- phase_<n>/todos.yaml: Copy of todos.yaml at phase end
- phase_<n>/archive/: Copy of archived todos from previous phases
"""

import json
import logging
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime, UTC
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


@dataclass
class PhaseSnapshot:
    """Metadata for a phase snapshot."""

    phase_number: int
    is_strategic_phase: bool
    iteration: int
    message_count: int
    timestamp: str
    todos_completed: int = 0
    todos_total: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "PhaseSnapshot":
        """Create from dictionary."""
        return cls(
            phase_number=data["phase_number"],
            is_strategic_phase=data.get("is_strategic_phase", True),
            iteration=data["iteration"],
            message_count=data["message_count"],
            timestamp=data["timestamp"],
            todos_completed=data.get("todos_completed", 0),
            todos_total=data.get("todos_total", 0),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)


def get_phase_snapshots_path() -> Path:
    """Get base path for phase snapshots.

    Returns:
        Path to phase_snapshots directory (created if needed)
    """
    from .workspace import get_workspace_base_path

    base = get_workspace_base_path()
    snapshots_dir = base / "phase_snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    return snapshots_dir


class PhaseSnapshotManager:
    """Manages phase snapshots for job recovery.

    Creates snapshots at phase boundaries that can be used to recover
    from corrupted states without losing all progress.

    Example:
        ```python
        manager = PhaseSnapshotManager(job_id="abc123")

        # Create snapshot at phase transition
        manager.create_snapshot(
            phase_number=2,
            iteration=150,
            checkpoint_path=Path("workspace/checkpoints/job_abc123.db"),
            workspace_manager=ws,
            message_count=45,
            is_strategic_phase=False,
        )

        # List available snapshots
        snapshots = manager.list_snapshots()
        for s in snapshots:
            print(f"Phase {s.phase_number}: iteration {s.iteration}")

        # Recover to a specific phase
        manager.recover_to_phase(
            phase_number=2,
            checkpoint_path=Path("workspace/checkpoints/job_abc123.db"),
            workspace_manager=ws,
        )
        ```
    """

    def __init__(self, job_id: str, base_path: Optional[Path] = None):
        """Initialize snapshot manager.

        Args:
            job_id: Unique job identifier
            base_path: Override base path (for testing)
        """
        self.job_id = job_id

        if base_path is None:
            from .workspace import get_workspace_base_path

            base_path = get_workspace_base_path()

        self._base_path = base_path
        self._snapshots_dir = get_phase_snapshots_path() / f"job_{job_id}"
        self._workspace_path = base_path / f"job_{job_id}"
        self._checkpoint_path = base_path / "checkpoints" / f"job_{job_id}.db"

    @property
    def snapshots_dir(self) -> Path:
        """Get the snapshots directory for this job."""
        return self._snapshots_dir

    def create_snapshot(
        self,
        phase_number: int,
        iteration: int,
        checkpoint_path: Optional[Path] = None,
        workspace_manager: Optional["WorkspaceManager"] = None,
        message_count: int = 0,
        is_strategic_phase: bool = True,
        todos_completed: int = 0,
        todos_total: int = 0,
    ) -> Optional["PhaseSnapshot"]:
        """Create a phase snapshot.

        Should be called BEFORE handle_transition clears messages.

        Args:
            phase_number: Current phase number
            iteration: Current iteration count
            checkpoint_path: Path to LangGraph checkpoint DB (uses default if None)
            workspace_manager: WorkspaceManager for file access (uses paths directly if None)
            message_count: Number of messages in conversation
            is_strategic_phase: Whether in strategic phase
            todos_completed: Number of completed todos
            todos_total: Total number of todos

        Returns:
            PhaseSnapshot with metadata, or None if failed
        """
        checkpoint_path = checkpoint_path or self._checkpoint_path

        try:
            # Create snapshot directory
            snapshot_dir = self._snapshots_dir / f"phase_{phase_number}"
            snapshot_dir.mkdir(parents=True, exist_ok=True)

            # 1. Copy checkpoint database
            if checkpoint_path.exists():
                shutil.copy2(checkpoint_path, snapshot_dir / "checkpoint.db")
                logger.debug(f"[{self.job_id}] Snapshot: copied checkpoint.db")
            else:
                logger.warning(
                    f"[{self.job_id}] Snapshot: checkpoint.db not found at {checkpoint_path}"
                )

            # 2. Copy workspace files
            workspace_path = self._workspace_path
            if workspace_manager:
                workspace_path = workspace_manager.path

            files_to_copy = ["workspace.md", "plan.md", "todos.yaml"]
            for filename in files_to_copy:
                src = workspace_path / filename
                if src.exists():
                    shutil.copy2(src, snapshot_dir / filename)
                    logger.debug(f"[{self.job_id}] Snapshot: copied {filename}")

            # 3. Copy archive directory
            archive_src = workspace_path / "archive"
            archive_dst = snapshot_dir / "archive"
            if archive_src.exists() and any(archive_src.iterdir()):
                if archive_dst.exists():
                    shutil.rmtree(archive_dst)
                shutil.copytree(archive_src, archive_dst)
                logger.debug(f"[{self.job_id}] Snapshot: copied archive/")

            # 4. Write metadata
            snapshot = PhaseSnapshot(
                phase_number=phase_number,
                is_strategic_phase=is_strategic_phase,
                iteration=iteration,
                message_count=message_count,
                timestamp=datetime.now(UTC).isoformat(),
                todos_completed=todos_completed,
                todos_total=todos_total,
            )

            metadata_path = snapshot_dir / "metadata.json"
            with open(metadata_path, "w") as f:
                json.dump(snapshot.to_dict(), f, indent=2)

            logger.info(
                f"[{self.job_id}] Created phase {phase_number} snapshot "
                f"(iteration={iteration}, messages={message_count})"
            )

            return snapshot

        except Exception as e:
            logger.error(f"[{self.job_id}] Failed to create phase snapshot: {e}")
            return None

    def list_snapshots(self) -> List[PhaseSnapshot]:
        """List all available snapshots for this job.

        Returns:
            List of PhaseSnapshot objects, sorted by phase_number
        """
        snapshots = []

        if not self._snapshots_dir.exists():
            return snapshots

        for phase_dir in sorted(self._snapshots_dir.iterdir()):
            if not phase_dir.is_dir():
                continue
            if not phase_dir.name.startswith("phase_"):
                continue

            metadata_path = phase_dir / "metadata.json"
            if metadata_path.exists():
                try:
                    with open(metadata_path) as f:
                        data = json.load(f)
                    snapshots.append(PhaseSnapshot.from_dict(data))
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Invalid snapshot metadata at {phase_dir}: {e}")

        return sorted(snapshots, key=lambda s: s.phase_number)

    def get_snapshot(self, phase_number: int) -> Optional[PhaseSnapshot]:
        """Get snapshot metadata for a specific phase.

        Args:
            phase_number: Phase number to retrieve

        Returns:
            PhaseSnapshot if found, None otherwise
        """
        metadata_path = self._snapshots_dir / f"phase_{phase_number}" / "metadata.json"

        if not metadata_path.exists():
            return None

        try:
            with open(metadata_path) as f:
                data = json.load(f)
                return PhaseSnapshot.from_dict(data)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to read snapshot {metadata_path}: {e}")
            return None

    def recover_to_phase(
        self,
        phase_number: int,
        checkpoint_path: Optional[Path] = None,
        workspace_manager: Optional["WorkspaceManager"] = None,
    ) -> bool:
        """Recover job state to a specific phase snapshot.

        This restores:
        - The LangGraph checkpoint DB
        - workspace.md, plan.md, todos.yaml
        - archive/ directory

        Args:
            phase_number: Phase to recover to
            checkpoint_path: Path to LangGraph checkpoint DB to restore
            workspace_manager: WorkspaceManager for file restoration

        Returns:
            True if recovery succeeded, False otherwise
        """
        snapshot = self.get_snapshot(phase_number)
        if not snapshot:
            logger.error(f"[{self.job_id}] No snapshot found for phase {phase_number}")
            return False

        checkpoint_path = checkpoint_path or self._checkpoint_path
        workspace_path = self._workspace_path
        if workspace_manager:
            workspace_path = workspace_manager.path

        snapshot_dir = self._snapshots_dir / f"phase_{phase_number}"

        try:
            # 1. Restore checkpoint database
            snapshot_checkpoint = snapshot_dir / "checkpoint.db"
            if snapshot_checkpoint.exists():
                # Backup current checkpoint before overwriting
                if checkpoint_path.exists():
                    backup_path = checkpoint_path.with_suffix(".db.backup")
                    shutil.copy2(checkpoint_path, backup_path)
                    logger.info(f"[{self.job_id}] Backed up current checkpoint to {backup_path}")

                # Ensure checkpoints directory exists
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snapshot_checkpoint, checkpoint_path)
                logger.info(f"[{self.job_id}] Restored checkpoint.db from phase {phase_number}")
            else:
                logger.warning(
                    f"[{self.job_id}] No checkpoint.db in phase {phase_number} snapshot"
                )

            # 2. Restore workspace files
            files_to_restore = ["workspace.md", "plan.md", "todos.yaml"]
            for filename in files_to_restore:
                src = snapshot_dir / filename
                dst = workspace_path / filename
                if src.exists():
                    shutil.copy2(src, dst)
                    logger.debug(f"[{self.job_id}] Restored {filename}")
                elif dst.exists():
                    # File doesn't exist in snapshot but exists now - keep it
                    # (might be instructions.md or other files we don't snapshot)
                    pass

            # 3. Restore archive directory
            archive_src = snapshot_dir / "archive"
            archive_dst = workspace_path / "archive"
            if archive_src.exists():
                if archive_dst.exists():
                    shutil.rmtree(archive_dst)
                shutil.copytree(archive_src, archive_dst)
                logger.debug(f"[{self.job_id}] Restored archive/")
            elif archive_dst.exists():
                # Clear archive if snapshot has no archive
                shutil.rmtree(archive_dst)
                archive_dst.mkdir()
                logger.debug(f"[{self.job_id}] Cleared archive/ (empty in snapshot)")

            logger.info(
                f"[{self.job_id}] Successfully recovered to phase {phase_number} "
                f"(iteration={snapshot.iteration})"
            )
            return True

        except Exception as e:
            logger.error(f"[{self.job_id}] Failed to recover to phase {phase_number}: {e}")
            return False

    def delete_snapshots_after(self, phase_number: int) -> int:
        """Delete all snapshots after a given phase number.

        Useful after recovery to avoid confusion with stale snapshots.

        Args:
            phase_number: Delete snapshots with phase > this number

        Returns:
            Number of snapshots deleted
        """
        deleted = 0

        for snapshot in self.list_snapshots():
            if snapshot.phase_number > phase_number:
                snapshot_dir = self._snapshots_dir / f"phase_{snapshot.phase_number}"
                if snapshot_dir.exists():
                    shutil.rmtree(snapshot_dir)
                    deleted += 1
                    logger.info(
                        f"[{self.job_id}] Deleted snapshot for phase {snapshot.phase_number}"
                    )

        return deleted

    def cleanup(self) -> bool:
        """Remove all snapshots for this job.

        Returns:
            True if cleanup succeeded, False otherwise
        """
        if not self._snapshots_dir.exists():
            return True

        try:
            shutil.rmtree(self._snapshots_dir)
            logger.info(f"[{self.job_id}] Cleaned up all phase snapshots")
            return True
        except Exception as e:
            logger.error(f"[{self.job_id}] Failed to cleanup snapshots: {e}")
            return False

    def get_latest_snapshot(self) -> Optional[PhaseSnapshot]:
        """Get the most recent snapshot.

        Returns:
            Latest PhaseSnapshot, or None if no snapshots exist
        """
        snapshots = self.list_snapshots()
        return snapshots[-1] if snapshots else None

    def __repr__(self) -> str:
        return f"PhaseSnapshotManager(job_id='{self.job_id}')"


def format_snapshots_table(snapshots: List[PhaseSnapshot]) -> str:
    """Format snapshots as a human-readable table.

    Args:
        snapshots: List of snapshots to format

    Returns:
        Formatted table string
    """
    if not snapshots:
        return "No phase snapshots available."

    lines = [
        "",
        "=" * 75,
        "PHASE SNAPSHOTS",
        "=" * 75,
        f"{'Phase':<8}{'Type':<12}{'Iter':<10}{'Messages':<12}{'Todos':<12}{'Timestamp':<22}",
        "-" * 75,
    ]

    for s in snapshots:
        phase_type = "strategic" if s.is_strategic_phase else "tactical"
        todos_str = f"{s.todos_completed}/{s.todos_total}" if s.todos_total > 0 else "-"
        # Parse timestamp and format nicely
        try:
            ts = s.timestamp[:19].replace("T", " ")
        except (TypeError, IndexError):
            ts = str(s.timestamp)[:19]
        lines.append(
            f"{s.phase_number:<8}{phase_type:<12}{s.iteration:<10}{s.message_count:<12}{todos_str:<12}{ts:<22}"
        )

    lines.append("=" * 75)
    lines.append(f"Total: {len(snapshots)} snapshot(s)")
    lines.append("")

    return "\n".join(lines)
