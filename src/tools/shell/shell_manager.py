"""Persistent shell session manager — delegates to the workspace backend.

The agent's shells run ON THE WORKSPACE (container pod or VM), never in the
agent pod: ShellManager requires a workspace backend that declares
``supports_shell`` and forwards every operation to it (RemoteBackend drives
tmux on the workspace over SSH). The former local-libtmux execution path was
removed — it served a bare-metal dev posture that is deprecated, and a
dormant in-pod execution path is a liability (same rationale as
docs/issues/remove_local_browser_fallback.md; see
docs/features/no_workspace_agent_mode.md §9).

What remains agent-side:
  * sudo interception and blocked-command gating (must fire before any
    delegation so the backend path cannot bypass them),
  * the shared sentinel/stall machinery (templates + pure helpers) consumed
    by the backend implementation in src/core/backends/remote.py.

Two tools expose this manager: shell_execute / run_command and shell_read.
"""

import logging
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def build_sentinel_command(command: str, sentinel: str) -> Tuple[str, Optional[str]]:
    """Build the command string to send to tmux for sentinel-based completion.

    Single-line commands use simple ';' chaining: this preserves the
    existing interactive-prompt detection semantics — when a single-line
    command waits on stdin (e.g. `read`, ssh password prompt), the polling
    loop sees the prompt and reports it.

    Multi-line commands are wrapped in an outer bash heredoc so the user's
    command (including any inner heredocs like `python3 <<'PY' ... PY`) is
    read by inner bash from a captured heredoc body, instead of being typed
    line-by-line into tmux. This fixes BUG-5: previously the heredoc
    terminator landed on the same line as the sentinel echo (`PY; echo ...`)
    and the heredoc never closed, leaving the tab permanently stuck.

    Returns:
        (full_cmd, start_marker) where start_marker is a unique string for
        multi-line commands (used by extraction to locate where the user
        command's stdout begins) or None for single-line commands.
    """
    if "\n" not in command:
        return f'{command}; echo "{sentinel} $?"', None

    outer_delim = f"SRW_DELIM_{uuid.uuid4().hex[:12]}"
    start_marker = f"__SRW_START_{uuid.uuid4().hex[:12]}__"
    full_cmd = (
        f'bash << "{outer_delim}"\n'
        f'echo "{start_marker}"\n'
        f"{command}\n"
        f'echo "{sentinel} $?"\n'
        f"{outer_delim}"
    )
    return full_cmd, start_marker


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

# Seconds of *no new output* before a still-running command yields control back
# to the model (the "soft" no-change timeout). Generous on purpose: heavy
# installs/builds (e.g. pip downloading torch + CUDA wheels) routinely go quiet
# for many seconds without waiting for input. Matches OpenHands' default.
NO_CHANGE_TIMEOUT_SECONDS = 30.0

# Absolute ceiling on how long a single synchronous command may block, even
# when the caller passes a larger timeout.
HARD_TIMEOUT_CAP_SECONDS = 600

# Non-interactive environment applied to every fresh shell, so that pagers,
# progress bars and credential prompts can't stall the no-change detector or
# hang the command. (Deliberately does NOT set TERM=dumb — too disruptive.)
NONINTERACTIVE_ENV_EXPORT = (
    "export PAGER=cat GIT_PAGER=cat GIT_TERMINAL_PROMPT=0 GIT_ASKPASS= "
    "SSH_ASKPASS= DEBIAN_FRONTEND=noninteractive PIP_PROGRESS_BAR=off "
    "PIP_DISABLE_PIP_VERSION_CHECK=1"
)

