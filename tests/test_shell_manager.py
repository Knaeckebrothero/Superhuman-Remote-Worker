"""Tests for ShellManager (persistent tmux-backed terminal sessions).

These tests require tmux to be installed and accessible via PATH.
Tests are automatically skipped if tmux is not available.
"""

import shutil
import time
import uuid

import pytest

# Skip entire module if tmux is not available
pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux not installed"
)

from src.tools.coding.shell_manager import ShellManager, ShellTab, TAB_NAME_PATTERN


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


class TestShellManagerInit:
    """Tests for ShellManager initialization."""

    def test_default_shell_tab_created(self, manager):
        tabs = manager.list_tabs()
        assert len(tabs) == 1
        assert tabs[0]["name"] == "default"
        assert tabs[0]["type"] == "shell"

    def test_session_is_alive(self, manager):
        assert manager.is_alive() is True

    def test_cleanup_kills_session(self):
        job_id = str(uuid.uuid4())
        sm = ShellManager(job_id=job_id)
        assert sm.is_alive() is True
        sm.cleanup()
        assert sm.is_alive() is False


class TestTabLifecycle:
    """Tests for opening, listing, and closing tabs."""

    def test_open_tab(self, manager):
        result = manager.open_tab("test-tab")
        assert result["name"] == "test-tab"
        assert result["type"] == "shell"
        assert len(manager.list_tabs()) == 2

    def test_open_tab_with_command(self, manager):
        result = manager.open_tab("my-proc", command="echo running")
        assert result["name"] == "my-proc"
        assert result["type"] == "process"

    def test_close_tab(self, manager):
        manager.open_tab("temp")
        assert len(manager.list_tabs()) == 2
        manager.close_tab("temp")
        assert len(manager.list_tabs()) == 1

    def test_close_nonexistent_tab(self, manager):
        with pytest.raises(KeyError, match="not found"):
            manager.close_tab("no-such-tab")

    def test_duplicate_tab_rejected(self, manager):
        manager.open_tab("my-tab")
        with pytest.raises(ValueError, match="already exists"):
            manager.open_tab("my-tab")


class TestMaxTabs:
    """Tests for max_tabs limit enforcement."""

    def test_max_tabs_enforced(self, manager):
        """Opening tabs beyond the limit raises ValueError."""
        # manager fixture has max_tabs=4, default tab already uses 1 slot
        manager.open_tab("tab-1")
        manager.open_tab("tab-2")
        manager.open_tab("tab-3")
        assert len(manager.list_tabs()) == 4

        with pytest.raises(ValueError, match="Maximum tabs") as exc_info:
            manager.open_tab("tab-4")
        assert "Close unused tabs first" in str(exc_info.value)
        assert "default" in str(exc_info.value)  # lists open tabs

    def test_can_open_after_closing(self, manager):
        """After closing a tab, a new one can be opened within the limit."""
        manager.open_tab("tab-1")
        manager.open_tab("tab-2")
        manager.open_tab("tab-3")
        assert len(manager.list_tabs()) == 4

        manager.close_tab("tab-2")
        assert len(manager.list_tabs()) == 3

        # Should succeed now
        result = manager.open_tab("tab-new")
        assert result["name"] == "tab-new"
        assert len(manager.list_tabs()) == 4

    def test_ensure_tab_respects_limit(self, manager):
        """ensure_tab should also fail when the limit is reached."""
        manager.open_tab("tab-1")
        manager.open_tab("tab-2")
        manager.open_tab("tab-3")

        with pytest.raises(ValueError, match="Maximum tabs"):
            manager.ensure_tab("tab-4")


class TestEnsureTab:
    """Tests for ensure_tab (auto-create on first use)."""

    def test_returns_existing_tab(self, manager):
        tab = manager.ensure_tab("default")
        assert tab.name == "default"
        assert len(manager.list_tabs()) == 1  # No new tab created

    def test_creates_new_tab(self, manager):
        tab = manager.ensure_tab("new-tab")
        assert tab.name == "new-tab"
        assert tab.tab_type == "shell"
        assert len(manager.list_tabs()) == 2

    def test_subsequent_calls_return_same_tab(self, manager):
        tab1 = manager.ensure_tab("my-tab")
        tab2 = manager.ensure_tab("my-tab")
        assert tab1 is tab2
        assert len(manager.list_tabs()) == 2


