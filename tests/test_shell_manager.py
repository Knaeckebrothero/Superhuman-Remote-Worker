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


class TestShellTab:
    """Tests for ShellTab dataclass."""

    def test_to_metadata(self, manager):
        tab = manager._tabs["default"]
        meta = tab.to_metadata()
        assert meta["name"] == "default"
        assert meta["type"] == "shell"
        assert "created_at" in meta
        assert "last_activity" in meta
