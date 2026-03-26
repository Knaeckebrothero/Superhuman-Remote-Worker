"""Shell tools for the Universal Agent.

Provides tools for command execution in two modes (configured via shell.mode):

Stateless mode (default):
- run_command: Simple command→output execution (hidden persistent tab underneath)
- shell_read: Read more output from scrollback when needed

Persistent mode (opt-in):
- shell_execute: Full tab management, keystrokes, async commands
- shell_read: Read output from any named terminal tab

Available in both strategic and tactical phases.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from .coding_tools import _truncate_output
from .shell_manager import SUDO_FREEZE_SENTINEL
from ..context import ToolContext

logger = logging.getLogger(__name__)

# Default max output characters for shell reads
DEFAULT_MAX_READ_LINES = 200
DEFAULT_MAX_OUTPUT_CHARS = 50000

# Error patterns to scan for in shell output (P11 mitigation)
# These indicate application-level errors even when exit code is 0
_SHELL_ERROR_PATTERNS = [
    ("Traceback (most recent call last)", "Python traceback"),
    ("PermissionError:", "Permission denied"),
    ("ConnectionRefusedError:", "Connection refused"),
    ("FileNotFoundError:", "File not found"),
    ("ModuleNotFoundError:", "Missing Python module"),
    ("OSError:", "OS error"),
    ("FATAL:", "Fatal error"),
    ("panic:", "Go/Rust panic"),
    ("segmentation fault", "Segfault"),
    ("killed", "Process killed (OOM?)"),
    ("No space left on device", "Disk full"),
    ("Connection timed out", "Connection timeout"),
    ("Name or service not known", "DNS resolution failed"),
]


def _scan_for_error_patterns(output: str) -> Optional[str]:
    """Scan shell output for known error patterns and return a warning if found."""
    output_lower = output.lower()
    found = []
    for pattern, label in _SHELL_ERROR_PATTERNS:
        if pattern.lower() in output_lower:
            found.append(label)
    if found:
        return f"⚠ Possible error in output: {', '.join(found)}. Read the output carefully before proceeding."
    return None


# Tmux special key names that should NOT get Enter appended in keys mode
TMUX_SPECIAL_KEYS = frozenset(
    {
        "Up",
        "Down",
        "Left",
        "Right",
        "Enter",
        "Tab",
        "Escape",
        "Space",
        "BSpace",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "NPage",
        "PPage",
        "F1",
        "F2",
        "F3",
        "F4",
        "F5",
        "F6",
        "F7",
        "F8",
        "F9",
        "F10",
        "F11",
        "F12",
        "IC",
        "DC",  # Insert, Delete
    }
)

# Tool metadata for registry
SHELL_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "run_command": {
        "module": "coding.shell_tools",
        "function": "run_command",
        "description": "Execute a shell command and return its output",
        "category": "coding",
        "short_description": "Run a shell command and get output.",
        "phases": ["strategic", "tactical"],
    },
    "shell_execute": {
        "module": "coding.shell_tools",
        "function": "shell_execute",
        "description": "Execute a command or send keystrokes in an independent persistent terminal tab",
        "category": "coding",
        "short_description": "Run commands in a persistent terminal tab.",
        "phases": ["strategic", "tactical"],
    },
    "shell_read": {
        "module": "coding.shell_tools",
        "function": "shell_read",
        "description": "Read scrollback output from a persistent terminal tab",
        "category": "coding",
        "short_description": "Read output from a terminal tab.",
        "phases": ["strategic", "tactical"],
    },
}


def _check_sudo_freeze(output: str, command: str, context: ToolContext) -> Optional[str]:
    """If output is the sudo freeze sentinel, trigger a job freeze and return
    a message for the agent. Returns None if not a sudo freeze."""
    if output != SUDO_FREEZE_SENTINEL:
        return None
    context.request_freeze({
        "freeze_type": "vm_upgrade_required",
        "reason": "Agent attempted a sudo command that requires VM-level access.",
        "command": command,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return (
        "This command requires elevated privileges (sudo). "
        "The job has been paused while the operator decides whether to "
        "upgrade this job to a VM environment. You do not need to take "
        "any action — the job will resume automatically if approved."
    )


def _apply_tail(output: str, tail: int) -> str:
    """Apply tail truncation to run_sync output, preserving the header.

    run_sync returns formats like:
        Exit code: 0
        --- stdout ---
        line1
        ...

    Or on timeout/interactive detection:
        Command timed out after 120s: ssh admin@host
        --- terminal state ---
        Are you sure you want to continue connecting (yes/no)?

    This function keeps the header and truncates only the body.
    """
    lines = output.split("\n")

    # Find the separator line (either stdout or terminal state)
    separator_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in ("--- stdout ---", "--- terminal state ---"):
            separator_idx = i
            break

    if separator_idx is None:
        # No separator (e.g. "(no output)" or short messages)
        return output

    header = lines[: separator_idx + 1]  # "Exit code: N" + "--- stdout ---"
    body = lines[separator_idx + 1 :]

    if len(body) <= tail:
        return output

    truncated_body = body[-tail:]
    skipped = len(body) - tail
    return (
        "\n".join(header)
        + f"\n[...{skipped} lines truncated...]\n"
        + "\n".join(truncated_body)
    )


def create_shell_tools(context: ToolContext) -> List[Any]:
    """Create shell tools with injected context.

    Returns different tool sets based on shell.mode config:
    - "stateless" (default): [run_command, shell_read]
    - "persistent": [shell_execute, shell_read]

    Args:
        context: ToolContext with shell_manager

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If shell_manager not available in context
    """
    sm = context.shell_manager
    if sm is None:
        raise ValueError("Shell tools require shell_manager in ToolContext")

    max_output_chars = context.get_config("max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)
    max_read_lines = context.get_config("shell_max_read_lines", DEFAULT_MAX_READ_LINES)

    # Determine shell mode from config
    shell_config = context.get_config("shell", {})
    mode = (
        shell_config.get("mode", "stateless")
        if isinstance(shell_config, dict)
        else "stateless"
    )

    @tool
    def run_command(
        command: str,
        timeout: int = 120,
        tail: int = 30,
    ) -> str:
        """Execute a shell command and return its output.

        Runs the command to completion and returns the exit code + stdout.
        Commands run in the workspace directory. Use for: running tests,
        building projects, git operations, file system commands, deploying,
        checking logs, SSH via sshpass, and any other shell task.

        If the command requires interactive input (password prompt, y/n
        confirmation), it will return an error. Use non-interactive
        alternatives instead:
          - SSH: sshpass -p 'pass' ssh -o StrictHostKeyChecking=no user@host "cmd"
          - apt/dnf: use -y flag
          - git: configure credential helper

        SUDO NOTE: Commands prefixed with `sudo` may pause the job while
        the operator decides whether to upgrade to a VM environment. If the
        job is upgraded, it will resume automatically with sudo access. If
        not upgraded, try an alternative approach (pip install as user,
        compile from source in userspace, etc.).

        For long output, only the last `tail` lines are returned. Use
        shell_read() to page through the full scrollback if needed.

        Args:
            command: Shell command to execute (e.g., "pytest tests/ -x",
                "git status", "curl -s https://api.example.com/health").
            timeout: Maximum seconds to wait (default 120, max 600).
                Use 600 for sudo commands (approval may take minutes).
            tail: Max stdout lines to return (default 30). Increase for
                verbose output (test suites, builds, logs).

        Returns:
            Exit code + stdout output (last `tail` lines), or error message.

        Examples:
            run_command(command="pytest tests/ -x")
            run_command(command="git diff HEAD~1", tail=100)
            run_command(command="npm run build", timeout=300)
            run_command(command="sshpass -p 'pass' ssh user@host 'systemctl status nginx'")
        """
        try:
            sm.ensure_tab("default")

            output = sm.run_sync(command, tab_name="default", timeout=min(timeout, 600))

            # Sudo intercept: trigger freeze for VM upgrade
            freeze_msg = _check_sudo_freeze(output, command, context)
            if freeze_msg:
                return freeze_msg

            # Interactive prompt → error (model should use non-interactive alternatives)
            if (
                "Interactive prompt detected" in output
                or "Command appears to be waiting for input" in output
            ):
                return (
                    f"Error: Command requires interactive input.\n"
                    f"Use non-interactive alternatives (sshpass, -y flags, etc.).\n"
                    f"{output}"
                )

            output = _apply_tail(output, tail)
            output = _truncate_output(output, max_output_chars, "output")

            warning = _scan_for_error_patterns(output)
            if warning:
                return f"{warning}\n{output}"
            return output

        except (ValueError, KeyError, TimeoutError) as e:
            return f"Error: {e}"

    @tool
    def shell_execute(
        command: str,
        name: str = "default",
        tail: int = 30,
        is_async: bool = False,
        keys: bool = False,
    ) -> str:
        """Execute a command or send keystrokes in a persistent terminal tab.

        Each tab is an independent shell with its own PTY (like a terminal
        window). Tabs auto-create on first use and persist between calls —
        environment, working directory, virtualenvs, and history all survive.
        You can SSH in one tab while running local commands in another.

        IMPORTANT: Never write paramiko, fabric, pexpect, or subprocess SSH
        scripts. Every tab is a real terminal — just use ssh/scp/rsync directly.

        Modes:
          Default (sync): Runs the command and waits for it to finish. Returns
              exit code + stdout. If the command hits an interactive prompt
              (password, y/n, host key, etc.), it returns early with the prompt
              text so you can respond with keys=True. After responding, the tab
              works normally for subsequent commands.
          keys=True: Send raw keystrokes. Text input (passwords, "yes", "y")
              auto-submits with Enter. Control keys ("C-c", "Up", "Escape")
              are sent as-is. Send "Enter" alone to press Enter without text.
          is_async=True: Fire-and-forget. Returns immediately without waiting.
              ONLY for long-running background processes (dev servers, builds,
              VPN connections). Use shell_read() later to check progress.

        SSH workflow (use sync mode, not is_async):
          1. shell_execute(command="ssh user@host", name="srv")
               → detects password prompt, returns terminal state
          2. shell_execute(command="the_password", name="srv", keys=True)
               → password sent + Enter, SSH connects
          3. shell_execute(command="hostname", name="srv")
               → runs on the remote host via sync mode (returns exit code)
          4. shell_execute(command="exit", name="srv", keys=True)
               → closes SSH session, tab returns to local shell

        Args:
            command: Shell command to run, or keystroke when keys=True.
                Keys: "C-c", "C-d", "C-z", "C-l", "Up", "Down", "Enter",
                "Tab", "Escape", or literal text (passwords, "yes", "y").
            name: Tab name (auto-created if new). Lowercase + hyphens, max 20
                chars. Use descriptive names: "gpu-box", "build", "db-server".
            tail: Max stdout lines to return (default 30). Increase for
                verbose output (test suites, builds, logs).
            is_async: Don't wait for completion — return immediately. Only for
                long-running background processes. NOT needed for SSH.
            keys: Send raw keystrokes. Text auto-submits with Enter;
                control keys (C-c, Up, Escape, etc.) are sent as-is.

        Returns:
            [Shells: tab1 | tab2 | ...] header + command output.

        Examples:
            shell_execute(command="pytest tests/ -x")
            shell_execute(command="npm run dev", name="dev", is_async=True)
            shell_execute(command="C-c", name="dev", keys=True)
        """
        try:
            # Ensure tab exists (auto-create if needed)
            sm.ensure_tab(name)
            tab_header = sm.format_tab_header()

            if keys:
                # Keys mode: text input auto-submits with Enter, control keys sent as-is
                is_special = command in TMUX_SPECIAL_KEYS or command.startswith(
                    ("C-", "M-")
                )
                result = sm.send(name, command, enter=not is_special)
                # Sudo intercept: trigger freeze for VM upgrade
                freeze_msg = _check_sudo_freeze(result, command, context)
                if freeze_msg:
                    return freeze_msg
                time.sleep(0.5)
                text, metadata = sm.read_with_offset(name, lines=tail)
                text = _truncate_output(text, max_output_chars, "shell output")
                return f"{tab_header}\n{text}"

            elif is_async:
                # Async mode: send command, wait briefly, return what appeared
                sm.read(name, lines=1, since_cursor=False)  # snapshot cursor
                result = sm.send(name, command, enter=True)
                # Sudo intercept: trigger freeze for VM upgrade
                freeze_msg = _check_sudo_freeze(result, command, context)
                if freeze_msg:
                    return freeze_msg
                time.sleep(0.5)
                text, metadata = sm.read(name, since_cursor=True)
                text = _truncate_output(text, max_output_chars, "shell output")
                return f"{tab_header}\n{text}"

            else:
                # Sync mode: sentinel-based wait for completion
                output = sm.run_sync(command, tab_name=name)
                # Sudo intercept: trigger freeze for VM upgrade
                freeze_msg = _check_sudo_freeze(output, command, context)
                if freeze_msg:
                    return freeze_msg
                output = _apply_tail(output, tail)
                output = _truncate_output(output, max_output_chars, "output")
                # Scan for application-level errors in output
                warning = _scan_for_error_patterns(output)
                if warning:
                    return f"{tab_header}\n{warning}\n{output}"
                return f"{tab_header}\n{output}"

        except (ValueError, KeyError, TimeoutError) as e:
            try:
                tab_header = sm.format_tab_header()
            except Exception:
                tab_header = "[Shells: ?]"
            return f"{tab_header}\nError: {e}"

    @tool
    def shell_read(
        name: str = "default",
        offset: Optional[int] = None,
        lines: int = 30,
    ) -> str:
        """Read output from a persistent terminal tab.

        Returns terminal scrollback from the named tab. Use after is_async
        commands to check on long-running processes, or to inspect a tab's
        full history. Omit offset to read from the end (tail), or set
        offset=0 to read from the start like a file.

        Args:
            name: Tab name (default "default").
            offset: Line position to start from (0 = start of scrollback).
                If omitted, reads from the end (tail behavior).
            lines: Number of lines to return (default 30, max 200).

        Returns:
            Tab header + terminal output with line count metadata.

        Examples:
            shell_read()                             # tail of default tab
            shell_read(name="build", lines=100)      # last 100 lines of build
            shell_read(name="srv", offset=0, lines=50)  # first 50 lines
        """
        try:
            capped_lines = min(lines, max_read_lines)
            sm.ensure_tab(name)
            tab_header = sm.format_tab_header()

            text, metadata = sm.read_with_offset(
                name, lines=capped_lines, offset=offset
            )
            text = _truncate_output(text, max_output_chars, "shell output")
            info = f"({metadata['mode']}) {metadata['lines_returned']}/{metadata['total_lines']} lines"
            return f"{tab_header}\n{info}\n{text}"
        except (KeyError, ValueError) as e:
            try:
                tab_header = sm.format_tab_header()
            except Exception:
                tab_header = "[Shells: ?]"
            return f"{tab_header}\nError: {e}"

    if mode == "persistent":
        return [shell_execute, shell_read]
    else:
        # Stateless mode (default)
        return [run_command, shell_read]
