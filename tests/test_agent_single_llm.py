"""``UniversalAgent._create_phase_llms`` after the U1 tier collapse.

One ``llm.model`` builds ONE client that every phase shares; the only way to get
a second client is an ``llm.summarization`` override that resolves to a
different config. Ported from the retired ``tests/test_phase_model_budget.py``
(the per-phase window/params reconciliation went with the tiers).
"""

from types import SimpleNamespace
from unittest.mock import patch

from src.core.loader import _apply_settings_matrix, load_agent_config_from_dict

GEMMA = "RedHatAI/gemma-4-31B-it-FP8-Dynamic"  # base default
GPT55 = "gpt-5.5"
GPT54MINI = "gpt-5.4-mini"


def _make_config(llm_overrides: dict):
    """Build an AgentConfig the way the loader does: base gemma with the settings
    matrix applied, plus the given llm keys."""
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


class TestCreatePhaseLLMsSingleModel:
    def test_session_shape_unchanged(self):
        config = _make_config({})
        before = config.limits.model_max_context_tokens
        fake, created = _run_create_phase_llms(config)
        assert fake.config.limits.model_max_context_tokens == before
        assert fake._model_config_warnings == []
        assert len(created) == 1  # one client for every phase
        assert fake._llm is fake._strategic_llm is fake._tactical_llm
        assert fake._summarization_llm is fake._llm
        assert created[0].model == GEMMA

    def test_legacy_phase_pins_collapse_to_one_client(self):
        """A hand-built pre-U1 dict with strategic/tactical pins runs ONE model
        (the strategic one, per the merged-dict rule) — no second client."""
        config = _make_config(
            {"strategic": {"model": GPT55}, "tactical": {"model": GPT54MINI}}
        )
        assert config.llm.model == GPT55
        fake, created = _run_create_phase_llms(config)
        assert [c.model for c in created] == [GPT55]
        assert fake._strategic_llm is fake._tactical_llm

    def test_summarization_override_creates_second_client(self):
        config = _make_config({"summarization": {"model": GPT54MINI}})
        fake, created = _run_create_phase_llms(config)
        assert [c.model for c in created] == [GEMMA, GPT54MINI]
        assert fake._summarization_llm is not fake._llm
        assert fake._summarization_llm.cfg.model == GPT54MINI
        # The resolved summarization client carries no phase override itself.
        assert fake._summarization_llm.cfg.summarization is None

    def test_identical_summarization_dedupes(self):
        """An override that resolves to the main model's config reuses the
        client instead of opening a second connection to the same endpoint."""
        config = _make_config({"summarization": {"model": GEMMA}})
        fake, created = _run_create_phase_llms(config)
        assert len(created) == 1
        assert fake._summarization_llm is fake._llm

    def test_summarization_params_only_override_still_distinct(self):
        config = _make_config({"summarization": {"temperature": 0.9}})
        fake, created = _run_create_phase_llms(config)
        assert len(created) == 2
        assert fake._summarization_llm.cfg.temperature == 0.9
