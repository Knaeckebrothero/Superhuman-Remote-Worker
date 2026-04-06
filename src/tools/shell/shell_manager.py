"""Persistent shell session manager backed by tmux.

Provides a terminal multiplexer that gives the agent persistent, named shells.
The default "default" shell persists across calls, preserving environment
variables, working directory, and command history.

Shells are auto-created on first use and closed via `exit`. Two tools expose
this manager: shell_execute (run commands / send keys) and shell_read (read
scrollback history).

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
}

# Sentinel returned by _check_blocked when sudo is intercepted (freeze mode)
SUDO_FREEZE_SENTINEL = "SUDO_FREEZE_REQUESTED"

# Default blocked commands (sudo handled separately via sudo_action)
DEFAULT_BLOCKED_COMMANDS = frozenset(
    [
        "reboot",
        "shutdown",
        "poweroff",
        "halt",
        "init",
    ]
)

# Patterns that indicate the terminal is waiting for interactive input.
# Each entry is a tuple of (compiled_regex, description).
INTERACTIVE_PROMPT_PATTERNS = [
    # Yes/No confirmation prompts
    (
        re.compile(
            r"\[y/n\]|\[Y/n\]|\[y/N\]|\[N/y\]|\(yes/no\)|\(yes/no/\[fingerprint\]\)",
            re.IGNORECASE,
        ),
        "confirmation prompt",
    ),
    # Password / passphrase prompts
    (re.compile(r"(?:password|passphrase)\s*:", re.IGNORECASE), "password prompt"),
    # SSH host key verification
    (
        re.compile(r"Are you sure you want to continue connecting", re.IGNORECASE),
        "SSH host key verification",
    ),
    # PackageKit / dnf install prompts (Fedora)
    (
        re.compile(r"Install package '.*?' to provide command", re.IGNORECASE),
        "package install prompt",
    ),
    # sudo password
    (re.compile(r"\[sudo\] password for", re.IGNORECASE), "sudo password prompt"),
    # Press any key / press enter
    (
        re.compile(r"press any key|press enter to continue|hit enter", re.IGNORECASE),
        "press key prompt",
    ),
    # GPG passphrase
    (re.compile(r"enter passphrase", re.IGNORECASE), "passphrase prompt"),
]

# Seconds of unchanged output before declaring a stall (command waiting for input)
STALL_DETECTION_SECONDS = 5.0


@dataclass
class ShellTab:
    """Tracks per-tab state for a persistent shell session."""

    name: str
    tab_type: str  # "shell", "ssh", "repl", "process"
    window: Any  # libtmux.Window
    pane: Any  # libtmux.Pane
    read_cursor: int = 0  # Line index for since_cursor reads
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_metadata(self) -> Dict[str, Any]:
        """Return a metadata dict for list_tabs."""
        return {
            "name": self.name,
            "type": self.tab_type,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
        }


class ShellManager:
    """Manages persistent tmux-backed shell sessions for the agent.

    Creates a detached tmux session with a default shell and provides
    methods for command execution, keystroke sending, and output reading.
    New shells are auto-created on first use via ensure_tab().

    When a workspace backend with shell support is provided (e.g. RemoteBackend),
    all shell operations are delegated to the backend. Otherwise, local libtmux
    is used directly (current behavior).
    """

    # Compiled patterns for ANSI escape filtering
    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07")

    def __init__(
        self,
        job_id: str,
        max_tabs: int = 15,
        scrollback_limit: int = 5000,
        default_timeout: int = 120,
        blocked_commands: Optional[List[str]] = None,
        sandbox_cwd: Optional[str] = None,
        backend: Optional[Any] = None,
        sudo_action: str = "freeze",
    ):
        """Initialize ShellManager with a new tmux session.

        Args:
            job_id: Unique job identifier (used in session name)
            max_tabs: Maximum number of concurrent shell tabs
            scrollback_limit: Tmux history-limit per pane
            default_timeout: Default timeout for run_sync in seconds
            blocked_commands: Commands to block (None = use defaults)
            sandbox_cwd: Working directory to restrict commands to (None = no restriction)
            backend: Optional workspace backend with shell support. When provided
                     and backend.supports_shell is True, all shell operations delegate
                     to the backend (for remote execution). When None, local libtmux
                     is used.
            sudo_action: How to handle sudo commands. "freeze" returns a sentinel
                         for the tool layer to trigger a job freeze (VM upgrade prompt).
                         "block" hard-rejects. "allow" passes through (VM-backed agents).
        """
        self.job_id = job_id
        self.max_tabs = max_tabs
        self.scrollback_limit = scrollback_limit
        self.default_timeout = default_timeout
        self.sandbox_cwd = sandbox_cwd
        self._backend = backend
        self.sudo_action = sudo_action

        if blocked_commands is None:
            self.blocked_commands = DEFAULT_BLOCKED_COMMANDS
        else:
            self.blocked_commands = frozenset(blocked_commands)

        # Check if we should delegate to the backend
        self._use_backend = backend is not None and getattr(
            backend, "supports_shell", False
        )

        if self._use_backend:
            # Backend handles all shell state — no local tmux needed
            self._sync_lock = threading.Lock()
            self._tabs = OrderedDict()
            self._session_name = f"agent_{job_id[:12]}"
            logger.info(
                f"ShellManager initialized with backend delegation: "
                f"session={self._session_name}"
            )
            return

        # --- Local libtmux initialization (existing behavior) ---

        # Thread lock for synchronous command execution
        self._sync_lock = threading.Lock()

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
            window_name="default",
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
        self._tabs["default"] = ShellTab(
            name="default",
            tab_type="shell",
            window=default_window,
            pane=default_pane,
        )

        # Set working directory if sandbox_cwd is set
        if self.sandbox_cwd:
            default_pane.send_keys(f"cd {self.sandbox_cwd}", enter=True)
            time.sleep(0.1)

        logger.info(
            f"ShellManager initialized: session={session_name}, "
            f"tabs={list(self._tabs.keys())}"
        )

    def _ensure_session_alive(self) -> None:
        """Recreate the tmux session if it has died externally."""
        if self._use_backend:
            return  # Backend manages its own session
        if self.is_alive():
            return

        logger.warning(f"Tmux session '{self._session_name}' is dead, recreating")
        self._tabs.clear()

        self._session = self._server.new_session(
            session_name=self._session_name,
            window_name="default",
            x=200,
            y=30,
            detach=True,
        )
        self._session.set_option("history-limit", str(self.scrollback_limit))

        default_window = self._session.active_window
        default_pane = default_window.active_pane
        self._tabs["default"] = ShellTab(
            name="default",
            tab_type="shell",
            window=default_window,
            pane=default_pane,
        )

        if self.sandbox_cwd:
            default_pane.send_keys(f"cd {self.sandbox_cwd}", enter=True)
            time.sleep(0.1)

    def ensure_tab(self, name: str) -> ShellTab:
        """Get an existing tab or auto-create a new shell tab.

        Args:
            name: Tab name (lowercase alphanumeric + hyphens, max 20 chars)

        Returns:
            ShellTab instance (existing or newly created)

        Raises:
            ValueError: If name is invalid
        """
        if self._use_backend:
            self._backend.shell_ensure_tab(name)
            # Return a stub ShellTab for compatibility
            if name not in self._tabs:
                self._tabs[name] = ShellTab(
                    name=name,
                    tab_type="shell",
                    window=None,
                    pane=None,
                )
            return self._tabs[name]
        self._ensure_session_alive()
        if name in self._tabs:
            return self._tabs[name]
        self.open_tab(name)
        return self._tabs[name]

    def open_tab(
        self,
        name: str,
        command: Optional[str] = None,
        tab_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Open a new named tab in the tmux session.

        Args:
            name: Tab name (lowercase alphanumeric + hyphens, max 20 chars)
            command: Optional command to run on tab creation
            tab_type: Tab type ("shell", "ssh", "repl", "process").
                      Auto-detected from command if not specified.

        Returns:
            Metadata dict for the new tab

        Raises:
            ValueError: If name is invalid or duplicate
        """
        if self._use_backend:
            metadata = self._backend.shell_open_tab(
                name, command=command, tab_type=tab_type
            )
            # Create stub ShellTab for local tracking
            self._tabs[name] = ShellTab(
                name=name,
                tab_type=metadata.get("type", "shell"),
                window=None,
                pane=None,
            )
            return metadata
        # Validate name
        if not TAB_NAME_PATTERN.match(name):
            raise ValueError(
                f"Invalid tab name '{name}': must match {TAB_NAME_PATTERN.pattern}"
            )

        if name in self._tabs:
            raise ValueError(f"Tab '{name}' already exists")

        if len(self._tabs) >= self.max_tabs:
            tab_names = ", ".join(self._tabs.keys())
            raise ValueError(
                f"Maximum tabs ({self.max_tabs}) reached. Close unused tabs first: "
                f"shell_execute(command='exit', name='<tab>') — repeat until the tab closes. "
                f"Open tabs: {tab_names}"
            )

        # Auto-detect type from command
        if tab_type is None:
            tab_type = "shell"
            if command:
                first_word = command.strip().split()[0]
                # Strip path prefix (e.g. /usr/bin/python -> python)
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
            text: Text to send (plain text or tmux key names like "Up", "C-c")
            enter: Whether to press Enter after sending (default True)

        Returns:
            Confirmation message or blocked error

        Raises:
            KeyError: If tab doesn't exist
        """
        if self._use_backend:
            return self._backend.shell_send(name, text, enter=enter)
        # Check blocked commands when actually executing (enter=True)
        if enter:
            blocked = self._check_blocked(text)
            if blocked:
                return blocked
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
        if self._use_backend:
            return self._backend.shell_read(
                name, lines=lines, since_cursor=since_cursor
            )
        tab = self._get_tab(name)
        all_lines = self._capture_lines(tab)
        total_lines = len(all_lines)

        if since_cursor:
            # Return only lines after the read cursor
            new_lines = all_lines[tab.read_cursor :]
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
            tab.read_cursor = total_lines
            text = "\n".join(selected) if selected else "(empty)"
            metadata = {
                "tab": name,
                "mode": "tail",
                "lines_requested": lines,
                "lines_returned": len(selected),
                "total_lines": total_lines,
            }

        return text, metadata

    def close_tab(self, name: str) -> str:
        """Close a tab by killing its tmux window.

        Args:
            name: Tab name

        Returns:
            Confirmation message

        Raises:
            KeyError: If tab doesn't exist
        """
        if self._use_backend:
            result = self._backend.shell_close_tab(name)
            self._tabs.pop(name, None)
            return result
        tab = self._get_tab(name)
        tab.window.kill()
        del self._tabs[name]
        logger.info(f"Closed tab '{name}'")
        return f"Tab '{name}' closed"

    def read_with_offset(
        self,
        name: str,
        lines: int = 30,
        offset: Optional[int] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Read output from a tab with optional absolute offset.

        Reads terminal scrollback like a file. When offset is provided,
        reads `lines` lines starting from that position. When offset is
        None, reads the last `lines` lines (tail behavior).

        Args:
            name: Tab name
            lines: Number of lines to return (default 30)
            offset: Absolute line position to start from (0 = start of scrollback).
                    If None, reads from the end (tail behavior).

        Returns:
            Tuple of (text, metadata_dict)

        Raises:
            KeyError: If tab doesn't exist
        """
        if self._use_backend:
            return self._backend.shell_read_with_offset(
                name, lines=lines, offset=offset
            )
        tab = self._get_tab(name)
        all_lines = self._capture_lines(tab)
        total_lines = len(all_lines)

        if offset is not None:
            # Read `lines` lines starting from absolute offset
            start = max(0, min(offset, total_lines))
            end = min(start + lines, total_lines)
            selected = all_lines[start:end]
            text = "\n".join(selected) if selected else "(empty)"
            metadata = {
                "tab": name,
                "mode": "offset",
                "offset": start,
                "lines_returned": len(selected),
                "total_lines": total_lines,
            }
        else:
            # Tail: return last N lines
            start = max(0, total_lines - lines)
            selected = all_lines[start:]
            text = "\n".join(selected) if selected else "(empty)"
            metadata = {
                "tab": name,
                "mode": "tail",
                "lines_returned": len(selected),
                "total_lines": total_lines,
            }

        return text, metadata

    def format_tab_header(self) -> str:
        """Build tab header string showing all live shells.

        Prunes dead tabs first, then returns format like:
        [Shells: default | gpu-box | dev-server]

        Returns:
            Header string
        """
        if self._use_backend:
            return self._backend.shell_format_tab_header()
        self._prune_dead_tabs()
        names = list(self._tabs.keys())
        return f"[Shells: {' | '.join(names)}]"

    def list_tabs(self) -> List[Dict[str, Any]]:
        """Return metadata for all tabs.

        Returns:
            List of tab metadata dicts
        """
        if self._use_backend:
            return self._backend.shell_list_tabs()
        self._prune_dead_tabs()
        return [tab.to_metadata() for tab in self._tabs.values()]

    def run_sync(
        self,
        command: str,
        timeout: Optional[int] = None,
        working_dir: Optional[str] = None,
        tab_name: str = "default",
    ) -> str:
        """Execute a command synchronously in a shell-type tab.

        Uses a sentinel marker pattern to detect command completion and
        extract the exit code. Thread-safe via lock.

        Args:
            command: Shell command to execute
            timeout: Timeout in seconds (default: self.default_timeout)
            working_dir: Working directory for the command (relative to sandbox)
            tab_name: Name of the tab to run in (default "default"). Must be
                      a shell-type tab (sentinel detection requires a bash-like shell).

        Returns:
            Formatted output string with exit code and stdout

        Raises:
            ValueError: If command is blocked or tab is not shell-type
            KeyError: If tab does not exist
            TimeoutError: If command exceeds timeout
        """
        if self._use_backend:
            if timeout is None:
                timeout = self.default_timeout
            return self._backend.shell_run(
                command,
                timeout=timeout,
                tab_name=tab_name,
                working_dir=working_dir,
            )
        # Safety check
        blocked = self._check_blocked(command)
        if blocked:
            return blocked

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
                f"Use is_async=True for interactive tabs."
            )

        with self._sync_lock:
            # --- Pre-flight check: is the tab already stuck? ---
            # If an interactive prompt is already visible, refuse to send the
            # command.  Sending it would type the command text into the waiting
            # prompt (e.g. a [y/N] dialog), making things worse.
            pre_lines = tab.pane.capture_pane(start="-{}".format(self.scrollback_limit))
            if isinstance(pre_lines, str):
                pre_check_lines = pre_lines.splitlines()
            else:
                pre_check_lines = list(pre_lines)

            existing_prompt = self._detect_blocked_tab(pre_check_lines, tab)
            if existing_prompt:
                state_lines = [ln for ln in pre_check_lines if ln.strip()][-30:]
                terminal_state = "\n".join(state_lines) or "(empty)"
                logger.debug(
                    f"Tab '{tab_name}' is already blocked by "
                    f"{existing_prompt} — command not sent: {command}"
                )
                return (
                    f"Tab '{tab_name}' is blocked by a previous "
                    f"{existing_prompt}. Your command was NOT executed.\n"
                    f"Resolve the prompt first: send the expected input "
                    f"with keys mode (e.g. keys='N' or keys='yes'), "
                    f"send C-c to cancel, or use a different tab.\n"
                    f"--- terminal state ---\n{terminal_state}"
                )

            # Change directory if needed
            if working_dir:
                if self.sandbox_cwd:
                    from pathlib import Path

                    full_dir = str(Path(self.sandbox_cwd) / working_dir)
                else:
                    full_dir = working_dir
                tab.pane.send_keys(f"cd {full_dir}", enter=True)
                time.sleep(0.1)

            # Record buffer position before command
            pre_count = len(pre_check_lines)

            # Send command with sentinel
            full_cmd = f'{command}; echo "{sentinel} $?"'
            tab.pane.send_keys(full_cmd, enter=True)

            # Poll for sentinel
            start_time = time.monotonic()
            output_text = ""
            exit_code = None
            last_content_hash = None
            stall_start = None

            while time.monotonic() - start_time < timeout:
                time.sleep(0.2)
                captured = tab.pane.capture_pane(
                    start="-{}".format(self.scrollback_limit)
                )
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
                    new_lines = all_lines[pre_count:sentinel_line_idx]
                    # Filter out lines containing the sentinel (echoed command lines)
                    output_lines = [ol for ol in new_lines if sentinel not in ol]
                    # Skip prompt/command echo lines at the start
                    while output_lines and (
                        command.split()[0] in output_lines[0]
                        or output_lines[0].strip().endswith("$")
                    ):
                        output_lines = output_lines[1:]

                    output_text = "\n".join(output_lines).strip()
                    break

                # --- Early exit: interactive prompt detection ---
                # Only check after the command has had time to produce output
                elapsed = time.monotonic() - start_time
                if elapsed > 1.0:
                    prompt_type = self._detect_interactive_prompt(all_lines, tab)
                    if prompt_type:
                        terminal_state = self._capture_terminal_state(
                            tab, sentinel, pre_count
                        )
                        logger.debug(
                            f"Interactive prompt detected ({prompt_type}) "
                            f"after {elapsed:.1f}s for: {command}"
                        )
                        if working_dir and self.sandbox_cwd:
                            tab.pane.send_keys(f"cd {self.sandbox_cwd}", enter=True)
                            time.sleep(0.1)
                        tab.last_activity = datetime.now(timezone.utc)
                        return (
                            f"Interactive prompt detected ({prompt_type}). "
                            f"Use keys mode to respond.\n"
                            f"--- terminal state ---\n{terminal_state}"
                        )

                    # --- Stall detection ---
                    content_hash = hash(tuple(all_lines[-20:]))
                    if content_hash == last_content_hash:
                        if stall_start is None:
                            stall_start = time.monotonic()
                        elif time.monotonic() - stall_start >= STALL_DETECTION_SECONDS:
                            terminal_state = self._capture_terminal_state(
                                tab, sentinel, pre_count
                            )
                            logger.debug(
                                f"Output stall detected after {elapsed:.1f}s "
                                f"for: {command}"
                            )
                            if working_dir and self.sandbox_cwd:
                                tab.pane.send_keys(f"cd {self.sandbox_cwd}", enter=True)
                                time.sleep(0.1)
                            tab.last_activity = datetime.now(timezone.utc)
                            return (
                                f"Command appears to be waiting for input "
                                f"(no output change for "
                                f"{STALL_DETECTION_SECONDS:.0f}s). "
                                f"Use keys mode to respond or C-c to cancel.\n"
                                f"--- terminal state ---\n{terminal_state}"
                            )
                    else:
                        stall_start = None
                    last_content_hash = content_hash

            # Restore working directory if changed
            if working_dir and self.sandbox_cwd:
                tab.pane.send_keys(f"cd {self.sandbox_cwd}", enter=True)
                time.sleep(0.1)

            tab.last_activity = datetime.now(timezone.utc)

            if exit_code is None:
                terminal_state = self._capture_terminal_state(tab, sentinel, pre_count)
                return (
                    f"Command timed out after {timeout}s: {command}\n"
                    f"--- terminal state ---\n{terminal_state}"
                )

            # Format output
            parts = [f"Exit code: {exit_code}"]
            if output_text:
                parts.append(f"--- stdout ---\n{output_text}")
            else:
                parts.append("(no output)")

            return "\n".join(parts)

    def cleanup(self) -> None:
        """Kill the entire tmux session and clean up."""
        if self._use_backend:
            self._backend.shell_cleanup()
            self._tabs.clear()
            return
        try:
            self._session.kill()
            logger.info(f"Cleaned up tmux session '{self._session_name}'")
        except Exception as e:
            logger.warning(f"Error cleaning up tmux session: {e}")
        self._tabs.clear()

    def is_alive(self) -> bool:
        """Check if the tmux session still exists."""
        if self._use_backend:
            return self._backend.shell_is_alive()
        try:
            sessions = self._server.sessions.filter(session_name=self._session_name)
            return len(sessions) > 0
        except Exception:
            return False

    def _prune_dead_tabs(self) -> None:
        """Remove tabs whose tmux windows are no longer alive."""
        try:
            live_window_ids = {w.id for w in self._session.windows}
        except Exception:
            return

        dead = [
            name
            for name, tab in self._tabs.items()
            if tab.window.id not in live_window_ids
        ]
        for name in dead:
            del self._tabs[name]
            logger.debug(f"Pruned dead tab '{name}'")

    def _capture_lines(self, tab: ShellTab) -> List[str]:
        """Capture and clean the pane buffer for a tab.

        Returns:
            List of output lines with trailing blanks stripped.
        """
        captured = tab.pane.capture_pane(start="-{}".format(self.scrollback_limit))
        if isinstance(captured, str):
            all_lines = captured.splitlines()
        else:
            all_lines = list(captured)

        # Strip trailing empty lines
        while all_lines and not all_lines[-1].strip():
            all_lines.pop()

        return all_lines

    def _check_alternate_screen(self, tab: ShellTab) -> bool:
        """Check if the pane is in alternate screen mode (vim, less, nano, etc.).

        Returns:
            True if alternate screen is active.
        """
        try:
            result = tab.pane.cmd("display-message", "-p", "#{alternate_on}")
            return result.stdout and result.stdout[0].strip() == "1"
        except Exception:
            return False

    def _detect_interactive_prompt(
        self, all_lines: List[str], tab: ShellTab
    ) -> Optional[str]:
        """Check if the terminal appears to be waiting for interactive input.

        Examines the last few lines of pane output for known interactive
        prompt patterns, and checks for alternate screen mode (editors/pagers).

        Args:
            all_lines: Current pane content lines.
            tab: The ShellTab to check.

        Returns:
            Description of the detected prompt type, or None if no prompt detected.
        """
        # Check alternate screen mode (vim, less, nano, etc.)
        if self._check_alternate_screen(tab):
            return "alternate screen (editor/pager)"

        # Check last 5 lines for interactive prompt patterns
        check_lines = all_lines[-5:] if len(all_lines) >= 5 else all_lines
        text_to_check = "\n".join(check_lines)

        for pattern, description in INTERACTIVE_PROMPT_PATTERNS:
            if pattern.search(text_to_check):
                return description

        return None

    def _detect_blocked_tab(self, all_lines: List[str], tab: ShellTab) -> Optional[str]:
        """Stricter check for pre-flight: is the tab stuck on a prompt RIGHT NOW?

        Unlike _detect_interactive_prompt (which fires during polling when we
        know a command is running), this checks BEFORE sending a command.  It
        must avoid false positives when old prompt text is still in scrollback
        but the shell has already recovered (e.g. after C-c).

        Returns:
            Description of the blocking prompt, or None if the tab is ready.
        """
        # Alternate screen is always a blocker
        if self._check_alternate_screen(tab):
            return "alternate screen (editor/pager)"

        # If the last non-blank line looks like a normal shell prompt
        # (ends with $ or # or %), the tab is ready — not blocked.
        last_nonblank = ""
        for line in reversed(all_lines):
            stripped = line.strip()
            if stripped:
                last_nonblank = stripped
                break

        if last_nonblank and last_nonblank[-1] in ("$", "#", "%"):
            return None

        # Check last 3 lines (tighter window than the 5-line polling check)
        check_lines = all_lines[-3:] if len(all_lines) >= 3 else all_lines
        text_to_check = "\n".join(check_lines)

        for pattern, description in INTERACTIVE_PROMPT_PATTERNS:
            if pattern.search(text_to_check):
                return description

        return None

    def _capture_terminal_state(
        self, tab: ShellTab, sentinel: str, pre_count: int
    ) -> str:
        """Capture the current visible terminal state for timeout reporting.

        Args:
            tab: The ShellTab to capture from.
            sentinel: The sentinel string to filter out.
            pre_count: Line count before the command was sent.

        Returns:
            Cleaned terminal state string (last 30 lines, sentinel lines filtered).
        """
        try:
            captured = tab.pane.capture_pane(start="-{}".format(self.scrollback_limit))
            if isinstance(captured, str):
                all_lines = captured.splitlines()
            else:
                all_lines = list(captured)

            # Get lines after the command was sent, filtering sentinel artifacts
            post_lines = all_lines[pre_count:]
            clean_lines = [line for line in post_lines if sentinel not in line]

            # If no post-command lines, fall back to last 30 visible lines
            if not clean_lines:
                clean_lines = all_lines[-30:]

            # Cap at 30 lines to keep it concise
            if len(clean_lines) > 30:
                clean_lines = clean_lines[-30:]

            # Strip trailing empty lines
            while clean_lines and not clean_lines[-1].strip():
                clean_lines.pop()

            return "\n".join(clean_lines)
        except Exception as e:
            logger.debug(f"Failed to capture terminal state: {e}")
            return "(failed to capture terminal state)"

    def _check_blocked(self, command: str) -> str | None:
        """Return error message if command's first word is blocked, else None.

        Returns SUDO_FREEZE_SENTINEL when sudo_action is "freeze" and the
        command starts with sudo. The tool layer detects this sentinel and
        triggers a job freeze (VM upgrade prompt).
        """
        first_word = command.strip().split()[0] if command.strip() else ""
        if not first_word:
            return None

        # Sudo intercept (separate from blocked_commands)
        if first_word == "sudo":
            if self.sudo_action == "allow":
                return None  # VM-backed agents: pass through
            elif self.sudo_action == "freeze":
                return SUDO_FREEZE_SENTINEL
            else:  # "block"
                return (
                    "Command blocked: 'sudo' is not available in this container. "
                    "System package installation requires a VM runtime."
                )

        # Standard blocked commands
        if self.blocked_commands and first_word in self.blocked_commands:
            return (
                f"Command blocked: '{first_word}' is not allowed. "
                f"Blocked commands: {', '.join(sorted(self.blocked_commands))}"
            )
        return None

    def _get_tab(self, name: str) -> ShellTab:
        """Get a tab by name, raising KeyError if not found."""
        if name not in self._tabs:
            available = ", ".join(self._tabs.keys()) or "(none)"
            raise KeyError(f"Tab '{name}' not found. Available: {available}")
        return self._tabs[name]
