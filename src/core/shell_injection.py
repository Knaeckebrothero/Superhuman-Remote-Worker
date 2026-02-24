"""Shell state injection as transient SystemMessage.

Injects persistent terminal tab state and usage instructions as a
SystemMessage. Re-injected fresh every execute() call and excluded
from summarization via is_workspace_injection_message().
"""

from langchain_core.messages import SystemMessage

# Prefix for identifying transient shell injection messages.
# Used by is_workspace_injection_message() to exclude from summarization.
SHELL_INJECTION_CONTENT_PREFIX = "<terminal_state>\n"

# Usage instructions included in every shell injection.
_SHELL_USAGE_INSTRUCTIONS = """\
You have persistent terminal tabs backed by tmux. Tabs preserve environment \
variables, virtualenvs, working directory, and command history across calls.

Tools:
- `shell(command, tab)` — Run a command synchronously and get output. Use for most commands.
- `shell_send(name, input, wait)` — Send keystrokes and return new output. \
Use for interactive processes (REPLs, SSH, Claude Code).
- `shell_read(name, lines, since_cursor, wait)` — Read or poll output. \
Use to check on long-running processes or read more history.
- `shell_open(name, command)` / `shell_close(name)` — Manage tabs.
- `shell_list()` — List all open tabs.

Tabs marked [NEW OUTPUT] have new content since you last saw them."""


def create_shell_state_message(content: str, tab_count: int) -> SystemMessage:
    """Create a transient SystemMessage with shell tab state and usage instructions.

    Args:
        content: Tab status with content previews from ShellManager.get_injection_status()
        tab_count: Number of open tabs

    Returns:
        SystemMessage with terminal state, instructions, and tab contents
    """
    return SystemMessage(
        content=(
            f"{SHELL_INJECTION_CONTENT_PREFIX}"
            f"{_SHELL_USAGE_INSTRUCTIONS}\n\n"
            f"{content}\n"
            f"</terminal_state>"
        )
    )
