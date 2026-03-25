"""Unit tests for stuck agent detection and recovery.

Tests the refactored create_audited_tool_node function in src/graph.py,
covering: fingerprint-based loop warnings (soft, never blocking),
progress tracking, category failure logging, stuck detection with
reflection/freeze, hard caps, and tool-not-found enrichment.

See docs/features/stuck_agent_recovery.md for the full design.
"""

import json
import pytest
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from langchain_core.messages import AIMessage, ToolMessage, SystemMessage

# Add project root to path
project_root = Path(__file__).parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from src.graph import create_audited_tool_node  # noqa: E402
from src.core.loader import LimitsConfig  # noqa: E402


# =============================================================================
# Test fixtures and helpers
# =============================================================================


@dataclass
class FakeToolsConfig:
    workspace: list = field(default_factory=list)
    core: list = field(default_factory=list)
    document: list = field(default_factory=list)
    research: list = field(default_factory=list)
    citation: list = field(default_factory=list)
    graph: list = field(default_factory=list)
    sql: list = field(default_factory=list)
    mongodb: list = field(default_factory=list)
    git: list = field(default_factory=list)
    coding: list = field(default_factory=list)
    evaluation: list = field(default_factory=list)
    knowledge: list = field(default_factory=list)
    cloud: list = field(default_factory=list)
    communication: list = field(default_factory=list)


@dataclass
class FakeConfig:
    """Minimal config matching what create_audited_tool_node reads."""

    agent_id: str = "test_agent"
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    tools: FakeToolsConfig = field(default_factory=FakeToolsConfig)


def make_tool_call(name: str, args: dict = None, call_id: str = None):
    """Create a tool call dict matching LangChain format."""
    return {
        "name": name,
        "id": call_id or f"call_{name}",
        "args": args or {},
    }


def make_state(
    tool_calls: list,
    phase_number: int = 1,
    is_strategic: bool = False,
    job_id: str = "test-job",
):
    """Create a minimal state dict with an AIMessage containing tool_calls."""
    ai_msg = AIMessage(content="", tool_calls=tool_calls)
    return {
        "messages": [ai_msg],
        "job_id": job_id,
        "iteration": 1,
        "is_strategic_phase": is_strategic,
        "phase_number": phase_number,
        "metadata": {},
    }


def make_tool_result(name: str, content: str, call_id: str = None):
    """Create a ToolMessage result."""
    return ToolMessage(
        content=content,
        tool_call_id=call_id or f"call_{name}",
        name=name,
    )


@pytest.fixture
def config():
    """Default test config."""
    return FakeConfig()


@pytest.fixture
def low_threshold_config():
    """Config with low thresholds for easier testing."""
    return FakeConfig(
        limits=LimitsConfig(
            progress_stall_threshold=3,
            max_tool_calls_per_phase=10,
        )
    )


@pytest.fixture
def mock_tool_node():
    """Create a mock ToolNode that returns configurable results."""
    mock = AsyncMock()
    return mock


# =============================================================================
# Test: Fingerprint-based loop detection -> soft warnings (never blocks)
# =============================================================================


