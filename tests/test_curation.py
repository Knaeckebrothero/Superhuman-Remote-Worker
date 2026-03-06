"""Unit tests for curator subjob infrastructure (Phase 2 of project knowledge base).

Tests the curation trigger functions, config helpers, instruction formatting,
and the curation callback wiring in the archive_phase node.
"""

import asyncio
import pytest
import tempfile
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

project_root = Path(__file__).parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from src.api.orchestrator_client import OrchestratorClient  # noqa: E402
from src.core.loader import load_agent_config, resolve_config_path  # noqa: E402


def _load_config(name: str):
    """Helper to load a config by name."""
    path, deploy_dir = resolve_config_path(name)
    return load_agent_config(path, deployment_dir=deploy_dir)


# =========================================================================
# OrchestratorClient.create_curation_job
# =========================================================================

class TestCreateCurationJob:
    """Tests for OrchestratorClient.create_curation_job."""

    @pytest.fixture
    def client(self):
        c = OrchestratorClient(
            orchestrator_url="http://localhost:8085",
            pod_ip="127.0.0.1",
            pod_port=8001,
            hostname="test",
            config_name="test",
        )
        c._client = AsyncMock()
        return c

    @pytest.mark.asyncio
    async def test_create_curation_job_success(self, client):
        """Verify create_curation_job sends correct payload to orchestrator."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "curator-job-123"}
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.create_curation_job(
            job_id="target-job-456",
            description="Build feature X",
            config_name="developer",
            phase_data="Phase 1 (tactical) archived.\n- Built X: done",
            project_id="proj-789",
            curator_config="curator",
        )

        assert result is not None
        assert result["id"] == "curator-job-123"

        # Verify the POST payload
        call_args = client._client.post.call_args
        url = call_args[0][0]
        assert url.endswith("/api/jobs")

        payload = call_args[1]["json"]
        assert payload["config_name"] == "curator"
        assert payload["parent_job_id"] == "target-job-456"
        assert payload["project_id"] == "proj-789"
        assert payload["priority"] == 5
        assert payload["config_override"]["autonomy"] == "full"
        assert payload["context"]["curation_target"] == "target-job-456"
        assert payload["context"]["original_config"] == "developer"

    @pytest.mark.asyncio
    async def test_create_curation_job_no_project_id(self, client):
        """Verify create_curation_job fails gracefully without project_id."""
        result = await client.create_curation_job(
            job_id="target-job-456",
            description="Build feature X",
            config_name="developer",
            phase_data="Phase 1 archived.",
            project_id=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_create_curation_job_api_failure(self, client):
        """Verify create_curation_job handles API errors gracefully."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.create_curation_job(
            job_id="target-job-456",
            description="Build feature X",
            config_name="developer",
            phase_data="Phase 1 archived.",
            project_id="proj-789",
        )
        assert result is None


# =========================================================================
# _format_curation_instructions
# =========================================================================

class TestFormatCurationInstructions:
    """Tests for OrchestratorClient._format_curation_instructions."""

    def test_format_incremental_mode(self):
        """Verify incremental mode produces instructions with correct placeholders."""
        result = OrchestratorClient._format_curation_instructions(
            job_id="job-123",
            description="Build auth module",
            config_name="developer",
            phase_data="Phase 2 archived. Built JWT middleware.",
            curation_mode="incremental",
            curation_phase="phase_2",
        )
        assert result is not None
        assert "job-123" in result
        assert "developer" in result
        assert "Build auth module" in result
        assert "phase_2" in result
        assert "Phase 2 archived" in result
        assert "kb_search" in result  # Incremental mode mentions search

    def test_format_final_mode(self):
        """Verify final mode produces comprehensive sweep instructions."""
        result = OrchestratorClient._format_curation_instructions(
            job_id="job-123",
            description="Build auth module",
            config_name="developer",
            phase_data="Final pass context.",
            curation_mode="final",
            curation_phase="final",
        )
        assert result is not None
        assert "FINAL" in result
        assert "memories" in result

    def test_format_missing_template(self):
        """Verify graceful failure when template is not found."""
        with patch("pathlib.Path.exists", return_value=False):
            result = OrchestratorClient._format_curation_instructions(
                job_id="job-123",
                description="test",
                config_name="test",
                phase_data="test",
            )
            assert result is None


# =========================================================================
# Curation config helpers
# =========================================================================

