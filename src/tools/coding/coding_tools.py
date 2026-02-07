"""Coding tools for the Universal Agent.

Provides shell command execution for coding-capable agents:
- run_command: Execute shell commands with timeout and output truncation

The tool executes commands via subprocess in the agent's workspace.
In production (k3s), the container IS the sandbox — no nested containers needed.
"""

import logging
import subprocess
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)

# Default maximum output characters (stdout + stderr each)
DEFAULT_MAX_OUTPUT_CHARS = 50000

# Default command timeout in seconds
DEFAULT_TIMEOUT = 120

# Commands that are blocked for safety (interactive or destructive to host)
BLOCKED_COMMANDS = frozenset([
    "sudo",
    "reboot",
    "shutdown",
    "poweroff",
    "halt",
    "init",
    "systemctl",
])


# Tool metadata for registry
CODING_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "run_command": {
        "module": "coding.coding_tools",
        "function": "run_command",
        "description": "Execute a shell command in the workspace and return stdout/stderr",
        "category": "coding",
        "short_description": "Run a shell command with timeout and output capture.",
        "phases": ["strategic", "tactical"],
    },
}


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
        truncated = truncated[first_newline + 1:]

    chars_removed = len(text) - len(truncated)
    return f"[{label} truncated: {chars_removed} chars removed from start]\n{truncated}"


def _check_blocked(command: str) -> Optional[str]:
    """Check if command starts with a blocked prefix.

    Args:
        command: The command string to check

    Returns:
        Error message if blocked, None if allowed
    """
    first_word = command.strip().split()[0] if command.strip() else ""
    if first_word in BLOCKED_COMMANDS:
        return f"Command blocked: '{first_word}' is not allowed. Blocked commands: {', '.join(sorted(BLOCKED_COMMANDS))}"
    return None


def create_coding_tools(context: ToolContext) -> List[Any]:
    """Create coding tools with injected context.

    Args:
        context: ToolContext with workspace_manager

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If workspace manager not available
    """
    if not context.has_workspace():
        raise ValueError("Coding tools require workspace_manager in ToolContext")

    ws = context.workspace_manager
    max_output_chars = context.get_config("max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)

    @tool
    def run_command(
        command: str,
        working_dir: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> str:
        """Execute a shell command in the workspace and return stdout/stderr.

        Use this to run tests, linters, build commands, git operations, package
        managers, or any other shell command. The command runs in the workspace
        directory by default, or in a subdirectory if working_dir is specified.

        Args:
            command: Shell command to execute (e.g., "pytest tests/ -x",
                     "npm test", "python script.py", "git commit -m 'msg'")
            working_dir: Subdirectory within workspace to run in (optional).
                         Relative to workspace root. Example: "repo" to run
                         in the cloned repository directory.
            timeout: Maximum execution time in seconds (default: 120).
                     Commands exceeding this are killed.

        Returns:
            Structured output with exit code, stdout, and stderr.

        Example:
            run_command(command="pytest tests/ -x")
            run_command(command="npm test", working_dir="repo/cockpit")
            run_command(command="git status", working_dir="repo")
            run_command(command="python -m py_compile src/main.py", working_dir="repo")
        """
        # Safety check
        blocked = _check_blocked(command)
        if blocked:
            return blocked

        # Resolve working directory
        if working_dir:
            # Validate the path is within workspace (security)
            try:
                cwd = str(ws.get_path(working_dir))
            except (ValueError, PermissionError) as e:
                return f"Invalid working directory: {e}"
        else:
            cwd = str(ws.workspace_path)

        # Cap timeout to reasonable maximum
        timeout_capped = min(timeout, 600)  # 10 minutes max

        logger.info(f"run_command: {command!r} (cwd={cwd}, timeout={timeout_capped}s)")

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_capped,
            )

            # Build structured output
            output_parts = [f"Exit code: {result.returncode}"]

            if result.stdout:
                stdout = _truncate_output(result.stdout, max_output_chars, "stdout")
                output_parts.append(f"--- stdout ---\n{stdout}")

            if result.stderr:
                stderr = _truncate_output(result.stderr, max_output_chars, "stderr")
                output_parts.append(f"--- stderr ---\n{stderr}")

            if not result.stdout and not result.stderr:
                output_parts.append("(no output)")

            output = "\n".join(output_parts)
            logger.info(f"run_command exit={result.returncode}, stdout={len(result.stdout or '')} chars, stderr={len(result.stderr or '')} chars")
            return output

        except subprocess.TimeoutExpired:
            msg = f"Command timed out after {timeout_capped}s: {command}"
            logger.warning(msg)
            return msg

        except Exception as e:
            msg = f"Command execution failed: {e}"
            logger.error(msg)
            return msg

    return [run_command]
