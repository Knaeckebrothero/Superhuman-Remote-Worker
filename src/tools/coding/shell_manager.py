"""Persistent shell session manager backed by tmux.

Provides a terminal multiplexer that gives the agent persistent, named tabs
for shell sessions, SSH connections, REPLs, and Claude Code interactions.

The key shift from run_command: commands and output are decoupled. Sending a
command is one action; reading the result is another. The default shell tab
accumulates history just like a human's terminal scrollback.

Requires: tmux installed and accessible via PATH.
Uses: libtmux for programmatic tmux control.
"""

import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import libtmux

logger = logging.getLogger(__name__)

# Suppress libtmux DEBUG logging (it logs every capture-pane call)
logging.getLogger("libtmux").setLevel(logging.WARNING)

# Tab name validation pattern
TAB_NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,20}$")

# Auto-detected tab types based on command prefix
COMMAND_TYPE_MAP = {
    "ssh": "ssh",
    "python": "repl",
    "python3": "repl",
    "ipython": "repl",
    "jupyter": "repl",
    "node": "repl",
    "psql": "repl",
    "mysql": "repl",
    "mongosh": "repl",
    "redis-cli": "repl",
    "claude": "claude-code",
}

# Default blocked commands (same as run_command for safety)
DEFAULT_BLOCKED_COMMANDS = frozenset([
    "sudo", "reboot", "shutdown", "poweroff", "halt", "init", "systemctl",
])


@dataclass
class ShellTab:
    """Tracks per-tab state for a persistent shell session."""

    name: str
    tab_type: str  # "shell", "ssh", "repl", "claude-code", "process"
    window: Any  # libtmux.Window
    pane: Any  # libtmux.Pane
    read_cursor: int = 0  # Line index for since_cursor reads
    last_injection_cursor: int = 0  # Cursor at last injection (for [NEW OUTPUT] detection)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closeable: bool = True  # False for default tabs (shell, claude-code)

    def to_metadata(self) -> Dict[str, Any]:
        """Return a metadata dict for list_tabs / injection status."""
        return {
            "name": self.name,
            "type": self.tab_type,
            "closeable": self.closeable,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
        }


