"""Tests for ShellManager (backend-delegating persistent shell sessions).

ShellManager has no local execution path: it requires a workspace backend
that declares ``supports_shell`` and forwards every operation to it (the
live implementation is RemoteBackend, covered by test_workspace_backends.py;
the pure sentinel/stall helpers are covered by test_shell_stall_logic.py).
These tests run against a mock backend — no tmux required.
"""

import re
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.tools.shell.shell_manager import (
    DEFAULT_BLOCKED_COMMANDS,
    SUDO_FREEZE_SENTINEL,
    ShellManager,
    ShellTab,
)


def make_backend(**overrides):
    """Mock workspace backend that declares shell support."""
    backend = MagicMock()
    backend.supports_shell = True
    backend.shell_run.return_value = "Exit code: 0\n--- stdout ---\nok"
    backend.shell_send.return_value = "Sent to 'default'"
    backend.shell_read.return_value = ("output", {"tab": "default", "mode": "tail"})
    backend.shell_read_with_offset.return_value = ("output", {"mode": "tail"})
    backend.shell_open_tab.return_value = {"name": "new-tab", "type": "shell"}
    backend.shell_close_tab.return_value = "Tab 'new-tab' closed"
    backend.shell_list_tabs.return_value = [{"name": "default", "type": "shell"}]
    backend.shell_format_tab_header.return_value = "[Shells: default]"
    backend.shell_is_alive.return_value = True
    for key, value in overrides.items():
        setattr(backend, key, value)
    return backend


@pytest.fixture
def backend():
    return make_backend()


@pytest.fixture
def manager(backend):
    return ShellManager(job_id=str(uuid.uuid4()), backend=backend)


class TestHardOff:
    """ShellManager must refuse to construct without a shell-capable backend.

    The local libtmux execution path was removed (the sibling of the
    local-browser fallback removal, docs/issues/remove_local_browser_fallback.md);
    a missing or shell-less backend must be a loud error, never a silent
    degradation to in-pod execution.
    """

    def test_no_backend_raises(self):
        with pytest.raises(RuntimeError, match="shell support"):
            ShellManager(job_id=str(uuid.uuid4()))

    def test_backend_without_shell_support_raises(self):
        backend = MagicMock()
        backend.supports_shell = False
        with pytest.raises(RuntimeError, match="shell support"):
            ShellManager(job_id=str(uuid.uuid4()), backend=backend)

    def test_error_says_no_local_execution(self):
        with pytest.raises(RuntimeError, match="workspace"):
            ShellManager(job_id=str(uuid.uuid4()), backend=None)


class TestNoLocalShellPath:
    """The agent runtime must not contain an in-pod shell execution path."""

    def test_libtmux_not_imported_under_src(self):
        src_root = Path(__file__).resolve().parents[1] / "src"
        pattern = re.compile(r"^\s*(?:from|import)\s+libtmux", re.MULTILINE)
        offenders = sorted(
            str(path.relative_to(src_root))
            for path in src_root.rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        )
        assert offenders == [], (
            f"libtmux must not be imported in the agent runtime: {offenders}"
        )


