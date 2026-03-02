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
        "description": "Execute a command or send keystrokes in a persistent terminal tab",
        "category": "coding",
        "short_description": "Run commands in a persistent terminal.",
        "phases": ["strategic", "tactical"],
    },
    "shell_read": {
        "module": "coding.shell_tools",
        "function": "shell_read",
        "description": "Read output from a persistent terminal tab with offset support",
        "category": "coding",
        "short_description": "Read output from a terminal tab.",
        "phases": ["strategic", "tactical"],
    },
}


def _apply_tail(output: str, tail: int) -> str:
    """Apply tail truncation to run_sync output, preserving the exit code header.

    run_sync returns format like:
        Exit code: 0
        --- stdout ---
        line1
        line2
        ...

    This function keeps the header and truncates only the stdout body.
    """
    lines = output.split("\n")

    # Find the "--- stdout ---" separator
    separator_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "--- stdout ---":
            separator_idx = i
            break

    if separator_idx is None:
        # No separator (e.g. "(no output)" or "Command timed out...")
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
        """Execute a command or send keystrokes in a persistent terminal.

        Runs commands in named shells that persist across calls. Environment
        variables, virtualenvs, working directory, and history are preserved.
        If the named shell doesn't exist, it is auto-created.

        Args:
            command: Command to execute, or special key name when keys=True.
                     Special keys: "Up", "Down", "Left", "Right", "Enter",
                     "Tab", "Escape", "C-c", "C-d", "C-z", "C-l".
            name: Shell name (default "default"). Auto-creates if it doesn't
                  exist. Use descriptive names like "gpu-box", "dev-server".
            tail: Number of output lines to return from the end (default 30).
                  Increase for verbose commands (e.g. test suites, builds).
            is_async: If true, send command without waiting for completion
                      and return whatever output appeared after ~0.5s.
                      Use for long-running processes like dev servers.
                      Default false (waits for command to finish).
            keys: If true, treat command as special keystrokes sent via tmux
                  (e.g. "C-c" to interrupt, "Up" for history). No Enter is
                  appended automatically. Default false.

        Returns:
            Tab header + command output with exit code (sync mode),
            or tab header + partial output (async/keys mode).

        Example:
            shell_execute(command="pytest tests/ -x")
            shell_execute(command="npm run dev", name="dev-server", is_async=True)
            shell_execute(command="C-c", name="dev-server", keys=True)
            shell_execute(command="exit", name="dev-server")
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

        Returns terminal output from the named shell. Use offset to read
        from a specific position in the scrollback (like reading a file),
        or omit offset to read from the end (tail behavior).

        Args:
            name: Shell name (default "default").
            offset: Line position to start reading from (0 = start of
                    scrollback buffer). If omitted, reads from the end.
            lines: Number of lines to return (default 30, max 200).

        Returns:
            Tab header + terminal output with line count metadata.

        Example:
            shell_read()
            shell_read(name="dev-server", lines=100)
            shell_read(offset=0, lines=50)
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