class TestFingerPrintWarning:
    """Tests for fingerprint-based loop detection with soft warnings."""

    @pytest.mark.asyncio
    async def test_identical_calls_get_warning(self, config):
        """After threshold identical calls, tool results get a LOOP WARNING."""
        fake_tool = MagicMock()
        fake_tool.name = "some_tool"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            # Call up to just under threshold (default 10 in last 30).
            # All calls should execute — loop detection is advisory.
            for i in range(9):
                call_id = f"call_{i}"
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [make_tool_result("some_tool", "ok", call_id)]
                    }
                )
                state = make_state([make_tool_call("some_tool", {}, call_id)])
                result = await audited(state)
                msgs = result.get("messages", [])
                tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
                assert len(tool_msgs) >= 1
                # No "restricted" — tools are never blocked
                assert not any("restricted" in (m.content or "") for m in tool_msgs)
                assert not any("LOOP WARNING" in (m.content or "") for m in tool_msgs)

            # Call 10: hits threshold (>= 10), should have LOOP WARNING appended
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [make_tool_result("some_tool", "ok", "call_9")]
                }
            )
            state = make_state([make_tool_call("some_tool", {}, "call_9")])
            result = await audited(state)
            msgs = result.get("messages", [])
            tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
            assert len(tool_msgs) >= 1
            # Tool still executed (result contains "ok") plus warning
            assert any("LOOP WARNING" in (m.content or "") for m in tool_msgs)
            assert any("ok" in (m.content or "") for m in tool_msgs)

    @pytest.mark.asyncio
    async def test_different_args_no_warning(self, config):
        """Different arguments should NOT trigger warnings."""
        fake_tool = MagicMock()
        fake_tool.name = "search"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={"messages": [make_tool_result("search", "results")]}
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            # 15 calls with different args: no warnings
            for i in range(15):
                state = make_state(
                    [make_tool_call("search", {"query": f"query_{i}"}, f"call_{i}")]
                )
                result = await audited(state)
                msgs = [
                    m
                    for m in result.get("messages", [])
                    if isinstance(m, ToolMessage)
                    and "LOOP WARNING" in (m.content or "")
                ]
                assert len(msgs) == 0

    @pytest.mark.asyncio
    async def test_warning_resets_on_phase_change(self, config):
        """Loop warning state should be cleared when phase changes."""
        fake_tool = MagicMock()
        fake_tool.name = "some_tool"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={"messages": [make_tool_result("some_tool", "ok")]}
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            # Phase 1: build up call history (9 identical calls, just under threshold)
            for i in range(9):
                state = make_state(
                    [make_tool_call("some_tool", {}, f"call_{i}")],
                    phase_number=1,
                )
                await audited(state)

            # Phase 2: state resets, so 1 more call should NOT trigger warning
            state = make_state(
                [make_tool_call("some_tool", {}, "call_new")],
                phase_number=2,
            )
            result = await audited(state)
            msgs = result.get("messages", [])
            tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
            assert not any("LOOP WARNING" in (m.content or "") for m in tool_msgs)

    @pytest.mark.asyncio
    async def test_tools_always_execute(self, config):
        """Tools should ALWAYS execute, even after warning threshold."""
        fake_tool = MagicMock()
        fake_tool.name = "stuck_tool"

        with patch("src.graph.ToolNode") as MockToolNode:
            call_count = [0]

            async def counting_ainvoke(state):
                call_count[0] += 1
                return {"messages": [make_tool_result("stuck_tool", "ok")]}

            mock_tn = AsyncMock()
            mock_tn.ainvoke = counting_ainvoke
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            # 15 identical calls — all should execute
            for i in range(15):
                state = make_state([make_tool_call("stuck_tool", {}, f"call_{i}")])
                await audited(state)

            assert call_count[0] == 15  # All 15 executed


# =============================================================================
# Test: Progress tracking
# =============================================================================


