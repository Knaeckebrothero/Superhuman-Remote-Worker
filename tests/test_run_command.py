"""Tests for the run_command tool and shell mode toggle.

Tests require tmux to be installed and accessible via PATH.
Tests are automatically skipped if tmux is not available.
"""

import shutil
import uuid
from unittest.mock import MagicMock

import pytest

# Skip entire module if tmux is not available
pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)

from src.tools.shell.shell_manager import ShellManager  # noqa: E402
from src.tools.shell.shell_tools import create_shell_tools  # noqa: E402


@pytest.fixture
def manager():
    """Create a ShellManager for testing and clean up after."""
    job_id = str(uuid.uuid4())
    sm = ShellManager(
        job_id=job_id,
        max_tabs=4,
        scrollback_limit=1000,
        default_timeout=10,
    )
    yield sm
    sm.cleanup()


def _make_context(manager, mode="stateless"):
    """Create a mock ToolContext with the given shell mode."""
    context = MagicMock()
    context.shell_manager = manager

    config = {
        "shell": {"mode": mode},
        "max_output_chars": 50000,
        "shell_max_read_lines": 200,
    }
    context.get_config = lambda key, default=None: config.get(key, default)
    return context


class TestModeToggle:
    """Tests for shell mode-based tool selection."""

    def test_stateless_mode_returns_run_command(self, manager):
        context = _make_context(manager, mode="stateless")
        tools = create_shell_tools(context)
        tool_names = {t.name for t in tools}
        assert "run_command" in tool_names
        assert "shell_read" in tool_names
        assert "shell_execute" not in tool_names

    def test_persistent_mode_returns_shell_execute(self, manager):
        context = _make_context(manager, mode="persistent")
        tools = create_shell_tools(context)
        tool_names = {t.name for t in tools}
        assert "shell_execute" in tool_names
        assert "shell_read" in tool_names
        assert "run_command" not in tool_names

    def test_default_mode_is_stateless(self, manager):
        """When no mode is configured, default to stateless."""
        context = MagicMock()
        context.shell_manager = manager
        context.get_config = lambda key, default=None: {
            "max_output_chars": 50000,
            "shell_max_read_lines": 200,
        }.get(key, default)

        tools = create_shell_tools(context)
        tool_names = {t.name for t in tools}
        assert "run_command" in tool_names
        assert "shell_execute" not in tool_names


class TestRunCommand:
    """Tests for the run_command tool."""

    def _get_run_command(self, manager):
        context = _make_context(manager, mode="stateless")
        tools = create_shell_tools(context)
        return next(t for t in tools if t.name == "run_command")

    def test_basic_execution(self, manager):
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "echo hello-world"})
        assert "Exit code: 0" in result
        assert "hello-world" in result

    def test_exit_code_captured(self, manager):
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "false"})
        assert "Exit code: 1" in result

    def test_no_tab_header_in_output(self, manager):
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "echo test"})
        assert "[Shells:" not in result

    def test_tail_truncation(self, manager):
        tool = self._get_run_command(manager)
        # Generate 50 lines, request tail=10
        result = tool.invoke(
            {
                "command": "for i in $(seq 1 50); do echo line_$i; done",
                "tail": 10,
            }
        )
        assert "line_50" in result  # Last line present
        assert "line_1\n" not in result  # Early lines truncated
        assert "truncated" in result

    def test_default_tail_is_30(self, manager):
        tool = self._get_run_command(manager)
        # Generate 60 lines with default tail
        result = tool.invoke(
            {
                "command": "for i in $(seq 1 60); do echo line_$i; done",
            }
        )
        assert "line_60" in result
        assert "30 lines truncated" in result

    def test_blocked_command_rejected(self, manager):
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "reboot"})
        assert "blocked" in result.lower() or "Error" in result

    def test_timeout_parameter(self, manager):
        tool = self._get_run_command(manager)
        result = tool.invoke(
            {
                "command": "sleep 30",
                "timeout": 2,
            }
        )
        assert "timed out" in result.lower() or "timeout" in result.lower()

    def test_timeout_capped_at_600(self, manager):
        """Timeout values above 600 should be capped."""
        tool = self._get_run_command(manager)
        # Just verify it doesn't error — the cap is internal
        result = tool.invoke(
            {
                "command": "echo ok",
                "timeout": 9999,
            }
        )
        assert "Exit code: 0" in result

    def test_multiline_output(self, manager):
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "echo line1; echo line2; echo line3"})
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result

    def test_error_pattern_warning(self, manager):
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "echo 'Traceback (most recent call last):'"})
        assert "Possible error" in result or "Python traceback" in result

    def test_no_output_command(self, manager):
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "true"})
        assert "Exit code: 0" in result


class TestToolNameAliasing:
    """Tests for shell mode aliasing in get_all_tool_names."""

    def test_stateless_aliases_shell_execute_to_run_command(self):
        from src.core.loader import get_all_tool_names

        config = MagicMock()
        config.tools.workspace = []
        config.tools.core = []
        config.tools.document = []
        config.tools.research = []
        config.tools.citation = []
        config.tools.graph = []
        config.tools.sql = []
        config.tools.mongodb = []
        config.tools.git = []
        config.tools.shell = ["shell_execute", "shell_read"]
        config.tools.evaluation = []
        config.tools.knowledge = []
        config.tools.communication = []
        config.tools.cloud = []
        config.tools.delegation = []
        config.tools.orchestrator = []
        config.extra = {"shell": {"mode": "stateless"}}

        names = get_all_tool_names(config)
        assert "run_command" in names
        assert "shell_execute" not in names
        assert "shell_read" in names

    def test_persistent_aliases_run_command_to_shell_execute(self):
        from src.core.loader import get_all_tool_names

        config = MagicMock()
        config.tools.workspace = []
        config.tools.core = []
        config.tools.document = []
        config.tools.research = []
        config.tools.citation = []
        config.tools.graph = []
        config.tools.sql = []
        config.tools.mongodb = []
        config.tools.git = []
        config.tools.shell = ["run_command", "shell_read"]
        config.tools.evaluation = []
        config.tools.knowledge = []
        config.tools.communication = []
        config.tools.cloud = []
        config.tools.delegation = []
        config.tools.orchestrator = []
        config.extra = {"shell": {"mode": "persistent"}}

        names = get_all_tool_names(config)
        assert "shell_execute" in names
        assert "run_command" not in names
        assert "shell_read" in names

    def test_default_mode_is_stateless(self):
        from src.core.loader import get_all_tool_names

        config = MagicMock()
        config.tools.workspace = []
        config.tools.core = []
        config.tools.document = []
        config.tools.research = []
        config.tools.citation = []
        config.tools.graph = []
        config.tools.sql = []
        config.tools.mongodb = []
        config.tools.git = []
        config.tools.shell = ["shell_execute"]
        config.tools.evaluation = []
        config.tools.knowledge = []
        config.tools.communication = []
        config.tools.cloud = []
        config.tools.delegation = []
        config.tools.orchestrator = []
        config.extra = {}  # No shell config at all

        names = get_all_tool_names(config)
        assert "run_command" in names
        assert "shell_execute" not in names
