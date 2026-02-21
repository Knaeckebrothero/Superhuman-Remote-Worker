"""Tests for phase snapshot management.

Tests PhaseSnapshot dataclass, PhaseSnapshotManager CRUD operations,
recovery logic, cleanup, and the format_snapshots_table utility.
"""

import json
import shutil
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.phase_snapshot import (
    PhaseSnapshot,
    PhaseSnapshotManager,
    discover_thread_id_from_checkpoint,
    format_snapshots_table,
)


# =============================================================================
# PhaseSnapshot dataclass
# =============================================================================


class TestPhaseSnapshot:
    """Tests for the PhaseSnapshot dataclass."""

    def test_from_dict_minimal(self):
        """Should create from dict with required fields only."""
        data = {
            "phase_number": 1,
            "iteration": 50,
            "message_count": 20,
            "timestamp": "2026-01-01T00:00:00",
        }
        snap = PhaseSnapshot.from_dict(data)
        assert snap.phase_number == 1
        assert snap.iteration == 50
        assert snap.message_count == 20
        assert snap.is_strategic_phase is True  # default
        assert snap.todos_completed == 0
        assert snap.todos_total == 0
        assert snap.thread_id is None

    def test_from_dict_full(self):
        """Should create from dict with all fields."""
        data = {
            "phase_number": 3,
            "is_strategic_phase": False,
            "iteration": 150,
            "message_count": 45,
            "timestamp": "2026-01-01T12:00:00",
            "todos_completed": 8,
            "todos_total": 10,
            "thread_id": "thread-abc",
        }
        snap = PhaseSnapshot.from_dict(data)
        assert snap.is_strategic_phase is False
        assert snap.todos_completed == 8
        assert snap.thread_id == "thread-abc"

    def test_to_dict_roundtrip(self):
        """from_dict(to_dict(snap)) should produce equivalent snapshot."""
        original = PhaseSnapshot(
            phase_number=2,
            is_strategic_phase=False,
            iteration=100,
            message_count=30,
            timestamp="2026-02-18T10:00:00",
            todos_completed=5,
            todos_total=10,
            thread_id="t-123",
        )
        data = original.to_dict()
        restored = PhaseSnapshot.from_dict(data)
        assert restored.phase_number == original.phase_number
        assert restored.is_strategic_phase == original.is_strategic_phase
        assert restored.thread_id == original.thread_id


# =============================================================================
# PhaseSnapshotManager — creation and listing
# =============================================================================