class TestProgressTracking:
    """Tests for progress-based stuck detection."""

    @pytest.mark.asyncio
    async def test_progress_tool_resets_counter(self, low_threshold_config):
        """Calling a progress tool should reset the stall counter."""
        fake_read = MagicMock()
        fake_read.name = "read_file"
        fake_complete = MagicMock()
        fake_complete.name = "todo_complete"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node(
                [fake_read, fake_complete], low_threshold_config
            )

            # 2 non-progress calls (different args to avoid fingerprint warnings)
            for i in range(2):
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [
                            make_tool_result("read_file", f"content_{i}", f"call_{i}")
                        ]
                    }
                )
                state = make_state(
                    [make_tool_call("read_file", {"path": f"f{i}"}, f"call_{i}")]
                )
                await audited(state)

            # 1 progress call (todo_complete) — resets counter.
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [make_tool_result("todo_complete", "Done", "call_p")]
                }
            )
            state = make_state(
                [make_tool_call("todo_complete", {"notes": "done"}, "call_p")]
            )
            result = await audited(state)
            # No stuck detection should have fired
            sys_msgs = [
                m for m in result.get("messages", []) if isinstance(m, SystemMessage)
            ]
            assert len(sys_msgs) == 0

            # 2 more non-progress calls — still under threshold (3)
            for i in range(2):
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [
                            make_tool_result("read_file", f"more_{i}", f"call_g{i}")
                        ]
                    }
                )
                state = make_state(
                    [make_tool_call("read_file", {"path": f"g{i}"}, f"call_g{i}")]
                )
                result = await audited(state)
                sys_msgs = [
                    m
                    for m in result.get("messages", [])
                    if isinstance(m, SystemMessage)
                ]
                assert len(sys_msgs) == 0  # Not stuck yet

    @pytest.mark.asyncio
    async def test_failed_progress_tool_does_not_reset(self, low_threshold_config):
        """A progress tool that errors should NOT reset the counter."""
        fake_complete = MagicMock()
        fake_complete.name = "todo_complete"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_complete], low_threshold_config)

            # 3 calls to todo_complete that all error (threshold = 3).
            # Use different args each time to avoid fingerprint warnings.
            for i in range(3):
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [
                            make_tool_result(
                                "todo_complete",
                                f"Error: todo_{i} not found",
                                f"call_tc{i}",
                            )
                        ]
                    }
                )
                state = make_state(
                    [
                        make_tool_call(
                            "todo_complete", {"notes": f"attempt_{i}"}, f"call_tc{i}"
                        )
                    ]
                )
                result = await audited(state)

            # Should have triggered progress nudge (errors don't count as progress)
            all_msgs = result.get("messages", [])
            sys_msgs = [m for m in all_msgs if isinstance(m, SystemMessage)]
            assert len(sys_msgs) == 1
            assert "OBSERVATION:" in sys_msgs[0].content


# =============================================================================
# Test: Stuck detection -> reflection -> freeze
# =============================================================================


class TestStuckDetectionEscalation:
    """Tests for the stuck detection escalation chain."""

    @pytest.mark.asyncio
    async def test_nudge_injected_at_threshold(self, low_threshold_config):
        """Progress nudge should inject a SystemMessage at threshold."""
        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node(
                [fake_tool],
                low_threshold_config,  # threshold=3
            )

            # 3 non-progress calls (different args to avoid fingerprint warnings)
            for i in range(3):
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [
                            make_tool_result("read_file", f"content_{i}", f"call_{i}")
                        ]
                    }
                )
                state = make_state(
                    [
                        make_tool_call(
                            "read_file", {"path": f"file_{i}.txt"}, f"call_{i}"
                        )
                    ]
                )
                result = await audited(state)

            # 3rd call should trigger progress nudge
            all_msgs = result.get("messages", [])
            sys_msgs = [m for m in all_msgs if isinstance(m, SystemMessage)]
            assert len(sys_msgs) == 1
            assert "OBSERVATION:" in sys_msgs[0].content
            assert "write it to a file" in sys_msgs[0].content
            assert not result.get("should_stop", False)

    @pytest.mark.asyncio
    async def test_no_freeze_after_nudge(self, low_threshold_config):
        """Agent should never be frozen by progress nudge — only nudged repeatedly."""
        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node(
                [fake_tool],
                low_threshold_config,  # threshold=3
            )

            # 6 non-progress calls (2x threshold): should get nudges but never freeze
            for i in range(6):
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [
                            make_tool_result("read_file", f"c_{i}", f"call_{i}")
                        ]
                    }
                )
                state = make_state(
                    [make_tool_call("read_file", {"path": f"f_{i}"}, f"call_{i}")]
                )
                result = await audited(state)

            assert not result.get("should_stop", False)
            assert result.get("freeze_data") is None

    @pytest.mark.asyncio
    async def test_progress_after_nudge_resets(self, low_threshold_config):
        """Progress after nudge should reset the counter."""
        fake_read = MagicMock()
        fake_read.name = "read_file"
        fake_write = MagicMock()
        fake_write.name = "write_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node(
                [fake_read, fake_write], low_threshold_config
            )

            # 3 non-progress calls: triggers nudge
            for i in range(3):
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [
                            make_tool_result("read_file", f"c_{i}", f"call_{i}")
                        ]
                    }
                )
                state = make_state(
                    [make_tool_call("read_file", {"path": f"f_{i}"}, f"call_{i}")]
                )
                await audited(state)

            # Now make progress with write_file
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [make_tool_result("write_file", "Written", "call_w")]
                }
            )
            state = make_state(
                [make_tool_call("write_file", {"path": "out.md"}, "call_w")]
            )
            result = await audited(state)
            assert not result.get("should_stop", False)

            # 3 more non-progress calls: should trigger another nudge
            for i in range(3):
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [
                            make_tool_result("read_file", f"d_{i}", f"call_d{i}")
                        ]
                    }
                )
                state = make_state(
                    [make_tool_call("read_file", {"path": f"g_{i}"}, f"call_d{i}")]
                )
                result = await audited(state)

            # Should get another nudge (not a freeze)
            sys_msgs = [
                m for m in result.get("messages", []) if isinstance(m, SystemMessage)
            ]
            assert len(sys_msgs) == 1
            assert "OBSERVATION:" in sys_msgs[0].content
            assert not result.get("should_stop", False)


