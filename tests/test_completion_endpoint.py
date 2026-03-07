"""Unit tests for orchestrator-side job completion handling.

Tests the pure logic functions in orchestrator/services/completion.py
and the orchestrator endpoint helpers.
"""

import json
import pytest
import sys
from pathlib import Path
from unittest.mock import patch

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from orchestrator.services.completion import (  # noqa: E402
    determine_job_status,
    get_verification_config,
    is_verification_enabled,
    get_curation_config,
    is_curation_enabled,
    get_autonomy_level,
    is_job_completion_freeze,
    format_verification_instructions,
)


# =============================================================================
# Fixtures — job dicts with resolved_config
# =============================================================================


def make_job(
    *,
    status: str = "processing",
    parent_job_id: str | None = None,
    freeze_data: dict | None = None,
    verification_enabled: bool = True,
    curation_enabled: bool = False,
    autonomy: str = "review",
    config_name: str = "defaults",
) -> dict:
    """Create a minimal job dict for testing."""
    resolved_config = {
        "agent": {
            "agent_id": config_name,
            "autonomy": autonomy,
            "verification": {
                "enabled": verification_enabled,
                "critic_config": "critic",
                "max_rounds": 3,
            },
            "curator": {
                "enabled": curation_enabled,
                "curator_config": "curator",
            },
        },
        "model_family": "openai",
    }

    job = {
        "id": "test-job-1",
        "status": status,
        "config_name": config_name,
        "parent_job_id": parent_job_id,
        "resolved_config": resolved_config,
        "description": "Test job description",
        "context": {},
        "project_id": None,
    }
    if freeze_data is not None:
        job["freeze_data"] = freeze_data
    return job


# =============================================================================
# Config resolution tests
# =============================================================================


class TestGetVerificationConfig:
    """Test reading verification config from resolved_config."""

    def test_standard_path(self):
        job = make_job(verification_enabled=True)
        vc = get_verification_config(job)
        assert vc["enabled"] is True
        assert vc["critic_config"] == "critic"
        assert vc["max_rounds"] == 3

    def test_missing_resolved_config(self):
        job = {"id": "x", "resolved_config": None}
        assert get_verification_config(job) == {}

    def test_string_resolved_config(self):
        rc = json.dumps({
            "agent": {"verification": {"enabled": True, "critic_config": "critic"}},
        })
        job = {"id": "x", "resolved_config": rc}
        vc = get_verification_config(job)
        assert vc["enabled"] is True

    def test_top_level_fallback(self):
        """When verification is at top level (not nested in agent)."""
        job = {
            "id": "x",
            "resolved_config": {
                "verification": {"enabled": True, "max_rounds": 5},
            },
        }
        vc = get_verification_config(job)
        assert vc["enabled"] is True
        assert vc["max_rounds"] == 5

    def test_no_verification_key(self):
        job = {"id": "x", "resolved_config": {"agent": {}}}
        assert get_verification_config(job) == {}

    def test_is_verification_enabled(self):
        assert is_verification_enabled(make_job(verification_enabled=True)) is True
        assert is_verification_enabled(make_job(verification_enabled=False)) is False
        assert is_verification_enabled({"id": "x", "resolved_config": None}) is False


class TestGetCurationConfig:
    """Test reading curation config from resolved_config."""

    def test_standard_path(self):
        job = make_job(curation_enabled=True)
        cc = get_curation_config(job)
        assert cc["enabled"] is True

    def test_is_curation_enabled(self):
        assert is_curation_enabled(make_job(curation_enabled=True)) is True
        assert is_curation_enabled(make_job(curation_enabled=False)) is False


class TestGetAutonomyLevel:
    """Test reading autonomy from resolved_config."""

    def test_standard_path(self):
        assert get_autonomy_level(make_job(autonomy="full")) == "full"
        assert get_autonomy_level(make_job(autonomy="review")) == "review"
        assert get_autonomy_level(make_job(autonomy="partial")) == "partial"

    def test_default(self):
        job = {"id": "x", "resolved_config": {"agent": {}}}
        assert get_autonomy_level(job) == "review"


# =============================================================================
# Freeze data tests
# =============================================================================


class TestIsJobCompletionFreeze:
    """Test detecting job completion vs phase boundary freezes."""

    def test_job_complete(self):
        job = make_job(freeze_data={"freeze_type": "job_complete", "summary": "done"})
        assert is_job_completion_freeze(job) is True

    def test_job_completed_status(self):
        """Full autonomy format: status='job_completed' instead of freeze_type."""
        job = make_job(freeze_data={"status": "job_completed"})
        assert is_job_completion_freeze(job) is True

    def test_phase_boundary(self):
        job = make_job(freeze_data={"freeze_type": "phase_boundary"})
        assert is_job_completion_freeze(job) is False

    def test_no_freeze_data(self):
        job = make_job()
        assert is_job_completion_freeze(job) is False

    def test_string_freeze_data(self):
        job = make_job()
        job["freeze_data"] = json.dumps({"freeze_type": "job_complete"})
        assert is_job_completion_freeze(job) is True