class TestSnapshotCreation:
    """Tests for create_snapshot and list_snapshots."""

    def _make_manager(self, tmp_path, job_id="test-job"):
        """Create a PhaseSnapshotManager with tmp_path base and patched snapshots_dir."""
        mgr = PhaseSnapshotManager.__new__(PhaseSnapshotManager)
        mgr.job_id = job_id
        mgr._base_path = tmp_path
        mgr._snapshots_dir = tmp_path / "phase_snapshots" / f"job_{job_id}"
        mgr._workspace_path = tmp_path / f"job_{job_id}"
        mgr._checkpoint_path = tmp_path / "checkpoints" / f"job_{job_id}.db"
        return mgr

    def _setup_workspace(self, tmp_path, job_id="test-job"):
        """Create minimal workspace files."""
        ws_path = tmp_path / f"job_{job_id}"
        ws_path.mkdir(parents=True)
        (ws_path / "workspace.md").write_text("# Workspace")
        (ws_path / "plan.md").write_text("# Plan")
        (ws_path / "todos.yaml").write_text("todos: []")
        return ws_path

    def _setup_checkpoint(self, tmp_path, job_id="test-job"):
        """Create a fake checkpoint.db."""
        cp_dir = tmp_path / "checkpoints"
        cp_dir.mkdir(parents=True)
        cp_path = cp_dir / f"job_{job_id}.db"
        cp_path.write_bytes(b"fake-checkpoint-data")
        return cp_path

    def test_create_snapshot_basic(self, tmp_path):
        """Should create snapshot directory with metadata."""
        self._setup_workspace(tmp_path)
        self._setup_checkpoint(tmp_path)
        mgr = self._make_manager(tmp_path)

        snap = mgr.create_snapshot(
            phase_number=1, iteration=50, message_count=20,
        )
        assert snap is not None
        assert snap.phase_number == 1
        assert snap.iteration == 50

        # Verify files exist
        snap_dir = mgr._snapshots_dir / "phase_1"
        assert (snap_dir / "metadata.json").exists()
        assert (snap_dir / "checkpoint.db").exists()
        assert (snap_dir / "workspace.md").exists()
        assert (snap_dir / "plan.md").exists()
        assert (snap_dir / "todos.yaml").exists()

    def test_create_snapshot_without_checkpoint(self, tmp_path):
        """Should succeed even without checkpoint.db (logs warning)."""
        self._setup_workspace(tmp_path)
        mgr = self._make_manager(tmp_path)

        snap = mgr.create_snapshot(phase_number=1, iteration=10)
        assert snap is not None
        snap_dir = mgr._snapshots_dir / "phase_1"
        assert not (snap_dir / "checkpoint.db").exists()

    def test_create_snapshot_copies_archive(self, tmp_path):
        """Should copy archive/ if it exists and is non-empty."""
        ws_path = self._setup_workspace(tmp_path)
        archive_dir = ws_path / "archive"
        archive_dir.mkdir()
        (archive_dir / "todos_phase_1.yaml").write_text("archived: true")

        mgr = self._make_manager(tmp_path)
        mgr.create_snapshot(phase_number=2, iteration=100)

        snap_dir = mgr._snapshots_dir / "phase_2"
        assert (snap_dir / "archive" / "todos_phase_1.yaml").exists()

    def test_create_snapshot_stores_thread_id(self, tmp_path):
        """Should store thread_id in metadata."""
        self._setup_workspace(tmp_path)
        mgr = self._make_manager(tmp_path)

        snap = mgr.create_snapshot(
            phase_number=1, iteration=10, thread_id="thread-xyz",
        )
        assert snap.thread_id == "thread-xyz"

        # Verify in persisted metadata
        meta_path = mgr._snapshots_dir / "phase_1" / "metadata.json"
        data = json.loads(meta_path.read_text())
        assert data["thread_id"] == "thread-xyz"

    def test_list_snapshots_empty(self, tmp_path):
        """Should return empty list when no snapshots exist."""
        mgr = self._make_manager(tmp_path)
        assert mgr.list_snapshots() == []

    def test_list_snapshots_sorted(self, tmp_path):
        """Should return snapshots sorted by phase_number."""
        self._setup_workspace(tmp_path)
        mgr = self._make_manager(tmp_path)

        mgr.create_snapshot(phase_number=3, iteration=300)
        mgr.create_snapshot(phase_number=1, iteration=100)
        mgr.create_snapshot(phase_number=2, iteration=200)

        snapshots = mgr.list_snapshots()
        assert len(snapshots) == 3
        assert [s.phase_number for s in snapshots] == [1, 2, 3]

    def test_list_snapshots_skips_invalid(self, tmp_path):
        """Should skip directories with invalid metadata."""
        mgr = self._make_manager(tmp_path)
        mgr._snapshots_dir.mkdir(parents=True)

        # Create invalid snapshot directory
        bad_dir = mgr._snapshots_dir / "phase_99"
        bad_dir.mkdir()
        (bad_dir / "metadata.json").write_text("not valid json")

        snapshots = mgr.list_snapshots()
        assert len(snapshots) == 0


# =============================================================================
# get_snapshot
# =============================================================================


class TestGetSnapshot:
    """Tests for get_snapshot()."""

    def _make_manager(self, tmp_path, job_id="test-job"):
        mgr = PhaseSnapshotManager.__new__(PhaseSnapshotManager)
        mgr.job_id = job_id
        mgr._base_path = tmp_path
        mgr._snapshots_dir = tmp_path / "phase_snapshots" / f"job_{job_id}"
        mgr._workspace_path = tmp_path / f"job_{job_id}"
        mgr._checkpoint_path = tmp_path / "checkpoints" / f"job_{job_id}.db"
        return mgr

    def test_get_existing_snapshot(self, tmp_path):
        """Should return snapshot for existing phase."""
        mgr = self._make_manager(tmp_path)
        ws = tmp_path / "job_test-job"
        ws.mkdir(parents=True)
        (ws / "workspace.md").write_text("")
        mgr.create_snapshot(phase_number=1, iteration=50)

        snap = mgr.get_snapshot(1)
        assert snap is not None
        assert snap.phase_number == 1

    def test_get_nonexistent_snapshot(self, tmp_path):
        """Should return None for nonexistent phase."""
        mgr = self._make_manager(tmp_path)
        assert mgr.get_snapshot(99) is None