# =============================================================================
# Test: Hard cap
# =============================================================================


class TestHardCap:
    """Tests for the hard budget cap per phase."""

    @pytest.mark.asyncio
    async def test_hard_cap_rewinds_tactical_phase(self):
        """Exceeding hard cap in tactical phase should rewind, not freeze."""
        cfg = FakeConfig(limits=LimitsConfig(max_tool_calls_per_phase=5))
        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        mock_todo = MagicMock()
        mock_todo.archive_with_failure_note = MagicMock(return_value="Archived")
        mock_todo.has_staged_todos = MagicMock(return_value=False)
        mock_ctx = MagicMock()
        mock_ctx.workspace_manager = MagicMock()
        mock_ctx.todo_manager = mock_todo
        mock_ctx.consume_freeze_request.return_value = None
        mock_ctx.drain_pending_memories.return_value = []

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={"messages": [make_tool_result("read_file", "ok")]}
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], cfg, tool_context=mock_ctx)

            # 5 calls: within cap
            for i in range(5):
                state = make_state(
                    [make_tool_call("read_file", {"path": f"f_{i}"}, f"call_{i}")]
                )
                result = await audited(state)
                assert not result.get("should_stop", False)

            # 6th call: exceeds cap — should rewind, NOT freeze
            state = make_state([make_tool_call("read_file", {"path": "f_6"}, "call_6")])
            result = await audited(state)
            assert not result.get("should_stop", False)
            # Should have rewind message
            sys_msgs = [
                m for m in result.get("messages", []) if isinstance(m, SystemMessage)
            ]
            assert len(sys_msgs) == 1
            assert "PHASE BUDGET" in sys_msgs[0].content
            assert "next_phase_todos" in sys_msgs[0].content
            # Todos should have been archived
            mock_todo.archive_with_failure_note.assert_called_once()

    @pytest.mark.asyncio
    async def test_hard_cap_freezes_strategic_phase(self):
        """Exceeding hard cap in strategic phase should freeze."""
        cfg = FakeConfig(limits=LimitsConfig(max_tool_calls_per_phase=5))
        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        mock_ws = MagicMock()
        mock_ws.git_manager = MagicMock()
        mock_ws.git_manager.is_active = False
        mock_ctx = MagicMock()
        mock_ctx.workspace_manager = mock_ws
        mock_ctx.consume_freeze_request.return_value = None
        mock_ctx.drain_pending_memories.return_value = []

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={"messages": [make_tool_result("read_file", "ok")]}
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], cfg, tool_context=mock_ctx)

            # 6 calls in strategic phase: exceeds cap
            for i in range(6):
                state = make_state(
                    [make_tool_call("read_file", {"path": f"f_{i}"}, f"call_{i}")],
                    is_strategic=True,
                )
                result = await audited(state)

            assert result.get("should_stop") is True
            msgs = result.get("messages", [])
            assert any("phase frozen" in m.content.lower() for m in msgs)
            assert any(isinstance(m, ToolMessage) for m in msgs)

    @pytest.mark.asyncio
    async def test_hard_cap_resets_on_phase_change(self):
        """Hard cap counter should reset when phase changes."""
        cfg = FakeConfig(limits=LimitsConfig(max_tool_calls_per_phase=5))
        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={"messages": [make_tool_result("read_file", "ok")]}
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], cfg)

            # Phase 1: use 4 of 5 budget
            for i in range(4):
                state = make_state(
                    [make_tool_call("read_file", {"path": f"f_{i}"}, f"call_{i}")],
                    phase_number=1,
                )
                await audited(state)

            # Phase 2: counter resets, can do 5 more
            for i in range(5):
                state = make_state(
                    [make_tool_call("read_file", {"path": f"g_{i}"}, f"call_g{i}")],
                    phase_number=2,
                )
                result = await audited(state)
                assert not result.get("should_stop", False)