class TestTabNaming:
    """Tests for tab name validation."""

    def test_valid_names(self):
        assert TAB_NAME_PATTERN.match("default")
        assert TAB_NAME_PATTERN.match("gpu-box")
        assert TAB_NAME_PATTERN.match("test-123")
        assert TAB_NAME_PATTERN.match("a")

    def test_invalid_names(self):
        assert not TAB_NAME_PATTERN.match("")
        assert not TAB_NAME_PATTERN.match("UPPERCASE")
        assert not TAB_NAME_PATTERN.match("has spaces")
        assert not TAB_NAME_PATTERN.match("has_underscore")
        assert not TAB_NAME_PATTERN.match("a" * 21)  # Too long
        assert not TAB_NAME_PATTERN.match("special!chars")

    def test_open_tab_invalid_name(self, manager):
        with pytest.raises(ValueError, match="Invalid tab name"):
            manager.open_tab("BAD_NAME!")


class TestTypeAutoDetection:
    """Tests for auto-detecting tab type from command."""

    def test_ssh_detected(self, manager):
        result = manager.open_tab("test-ssh", command="ssh user@host", tab_type=None)
        assert result["type"] == "ssh"

    def test_python_detected(self, manager):
        result = manager.open_tab("py", command="python3 -i", tab_type=None)
        assert result["type"] == "repl"

    def test_explicit_type_overrides(self, manager):
        result = manager.open_tab("custom", command="python3", tab_type="process")
        assert result["type"] == "process"

    def test_unknown_command_is_process(self, manager):
        result = manager.open_tab("proc", command="my-custom-tool --flag")
        assert result["type"] == "process"


class TestRunSync:
    """Tests for synchronous command execution."""

    def test_basic_command(self, manager):
        output = manager.run_sync("echo hello-world")
        assert "Exit code: 0" in output
        assert "hello-world" in output

    def test_exit_code_captured(self, manager):
        output = manager.run_sync("false")  # exits with code 1
        assert "Exit code: 1" in output

    def test_multiline_output(self, manager):
        output = manager.run_sync("echo line1; echo line2; echo line3")
        assert "Exit code: 0" in output
        assert "line1" in output
        assert "line2" in output
        assert "line3" in output

    def test_timeout_handling(self, manager):
        output = manager.run_sync("sleep 30", timeout=1)
        assert "timed out" in output.lower()
        assert "--- terminal state ---" in output

    def test_blocked_command(self, manager):
        output = manager.run_sync("sudo ls")
        assert "blocked" in output.lower()

    def test_no_output_command(self, manager):
        output = manager.run_sync("true")
        assert "Exit code: 0" in output

    def test_named_tab_default(self, manager):
        """Explicitly passing tab_name='default' works the same as default."""
        output = manager.run_sync("echo named-tab-test", tab_name="default")
        assert "Exit code: 0" in output
        assert "named-tab-test" in output

    def test_named_tab_custom_shell(self, manager):
        """Can run sync commands on a custom shell-type tab."""
        manager.open_tab("build", tab_type="shell")
        output = manager.run_sync("echo build-output", tab_name="build")
        assert "Exit code: 0" in output
        assert "build-output" in output

    def test_rejects_non_shell_tab(self, manager):
        """run_sync raises ValueError on non-shell tabs (repl, ssh, etc.)."""
        manager.open_tab("my-repl", command="echo running", tab_type="repl")
        with pytest.raises(ValueError, match="Synchronous execution only works on shell-type tabs"):
            manager.run_sync("echo test", tab_name="my-repl")

    def test_rejects_nonexistent_tab(self, manager):
        """run_sync raises KeyError for tabs that don't exist."""
        with pytest.raises(KeyError, match="not found"):
            manager.run_sync("echo test", tab_name="no-such-tab")


