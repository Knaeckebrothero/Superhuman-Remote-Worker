"""Artifact tool schemas and auto-summarization for the instruction builder.

The builder LLM receives these as tool/function definitions. When it calls them,
the backend emits SSE `tool_call` events that the frontend applies to the job form.
"""

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Artifact Tool Definitions (OpenAI function-calling format)
# =============================================================================

# Tools that are executed server-side (not forwarded to the frontend as artifact mutations)
SERVER_SIDE_TOOLS = {
    "web_search",
    "list_jobs",
    "get_job",
    "get_job_progress",
    "get_workspace_file",
    "get_workspace_overview",
    "get_frozen_job",
    "get_todos",
    "get_chat_history",
    "approve_job",
    "resume_job_with_feedback",
    "create_follow_up_job",
}

BUILDER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web to research a topic before writing instructions. "
                "Use this to learn about domain best practices, methodologies, and pitfalls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Specific search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (1-10, default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_instructions",
            "description": (
                "Replace the full instructions content. Use this when making large changes "
                "or when starting fresh. The entire instructions.md will be overwritten."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The complete new instructions content (markdown)",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_instructions",
            "description": (
                "Find and replace text within the instructions. Use this for targeted edits "
                "like fixing a section, renaming terms, or adjusting specific parts. "
                "The old_text must match exactly (including whitespace)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find in the instructions",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text",
                    },
                },
                "required": ["old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_instructions",
            "description": (
                "Insert text at a specific line in the instructions, or append to the end. "
                "Use this for adding new sections or requirements without disturbing existing content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The text to insert (markdown)",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number to insert at (1-indexed). Omit to append to end.",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_config",
            "description": (
                "Update the agent configuration. Objects merge recursively, arrays replace entirely. "
                "Use this to change the model, temperature, reasoning level, or tool availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "llm": {
                        "type": "object",
                        "description": "LLM settings to update",
                        "properties": {
                            "model": {"type": "string", "description": "Model name (e.g. 'gpt-4o', 'claude-sonnet-4-5-20250929')"},
                            "temperature": {"type": "number", "description": "Temperature (0.0 to 2.0)"},
                            "reasoning_level": {"type": "string", "description": "Reasoning level (low, medium, high)"},
                            "strategic": {
                                "type": "object",
                                "description": "Overrides for strategic (planning) phases",
                                "properties": {
                                    "model": {"type": "string"},
                                    "temperature": {"type": "number"},
                                    "reasoning_level": {"type": "string", "enum": ["low", "medium", "high"]},
                                },
                            },
                            "tactical": {
                                "type": "object",
                                "description": "Overrides for tactical (execution) phases",
                                "properties": {
                                    "model": {"type": "string"},
                                    "temperature": {"type": "number"},
                                    "reasoning_level": {"type": "string", "enum": ["low", "medium", "high"]},
                                },
                            },
                        },
                    },
                    "tools": {
                        "type": "object",
                        "description": "Tool category overrides. Set a category to [] to disable it.",
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_description",
            "description": (
                "Replace the job description. Use this when the user wants to change "
                "what the agent should accomplish."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The new job description",
                    },
                },
                "required": ["content"],
            },
        },
    },
    # -----------------------------------------------------------------
    # Job Inspection & Action Tools (server-side, loopback to orchestrator API)
    # -----------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "list_jobs",
            "description": (
                "List recent jobs. Use this when the user asks about their jobs, "
                "recent work, or wants to find a specific job."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["created", "queued", "running", "completed", "failed", "cancelled", "pending_review"],
                        "description": "Filter by job status",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max jobs to return (default 10)",
                        "default": 10,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job",
            "description": (
                "Get detailed information about a specific job including its status, "
                "config, description, and timestamps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_progress",
            "description": (
                "Get detailed progress for a job including phase, todo completion, "
                "and timing information."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace_file",
            "description": (
                "Read a file from a job's workspace. Common files: workspace.md (agent memory), "
                "plan.md (strategic plan), todos.yaml (current tasks), "
                "archive/phase_N_retrospective.md (phase reviews)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative file path (e.g. 'workspace.md', 'plan.md', 'archive/phase_1_retrospective.md')",
                    },
                },
                "required": ["job_id", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace_overview",
            "description": (
                "Get a high-level overview of a job's workspace including file listing, "
                "truncated workspace.md/plan.md content, current todos, and archive count."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_frozen_job",
            "description": (
                "Get the frozen job data (summary, deliverables, confidence) for a job "
                "that is pending review. Use this to understand what the agent produced."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_todos",
            "description": (
                "Get all todos for a job including current active todos and archived phases. "
                "Shows task planning and execution progress."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chat_history",
            "description": (
                "Get the agent's conversation history for a job. Shows LLM input/response pairs. "
                "Use page=-1 to get the most recent messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (1-indexed, -1 for last page)",
                        "default": -1,
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Entries per page (default 10)",
                        "default": 10,
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_job",
            "description": (
                "Approve a frozen job that is pending review. This marks the job as completed. "
                "Only works on jobs with status 'pending_review'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID to approve",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_job_with_feedback",
            "description": (
                "Resume a failed or frozen job, optionally with feedback for the agent. "
                "The feedback is injected into the agent's context before resuming."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID to resume",
                    },
                    "feedback": {
                        "type": "string",
                        "description": "Optional feedback or instructions for the agent",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_follow_up_job",
            "description": (
                "Create a new job. Use this to start follow-up work or create jobs based on "
                "the user's request. Returns the new job ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What the agent should accomplish",
                    },
                    "config_name": {
                        "type": "string",
                        "description": "Agent config to use (e.g. 'default', 'scholar', 'developer')",
                        "default": "default",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Additional instructions for the agent",
                    },
                },
                "required": ["description"],
            },
        },
    },
]


# =============================================================================
# Auto-Summarization
# =============================================================================

SUMMARY_SYSTEM_PROMPT = """You are a conversation summarizer. Condense the following chat messages into a brief summary that preserves:
- Key decisions made about the instructions/configuration
- Important context the user provided about their use case
- Any constraints or preferences expressed

Be concise (2-4 paragraphs). Focus on information the AI will need to continue the conversation coherently."""


def estimate_token_count(text: str) -> int:
    """Rough token estimate (4 chars per token)."""
    return len(text) // 4


def build_message_context(
    messages: list[dict[str, Any]],
    summary: str | None = None,
    max_context_tokens: int = 6000,
) -> tuple[list[dict[str, str]], bool]:
    """Build conversation context from messages, respecting token budget.

    Returns a tuple of (context_messages, needs_summarization).
    If the messages exceed the budget, returns only recent ones and signals
    that older messages should be summarized.

    Args:
        messages: All session messages in chronological order
        summary: Existing summary of older messages (if any)
        max_context_tokens: Token budget for conversation history

    Returns:
        Tuple of (messages for LLM context, whether summarization is needed)
    """
    context: list[dict[str, str]] = []
    needs_summarization = False

    # Start with summary if available
    if summary:
        context.append({
            "role": "system",
            "content": f"Summary of earlier conversation:\n{summary}",
        })

    # Calculate total token cost of all messages
    total_tokens = sum(
        estimate_token_count(m.get("content") or "") + estimate_token_count(
            json.dumps(m.get("tool_calls") or [])
        )
        for m in messages
    )

    if total_tokens <= max_context_tokens:
        # All messages fit
        for m in messages:
            content = m.get("content") or ""
            if m.get("tool_calls"):
                content += f"\n[Tool calls: {json.dumps(m['tool_calls'])}]"
            context.append({"role": m["role"], "content": content})
    else:
        # Need to trim — keep recent messages, signal summarization needed
        needs_summarization = True
        running_tokens = 0
        recent: list[dict[str, str]] = []

        for m in reversed(messages):
            msg_tokens = estimate_token_count(m.get("content") or "") + estimate_token_count(
                json.dumps(m.get("tool_calls") or [])
            )
            if running_tokens + msg_tokens > max_context_tokens:
                break
            content = m.get("content") or ""
            if m.get("tool_calls"):
                content += f"\n[Tool calls: {json.dumps(m['tool_calls'])}]"
            recent.append({"role": m["role"], "content": content})
            running_tokens += msg_tokens

        context.extend(reversed(recent))

    return context, needs_summarization


def build_summarization_prompt(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build a prompt to summarize older messages.

    Args:
        messages: Messages to summarize

    Returns:
        Messages formatted for the summarization LLM call
    """
    conversation_text = "\n".join(
        f"[{m['role']}]: {m.get('content') or ''}"
        + (f" [tools: {json.dumps(m.get('tool_calls') or [])}]" if m.get("tool_calls") else "")
        for m in messages
    )

    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": conversation_text},
    ]


# =============================================================================
# LLM Provider Configuration
# =============================================================================

def get_builder_model() -> str:
    """Get the model name for the builder LLM."""
    return os.getenv("BUILDER_MODEL", "gpt-5.2-pro")


def get_builder_api_key() -> str | None:
    """Get the API key for the builder LLM.

    Falls back to OPENAI_API_KEY or ANTHROPIC_API_KEY based on provider.
    """
    explicit = os.getenv("BUILDER_API_KEY")
    if explicit:
        return explicit
    provider = get_builder_provider()
    if provider == "anthropic":
        return os.getenv("ANTHROPIC_API_KEY")
    return os.getenv("OPENAI_API_KEY")


def get_builder_base_url() -> str | None:
    """Get the base URL for the builder LLM.

    Falls back to OPENAI_BASE_URL for OpenAI-compatible providers.
    Anthropic doesn't use a base URL override.
    """
    explicit = os.getenv("BUILDER_BASE_URL")
    if explicit:
        return explicit
    provider = get_builder_provider()
    if provider == "anthropic":
        return None
    return os.getenv("OPENAI_BASE_URL")


def get_builder_provider() -> str:
    """Detect the LLM provider for the builder.

    If BUILDER_LLM_PROVIDER is set, use that.
    Otherwise auto-detect from model name.
    """
    explicit = os.getenv("BUILDER_LLM_PROVIDER")
    if explicit:
        return explicit.lower()

    model = get_builder_model()
    if model.startswith("claude-"):
        return "anthropic"
    return "openai"
