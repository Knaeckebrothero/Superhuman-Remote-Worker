"""Per-family mutation smoke tests against live endpoints (Slice D rung 3).

Chat-template strictness is the least documented layer of the model-swap
problem: whether a server accepts {tool-bearing history} × {some tools
removed, all tools removed, model swapped} is decided by its (often
hand-written) template, not its API docs. These smokes drive the repo's own
transport (``create_llm``) against real endpoints and assert the mutations
our live-session settings allow never produce a 4xx/template crash.

Opt-in: set ``SRW_SMOKE_LLM_ENDPOINTS`` to a JSON list of endpoint specs —

    export SRW_SMOKE_LLM_ENDPOINTS='[
      {"model": "gemma-4-moe", "base_url": "https://host/v1", "api_key": "..."},
      {"model": "MiniMax-M3", "base_url": "https://api.minimax.io/v1",
       "api_key": "...", "provider": "openai"}
    ]'

Unset → the whole module skips (CI never hits the network). Each entry gets
the full mutation matrix. Offline unit coverage for the sanitizer/ladder is
in ``tests/test_model_swap_hardening.py``.
"""

import json
import os

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent.core.context import sanitize_history_for_provider_boundary
from shared.runtime.core.loader import LLMConfig, create_llm

_RAW = os.environ.get("SRW_SMOKE_LLM_ENDPOINTS", "")

pytestmark = pytest.mark.skipif(
    not _RAW,
    reason="SRW_SMOKE_LLM_ENDPOINTS not set — live LLM mutation smokes are opt-in",
)


def _endpoints() -> list:
    try:
        return json.loads(_RAW) if _RAW else []
    except json.JSONDecodeError:
        return []


def _endpoint_id(spec) -> str:
    return spec.get("model", "unknown")


@tool
def get_time(timezone: str = "UTC") -> str:
    """Return the current time in the given timezone."""
    return "12:00"


@tool
def read_note(name: str) -> str:
    """Read a stored note by name."""
    return f"note {name}: hello"


@tool
def list_notes() -> str:
    """List the names of all stored notes."""
    return "alpha, beta"


ALL_TOOLS = [get_time, read_note, list_notes]


def _build_llm(spec: dict):
    cfg = LLMConfig(
        model=spec["model"],
        base_url=spec.get("base_url"),
        api_key=spec.get("api_key"),
        provider=spec.get("provider"),
        temperature=0.0,
    )
    return create_llm(cfg)


def _native_tool_history(llm) -> list:
    """Produce a tool-bearing history in the endpoint's own native format.

    Asks the model to call ``get_time``; if it declines (temperature 0 makes
    this rare but possible), falls back to a synthetic generic-format history
    so the mutation cases still run.
    """
    prompt = HumanMessage(
        content="Call the get_time tool for UTC, then tell me the time."
    )
    response = llm.bind_tools(ALL_TOOLS).invoke([prompt])
    if getattr(response, "tool_calls", None):
        results = [
            ToolMessage(content="12:00", tool_call_id=tc["id"])
            for tc in response.tool_calls
        ]
        return [prompt, response, *results]
    return [
        prompt,
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_smoke0001",
                    "name": "get_time",
                    "args": {"timezone": "UTC"},
                }
            ],
        ),
        ToolMessage(content="12:00", tool_call_id="call_smoke0001"),
    ]


def _assert_answers(llm, history: list) -> None:
    """The request must complete without a 4xx/template error and produce an
    AIMessage. Empty content is tolerated only when the model chose another
    tool call — what we are smoking is history *acceptance*."""
    response = llm.invoke([*history, HumanMessage(content="Now answer in one word.")])
    assert isinstance(response, AIMessage)
    assert response.content or getattr(response, "tool_calls", None)


@pytest.mark.parametrize("spec", _endpoints(), ids=_endpoint_id)
class TestFamilyMutationMatrix:
    def test_some_tools_removed(self, spec):
        """History references a tool that is no longer bound."""
        llm = _build_llm(spec)
        history = _native_tool_history(llm)
        _assert_answers(llm.bind_tools([read_note, list_notes]), history)

    def test_all_requested_tools_removed_floor_bound(self, spec):
        """Everything the user disabled is gone; the never-bind-zero floor
        leaves one unrelated tool (prod behavior after 'remove all')."""
        llm = _build_llm(spec)
        history = _native_tool_history(llm)
        _assert_answers(llm.bind_tools([list_notes]), history)

    def test_no_tools_bound_at_all(self, spec):
        """The poisoned case the floor guard exists for — documents per-family
        behavior. Tool-bearing history, zero tools in the request."""
        llm = _build_llm(spec)
        history = _native_tool_history(llm)
        _assert_answers(llm, history)

    def test_cross_provider_swapped_history_sanitized(self, spec):
        """A foreign (Anthropic-shaped) history — signed thinking blocks,
        toolu ids, one deliberately malformed id — must pass after the
        boundary sanitizer runs."""
        foreign_ok = "toolu_01AbCdEfGhIjKlMnOpQrSt"
        foreign_bad = "toolu_01+bad/id!with#chars$that&no*provider(accepts)"
        history = [
            HumanMessage(content="What time is it and what notes exist?"),
            AIMessage(
                content=[
                    {
                        "type": "thinking",
                        "thinking": "I should check.",
                        "signature": "x",
                    },
                    {"type": "text", "text": "Checking."},
                ],
                tool_calls=[
                    {"id": foreign_ok, "name": "get_time", "args": {"timezone": "UTC"}},
                    {"id": foreign_bad, "name": "list_notes", "args": {}},
                ],
                additional_kwargs={"reasoning_content": "foreign chain of thought"},
            ),
            ToolMessage(content="12:00", tool_call_id=foreign_ok),
            ToolMessage(content="alpha, beta", tool_call_id=foreign_bad),
        ]
        sanitized = sanitize_history_for_provider_boundary(history, spec["model"])
        llm = _build_llm(spec)
        _assert_answers(llm.bind_tools(ALL_TOOLS), sanitized)
