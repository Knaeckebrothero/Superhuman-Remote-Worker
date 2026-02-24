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
        idle_timeout=60,
        auto_start_claude_code=False,
    )
    yield sm
    sm.cleanup()


class TestShellManagerInit:
    """Tests for ShellManager initialization."""

    def test_default_shell_tab_created(self, manager):
        tabs = manager.list_tabs()
        assert len(tabs) == 1
        assert tabs[0]["name"] == "shell"
        assert tabs[0]["type"] == "shell"
        assert tabs[0]["closeable"] is False

    def test_session_is_alive(self, manager):
        assert manager.is_alive() is True

    def test_cleanup_kills_session(self):
        job_id = str(uuid.uuid4())
        sm = ShellManager(job_id=job_id, auto_start_claude_code=False)
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

    def test_cannot_close_default_shell(self, manager):
        with pytest.raises(ValueError, match="Cannot close default tab"):
            manager.close_tab("shell")

    def test_close_nonexistent_tab(self, manager):
        with pytest.raises(KeyError, match="not found"):
            manager.close_tab("no-such-tab")

    def test_max_tabs_enforced(self, manager):
        # manager has max_tabs=4, already has 1 (shell)
        manager.open_tab("tab-1")
        manager.open_tab("tab-2")
        manager.open_tab("tab-3")
        with pytest.raises(ValueError, match="Maximum tabs"):
            manager.open_tab("tab-4")

    def test_duplicate_tab_rejected(self, manager):
        manager.open_tab("my-tab")
        with pytest.raises(ValueError, match="already exists"):
            manager.open_tab("my-tab")


class TestTabNaming:
    """Tests for tab name validation."""

    def test_valid_names(self):
        assert TAB_NAME_PATTERN.match("shell")
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
        # Don't actually connect, just test type detection
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

    def test_named_tab_default_shell(self, manager):
        """Explicitly passing tab_name='shell' works the same as default."""
        output = manager.run_sync("echo named-tab-test", tab_name="shell")
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
        manager.send("shell", "echo test-send-read")
        time.sleep(0.5)
        text, metadata = manager.read("shell", lines=10)
        assert "test-send-read" in text

    def test_read_since_cursor(self, manager):
        # First read to set cursor
        manager.read("shell", lines=50, since_cursor=False)

        # Send new command
        manager.send("shell", "echo cursor-test-1234")
        time.sleep(0.5)

        # Read since cursor should only show new output
        text, metadata = manager.read("shell", since_cursor=True)
        assert metadata["mode"] == "since_cursor"
        assert "cursor-test-1234" in text

    def test_send_then_read_since_cursor_pattern(self, manager):
        """Test the pattern shell_send uses: snapshot cursor, send, read since_cursor."""
        # Snapshot cursor (same as shell_send does internally)
        manager.read("shell", lines=1, since_cursor=False)

        # Send command
        manager.send("shell", "echo send-read-pattern-test")
        time.sleep(0.5)

        # Read since cursor should capture the new output
        text, metadata = manager.read("shell", since_cursor=True)
        assert "send-read-pattern-test" in text
        assert metadata["lines_returned"] > 0

    def test_send_to_nonexistent_tab(self, manager):
        with pytest.raises(KeyError, match="not found"):
            manager.send("no-such-tab", "hello")

    def test_read_nonexistent_tab(self, manager):
        with pytest.raises(KeyError, match="not found"):
            manager.read("no-such-tab")


