"""Slice D of live_session_settings.md — model hot-swap hardening.

Covers the three rungs:
  1. the provider-boundary history sanitizer
     (``core.context.sanitize_history_for_provider_boundary``),
  2. the swap-time fit-check ladder (``persistent_app._model_swap_fit_ladder``),
  3. the provider-usage anchor invalidation on compaction success
     (``ContextManager._note_compaction_success``).

The live per-family mutation smokes against a real vLLM endpoint live in
``tests/test_family_mutation_smoke.py`` (env-gated).
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from src.core.context import (
    ContextConfig,
    ContextManager,
    sanitize_history_for_provider_boundary,
)

GENERIC_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
MISTRAL_ID_RE = re.compile(r"^[a-zA-Z0-9]{9}$")

# An id no provider format accepts verbatim (49 chars, illegal characters).
FOREIGN_ID = "toolu_01+extremely/long!identifier=with#bad$chars"


def _tool_pair(tool_id: str, text: str = "hi") -> list:
    return [
        AIMessage(content=text, tool_calls=[{"id": tool_id, "name": "t", "args": {}}]),
        ToolMessage(content="result", tool_call_id=tool_id),
    ]


class TestSanitizeHistoryReasoning:
    def test_strips_anthropic_thinking_blocks_and_collapses_to_text(self):
        msgs = [
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "secret", "signature": "sig"},
                    {"type": "text", "text": "answer"},
                ]
            )
        ]
        out = sanitize_history_for_provider_boundary(msgs, "gemma-4-moe")
        assert out[0].content == "answer"

    def test_strips_redacted_thinking_and_responses_reasoning_blocks(self):
        msgs = [
            AIMessage(
                content=[
                    {"type": "redacted_thinking", "data": "opaque"},
                    {"type": "reasoning", "summary": [{"text": "why"}]},
                    {"type": "text", "text": "final"},
                ]
            )
        ]
        out = sanitize_history_for_provider_boundary(msgs, "MiniMax-M3")
        assert out[0].content == "final"

    def test_keeps_non_reasoning_blocks_as_list(self):
        image = {"type": "image_url", "image_url": {"url": "data:x"}}
        msgs = [
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "t"},
                    {"type": "text", "text": "a"},
                    image,
                ]
            )
        ]
        out = sanitize_history_for_provider_boundary(msgs, "gemma-4-moe")
        assert out[0].content == [{"type": "text", "text": "a"}, image]

    def test_strips_reasoning_additional_kwargs_preserves_others(self):
        msgs = [
            AIMessage(
                content="answer",
                additional_kwargs={
                    "reasoning_content": "chain of thought",
                    "reasoning_details": [{"x": 1}],
                    "refusal": None,
                },
            )
        ]
        out = sanitize_history_for_provider_boundary(msgs, "gpt-oss-120b")
        assert "reasoning_content" not in out[0].additional_kwargs
        assert "reasoning_details" not in out[0].additional_kwargs
        assert "refusal" in out[0].additional_kwargs

    def test_reasoning_only_message_without_tool_calls_is_dropped(self):
        msgs = [
            HumanMessage(content="q"),
            AIMessage(content=[{"type": "thinking", "thinking": "only"}]),
            AIMessage(content="real answer"),
        ]
        out = sanitize_history_for_provider_boundary(msgs, "gemma-4-moe")
        assert len(out) == 2
        assert out[1].content == "real answer"

    def test_reasoning_only_content_with_tool_calls_keeps_message(self):
        msgs = [
            AIMessage(
                content=[{"type": "thinking", "thinking": "t"}],
                tool_calls=[{"id": "call_ok1", "name": "t", "args": {}}],
            ),
            ToolMessage(content="r", tool_call_id="call_ok1"),
        ]
        out = sanitize_history_for_provider_boundary(msgs, "gemma-4-moe")
        assert out[0].tool_calls[0]["id"] == "call_ok1"
        assert out[0].content == ""

    def test_drops_invalid_tool_calls(self):
        msg = AIMessage(content="x")
        msg.invalid_tool_calls = [
            {"id": "bad", "name": "t", "args": "{broken", "error": "parse"}
        ]
        out = sanitize_history_for_provider_boundary([msg], "gemma-4-moe")
        assert out[0].invalid_tool_calls == []

    def test_non_ai_messages_pass_through_untouched(self):
        msgs = [
            SystemMessage(content="sys"),
            HumanMessage(content="hi"),
        ]
        out = sanitize_history_for_provider_boundary(msgs, "gemma-4-moe")
        assert out[0] is msgs[0]
        assert out[1] is msgs[1]


class TestSanitizeHistoryToolCallIds:
    def test_remaps_nonconforming_id_consistently_on_both_sides(self):
        out = sanitize_history_for_provider_boundary(
            _tool_pair(FOREIGN_ID), "gemma-4-moe"
        )
        new_id = out[0].tool_calls[0]["id"]
        assert new_id != FOREIGN_ID
        assert GENERIC_ID_RE.match(new_id)
        assert out[1].tool_call_id == new_id

    def test_conforming_ids_untouched_for_generic_target(self):
        for ok in ("toolu_01AbCdEfGhIjKlMnOpQrSt", "call_abc123", "a-b_c9"):
            out = sanitize_history_for_provider_boundary(_tool_pair(ok), "gpt-5.5")
            assert out[0].tool_calls[0]["id"] == ok
            assert out[1].tool_call_id == ok

    def test_mistral_target_enforces_nine_alnum(self):
        out = sanitize_history_for_provider_boundary(
            _tool_pair("call_abc123def456"), "mistral-large-latest"
        )
        new_id = out[0].tool_calls[0]["id"]
        assert MISTRAL_ID_RE.match(new_id)
        assert out[1].tool_call_id == new_id

    def test_mistral_target_keeps_conforming_nine_alnum(self):
        out = sanitize_history_for_provider_boundary(
            _tool_pair("aB3dE6gH9"), "codestral-latest"
        )
        assert out[0].tool_calls[0]["id"] == "aB3dE6gH9"

    def test_idempotent(self):
        once = sanitize_history_for_provider_boundary(
            _tool_pair(FOREIGN_ID), "mistral-large-latest"
        )
        twice = sanitize_history_for_provider_boundary(once, "mistral-large-latest")
        assert [m.content for m in once] == [m.content for m in twice]
        assert once[0].tool_calls == twice[0].tool_calls
        assert once[1].tool_call_id == twice[1].tool_call_id

    def test_pairing_verified_after_transform(self):
        msgs = _tool_pair(FOREIGN_ID) + [
            ToolMessage(content="orphan", tool_call_id="never_called")
        ]
        out = sanitize_history_for_provider_boundary(msgs, "gemma-4-moe")
        assert len(out) == 2  # orphan dropped by the pairing verification

    def test_input_messages_never_mutated(self):
        msgs = _tool_pair(FOREIGN_ID)
        msgs[0].additional_kwargs["reasoning_content"] = "cot"
        sanitize_history_for_provider_boundary(msgs, "gemma-4-moe")
        assert msgs[0].tool_calls[0]["id"] == FOREIGN_ID
        assert msgs[1].tool_call_id == FOREIGN_ID
        assert msgs[0].additional_kwargs["reasoning_content"] == "cot"


class TestNoteCompactionSuccess:
    def test_clears_provider_anchor_and_bumps_runs(self):
        cm = ContextManager(config=ContextConfig())
        cm.record_provider_usage(123_456)
        assert cm.state.last_provider_input_tokens == 123_456
        cm._note_compaction_success()
        assert cm.compaction_runs == 1
        assert cm.state.last_provider_input_tokens is None

    def test_summarize_and_compact_success_paths_use_it(self):
        """Both compaction-success sites must invalidate the anchor — a raw
        ``compaction_runs += 1`` reintroduces the post-compaction re-trigger
        (the trigger floors at the stale anchor until an LLM call heals it)."""
        from inspect import getsource

        src = getsource(ContextManager.summarize_and_compact)
        assert "self.compaction_runs += 1" not in src
        assert src.count("self._note_compaction_success()") == 2


class _FakeContextManager:
    """Double for the live ContextManager as the fit ladder uses it."""

    def __init__(self, anchor=None, compact_result=None, compact_error=None):
        self.state = SimpleNamespace(last_provider_input_tokens=anchor)
        self.compaction_runs = 0
        self.limit_calls = []
        self.progress_cb = None
        self._compact_result = compact_result
        self._compact_error = compact_error

    def set_progress_callback(self, cb):
        self.progress_cb = cb

    def update_limits(self, config, model):
        self.limit_calls.append((config, model))

    async def ensure_within_limits(self, messages, auxiliary, **kwargs):
        if self._compact_error is not None:
            raise self._compact_error
        self.compaction_runs += 1
        # On success, the real manager invalidates the anchor.
        self.state.last_provider_input_tokens = None
        return list(self._compact_result)


def _ladder_session(
    messages,
    *,
    context_manager,
    auxiliary_llm,
    old_cfg: ContextConfig,
    new_cfg: ContextConfig,
):
    """Session double: _build_context_config(None) → old, (cfg) → new."""

    def _build(config=None):
        return old_cfg if config is None else new_cfg

    return SimpleNamespace(
        messages=messages,
        context_manager=context_manager,
        auxiliary_llm=auxiliary_llm,
        config=SimpleNamespace(
            llm=SimpleNamespace(model="old-model"),
            context_management=SimpleNamespace(max_summary_length=10000),
        ),
        _build_context_config=_build,
    )


NEW_CONFIG = SimpleNamespace(llm=SimpleNamespace(model="new-model"))

# ~600 tokens of history — over the tiny thresholds below, under the big ones.
BIG_HISTORY = [HumanMessage(content="word " * 600)]


@pytest.mark.asyncio
class TestModelSwapFitLadder:
    async def _run(self, monkeypatch, session, *, in_flight=False):
        import src.api.persistent_app as mod

        record = AsyncMock()
        monkeypatch.setattr(mod, "_session", session)
        monkeypatch.setattr(mod, "_turn_in_flight", lambda: in_flight)
        monkeypatch.setattr(mod, "_record_compaction", record)
        result = await mod._model_swap_fit_ladder(NEW_CONFIG)
        return result, record

    async def test_history_fits_new_budget_passes_without_compaction(self, monkeypatch):
        cm = _FakeContextManager()
        session = _ladder_session(
            list(BIG_HISTORY),
            context_manager=cm,
            auxiliary_llm=MagicMock(),
            old_cfg=ContextConfig(),
            new_cfg=ContextConfig(
                compaction_threshold_tokens=80_000, model_max_context_tokens=100_000
            ),
        )
        result, record = await self._run(monkeypatch, session)
        assert result is None
        assert cm.limit_calls == []
        record.assert_not_awaited()

    async def test_empty_history_always_fits(self, monkeypatch):
        session = _ladder_session(
            [],
            context_manager=_FakeContextManager(),
            auxiliary_llm=MagicMock(),
            old_cfg=ContextConfig(),
            new_cfg=ContextConfig(compaction_threshold_tokens=1),
        )
        result, _ = await self._run(monkeypatch, session)
        assert result is None

    async def test_provider_anchor_floors_the_check(self, monkeypatch):
        """A tiny local count with a huge provider anchor still triggers the
        ladder — the anchor carries prompt/schema overhead the list lacks."""
        cm = _FakeContextManager(anchor=90_000, compact_result=list(BIG_HISTORY))
        session = _ladder_session(
            [HumanMessage(content="small")],
            context_manager=cm,
            auxiliary_llm=MagicMock(),
            old_cfg=ContextConfig(),
            new_cfg=ContextConfig(
                compaction_threshold_tokens=80_000, model_max_context_tokens=100_000
            ),
        )
        result, record = await self._run(monkeypatch, session)
        assert result is None  # compaction ran and the result fits
        assert cm.compaction_runs == 1
        record.assert_awaited_once()

    async def test_fixed_overhead_counts_against_the_window(self, monkeypatch):
        """anchor − bare count estimates the system-prompt/tool-schema
        overhead compaction cannot shrink; the post-compaction verdict must
        include it. Live-observed failure: a 2.2k bare history passed a 3k
        window while the real request measured 17.6k → 413 on the next turn."""
        cm = _FakeContextManager(
            anchor=90_000, compact_result=[HumanMessage(content="tiny")]
        )
        old_cfg = ContextConfig()
        new_cfg = ContextConfig(
            compaction_threshold_tokens=40_000, model_max_context_tokens=50_000
        )
        session = _ladder_session(
            [HumanMessage(content="small")],  # bare ≈ nothing; overhead ≈ 90k
            context_manager=cm,
            auxiliary_llm=MagicMock(),
            old_cfg=old_cfg,
            new_cfg=new_cfg,
        )
        result, _ = await self._run(monkeypatch, session)
        assert result is not None and "system prompt" in result
        assert cm.limit_calls == [(new_cfg, "new-model"), (old_cfg, "old-model")]

    async def test_turn_in_flight_rejects_instead_of_compacting(self, monkeypatch):
        cm = _FakeContextManager()
        session = _ladder_session(
            list(BIG_HISTORY),
            context_manager=cm,
            auxiliary_llm=MagicMock(),
            old_cfg=ContextConfig(),
            new_cfg=ContextConfig(
                compaction_threshold_tokens=10, model_max_context_tokens=100
            ),
        )
        result, record = await self._run(monkeypatch, session, in_flight=True)
        assert result is not None and "current turn" in result
        assert cm.limit_calls == []
        record.assert_not_awaited()

    async def test_no_summarizer_over_hard_cap_rejects(self, monkeypatch):
        session = _ladder_session(
            list(BIG_HISTORY),
            context_manager=_FakeContextManager(),
            auxiliary_llm=None,
            old_cfg=ContextConfig(),
            new_cfg=ContextConfig(
                compaction_threshold_tokens=10, model_max_context_tokens=100
            ),
        )
        result, _ = await self._run(monkeypatch, session)
        assert result is not None and "was not applied" in result

    async def test_no_summarizer_under_hard_cap_allows(self, monkeypatch):
        session = _ladder_session(
            list(BIG_HISTORY),
            context_manager=_FakeContextManager(),
            auxiliary_llm=None,
            old_cfg=ContextConfig(),
            new_cfg=ContextConfig(
                compaction_threshold_tokens=10, model_max_context_tokens=100_000
            ),
        )
        result, _ = await self._run(monkeypatch, session)
        assert result is None

    async def test_compacts_then_passes_and_records_checkpoint(self, monkeypatch):
        compacted = [HumanMessage(content="short")]
        cm = _FakeContextManager(compact_result=compacted)
        new_cfg = ContextConfig(
            compaction_threshold_tokens=100, model_max_context_tokens=1_000
        )
        session = _ladder_session(
            list(BIG_HISTORY),
            context_manager=cm,
            auxiliary_llm=MagicMock(),
            old_cfg=ContextConfig(),
            new_cfg=new_cfg,
        )
        result, record = await self._run(monkeypatch, session)
        assert result is None
        # History adopted in place.
        assert session.messages == compacted
        # Limits rolled FORWARD to the candidate and not rolled back.
        assert cm.limit_calls == [(new_cfg, "new-model")]
        record.assert_awaited_once()
        assert record.await_args.kwargs.get("trigger") == "model_swap"

    async def test_still_over_after_compaction_rejects_and_rolls_back(
        self, monkeypatch
    ):
        cm = _FakeContextManager(compact_result=list(BIG_HISTORY))
        old_cfg = ContextConfig()
        new_cfg = ContextConfig(
            compaction_threshold_tokens=100, model_max_context_tokens=200
        )
        session = _ladder_session(
            list(BIG_HISTORY),
            context_manager=cm,
            auxiliary_llm=MagicMock(),
            old_cfg=old_cfg,
            new_cfg=new_cfg,
        )
        result, _ = await self._run(monkeypatch, session)
        assert result is not None and "was not applied" in result
        assert cm.limit_calls == [(new_cfg, "new-model"), (old_cfg, "old-model")]

    async def test_compaction_failure_rejects_and_rolls_back(self, monkeypatch):
        cm = _FakeContextManager(compact_error=RuntimeError("aux down"))
        old_cfg = ContextConfig()
        new_cfg = ContextConfig(
            compaction_threshold_tokens=100, model_max_context_tokens=200
        )
        session = _ladder_session(
            list(BIG_HISTORY),
            context_manager=cm,
            auxiliary_llm=MagicMock(),
            old_cfg=old_cfg,
            new_cfg=new_cfg,
        )
        result, record = await self._run(monkeypatch, session)
        assert result is not None and "aux down" in result
        assert cm.limit_calls == [(new_cfg, "new-model"), (old_cfg, "old-model")]
        record.assert_not_awaited()


class TestConfigUpdateSwapWiring:
    """Source-level pins for the _handle_config_update integration (the repo's
    established pattern for this handler — the full async chain is brittle to
    mock; see TestHandleConfigUpdateEnrichmentGate)."""

    def _src(self):
        from inspect import getsource

        from src.api.persistent_app import _handle_config_update

        return getsource(_handle_config_update)

    def test_ladder_runs_before_swap_is_applied(self):
        src = self._src()
        assert src.index("_model_swap_fit_ladder(") < src.index(
            "_session._llm = new_llm"
        )

    def test_ladder_rejection_stops_the_update(self):
        src = self._src()
        rejection_block = src[src.index("_model_swap_fit_ladder(") :]
        assert "return" in rejection_block.split("_session._llm")[0]

    def test_sanitizer_gated_on_family_or_provider_boundary(self):
        src = self._src()
        assert "sanitize_history_for_provider_boundary(" in src
        assert "family_of(" in src

    def test_temperature_only_llm_fragment_skips_both_rungs(self):
        src = self._src()
        assert "model_swapped" in src
        # The rungs are inside the model_swapped gate, not bare llm_changed.
        gate = src.index("if model_swapped:")
        assert gate < src.index("_model_swap_fit_ladder(")


class TestSanitizeRestoredHistory:
    """The restore rung: histories persisted under one provider are remapped
    for the currently-bound model at attach (covers Slice C offline swaps)."""

    def test_remaps_foreign_ids_for_bound_model(self, monkeypatch):
        import src.api.persistent_app as mod

        session = SimpleNamespace(
            config=SimpleNamespace(llm=SimpleNamespace(model="mistral-large-latest"))
        )
        monkeypatch.setattr(mod, "_session", session)
        out = mod._sanitize_restored_history(_tool_pair("toolu_01AbCdEfGhIjKlMnOp"))
        assert MISTRAL_ID_RE.match(out[0].tool_calls[0]["id"])
        assert out[0].tool_calls[0]["id"] == out[1].tool_call_id

    def test_survives_missing_session(self, monkeypatch):
        import src.api.persistent_app as mod

        monkeypatch.setattr(mod, "_session", None)
        out = mod._sanitize_restored_history(_tool_pair("call_ok"))
        assert out[1].tool_call_id == "call_ok"

    def test_both_restore_paths_call_it_after_pairing_repair(self):
        from inspect import getsource

        from src.api.persistent_app import _restore_session_messages

        src = getsource(_restore_session_messages)
        assert src.count("_sanitize_restored_history(") == 2
        # Ordering: pairing repair first, then the boundary sanitize.
        first_repair = src.index("_repair_tool_pairing(")
        assert first_repair < src.index("_sanitize_restored_history(")