# =============================================================================
# Recovery
# =============================================================================


class TestRecoverToPhase:
    """Tests for recover_to_phase()."""

    def _make_manager(self, tmp_path, job_id="test-job"):
        mgr = PhaseSnapshotManager.__new__(PhaseSnapshotManager)
        mgr.job_id = job_id
        mgr._base_path = tmp_path
        mgr._snapshots_dir = tmp_path / "phase_snapshots" / f"job_{job_id}"
        mgr._workspace_path = tmp_path / f"job_{job_id}"
        mgr._checkpoint_path = tmp_path / "checkpoints" / f"job_{job_id}.db"
        return mgr

    def test_recover_restores_files(self, tmp_path):
        """Recovery should restore workspace files from snapshot."""
        job_id = "test-job"
        mgr = self._make_manager(tmp_path, job_id)

        # Setup workspace with initial state
        ws_path = tmp_path / f"job_{job_id}"
        ws_path.mkdir(parents=True)
        (ws_path / "workspace.md").write_text("Phase 1 workspace")
        (ws_path / "plan.md").write_text("Phase 1 plan")
        (ws_path / "todos.yaml").write_text("phase: 1")

        # Create checkpoint
        cp_dir = tmp_path / "checkpoints"
        cp_dir.mkdir(parents=True)
        cp_path = cp_dir / f"job_{job_id}.db"
        cp_path.write_bytes(b"checkpoint-phase-1")

        # Create snapshot at phase 1
        mgr.create_snapshot(phase_number=1, iteration=50)

        # Modify workspace (simulate phase 2)
        (ws_path / "workspace.md").write_text("Phase 2 workspace")
        (ws_path / "plan.md").write_text("Phase 2 plan")
        cp_path.write_bytes(b"checkpoint-phase-2")

        # Recover to phase 1
        result = mgr.recover_to_phase(1)
        assert result is True

        # Verify files restored
        assert (ws_path / "workspace.md").read_text() == "Phase 1 workspace"
        assert (ws_path / "plan.md").read_text() == "Phase 1 plan"
        assert cp_path.read_bytes() == b"checkpoint-phase-1"

    def test_recover_backs_up_checkpoint(self, tmp_path):
        """Recovery should create .db.backup before overwriting."""
        job_id = "test-job"
        mgr = self._make_manager(tmp_path, job_id)

        ws_path = tmp_path / f"job_{job_id}"
        ws_path.mkdir(parents=True)
        (ws_path / "workspace.md").write_text("initial")

        cp_dir = tmp_path / "checkpoints"
        cp_dir.mkdir(parents=True)
        cp_path = cp_dir / f"job_{job_id}.db"
        cp_path.write_bytes(b"old-checkpoint")

        mgr.create_snapshot(phase_number=1, iteration=10)

        # Modify checkpoint
        cp_path.write_bytes(b"current-checkpoint")

        # Recover — should backup current
        mgr.recover_to_phase(1)

        backup = cp_path.with_suffix(".db.backup")
        assert backup.exists()
        assert backup.read_bytes() == b"current-checkpoint"

    def test_recover_nonexistent_returns_false(self, tmp_path):
        """Recovery to nonexistent phase should return False."""
        mgr = self._make_manager(tmp_path)
        assert mgr.recover_to_phase(99) is False

    def test_recover_restores_archive(self, tmp_path):
        """Recovery should restore archive directory."""
        job_id = "test-job"
        mgr = self._make_manager(tmp_path, job_id)

        ws_path = tmp_path / f"job_{job_id}"
        ws_path.mkdir(parents=True)
        (ws_path / "workspace.md").write_text("")
        archive = ws_path / "archive"
        archive.mkdir()
        (archive / "phase_1.yaml").write_text("archived")

        mgr.create_snapshot(phase_number=1, iteration=10)

        # Remove archive and add different content
        shutil.rmtree(archive)
        archive.mkdir()
        (archive / "phase_2.yaml").write_text("new stuff")

        # Recover
        mgr.recover_to_phase(1)

        assert (archive / "phase_1.yaml").exists()
        assert not (archive / "phase_2.yaml").exists()