class TestDelegation:
    """Every operation forwards to the corresponding backend.shell_* method."""

    def test_ensure_tab_delegates_and_tracks_stub(self, manager, backend):
        tab = manager.ensure_tab("build")
        backend.shell_ensure_tab.assert_called_once_with("build")
        assert tab.name == "build"
        assert manager.ensure_tab("build") is tab  # stub reused
        assert backend.shell_ensure_tab.call_count == 2  # backend still consulted

    def test_open_tab_delegates_and_returns_metadata(self, manager, backend):
        backend.shell_open_tab.return_value = {"name": "py", "type": "repl"}
        metadata = manager.open_tab("py", command="python3", tab_type=None)
        backend.shell_open_tab.assert_called_once_with(
            "py", command="python3", tab_type=None
        )
        assert metadata["type"] == "repl"
        assert manager._tabs["py"].tab_type == "repl"

    def test_send_delegates(self, manager, backend):
        result = manager.send("default", "echo hi", enter=True)
        backend.shell_send.assert_called_once_with("default", "echo hi", enter=True)
        assert result == "Sent to 'default'"

    def test_send_without_enter_delegates(self, manager, backend):
        manager.send("default", "C-c", enter=False)
        backend.shell_send.assert_called_once_with("default", "C-c", enter=False)

    def test_read_delegates(self, manager, backend):
        text, metadata = manager.read("default", lines=10, since_cursor=True)
        backend.shell_read.assert_called_once_with(
            "default", lines=10, since_cursor=True
        )
        assert text == "output"
        assert metadata["tab"] == "default"

    def test_read_with_offset_delegates(self, manager, backend):
        manager.read_with_offset("default", lines=5, offset=100)
        backend.shell_read_with_offset.assert_called_once_with(
            "default", lines=5, offset=100
        )

    def test_close_tab_delegates_and_drops_stub(self, manager, backend):
        manager.ensure_tab("temp")
        result = manager.close_tab("temp")
        backend.shell_close_tab.assert_called_once_with("temp")
        assert "temp" not in manager._tabs
        assert "closed" in result

    def test_list_tabs_delegates(self, manager, backend):
        tabs = manager.list_tabs()
        backend.shell_list_tabs.assert_called_once()
        assert tabs[0]["name"] == "default"

    def test_format_tab_header_delegates(self, manager, backend):
        header = manager.format_tab_header()
        backend.shell_format_tab_header.assert_called_once()
        assert header == "[Shells: default]"

    def test_run_sync_delegates_with_args(self, manager, backend):
        result = manager.run_sync(
            "echo hi", timeout=42, working_dir="sub", tab_name="build"
        )
        backend.shell_run.assert_called_once_with(
            "echo hi", timeout=42, tab_name="build", working_dir="sub"
        )
        assert "Exit code: 0" in result

    def test_run_sync_passes_none_timeout(self, manager, backend):
        """None timeout passes through so the backend applies its soft timeout."""
        manager.run_sync("echo hi")
        assert backend.shell_run.call_args[1]["timeout"] is None

    def test_cleanup_delegates_and_clears_stubs(self, manager, backend):
        manager.ensure_tab("a")
        manager.cleanup()
        backend.shell_cleanup.assert_called_once()
        assert manager._tabs == {}

    def test_is_alive_delegates(self, manager, backend):
        assert manager.is_alive() is True
        backend.shell_is_alive.assert_called_once()


class TestBlockedCommandSend:
    """Blocked-command enforcement in send() fires before delegation."""

    def test_blocked_command_send(self, manager, backend):
        result = manager.send("default", "shutdown now", enter=True)
        assert "blocked" in result.lower()
        backend.shell_send.assert_not_called()

    def test_send_allows_non_blocked(self, manager):
        result = manager.send("default", "echo hello", enter=True)
        assert result == "Sent to 'default'"

    def test_send_freezes_sudo_by_default(self, manager, backend):
        result = manager.send("default", "sudo ls", enter=True)
        assert result == SUDO_FREEZE_SENTINEL
        backend.shell_send.assert_not_called()

    def test_send_allows_sudo_when_action_allow(self, backend):
        manager = ShellManager(
            job_id=str(uuid.uuid4()), backend=backend, sudo_action="allow"
        )
        result = manager.send("default", "sudo ls", enter=True)
        assert result == "Sent to 'default'"

    def test_send_blocks_sudo_when_action_block(self, backend):
        manager = ShellManager(
            job_id=str(uuid.uuid4()), backend=backend, sudo_action="block"
        )
        result = manager.send("default", "sudo ls", enter=True)
        assert "blocked" in result.lower()
        assert "vm runtime" in result.lower()
        backend.shell_send.assert_not_called()

    def test_send_skips_check_without_enter(self, manager, backend):
        """send() with enter=False skips blocking (control keys, etc.)."""
        result = manager.send("default", "shutdown", enter=False)
        assert result == "Sent to 'default'"
        backend.shell_send.assert_called_once()

    def test_all_default_blocked_commands_in_send(self, manager, backend):
        for cmd in DEFAULT_BLOCKED_COMMANDS:
            result = manager.send("default", f"{cmd} something", enter=True)
            assert "blocked" in result.lower(), f"{cmd} was not blocked by send()"
        backend.shell_send.assert_not_called()

    def test_custom_blocked_commands(self, backend):
        manager = ShellManager(
            job_id=str(uuid.uuid4()),
            backend=backend,
            blocked_commands=["rm"],
        )
        result = manager.send("default", "rm -rf /", enter=True)
        assert "blocked" in result.lower()
        # Default-blocked commands are replaced, not merged.
        assert manager.send("default", "reboot", enter=True) == "Sent to 'default'"


