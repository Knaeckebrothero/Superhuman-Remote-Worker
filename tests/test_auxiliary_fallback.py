"""Auxiliary provider routing + main-model fallback.

Covers the two-part fix for
knowledge-base/knowledge/issues/openrouter_auxiliary_misrouted_to_openai.md:

Part A — an OpenRouter auxiliary must not be silently routed to api.openai.com.
The provider is threaded from AuxiliaryConfig into create_llm, and create_llm
auto-detects the ``openrouter/`` model prefix when no provider is given.

Part B — when the dedicated aux model is unreachable, aux tasks fall back to
the main model (drop-in) instead of crashing the session, LOUDLY (the
aux-degraded heartbeat flag lights up), and only raise when there is no
fallback or the fallback also fails.
"""

from dataclasses import replace
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
from src.core.llm_retry import NO_RETRY
from src.services.auxiliary import (
    AuxiliaryLLM,
    AuxInputTooLarge,
    CurateKnowledgeTask,
    CurationResult,
)


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """Keep retry semantics under test without real one-second backoffs."""
    from src.services import auxiliary

    fast_policy = replace(auxiliary._AUX_RETRY, base_delay=0.0, max_delay=0.0)
    monkeypatch.setattr(auxiliary, "_AUX_RETRY", fast_policy)
    monkeypatch.setattr(auxiliary, "_AUX_FALLBACK_RETRY", fast_policy)


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
        # aux exhausts its retry budget (fallback covers it), then recovers on
        # the next call. Two failures, not one: a single blip is now retried on
        # aux and never reaches the fallback (see test_transient_blip_* below).
        aux = MagicMock()
        aux.model_name = "aux-model"
        aux.ainvoke = AsyncMock(
            side_effect=[RuntimeError("blip"), RuntimeError("blip"), "aux-back"]
        )
        fb = _mock_llm("main-model", result="fb-answer")
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        first = await wrapper.ainvoke(["a"], task_name="t")
        assert first == "fb-answer"
        assert wrapper.health.aux_reachable is False

        second = await wrapper.ainvoke(["b"], task_name="t")
        assert second == "aux-back"
        assert wrapper.health.aux_reachable is True
        assert wrapper.health.heartbeat_summary()["on_fallback"] is False


class TestAuxRetryBeforeFallback:
    """Retry and fallback are different axes and compose in that order.

    Before this, aux had no retry, so the fallback was doing retry's job: one
    transient blip on the cheap aux model instantly rerouted the call to the
    expensive main model and lit the aux-degraded heartbeat flag.
    knowledge-history/done/llm_retry_and_fallback_reimplemented_per_call_site.md
    """

    @pytest.mark.asyncio
    async def test_transient_blip_is_retried_on_aux_not_escalated(self):
        aux = MagicMock()
        aux.model_name = "aux-model"
        aux.ainvoke = AsyncMock(side_effect=[RuntimeError("blip"), "aux-answer"])
        fb = _mock_llm("main-model", result="fb-answer")
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        result = await wrapper.ainvoke(["a"], task_name="t")

        # Retried on aux and succeeded — the expensive model was never called...
        assert result == "aux-answer"
        assert aux.ainvoke.await_count == 2
        fb.ainvoke.assert_not_awaited()
        # ...and one blip must not declare the model unreachable.
        assert wrapper.health.aux_reachable is True
        assert wrapper.health.heartbeat_summary()["degraded"] is False

    @pytest.mark.asyncio
    async def test_timeout_escalates_without_burning_a_second_timeout(self):
        # A hung aux model should escalate immediately: falling back answers
        # now, retrying just spends another full timeout to learn nothing.
        aux = MagicMock()
        aux.model_name = "aux-model"
        aux.ainvoke = AsyncMock(side_effect=TimeoutError())
        fb = _mock_llm("main-model", result="fb-answer")
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        result = await wrapper.ainvoke(["a"], task_name="t")

        assert result == "fb-answer"
        assert aux.ainvoke.await_count == 1
        assert wrapper.health.aux_reachable is False

    @pytest.mark.asyncio
    async def test_permanent_error_escalates_without_retry(self):
        # No wait fixes a bad model name — go straight to the fallback.
        aux = MagicMock()
        aux.model_name = "aux-model"
        aux.ainvoke = AsyncMock(side_effect=Exception("model gpt-x does not exist"))
        fb = _mock_llm("main-model", result="fb-answer")
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        assert await wrapper.ainvoke(["a"], task_name="t") == "fb-answer"
        assert aux.ainvoke.await_count == 1

    @pytest.mark.asyncio
    async def test_deterministic_overflow_is_never_retried(self):
        # Regression: ContextOverflowError / AuxInputTooLarge reach the
        # classifier as internal typed exceptions, not provider errors, so its
        # catch-all called them `transient` and the aux layer cheerfully re-sent
        # a 951k-token payload that cannot fit by construction. AuxInputTooLarge's
        # own docstring says callers must NOT retry these.
        from src.llm.exceptions import ContextOverflowError

        for exc in (
            ContextOverflowError(951682, 131072),
            AuxInputTooLarge(951682, 131072, "SummarizeTask"),
        ):
            aux = MagicMock()
            aux.model_name = "aux-model"
            aux.ainvoke = AsyncMock(side_effect=exc)
            wrapper = AuxiliaryLLM(llm=aux, fallback_llm=None)

            with pytest.raises(type(exc)):
                await wrapper.ainvoke(["a"], task_name="t")
            assert aux.ainvoke.await_count == 1, f"{type(exc).__name__} was retried"

    @pytest.mark.asyncio
    async def test_no_retry_policy_disables_the_aux_layer_loop(self):
        # Callers that already own a retry loop (the summarization fold, memory
        # extraction) opt out so retry stays at exactly one layer per path.
        aux = MagicMock()
        aux.model_name = "aux-model"
        aux.ainvoke = AsyncMock(side_effect=[RuntimeError("blip"), "aux-answer"])
        fb = _mock_llm("main-model", result="fb-answer")
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        result = await wrapper.ainvoke(["a"], task_name="t", retry_policy=NO_RETRY)

        # One aux attempt, then straight to the fallback — no inner retry.
        assert aux.ainvoke.await_count == 1
        assert result == "fb-answer"

    @pytest.mark.asyncio
    async def test_escalated_fallback_call_is_itself_retried(self):
        # The escalated path used to be the ONE path with no protection: a blip
        # on the main model raised straight out, which compaction surfaces as
        # SummarizationFailed('aux_unavailable') — failing the turn on a blip.
        aux = _mock_llm("aux-model", error=RuntimeError("aux down"))
        fb = MagicMock()
        fb.model_name = "main-model"
        fb.ainvoke = AsyncMock(side_effect=[RuntimeError("blip"), "fb-answer"])
        wrapper = AuxiliaryLLM(llm=aux, fallback_llm=fb)

        result = await wrapper.ainvoke(["a"], task_name="t")

        assert result == "fb-answer"
        assert fb.ainvoke.await_count == 2

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
