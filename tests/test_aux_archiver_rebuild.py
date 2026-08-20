"""Per-job LLM rebuilds must not orphan the auxiliary archiver wiring.

Regression tests for the lane-ab-01 bench finding: ``process_job`` wires the
audit archiver onto ``self._auxiliary_llm`` *before* ``_setup_job_workspace``
recreates the phase LLMs (every dispatched job is config-dirty — the
orchestrator injects credentials into ``config_override``), so the rebuilt
``AuxiliaryLLM`` ran every memory-extraction/assembly call with
``_archiver=None``. Real provider spend, zero ``llm_requests`` rows, nothing
metered. The fix carries the previous instance's job wiring across rebuilds.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.agent import UniversalAgent
from src.core.loader import load_agent_config


def _agent_with_worker_config():
    agent = UniversalAgent.__new__(UniversalAgent)
    cfg = load_agent_config("config/worker_base.yaml")
    # Force the reuse-summarization branch: no dedicated aux model means no
    # create_llm call, so the rebuild is exercised without any LLM factory.
    cfg.auxiliary.model = None
    agent.config = cfg
    agent._summarization_llm = MagicMock(name="summarization_llm")
    agent._auxiliary_llm = None
    agent._citation_verify_aux = None
    return agent


def test_rebuild_carries_job_wiring_forward():
    agent = _agent_with_worker_config()
    limits = MagicMock(model_max_context_tokens=100_000)

    # Boot build, then the wiring process_job performs at job start.
    agent._initialize_auxiliary_llm(agent.config.llm, limits)
    boot_instance = agent._auxiliary_llm
    archiver = MagicMock(name="archiver")
    boot_instance.set_job_context(
        archiver=archiver, job_id="job-123", agent_type="worker_base"
    )

    # The per-job recreate that used to drop the wiring.
    agent._initialize_auxiliary_llm(agent.config.llm, limits)

    rebuilt = agent._auxiliary_llm
    assert rebuilt is not boot_instance
    assert rebuilt._archiver is archiver
    assert rebuilt._job_id == "job-123"
    assert rebuilt._agent_type == "worker_base"


def test_boot_build_stays_unwired():
    agent = _agent_with_worker_config()
    limits = MagicMock(model_max_context_tokens=100_000)

    agent._initialize_auxiliary_llm(agent.config.llm, limits)

    assert agent._auxiliary_llm._archiver is None
    assert agent._auxiliary_llm._job_id is None


def test_rebuild_without_prior_wiring_stays_unwired():
    agent = _agent_with_worker_config()
    limits = MagicMock(model_max_context_tokens=100_000)

    agent._initialize_auxiliary_llm(agent.config.llm, limits)
    agent._initialize_auxiliary_llm(agent.config.llm, limits)

    assert agent._auxiliary_llm._archiver is None


def test_disabled_aux_fallback_also_carries_wiring():
    agent = _agent_with_worker_config()
    agent.config.auxiliary.enabled = False
    limits = MagicMock(model_max_context_tokens=100_000)

    agent._initialize_auxiliary_llm(agent.config.llm, limits)
    archiver = MagicMock(name="archiver")
    agent._auxiliary_llm.set_job_context(
        archiver=archiver, job_id="job-456", agent_type="worker_base"
    )

    agent._initialize_auxiliary_llm(agent.config.llm, limits)

    assert agent._auxiliary_llm._archiver is archiver
    assert agent._auxiliary_llm._job_id == "job-456"
