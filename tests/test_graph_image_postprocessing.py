"""Integration tests for image post-processing in src/graph.py audited_tools.

Verifies that when LangGraph's ToolNode returns a ToolMessage whose
content includes an `<image_data>` or `<page_image>` tag, the audited
tool node:

1. Strips the base64 from the ToolMessage content (replaces with marker).
2. Appends a synthesized HumanMessage carrying the image as a real
   provider content block (`{"type": "image_url", ...}`).
3. Leaves non-image tool results untouched (fast path).
4. Preserves message ordering: every ToolMessage from this batch comes
   before the synthesized HumanMessage(s) so providers that require
   tool_use answering (Anthropic) stay valid.
"""

from __future__ import annotations

import base64
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

project_root = Path(__file__).parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from src.core.loader import LimitsConfig  # noqa: E402
from src.graph import create_audited_tool_node  # noqa: E402
from src.services.image_content import IMAGE_DATA_TAG_TEMPLATE  # noqa: E402


# =============================================================================
# Test fixtures and helpers
# =============================================================================


@dataclass
class _FakeToolsConfig:
    workspace: list = field(default_factory=list)
    core: list = field(default_factory=list)
    document: list = field(default_factory=list)
    research: list = field(default_factory=list)
    citation: list = field(default_factory=list)
    graph: list = field(default_factory=list)
    sql: list = field(default_factory=list)
    mongodb: list = field(default_factory=list)
    git: list = field(default_factory=list)
    shell: list = field(default_factory=list)
    evaluation: list = field(default_factory=list)
    knowledge: list = field(default_factory=list)
    cloud: list = field(default_factory=list)
    communication: list = field(default_factory=list)


@dataclass
class _FakeLLMConfig:
    model: Optional[str] = None


@dataclass
class _FakeConfig:
    agent_id: str = "test_agent"
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    tools: _FakeToolsConfig = field(default_factory=_FakeToolsConfig)
    llm: _FakeLLMConfig = field(default_factory=_FakeLLMConfig)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _make_state(tool_calls: list, phase_number: int = 1):
    ai_msg = AIMessage(content="", tool_calls=tool_calls)
    return {
        "messages": [ai_msg],
        "job_id": "test-job",
        "iteration": 1,
        "is_strategic_phase": False,
        "phase_number": phase_number,
        "metadata": {},
    }


def _make_tool_call(name: str, call_id: str, args: dict | None = None):
    return {"name": name, "id": call_id, "args": args or {}}


@pytest.fixture
def config():
    return _FakeConfig()


# =============================================================================
# Tests
# =============================================================================