# =============================================================================
# Status determination tests
# =============================================================================


class TestDetermineJobStatus:
    """Test the status decision tree."""

    def test_error_returns_failed(self):
        job = make_job()
        result = {"error": {"message": "something broke"}}
        status, err = determine_job_status(job, result)
        assert status == "failed"
        assert err == "something broke"

    def test_error_string(self):
        job = make_job()
        result = {"error": "plain string error"}
        status, err = determine_job_status(job, result)
        assert status == "failed"
        assert err == "plain string error"

    def test_not_stopped_returns_none(self):
        job = make_job()
        result = {"should_stop": False}
        status, err = determine_job_status(job, result)
        assert status is None
        assert err is None

    def test_critic_job_skipped(self):
        """Critic jobs (with parent_job_id) should not have status overridden."""
        job = make_job(parent_job_id="parent-1")
        result = {"should_stop": True, "goal_achieved": True}
        status, err = determine_job_status(job, result)
        assert status is None
        assert err is None

    def test_goal_achieved_verification_enabled(self):
        job = make_job(
            verification_enabled=True,
            freeze_data={"freeze_type": "job_complete"},
        )
        result = {"should_stop": True, "goal_achieved": True}
        status, err = determine_job_status(job, result)
        assert status == "reviewing"

    def test_goal_achieved_verification_disabled(self):
        """Full autonomy, no verification — graph already set completed."""
        job = make_job(verification_enabled=False)
        result = {"should_stop": True, "goal_achieved": True}
        status, err = determine_job_status(job, result)
        assert status is None  # No change needed

    def test_job_completion_freeze_verification_enabled(self):
        """Non-full autonomy, freeze_type=job_complete, verification enabled."""
        job = make_job(
            verification_enabled=True,
            freeze_data={"freeze_type": "job_complete"},
        )
        result = {"should_stop": True, "goal_achieved": False}
        status, err = determine_job_status(job, result)
        assert status == "reviewing"

    def test_job_completion_freeze_no_verification(self):
        """Non-full autonomy, freeze_type=job_complete, no verification."""
        job = make_job(
            verification_enabled=False,
            freeze_data={"freeze_type": "job_complete"},
        )
        result = {"should_stop": True, "goal_achieved": False}
        status, err = determine_job_status(job, result)
        assert status == "pending_review"

    def test_phase_boundary(self):
        """Phase boundary freeze — always pending_review."""
        job = make_job(freeze_data={"freeze_type": "phase_boundary"})
        result = {"should_stop": True, "goal_achieved": False}
        status, err = determine_job_status(job, result)
        assert status == "pending_review"

    def test_no_freeze_data_not_goal_achieved(self):
        """Stopped without goal or freeze data — pending_review."""
        job = make_job(verification_enabled=True)
        result = {"should_stop": True, "goal_achieved": False}
        status, err = determine_job_status(job, result)
        assert status == "pending_review"


# =============================================================================
# Template formatting tests
# =============================================================================


class TestFormatVerificationInstructions:
    """Test verification instructions template formatting."""

    def test_basic_formatting(self):
        freeze_data = {
            "deliverables": ["output/report.md", "output/data.json"],
            "summary": "Completed the analysis",
            "confidence": 0.85,
        }
        result = format_verification_instructions(
            job_id="test-123",
            description="Analyze the dataset",
            freeze_data=freeze_data,
            config_name="developer",
        )
        assert result is not None
        assert "test-123" in result
        assert "developer" in result
        assert "Analyze the dataset" in result
        assert "output/report.md" in result
        assert "output/data.json" in result
        assert "Completed the analysis" in result
        assert "85%" in result

    def test_no_deliverables(self):
        freeze_data = {"summary": "Done", "confidence": 0.5}
        result = format_verification_instructions(
            job_id="test-456",
            description="Do something",
            freeze_data=freeze_data,
            config_name="defaults",
        )
        assert result is not None
        assert "no deliverables listed" in result

    def test_template_not_found(self):
        with patch(
            "orchestrator.services.completion._REPO_ROOT",
            Path("/nonexistent"),
        ):
            result = format_verification_instructions(
                job_id="x",
                description="x",
                freeze_data={},
                config_name="x",
            )
            assert result is None
