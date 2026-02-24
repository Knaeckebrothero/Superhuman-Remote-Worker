"""Persistent shell session tools for the Universal Agent.

Provides 6 tools for managing tmux-backed terminal sessions:
- shell: Synchronous command execution in a named tab (replaces run_command + shell_run)
- shell_open: Open a new named tab
- shell_send: Send keystrokes to a tab
- shell_read: Read output from a tab
- shell_close: Close a tab
- shell_list: List all tabs with status

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
    "shell_open": {
        "module": "coding.shell_tools",
        "function": "shell_open",
        "description": "Open a new persistent terminal tab (shell, SSH, REPL, etc.)",
        "category": "coding",
        "short_description": "Open a named terminal tab.",
        "phases": ["strategic", "tactical"],
    },
    "shell_send": {
        "module": "coding.shell_tools",
        "function": "shell_send",
        "description": "Send keystrokes to a persistent terminal tab",
        "category": "coding",
        "short_description": "Send input to a terminal tab.",
        "phases": ["strategic", "tactical"],
    },
    "shell_read": {
        "module": "coding.shell_tools",
        "function": "shell_read",
        "description": "Read output from a persistent terminal tab",
        "category": "coding",
        "short_description": "Read output from a terminal tab.",
        "phases": ["strategic", "tactical"],
    },
    "shell_close": {
        "module": "coding.shell_tools",
        "function": "shell_close",
        "description": "Close a persistent terminal tab",
        "category": "coding",
        "short_description": "Close a terminal tab.",
        "phases": ["strategic", "tactical"],
    },
    "shell_list": {
        "module": "coding.shell_tools",
        "function": "shell_list",
        "description": "List all open terminal tabs with status",
        "category": "coding",
        "short_description": "List open terminal tabs.",
        "phases": ["strategic", "tactical"],
    },
    "shell": {
        "module": "coding.shell_tools",
        "function": "shell",
        "description": "Execute a command synchronously in a persistent terminal tab and return output",
        "category": "coding",
        "short_description": "Run a command in a persistent terminal tab with output capture.",
        "phases": ["strategic", "tactical"],
    },
}


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
    def shell_open(
        name: str,
        command: Optional[str] = None,
        type: Optional[str] = None,
        cols: int = 200,
        rows: int = 30,
    ) -> str:
        """Open a new persistent terminal tab.

        Creates a named tab in the terminal multiplexer. Use for SSH sessions,
        REPLs, dev servers, or any process that should persist across commands.

        Args:
            name: Tab name (lowercase, alphanumeric + hyphens, max 20 chars).
                  Use descriptive names like "gpu-box", "dev-server", "python-repl".
            command: Optional command to run on tab creation (e.g., "ssh user@host",
                     "python3", "npm run dev"). Tab type is auto-detected from this.
            type: Explicit tab type ("shell", "ssh", "repl", "claude-code", "process").
                  Auto-detected from command if not specified.
            cols: Terminal width (default 200)
            rows: Terminal height (default 30)

        Returns:
            Tab metadata or error message.

        Example:
            shell_open(name="gpu-box", command="ssh user@gpu-box.internal")
            shell_open(name="dev-server", command="npm run dev")
            shell_open(name="python-repl", command="python3")
        """
        try:
            metadata = sm.open_tab(name, command=command, tab_type=type, cols=cols, rows=rows)
            return f"Tab '{name}' opened (type={metadata['type']})"
        except (ValueError, KeyError) as e:
            return f"Error: {e}"

    @tool
    def shell_send(
        name: str,
        input: str,
        enter: bool = True,
        wait: float = 0.5,
    ) -> str:
        """Send keystrokes to a persistent terminal tab and return new output.

        Sends text to the terminal and waits briefly for a response. Returns
        the new output that appeared after sending — no need for a separate
        shell_read call for most interactions.

        For slow processes, set wait=0 and poll later with shell_read(wait=...).

        Args:
            name: Tab name (e.g., "shell", "claude-code", "gpu-box")
            input: Text to send to the terminal
            enter: Whether to press Enter after sending (default True).
                   Set to False for partial input or special keys.
            wait: Seconds to wait for output after sending (default 0.5, max 10).
                  Set to 0 for fire-and-forget (then poll with shell_read).

        Returns:
            New terminal output after sending, or "(no new output)" if nothing appeared yet.

        Example:
            shell_send(name="gpu-box", input="nvidia-smi")
            shell_send(name="claude-code", input="Write tests for auth", wait=3)
            shell_send(name="python-repl", input="import pandas as pd")
        """
        try:
            # Snapshot cursor to current buffer end (so since_cursor returns only NEW lines)
            sm.read(name, lines=1, since_cursor=False)

            # Send keystrokes
            sm.send(name, input, enter=enter)

            # Wait for output
            if wait > 0:
                time.sleep(min(wait, 10.0))

            # Read new output since pre-send position
            text, metadata = sm.read(name, since_cursor=True)
            text = _truncate_output(text, max_output_chars, "shell output")

            header = f"[{name}] {metadata['lines_returned']} new lines"
            return f"{header}\n{text}"
        except KeyError as e:
            return f"Error: {e}"

    @tool
    def shell_read(
        name: str,
        lines: int = 50,
        since_cursor: bool = False,
        wait: float = 0,
        timeout: float = 30,
    ) -> str:
        """Read output from a persistent terminal tab.

        Returns recent terminal output. Use `since_cursor=True` to get only
        new output since the last read (useful for polling long-running tasks).
        Use `wait > 0` to wait for new output before reading.

        Args:
            name: Tab name (e.g., "shell", "claude-code", "gpu-box")
            lines: Number of lines to read from the end (default 50, max 200).
                   Only used when since_cursor is False.
            since_cursor: If True, return only output added since last read.
                          Useful for incremental monitoring of running processes.
            wait: Seconds to wait for output before reading (default 0).
                  Useful after shell_send to wait for response.
            timeout: Max seconds to wait when wait > 0 (default 30).

        Returns:
            Terminal output text with metadata, or error message.

        Example:
            shell_read(name="shell", lines=100)
            shell_read(name="gpu-box", since_cursor=True)
            shell_read(name="claude-code", wait=5)
        """
        try:
            # Cap lines
            lines = min(lines, max_read_lines)

            # Wait for output if requested
            if wait > 0:
                deadline = time.monotonic() + min(wait, timeout)
                # Read initial state
                _, initial_meta = sm.read(name, lines=1, since_cursor=False)
                initial_total = initial_meta.get("total_lines", 0)

                while time.monotonic() < deadline:
                    time.sleep(0.3)
                    _, check_meta = sm.read(name, lines=1, since_cursor=False)
                    if check_meta.get("total_lines", 0) > initial_total:
                        # New output appeared, give a moment to settle
                        time.sleep(0.2)
                        break

            text, metadata = sm.read(name, lines=lines, since_cursor=since_cursor)
            text = _truncate_output(text, max_output_chars, "shell output")
            info = f"[{metadata['mode']}] {metadata['lines_returned']} lines"
            return f"{info}\n{text}"
        except KeyError as e:
            return f"Error: {e}"

    @tool
    def shell_close(name: str) -> str:
        """Close a persistent terminal tab.

        Kills the tab and its running processes. Default tabs (shell, claude-code)
        cannot be closed — they persist for the entire job.

        Args:
            name: Tab name to close

        Returns:
            Confirmation message or error.

        Example:
            shell_close(name="gpu-box")
            shell_close(name="dev-server")
        """
        try:
            return sm.close_tab(name)
        except (KeyError, ValueError) as e:
            return f"Error: {e}"

    @tool
    def shell_list() -> str:
        """List all open terminal tabs with their status.

        Returns a summary of all tabs including type, activity time,
        and whether they can be closed.

        Returns:
            Formatted tab list.
        """
        tabs = sm.list_tabs()
        if not tabs:
            return "No open tabs"

        lines = []
        for t in tabs:
            closeable = "" if t["closeable"] else " (permanent)"
            lines.append(f"  {t['name']} ({t['type']}){closeable}")
        return f"{len(tabs)} open tabs:\n" + "\n".join(lines)

    @tool
    def shell(
        command: str,
        tab: str = "shell",
        timeout: int = 120,
        working_dir: Optional[str] = None,
    ) -> str:
        """Execute a command in a persistent terminal tab and return output.

        Runs the command synchronously in the named tab. The tab preserves state
        between calls — environment variables, virtual environments, working
        directory, and command history all persist.

        Args:
            command: Shell command to execute (e.g., "pytest tests/ -x",
                     "npm test", "git status", "pip install pandas")
            tab: Terminal tab to run in (default "shell"). Must be a shell-type
                 tab. Use shell_open to create new tabs, shell_list to see all tabs.
            timeout: Maximum execution time in seconds (default 120, max 600).
            working_dir: Subdirectory to run in (relative to workspace when
                         sandbox is enabled). Restores original directory after.

        Returns:
            Structured output with exit code and stdout.

        Example:
            shell(command="pytest tests/ -x")
            shell(command="npm test", working_dir="cockpit")
            shell(command="nvidia-smi", tab="gpu-box")
        """
        try:
            output = sm.run_sync(command, timeout=timeout, working_dir=working_dir, tab_name=tab)
            return _truncate_output(output, max_output_chars, "output")
        except (ValueError, KeyError, TimeoutError) as e:
            return f"Error: {e}"

    return [shell, shell_open, shell_send, shell_read, shell_close, shell_list]
