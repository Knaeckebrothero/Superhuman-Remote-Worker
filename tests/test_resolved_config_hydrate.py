"""Agent-side hydration of an orchestrator-resolved config blob.

``UniversalAgent.from_resolved`` is the executor half of the contract: it takes
the frozen blob the orchestrator produced (``serialize_resolved_config`` shape)
and rebuilds the ``AgentConfig`` with the resolved prompts/instructions inline —
no disk or DB resolution. Together with ``resolve_config`` (Phase 1) this is the
full round trip the migration depends on.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.services.config_resolver import resolve_config
from src.agent import UniversalAgent
from src.api.models import JobResumeRequest, JobStartRequest
from src.core.loader import (
    SubagentsConfig,
    get_all_tool_names,
    load_agent_config,
    resolve_config_path,
    serialize_resolved_config,
)


def test_job_start_request_carries_resolved_config():
    """The blob travels to the agent on JobStartRequest.resolved_config; it
    defaults to None so a flag-off dispatch is unchanged (fallback)."""
    assert JobStartRequest(job_id="j", description="d").resolved_config is None
    blob = {"agent": {"agent_id": "x"}}
    req = JobStartRequest(job_id="j", description="d", resolved_config=blob)
    assert req.resolved_config == blob


def test_job_resume_request_carries_optional_resolved_config():
    """Older orchestrators may omit the additive resume field."""
    assert JobResumeRequest(job_id="j").resolved_config is None
    blob = {"agent": {"agent_id": "developer"}}
    assert JobResumeRequest(job_id="j", resolved_config=blob).resolved_config == blob


def test_from_resolved_round_trips():
    p, d = resolve_config_path("persistent_defaults")
    blob = serialize_resolved_config(load_agent_config(p, d), model="m")

    agent = UniversalAgent.from_resolved(blob)

    assert agent.config.agent_id == blob["agent"]["agent_id"]
    # Resolved prompt/instruction text is seeded for the render path.
    assert "_resolved_prompts" in agent.config.extra
    assert "_resolved_instructions" in agent.config.extra


def test_from_resolved_carries_expert_persona_and_marker():
    """End-to-end Phase 1 → Phase 2: the orchestrator resolves a DB expert and
    the agent hydrates it — persona text and the decision-7 fence marker land in
    config.extra so the render path fences the persona."""
    blob = resolve_config(
        base_config_name="persistent_defaults",
        expert_row={
            "expert_type": "session",
            "name": "pirate",
            "config": {"llm": {"model": "gemma-4-moe"}},
            "prompts": {"persona": "PIRATE-PERSONA"},
        },
        expert_type="session",
    )

    agent = UniversalAgent.from_resolved(blob)

    assert agent.config.extra.get("_persona_source") == "db"
    assert agent.config.extra["_resolved_prompts"].get("persona") == "PIRATE-PERSONA"
    assert agent.config.llm.model == "gemma-4-moe"


@pytest.mark.asyncio
async def test_resume_delivery_blob_wins_over_worker_base_database_snapshot():
    """The delivered expert config includes the resume-scoped credentials.

    A database snapshot is deliberately present to prove that resume hydration
    does not consume the secret-free snapshot first and then ignore the wire
    blob. The assertions pin the effective developer tool set that was lost in
    the live incident.
    """
    worker_path, worker_dir = resolve_config_path("worker_base")
    worker_config = load_agent_config(worker_path, worker_dir)
    worker_blob = serialize_resolved_config(worker_config)
    developer_blob = resolve_config(base_config_name="developer")

    agent = UniversalAgent.__new__(UniversalAgent)
    agent.config = worker_config
    get_db_config = AsyncMock(return_value=worker_blob)
    agent.postgres_conn = SimpleNamespace(
        jobs=SimpleNamespace(get_resolved_config=get_db_config)
    )

    hydrated = await agent._hydrate_dispatched_config(
        "00000000-0000-0000-0000-000000000001",
        {"resolved_config": developer_blob},
        resume=True,
    )

    assert hydrated is True
    assert agent.config.agent_id == "developer"
    assert set(agent.config.tools.shell) == {
        "run_command",
        "cancel_command",
        "shell_read",
    }
    assert {"run_command", "cancel_command", "shell_read"} <= set(
        get_all_tool_names(agent.config)
    )
    get_db_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_without_delivery_blob_keeps_database_fallback():
    """No resolved_config on the wire preserves the older resume path."""
    worker_path, worker_dir = resolve_config_path("worker_base")
    worker_config = load_agent_config(worker_path, worker_dir)
    developer_blob = resolve_config(base_config_name="developer")

    agent = UniversalAgent.__new__(UniversalAgent)
    agent.config = worker_config
    get_db_config = AsyncMock(return_value=developer_blob)
    agent.postgres_conn = SimpleNamespace(
        jobs=SimpleNamespace(get_resolved_config=get_db_config)
    )

    hydrated = await agent._hydrate_dispatched_config(
        "00000000-0000-0000-0000-000000000002", {}, resume=True
    )

    assert hydrated is True
    assert agent.config.agent_id == "developer"
    assert "run_command" in get_all_tool_names(agent.config)
    get_db_config.assert_awaited_once()


def test_from_resolved_hydrates_a_materialised_roster():
    """U1 WP3: the orchestrator materialises ``subagents.roster`` into the
    blob; the agent hydrates it as ``config.subagents`` (a parsed field, not
    ``extra``) with every entry a ready-to-parse child config."""
    blob = resolve_config(
        base_config_name="worker_base",
        expert_row={
            "expert_type": "worker",
            "name": "lead",
            "config": {
                "llm": {"model": "claude-opus-4-1"},
                "subagents": {
                    "default": "explorer",
                    "roster": {
                        "explorer": {"$ref": "subagents/explorer"},
                        "implementer": {
                            "description": "Implements one bounded change.",
                            "tools": {"workspace": ["read_file", "write_file"]},
                        },
                    },
                },
            },
            "prompts": {},
        },
        expert_type="worker",
    )
    assert set(blob["agent"]["subagents"]["roster"]) == {"explorer", "implementer"}

    agent = UniversalAgent.from_resolved(blob)

    subagents = agent.config.subagents
    assert isinstance(subagents, SubagentsConfig)
    assert subagents.default == "explorer"
    assert set(subagents.roster) == {"explorer", "implementer"}
    assert subagents.roster["explorer"]["_ref_kind"] == "library"
    assert subagents.roster["implementer"]["tools"]["workspace"] == [
        "read_file",
        "write_file",
    ]
    # Both children inherited the parent's model at resolve time.
    assert subagents.roster["explorer"]["llm"]["model"] == "claude-opus-4-1"
    assert "subagents" not in agent.config.extra
    # And each entry is a config in its own right (what the roster runtime parses).
    from src.core.loader import load_agent_config_from_dict

    child = load_agent_config_from_dict(subagents.roster["implementer"])
    assert child.agent_id == "implementer"
    assert child.tools.workspace == ["read_file", "write_file"]
    assert child.interactive.permission_mode == "autonomous"
