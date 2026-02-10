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

BUILDER_TOOLS = [
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
    return os.getenv("BUILDER_MODEL", "gpt-4o-mini")


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
