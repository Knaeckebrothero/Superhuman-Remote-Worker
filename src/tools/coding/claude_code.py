"""Claude Code delegation tool for the Universal Agent.

Spawns Claude Code CLI sessions in print mode (-p) to delegate heavy work
(writing, research, multi-step file operations) while the agent orchestrates
via the cheaper API.

Supports multi-turn sessions: the first call returns a session_id, which
subsequent calls can pass via --resume to continue the conversation.

Uses the CLI's own authentication (claude login / Max plan), NOT an API key.
This means rate limits come from the user's Claude subscription, not the
organization's API quota.

Requires: Claude Code CLI installed and authenticated (`claude auth login`).
"""

import asyncio
import json
import logging
import shutil
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)

# Max output chars to return to the agent (protect context window)
MAX_OUTPUT_CHARS = 50000

# Tool metadata for registry
CLAUDE_CODE_METADATA: Dict[str, Dict[str, Any]] = {
    "claude_code": {
        "module": "coding.claude_code",
        "function": "claude_code",
        "description": "Delegate a task to a Claude Code session for heavy work (writing, research, file ops)",
        "category": "coding",
        "short_description": "Spawn Claude Code to do heavy work (writing, research, file ops).",
        "phases": ["strategic", "tactical"],
    },
}


def _truncate_output(text: str, max_chars: int, label: str = "output") -> str:
    """Truncate output, keeping the tail (most useful part)."""
    if len(text) <= max_chars:
        return text

    truncated = text[-max_chars:]
    first_newline = truncated.find("\n")
    if 0 < first_newline < 200:
        truncated = truncated[first_newline + 1:]

    chars_removed = len(text) - len(truncated)
    return f"[{label} truncated: {chars_removed} chars removed from start]\n{truncated}"


def _find_claude_cli() -> Optional[str]:
    """Find the claude CLI binary."""
    return shutil.which("claude")


