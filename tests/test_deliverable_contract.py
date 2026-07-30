"""P1-C deliverable contract — agent side.

docs/issues/officer_blind_reads_and_worker_bureaucracy.md §4 P1-C, F13, F14.

Covered here:
  - path normalization + workspace resolution tolerant of the ``repo/``
    prefix (F14)
  - the job-stamped ``output/manifest_status.json`` written at phase
    boundaries (F13: inherited parent-snapshot completion files are exposed
    by the embedded job_id); no manifest → no file
  - ``_complete_phase_with_git`` wiring (manifest written even when git is
    inactive; absent without a manifest)
  - the "Required Deliverables (Contract)" task-brief block
  - F14 regression: ``job_complete`` must ACCEPT a correct deliverable list
    whose paths lack the ``repo/`` prefix when the files exist under it —
    and still reject genuinely nonexistent files
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

project_root = Path(__file__).parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from src.core.deliverables import (  # noqa: E402
    MANIFEST_STATUS_PATH,
    format_deliverable_contract_block,
    parse_required_deliverables,
    resolve_workspace_deliverable,
    write_manifest_status,
)
from src.core.phase import _complete_phase_with_git  # noqa: E402
from src.core.workspace import WorkspaceManager  # noqa: E402
from src.tools.core.job import _final_phase_data, create_job_tools  # noqa: E402
from tests._fs_backend import FilesystemTestBackend  # noqa: E402


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _clear_final_phase_data():
    _final_phase_data.clear()
    yield
    _final_phase_data.clear()


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = WorkspaceManager(
            job_id="job-deliverables-test",
            base_path=Path(tmpdir),
            backend=FilesystemTestBackend(Path(tmpdir)),
        )
        ws.initialize()
        yield ws


def _job_tools(workspace, job_id="job-deliverables-test"):
    context = MagicMock()
    context.job_id = job_id
    context.has_workspace.return_value = True
    context.workspace_manager = workspace
    context.has_todo.return_value = False
    return create_job_tools(context)


# =============================================================================
# Workspace resolution (F14)
# =============================================================================


class TestResolveWorkspaceDeliverable:
    def test_unprefixed_path_found_under_repo(self, workspace):
        workspace.write_file("repo/output/report.md", "x" * 100)
        resolved, exists = resolve_workspace_deliverable(workspace, "output/report.md")
        assert exists is True
        assert resolved == "repo/output/report.md"

    def test_prefixed_path_found_unprefixed(self, workspace):
        workspace.write_file("output/report.md", "x" * 100)
        resolved, exists = resolve_workspace_deliverable(
            workspace, "repo/output/report.md"
        )
        assert exists is True
        assert resolved == "output/report.md"

    def test_missing_everywhere(self, workspace):
        resolved, exists = resolve_workspace_deliverable(workspace, "output/nope.md")
        assert exists is False
        assert resolved == "output/nope.md"


# =============================================================================
# Manifest status writer (F13)
# =============================================================================


class TestWriteManifestStatus:
    def test_written_with_job_stamp_and_flags(self, workspace):
        workspace.write_file("output/a.md", "content of a")
        status = write_manifest_status(
            workspace,
            "job-deliverables-test",
            phase_number=3,
            required_deliverables=["output/a.md", "output/missing.md", "kb:note"],
            branch="job/deadbeef",
        )
        assert status is not None
        on_disk = json.loads(workspace.read_file(MANIFEST_STATUS_PATH))
        assert on_disk == status
        assert on_disk["job_id"] == "job-deliverables-test"
        assert on_disk["branch"] == "job/deadbeef"
        assert on_disk["phase"] == 3
        assert on_disk["generated_at"]
        by_path = {d["path"]: d for d in on_disk["deliverables"]}
        assert by_path["output/a.md"]["exists"] is True
        assert by_path["output/a.md"]["size_bytes"] == len("content of a")
        assert by_path["output/missing.md"]["exists"] is False
        assert by_path["output/missing.md"]["size_bytes"] is None
        # kb entries are recorded as an obligation, never claimed verified.
        assert by_path["kb:note"]["exists"] is None

    def test_repo_prefixed_file_counts_as_present(self, workspace):
        workspace.write_file("repo/output/a.md", "prefixed content")
        status = write_manifest_status(workspace, "j", 1, ["output/a.md"], branch=None)
        assert status["deliverables"][0]["exists"] is True
        assert status["deliverables"][0]["size_bytes"] == len("prefixed content")

    def test_no_manifest_writes_nothing(self, workspace):
        assert write_manifest_status(workspace, "j", 1, None) is None
        assert write_manifest_status(workspace, "j", 1, []) is None
        assert write_manifest_status(workspace, "j", 1, "  ") is None
        assert not workspace.exists(MANIFEST_STATUS_PATH)

    def test_never_raises(self, workspace):
        broken = MagicMock()
        broken.exists.side_effect = RuntimeError("probe broke")
        broken.write_file.side_effect = RuntimeError("write broke")
        assert write_manifest_status(broken, "j", 1, ["output/a.md"]) is None


# =============================================================================
# Phase-boundary wiring
# =============================================================================


class TestPhaseBoundaryManifest:
    def test_written_before_boundary_commit_with_branch(self, workspace):
        """The fixture workspace has a live local git repo — the manifest is
        stamped with its branch and lands before the boundary commit."""
        workspace.write_file("output/a.md", "x" * 60)
        assert workspace.git_manager and workspace.git_manager.is_active
        _complete_phase_with_git(
            workspace=workspace,
            phase_number=2,
            phase_type="tactical",
            todos_archived=4,
            job_id="job-deliverables-test",
            required_deliverables=["output/a.md", "output/b.md"],
        )
        on_disk = json.loads(workspace.read_file(MANIFEST_STATUS_PATH))
        assert on_disk["job_id"] == "job-deliverables-test"
        assert on_disk["phase"] == 2
        assert on_disk["branch"] == workspace.git_manager.current_branch()
        flags = {d["path"]: d["exists"] for d in on_disk["deliverables"]}
        assert flags == {"output/a.md": True, "output/b.md": False}

    def test_written_even_without_git(self):
        """Boundary provenance is not conditional on a live git remote."""
        from src.core.workspace import WorkspaceManagerConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            ws = WorkspaceManager(
                job_id="job-deliverables-test",
                base_path=Path(tmpdir),
                backend=FilesystemTestBackend(Path(tmpdir)),
                config=WorkspaceManagerConfig(git_versioning=False),
            )
            ws.initialize()
            ws.write_file("output/a.md", "x" * 60)
            assert ws.git_manager is None or not ws.git_manager.is_active
            _complete_phase_with_git(
                workspace=ws,
                phase_number=4,
                phase_type="strategic",
                job_id="job-deliverables-test",
                required_deliverables=["output/a.md"],
            )
            on_disk = json.loads(ws.read_file(MANIFEST_STATUS_PATH))
            assert on_disk["branch"] is None
            assert on_disk["deliverables"][0]["exists"] is True

    def test_no_manifest_no_file(self, workspace):
        _complete_phase_with_git(
            workspace=workspace,
            phase_number=2,
            phase_type="tactical",
            job_id="job-deliverables-test",
        )
        assert not workspace.exists(MANIFEST_STATUS_PATH)


# =============================================================================
# Task-brief contract block
# =============================================================================


class TestContractBlock:
    def test_lists_paths_and_rules(self):
        block = format_deliverable_contract_block(
            ["repo/output/report.md", "kb:findings"]
        )
        assert "## Required Deliverables (Contract)" in block
        assert "`output/report.md`" in block  # normalized
        assert "`kb:findings`" in block
        assert "kb_write" in block
        assert "manifest_status.json" in block
        assert "Scaffold" in block

    def test_empty_manifest_renders_nothing(self):
        assert format_deliverable_contract_block(None) == ""
        assert format_deliverable_contract_block([]) == ""
        assert format_deliverable_contract_block(["", "   "]) == ""

    def test_metadata_passthrough_shape(self):
        """The dispatch payload carries the manifest inside context →
        metadata (dual_app does metadata.update(context)); the parser reads
        it from either the dict or the raw list."""
        metadata = {
            "description": "d",
            "required_deliverables": ["./repo/output/a.md"],
        }
        assert parse_required_deliverables(metadata) == ["output/a.md"]


# =============================================================================
# F14 regression — job_complete accepts normalized paths
# =============================================================================


class TestJobCompleteF14:
    @pytest.mark.asyncio
    async def test_unprefixed_paths_accepted_when_files_live_under_repo(
        self, workspace
    ):
        """The 58027ee7 shape: all deliverables exist (under repo/), listed
        without the prefix, confidence 0.9 — the seal must be ACCEPTED, not
        forced to an honest floor."""
        workspace.write_file("repo/output/report.md", "r" * 200)
        workspace.write_file("repo/output/data.json", "d" * 200)
        _, job_complete = _job_tools(workspace)

        result = await job_complete.ainvoke(
            {
                "summary": "All deliverables shipped.",
                "deliverables": ["output/report.md", "output/data.json"],
                "confidence": 0.9,
            }
        )
        assert "ERROR" not in result
        assert "Phase marked as final" in result
        assert _final_phase_data["job-deliverables-test"]["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_prefixed_paths_accepted_when_files_unprefixed(self, workspace):
        workspace.write_file("output/report.md", "r" * 200)
        _, job_complete = _job_tools(workspace)
        result = await job_complete.ainvoke(
            {
                "summary": "Done.",
                "deliverables": ["repo/output/report.md"],
                "confidence": 0.95,
            }
        )
        assert "ERROR" not in result
        assert "Phase marked as final" in result

    @pytest.mark.asyncio
    async def test_genuinely_missing_file_still_rejected(self, workspace):
        _, job_complete = _job_tools(workspace)
        result = await job_complete.ainvoke(
            {
                "summary": "Done.",
                "deliverables": ["output/never_written.md"],
                "confidence": 0.9,
            }
        )
        assert "ERROR" in result
        assert "does not exist" in result

    @pytest.mark.asyncio
    async def test_kb_deliverables_skip_workspace_check(self, workspace):
        """kb: entries are validated server-side by the gate, never against
        the workspace — they must not fail the seal here."""
        _, job_complete = _job_tools(workspace)
        result = await job_complete.ainvoke(
            {
                "summary": "Done.",
                "deliverables": ["kb:century-findings"],
                "confidence": 0.9,
            }
        )
        assert "ERROR" not in result
        assert "Phase marked as final" in result