# =============================================================================
# Test: Tool-not-found enrichment
# =============================================================================


class TestToolNotFoundEnrichment:
    """Tests for enriching tool-not-found errors with guidance."""

    @pytest.mark.asyncio
    async def test_unknown_tool_gets_guidance(self, config):
        """Tool-not-found errors should get actionable guidance appended."""
        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            # Simulate ToolNode's response for unknown tool
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        ToolMessage(
                            content="Error: kb_list is not a valid tool, try one of [read_file].",
                            tool_call_id="call_kb",
                            name="kb_list",
                            status="error",
                        )
                    ]
                }
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            state = make_state([make_tool_call("kb_list", {}, "call_kb")])
            result = await audited(state)

            msgs = result.get("messages", [])
            tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
            assert len(tool_msgs) >= 1
            assert "todo_complete" in tool_msgs[0].content
            assert "blocked" in tool_msgs[0].content

    @pytest.mark.asyncio
    async def test_normal_error_not_enriched(self, config):
        """Normal tool errors should NOT get the not-found guidance."""
        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        make_tool_result(
                            "read_file", "Error: file not found: missing.txt"
                        )
                    ]
                }
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            state = make_state(
                [make_tool_call("read_file", {"path": "missing.txt"}, "call_rf")]
            )
            result = await audited(state)

            msgs = result.get("messages", [])
            tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
            assert len(tool_msgs) >= 1
            # Should NOT have the "blocked" guidance
            assert "blocked" not in tool_msgs[0].content


# =============================================================================
# Test: Category failure tracking (logging only, no masking)
# =============================================================================