# Returned when a command has produced no new output for NO_CHANGE_TIMEOUT_SECONDS
# (the soft no-change timeout). Leads with "Exit code: -1" so downstream parsers
# read it as "not finished" (distinct from success 0 and generic failure 1).
# This is NOT an error — the process keeps running on its tab.
#
# Guidance here is mode-NEUTRAL: it must be valid even for the least-capable tool
# set (stateless run_command + shell_read, which cannot send keys, abort, or use
# other tabs). Tool-specific options (C-c, extra tabs) are taught by the
# persistent shell_execute tool's own docstring, not baked in here.
STILL_RUNNING_TEMPLATE = (
    "Exit code: -1\n"
    "--- still running ---\n"
    "Command on tab '{tab}' has been running {elapsed:.0f}s with no new output "
    "in the last {quiet:.0f}s — it has NOT finished (this is NOT an error; the "
    "process is still executing on the tab).\n"
    "Read the tab again after a moment to check for new output or completion; "
    "the tab stays busy until it finishes, so don't send it another command yet "
    "and don't poll in a tight loop. Tip: for work you expect to be slow or "
    "quiet (large installs, builds, data ingestion/embedding, downloads), pass "
    "an explicit `timeout` when you START the command so the call waits for the "
    "full duration instead of returning here.\n"
    "--- terminal state ---\n{terminal_state}"
)

# Returned when a command hits the hard timeout cap (the maximum a single call
# will wait) without completing. Distinct from the soft message above: the
# process may have been emitting output the whole time, so this must NOT claim
# the output went quiet.
STILL_RUNNING_HARDCAP_TEMPLATE = (
    "Exit code: -1\n"
    "--- still running ---\n"
    "Command on tab '{tab}' is still running after {elapsed:.0f}s — that is the "
    "maximum wait for one call, not an error, and it may still be producing "
    "output.\n"
    "Read the tab again to keep monitoring; the tab stays busy until it "
    "finishes. If you expect it to take much longer, start such commands with a "
    "larger explicit `timeout`.\n"
    "--- terminal state ---\n{terminal_state}"
)

# Returned when a new command is sent to a tab whose previous command is still
# running. The new command is NOT executed (avoids head-of-line blocking the
# tab with two interleaved commands). Mode-NEUTRAL guidance only (see above).
COLLIDING_COMMAND_TEMPLATE = (
    "Tab '{tab}' has a previous command still running; your new command was "
    "NOT executed.\n"
    "Wait for it to finish before sending another command here — read the tab "
    "to monitor its progress.\n"
    "--- terminal state ---\n{terminal_state}"
)

# Returned when the command is genuinely blocked on an interactive prompt
# (password, y/n, etc.). Unlike a no-change stall, this one really does need
# input — the model should respond in keys mode.
INTERACTIVE_PROMPT_TEMPLATE = (
    "Interactive prompt detected ({prompt_type}). The command is waiting for "
    "input on tab '{tab}'.\n"
    "Respond by sending the expected input (or a control key like C-c to "
    "cancel) in keys mode.\n"
    "--- terminal state ---\n{terminal_state}"
)


def compute_no_change_state(
    all_lines: List[str],
    prev_hash: Optional[int],
    stall_start: Optional[float],
    now: float,
    soft_enabled: bool,
    threshold: float,
) -> Tuple[int, Optional[float], bool]:
    """Track whether a running command's output has gone quiet long enough.

    Hashes the FULL captured buffer (not just the visible tail) so output
    scrolling anywhere — e.g. a long ``pip`` download printing above the
    visible lines — counts as activity and resets the clock. The previous
    implementation hashed only ``all_lines[-20:]`` and so mistook steady
    long-running work for a stall.

    Args:
        all_lines: Current full pane capture, as lines.
        prev_hash: Hash returned by the previous call (None on the first poll).
        stall_start: Monotonic time the current no-change streak began, or None.
        now: Current monotonic time.
        soft_enabled: Whether the soft no-change timeout applies. When the
            caller supplied an explicit timeout this is False, so only the
            caller's hard timeout bounds the command.
        threshold: Seconds of no change before declaring a soft timeout.

    Returns:
        ``(new_hash, new_stall_start, timed_out)``. ``timed_out`` is True only
        when ``soft_enabled`` and output has been unchanged for >= ``threshold``.
    """
    new_hash = hash(tuple(all_lines))
    if new_hash != prev_hash:
        # Output changed (or first observation) -> reset the no-change clock.
        return new_hash, None, False
    # Output unchanged since the previous poll.
    if stall_start is None:
        stall_start = now
    timed_out = soft_enabled and (now - stall_start >= threshold)
    return new_hash, stall_start, timed_out


