"""Unit tests for heartbeat metric collection in agent entrypoints."""

from unittest.mock import MagicMock, patch

import pytest

from src.api import app as worker_app
from src.api import dual_app
from src.services.recall_store import memory_health
from src.tools.context import ToolContext


def _build_agent_with_graph_progress(value: int) -> object:
    context = ToolContext()
    for _ in range(value):
        context.next_graph_progress()

    agent = MagicMock()
    agent._tool_context = context
    return agent


def _build_fake_psutil(mem_mb: int = 100, cpu: float = 5.5, pids: int = 7):
    process = MagicMock()
    process.memory_info.return_value.rss = mem_mb * 1_048_576
    process.cpu_percent.return_value = cpu

    psutil = MagicMock()
    psutil.Process.return_value = process
    psutil.net_connections.return_value = [MagicMock(status="LISTEN") for _ in range(2)]
    psutil.pids.return_value = list(range(pids))
    return psutil


class TestWorkerAppMetrics:
    """Coverage for worker-mode app heartbeat metrics."""

    def test_get_agent_metrics_includes_graph_progress(self, monkeypatch):
        original_agent = worker_app._agent
        worker_app._agent = _build_agent_with_graph_progress(3)
        try:
            fake_psutil = _build_fake_psutil(mem_mb=256, cpu=9.2)
            with __import__("unittest.mock").mock.patch.dict(
                "sys.modules", {"psutil": fake_psutil}
            ):
                result = worker_app._get_agent_metrics()

            assert result is not None
            assert result["graph_progress"] == 3
            assert result["memory_mb"] == 256.0
            assert result["cpu_percent"] == 9.2
            assert result["process_count"] == 7
            assert result["listening_ports"] == 2
        finally:
            worker_app._agent = original_agent


class TestDualAppMetrics:
    """Coverage for dual-mode app heartbeat metrics."""

    def test_get_agent_metrics_includes_graph_progress(self, monkeypatch):
        original_agent = dual_app._agent
        dual_app._agent = _build_agent_with_graph_progress(5)
        try:
            fake_psutil = _build_fake_psutil(mem_mb=128, cpu=3.5)
            with __import__("unittest.mock").mock.patch.dict(
                "sys.modules", {"psutil": fake_psutil}
            ):
                result = dual_app._get_agent_metrics()

            assert result is not None
            assert result["graph_progress"] == 5
            assert result["memory_mb"] == 128.0
            assert result["cpu_percent"] == 3.5
        finally:
            dual_app._agent = original_agent


class TestMemoryHealthMetrics:
    """Contained memory-failure counters ride the heartbeat metrics dict."""

    @pytest.fixture(autouse=True)
    def _reset_health(self):
        memory_health.reset()
        yield
        memory_health.reset()

    @staticmethod
    def _metrics(module):
        with patch.dict("sys.modules", {"psutil": _build_fake_psutil()}):
            return module._get_agent_metrics()

    @pytest.mark.parametrize("module", [worker_app, dual_app], ids=["worker", "dual"])
    def test_memory_included_when_counters_nonzero(self, module):
        memory_health.increment("retrieval_deadlock")
        memory_health.increment("access_stats_deadlock")
        memory_health.increment("access_stats_deadlock")

        result = self._metrics(module)

        assert result["memory"] == {
            "retrieval_deadlock": 1,
            "access_stats_deadlock": 2,
        }

    @pytest.mark.parametrize("module", [worker_app, dual_app], ids=["worker", "dual"])
    def test_memory_omitted_when_counters_zero(self, module):
        result = self._metrics(module)
        assert result is not None
        assert "memory" not in result
