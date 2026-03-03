"""Persistent shell session tools for the Universal Agent.

Provides 2 tools for managing tmux-backed terminal sessions:
- shell_execute: Execute commands, send keystrokes, or run async commands
- shell_read: Read output from a terminal tab with offset support

Available in both strategic and tactical phases.
"""

import logging
import time
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext
from .coding_tools import _truncate_output

logger = logging.getLogger(__name__)

# Default max output characters for shell reads
DEFAULT_MAX_READ_LINES = 200
DEFAULT_MAX_OUTPUT_CHARS = 50000

# Tool metadata for registry
SHELL_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
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

    header = lines[:separator_idx + 1]  # "Exit code: N" + "--- stdout ---"
    body = lines[separator_idx + 1:]

    if len(body) <= tail:
        return output

    truncated_body = body[-tail:]
    skipped = len(body) - tail
    return "\n".join(header) + f"\n[...{skipped} lines truncated...]\n" + "\n".join(truncated_body)


def create_shell_tools(context: ToolContext) -> List[Any]:
    """Create shell tools with injected context.

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
          keys=True: Send raw keystrokes — passwords, "C-c", "Enter", "Up",
              "y", etc. No Enter is appended; send "Enter" separately if needed.
          is_async=True: Fire-and-forget. Returns immediately without waiting.
              ONLY for long-running background processes (dev servers, builds,
              VPN connections). Use shell_read() later to check progress.

        SSH workflow (use sync mode, not is_async):
          1. shell_execute(command="ssh user@host", name="srv")
               → detects password prompt, returns terminal state
          2. shell_execute(command="the_password", name="srv", keys=True)
             shell_execute(command="Enter", name="srv", keys=True)
               → password sent, SSH connects
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
            keys: Send raw keystrokes instead of executing a command.

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
                # Keys mode: send special keystrokes, wait briefly, return output
                sm.send(name, command, enter=False)
                time.sleep(0.5)
                text, metadata = sm.read_with_offset(name, lines=tail)
                text = _truncate_output(text, max_output_chars, "shell output")
                return f"{tab_header}\n{text}"

            elif is_async:
                # Async mode: send command, wait briefly, return what appeared
                sm.read(name, lines=1, since_cursor=False)  # snapshot cursor
                sm.send(name, command, enter=True)
                time.sleep(0.5)
                text, metadata = sm.read(name, since_cursor=True)
                text = _truncate_output(text, max_output_chars, "shell output")
                return f"{tab_header}\n{text}"

            else:
                # Sync mode: sentinel-based wait for completion
                output = sm.run_sync(command, tab_name=name)
                output = _apply_tail(output, tail)
                output = _truncate_output(output, max_output_chars, "output")
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

            text, metadata = sm.read_with_offset(name, lines=capped_lines, offset=offset)
            text = _truncate_output(text, max_output_chars, "shell output")
            info = f"({metadata['mode']}) {metadata['lines_returned']}/{metadata['total_lines']} lines"
            return f"{tab_header}\n{info}\n{text}"
        except (KeyError, ValueError) as e:
            try:
                tab_header = sm.format_tab_header()
            except Exception:
                tab_header = "[Shells: ?]"
            return f"{tab_header}\nError: {e}"

    return [shell_execute, shell_read]
