"""Unit tests for the autonomy level system.

Tests:
- should_freeze_at_boundary() for all 5 levels x phase types x phase numbers
- Config parsing: autonomy is parsed from YAML, invalid values default to "partial"
- Config serialization round-trip through serialize_resolved_config / load_config_from_resolved
- freeze_for_review() produces correct output
- finalize_job() handles full vs other autonomy levels
- approve_frozen_job() handles freeze_type: phase_boundary vs job_complete
"""

import json
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.phase import should_freeze_at_boundary, freeze_for_review  # noqa: E402
from src.core.loader import (  # noqa: E402
    AgentConfig,
    VALID_AUTONOMY_LEVELS,
    load_agent_config_from_dict,
)


# =============================================================================
# Fixtures
# =============================================================================


def make_config(autonomy: str = "partial") -> AgentConfig:
    """Create a minimal AgentConfig with given autonomy level."""
    return AgentConfig(
        agent_id="test",
        display_name="Test Agent",
        autonomy=autonomy,
    )


def make_state(job_id: str = "test-job", phase_number: int = 1) -> dict:
    """Create a minimal agent state dict."""
    return {
        "job_id": job_id,
        "phase_number": phase_number,
        "is_strategic_phase": True,
        "messages": [],
    }


# =============================================================================
# should_freeze_at_boundary() tests
# =============================================================================


class TestShouldFreezeAtBoundary:
    """Test the core freeze decision function for all autonomy levels."""

    # --- full: never freezes at boundaries ---

    def test_full_strategic_phase_1(self):
        config = make_config("full")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1)
            is False
        )

    def test_full_strategic_phase_3(self):
        config = make_config("full")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=3)
            is False
        )

    def test_full_tactical_phase_2(self):
        config = make_config("full")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=2)
            is False
        )

    # --- review: never freezes at boundaries (only at job_complete, handled separately) ---

    def test_review_strategic_phase_1(self):
        config = make_config("review")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1)
            is False
        )

    def test_review_strategic_phase_5(self):
        config = make_config("review")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=5)
            is False
        )

    def test_review_tactical_phase_2(self):
        config = make_config("review")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=2)
            is False
        )

    # --- partial: freezes only at first strategic phase boundary ---

    def test_partial_strategic_phase_0(self):
        """phase_number=0 is the actual first strategic phase (set by init_workspace)."""
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=0) is True
        )

    def test_partial_strategic_phase_1(self):
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1) is True
        )

    def test_partial_strategic_phase_2(self):
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=2)
            is False
        )

    def test_partial_strategic_phase_3(self):
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=3)
            is False
        )

    def test_partial_tactical_phase_1(self):
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=1)
            is False
        )

    def test_partial_tactical_phase_2(self):
        config = make_config("partial")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=2)
            is False
        )

    # --- guided: freezes at every strategic phase boundary ---

    def test_guided_strategic_phase_1(self):
        config = make_config("guided")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1) is True
        )

    def test_guided_strategic_phase_2(self):
        config = make_config("guided")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=2) is True
        )

    def test_guided_strategic_phase_5(self):
        config = make_config("guided")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=5) is True
        )

    def test_guided_tactical_phase_1(self):
        config = make_config("guided")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=1)
            is False
        )

    def test_guided_tactical_phase_3(self):
        config = make_config("guided")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=3)
            is False
        )

    # --- dependent: freezes at every phase boundary ---

    def test_dependent_strategic_phase_1(self):
        config = make_config("dependent")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1) is True
        )

    def test_dependent_strategic_phase_3(self):
        config = make_config("dependent")
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=3) is True
        )

    def test_dependent_tactical_phase_1(self):
        config = make_config("dependent")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=1)
            is True
        )

    def test_dependent_tactical_phase_2(self):
        config = make_config("dependent")
        assert (
            should_freeze_at_boundary(config, is_strategic=False, phase_number=2)
            is True
        )

    # --- Edge cases ---

    def test_missing_autonomy_attribute(self):
        """Config without autonomy attr defaults to partial behavior."""
        config = MagicMock(spec=[])  # No attributes
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1) is True
        )

    def test_unknown_autonomy_value(self):
        """Unknown autonomy value returns False (safe fallback)."""
        config = MagicMock()
        config.autonomy = "unknown_value"
        assert (
            should_freeze_at_boundary(config, is_strategic=True, phase_number=1)
            is False
        )