class TestSendAndRead:
    """Tests for send/read workflow."""

    def test_send_and_read(self, manager):
        manager.send("default", "echo test-send-read")
        time.sleep(0.5)
        text, metadata = manager.read("default", lines=10)
        assert "test-send-read" in text

    def test_read_since_cursor(self, manager):
        # First read to set cursor
        manager.read("default", lines=50, since_cursor=False)

        # Send new command
        manager.send("default", "echo cursor-test-1234")
        time.sleep(0.5)

        # Read since cursor should only show new output
        text, metadata = manager.read("default", since_cursor=True)
        assert metadata["mode"] == "since_cursor"
        assert "cursor-test-1234" in text

    def test_send_then_read_since_cursor_pattern(self, manager):
        """Test the async pattern: snapshot cursor, send, read since_cursor."""
        manager.read("default", lines=1, since_cursor=False)
        manager.send("default", "echo send-read-pattern-test")
        time.sleep(0.5)
        text, metadata = manager.read("default", since_cursor=True)
        assert "send-read-pattern-test" in text
        assert metadata["lines_returned"] > 0

    def test_send_to_nonexistent_tab(self, manager):
        with pytest.raises(KeyError, match="not found"):
            manager.send("no-such-tab", "hello")

    def test_read_nonexistent_tab(self, manager):
        with pytest.raises(KeyError, match="not found"):
            manager.read("no-such-tab")


class TestReadWithOffset:
    """Tests for read_with_offset (file-like scrollback reading)."""

    def test_tail_mode_default(self, manager):
        manager.run_sync("echo offset-test")
        text, metadata = manager.read_with_offset("default", lines=10)
        assert metadata["mode"] == "tail"
        assert "offset-test" in text

    def test_offset_mode(self, manager):
        manager.run_sync("echo offset-line-test")
        text, metadata = manager.read_with_offset("default", lines=5, offset=0)
        assert metadata["mode"] == "offset"
        assert metadata["offset"] == 0
        assert metadata["lines_returned"] <= 5

    def test_offset_beyond_buffer(self, manager):
        text, metadata = manager.read_with_offset("default", lines=10, offset=99999)
        assert metadata["lines_returned"] == 0
        assert text == "(empty)"

    def test_returns_total_lines(self, manager):
        manager.run_sync("echo line1; echo line2; echo line3")
        _, metadata = manager.read_with_offset("default", lines=50)
        assert metadata["total_lines"] > 0


class TestFormatTabHeader:
    """Tests for format_tab_header."""

    def test_single_tab(self, manager):
        header = manager.format_tab_header()
        assert header == "[Shells: default]"

    def test_multiple_tabs(self, manager):
        manager.open_tab("extra")
        header = manager.format_tab_header()
        assert "default" in header
        assert "extra" in header
        assert header.startswith("[Shells: ")
        assert header.endswith("]")


class TestPruneDeadTabs:
    """Tests for dead tab pruning."""

    def test_dead_tab_pruned(self, manager):
        manager.open_tab("will-die")
        assert len(manager.list_tabs()) == 2

        # Kill the window directly via tmux
        manager._tabs["will-die"].window.kill()

        # Pruning should remove it
        manager._prune_dead_tabs()
        assert len(manager._tabs) == 1
        assert "will-die" not in manager._tabs

    def test_alive_tabs_preserved(self, manager):
        manager.open_tab("alive")
        manager._prune_dead_tabs()
        assert "alive" in manager._tabs