# =============================================================================
# Deletion and cleanup
# =============================================================================


class TestDeletionAndCleanup:
    """Tests for delete_snapshots_after and cleanup."""

    def _make_manager(self, tmp_path, job_id="test-job"):
        mgr = PhaseSnapshotManager.__new__(PhaseSnapshotManager)
        mgr.job_id = job_id
        mgr._base_path = tmp_path
        mgr._snapshots_dir = tmp_path / "phase_snapshots" / f"job_{job_id}"
        mgr._workspace_path = tmp_path / f"job_{job_id}"
        mgr._checkpoint_path = tmp_path / "checkpoints" / f"job_{job_id}.db"
        return mgr

    def test_delete_after_removes_later_phases(self, tmp_path):
        """delete_snapshots_after should remove phases > N."""
        mgr = self._make_manager(tmp_path)
        ws = tmp_path / "job_test-job"
        ws.mkdir(parents=True)
        (ws / "workspace.md").write_text("")

        mgr.create_snapshot(phase_number=1, iteration=10)
        mgr.create_snapshot(phase_number=2, iteration=20)
        mgr.create_snapshot(phase_number=3, iteration=30)

        deleted = mgr.delete_snapshots_after(1)
        assert deleted == 2

        remaining = mgr.list_snapshots()
        assert len(remaining) == 1
        assert remaining[0].phase_number == 1

    def test_delete_after_returns_zero_when_nothing(self, tmp_path):
        """Should return 0 when no snapshots to delete."""
        mgr = self._make_manager(tmp_path)
        ws = tmp_path / "job_test-job"
        ws.mkdir(parents=True)
        (ws / "workspace.md").write_text("")

        mgr.create_snapshot(phase_number=1, iteration=10)
        deleted = mgr.delete_snapshots_after(5)
        assert deleted == 0

    def test_delete_after_preserves_given_phase(self, tmp_path):
        """delete_snapshots_after(N) should NOT delete phase N itself."""
        mgr = self._make_manager(tmp_path)
        ws = tmp_path / "job_test-job"
        ws.mkdir(parents=True)
        (ws / "workspace.md").write_text("")

        mgr.create_snapshot(phase_number=2, iteration=20)
        mgr.create_snapshot(phase_number=3, iteration=30)

        mgr.delete_snapshots_after(2)
        remaining = mgr.list_snapshots()
        assert any(s.phase_number == 2 for s in remaining)

    def test_cleanup_removes_everything(self, tmp_path):
        """cleanup should remove entire snapshots directory."""
        mgr = self._make_manager(tmp_path)
        ws = tmp_path / "job_test-job"
        ws.mkdir(parents=True)
        (ws / "workspace.md").write_text("")

        mgr.create_snapshot(phase_number=1, iteration=10)
        assert mgr._snapshots_dir.exists()

        result = mgr.cleanup()
        assert result is True
        assert not mgr._snapshots_dir.exists()

    def test_cleanup_returns_true_when_no_dir(self, tmp_path):
        """cleanup should return True when directory doesn't exist."""
        mgr = self._make_manager(tmp_path)
        assert mgr.cleanup() is True


# =============================================================================
# get_latest_snapshot
# =============================================================================


class TestGetLatestSnapshot:
    """Tests for get_latest_snapshot."""

    def _make_manager(self, tmp_path, job_id="test-job"):
        mgr = PhaseSnapshotManager.__new__(PhaseSnapshotManager)
        mgr.job_id = job_id
        mgr._base_path = tmp_path
        mgr._snapshots_dir = tmp_path / "phase_snapshots" / f"job_{job_id}"
        mgr._workspace_path = tmp_path / f"job_{job_id}"
        mgr._checkpoint_path = tmp_path / "checkpoints" / f"job_{job_id}.db"
        return mgr

    def test_returns_last_snapshot(self, tmp_path):
        """Should return the highest phase number snapshot."""
        mgr = self._make_manager(tmp_path)
        ws = tmp_path / "job_test-job"
        ws.mkdir(parents=True)
        (ws / "workspace.md").write_text("")

        mgr.create_snapshot(phase_number=1, iteration=10)
        mgr.create_snapshot(phase_number=3, iteration=30)

        latest = mgr.get_latest_snapshot()
        assert latest is not None
        assert latest.phase_number == 3

    def test_returns_none_when_empty(self, tmp_path):
        """Should return None when no snapshots."""
        mgr = self._make_manager(tmp_path)
        assert mgr.get_latest_snapshot() is None


