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


# ---------------------------------------------------------------------------
# ``_bind_job_tools`` (U2 WP3): one binding for every phase, or the legacy pair
# ---------------------------------------------------------------------------

from langchain_core.tools import StructuredTool  # noqa: E402

from src.core.loader import (  # noqa: E402
    PROMPT_MODE_LEGACY,
    InstructionFileEntry,
    supports_parallel_tool_calls,
)
from src.tools.context import ToolContext  # noqa: E402
from src.tools.registry import apply_instruction_enforcement  # noqa: E402

_BIND_TOOLS = ("job_complete", "request_replan", "read_file")


def _structured(name: str) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda: "ok", name=name, description=f"Does {name}."
    )


class _RecordingLLM:
    """``bind_tools`` returns a distinct object per call and records the schema
    it was handed (names, descriptions, kwargs)."""

    def __init__(self):
        self.calls = []

    def bind_tools(self, tools, **kwargs):
        bound = SimpleNamespace(
            tool_names=[t.name for t in tools],
            descriptions={t.name: t.description for t in tools},
            kwargs=kwargs,
        )
        self.calls.append(bound)
        return bound


class _BindAgent:
    """Only what _bind_job_tools / _graph_llm_bindings touch."""

    def __init__(self, config, tools):
        self.config = config
        self._tools = tools
        self._llm = self._strategic_llm = self._tactical_llm = _RecordingLLM()
        self._llm_with_tools = None
        self._strategic_llm_with_tools = None
        self._tactical_llm_with_tools = None


def _bind(config, tools) -> _BindAgent:
    from src.agent import UniversalAgent

    agent = _BindAgent(config, tools)
    UniversalAgent._bind_job_tools(agent)
    return agent


def _legacy_config():
    config = _make_config({})
    config.phase_settings.prompt_mode = PROMPT_MODE_LEGACY
    return config


class TestBindJobTools:
    def test_skills_mode_binds_the_union_once(self):
        config = _make_config({})
        tools = [_structured(n) for n in _BIND_TOOLS]
        agent = _bind(config, tools)

        (bound,) = agent._llm.calls
        assert sorted(bound.tool_names) == sorted(_BIND_TOOLS)
        assert agent._llm_with_tools is bound
        assert (
            agent._strategic_llm_with_tools
            is agent._tactical_llm_with_tools
            is agent._llm_with_tools
        )
        # Single-phase tools state their phase in the bound description;
        # both-phase tools do not (the family Examples block the guardrails
        # append for gemma's read_file is why these are prefix checks).
        assert bound.descriptions["job_complete"].startswith(
            "[strategic-phase tool] Does job_complete."
        )
        assert bound.descriptions["request_replan"].startswith(
            "[tactical-phase tool] Does request_replan."
        )
        assert bound.descriptions["read_file"].startswith("Does read_file.")
        assert "-phase tool]" not in bound.descriptions["read_file"]
        # ... on the bound copies only: the ToolNode's objects are untouched.
        assert agent._tools is tools
        assert [t.description for t in tools] == [f"Does {n}." for n in _BIND_TOOLS]
        # The parallel_tool_calls gate is applied once, from the config.
        llm_cfg = config.llm
        expected = (
            {"parallel_tool_calls": llm_cfg.parallel_tool_calls}
            if supports_parallel_tool_calls(llm_cfg.provider, llm_cfg.model)
            else {}
        )
        assert bound.kwargs == expected

    def test_legacy_prompt_mode_binds_the_filtered_pair(self):
        tools = [_structured(n) for n in _BIND_TOOLS]
        agent = _bind(_legacy_config(), tools)

        strategic, tactical = agent._llm.calls
        assert sorted(strategic.tool_names) == ["job_complete", "read_file"]
        assert sorted(tactical.tool_names) == ["read_file", "request_replan"]
        assert agent._strategic_llm_with_tools is strategic
        assert agent._tactical_llm_with_tools is tactical
        assert agent._llm_with_tools is strategic  # the old compat alias
        for binding in (strategic, tactical):  # arm A: descriptions as before
            assert not any("-phase tool]" in d for d in binding.descriptions.values())

    def test_unregistered_tools_are_bound_in_neither_mode(self):
        for config in (_make_config({}), _legacy_config()):
            agent = _bind(
                config, [_structured("read_file"), _structured("custom_unregistered")]
            )
            for binding in agent._llm.calls:
                assert "custom_unregistered" not in binding.tool_names
                assert "read_file" in binding.tool_names

    def test_graph_bindings_follow_the_binding_shape(self):
        from src.agent import UniversalAgent

        skills = _bind(_make_config({}), [_structured("read_file")])
        assert UniversalAgent._graph_llm_bindings(skills) == {
            "llm_with_tools": skills._llm_with_tools
        }
        legacy = _bind(_legacy_config(), [_structured("read_file")])
        assert UniversalAgent._graph_llm_bindings(legacy) == {
            "strategic_llm_with_tools": legacy._strategic_llm_with_tools,
            "tactical_llm_with_tools": legacy._tactical_llm_with_tools,
        }

    def test_before_tool_enforcement_survives_binding_in_both_modes(self):
        """The todo-guide gate wraps the ToolNode's tool objects in place;
        binding (copies for the schema) never undoes it, whichever mode."""
        for config in (_make_config({}), _legacy_config()):
            ctx = ToolContext()
            ctx._instruction_files = [
                InstructionFileEntry(
                    trigger="before_tool:next_phase_todos",
                    skill="todo-guide",
                    enforce=True,
                )
            ]
            tools = apply_instruction_enforcement(
                [_structured("next_phase_todos"), _structured("read_file")], ctx
            )
            gate = tools[0].func
            ctx.set_current_phase("strategic", phase_number=1, turn_count=1)
            assert "skills/todo-guide/SKILL.md" in gate()  # closed: guide unread

            agent = _bind(config, tools)
            assert agent._tools[0].func is gate  # what the ToolNode executes
            assert all("next_phase_todos" in b.tool_names for b in agent._llm.calls[:1])