class TestCategoryFailureTracking:
    """Tests for category-wide failure logging (no masking)."""

    @pytest.mark.asyncio
    async def test_category_failures_logged_not_masked(self, config):
        """3 distinct tools in same category failing should log, NOT mask."""
        fake_tools = []
        for name in ["kb_read", "kb_write", "kb_search", "kb_list", "read_file"]:
            t = MagicMock()
            t.name = name
            fake_tools.append(t)

        # Mock TOOL_REGISTRY to return "knowledge" category for kb_* tools
        mock_registry = {
            "kb_read": {"category": "knowledge"},
            "kb_write": {"category": "knowledge"},
            "kb_search": {"category": "knowledge"},
            "kb_list": {"category": "knowledge"},
            "read_file": {"category": "workspace"},
        }

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node(fake_tools, config)

            # Patch the registry lookups inside the closure helpers
            with patch("src.tools.registry.TOOL_REGISTRY", mock_registry):
                # 3 different kb tools all fail
                for i, name in enumerate(["kb_read", "kb_write", "kb_search"]):
                    mock_tn.ainvoke = AsyncMock(
                        return_value={
                            "messages": [
                                make_tool_result(
                                    name,
                                    "Error: knowledge store not configured",
                                    f"call_{name}",
                                )
                            ]
                        }
                    )
                    state = make_state(
                        [make_tool_call(name, {"id": f"note_{i}"}, f"call_{name}")]
                    )
                    await audited(state)

                # Now kb_list (same category) should still execute (no masking)
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [
                            make_tool_result(
                                "kb_list", "Error: not configured", "call_kl"
                            )
                        ]
                    }
                )
                state = make_state([make_tool_call("kb_list", {}, "call_kl")])
                result = await audited(state)
                msgs = result.get("messages", [])
                tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
                # Tool should have executed (no "restricted" message)
                assert not any("restricted" in (m.content or "") for m in tool_msgs)
                # But the error from the tool itself should be there
                assert any("not configured" in (m.content or "") for m in tool_msgs)


# =============================================================================
# Test: Structured freeze payload
# =============================================================================


class TestFreezePayload:
    """Tests for the structured freeze payload content."""

    @pytest.mark.asyncio
    async def test_progress_nudge_repeats_periodically(self):
        """Progress nudge should fire every threshold interval, never freeze."""
        cfg = FakeConfig(limits=LimitsConfig(progress_stall_threshold=2))
        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], cfg)

            nudge_counts = 0
            # 6 calls = 3x threshold of 2
            for i in range(6):
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [
                            make_tool_result("read_file", f"c_{i}", f"call_{i}")
                        ]
                    }
                )
                state = make_state(
                    [make_tool_call("read_file", {"path": f"f_{i}"}, f"call_{i}")]
                )
                result = await audited(state)
                sys_msgs = [
                    m
                    for m in result.get("messages", [])
                    if isinstance(m, SystemMessage) and "OBSERVATION:" in m.content
                ]
                nudge_counts += len(sys_msgs)

            # Should have gotten nudges at calls 2, 4, 6
            assert nudge_counts == 3
            # Never frozen
            assert not result.get("should_stop", False)

    @pytest.mark.asyncio
    async def test_hard_cap_strategic_freeze_has_required_fields(self):
        """Hard cap freeze in strategic phase should contain budget information."""
        cfg = FakeConfig(limits=LimitsConfig(max_tool_calls_per_phase=2))
        fake_tool = MagicMock()
        fake_tool.name = "read_file"

        mock_ws = MagicMock()
        mock_ws.git_manager = MagicMock()
        mock_ws.git_manager.is_active = False
        mock_ctx = MagicMock()
        mock_ctx.workspace_manager = mock_ws
        mock_ctx.consume_freeze_request.return_value = None
        mock_ctx.drain_pending_memories.return_value = []

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={"messages": [make_tool_result("read_file", "ok")]}
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], cfg, tool_context=mock_ctx)

            # 3 calls in strategic phase: exceeds cap of 2
            for i in range(3):
                state = make_state(
                    [make_tool_call("read_file", {"path": f"f_{i}"}, f"call_{i}")],
                    is_strategic=True,
                )
                result = await audited(state)

            assert result.get("should_stop") is True
            mock_ws.write_file.assert_called_once()
            freeze_json = mock_ws.write_file.call_args[0][1]
            payload = json.loads(freeze_json)

            assert payload["freeze_type"] == "budget_exceeded"
            assert "tool_calls_this_phase" in payload
            assert (
                payload["tool_calls_this_phase"] > cfg.limits.max_tool_calls_per_phase
            )