class TestCurationConfig:
    """Tests for curation config in defaults.yaml and curator expert config."""

    def test_curation_config_defaults(self):
        """Verify defaults.yaml has the curator section."""
        config = _load_config("defaults")
        assert hasattr(config, "extra")
        curator = config.extra.get("curator", {})
        assert curator.get("enabled") is False
        assert curator.get("curator_config") == "curator"
        assert curator.get("autonomy") == "full"

    def test_curator_config_loads(self):
        """Verify the curator expert config loads without errors."""
        config = _load_config("curator")
        assert config.agent_id == "curator"
        assert config.display_name == "Curator"


# =========================================================================
# Curation callback in archive_phase
# =========================================================================

class TestCurationCallbackWiring:
    """Tests that the curation callback is called from archive_phase."""

    @pytest.fixture
    def temp_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def workspace_manager(self, temp_workspace):
        from src.core.workspace import WorkspaceManager
        ws = WorkspaceManager(job_id="test-job-123", base_path=temp_workspace)
        ws.initialize()
        return ws

    @pytest.mark.asyncio
    async def test_curation_callback_called_on_archive(self, workspace_manager):
        """Verify the curation callback fires during archive_phase."""
        from src.graph import create_archive_phase_node
        from src.managers import TodoManager, PlanManager
        from src.core.context import ContextManager

        config = _load_config("defaults")
        # Disable compaction so we don't need a real LLM
        config.context_management.compact_on_archive = False

        todo_manager = TodoManager(workspace_manager)
        plan_manager = PlanManager(workspace_manager)

        # Write a simple plan.md so get_current_phase works
        plan_manager.write("# Plan\n## Phase 1\nTactical work.\n")

        # Create and complete a todo
        todo_manager.add("Test task")
        for t in todo_manager.list_all():
            todo_manager.complete(t.id, notes=["Completed successfully"])

        context_mgr = ContextManager(config)
        mock_llm = MagicMock()

        # Track callback calls
        callback_calls = []

        async def mock_callback(job_id, phase_number, phase_data):
            callback_calls.append({
                "job_id": job_id,
                "phase_number": phase_number,
                "phase_data": phase_data,
            })

        archive_fn = create_archive_phase_node(
            todo_manager=todo_manager,
            plan_manager=plan_manager,
            config=config,
            context_mgr=context_mgr,
            llm=mock_llm,
            summarization_prompt="Summarize.",
            curation_callback=mock_callback,
        )

        state = {
            "job_id": "test-job-123",
            "iteration": 5,
            "phase_number": 1,
            "is_strategic_phase": False,
            "messages": [],
        }

        await archive_fn(state)

        # Allow async tasks to complete
        await asyncio.sleep(0.1)

        assert len(callback_calls) == 1
        assert callback_calls[0]["job_id"] == "test-job-123"
        assert callback_calls[0]["phase_number"] == 1
        assert "Phase 1 (tactical) archived" in callback_calls[0]["phase_data"]
        # Should include completed todo info
        assert "Test task" in callback_calls[0]["phase_data"]

    @pytest.mark.asyncio
    async def test_no_callback_when_none(self, workspace_manager):
        """Verify no error when curation_callback is None."""
        from src.graph import create_archive_phase_node
        from src.managers import TodoManager, PlanManager
        from src.core.context import ContextManager

        config = _load_config("defaults")
        config.context_management.compact_on_archive = False
        todo_manager = TodoManager(workspace_manager)
        plan_manager = PlanManager(workspace_manager)
        plan_manager.write("# Plan\n## Phase 1\nTest.\n")

        context_mgr = ContextManager(config)
        mock_llm = MagicMock()

        archive_fn = create_archive_phase_node(
            todo_manager=todo_manager,
            plan_manager=plan_manager,
            config=config,
            context_mgr=context_mgr,
            llm=mock_llm,
            summarization_prompt="Summarize.",
            curation_callback=None,
        )

        state = {
            "job_id": "test-job-123",
            "iteration": 5,
            "phase_number": 1,
            "is_strategic_phase": False,
            "messages": [],
        }

        # Should not raise
        result = await archive_fn(state)
        assert "messages" in result


# =========================================================================
# Agent curation_callback attribute
# =========================================================================

class TestAgentCurationCallback:
    """Tests that the agent correctly passes curation_callback to the graph."""

    def test_agent_has_curation_callback_attr(self):
        """Verify UniversalAgent has the curation_callback attribute."""
        from src.agent import UniversalAgent

        config = _load_config("defaults")
        agent = UniversalAgent(config)
        assert hasattr(agent, "curation_callback")
        assert agent.curation_callback is None