def prompt_is_ready(all_lines: List[str]) -> bool:
    """True when the shell appears idle at a prompt.

    The last non-blank line ending in ``$``, ``#`` or ``%`` means bash is back
    at its prompt — i.e. a previously-running command has finished or been
    interrupted, even when its completion sentinel never printed (e.g. after a
    C-c). Used by the colliding-command guard to know when a tab is free again.
    """
    for line in reversed(all_lines):
        stripped = line.strip()
        if stripped:
            return stripped[-1] in ("$", "#", "%")
    return False


@dataclass
class ShellTab:
    """Agent-side stub tracking a tab that lives on the workspace backend."""

    name: str
    tab_type: str  # "shell", "ssh", "repl", "process"
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
    """Forwards persistent tmux-backed shell sessions to the workspace backend.

    Every operation delegates to a workspace backend that declares
    ``supports_shell`` (e.g. RemoteBackend, which owns the tmux session on
    the workspace over SSH). There is NO local execution path: constructing
    a ShellManager without a shell-capable backend raises, and the callers
    simply don't register shell tools for such workspaces (capability, not
    inference).

    Agent-side gating (sudo interception, blocked commands) runs before any
    delegation so the backend path cannot bypass it. Tab limits, scrollback
    and timeout semantics are enforced by the backend implementation; the
    corresponding constructor parameters are retained for config plumbing.
    """

    def __init__(
        self,
        job_id: str,
        max_tabs: int = 15,
        scrollback_limit: int = 5000,
        default_timeout: int = 120,
        no_change_timeout: float = NO_CHANGE_TIMEOUT_SECONDS,
        blocked_commands: Optional[List[str]] = None,
        sandbox_cwd: Optional[str] = None,
        backend: Optional[Any] = None,
        sudo_action: str = "freeze",
    ):
        """Initialize ShellManager over a shell-capable workspace backend.

        Args:
            job_id: Unique job identifier (used in session name)
            max_tabs: Maximum number of concurrent shell tabs (backend-enforced)
            scrollback_limit: Tmux history-limit per pane (backend-enforced)
            default_timeout: Default (hard) timeout for run_sync in seconds
                             (backend-enforced)
            no_change_timeout: Seconds of no new output before a still-running
                               command yields control back (backend-enforced)
            blocked_commands: Commands to block (None = use defaults)
            sandbox_cwd: Working directory to restrict commands to (backend
                         workspaces start in the workspace root)
            backend: Workspace backend with shell support — REQUIRED. All
                     shell operations delegate to it.
            sudo_action: How to handle sudo commands. "freeze" returns a sentinel
                         for the tool layer to trigger a job freeze (VM upgrade prompt).
                         "block" hard-rejects. "allow" passes through (VM-backed agents).

        Raises:
            RuntimeError: If no backend is given or it does not declare shell
                support. The local (in-pod) libtmux execution path was removed —
                shells only run on the workspace, never in the agent pod.
        """
        if backend is None or not getattr(backend, "supports_shell", False):
            raise RuntimeError(
                "ShellManager requires a workspace backend with shell support "
                "(backend.supports_shell=True). Local in-pod tmux execution was "
                "removed — shells run only on the workspace. See "
                "docs/features/no_workspace_agent_mode.md §9."
            )

        self.job_id = job_id
        self.max_tabs = max_tabs
        self.scrollback_limit = scrollback_limit
        self.default_timeout = default_timeout
        self.no_change_timeout = no_change_timeout
        self.sandbox_cwd = sandbox_cwd
        self._backend = backend
        self.sudo_action = sudo_action

        if blocked_commands is None:
            self.blocked_commands = DEFAULT_BLOCKED_COMMANDS
        else:
            self.blocked_commands = frozenset(blocked_commands)

        # Agent-side stubs mirroring backend tabs (bookkeeping only — the
        # backend owns the authoritative tab state).
        self._tabs: OrderedDict[str, ShellTab] = OrderedDict()
        self._session_name = f"agent_{job_id[:12]}"
        logger.info(
            f"ShellManager initialized with backend delegation: "
            f"session={self._session_name}"
        )

    def ensure_tab(self, name: str) -> ShellTab:
        """Get an existing tab or auto-create a new shell tab on the backend.

        Args:
            name: Tab name (lowercase alphanumeric + hyphens, max 20 chars)

        Returns:
            ShellTab stub (existing or newly created)

        Raises:
            ValueError: If name is invalid (backend-validated)
        """
        self._backend.shell_ensure_tab(name)
        if name not in self._tabs:
            self._tabs[name] = ShellTab(name=name, tab_type="shell")
        return self._tabs[name]

    def open_tab(
        self,
        name: str,
        command: Optional[str] = None,
        tab_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Open a new named tab in the backend's tmux session.

        Args:
            name: Tab name (lowercase alphanumeric + hyphens, max 20 chars)
            command: Optional command to run on tab creation
            tab_type: Tab type ("shell", "ssh", "repl", "process").
                      Auto-detected from command if not specified.

        Returns:
            Metadata dict for the new tab

        Raises:
            ValueError: If name is invalid or duplicate (backend-validated)
        """
        metadata = self._backend.shell_open_tab(
            name, command=command, tab_type=tab_type
        )
        self._tabs[name] = ShellTab(
            name=name,
            tab_type=metadata.get("type", "shell"),
        )
        return metadata

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
        # Check blocked commands when actually executing (enter=True).
        # Must run before backend delegation so sudo intercept always fires.
        if enter:
            blocked = self._check_blocked(text)
            if blocked:
                return blocked
        return self._backend.shell_send(name, text, enter=enter)

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
        return self._backend.shell_read(name, lines=lines, since_cursor=since_cursor)

    def close_tab(self, name: str) -> str:
        """Close a tab by killing its tmux window on the backend.

        Args:
            name: Tab name

        Returns:
            Confirmation message

        Raises:
            KeyError: If tab doesn't exist
        """
        result = self._backend.shell_close_tab(name)
        self._tabs.pop(name, None)
        return result

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
        return self._backend.shell_read_with_offset(name, lines=lines, offset=offset)

    def format_tab_header(self) -> str:
        """Build tab header string showing all live shells.

        Returns:
            Header string like ``[Shells: default | gpu-box]``
        """
        return self._backend.shell_format_tab_header()

    def list_tabs(self) -> List[Dict[str, Any]]:
        """Return metadata for all tabs.

        Returns:
            List of tab metadata dicts
        """
        return self._backend.shell_list_tabs()

    def run_sync(
        self,
        command: str,
        timeout: Optional[int] = None,
        working_dir: Optional[str] = None,
        tab_name: str = "default",
    ) -> str:
        """Execute a command synchronously in a shell-type tab on the backend.

        The backend uses a sentinel marker pattern to detect command
        completion and extract the exit code; it derives the soft (no-change)
        and hard timeouts from ``timeout`` exactly as documented on the
        module constants (an explicit timeout disables the soft no-change
        timeout).

        Args:
            command: Shell command to execute
            timeout: Timeout in seconds (None => backend default + soft timeout)
            working_dir: Working directory for the command (relative to workspace)
            tab_name: Name of the tab to run in (default "default"). Must be
                      a shell-type tab (sentinel detection requires a bash-like shell).

        Returns:
            Formatted output string with exit code and stdout

        Raises:
            ValueError: If command is blocked or tab is not shell-type
            KeyError: If tab does not exist
        """
        # Safety check — must run before backend delegation so sudo
        # intercept and blocked-command checks always fire.
        blocked = self._check_blocked(command)
        if blocked:
            return blocked

        return self._backend.shell_run(
            command,
            timeout=timeout,
            tab_name=tab_name,
            working_dir=working_dir,
        )

    def cleanup(self) -> None:
        """Kill the backend tmux session and clear local tab stubs."""
        self._backend.shell_cleanup()
        self._tabs.clear()

    def is_alive(self) -> bool:
        """Check if the backend tmux session still exists."""
        return self._backend.shell_is_alive()

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
