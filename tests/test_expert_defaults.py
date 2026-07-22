"""Mode-specific virtual framework-default expert details."""

import pytest

from orchestrator.main import _load_expert_detail


@pytest.mark.asyncio
async def test_session_defaults_use_persistent_base():
    detail = await _load_expert_detail("defaults", defaults_type="session")

    config = detail["config"]
    assert config["agent_id"] == "persistent"
    assert config["llm"]["max_retries"] == 3
    assert config["tools"]["communication"] == []
    assert config["tools"]["delegation"] == []


@pytest.mark.asyncio
async def test_unspecified_defaults_type_remains_worker_for_compatibility():
    detail = await _load_expert_detail("defaults")

    config = detail["config"]
    assert config["agent_id"] == "default"
    assert config["llm"]["max_retries"] == 0
