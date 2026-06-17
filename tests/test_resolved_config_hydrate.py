"""Agent-side hydration of an orchestrator-resolved config blob.

``UniversalAgent.from_resolved`` is the executor half of the contract: it takes
the frozen blob the orchestrator produced (``serialize_resolved_config`` shape)
and rebuilds the ``AgentConfig`` with the resolved prompts/instructions inline —
no disk or DB resolution. Together with ``resolve_config`` (Phase 1) this is the
full round trip the migration depends on.
"""

from orchestrator.services.config_resolver import resolve_config
from src.agent import UniversalAgent
from src.api.models import JobStartRequest
from src.core.loader import (
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
