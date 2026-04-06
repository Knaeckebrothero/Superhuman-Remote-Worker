"""Coding utility functions for the Universal Agent.

Provides shared utilities used by shell tools:
- _truncate_output: Truncate large output keeping the tail

The run_command tool has been removed — use the `shell` tool
from shell_tools.py instead, which runs commands in persistent
tmux-backed terminal tabs.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Default maximum output characters (stdout + stderr each)
DEFAULT_MAX_OUTPUT_CHARS = 50000

# Tool metadata for registry (empty — run_command removed)
CODING_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {}


def _truncate_output(text: str, max_chars: int, label: str = "output") -> str:
    """Truncate output, keeping the tail (most useful for test output).

    Args:
        text: The output text to truncate
        max_chars: Maximum characters to keep
        label: Label for the truncation notice

    Returns:
        Truncated text with notice if truncation occurred
    """
    if len(text) <= max_chars:
        return text

    truncated = text[-max_chars:]
    # Try to start at a line boundary
    first_newline = truncated.find("\n")
    if first_newline > 0 and first_newline < 200:
        truncated = truncated[first_newline + 1 :]

    chars_removed = len(text) - len(truncated)
    return f"[{label} truncated: {chars_removed} chars removed from start]\n{truncated}"


def create_coding_tools(context) -> list:
    """Create coding tools with injected context.

    Returns an empty list — run_command has been removed.
    The `shell` tool in shell_tools.py replaces it.
    Kept for backward compatibility with __init__.py imports.
    """
    return []
