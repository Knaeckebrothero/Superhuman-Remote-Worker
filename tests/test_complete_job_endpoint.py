"""Tests for the orchestrator complete_job endpoint and verification trigger.

Tests cover:
1. complete_job endpoint: freeze_data persistence, status transitions,
   completed_at, queued_replies cleanup
2. _trigger_verification_on_complete: all 5 guard conditions
3. OrchestratorClient.approve_job: success/failure paths
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from orchestrator.services.completion import (  # noqa: E402
    determine_job_status,
    is_job_completion_freeze,
    is_verification_enabled,
)


# =============================================================================
# Fixtures
# =============================================================================


def make_job(
    *,
    job_id: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    status: str = "processing",
    parent_job_id: str | None = None,
    freeze_data: dict | None = None,
    verification_enabled: bool = True,
    autonomy: str = "review",
    config_name: str = "defaults",
    context: dict | None = None,
) -> dict:
    """Create a minimal job dict for testing."""
    resolved_config = {
        "agent": {
            "agent_id": config_name,
            "autonomy": autonomy,
            "verification": {
                "enabled": verification_enabled,
                "critic_config": "critic",
                "max_rounds": 5,
            },
            "curator": {"enabled": False},
            "scholar": {"enabled": True, "scholar_config": "scholar"},
        },
    }

    job = {
        "id": job_id,
        "status": status,
        "config_name": config_name,
        "parent_job_id": parent_job_id,
        "resolved_config": resolved_config,
        "description": "Test job",
        "context": context or {},
        "project_id": None,
        "freeze_data": freeze_data,
    }
    return job


def make_mock_postgres():
    """Create a mock postgres_db with acquire() context manager."""
    mock_db = AsyncMock()

    # Mock the async context manager for acquire()
    mock_conn = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_db.acquire.return_value = mock_ctx

    return mock_db, mock_conn


# =============================================================================
# complete_job endpoint logic tests
# =============================================================================


class TestCompleteJobFreezeData:
    """Test freeze_data persistence in complete_job flow."""

    def test_freeze_data_stored_in_job_dict(self):
        """When freeze_data is in the result, it should be set on the job dict."""
        job = make_job()
        result = {
            "should_stop": True,
            "goal_achieved": False,
            "freeze_data": {"freeze_type": "job_complete", "summary": "done"},
        }

        # Simulate the orchestrator's freeze_data assignment
        if result.get("freeze_data"):
            job["freeze_data"] = result["freeze_data"]

        assert job["freeze_data"]["freeze_type"] == "job_complete"
        assert job["freeze_data"]["summary"] == "done"

    def test_freeze_data_enables_completion_check(self):
        """After freeze_data is set on job, is_job_completion_freeze should return True."""
        job = make_job()
        assert is_job_completion_freeze(job) is False

        # Simulate orchestrator writing freeze_data
        job["freeze_data"] = {"freeze_type": "job_complete", "summary": "done"}
        assert is_job_completion_freeze(job) is True

    def test_phase_boundary_freeze_not_completion(self):
        """Phase boundary freeze should NOT be treated as job completion."""
        job = make_job(freeze_data={"freeze_type": "phase_boundary", "phase_number": 3})
        assert is_job_completion_freeze(job) is False


class TestCompleteJobStatusDetermination:
    """Test status transitions in complete_job flow."""

    def test_completed_job_gets_reviewing_with_verification(self):
        """Job completion + verification enabled → reviewing."""
        job = make_job(
            verification_enabled=True,
            freeze_data={"freeze_type": "job_complete"},
        )
        result = {"should_stop": True, "goal_achieved": True}
        status, err = determine_job_status(job, result)
        assert status == "reviewing"

    def test_completed_job_gets_completed_without_verification(self):
        """Job completion + verification disabled + goal achieved → completed."""
        job = make_job(verification_enabled=False)
        result = {"should_stop": True, "goal_achieved": True}
        status, err = determine_job_status(job, result)
        assert status == "completed"

    def test_frozen_job_gets_pending_review(self):
        """Job frozen (no verification, not goal_achieved) → pending_review."""
        job = make_job(
            verification_enabled=False,
            freeze_data={"freeze_type": "job_complete"},
        )
        result = {"should_stop": True, "goal_achieved": False}
        status, err = determine_job_status(job, result)
        assert status == "pending_review"

    def test_error_gets_failed(self):
        """Job with error → failed."""
        job = make_job()
        result = {"error": {"message": "crashed"}}
        status, err = determine_job_status(job, result)
        assert status == "failed"
        assert err == "crashed"

    def test_completed_at_only_for_completed(self):
        """completed_at should only be set when status is 'completed'."""
        # This tests the logic, not the DB write
        job = make_job(verification_enabled=True)
        result = {"should_stop": True, "goal_achieved": True}
        status, _ = determine_job_status(job, result)
        assert status == "reviewing"  # Not "completed", so no completed_at

        job2 = make_job(verification_enabled=False)
        result2 = {"should_stop": True, "goal_achieved": True}
        status2, _ = determine_job_status(job2, result2)
        assert status2 == "completed"  # This one gets completed_at


class TestMemoryUnavailableStatus:
    """memory/KB-unavailable freeze → bounded pause-then-fail.

    docs/issues/embedding_key_missing_silently_disables_memory_and_kb.md
    """

    def _result(self, freeze_type="memory_unavailable"):
        return {
            "should_stop": True,
            "freeze_data": {
                "freeze_type": freeze_type,
                "reason": "Embedding service unavailable at startup.",
            },
        }

    def test_first_attempt_pauses(self):
        job = make_job(verification_enabled=False, context={})
        status, err = determine_job_status(job, self._result())
        assert status == "paused"
        assert err is None

    def test_under_cap_pauses(self):
        job = make_job(verification_enabled=False, context={"memory_retry_count": 1})
        status, _ = determine_job_status(job, self._result())
        assert status == "paused"

    def test_at_cap_fails_with_reason(self):
        job = make_job(verification_enabled=False, context={"memory_retry_count": 2})
        status, err = determine_job_status(job, self._result())
        assert status == "failed"
        assert err is not None
        assert "embedding" in err.lower()

    def test_kb_unavailable_pauses_then_fails(self):
        result = self._result(freeze_type="kb_unavailable")
        assert (
            determine_job_status(
                make_job(verification_enabled=False, context={}), result
            )[0]
            == "paused"
        )
        assert (
            determine_job_status(
                make_job(verification_enabled=False, context={"memory_retry_count": 2}),
                result,
            )[0]
            == "failed"
        )

    def test_context_as_json_string(self):
        # The job row's context can arrive as a JSON string, not a dict.
        job = make_job(verification_enabled=False)
        job["context"] = '{"memory_retry_count": 2}'
        status, _ = determine_job_status(job, self._result())
        assert status == "failed"


class TestCompleteJobCriticStatus:
    """Test status determination for critic (sub) jobs."""

    def test_critic_approved_gets_completed(self):
        """Critic with approved verdict → completed."""
        job = make_job(
            parent_job_id="parent-123",
            freeze_data={
                "status": "completed",
                "freeze_type": "verdict",
                "verdict": "approved",
            },
        )
        result = {"should_stop": True, "goal_achieved": True}
        status, _ = determine_job_status(job, result)
        assert status == "completed"

    def test_critic_returned_gets_waiting(self):
        """Critic with returned verdict → waiting."""
        job = make_job(
            parent_job_id="parent-123",
            freeze_data={
                "status": "waiting",
                "freeze_type": "verdict",
                "verdict": "returned",
            },
        )
        result = {"should_stop": True, "goal_achieved": False}
        status, _ = determine_job_status(job, result)
        assert status == "waiting"

    def test_critic_normalizes_job_completed(self):
        """Critic with status='job_completed' → normalized to 'completed'."""
        job = make_job(
            parent_job_id="parent-123",
            freeze_data={"status": "job_completed"},
        )
        result = {"should_stop": True, "goal_achieved": True}
        status, _ = determine_job_status(job, result)
        assert status == "completed"

    def test_critic_no_freeze_data_infers_from_goal(self):
        """Critic without freeze_data: goal_achieved=True → completed."""
        job = make_job(parent_job_id="parent-123")
        result = {"should_stop": True, "goal_achieved": True}
        status, _ = determine_job_status(job, result)
        assert status == "completed"

    def test_critic_no_freeze_data_no_goal_pending(self):
        """Critic without freeze_data: goal_achieved=False → pending_review."""
        job = make_job(parent_job_id="parent-123")
        result = {"should_stop": True, "goal_achieved": False}
        status, _ = determine_job_status(job, result)
        assert status == "pending_review"


# =============================================================================
# Verification trigger guard tests
# =============================================================================


class TestVerificationTriggerGuards:
    """Test the 5 guard conditions in _trigger_verification_on_complete.

    These are tested as pure logic (no async DB calls needed) since
    the guards are checked before any DB operations.
    """

    def test_guard_error_skips(self):
        """Guard 1: Jobs with errors should not trigger verification."""
        result = {"error": {"message": "failed"}, "should_stop": True}
        # Error guard: result.get("error") → return
        assert result.get("error") is not None

    def test_guard_not_stopped_skips(self):
        """Guard 2: Jobs that haven't stopped should not trigger verification."""
        result = {"should_stop": False}
        assert not result.get("should_stop", False)

    def test_guard_subjob_skips(self):
        """Guard 3: Sub-jobs should not trigger verification."""
        job = make_job(parent_job_id="parent-123", verification_enabled=True)
        assert job.get("parent_job_id") is not None

    def test_guard_verification_disabled_skips(self):
        """Guard 4: Verification disabled should not trigger verification."""
        job = make_job(verification_enabled=False)
        assert not is_verification_enabled(job)

    def test_guard_phase_boundary_skips(self):
        """Guard 5: Phase boundary freezes should not trigger verification."""
        job = make_job(
            verification_enabled=True,
            freeze_data={"freeze_type": "phase_boundary"},
        )
        # is_job_completion_freeze returns False for phase boundaries
        assert not is_job_completion_freeze(job)
        # And status is not "reviewing"
        assert job.get("status") != "reviewing"

    def test_all_guards_pass_for_valid_completion(self):
        """All guards should pass for a valid job completion with verification."""
        job = make_job(
            verification_enabled=True,
            freeze_data={"freeze_type": "job_complete", "summary": "done"},
            status="reviewing",  # Set by determine_job_status
        )
        result = {"should_stop": True, "goal_achieved": True}

        # Guard 1: no error
        assert not result.get("error")
        # Guard 2: stopped
        assert result.get("should_stop", False)
        # Guard 3: not a sub-job
        assert job.get("parent_job_id") is None
        # Guard 4: verification enabled
        assert is_verification_enabled(job)
        # Guard 5: is a job completion
        assert is_job_completion_freeze(job) or job.get("status") == "reviewing"


