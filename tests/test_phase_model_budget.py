"""Phase-model context budget + inference-params resolution.

Covers `resolve_phase_model_budget` (the pure resolver) and the end-state it
produces in `UniversalAgent._create_phase_llms`: each phase resolves its OWN
family params/window instead of inheriting the base/primary slot (gemma by
default), and the shared compaction budget is the `min` of the two phase
windows. Sessions (single-model) are untouched.

Root cause + design:
docs/issues/context_budget_uses_base_model_not_phase_models.md
"""

from types import SimpleNamespace
from unittest.mock import patch

from src.core.loader import (
    CONTEXT_THRESHOLD_FRACTION,
    MESSAGE_COUNT_MIN_FRACTION,
    PhaseLLMOverride,
    _apply_settings_matrix,
    load_agent_config_from_dict,
    resolve_phase_model_budget,
)

GEMMA = "RedHatAI/gemma-4-31B-it-FP8-Dynamic"  # base default; multimodal, 131072
GPT55 = "gpt-5.5"
GPT54MINI = "gpt-5.4-mini"
GPT_OSS = "openai/gpt-oss-120b"  # text-only family (multimodal=false), 131072
GPT5_WINDOW = 1_050_000
GEMMA_WINDOW = 131_072


def _ov(**kw):
    return PhaseLLMOverride(**kw)


class TestResolvePhaseModelBudget:
    """The pure resolver — the core of the fix."""

    def test_incident_mixed_family_same_window(self):
        # The original incident: base gemma, strategic gpt-5.5, tactical
        # gpt-5.4-mini (both gpt-5 family).
        r = resolve_phase_model_budget(
            base_model=GEMMA,
            strategic_override=_ov(model=GPT55),
            tactical_override=_ov(model=GPT54MINI),
        )
        # Window escapes the gemma 131072 cap -> gpt-5 family window.
        assert r["min_window"] == GPT5_WINDOW
        assert r["windows"] == {"strategic": GPT5_WINDOW, "tactical": GPT5_WINDOW}
        # Params are gpt-5 family (temp 1.0, top_p/top_k cleared), NOT gemma's.
        for phase in ("strategic", "tactical"):
            assert r["params"][phase]["temperature"] == 1.0
            assert r["params"][phase]["top_p"] is None
            assert r["params"][phase]["top_k"] is None
        assert r["effective_multimodal"] is True
        # Same family + same window -> no warnings at all.
        assert r["warnings"] == []

    def test_gemma_params_never_leak_into_phase(self):
        # Guards the params half of the bug: gemma's 0.95/64 must not appear.
        r = resolve_phase_model_budget(
            base_model=GEMMA,
            strategic_override=_ov(model=GPT55),
            tactical_override=_ov(model=GPT54MINI),
        )
        assert r["params"]["strategic"]["top_p"] != 0.95
        assert r["params"]["strategic"]["top_k"] != 64
        assert r["params"]["strategic"]["temperature"] != 0.3

    def test_explicit_param_pin_preserved(self):
        # An explicitly-pinned phase param wins over the family default.
        r = resolve_phase_model_budget(
            base_model=GEMMA,
            strategic_override=_ov(model=GPT55, temperature=0),
            tactical_override=_ov(model=GPT54MINI),
        )
        assert r["params"]["strategic"]["temperature"] == 0  # not family 1.0

    def test_same_model_both_phases_is_noop(self):
        r = resolve_phase_model_budget(
            base_model=GEMMA,
            strategic_override=_ov(model=GPT55),
            tactical_override=_ov(model=GPT55),
        )
        assert r["min_window"] == GPT5_WINDOW
        assert r["effective_multimodal"] is True
        assert r["warnings"] == []

    def test_mixed_window_takes_min_and_warns(self):
        # gpt-5.5 (1.05M) + gemma (131072), both multimodal -> pure window mismatch.
        r = resolve_phase_model_budget(
            base_model=GEMMA,
            strategic_override=_ov(model=GPT55),
            tactical_override=_ov(model=GEMMA),
        )
        assert r["min_window"] == GEMMA_WINDOW
        levels = [lvl for lvl, _ in r["warnings"]]
        msgs = " ".join(m for _, m in r["warnings"])
        assert "warning" in levels  # 8x ratio > 2 -> warning level
        assert "different context windows" in msgs
        assert "multimodal capability" not in msgs  # both multimodal -> no mm warning

    def test_mixed_multimodal_ands_and_warns(self):
        # gpt-5.5 (multimodal) + gpt-oss-120b (text-only) -> AND = False.
        r = resolve_phase_model_budget(
            base_model=GEMMA,
            strategic_override=_ov(model=GPT55),
            tactical_override=_ov(model=GPT_OSS),
        )
        assert r["effective_multimodal"] is False
        msgs = " ".join(m for _, m in r["warnings"])
        assert "multimodal capability" in msgs

    def test_catalog_window_pin_participates_in_min(self):
        # A dispatch/admin per-model window below the family max wins for that phase.
        r = resolve_phase_model_budget(
            base_model=GEMMA,
            strategic_override=_ov(model=GPT55, model_max_context_tokens=200_000),
            tactical_override=_ov(model=GPT54MINI),
        )
        assert r["windows"]["strategic"] == 200_000
        assert r["min_window"] == 200_000

    def test_phase_without_override_uses_base_model_window(self):
        # tactical has no override -> genuinely runs base gemma -> constrains history.
        r = resolve_phase_model_budget(
            base_model=GEMMA,
            strategic_override=_ov(model=GPT55),
            tactical_override=None,
        )
        assert r["min_window"] == GEMMA_WINDOW
        assert list(r["params"].keys()) == ["strategic"]  # only the overridden phase


