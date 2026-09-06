"""Prompt resolution shared by agent and orchestrator memory runtimes.

This module intentionally stays below ``agent.api``.  The orchestrator's
always-on session-memory drain imports it during startup, where agent-only
runtime dependencies are not installed.
"""

from __future__ import annotations

import logging

from shared.runtime.core.loader import AgentConfig, load_auxiliary_prompt

logger = logging.getLogger(__name__)


def resolve_memory_extraction_prompt(config: AgentConfig) -> str:
    """Load the memory-extraction prompt through the prompt matrix."""

    aux_model = (
        config.auxiliary.model
        or config.llm.get_phase_config("summarization").model
        or config.llm.model
    )
    try:
        return load_auxiliary_prompt(config, "memory_extraction", model=aux_model)
    except Exception as exc:
        logger.warning(
            "Memory extraction prompt could not be loaded — extraction "
            "will run without instructions: %s",
            exc,
        )
        return ""


def resolve_citation_verification_prompt(config: AgentConfig) -> str:
    """Load the citation-verification prompt through the prompt matrix."""

    aux_model = (
        config.auxiliary.model
        or config.llm.get_phase_config("summarization").model
        or config.llm.model
    )
    try:
        return load_auxiliary_prompt(config, "citation_verification", model=aux_model)
    except Exception as exc:
        logger.warning(
            "Citation verification prompt could not be loaded — verification "
            "will run without instructions: %s",
            exc,
        )
        return ""


__all__ = [
    "resolve_citation_verification_prompt",
    "resolve_memory_extraction_prompt",
]