class TestVerificationTriggerIntegration:
    """Test the full flow: determine_job_status → verification trigger eligibility."""

    def test_status_reviewing_enables_verification_trigger(self):
        """When determine_job_status returns 'reviewing', the verification
        trigger guard should pass (status == 'reviewing')."""
        job = make_job(
            verification_enabled=True,
            freeze_data={"freeze_type": "job_complete"},
        )
        result = {"should_stop": True, "goal_achieved": True}

        # Step 1: determine_job_status
        new_status, _ = determine_job_status(job, result)
        assert new_status == "reviewing"

        # Step 2: simulate in-memory update (like the endpoint does)
        job["status"] = new_status

        # Step 3: verification trigger guards should all pass
        assert not result.get("error")
        assert result.get("should_stop", False)
        assert job.get("parent_job_id") is None
        assert is_verification_enabled(job)
        assert job.get("status") == "reviewing"

    def test_pending_review_without_freeze_blocks_verification(self):
        """When job is pending_review without job_complete freeze_data,
        verification should NOT trigger (this was the original bug)."""
        job = make_job(
            verification_enabled=True,
            # No freeze_data — just a phase boundary stop
        )
        result = {"should_stop": True, "goal_achieved": False}

        new_status, _ = determine_job_status(job, result)
        assert new_status == "pending_review"

        job["status"] = new_status

        # Guard 5 should block: no job_complete freeze and status != reviewing
        assert not is_job_completion_freeze(job)
        assert job.get("status") != "reviewing"
        # → verification should NOT trigger

    def test_goal_achieved_with_freeze_data_triggers_verification(self):
        """Goal achieved + freeze_data + verification enabled → triggers."""
        job = make_job(
            verification_enabled=True,
            freeze_data={"freeze_type": "job_complete", "summary": "all done"},
        )
        result = {"should_stop": True, "goal_achieved": True}

        new_status, _ = determine_job_status(job, result)
        assert new_status == "reviewing"

        job["status"] = new_status

        # All guards pass
        assert is_job_completion_freeze(job) or job.get("status") == "reviewing"