class TestAuditedToolsImagePostProcessor:
    """Verify image-tag stripping + HumanMessage injection in audited_tools."""

    @pytest.mark.asyncio
    async def test_image_data_tag_stripped_and_humanmessage_appended(self, config):
        """A ToolMessage with <image_data> → cleaned content + HumanMessage with image_url block."""
        b64 = _b64("fake-jpeg-bytes")
        raw_content = (
            "[IMAGE: photo.jpg]\nType: image/jpeg\nSize: 16 bytes\n\n"
            + IMAGE_DATA_TAG_TEMPLATE.format(mime="image/jpeg", b64=b64)
        )

        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        ToolMessage(
                            content=raw_content,
                            tool_call_id="call_read",
                            name="read_file",
                        )
                    ]
                }
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)
            state = _make_state([_make_tool_call("read_file", "call_read")])
            result = await audited(state)

        msgs = result["messages"]
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        human_msgs = [m for m in msgs if isinstance(m, HumanMessage)]

        # ToolMessage content cleaned: marker present, no base64
        assert len(tool_msgs) == 1
        assert b64 not in tool_msgs[0].content
        assert "image attached: image/jpeg" in tool_msgs[0].content
        # The leading metadata header is preserved
        assert "[IMAGE: photo.jpg]" in tool_msgs[0].content

        # Synthesized HumanMessage carries the image as a real content block
        assert len(human_msgs) == 1
        content = human_msgs[0].content
        assert isinstance(content, list)
        # First part is the text prefix referencing the tool call
        assert content[0]["type"] == "text"
        assert "call_read" in content[0]["text"]
        # Second part is the image content block
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"] == f"data:image/jpeg;base64,{b64}"

    @pytest.mark.asyncio
    async def test_humanmessage_appended_after_toolmessage(self, config):
        """Provider safety: HumanMessage must come AFTER the ToolMessage."""
        b64 = _b64("bytes")
        raw = "Header\n" + IMAGE_DATA_TAG_TEMPLATE.format(mime="image/png", b64=b64)

        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        ToolMessage(content=raw, tool_call_id="c1", name="read_file")
                    ]
                }
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)
            state = _make_state([_make_tool_call("read_file", "c1")])
            result = await audited(state)

        msgs = result["messages"]
        # Find the indices of ToolMessage and HumanMessage in the result
        tool_idx = next(i for i, m in enumerate(msgs) if isinstance(m, ToolMessage))
        human_idx = next(i for i, m in enumerate(msgs) if isinstance(m, HumanMessage))
        assert tool_idx < human_idx

    @pytest.mark.asyncio
    async def test_no_image_tag_no_extra_humanmessage(self, config):
        """Fast path: a normal text-only tool result is left untouched."""
        fake_tool = MagicMock()
        fake_tool.name = "search"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        ToolMessage(
                            content="just a text result",
                            tool_call_id="c1",
                            name="search",
                        )
                    ]
                }
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)
            state = _make_state([_make_tool_call("search", "c1")])
            result = await audited(state)

        msgs = result["messages"]
        assert not any(isinstance(m, HumanMessage) for m in msgs)
        # ToolMessage content unchanged
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content == "just a text result"

    @pytest.mark.asyncio
    async def test_two_image_tool_calls_both_extracted(self, config):
        """Two image-bearing tool results in one batch each get a HumanMessage."""
        b64_a = _b64("first-image")
        b64_b = _b64("second-image")
        raw_a = IMAGE_DATA_TAG_TEMPLATE.format(mime="image/png", b64=b64_a)
        raw_b = IMAGE_DATA_TAG_TEMPLATE.format(mime="image/jpeg", b64=b64_b)

        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        ToolMessage(content=raw_a, tool_call_id="c_a"),
                        ToolMessage(content=raw_b, tool_call_id="c_b"),
                    ]
                }
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)
            state = _make_state(
                [
                    _make_tool_call("read_file", "c_a"),
                    _make_tool_call("read_file", "c_b"),
                ]
            )
            result = await audited(state)

        human_msgs = [m for m in result["messages"] if isinstance(m, HumanMessage)]
        assert len(human_msgs) == 2

        # Both ToolMessages cleaned
        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        for tm in tool_msgs:
            assert "image attached: image/" in tm.content
            assert b64_a not in tm.content
            assert b64_b not in tm.content

        # All ToolMessages come before all HumanMessages
        last_tool_idx = max(
            i for i, m in enumerate(result["messages"]) if isinstance(m, ToolMessage)
        )
        first_human_idx = min(
            i for i, m in enumerate(result["messages"]) if isinstance(m, HumanMessage)
        )
        assert last_tool_idx < first_human_idx

    @pytest.mark.asyncio
    async def test_mixed_image_and_text_results(self, config):
        """One image result + one text result → only one HumanMessage appended."""
        b64 = _b64("only-this-one")
        raw_image = IMAGE_DATA_TAG_TEMPLATE.format(mime="image/png", b64=b64)

        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        ToolMessage(content="plain text result", tool_call_id="c1"),
                        ToolMessage(content=raw_image, tool_call_id="c2"),
                    ]
                }
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)
            state = _make_state(
                [
                    _make_tool_call("read_file", "c1"),
                    _make_tool_call("read_file", "c2"),
                ]
            )
            result = await audited(state)

        human_msgs = [m for m in result["messages"] if isinstance(m, HumanMessage)]
        assert len(human_msgs) == 1
        # The HumanMessage references the image-bearing call_id, not the text one
        assert "c2" in human_msgs[0].content[0]["text"]
