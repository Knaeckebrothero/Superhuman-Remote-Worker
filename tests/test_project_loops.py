"""Unit tests for the project self-improvement loop service.

Focus: ``create_loop_job`` builds the right per-job ``config_override`` — the
"bare" lifecycle flags plus the per-loop ``model`` and ``workspace_backend``
overrides (the latter mirrors the former; it pins every spawned job's workspace
tier so e.g. a loop can run all of its roles on a VM).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.project_loops import (
    build_loop_description,
    build_loop_kickoff,
    create_loop_job,
)


def _loop(**over):
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "project_id": None,  # None → skip datasource linking in create_loop_job
        "owner_id": None,
        "goal": "Build a thing",
        "acceptance_criteria": None,
        "user_prompt": None,
        "model": None,
        "remaining_iterations": 5,
        "run_until": None,
        "workspace_backend": None,
    }
    base.update(over)
    return base


def _db():
    db = AsyncMock()
    db.create_job = AsyncMock(return_value={"id": "job-1"})
    # Keep the optional resolution paths inert regardless of EXPERTS_DB_ENABLED.
    db.list_experts_visible = AsyncMock(return_value=[])
    db.list_project_datasources = AsyncMock(return_value=[])
    return db


def _config_override(db):
    return db.create_job.call_args.kwargs["config_override"]


@pytest.mark.asyncio
async def test_workspace_backend_injected_when_set():
    db = _db()
    await create_loop_job(
        db, _loop(workspace_backend="vm"), role="developer", iteration=2
    )
    assert _config_override(db)["workspace"] == {"backend": "vm"}


@pytest.mark.asyncio
async def test_no_workspace_key_when_backend_unset():
    db = _db()
    await create_loop_job(db, _loop(), role="scholar", iteration=1)
    assert "workspace" not in _config_override(db)


@pytest.mark.asyncio
async def test_model_and_backend_coexist_with_bare_invariants():
    db = _db()
    await create_loop_job(
        db,
        _loop(model="openrouter/minimax/minimax-m3", workspace_backend="vm"),
        role="developer",
        iteration=3,
    )
    co = _config_override(db)
    assert co["llm"] == {"model": "openrouter/minimax/minimax-m3"}
    assert co["workspace"] == {"backend": "vm"}
    # The override must not clobber the loop's "bare" lifecycle invariants.
    assert co["verification"] == {"enabled": False}
    assert co["scholar"] == {"enabled": False}
    assert co["autonomy"] == "full"
    assert co["memory"] == {"required": True}


class TestProductQaLoopWiring:
    """Phase 0 wiring for the product-qa role (docs/features/loop_parallel_stages.md):
    the loop must give it a QA-specific kickoff (not the generic default) and the
    Critic must be told to triage QA findings alongside Scholar proposals."""

    def test_product_qa_gets_specific_role_block_not_default(self) -> None:
        kick = build_loop_kickoff(_loop(), role="product-qa", iteration=4)
        assert "YOUR ROLE THIS ITERATION — PRODUCT-QA:" in kick
        # QA-specific, not the "advance the goal acting as 'product-qa'" default.
        assert "advance the goal acting as" not in kick
        # Core QA behaviors: audits shipped product, files findings, doesn't fix.
        assert "qa-finding" in kick
        low = kick.lower()
        assert "do not fix" in low or "do not fix anything" in low
        assert "scholar" in low  # counterpart framing / be-fair-to-scholar

    def test_product_qa_description_is_specific(self) -> None:
        desc = build_loop_description(_loop(), role="product-qa", iteration=4)
        assert "PRODUCT-QA" in desc
        assert "audit" in desc.lower()

    def test_critic_block_triages_both_streams(self) -> None:
        kick = build_loop_kickoff(_loop(), role="critic", iteration=5)
        low = kick.lower()
        # Critic must see BOTH candidate streams and treat fix-vs-build as a choice.
        assert "proposal" in low
        assert "qa-finding" in kick
        assert "first-class" in low
