"""Regression tests for B1 — persistent memory extraction broken three ways.

See docs/issues/memory_bugs.md B1. The original bug: three call sites read
phantom attributes (``extraction_interval``, ``extraction_prompt``) off
``MemoryConfig``, which only survived in tests because MagicMock configs
fabricate any attribute. Every test here that exercises extraction wiring
uses the REAL ``MemoryConfig`` dataclass so a reintroduced phantom-attribute
access fails loudly instead of being swallowed by the non-fatal handlers.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.api.persistent_app import _handle_archive, _handle_idle_archive
from src.api.persistent_session import (
    PersistentSession,
    resolve_memory_extraction_prompt,
)
from src.core.loader import AgentConfig, MemoryConfig
from src.persistent_graph import PersistentLoopCallbacks, run_persistent_loop


def _make_callbacks(**overrides) -> PersistentLoopCallbacks:
    defaults = dict(
        get_user_input=AsyncMock(return_value="hello"),
        on_token=AsyncMock(),
        on_thinking=AsyncMock(),
        on_tool_start=AsyncMock(),
        on_tool_result=AsyncMock(),
        permission_check=AsyncMock(return_value=True),
        on_turn_start=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_error=AsyncMock(),
        check_interrupt=MagicMock(return_value=False),
        on_vm_upgrade_needed=None,
    )
    defaults.update(overrides)
    return PersistentLoopCallbacks(**defaults)


def _make_streaming_llm(content="ok"):
    llm = AsyncMock()
    llm.reasoning = None
    response = AIMessage(content=content)

    async def _astream(messages, **kw):
        yield response

    llm.astream = _astream
    llm.ainvoke = AsyncMock(return_value=response)
    return llm


def _loop_config(observer_interval: int) -> MagicMock:
    """Loop config whose memory section is the REAL MemoryConfig."""
    cfg = MagicMock()
    cfg.llm.timeout = 600
    cfg.memory = MemoryConfig(enabled=True, observer_interval=observer_interval)
    cfg.context_management.max_summary_length = 10000
    return cfg


class TestMemoryConfigContract:
    """Canary: the attrs the persistent path reads must exist on the dataclass."""

    def test_no_phantom_extraction_attrs(self):
        mc = MemoryConfig()
        assert not hasattr(mc, "extraction_interval")
        assert not hasattr(mc, "extraction_prompt")

    def test_observer_interval_is_the_real_cadence_field(self):
        assert MemoryConfig(observer_interval=3).observer_interval == 3

    def test_session_carries_prompt_field(self):
        session = PersistentSession(thread_id="t", config=MagicMock())
        assert session.memory_extraction_prompt == ""


class TestResolveMemoryExtractionPrompt:
    def test_resolves_real_prompt_through_matrix(self):
        """A bare AgentConfig resolves the actual framework prompt file."""
        cfg = AgentConfig(agent_id="t", display_name="T")
        prompt = resolve_memory_extraction_prompt(cfg)
        assert len(prompt) > 100
        assert "memory" in prompt.lower()

    def test_load_failure_returns_empty_not_raise(self):
        cfg = AgentConfig(agent_id="t", display_name="T")
        with patch(
            "src.api.persistent_session.load_auxiliary_prompt",
            side_effect=FileNotFoundError("missing"),
        ):
            assert resolve_memory_extraction_prompt(cfg) == ""


class TestLoopExtractionWiring:
    """B1 (b)+(c): the loop honors observer_interval and threads the prompt."""

    async def _run_loop(self, turns: int, observer_interval: int, prompt: str):
        count = 0

        async def _input():
            nonlocal count
            count += 1
            if count <= turns:
                return f"turn {count}"
            raise asyncio.CancelledError

        extraction = AsyncMock()
        with patch("src.services.auxiliary.extract_and_store_memories", extraction):
            await run_persistent_loop(
                llm_with_tools=_make_streaming_llm(),
                tools=[],
                context_manager=AsyncMock(
                    ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
                ),
                config=_loop_config(observer_interval),
                system_prompt="sys",
                callbacks=_make_callbacks(get_user_input=_input),
                messages=[],
                recall_store=MagicMock(),
                auxiliary_llm=MagicMock(),
                memory_extraction_prompt=prompt,
            )
            # Let the fire-and-forget extraction task run
            await asyncio.sleep(0)
        return extraction

    @pytest.mark.asyncio
    async def test_fires_on_configured_observer_interval(self):
        """interval=2 fires at turn 2 — fails if the cadence falls back to 5."""
        extraction = await self._run_loop(
            turns=2, observer_interval=2, prompt="PROMPT SENTINEL"
        )
        extraction.assert_called_once()
        kwargs = extraction.call_args.kwargs
        assert kwargs["memory_extraction_prompt"] == "PROMPT SENTINEL"
        assert kwargs["source_turn_start"] == 0
        assert kwargs["source_turn_end"] == 2

    @pytest.mark.asyncio
    async def test_interval_zero_disables_extraction(self):
        extraction = await self._run_loop(turns=3, observer_interval=0, prompt="unused")
        extraction.assert_not_called()


class TestTeardownExtraction:
    """B1 (a): session-end and idle-archive extraction actually runs.

    The session mock carries a REAL MemoryConfig — a revert to
    ``_session.config.memory.extraction_prompt`` raises AttributeError,
    gets swallowed by the handler's non-fatal except, and these assertions
    then fail because extraction was never awaited.
    """

    def _make_session(self) -> MagicMock:
        session = MagicMock()
        session.config.memory = MemoryConfig(enabled=True)
        session.memory_extraction_prompt = "TEARDOWN PROMPT"
        session.tool_context.recall_store = MagicMock()
        session.auxiliary_llm = MagicMock()
        session.messages = [HumanMessage(content="hi")]
        session.postgres_conn = None
        session.workspace_sync = None
        session.workspace_manager = None
        return session

    @pytest.mark.asyncio
    async def test_archive_runs_extraction_with_resolved_prompt(self):
        ws = AsyncMock()
        session = self._make_session()
        extraction = AsyncMock()
        with (
            patch("src.api.persistent_app._session", session),
            patch("src.api.persistent_app._thread_id", "tid"),
            patch("src.services.auxiliary.extract_and_store_memories", extraction),
        ):
            await _handle_archive(ws)

        extraction.assert_awaited_once()
        kwargs = extraction.call_args.kwargs
        assert kwargs["memory_extraction_prompt"] == "TEARDOWN PROMPT"
        assert kwargs["messages"] == session.messages

    @pytest.mark.asyncio
    async def test_idle_archive_runs_extraction_with_resolved_prompt(self):
        session = self._make_session()
        extraction = AsyncMock()
        with (
            patch("src.api.persistent_app._session", session),
            patch("src.api.persistent_app._thread_id", "tid"),
            patch("src.api.persistent_app._broadcast"),
            patch("src.api.persistent_app._update_thread_status", AsyncMock()),
            patch("src.services.auxiliary.extract_and_store_memories", extraction),
        ):
            await _handle_idle_archive()

        extraction.assert_awaited_once()
        kwargs = extraction.call_args.kwargs
        assert kwargs["memory_extraction_prompt"] == "TEARDOWN PROMPT"
