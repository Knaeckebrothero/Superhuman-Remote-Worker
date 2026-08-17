"""P1-C deliverable contract — agent side.

knowledge-base/knowledge/issues/officer_blind_reads_and_worker_bureaucracy.md §4 P1-C, F13, F14.

Covered here:
  - path normalization + workspace resolution tolerant of the ``repo/``
    prefix (F14)
  - the "Required Deliverables (Contract)" task-brief block
  - F14 regression: ``job_complete`` must ACCEPT a correct deliverable list
    whose paths lack the ``repo/`` prefix when the files exist under it —
    and still reject genuinely nonexistent files
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests._tool_invoke import invoke_tool

project_root = Path(__file__).parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from src.core.deliverables import (  # noqa: E402
    format_deliverable_contract_block,
    parse_required_deliverables,
    resolve_workspace_deliverable,
)
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
    # MagicMock would auto-create a non-awaitable client and trip the
    # journal-before-observe POST; None takes the in-memory-only path.
    context.orchestrator_client = None
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
        assert "separate manifest or status file" in block
        assert "validates the listed artifacts directly" in block
        assert "manifest_status.json" not in block
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

        result = await invoke_tool(
            job_complete,
            {
                "summary": "All deliverables shipped.",
                "deliverables": ["output/report.md", "output/data.json"],
                "confidence": 0.9,
            },
        )
        assert "ERROR" not in result
        assert "Phase marked as final" in result
        assert _final_phase_data["job-deliverables-test"]["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_prefixed_paths_accepted_when_files_unprefixed(self, workspace):
        workspace.write_file("output/report.md", "r" * 200)
        _, job_complete = _job_tools(workspace)
        result = await invoke_tool(
            job_complete,
            {
                "summary": "Done.",
                "deliverables": ["repo/output/report.md"],
                "confidence": 0.95,
            },
        )
        assert "ERROR" not in result
        assert "Phase marked as final" in result

    @pytest.mark.asyncio
    async def test_genuinely_missing_file_still_rejected(self, workspace):
        _, job_complete = _job_tools(workspace)
        result = await invoke_tool(
            job_complete,
            {
                "summary": "Done.",
                "deliverables": ["output/never_written.md"],
                "confidence": 0.9,
            },
        )
        assert "ERROR" in result
        assert "does not exist" in result

    @pytest.mark.asyncio
    async def test_kb_deliverables_skip_workspace_check(self, workspace):
        """kb: entries are validated server-side by the gate, never against
        the workspace — they must not fail the seal here."""
        _, job_complete = _job_tools(workspace)
        result = await invoke_tool(
            job_complete,
            {
                "summary": "Done.",
                "deliverables": ["kb:century-findings"],
                "confidence": 0.9,
            },
        )
        assert "ERROR" not in result
        assert "Phase marked as final" in result
