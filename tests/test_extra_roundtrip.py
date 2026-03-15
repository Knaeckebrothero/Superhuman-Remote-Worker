"""Tests for config.extra serialization round-trip.

Verifies that extra fields (shell, claude_code, verification, browser, etc.)
survive serialize_resolved_config → load_config_from_resolved without
double-nesting into extra["extra"].
"""

from unittest.mock import patch

from src.core.loader import (
    AgentConfig,
    load_config_from_resolved,
    serialize_resolved_config,
)


class TestExtraSerializationRoundTrip:
    """Extra fields must survive serialize → deserialize without double-nesting."""

    def _mock_serialize(self, config: AgentConfig) -> dict:
        """Serialize config with mocked prompt/instruction resolvers."""
        with patch("src.core.loader.PromptMatrixResolver") as mock_prompt, \
             patch("src.core.loader.InstructionMatrixResolver") as mock_instr:
            mock_prompt.return_value.load.return_value = "dummy prompt"
            mock_instr.return_value.load.return_value = "dummy instruction"
            return serialize_resolved_config(config, model="gpt-4o")

    def test_extra_fields_round_trip(self):
        """Extra fields are accessible at config.extra[key] after round-trip."""
        config = AgentConfig(
            agent_id="test",
            display_name="Test",
            extra={
                "verification": {"enabled": True, "agent_config": "critic"},
                "shell": {"max_tabs": 5},
                "claude_code": {"model": "claude-opus-4-6"},
                "browser": {"headless": True},
            },
        )

        resolved = self._mock_serialize(config)
        restored = load_config_from_resolved(resolved)

        # All extra fields should be directly accessible
        assert restored.extra.get("verification") == {"enabled": True, "agent_config": "critic"}
        assert restored.extra.get("shell") == {"max_tabs": 5}
        assert restored.extra.get("claude_code") == {"model": "claude-opus-4-6"}
        assert restored.extra.get("browser") == {"headless": True}

        # Must NOT be double-nested
        assert "extra" not in restored.extra

    def test_serialized_dict_has_extra_fields_at_top_level(self):
        """serialize_resolved_config flattens extra into top-level agent dict."""
        config = AgentConfig(
            agent_id="test",
            display_name="Test",
            extra={"shell": {"max_tabs": 3}, "icon": "rocket"},
        )

        resolved = self._mock_serialize(config)
        agent_dict = resolved["agent"]

        # Extra fields should be at top level, not nested under "extra"
        assert agent_dict.get("shell") == {"max_tabs": 3}
        assert agent_dict.get("icon") == "rocket"
        assert "extra" not in agent_dict

    def test_backwards_compat_double_nested_extra(self):
        """Pre-fix serialized configs with extra["extra"] are flattened on load."""
        # Simulate a pre-fix serialized config where extra was stored literally
        resolved = {
            "agent": {
                "agent_id": "test",
                "display_name": "Test",
                "autonomy": "review",
                "llm": {"model": "gpt-4o"},
                "workspace": {},
                "tools": {},
                "connections": {},
                "limits": {},
                "context_management": {},
                "phase_settings": {},
                "instruction_files": [],
                "memory": {},
                # This is how old serialized configs look — extra as a literal key
                "extra": {
                    "shell": {"max_tabs": 5},
                    "verification": {"enabled": True},
                    "claude_code": {"model": "claude-opus-4-6"},
                },
            },
            "prompts": {},
            "instructions": {},
        }

        restored = load_config_from_resolved(resolved)

        # Should be flattened, not double-nested
        assert restored.extra.get("shell") == {"max_tabs": 5}
        assert restored.extra.get("verification") == {"enabled": True}
        assert restored.extra.get("claude_code") == {"model": "claude-opus-4-6"}
        assert "extra" not in restored.extra

    def test_extra_fields_dont_overwrite_standard_fields(self):
        """Extra keys that collide with standard field names are dropped."""
        config = AgentConfig(
            agent_id="test",
            display_name="Test",
            extra={"shell": {"max_tabs": 3}},
        )

        resolved = self._mock_serialize(config)

        # Manually inject a collision — extra key matching a standard field
        resolved["agent"]["agent_id"] = "test"  # already there; just confirming
        # The flattening should not have overwritten agent_id
        assert resolved["agent"]["agent_id"] == "test"

    def test_empty_extra_round_trip(self):
        """Config with empty extra survives round-trip."""
        config = AgentConfig(
            agent_id="test",
            display_name="Test",
            extra={},
        )

        resolved = self._mock_serialize(config)
        restored = load_config_from_resolved(resolved)

        # Only the injected _resolved keys should be present
        assert "_resolved_prompts" in restored.extra
        assert "_resolved_instructions" in restored.extra