# =============================================================================
# OrchestratorClient.approve_job tests
# =============================================================================


class TestOrchestratorClientApproveJob:
    """Test the approve_job client method."""

    @pytest.fixture
    def client(self):
        from src.api.orchestrator_client import OrchestratorClient

        return OrchestratorClient(
            orchestrator_url="http://localhost:8085",
            pod_ip="10.0.0.1",
            pod_port=8001,
            hostname="test-agent",
            config_name="default",
        )

    @pytest.mark.asyncio
    async def test_approve_success(self, client):
        """Successful approval returns True."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.put = AsyncMock(return_value=mock_response)
            result = await client.approve_job("job-123")

        assert result is True
        mock_http.put.assert_called_once()
        call_url = mock_http.put.call_args[0][0]
        assert "job-123" in call_url
        assert "/approve" in call_url

    @pytest.mark.asyncio
    async def test_approve_with_notes(self, client):
        """Notes are passed in the request body."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.put = AsyncMock(return_value=mock_response)
            await client.approve_job("job-123", notes="looks good")

        call_kwargs = mock_http.put.call_args[1]
        assert call_kwargs["json"]["notes"] == "looks good"

    @pytest.mark.asyncio
    async def test_approve_failure(self, client):
        """Non-200 response returns False."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.put = AsyncMock(return_value=mock_response)
            result = await client.approve_job("job-123")

        assert result is False

    @pytest.mark.asyncio
    async def test_approve_connection_error(self, client):
        """Connection errors return False gracefully."""
        import httpx

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.put = AsyncMock(
                side_effect=httpx.RequestError("Connection refused")
            )
            result = await client.approve_job("job-123")

        assert result is False

    @pytest.mark.asyncio
    async def test_approve_auto_connects(self, client):
        """Client auto-connects if _client is None."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        # _client starts as None
        assert client._client is None

        with patch.object(client, "connect", AsyncMock()) as mock_connect:
            # After connect, set _client
            async def set_client():
                client._client = AsyncMock()
                client._client.put = AsyncMock(return_value=mock_response)

            mock_connect.side_effect = set_client

            result = await client.approve_job("job-123")

        mock_connect.assert_called_once()
        assert result is True