class TestInjectionStatus:
    """Tests for get_injection_status() with content previews."""

    def test_basic_format(self, manager):
        manager.run_sync("echo injection-test")
        status = manager.get_injection_status()
        assert "[shell] (shell)" in status

    def test_shows_output_content(self, manager):
        """Injection includes terminal output as content preview."""
        manager.run_sync("echo visible-in-injection")
        # Clear recently_shown from run_sync so injection shows content
        manager._recently_shown_tabs.clear()
        status = manager.get_injection_status()
        assert "visible-in-injection" in status

    def test_new_output_flag(self, manager):
        # First injection sets the cursor
        manager.get_injection_status()

        # Send via pane directly to avoid recently_shown tracking
        manager._tabs["shell"].pane.send_keys("echo new-output-flag-test", enter=True)
        time.sleep(0.3)

        # Second injection should show [NEW OUTPUT]
        status = manager.get_injection_status()
        assert "[NEW OUTPUT]" in status

    def test_new_output_flag_clears(self, manager):
        """[NEW OUTPUT] flag clears on next injection if no new output."""
        manager.get_injection_status()
        manager._tabs["shell"].pane.send_keys("echo flag-clear-test", enter=True)
        time.sleep(0.3)

        # First check — flag present
        status1 = manager.get_injection_status()
        assert "[NEW OUTPUT]" in status1

        # Second check — no new output, flag absent
        status2 = manager.get_injection_status()
        assert "[NEW OUTPUT]" not in status2

    def test_multiple_tabs(self, manager):
        manager.open_tab("extra")
        manager._tabs["extra"].pane.send_keys("echo extra-output", enter=True)
        time.sleep(0.3)
        manager._recently_shown_tabs.clear()

        status = manager.get_injection_status()
        assert "[shell] (shell)" in status
        assert "[extra] (shell)" in status

    def test_recently_shown_tab_gets_short_note(self, manager):
        """Tabs whose output was returned via a tool call get a note, not content."""
        manager.run_sync("echo tool-output-already-shown")
        # run_sync marks "shell" as recently shown
        status = manager.get_injection_status()
        assert "output in tool result above" in status
        assert "tool-output-already-shown" not in status

    def test_recently_shown_clears_after_injection(self, manager):
        """recently_shown set is consumed by get_injection_status."""
        manager.run_sync("echo first-run")
        assert "shell" in manager._recently_shown_tabs

        manager.get_injection_status()
        assert len(manager._recently_shown_tabs) == 0

    def test_unread_tab_shows_content(self, manager):
        """Tabs not recently read via tools show full content preview."""
        manager.open_tab("extra")
        manager._tabs["extra"].pane.send_keys("echo preview-content-here", enter=True)
        time.sleep(0.3)
        # extra was not read via a tool — should show content
        manager._recently_shown_tabs.discard("extra")

        status = manager.get_injection_status()
        assert "preview-content-here" in status


class TestFilterTuiOutput:
    """Tests for _filter_tui_output (pure function, no tmux needed)."""

    def test_strips_box_drawing(self):
        text = "Hello\n────────────────────────────\nWorld"
        result = ShellManager._filter_tui_output(text)
        assert "────" not in result
        assert "Hello" in result
        assert "World" in result

    def test_strips_status_bar(self):
        text = "Some output\n  ⏵⏵ bypass permissions on (shift+tab to cycle)\nMore output"
        result = ShellManager._filter_tui_output(text)
        assert "⏵" not in result
        assert "Some output" in result
        assert "More output" in result

    def test_strips_suggestion_prompt(self):
        text = 'Output\n❯ Try "fix lint errors"\nMore'
        result = ShellManager._filter_tui_output(text)
        assert "Try" not in result
        assert "Output" in result
        assert "More" in result

    def test_collapses_blank_lines(self):
        text = "Line 1\n\n\n\n\nLine 2"
        result = ShellManager._filter_tui_output(text)
        assert result == "Line 1\n\nLine 2"

    def test_strips_ansi(self):
        text = "\x1b[32mGreen text\x1b[0m"
        result = ShellManager._filter_tui_output(text)
        assert result == "Green text"

    def test_preserves_content(self):
        text = "def hello():\n    print('world')\n\nError: file not found"
        assert ShellManager._filter_tui_output(text) == text

    def test_strips_trailing_blank_lines(self):
        text = "Output here\n\n\n"
        result = ShellManager._filter_tui_output(text)
        assert result == "Output here"

    def test_empty_input(self):
        assert ShellManager._filter_tui_output("") == ""

    def test_combined_noise(self):
        text = (
            "\x1b[32mReal output\x1b[0m\n"
            "────────────────────────────────────\n"
            "\n"
            "\n"
            "\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
            '❯ Try "fix lint errors"\n'
            "More real output"
        )
        result = ShellManager._filter_tui_output(text)
        assert "Real output" in result
        assert "More real output" in result
        assert "────" not in result
        assert "⏵" not in result
        assert "Try" not in result
        # Collapsed blank lines: only one blank between content
        lines = result.split("\n")
        consecutive_blanks = 0
        for line in lines:
            if not line.strip():
                consecutive_blanks += 1
                assert consecutive_blanks <= 1
            else:
                consecutive_blanks = 0


class TestShellTab:
    """Tests for ShellTab dataclass."""

    def test_to_metadata(self, manager):
        tab = manager._tabs["shell"]
        meta = tab.to_metadata()
        assert meta["name"] == "shell"
        assert meta["type"] == "shell"
        assert meta["closeable"] is False
        assert "created_at" in meta
        assert "last_activity" in meta
