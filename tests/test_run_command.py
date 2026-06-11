"""Tests for the run_command tool and shell mode toggle.

ShellManager delegates to the workspace backend, so these tests script
``backend.shell_run`` returns to exercise the tool layer — mode toggle,
tail truncation, still-running passthrough, interactive-prompt translation,
error-pattern warnings — without tmux. The live shell execution machinery
is RemoteBackend's, covered by test_workspace_backends.py.
"""

import uuid
from unittest.mock import MagicMock

import pytest

from src.tools.shell.shell_manager import STILL_RUNNING_TEMPLATE, ShellManager
from src.tools.shell.shell_tools import create_shell_tools


def make_backend():
    """Mock workspace backend with shell support and a benign default."""
    backend = MagicMock()
    backend.supports_shell = True
    backend.shell_run.return_value = "Exit code: 0\n--- stdout ---\nok"
    return backend


@pytest.fixture
def backend():
    return make_backend()


@pytest.fixture
def manager(backend):
    return ShellManager(job_id=str(uuid.uuid4()), backend=backend)


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
    """Tests for the run_command tool layer over a scripted backend."""

    def _get_run_command(self, manager):
        context = _make_context(manager, mode="stateless")
        tools = create_shell_tools(context)
        return next(t for t in tools if t.name == "run_command")

    def test_basic_execution(self, manager, backend):
        backend.shell_run.return_value = "Exit code: 0\n--- stdout ---\nhello-world"
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "echo hello-world"})
        assert "Exit code: 0" in result
        assert "hello-world" in result
        assert backend.shell_run.call_args[0][0] == "echo hello-world"

    def test_exit_code_captured(self, manager, backend):
        backend.shell_run.return_value = "Exit code: 1\n(no output)"
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "false"})
        assert "Exit code: 1" in result

    def test_no_tab_header_in_output(self, manager, backend):
        backend.shell_run.return_value = "Exit code: 0\n--- stdout ---\ntest"
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "echo test"})
        assert "[Shells:" not in result

    def test_tail_truncation(self, manager, backend):
        lines = "\n".join(f"line_{i}" for i in range(1, 51))
        backend.shell_run.return_value = f"Exit code: 0\n--- stdout ---\n{lines}"
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "seq", "tail": 10})
        assert "line_50" in result  # Last line present
        assert "line_1\n" not in result  # Early lines truncated
        assert "truncated" in result

    def test_default_tail_is_30(self, manager, backend):
        lines = "\n".join(f"line_{i}" for i in range(1, 61))
        backend.shell_run.return_value = f"Exit code: 0\n--- stdout ---\n{lines}"
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "seq"})
        assert "line_60" in result
        assert "30 lines truncated" in result

    def test_blocked_command_rejected(self, manager, backend):
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "reboot"})
        assert "blocked" in result.lower() or "Error" in result
        backend.shell_run.assert_not_called()

    def test_timeout_forwarded_to_backend(self, manager, backend):
        tool = self._get_run_command(manager)
        tool.invoke({"command": "sleep 5", "timeout": 42})
        assert backend.shell_run.call_args[1]["timeout"] == 42

    def test_omitted_timeout_forwarded_as_none(self, manager, backend):
        """None timeout passes through so the backend applies its soft
        no-change timeout (an explicit timeout disables it backend-side)."""
        tool = self._get_run_command(manager)
        tool.invoke({"command": "echo hi"})
        assert backend.shell_run.call_args[1]["timeout"] is None

    def test_still_running_passthrough_not_error(self, manager, backend):
        """A still-running result passes through honestly — not the old
        'requires interactive input' error (the original stall bug)."""
        backend.shell_run.return_value = STILL_RUNNING_TEMPLATE.format(
            tab="default", elapsed=30, quiet=30, terminal_state="(working)"
        )
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "sleep 999"})
        assert "still running" in result.lower()
        assert "Exit code: -1" in result
        assert "requires interactive input" not in result.lower()

    def test_interactive_prompt_becomes_error(self, manager, backend):
        """Stateless run_command can't answer prompts — the tool translates
        the backend's interactive-prompt report into a clear error."""
        backend.shell_run.return_value = (
            "Interactive prompt detected (password prompt). The command is "
            "waiting for input on tab 'default'.\n"
            "--- terminal state ---\nPassword:"
        )
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "ssh host"})
        assert "requires interactive input" in result
        assert "non-interactive" in result

    def test_error_pattern_warning(self, manager, backend):
        backend.shell_run.return_value = (
            "Exit code: 0\n--- stdout ---\nTraceback (most recent call last):"
        )
        tool = self._get_run_command(manager)
        result = tool.invoke({"command": "python broken.py"})
        assert "Possible error" in result or "Python traceback" in result

    def test_no_output_command(self, manager, backend):
        backend.shell_run.return_value = "Exit code: 0\n(no output)"
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
        config.tools.browser_direct = []
        config.tools.citation = []
        config.tools.graph = []
        config.tools.sql = []
        config.tools.mongodb = []
        config.tools.git = []
        config.tools.shell = ["shell_execute", "shell_read"]
        config.tools.evaluation = []
        config.tools.knowledge = []
        config.tools.communication = []
        config.tools.webdav = []
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
        config.tools.browser_direct = []
        config.tools.citation = []
        config.tools.graph = []
        config.tools.sql = []
        config.tools.mongodb = []
        config.tools.git = []
        config.tools.shell = ["run_command", "shell_read"]
        config.tools.evaluation = []
        config.tools.knowledge = []
        config.tools.communication = []
        config.tools.webdav = []
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
        config.tools.browser_direct = []
        config.tools.citation = []
        config.tools.graph = []
        config.tools.sql = []
        config.tools.mongodb = []
        config.tools.git = []
        config.tools.shell = ["shell_execute"]
        config.tools.evaluation = []
        config.tools.knowledge = []
        config.tools.communication = []
        config.tools.webdav = []
        config.tools.delegation = []
        config.tools.orchestrator = []
        config.extra = {}  # No shell config at all

        names = get_all_tool_names(config)
        assert "run_command" in names