def create_claude_code_tools(context: ToolContext) -> List[Any]:
    """Create Claude Code delegation tools with injected context.

    Args:
        context: ToolContext with workspace_manager

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If workspace manager not available
    """
    if not context.has_workspace():
        raise ValueError("Claude Code tool requires workspace_manager in ToolContext")

    ws = context.workspace_manager

    @tool
    async def claude_code(
        prompt: str,
        session_id: Optional[str] = None,
        working_dir: Optional[str] = None,
    ) -> str:
        """Delegate a task to a Claude Code session.

        Spawns a Claude Code CLI session (using the host's auth) that can
        read/write files, run commands, search the web, and perform complex
        multi-step work. Returns the result text, session metadata, and a
        session_id for continuing the conversation.

        **Multi-turn sessions**: The first call starts a new session and
        returns a session_id. Pass that session_id on follow-up calls to
        resume the conversation — Claude Code remembers prior context.

        Use this for heavy work: writing long documents, complex research,
        multi-file edits, code generation. Provide clear, detailed instructions
        and review the results afterwards.

        Args:
            prompt: Detailed instructions for what Claude Code should do.
                    Be specific about expected output, file paths, and quality
                    requirements. When resuming a session, you can reference
                    prior context without repeating it.
            session_id: Resume a previous session. Pass the session_id from
                        a prior call to continue the conversation. Claude Code
                        retains full context from the previous turns.
                        Omit or pass None to start a new session.
            working_dir: Subdirectory within workspace to use as cwd (optional).
                         Relative to workspace root. Example: "repo" to work
                         in the cloned repository.

        Returns:
            Result text + session metadata (session_id, turns, cost, duration).
            Always includes session_id for resuming the conversation.

        Example:
            # Start a new session
            claude_code(prompt="Implement the auth module in src/auth.py. Follow patterns from src/users.py.", working_dir="repo")
            # Resume to iterate on the result
            claude_code(prompt="Add input validation to the login function and run pytest tests/test_auth.py", session_id="abc-123", working_dir="repo")
        """
        # Check CLI is available
        claude_bin = _find_claude_cli()
        if not claude_bin:
            return (
                "Error: Claude Code CLI not found in PATH. "
                "Install it from https://claude.ai/code and run `claude auth login`.\n"
                "The agent can still use other tools to complete this task directly."
            )

        # Resolve working directory (same security check as run_command)
        if working_dir:
            try:
                cwd = str(ws.get_path(working_dir))
            except (ValueError, PermissionError) as e:
                return f"Invalid working directory: {e}"
        else:
            cwd = str(ws.path)

        # Read config from agent YAML (claude_code section)
        cc_config = context.get_config("claude_code", {})
        model = cc_config.get("model", "claude-opus-4-6")
        effort_level = cc_config.get("effort_level", "high")

        # Build CLI command
        cmd = [
            claude_bin,
            "-p",  # Print mode (non-interactive)
            "--output-format", "json",
            "--model", model,
            "--dangerously-skip-permissions",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)

        logger.info(
            f"claude_code: delegating task (cwd={cwd}, model={model}, "
            f"session={'resume:' + session_id if session_id else 'new'}, "
            f"prompt_len={len(prompt)})"
        )

        # Environment: strip CLAUDECODE to avoid nesting guard, set effort level.
        # Strip ANTHROPIC_API_KEY so the CLI uses OAuth/Max plan auth
        # instead of API key billing from the agent's .env.
        env_override = {
            "CLAUDECODE": "",
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_EFFORT_LEVEL": effort_level,
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**__import__("os").environ, **env_override},
            )
            stdout_bytes, stderr_bytes = await proc.communicate()

        except FileNotFoundError:
            return (
                "Error: Claude Code CLI not found. "
                "Ensure 'claude' is installed and in PATH."
            )
        except Exception as e:
            error_type = type(e).__name__
            msg = f"Claude Code session failed ({error_type}): {e}"
            logger.error(msg)
            return msg

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # Parse JSON result
        result_data = None
        if stdout.strip():
            try:
                result_data = json.loads(stdout)
            except json.JSONDecodeError:
                logger.warning("claude_code: failed to parse JSON output, returning raw")

        # Build result from parsed JSON
        if result_data:
            output_parts = []

            # Main result text
            result_text = result_data.get("result", "")
            if result_text:
                result_text = _truncate_output(result_text, MAX_OUTPUT_CHARS, "response")
                output_parts.append(result_text)

            # Session metadata
            meta_parts = []
            resp_session_id = result_data.get("session_id", "")
            if resp_session_id:
                meta_parts.append(f"session_id: {resp_session_id}")
            if result_data.get("num_turns"):
                meta_parts.append(f"turns: {result_data['num_turns']}")
            if result_data.get("total_cost_usd") is not None:
                meta_parts.append(f"cost: ${result_data['total_cost_usd']:.4f}")
            if result_data.get("duration_ms"):
                duration_s = result_data["duration_ms"] / 1000
                meta_parts.append(f"duration: {duration_s:.1f}s")
            if result_data.get("is_error"):
                meta_parts.append("status: ERROR")
            if meta_parts:
                output_parts.append(f"\n[Session: {', '.join(meta_parts)}]")

            if output_parts:
                result = "\n".join(output_parts)
                logger.info(
                    f"claude_code: completed "
                    f"(turns={result_data.get('num_turns', '?')}, "
                    f"session_id={resp_session_id or 'none'}, "
                    f"output_len={len(result)})"
                )
                return result

        # Fallback: return raw stdout/stderr if JSON parsing failed
        if stdout.strip():
            return _truncate_output(stdout, MAX_OUTPUT_CHARS, "stdout")

        if stderr.strip():
            return f"Claude Code produced no stdout. Stderr:\n{_truncate_output(stderr, MAX_OUTPUT_CHARS, 'stderr')}"

        if proc.returncode != 0:
            return f"Claude Code exited with code {proc.returncode} and no output."

        return "(Claude Code session completed with no output)"

    return [claude_code]