class ShellManager:
    """Manages persistent tmux-backed shell sessions for the agent.

    Creates a detached tmux session with pre-opened tabs and provides
    methods for tab lifecycle, command execution, and output reading.
    """

    # Compiled patterns for TUI output filtering (claude-code tabs)
    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07")
    _BOX_DRAWING = set(chr(c) for c in range(0x2500, 0x2580))

    def __init__(
        self,
        job_id: str,
        max_tabs: int = 6,
        scrollback_limit: int = 5000,
        default_timeout: int = 120,
        idle_timeout: int = 1800,
        blocked_commands: Optional[List[str]] = None,
        auto_start_claude_code: bool = False,
        claude_code_model: str = "claude-opus-4-6",
        sandbox_cwd: Optional[str] = None,
    ):
        """Initialize ShellManager with a new tmux session.

        Args:
            job_id: Unique job identifier (used in session name)
            max_tabs: Maximum number of concurrent tabs
            scrollback_limit: Tmux history-limit per pane
            default_timeout: Default timeout for run_sync in seconds
            idle_timeout: Seconds before a tab is considered idle
            blocked_commands: Commands to block (None = use defaults)
            auto_start_claude_code: Whether to auto-open a claude-code tab
            claude_code_model: Model for Claude Code session
            sandbox_cwd: Working directory to restrict commands to (None = no restriction)
        """
        self.job_id = job_id
        self.max_tabs = max_tabs
        self.scrollback_limit = scrollback_limit
        self.default_timeout = default_timeout
        self.idle_timeout = idle_timeout
        self.sandbox_cwd = sandbox_cwd
        self._claude_code_model = claude_code_model

        if blocked_commands is None:
            self.blocked_commands = DEFAULT_BLOCKED_COMMANDS
        else:
            self.blocked_commands = frozenset(blocked_commands)

        # Thread lock for synchronous command execution on default shell
        self._sync_lock = threading.Lock()

        # Tabs whose output was returned via a tool call since the last injection.
        # get_injection_status() shows a short note for these instead of content.
        self._recently_shown_tabs: set[str] = set()

        # Tab storage (ordered for consistent iteration)
        self._tabs: OrderedDict[str, ShellTab] = OrderedDict()

        # Create tmux session
        session_name = f"agent_{job_id[:12]}"
        self._server = libtmux.Server()

        # Kill any stale session with the same name
        existing = self._server.sessions.filter(session_name=session_name)
        if existing:
            for s in existing:
                s.kill()

        self._session = self._server.new_session(
            session_name=session_name,
            window_name="shell",
            x=200,
            y=30,
            detach=True,
        )
        self._session_name = session_name

        # Set history limit
        self._session.set_option("history-limit", str(scrollback_limit))

        # Register default shell tab
        default_window = self._session.active_window
        default_pane = default_window.active_pane
        self._tabs["shell"] = ShellTab(
            name="shell",
            tab_type="shell",
            window=default_window,
            pane=default_pane,
            closeable=False,
        )

        # Set working directory if sandbox_cwd is set
        if self.sandbox_cwd:
            default_pane.send_keys(f"cd {self.sandbox_cwd}", enter=True)
            time.sleep(0.1)

        # Auto-start Claude Code tab if configured
        if auto_start_claude_code:
            try:
                # Strip ANTHROPIC_API_KEY so Claude Code uses OAuth/Max plan auth
                # instead of API key billing from the agent's .env.
                # Also strip CLAUDECODE to avoid nesting guard.
                cmd = (
                    "env -u ANTHROPIC_API_KEY -u CLAUDECODE "
                    f"claude --model {claude_code_model} --dangerously-skip-permissions"
                )
                self.open_tab(
                    name="claude-code",
                    command=cmd,
                    tab_type="claude-code",
                )
                self._tabs["claude-code"].closeable = False
                # Handle any interactive startup prompts
                self._handle_claude_code_startup()
            except Exception as e:
                logger.warning(f"Failed to auto-start Claude Code tab: {e}")

        logger.info(
            f"ShellManager initialized: session={session_name}, "
            f"tabs={list(self._tabs.keys())}"
        )

    def open_tab(
        self,
        name: str,
        command: Optional[str] = None,
        tab_type: Optional[str] = None,
        cols: int = 200,
        rows: int = 30,
    ) -> Dict[str, Any]:
        """Open a new named tab in the tmux session.

        Args:
            name: Tab name (lowercase alphanumeric + hyphens, max 20 chars)
            command: Optional command to run on tab creation
            tab_type: Tab type ("shell", "ssh", "repl", "claude-code", "process").
                      Auto-detected from command if not specified.
            cols: Terminal width (default 200)
            rows: Terminal height (default 30)

        Returns:
            Metadata dict for the new tab

        Raises:
            ValueError: If name is invalid, duplicate, or max tabs reached
        """
        # Validate name
        if not TAB_NAME_PATTERN.match(name):
            raise ValueError(
                f"Invalid tab name '{name}': must match {TAB_NAME_PATTERN.pattern}"
            )

        if name in self._tabs:
            raise ValueError(f"Tab '{name}' already exists")

        if len(self._tabs) >= self.max_tabs:
            raise ValueError(
                f"Maximum tabs ({self.max_tabs}) reached. Close a tab first."
            )

        # Auto-detect type from command
        if tab_type is None:
            tab_type = "shell"
            if command:
                first_word = command.strip().split()[0]
                # Strip path prefix (e.g. /usr/bin/python → python)
                base_cmd = first_word.rsplit("/", 1)[-1]
                tab_type = COMMAND_TYPE_MAP.get(base_cmd, "process")

        # Create tmux window
        window = self._session.new_window(
            window_name=name,
            attach=False,
        )
        pane = window.active_pane

        # Set working directory if sandbox_cwd is set and this is a shell-like tab
        if self.sandbox_cwd and tab_type in ("shell", "process"):
            pane.send_keys(f"cd {self.sandbox_cwd}", enter=True)
            time.sleep(0.1)

        # Run initial command if provided
        if command:
            pane.send_keys(command, enter=True)

        # Create tab entry
        tab = ShellTab(
            name=name,
            tab_type=tab_type,
            window=window,
            pane=pane,
        )
        self._tabs[name] = tab

        logger.info(f"Opened tab '{name}' (type={tab_type}, command={command!r})")
        return tab.to_metadata()

    def send(self, name: str, text: str, enter: bool = True) -> str:
        """Send keystrokes to a tab.

        Args:
            name: Tab name
            text: Text to send
            enter: Whether to press Enter after sending (default True)

        Returns:
            Confirmation message

        Raises:
            KeyError: If tab doesn't exist
        """
        tab = self._get_tab(name)
        tab.pane.send_keys(text, enter=enter)
        tab.last_activity = datetime.now(timezone.utc)
        return f"Sent to '{name}'"

    def read(
        self,
        name: str,
        lines: int = 50,
        since_cursor: bool = False,
    ) -> Tuple[str, Dict[str, Any]]:
        """Read output from a tab's terminal buffer.

        Args:
            name: Tab name
            lines: Number of lines to read from the end (default 50)
            since_cursor: If True, read only lines added since last read

        Returns:
            Tuple of (text, metadata_dict)

        Raises:
            KeyError: If tab doesn't exist
        """
        tab = self._get_tab(name)
        captured = tab.pane.capture_pane(start="-{}".format(self.scrollback_limit))

        # captured is a list of strings (one per line)
        if isinstance(captured, str):
            all_lines = captured.splitlines()
        else:
            all_lines = list(captured)

        # Strip trailing empty lines
        while all_lines and not all_lines[-1].strip():
            all_lines.pop()

        total_lines = len(all_lines)

        if since_cursor:
            # Return only lines after the read cursor
            new_lines = all_lines[tab.read_cursor:]
            tab.read_cursor = total_lines
            text = "\n".join(new_lines) if new_lines else "(no new output)"
            metadata = {
                "tab": name,
                "mode": "since_cursor",
                "lines_returned": len(new_lines),
                "total_lines": total_lines,
            }
        else:
            # Return last N lines
            start = max(0, total_lines - lines)
            selected = all_lines[start:]
            tab.read_cursor = total_lines  # Update cursor to current end
            text = "\n".join(selected) if selected else "(empty)"
            metadata = {
                "tab": name,
                "mode": "tail",
                "lines_requested": lines,
                "lines_returned": len(selected),
                "total_lines": total_lines,
            }

        # Filter TUI noise for claude-code tabs
        if tab.tab_type == "claude-code":
            text = self._filter_tui_output(text)

        # Mark tab as recently shown (so injection skips its content)
        self._recently_shown_tabs.add(name)

        return text, metadata

    def close_tab(self, name: str) -> str:
        """Close a tab by killing its tmux window.

        Args:
            name: Tab name

        Returns:
            Confirmation message

        Raises:
            KeyError: If tab doesn't exist
            ValueError: If tab is not closeable (default tabs)
        """
        tab = self._get_tab(name)
        if not tab.closeable:
            raise ValueError(
                f"Cannot close default tab '{name}'. Default tabs persist for the entire job."
            )

        tab.window.kill()
        del self._tabs[name]
        logger.info(f"Closed tab '{name}'")
        return f"Tab '{name}' closed"

    def list_tabs(self) -> List[Dict[str, Any]]:
        """Return metadata for all tabs.

        Returns:
            List of tab metadata dicts
        """
        return [tab.to_metadata() for tab in self._tabs.values()]

    def run_sync(
        self,
        command: str,
        timeout: Optional[int] = None,
        working_dir: Optional[str] = None,
        tab_name: str = "shell",
    ) -> str:
        """Execute a command synchronously in a shell-type tab.

        Uses a sentinel marker pattern to detect command completion and
        extract the exit code. Thread-safe via lock on default shell tab.

        Args:
            command: Shell command to execute
            timeout: Timeout in seconds (default: self.default_timeout)
            working_dir: Working directory for the command (relative to sandbox)
            tab_name: Name of the tab to run in (default "shell"). Must be
                      a shell-type tab (sentinel detection requires a bash-like shell).

        Returns:
            Formatted output string with exit code and stdout

        Raises:
            ValueError: If command is blocked or tab is not shell-type
            KeyError: If tab does not exist
            TimeoutError: If command exceeds timeout
        """
        # Safety check
        if self.blocked_commands:
            first_word = command.strip().split()[0] if command.strip() else ""
            if first_word in self.blocked_commands:
                return (
                    f"Command blocked: '{first_word}' is not allowed. "
                    f"Blocked commands: {', '.join(sorted(self.blocked_commands))}"
                )

        if timeout is None:
            timeout = self.default_timeout
        timeout = min(timeout, 600)  # Cap at 10 minutes

        sentinel = f"__DONE_{uuid.uuid4().hex[:12]}__"
        tab = self._get_tab(tab_name)

        # Validate tab type — sentinel approach only works in bash-like shells
        if tab.tab_type not in ("shell",):
            raise ValueError(
                f"Synchronous execution only works on shell-type tabs. "
                f"Tab '{tab_name}' is type '{tab.tab_type}'. "
                f"Use shell_send/shell_read for interactive tabs."
            )

        with self._sync_lock:
            # Change directory if needed
            if working_dir:
                if self.sandbox_cwd:
                    # Relative to sandbox
                    from pathlib import Path
                    full_dir = str(Path(self.sandbox_cwd) / working_dir)
                else:
                    full_dir = working_dir
                tab.pane.send_keys(f"cd {full_dir}", enter=True)
                time.sleep(0.1)

            # Record buffer position before command
            pre_lines = tab.pane.capture_pane(start="-{}".format(self.scrollback_limit))
            if isinstance(pre_lines, str):
                pre_count = len(pre_lines.splitlines())
            else:
                pre_count = len(list(pre_lines))

            # Send command with sentinel
            full_cmd = f'{command}; echo "{sentinel} $?"'
            tab.pane.send_keys(full_cmd, enter=True)

            # Poll for sentinel
            start_time = time.monotonic()
            output_text = ""
            exit_code = None

            while time.monotonic() - start_time < timeout:
                time.sleep(0.2)
                captured = tab.pane.capture_pane(start="-{}".format(self.scrollback_limit))
                if isinstance(captured, str):
                    all_lines = captured.splitlines()
                else:
                    all_lines = list(captured)

                # Find the sentinel OUTPUT line (not the echoed command).
                # The output line starts with the sentinel text: "__DONE_xxxx__ 0"
                # The echoed command has it embedded: `echo "__DONE_xxxx__ $?"`
                sentinel_line_idx = None
                for i in range(len(all_lines) - 1, -1, -1):
                    stripped = all_lines[i].strip()
                    if stripped.startswith(sentinel):
                        sentinel_line_idx = i
                        break

                if sentinel_line_idx is not None:
                    line = all_lines[sentinel_line_idx]
                    # Extract exit code from sentinel line: "__DONE_xxxx__ 0"
                    parts = line.strip().split()
                    try:
                        exit_code = int(parts[-1]) if parts else 1
                    except (ValueError, IndexError):
                        exit_code = 1

                    # Extract output: lines between echoed command and sentinel.
                    # New lines start after pre_count; exclude sentinel and echo lines
                    new_lines = all_lines[pre_count:sentinel_line_idx]
                    # Filter out lines containing the sentinel (echoed command lines)
                    output_lines = [
                        ol for ol in new_lines
                        if sentinel not in ol
                    ]
                    # Skip prompt/command echo lines at the start
                    while output_lines and (
                        command.split()[0] in output_lines[0]
                        or output_lines[0].strip().endswith("$")
                    ):
                        output_lines = output_lines[1:]

                    output_text = "\n".join(output_lines).strip()
                    break

            # Restore working directory if changed
            if working_dir and self.sandbox_cwd:
                tab.pane.send_keys(f"cd {self.sandbox_cwd}", enter=True)
                time.sleep(0.1)

            tab.last_activity = datetime.now(timezone.utc)
            self._recently_shown_tabs.add(tab_name)

            if exit_code is None:
                return f"Command timed out after {timeout}s: {command}"

            # Format output to match run_command style
            parts = [f"Exit code: {exit_code}"]
            if output_text:
                parts.append(f"--- stdout ---\n{output_text}")
            else:
                parts.append("(no output)")

            return "\n".join(parts)

    def get_injection_status(self, lines_per_tab: int = 30) -> str:
        """Generate tab status with content previews for context injection.

        Shows last N lines of output per tab. Tabs whose output was already
        returned via a tool call since the last injection get a short note
        instead of content (to avoid duplication).

        Args:
            lines_per_tab: Number of lines to show per tab (default 30)

        Returns:
            Formatted tab status string, or empty string if no tabs
        """
        if not self._tabs:
            return ""

        # Consume recently-shown set (tool results already delivered output)
        recently_shown = self._recently_shown_tabs.copy()
        self._recently_shown_tabs.clear()

        sections = []
        for name, tab in self._tabs.items():
            # Capture current buffer
            captured = tab.pane.capture_pane(start="-{}".format(self.scrollback_limit))
            if isinstance(captured, str):
                all_lines = captured.splitlines()
            else:
                all_lines = list(captured)

            # Strip trailing empty lines
            while all_lines and not all_lines[-1].strip():
                all_lines.pop()

            total_lines = len(all_lines)
            has_new = total_lines > tab.last_injection_cursor
            tab.last_injection_cursor = total_lines

            if name in recently_shown:
                # Output was already shown as a tool result
                sections.append(f"[{name}] ({tab.tab_type}) — output in tool result above")
            else:
                # Show last N lines
                start = max(0, total_lines - lines_per_tab)
                preview_lines = all_lines[start:]
                preview = "\n".join(preview_lines) if preview_lines else "(empty)"

                # Filter TUI noise for claude-code tabs
                if tab.tab_type == "claude-code":
                    preview = self._filter_tui_output(preview)

                new_flag = " [NEW OUTPUT]" if has_new else ""
                sections.append(f"[{name}] ({tab.tab_type}){new_flag}\n{preview}")

        return "\n\n".join(sections)

    def cleanup(self) -> None:
        """Kill the entire tmux session and clean up."""
        try:
            self._session.kill()
            logger.info(f"Cleaned up tmux session '{self._session_name}'")
        except Exception as e:
            logger.warning(f"Error cleaning up tmux session: {e}")
        self._tabs.clear()

    def is_alive(self) -> bool:
        """Check if the tmux session still exists."""
        try:
            sessions = self._server.sessions.filter(session_name=self._session_name)
            return len(sessions) > 0
        except Exception:
            return False

    @staticmethod
    def _filter_tui_output(text: str) -> str:
        """Strip TUI decorations from Claude Code terminal output.

        Removes box-drawing separator lines, status bar, suggestion
        prompts, ANSI escapes, and collapses consecutive blank lines.
        Only intended for claude-code tab type output.
        """
        filtered = []
        prev_blank = False

        for line in text.splitlines():
            # Strip ANSI escape sequences
            clean = ShellManager._ANSI_RE.sub("", line)

            # Drop box-drawing separator lines (>80% box-drawing chars)
            non_ws = clean.replace(" ", "")
            if non_ws and sum(1 for c in non_ws if c in ShellManager._BOX_DRAWING) / len(non_ws) > 0.8:
                continue

            # Drop status bar lines
            if "\u23f5" in clean:
                continue

            # Drop suggestion prompts
            stripped = clean.strip()
            if stripped.startswith("\u276f") and "Try " in stripped:
                continue

            # Collapse consecutive blank lines
            if not stripped:
                if prev_blank:
                    continue
                prev_blank = True
            else:
                prev_blank = False

            filtered.append(clean)

        # Strip trailing blank lines
        while filtered and not filtered[-1].strip():
            filtered.pop()

        return "\n".join(filtered)

    def _handle_claude_code_startup(self, timeout: int = 30) -> None:
        """Handle Claude Code interactive startup prompts automatically.

        Waits for Claude Code to start and auto-dismisses setup prompts:
        - Bypass Permissions warning → select "Yes, I accept" (Down + Enter)
        - API key prompt → select "No" / press Enter (default)
        - Trust folder prompt → accept

        With ANTHROPIC_API_KEY stripped from the environment, the API key
        prompt shouldn't appear — but this handles it as a fallback.

        Args:
            timeout: Max seconds to wait for Claude Code to become ready
        """
        tab = self._tabs.get("claude-code")
        if tab is None:
            return

        start = time.monotonic()
        handled = set()

        while time.monotonic() - start < timeout:
            time.sleep(1.0)
            try:
                captured = tab.pane.capture_pane(start="-200")
                if isinstance(captured, str):
                    text = captured
                else:
                    text = "\n".join(captured)
            except Exception:
                continue

            text_lower = text.lower()

            # Prompt: "Bypass Permissions mode" warning
            # Default is "No, exit" — we need to select "Yes, I accept" (option 2)
            if "bypass permissions" in text_lower and "yes, i accept" in text_lower and "bypass" not in handled:
                # Navigate Down to "Yes, I accept", then press Enter
                tab.pane.send_keys("Down", enter=False)
                time.sleep(0.3)
                tab.pane.send_keys("", enter=True)
                handled.add("bypass")
                logger.info("Claude Code startup: accepted Bypass Permissions prompt")
                time.sleep(2.0)
                continue

            # Prompt: "Do you want to use this API key?"
            # (fallback — should be prevented by env stripping)
            if "do you want to use this api key" in text_lower and "api_key" not in handled:
                # "No (recommended)" is the default selection, just press Enter
                tab.pane.send_keys("", enter=True)
                handled.add("api_key")
                logger.info("Claude Code startup: dismissed API key prompt")
                time.sleep(2.0)
                continue

            # Prompt: "Do you trust the files in this folder?"
            if "trust" in text_lower and "folder" in text_lower and "trust" not in handled:
                tab.pane.send_keys("y", enter=True)
                handled.add("trust")
                logger.info("Claude Code startup: accepted trust prompt")
                time.sleep(2.0)
                continue

            # Prompt: Any other numbered menu with ❯ indicator — accept default
            if "❯" in text and "enter to confirm" in text_lower and "generic_menu" not in handled:
                tab.pane.send_keys("", enter=True)
                handled.add("generic_menu")
                logger.info("Claude Code startup: confirmed default menu selection")
                time.sleep(2.0)
                continue

            # Ready detection: Claude Code shows its welcome/input prompt
            if any(indicator in text for indicator in [
                "Welcome to Claude Code",
                "/help",
                "What can I help you with",
                "Type your prompt",
            ]):
                logger.info(
                    f"Claude Code startup complete "
                    f"(handled: {handled or 'no prompts'})"
                )
                return

            # Exit detection: Claude Code exited (prompt dismissed wrong, crashed, etc.)
            if "(.venv) $" in text and ("bypass" in handled or time.monotonic() - start > 10):
                # Shell prompt visible after handling — Claude Code may have exited
                logger.warning(
                    f"Claude Code may have exited during startup "
                    f"(handled: {handled or 'no prompts'})"
                )
                return

        logger.info(
            f"Claude Code startup handler timed out after {timeout}s "
            f"(handled: {handled or 'no prompts'})"
        )

    def _get_tab(self, name: str) -> ShellTab:
        """Get a tab by name, raising KeyError if not found."""
        if name not in self._tabs:
            available = ", ".join(self._tabs.keys()) or "(none)"
            raise KeyError(f"Tab '{name}' not found. Available: {available}")
        return self._tabs[name]
