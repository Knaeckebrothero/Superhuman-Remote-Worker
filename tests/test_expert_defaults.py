"""Mode-specific virtual framework-default expert details."""

from unittest.mock import AsyncMock

import pytest

import orchestrator.main as orchestrator_main
from orchestrator.main import _load_expert_detail


@pytest.mark.asyncio
async def test_session_defaults_use_persistent_base():
    detail = await _load_expert_detail("defaults", defaults_type="session")

    config = detail["config"]
    assert config["agent_id"] == "session_base"
    assert config["llm"]["max_retries"] == 3
    assert config["tools"]["communication"] == []
    assert config["tools"]["delegation"] == []


@pytest.mark.asyncio
async def test_unspecified_defaults_type_remains_worker_for_compatibility():
    detail = await _load_expert_detail("defaults")

    config = detail["config"]
    assert config["agent_id"] == "worker_base"
    assert config["llm"]["max_retries"] == 0


@pytest.mark.asyncio
async def test_db_expert_detail_includes_base_tools_and_settings_matrix(monkeypatch):
    expert_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
    monkeypatch.setattr(
        orchestrator_main.postgres_db,
        "get_expert_by_id",
        AsyncMock(
            return_value={
                "id": expert_id,
                "display_name": "DB Worker",
                "description": "",
                "icon": "smart_toy",
                "color": "#6B7280",
                "tags": [],
                "expert_type": "worker",
                "is_global": False,
                "managed_key": None,
                "config": {"tools": {"shell": []}},
                "prompts": {},
            }
        ),
    )

    detail = await _load_expert_detail(expert_id)

    assert detail["defaults_tools"]["workspace"]
    assert detail["defaults_tools"]["shell"] == []
    assert "gpt-5.6" in detail["settings_matrix"]
