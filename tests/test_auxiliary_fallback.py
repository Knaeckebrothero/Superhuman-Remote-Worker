"""Auxiliary provider routing + main-model fallback.

Covers the two-part fix for
docs/issues/openrouter_auxiliary_misrouted_to_openai.md:

Part A — an OpenRouter auxiliary must not be silently routed to api.openai.com.
The provider is threaded from AuxiliaryConfig into create_llm, and create_llm
auto-detects the ``openrouter/`` model prefix when no provider is given.

Part B — when the dedicated aux model is unreachable, aux tasks fall back to
the main model (drop-in) instead of crashing the session, LOUDLY (the
aux-degraded heartbeat flag lights up), and only raise when there is no
fallback or the fallback also fails.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from src.core.loader import (
    AuxiliaryConfig,
    LLMConfig,
    _parse_auxiliary_config,
    create_llm,
)
from src.services.auxiliary import AuxiliaryLLM, CurationResult, CurateKnowledgeTask


# ---------------------------------------------------------------------------
# Part A: provider threading + auto-detect
# ---------------------------------------------------------------------------


class TestAuxiliaryProviderParsing:
    def test_provider_is_parsed_from_dict(self):
        cfg = _parse_auxiliary_config(
            {"model": "openrouter/minimax/minimax-m3", "provider": "openrouter"}
        )
        assert cfg.provider == "openrouter"

    def test_provider_defaults_to_none(self):
        cfg = _parse_auxiliary_config({"model": "gemma-4-moe"})
        assert cfg.provider is None

    def test_auxiliaryconfig_has_provider_field(self):
        # Regression: the field must exist so an injected provider survives parse.
        assert "provider" in AuxiliaryConfig.__dataclass_fields__


class TestCreateLLMProviderRouting:
    """create_llm must route by provider, and auto-detect openrouter/ prefix."""

    def _cfg(self, **kw):
        return LLMConfig(
            model=kw.get("model", "gpt-4o"),
            provider=kw.get("provider", None),
            base_url=kw.get("base_url"),
            api_key=kw.get("api_key"),
        )

    @patch("src.core.loader._create_openrouter_llm")
    @patch("src.core.loader._create_openai_llm")
    def test_openrouter_prefix_autodetected_when_provider_none(
        self, mock_openai, mock_openrouter
    ):
        # The actual bug: aux LLMConfig built without provider, model carries the
        # openrouter/ prefix -> must NOT fall through to openai (api.openai.com).
        create_llm(self._cfg(model="openrouter/minimax/minimax-m3", provider=None))
        mock_openrouter.assert_called_once()
        mock_openai.assert_not_called()

    @patch("src.core.loader._create_openrouter_llm")
    @patch("src.core.loader._create_openai_llm")
    def test_explicit_openrouter_provider_routes_to_openrouter(
        self, mock_openai, mock_openrouter
    ):
        create_llm(
            self._cfg(model="openrouter/minimax/minimax-m3", provider="openrouter")
        )
        mock_openrouter.assert_called_once()
        mock_openai.assert_not_called()

    @patch("src.core.loader._create_openrouter_llm")
    @patch("src.core.loader._create_openai_llm")
    def test_plain_model_without_provider_still_openai(
        self, mock_openai, mock_openrouter
    ):
        create_llm(self._cfg(model="gpt-4o", provider=None))
        mock_openai.assert_called_once()
        mock_openrouter.assert_not_called()

    @patch("src.core.loader._create_openrouter_llm")
    @patch("src.core.loader._create_openai_llm")
    def test_explicit_provider_wins_over_prefix(self, mock_openai, mock_openrouter):
        # An explicit provider always beats the prefix heuristic.
        create_llm(self._cfg(model="openrouter/x/y", provider="openai"))
        mock_openai.assert_called_once()
        mock_openrouter.assert_not_called()


# ---------------------------------------------------------------------------
# Part B: main-model fallback
# ---------------------------------------------------------------------------


def _mock_llm(name: str, *, result="ok", error: Exception | None = None):
    """A stand-in chat model whose ``ainvoke`` returns ``result`` or raises."""
    llm = MagicMock()
    llm.model_name = name
    if error is not None:
        llm.ainvoke = AsyncMock(side_effect=error)
    else:
        llm.ainvoke = AsyncMock(return_value=result)
    return llm


class TestAuxiliaryMainModelFallback:
    @pytest.mark.asyncio
    async def test_success_marks_reachable_no_fallback_used(self):
        aux = _mock_llm("aux-model", result="aux-answer")
        fb = _mock_llm("main-model", result="fb-answer")
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        out = await wrapper.ainvoke(["hi"], task_name="title_generation")

        assert out == "aux-answer"
        fb.ainvoke.assert_not_called()
        assert wrapper.health.aux_reachable is True
        assert wrapper.health.degraded is False

    @pytest.mark.asyncio
    async def test_aux_failure_falls_back_to_main_and_is_loud(self):
        aux = _mock_llm("aux-model", error=RuntimeError("401 from api.openai.com"))
        fb = _mock_llm("main-model", result="fb-answer")
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        out = await wrapper.ainvoke(["hi"], task_name="title_generation")

        # Returned the fallback's answer (session survives), and it is LOUD:
        assert out == "fb-answer"
        fb.ainvoke.assert_awaited_once()
        assert wrapper.health.aux_reachable is False
        assert wrapper.health.degraded is True
        hb = wrapper.health.heartbeat_summary()
        assert hb["degraded"] is True
        assert hb["on_fallback"] is True
        assert "RuntimeError" in (hb["last_fallback_error"] or "")

    @pytest.mark.asyncio
    async def test_no_fallback_reraises(self):
        aux = _mock_llm("aux-model", error=RuntimeError("boom"))
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=None)

        with pytest.raises(RuntimeError, match="boom"):
            await wrapper.ainvoke(["hi"], task_name="title_generation")

    @pytest.mark.asyncio
    async def test_both_fail_reraises_fallback_error(self):
        aux = _mock_llm("aux-model", error=RuntimeError("aux down"))
        fb = _mock_llm("main-model", error=ValueError("main down too"))
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        with pytest.raises(ValueError, match="main down too"):
            await wrapper.ainvoke(["hi"], task_name="title_generation")

    @pytest.mark.asyncio
    async def test_fallback_success_does_not_mask_aux_down(self):
        # The core surfacing guarantee: a fallback call succeeds, so a caller
        # records per-task success (clearing the legacy _degraded escalation) —
        # but the aux model is still down, so the heartbeat must stay degraded.
        aux = _mock_llm("aux-model", error=RuntimeError("401"))
        fb = _mock_llm("main-model", result="fb-answer")
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        await wrapper.ainvoke(["hi"], task_name="title_generation")
        wrapper.health.record_success("title_generation")  # caller path

        assert wrapper.health.heartbeat_summary()["degraded"] is True
        assert wrapper.health.aux_reachable is False

    @pytest.mark.asyncio
    async def test_aux_recovers_clears_fallback_state(self):
        # aux fails once (fallback covers it), then recovers on the next call.
        aux = MagicMock()
        aux.model_name = "aux-model"
        aux.ainvoke = AsyncMock(side_effect=[RuntimeError("blip"), "aux-back"])
        fb = _mock_llm("main-model", result="fb-answer")
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        first = await wrapper.ainvoke(["a"], task_name="t")
        assert first == "fb-answer"
        assert wrapper.health.aux_reachable is False

        second = await wrapper.ainvoke(["b"], task_name="t")
        assert second == "aux-back"
        assert wrapper.health.aux_reachable is True
        assert wrapper.health.heartbeat_summary()["on_fallback"] is False

    def test_fallback_identical_to_primary_is_dropped(self):
        # If the "fallback" IS the primary (aux already the main model), there is
        # nothing to fall back to — the wrapper nulls it so a single failure
        # raises rather than pointlessly retrying the same dead endpoint.
        aux = _mock_llm("aux-model")
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=aux)
        assert wrapper.fallback_llm is None

    @pytest.mark.asyncio
    async def test_chain_recovers_from_parsed_none_parsing_error(self):
        # Simulates structured-output returning parsed=None + parsing_error while
        # still including a raw response that can be parsed as JSON.
        raw_text = (
            '{"notes_created": 2, "notes_updated": 1, "summary": "Recovered from raw"}'
        )
        structured_raw = AIMessage(content=raw_text)
        structured_response = {
            "raw": structured_raw,
            "parsed": None,
            "parsing_error": {"type": "SchemaMismatch"},
        }

        llm = MagicMock()
        structured_mock = AsyncMock()
        structured_mock.ainvoke = AsyncMock(return_value=structured_response)
        llm.with_structured_output = MagicMock(return_value=structured_mock)
        llm.ainvoke = AsyncMock(return_value=structured_raw)

        aux = AuxiliaryLLM(llm=llm)
        task = CurateKnowledgeTask(
            phase_data="phase",
            workspace_md="workspace",
            plan_md="plan",
            existing_notes=[],
            kb_tools=[],
            prompt="curate",
        )

        result = await aux.chain(task)

        assert result == CurationResult(
            notes_created=2, notes_updated=1, summary="Recovered from raw"
        )

    @pytest.mark.asyncio
    async def test_chain_recovers_from_validation_error_surface(self):
        # The raw structured-without-model-validation path should still recover from
        # the parse error in a second raw invoke pass.
        try:
            CurationResult.model_validate({})
        except ValidationError as parse_error:
            parse_error_value = parse_error
        else:
            raise AssertionError("Expected validation error")

        llm = MagicMock()
        structured_mock = AsyncMock()
        structured_mock.ainvoke = AsyncMock(side_effect=parse_error_value)
        llm.with_structured_output = MagicMock(return_value=structured_mock)
        llm.ainvoke = AsyncMock(
            return_value=AIMessage(
                content='{ "notes_created": 3, "notes_updated": 0, "summary": "Recovered" }'
            )
        )

        aux = AuxiliaryLLM(llm=llm)
        task = CurateKnowledgeTask(
            phase_data="phase",
            workspace_md="workspace",
            plan_md="plan",
            existing_notes=[],
            kb_tools=[],
            prompt="curate",
        )

        result = await aux.chain(task)

        assert result == CurationResult(
            notes_created=3, notes_updated=0, summary="Recovered"
        )