# =============================================================================
# Test: todo_rewind resets loop detection
# =============================================================================


class TestTodoRewindReset:
    """Tests for todo_rewind resetting loop detection state."""

    @pytest.mark.asyncio
    async def test_rewind_clears_loop_state(self, config):
        """Successful todo_rewind should reset loop detection state."""
        fake_tool = MagicMock()
        fake_tool.name = "some_tool"
        fake_rewind = MagicMock()
        fake_rewind.name = "todo_rewind"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool, fake_rewind], config)

            # Build up 9 identical calls (just under threshold of 10)
            mock_tn.ainvoke = AsyncMock(
                return_value={"messages": [make_tool_result("some_tool", "ok")]}
            )
            for i in range(9):
                state = make_state([make_tool_call("some_tool", {}, f"call_{i}")])
                await audited(state)

            # Rewind — should reset loop detection
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        make_tool_result(
                            "todo_rewind",
                            "Archived 5 todos. To recover...",
                            "call_rw",
                        )
                    ]
                }
            )
            state = make_state(
                [make_tool_call("todo_rewind", {"issue": "approach broken"}, "call_rw")]
            )
            await audited(state)

            # Now 9 more identical calls should NOT trigger warning
            # (because rewind cleared the history)
            mock_tn.ainvoke = AsyncMock(
                return_value={"messages": [make_tool_result("some_tool", "ok again")]}
            )
            for i in range(9):
                state = make_state([make_tool_call("some_tool", {}, f"call_post_{i}")])
                result = await audited(state)
                msgs = result.get("messages", [])
                tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
                assert not any("LOOP WARNING" in (m.content or "") for m in tool_msgs)

    @pytest.mark.asyncio
    async def test_failed_rewind_does_not_reset(self, config):
        """A failed todo_rewind should NOT reset loop detection state."""
        fake_tool = MagicMock()
        fake_tool.name = "some_tool"
        fake_rewind = MagicMock()
        fake_rewind.name = "todo_rewind"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool, fake_rewind], config)

            # Build up 9 identical calls
            mock_tn.ainvoke = AsyncMock(
                return_value={"messages": [make_tool_result("some_tool", "ok")]}
            )
            for i in range(9):
                state = make_state([make_tool_call("some_tool", {}, f"call_{i}")])
                await audited(state)

            # Failed rewind — should NOT reset
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        make_tool_result(
                            "todo_rewind",
                            "Error: You must provide an 'issue' describing why",
                            "call_rw",
                        )
                    ]
                }
            )
            state = make_state(
                [make_tool_call("todo_rewind", {"issue": ""}, "call_rw")]
            )
            await audited(state)

            # 1 more identical call should now trigger warning (9+1 = 10 = threshold)
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        make_tool_result("some_tool", "ok still", "call_extra")
                    ]
                }
            )
            state = make_state([make_tool_call("some_tool", {}, "call_extra")])
            result = await audited(state)
            msgs = result.get("messages", [])
            tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
            assert any("LOOP WARNING" in (m.content or "") for m in tool_msgs)


# =============================================================================
# Test: Phase gate defense-in-depth (rejects wrong-phase tool calls)
# =============================================================================