class TestRunSyncSudoFreeze:
    """Sudo interception in run_sync() fires before delegation.

    This validates the early-return contract: _check_blocked() must run
    before backend delegation in both run_sync() and send(), so the gate
    cannot be bypassed by the delegating path.
    """

    def test_run_sync_freezes_sudo_by_default(self, manager, backend):
        result = manager.run_sync("sudo apt-get install -y libxml2-dev")
        assert result == SUDO_FREEZE_SENTINEL
        backend.shell_run.assert_not_called()

    def test_run_sync_allows_sudo_when_action_allow(self, backend):
        manager = ShellManager(
            job_id=str(uuid.uuid4()), backend=backend, sudo_action="allow"
        )
        result = manager.run_sync("sudo true")
        assert "SUDO_FREEZE" not in result
        backend.shell_run.assert_called_once()

    def test_run_sync_blocks_sudo_when_action_block(self, backend):
        manager = ShellManager(
            job_id=str(uuid.uuid4()), backend=backend, sudo_action="block"
        )
        result = manager.run_sync("sudo ls")
        assert "blocked" in result.lower()
        backend.shell_run.assert_not_called()

    def test_run_sync_blocks_default_commands(self, manager, backend):
        result = manager.run_sync("shutdown now")
        assert "blocked" in result.lower()
        backend.shell_run.assert_not_called()

    def test_non_sudo_delegates_to_backend(self, manager, backend):
        backend.shell_run.return_value = "Exit code: 0\n--- stdout ---\nhello"
        result = manager.run_sync("echo hello")
        assert "hello" in result
        backend.shell_run.assert_called_once()


class TestApplyTailTerminalState:
    """Tests for _apply_tail handling terminal state format."""

    def test_terminal_state_preserved_when_short(self):
        from src.tools.shell.shell_tools import _apply_tail

        output = (
            "Command timed out after 120s: ssh admin@host\n"
            "--- terminal state ---\n"
            "Are you sure you want to continue connecting (yes/no)?"
        )
        result = _apply_tail(output, tail=30)
        assert result == output  # Should not truncate

    def test_terminal_state_truncated_when_long(self):
        from src.tools.shell.shell_tools import _apply_tail

        body_lines = [f"line {i}" for i in range(50)]
        output = (
            "Command timed out after 120s: ssh admin@host\n"
            "--- terminal state ---\n" + "\n".join(body_lines)
        )
        result = _apply_tail(output, tail=10)
        assert "--- terminal state ---" in result
        assert "Command timed out" in result
        assert "40 lines truncated" in result
        assert "line 49" in result  # Last line preserved

    def test_stdout_format_still_works(self):
        from src.tools.shell.shell_tools import _apply_tail

        body_lines = [f"line {i}" for i in range(50)]
        output = "Exit code: 0\n--- stdout ---\n" + "\n".join(body_lines)
        result = _apply_tail(output, tail=10)
        assert "Exit code: 0" in result
        assert "--- stdout ---" in result
        assert "40 lines truncated" in result


class TestShellTab:
    """Tests for the ShellTab stub dataclass."""

    def test_to_metadata(self):
        tab = ShellTab(name="default", tab_type="shell")
        meta = tab.to_metadata()
        assert meta["name"] == "default"
        assert meta["type"] == "shell"
        assert "created_at" in meta
        assert "last_activity" in meta