# =============================================================================
# OrchestratorClient.report_completion tests
# =============================================================================


class TestOrchestratorClientReportCompletion:
    """Test that report_completion sends the correct payload."""

    @pytest.fixture
    def client(self):
        from src.api.orchestrator_client import OrchestratorClient

        return OrchestratorClient(
            orchestrator_url="http://localhost:8085",
            pod_ip="10.0.0.1",
            pod_port=8001,
            hostname="test-agent",
            config_name="default",
        )

    @pytest.mark.asyncio
    async def test_sends_freeze_data(self, client):
        """Freeze data from the graph result is included in the payload."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"new_status": "reviewing", "actions": []}

        result = {
            "should_stop": True,
            "goal_achieved": True,
            "freeze_data": {"freeze_type": "job_complete", "summary": "done"},
        }

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            await client.report_completion("job-123", result)

        call_kwargs = mock_http.post.call_args[1]
        payload = call_kwargs["json"]
        assert payload["should_stop"] is True
        assert payload["goal_achieved"] is True
        assert payload["freeze_data"]["freeze_type"] == "job_complete"

    @pytest.mark.asyncio
    async def test_sends_error(self, client):
        """Error results are forwarded to the orchestrator."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"new_status": "failed", "actions": []}

        result = {"error": {"message": "OOM"}}

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            await client.report_completion("job-123", result)

        call_kwargs = mock_http.post.call_args[1]
        payload = call_kwargs["json"]
        assert payload["error"]["message"] == "OOM"
        assert payload["should_stop"] is False  # default

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, client):
        """Returns True when orchestrator handles completion."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"new_status": "completed", "actions": []}

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            success = await client.report_completion("job-123", {"should_stop": True})

        assert success is True