class TestInteractiveDetection:
    """Tests for interactive prompt detection and early return."""

    def test_confirmation_prompt_detected(self, manager):
        """Commands that produce a [y/N] prompt should return early."""
        output = manager.run_sync(
            'echo "Continue? [y/N]"; read answer',
            timeout=15,
        )
        assert "--- terminal state ---" in output
        assert "[y/N]" in output
        assert "interactive prompt" in output.lower() or "waiting for input" in output.lower()

    def test_password_prompt_detected(self, manager):
        """Commands that produce a password prompt should return early."""
        output = manager.run_sync(
            'echo "Password:"; read -s pass',
            timeout=15,
        )
        assert "--- terminal state ---" in output
        assert "Password:" in output

    def test_stall_detection(self, manager):
        """Commands that stall (no output change) should return early."""
        # 'read' with no prompt just waits silently
        start = time.monotonic()
        output = manager.run_sync("read answer", timeout=15)
        elapsed = time.monotonic() - start
        assert "--- terminal state ---" in output
        assert "waiting for input" in output.lower() or "no output change" in output.lower()
        # Should detect stall within ~7s (1s grace + 5s threshold + margin)
        assert elapsed < 10

    def test_normal_command_unaffected(self, manager):
        """Normal commands should still work via sentinel detection."""
        output = manager.run_sync("echo hello-world")
        assert "Exit code: 0" in output
        assert "hello-world" in output
        assert "--- terminal state ---" not in output

    def test_slow_command_not_false_positive(self, manager):
        """Commands that produce output slowly should not trigger stall detection."""
        output = manager.run_sync(
            'for i in 1 2 3; do echo "step $i"; sleep 1; done',
            timeout=10,
        )
        assert "Exit code: 0" in output
        assert "step 3" in output
        assert "--- terminal state ---" not in output

    def test_timeout_includes_visible_content(self, manager):
        """Timeout should include what's visible in the terminal."""
        output = manager.run_sync(
            'echo "visible-marker-12345"; sleep 30',
            timeout=2,
        )
        assert "timed out" in output.lower()
        assert "--- terminal state ---" in output
        assert "visible-marker-12345" in output

    def test_blocked_tab_rejects_new_commands(self, manager):
        """Commands sent to a tab stuck on a prompt should be rejected without executing."""
        # First: create an interactive prompt on the tab
        output1 = manager.run_sync(
            'echo "Continue? [y/N]"; read answer',
            timeout=15,
        )
        assert "--- terminal state ---" in output1

        # Second: try to send a new command to the same (stuck) tab
        output2 = manager.run_sync("echo should-not-run")
        assert "blocked" in output2.lower()
        assert "was not executed" in output2.lower()
        assert "--- terminal state ---" in output2
        # The original prompt should still be visible
        assert "[y/N]" in output2

    def test_blocked_tab_recovers_after_cancel(self, manager):
        """After C-c on a stuck tab, new commands should work again."""
        # Create an interactive prompt
        manager.run_sync(
            'echo "Continue? [y/N]"; read answer',
            timeout=15,
        )

        # Cancel the prompt
        manager.send("default", "C-c", enter=False)
        time.sleep(0.5)

        # Now a normal command should work
        output = manager.run_sync("echo recovered-ok")
        assert "Exit code:" in output
        assert "recovered-ok" in output


class TestApplyTailTerminalState:
    """Tests for _apply_tail handling terminal state format."""

    def test_terminal_state_preserved_when_short(self):
        from src.tools.coding.shell_tools import _apply_tail
        output = (
            "Command timed out after 120s: ssh admin@host\n"
            "--- terminal state ---\n"
            "Are you sure you want to continue connecting (yes/no)?"
        )
        result = _apply_tail(output, tail=30)
        assert result == output  # Should not truncate

    def test_terminal_state_truncated_when_long(self):
        from src.tools.coding.shell_tools import _apply_tail
        body_lines = [f"line {i}" for i in range(50)]
        output = (
            "Command timed out after 120s: ssh admin@host\n"
            "--- terminal state ---\n"
            + "\n".join(body_lines)
        )
        result = _apply_tail(output, tail=10)
        assert "--- terminal state ---" in result
        assert "Command timed out" in result
        assert "40 lines truncated" in result
        assert "line 49" in result  # Last line preserved

    def test_stdout_format_still_works(self):
        from src.tools.coding.shell_tools import _apply_tail
        body_lines = [f"line {i}" for i in range(50)]
        output = (
            "Exit code: 0\n"
            "--- stdout ---\n"
            + "\n".join(body_lines)
        )
        result = _apply_tail(output, tail=10)
        assert "Exit code: 0" in result
        assert "--- stdout ---" in result
        assert "40 lines truncated" in result


class TestShellTab:
    """Tests for ShellTab dataclass."""

    def test_to_metadata(self, manager):
        tab = manager._tabs["default"]
        meta = tab.to_metadata()
        assert meta["name"] == "default"
        assert meta["type"] == "shell"
        assert "created_at" in meta
        assert "last_activity" in meta