# --------------------------------------------------------------------------- #
# End-state integration: drive _create_phase_llms on a light shell, asserting
# the mutations it makes to self.config.limits / self.config.llm.
# --------------------------------------------------------------------------- #


def _make_config(llm_overrides: dict):
    """Build an AgentConfig the way the loader does: base gemma with the settings
    matrix applied (so limits START at the gemma 131072 cap), plus phase pins."""
    data = {
        "agent_id": "test-agent",
        "display_name": "Test Agent",
        "llm": {"model": GEMMA, **llm_overrides},
    }
    _apply_settings_matrix(data, set(), None)
    return load_agent_config_from_dict(data)


class _FakeAgent:
    """Minimal shell exposing only what _create_phase_llms touches."""

    def __init__(self, config):
        self.config = config
        self._strategic_llm = None
        self._tactical_llm = None
        self._summarization_llm = None
        self._llm = None

    def _initialize_auxiliary_llm(self, *a, **k):
        pass

    def _initialize_citation_verifier(self, *a, **k):
        pass


def _run_create_phase_llms(config):
    from src.agent import UniversalAgent

    fake = _FakeAgent(config)
    created = []

    def fake_create_llm(cfg, limits=None):
        created.append(cfg)
        return SimpleNamespace(model=cfg.model, cfg=cfg)

    with patch("src.agent.create_llm", side_effect=fake_create_llm):
        UniversalAgent._create_phase_llms(fake)
    return fake, created


class TestCreatePhaseLLMsEndState:
    def test_incident_raises_budget_off_gemma_cap(self):
        config = _make_config(
            {"strategic": {"model": GPT55}, "tactical": {"model": GPT54MINI}}
        )
        assert config.limits.model_max_context_tokens == GEMMA_WINDOW  # pre-fix cap

        fake, created = _run_create_phase_llms(config)

        # Shared budget raised to the gpt-5 family window + re-derived leaves.
        assert fake.config.limits.model_max_context_tokens == GPT5_WINDOW
        assert fake.config.limits.context_threshold_tokens == int(
            GPT5_WINDOW * CONTEXT_THRESHOLD_FRACTION
        )
        assert fake.config.limits.message_count_min_tokens == int(
            GPT5_WINDOW * MESSAGE_COUNT_MIN_FRACTION
        )
        # Each phase client got its OWN window (not 131072) + gpt-5 params.
        by_model = {c.model: c for c in created}
        assert by_model[GPT55].model_max_context_tokens == GPT5_WINDOW
        assert by_model[GPT55].top_p is None and by_model[GPT55].top_k is None
        # Different models -> two distinct clients (no false dedupe).
        assert GPT55 in by_model and GPT54MINI in by_model

    def test_session_shape_unchanged(self):
        config = _make_config({})  # no phase overrides
        before = config.limits.model_max_context_tokens
        fake, created = _run_create_phase_llms(config)
        assert fake.config.limits.model_max_context_tokens == before
        assert fake._model_config_warnings == []
        assert len(created) == 1  # single LLM for all phases

    def test_multimodal_writeback_drives_the_gate(self):
        config = _make_config(
            {"strategic": {"model": GPT55}, "tactical": {"model": GPT_OSS}}
        )
        fake, _ = _run_create_phase_llms(config)
        # The per-tool image gate reads self.config.llm.get_phase_config(p).multimodal.
        assert fake.config.llm.get_phase_config("strategic").multimodal is False
        assert fake.config.llm.get_phase_config("tactical").multimodal is False
        assert any("multimodal" in w for w in fake._model_config_warnings)

    def test_same_family_same_model_dedupes(self):
        config = _make_config(
            {"strategic": {"model": GPT55}, "tactical": {"model": GPT55}}
        )
        fake, created = _run_create_phase_llms(config)
        # Identical resolved configs -> tactical reuses strategic (one create call).
        assert len(created) == 1
        assert fake._strategic_llm is fake._tactical_llm