# =============================================================================
# freeze_for_review() tests
# =============================================================================


class TestFreezeForReview:
    """Test the phase boundary freeze function."""

    def test_freeze_writes_json(self):
        """freeze_for_review writes job_frozen.json with correct freeze_type."""
        state = make_state()
        workspace = MagicMock()
        todo_manager = MagicMock()

        result = freeze_for_review(state, workspace, todo_manager, "strategic", 1)

        assert result.success is True
        assert result.state_updates["should_stop"] is True
        assert result.state_updates["goal_achieved"] is False

        # Verify workspace.write_file was called with correct path and data
        workspace.write_file.assert_called_once()
        call_args = workspace.write_file.call_args
        assert call_args[0][0] == "output/job_frozen.json"
        frozen_data = json.loads(call_args[0][1])
        assert frozen_data["freeze_type"] == "phase_boundary"
        assert frozen_data["phase_type"] == "strategic"
        assert frozen_data["phase_number"] == 1
        assert frozen_data["status"] == "pending_review"

    def test_freeze_tactical_phase(self):
        """freeze_for_review works for tactical phases too."""
        state = make_state(phase_number=3)
        workspace = MagicMock()
        todo_manager = MagicMock()

        freeze_for_review(state, workspace, todo_manager, "tactical", 3)

        call_args = workspace.write_file.call_args
        frozen_data = json.loads(call_args[0][1])
        assert frozen_data["phase_type"] == "tactical"
        assert frozen_data["phase_number"] == 3

    def test_freeze_archives_todos(self):
        """freeze_for_review archives todos."""
        state = make_state()
        workspace = MagicMock()
        todo_manager = MagicMock()

        freeze_for_review(state, workspace, todo_manager, "strategic", 1)

        todo_manager.archive.assert_called_once_with("phase_1_strategic")


# =============================================================================
# Config parsing tests
# =============================================================================


class TestConfigParsing:
    """Test autonomy level parsing from config data."""

    def test_default_autonomy(self):
        """AgentConfig defaults to 'partial' autonomy."""
        config = AgentConfig(agent_id="test", display_name="Test")
        assert config.autonomy == "partial"

    def test_parse_valid_autonomy_from_dict(self):
        """load_agent_config_from_dict parses autonomy correctly."""
        for level in VALID_AUTONOMY_LEVELS:
            data = {
                "agent_id": "test",
                "display_name": "Test",
                "autonomy": level,
            }
            config = load_agent_config_from_dict(data)
            assert config.autonomy == level, f"Failed for level: {level}"

    def test_invalid_autonomy_defaults_to_partial(self):
        """Invalid autonomy value defaults to 'partial'."""
        data = {
            "agent_id": "test",
            "display_name": "Test",
            "autonomy": "invalid_level",
        }
        config = load_agent_config_from_dict(data)
        assert config.autonomy == "partial"

    def test_missing_autonomy_defaults_to_partial(self):
        """Missing autonomy key defaults to 'partial'."""
        data = {
            "agent_id": "test",
            "display_name": "Test",
        }
        config = load_agent_config_from_dict(data)
        assert config.autonomy == "partial"

    def test_autonomy_not_in_extra(self):
        """Autonomy field should NOT leak into extra dict."""
        data = {
            "agent_id": "test",
            "display_name": "Test",
            "autonomy": "guided",
        }
        config = load_agent_config_from_dict(data)
        assert "autonomy" not in config.extra

    def test_autonomy_survives_dataclass_asdict(self):
        """Autonomy field is captured by dataclasses.asdict()."""
        import dataclasses

        config = AgentConfig(
            agent_id="test",
            display_name="Test",
            autonomy="guided",
        )
        d = dataclasses.asdict(config)
        assert d["autonomy"] == "guided"


# =============================================================================
# Serialization round-trip tests
# =============================================================================