# =============================================================================
# format_snapshots_table
# =============================================================================


class TestFormatSnapshotsTable:
    """Tests for format_snapshots_table utility."""

    def test_empty_list(self):
        """Should return message for empty list."""
        result = format_snapshots_table([])
        assert result == "No phase snapshots available."

    def test_formats_strategic_type(self):
        """Should show 'strategic' for is_strategic_phase=True."""
        snap = PhaseSnapshot(
            phase_number=1, is_strategic_phase=True, iteration=50,
            message_count=20, timestamp="2026-01-01T10:00:00",
        )
        result = format_snapshots_table([snap])
        assert "strategic" in result

    def test_formats_tactical_type(self):
        """Should show 'tactical' for is_strategic_phase=False."""
        snap = PhaseSnapshot(
            phase_number=2, is_strategic_phase=False, iteration=100,
            message_count=30, timestamp="2026-01-01T12:00:00",
        )
        result = format_snapshots_table([snap])
        assert "tactical" in result

    def test_shows_todos_dash_when_zero(self):
        """Should show '-' when todos_total is 0."""
        snap = PhaseSnapshot(
            phase_number=1, is_strategic_phase=True, iteration=50,
            message_count=20, timestamp="2026-01-01T10:00:00",
            todos_total=0,
        )
        result = format_snapshots_table([snap])
        assert "-" in result

    def test_shows_todo_fraction(self):
        """Should show completed/total for non-zero todos."""
        snap = PhaseSnapshot(
            phase_number=1, is_strategic_phase=True, iteration=50,
            message_count=20, timestamp="2026-01-01T10:00:00",
            todos_completed=5, todos_total=10,
        )
        result = format_snapshots_table([snap])
        assert "5/10" in result

    def test_shows_total_count(self):
        """Should show total count at bottom."""
        snaps = [
            PhaseSnapshot(phase_number=i, is_strategic_phase=True, iteration=i*10,
                          message_count=i*5, timestamp="2026-01-01T00:00:00")
            for i in range(1, 4)
        ]
        result = format_snapshots_table(snaps)
        assert "3 snapshot(s)" in result


# =============================================================================
# discover_thread_id_from_checkpoint
# =============================================================================


class TestDiscoverThreadId:
    """Tests for discover_thread_id_from_checkpoint."""

    def test_returns_none_for_missing_file(self, tmp_path):
        """Should return None when file doesn't exist."""
        result = discover_thread_id_from_checkpoint(
            tmp_path / "nonexistent.db", "job-1",
        )
        assert result is None

    def test_discovers_matching_thread(self, tmp_path):
        """Should find thread_id matching the job_id."""
        db_path = tmp_path / "checkpoint.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)"
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES ('thread_job-1_abc', 'cp1')"
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES ('thread_job-1_abc', 'cp2')"
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES ('unrelated', 'cp3')"
        )
        conn.commit()
        conn.close()

        result = discover_thread_id_from_checkpoint(db_path, "job-1")
        assert result == "thread_job-1_abc"

    def test_returns_none_no_matching_job(self, tmp_path):
        """Should return None when no thread matches job_id."""
        db_path = tmp_path / "checkpoint.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)"
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES ('thread_other_job', 'cp1')"
        )
        conn.commit()
        conn.close()

        result = discover_thread_id_from_checkpoint(db_path, "job-xyz")
        assert result is None

    def test_returns_none_empty_db(self, tmp_path):
        """Should return None for empty checkpoints table."""
        db_path = tmp_path / "checkpoint.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)"
        )
        conn.commit()
        conn.close()

        result = discover_thread_id_from_checkpoint(db_path, "job-1")
        assert result is None