class TestPhaseGate:
    """Tests for the defense-in-depth phase gate in audited_tools.

    Primary enforcement is LLM schema binding (tested via test_tool_registry.py).
    These tests cover the secondary gate inside audited_tools that catches
    hallucinated tool calls not matching the current phase.
    """

    @pytest.mark.asyncio
    async def test_registered_tool_rejected_in_wrong_phase(self, config):
        """A tool registered as strategic-only is rejected in tactical phase."""
        fake_tool = MagicMock()
        fake_tool.name = "job_complete"  # strategic-only in registry

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            # Call in tactical phase — should be rejected by phase gate
            state = make_state(
                [
                    make_tool_call(
                        "job_complete",
                        {"summary": "done", "deliverables": []},
                        "call_jc",
                    )
                ],
                is_strategic=False,
            )
            result = await audited(state)
            msgs = result.get("messages", [])
            tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]

            assert len(tool_msgs) == 1
            assert "not available in the tactical phase" in tool_msgs[0].content
            assert tool_msgs[0].tool_call_id == "call_jc"

            # ToolNode should NOT have been called
            mock_tn.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_registered_tool_allowed_in_correct_phase(self, config):
        """A tool registered as strategic-only passes in strategic phase."""
        fake_tool = MagicMock()
        fake_tool.name = "job_complete"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [make_tool_result("job_complete", "ok", "call_jc")]
                }
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            # Call in strategic phase — should pass through to ToolNode
            state = make_state(
                [make_tool_call("job_complete", {}, "call_jc")],
                is_strategic=True,
            )
            result = await audited(state)
            msgs = result.get("messages", [])
            tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]

            assert len(tool_msgs) == 1
            assert tool_msgs[0].content == "ok"
            mock_tn.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_tactical_only_tool_rejected_in_strategic(self, config):
        """todo_rewind (tactical-only) is rejected in strategic phase."""
        fake_tool = MagicMock()
        fake_tool.name = "todo_rewind"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            state = make_state(
                [make_tool_call("todo_rewind", {"issue": "stuck"}, "call_rw")],
                is_strategic=True,
            )
            result = await audited(state)
            msgs = result.get("messages", [])
            tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]

            assert len(tool_msgs) == 1
            assert "not available in the strategic phase" in tool_msgs[0].content
            mock_tn.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregistered_tool_passes_through(self, config):
        """Tools not in TOOL_REGISTRY are not phase-gated (test/dynamic tools)."""
        fake_tool = MagicMock()
        fake_tool.name = "custom_dynamic_tool"  # Not in TOOL_REGISTRY

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        make_tool_result("custom_dynamic_tool", "executed", "call_cd")
                    ]
                }
            )
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            # Should pass through regardless of phase
            state = make_state(
                [make_tool_call("custom_dynamic_tool", {}, "call_cd")],
                is_strategic=False,
            )
            result = await audited(state)
            msgs = result.get("messages", [])
            tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]

            assert len(tool_msgs) == 1
            assert tool_msgs[0].content == "executed"
            mock_tn.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_rejected_when_any_call_violates(self, config):
        """If any tool call in a batch violates phase, entire batch is rejected."""
        fake_read = MagicMock()
        fake_read.name = "read_file"  # both phases
        fake_jc = MagicMock()
        fake_jc.name = "job_complete"  # strategic only

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_read, fake_jc], config)

            # Tactical phase: read_file is fine, job_complete is not
            state = make_state(
                [
                    make_tool_call("read_file", {"path": "x"}, "call_rf"),
                    make_tool_call("job_complete", {}, "call_jc"),
                ],
                is_strategic=False,
            )
            result = await audited(state)
            msgs = result.get("messages", [])
            tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]

            # Both calls get error responses (entire batch rejected)
            assert len(tool_msgs) == 2
            mock_tn.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_phase_tool_passes_in_either(self, config):
        """Tools declared for both phases work in both."""
        fake_tool = MagicMock()
        fake_tool.name = "read_file"  # both phases in registry

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            MockToolNode.return_value = mock_tn

            audited = create_audited_tool_node([fake_tool], config)

            for is_strategic in [True, False]:
                mock_tn.ainvoke = AsyncMock(
                    return_value={
                        "messages": [
                            make_tool_result("read_file", "content", "call_rf")
                        ]
                    }
                )
                state = make_state(
                    [make_tool_call("read_file", {}, "call_rf")],
                    is_strategic=is_strategic,
                )
                result = await audited(state)
                tool_msgs = [
                    m for m in result.get("messages", []) if isinstance(m, ToolMessage)
                ]
                assert tool_msgs[0].content == "content"