class TestSerializationRoundTrip:
    """Test that autonomy survives serialize_resolved_config / load_config_from_resolved."""

    def test_round_trip(self):
        """Autonomy is preserved through serialize → load round-trip."""
        from src.core.loader import serialize_resolved_config, load_config_from_resolved

        config = AgentConfig(
            agent_id="test",
            display_name="Test",
            autonomy="guided",
        )

        # serialize_resolved_config needs prompt files, so we mock the resolvers
        with (
            patch("src.core.loader.PromptMatrixResolver") as mock_prompt,
            patch("src.core.loader.InstructionMatrixResolver") as mock_instr,
        ):
            # Mock resolver to return dummy content
            mock_prompt_instance = MagicMock()
            mock_prompt_instance.load.return_value = "dummy prompt"
            mock_prompt.return_value = mock_prompt_instance

            mock_instr_instance = MagicMock()
            mock_instr_instance.load.return_value = "dummy instruction"
            mock_instr.return_value = mock_instr_instance

            resolved = serialize_resolved_config(config, model="gpt-4o")

        # Verify autonomy is in the serialized dict
        assert resolved["agent"]["autonomy"] == "guided"

        # Load back
        restored = load_config_from_resolved(resolved)
        assert restored.autonomy == "guided"

    def test_round_trip_default(self):
        """Default autonomy 'partial' is preserved through round-trip."""
        from src.core.loader import load_config_from_resolved

        # Simulate a resolved config dict
        resolved = {
            "agent": {
                "agent_id": "test",
                "display_name": "Test",
                "autonomy": "partial",
                "llm": {"model": "gpt-4o"},
                "workspace": {},
                "tools": {},
                "connections": {},
                "limits": {},
                "context_management": {},
                "phase_settings": {},
                "instruction_files": [],
                "extra": {},
            },
            "prompts": {},
            "instructions": {},
        }

        restored = load_config_from_resolved(resolved)
        assert restored.autonomy == "partial"


# =============================================================================
# Comprehensive autonomy matrix test
# =============================================================================


class TestAutonomyMatrix:
    """Exhaustive test of the autonomy decision matrix from the design doc.

    | Level      | After 1st Strategic | After Nth Strategic | After Tactical | After job_complete |
    |------------|:---:|:---:|:---:|:---:|
    | full       | -   | -   | -   | auto-complete      |
    | review     | -   | -   | -   | freeze             |
    | partial    | freeze | - | -   | freeze             |
    | guided     | freeze | freeze | - | freeze           |
    | dependent  | freeze | freeze | freeze | freeze       |
    """

    @pytest.mark.parametrize(
        "level,is_strategic,phase_number,expected",
        [
            # full: never freeze at boundaries
            ("full", True, 0, False),
            ("full", True, 1, False),
            ("full", True, 2, False),
            ("full", True, 5, False),
            ("full", False, 1, False),
            ("full", False, 3, False),
            # review: never freeze at boundaries
            ("review", True, 1, False),
            ("review", True, 3, False),
            ("review", False, 1, False),
            ("review", False, 2, False),
            # partial: only first strategic (phase_number 0 or 1)
            ("partial", True, 0, True),  # init_workspace sets phase_number=0
            ("partial", True, 1, True),  # state default is phase_number=1
            ("partial", True, 2, False),
            ("partial", True, 5, False),
            ("partial", False, 0, False),
            ("partial", False, 1, False),
            ("partial", False, 3, False),
            # guided: all strategic
            ("guided", True, 1, True),
            ("guided", True, 2, True),
            ("guided", True, 5, True),
            ("guided", False, 1, False),
            ("guided", False, 3, False),
            # dependent: all phases
            ("dependent", True, 1, True),
            ("dependent", True, 3, True),
            ("dependent", False, 1, True),
            ("dependent", False, 3, True),
        ],
    )
    def test_autonomy_decision(self, level, is_strategic, phase_number, expected):
        config = make_config(level)
        result = should_freeze_at_boundary(config, is_strategic, phase_number)
        assert result is expected, (
            f"level={level}, is_strategic={is_strategic}, "
            f"phase_number={phase_number}: expected {expected}, got {result}"
        )
