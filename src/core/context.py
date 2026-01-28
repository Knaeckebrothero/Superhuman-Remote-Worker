"""Context management for Universal Agent.

Implements context compaction and summarization strategies to keep
the conversation lean while preserving agent effectiveness.

Key strategies (in order of preference):
1. Tool result clearing - Replace old tool results with placeholders
2. Message trimming - Keep recent messages, trim older ones
3. Summarization - Use LLM to compress history when needed

References:
- Anthropic: "one of the safest, lightest touch forms of compaction"
- Phil Schmid: Context Engineering Part 2
- LangGraph: Manage Conversation History
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any, Callable, Dict, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ConversationSummary(BaseModel):
    """Structured summary — forces the model to stop after valid JSON."""
    summary: str = Field(description="General overview of the conversation and what happened")
    tasks_completed: str = Field(description="Bullet-point list of completed tasks")
    key_decisions: str = Field(description="Important decisions made")
    current_state: str = Field(description="Current progress and immediate next steps")
    blockers: str = Field(default="", description="Errors or blockers encountered, empty if none")


def find_safe_slice_start(messages: List[BaseMessage], target_start: int) -> int:
    """Find a safe starting index that doesn't orphan ToolMessages.

    When slicing messages, we must ensure that if we include a ToolMessage,
    we also include its corresponding AIMessage with the tool_call.
    This function adjusts the start index backwards if needed.

    Args:
        messages: Full message list
        target_start: Desired start index

    Returns:
        Adjusted start index that won't orphan ToolMessages
    """
    if target_start <= 0:
        return 0

    if target_start >= len(messages):
        return len(messages)

    # If the message at target_start is a ToolMessage, we need to find
    # the preceding AIMessage that contains the tool_call
    adjusted_start = target_start

    # Check if we're starting at or near ToolMessages that would be orphaned
    # Walk backwards to find a safe boundary
    while adjusted_start > 0:
        msg = messages[adjusted_start]

        if isinstance(msg, ToolMessage):
            # This ToolMessage needs its parent AIMessage
            # Look backwards for the AIMessage with matching tool_call
            tool_call_id = getattr(msg, 'tool_call_id', None)

            if tool_call_id:
                for i in range(adjusted_start - 1, -1, -1):
                    prev_msg = messages[i]
                    if isinstance(prev_msg, AIMessage):
                        if hasattr(prev_msg, 'tool_calls') and prev_msg.tool_calls:
                            # Check if this AIMessage has the matching tool_call
                            for tc in prev_msg.tool_calls:
                                if tc.get('id') == tool_call_id:
                                    # Found the parent - start from here
                                    adjusted_start = i
                                    break
                            else:
                                continue
                            break
                        # AIMessage without tool_calls - safe boundary
                        break
                    elif isinstance(prev_msg, HumanMessage):
                        # Human message - safe boundary
                        break
                else:
                    # Couldn't find parent, start from beginning
                    adjusted_start = 0
            break
        elif isinstance(msg, AIMessage):
            # Starting at an AIMessage is safe
            break
        elif isinstance(msg, HumanMessage):
            # Starting at a HumanMessage is safe
            break
        else:
            # Other message types - check the previous one
            adjusted_start -= 1

    if adjusted_start != target_start:
        logger.debug(
            f"Adjusted slice start from {target_start} to {adjusted_start} "
            "to preserve tool call pairs"
        )

    return adjusted_start


def sanitize_message_history(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Remove orphaned ToolMessages that lack a parent AIMessage with tool_calls.

    This function repairs corrupted message histories where a ToolMessage
    appears without a preceding AIMessage that made the corresponding tool call.
    Such corruption can occur from improper message slicing during context compaction.

    Args:
        messages: Message list that may contain orphaned ToolMessages

    Returns:
        Sanitized message list with orphaned ToolMessages removed
    """
    if not messages:
        return messages

    # Build a set of valid tool_call_ids from AIMessages
    valid_tool_call_ids = set()
    for msg in messages:
        if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
            for tc in msg.tool_calls:
                tc_id = tc.get('id')
                if tc_id:
                    valid_tool_call_ids.add(tc_id)

    # Filter out orphaned ToolMessages
    result = []
    orphaned_count = 0
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tool_call_id = getattr(msg, 'tool_call_id', None)
            if tool_call_id and tool_call_id not in valid_tool_call_ids:
                # This ToolMessage is orphaned - skip it
                orphaned_count += 1
                continue
        result.append(msg)

    if orphaned_count > 0:
        logger.warning(
            f"Removed {orphaned_count} orphaned ToolMessages from message history"
        )

    return result


# Try to import tiktoken for accurate token counting
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.warning("tiktoken not available, using approximate token counting")


@dataclass
class ContextManagementState:
    """State for context management tracking.

    Stored in agent metadata to track context management operations.
    """
    total_tool_results_cleared: int = 0
    total_messages_trimmed: int = 0
    total_summarizations: int = 0
    current_token_count: int = 0
    summaries: List[str] = field(default_factory=list)
    last_compaction_iteration: int = 0


@dataclass
class ContextConfig:
    """Configuration for context management.

    Attributes:
        compaction_threshold_tokens: Trigger compaction when context exceeds this
        summarization_threshold_tokens: Trigger summarization when exceeds this
        message_count_threshold: Message count threshold for alternate summarization trigger
        message_count_min_tokens: Minimum tokens required when using message count threshold
        keep_recent_tool_results: Number of recent tool results to keep in full
        keep_recent_messages: Number of recent messages to preserve
        max_tool_result_length: Max chars for truncated tool results
        placeholder_text: Text to use when replacing cleared tool results
        tool_retry_count: Number of retries for failed tool calls
        tool_retry_delay_seconds: Delay between retries
    """
    compaction_threshold_tokens: int = 100_000
    summarization_threshold_tokens: int = 100_000
    message_count_threshold: int = 200
    message_count_min_tokens: int = 30_000
    keep_recent_tool_results: int = 10
    keep_recent_messages: int = 10
    max_tool_result_length: int = 5000
    placeholder_text: str = "[Result processed - see workspace if needed]"
    tool_retry_count: int = 3
    tool_retry_delay_seconds: float = 1.0


def count_tokens_tiktoken(messages: List[BaseMessage], model: str = "gpt-4") -> int:
    """Count tokens using tiktoken for accurate counting.

    Args:
        messages: List of messages to count
        model: Model name for tokenizer selection

    Returns:
        Token count
    """
    if not TIKTOKEN_AVAILABLE:
        return count_tokens_approximate(messages)

    try:
        # Try to get encoding for specific model
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            # Fall back to cl100k_base (used by GPT-4)
            enc = tiktoken.get_encoding("cl100k_base")

        total = 0
        for msg in messages:
            # Count message content
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            total += len(enc.encode(content))

            # Count tool calls if present
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                total += len(enc.encode(str(msg.tool_calls)))

            # Add overhead for message structure (role, etc.)
            total += 4  # Approximate overhead per message

        return total

    except Exception as e:
        logger.warning(f"tiktoken error, falling back to approximate: {e}")
        return count_tokens_approximate(messages)


def count_tokens_approximate(messages: List[BaseMessage]) -> int:
    """Approximate token count using character-based estimation.

    Uses ~4 characters per token as a rough estimate.
    This is a fallback when tiktoken is not available.

    Args:
        messages: List of messages to count

    Returns:
        Approximate token count
    """
    total_chars = 0
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        total_chars += len(content)

        # Add tool calls if present
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            total_chars += len(str(msg.tool_calls))

    # ~4 chars per token on average
    return total_chars // 4


def get_token_counter(model: str = "gpt-4") -> Callable[[List[BaseMessage]], int]:
    """Get the appropriate token counter function.

    Args:
        model: Model name for tokenizer selection

    Returns:
        Token counter function
    """
    if TIKTOKEN_AVAILABLE:
        return lambda msgs: count_tokens_tiktoken(msgs, model)
    return count_tokens_approximate


class ContextManager:
    """Manages context window for the Universal Agent.

    Implements a multi-tier context management strategy:
    1. Tool result clearing (lowest impact, highest benefit)
    2. Message trimming (moderate impact)
    3. LLM summarization (highest impact, preserves meaning)

    Example:
        ```python
        config = ContextConfig(
            compaction_threshold_tokens=80000,
            keep_recent_tool_results=5,
        )
        context_mgr = ContextManager(config=config)

        # In graph process node
        prepared = context_mgr.prepare_messages_for_llm(messages)
        response = llm.invoke(prepared)

        # Check if summarization needed
        if context_mgr.should_summarize(messages):
            messages = await context_mgr.summarize_and_compact(messages, llm)
        ```
    """

    def __init__(
        self,
        config: Optional[ContextConfig] = None,
        model: str = "gpt-4",
    ):
        """Initialize context manager.

        Args:
            config: Context management configuration
            model: Model name for token counting
        """
        self.config = config or ContextConfig()
        self.token_counter = get_token_counter(model)
        self._state = ContextManagementState()

    @property
    def state(self) -> ContextManagementState:
        """Get current context management state."""
        return self._state

    def get_token_count(self, messages: List[BaseMessage]) -> int:
        """Get current token count for messages.

        Args:
            messages: Messages to count

        Returns:
            Token count
        """
        count = self.token_counter(messages)
        self._state.current_token_count = count
        return count

    def should_compact(self, messages: List[BaseMessage]) -> bool:
        """Check if context needs compaction.

        Args:
            messages: Current message history

        Returns:
            True if compaction threshold exceeded
        """
        return self.get_token_count(messages) > self.config.compaction_threshold_tokens

    def should_summarize(self, messages: List[BaseMessage]) -> bool:
        """Check if summarization is needed.

        Summarization is triggered when:
        1. Token count exceeds summarization_threshold_tokens, OR
        2. Message count exceeds message_count_threshold AND
           token count exceeds message_count_min_tokens

        Args:
            messages: Current message history

        Returns:
            True if summarization threshold exceeded
        """
        token_count = self.get_token_count(messages)
        message_count = len(messages)

        # Original threshold: high token count
        if token_count > self.config.summarization_threshold_tokens:
            return True

        # New: message count threshold with minimum token requirement
        if (message_count > self.config.message_count_threshold and
                token_count > self.config.message_count_min_tokens):
            return True

        return False

    def clear_old_tool_results(
        self,
        messages: List[BaseMessage],
        keep_recent: Optional[int] = None,
    ) -> List[BaseMessage]:
        """Replace old tool results with placeholder text.

        This is the "safest, lightest touch form of compaction" per Anthropic.
        The agent can always re-read files from workspace if needed.

        Args:
            messages: Message list to process
            keep_recent: Number of recent tool results to keep (default from config)

        Returns:
            Processed message list with old tool results replaced
        """
        keep_recent = keep_recent or self.config.keep_recent_tool_results

        # Count tool messages from the end
        tool_indices = [
            i for i, m in enumerate(messages)
            if isinstance(m, ToolMessage)
        ]

        if not tool_indices:
            return messages

        # Determine which tool messages to clear
        num_to_clear = max(0, len(tool_indices) - keep_recent)
        indices_to_clear = set(tool_indices[:num_to_clear])

        result = []
        cleared_count = 0

        for i, msg in enumerate(messages):
            if i in indices_to_clear:
                # Replace with placeholder
                result.append(
                    ToolMessage(
                        content=self.config.placeholder_text,
                        tool_call_id=msg.tool_call_id,
                    )
                )
                cleared_count += 1
            else:
                result.append(msg)

        if cleared_count > 0:
            self._state.total_tool_results_cleared += cleared_count
            logger.debug(f"Cleared {cleared_count} old tool results")

        return result

    def truncate_long_tool_results(
        self,
        messages: List[BaseMessage],
        max_length: Optional[int] = None,
        keep_recent: Optional[int] = None,
    ) -> List[BaseMessage]:
        """Truncate tool results that exceed max length.

        Only truncates older results; recent ones are kept in full.

        Args:
            messages: Message list to process
            max_length: Max chars for tool results (default from config)
            keep_recent: Number of recent results to keep in full

        Returns:
            Processed message list with truncated results
        """
        max_length = max_length or self.config.max_tool_result_length
        keep_recent = keep_recent or self.config.keep_recent_tool_results

        # Count tool messages from the end
        tool_indices = [
            i for i, m in enumerate(messages)
            if isinstance(m, ToolMessage)
        ]

        if not tool_indices:
            return messages

        # Recent tool messages (don't truncate)
        recent_indices = set(tool_indices[-keep_recent:]) if keep_recent else set()

        result = []
        for i, msg in enumerate(messages):
            if isinstance(msg, ToolMessage) and i not in recent_indices:
                if len(msg.content) > max_length:
                    truncated = (
                        msg.content[:max_length] +
                        f"\n\n[TRUNCATED - {len(msg.content) - max_length} chars omitted, see workspace]"
                    )
                    result.append(
                        ToolMessage(
                            content=truncated,
                            tool_call_id=msg.tool_call_id,
                        )
                    )
                else:
                    result.append(msg)
            else:
                result.append(msg)

        return result

    def prepare_messages_for_llm(
        self,
        messages: List[BaseMessage],
        aggressive: bool = False,
    ) -> List[BaseMessage]:
        """Prepare messages for LLM by applying context management.

        Applies the following in order:
        1. Clear old tool results (if aggressive or above threshold)
        2. Truncate long tool results
        3. Trim messages if still over threshold

        Args:
            messages: Original message list
            aggressive: If True, clear more aggressively

        Returns:
            Processed message list ready for LLM
        """
        if not messages:
            return messages

        token_count = self.get_token_count(messages)
        should_be_aggressive = aggressive or token_count > self.config.compaction_threshold_tokens

        # Step 1: Clear old tool results
        if should_be_aggressive:
            messages = self.clear_old_tool_results(messages)

        # Step 2: Truncate long results in remaining messages
        messages = self.truncate_long_tool_results(messages)

        # Step 3: If STILL above threshold, trim messages
        new_token_count = self.get_token_count(messages)
        if new_token_count > self.config.compaction_threshold_tokens:
            logger.warning(
                f"Context still at {new_token_count} tokens after tool compaction, "
                f"trimming messages (threshold: {self.config.compaction_threshold_tokens})"
            )
            messages = self.trim_messages(messages)

        return messages

    def trim_messages(
        self,
        messages: List[BaseMessage],
        keep_recent: Optional[int] = None,
    ) -> List[BaseMessage]:
        """Trim messages to keep only recent ones.

        Preserves (never trimmed - implements Layers 1-3 protection):
        - All system messages (Layer 1: system prompt, Layer 2: todo list)
        - The first human message (original task)
        - Recent conversation messages

        Note: Layer 2 (todo list with visual separators) is injected fresh
        AFTER this method is called in graph.py, so it's never subject to
        trimming anyway. This method preserves any SystemMessages that might
        be in the message history.

        Args:
            messages: Message list to trim
            keep_recent: Number of recent messages to keep

        Returns:
            Trimmed message list
        """
        keep_recent = keep_recent or self.config.keep_recent_messages

        # Separate system messages
        system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
        conversation = [m for m in messages if not isinstance(m, SystemMessage)]

        if len(conversation) <= keep_recent:
            return messages

        # Keep first human message (original task) and recent messages
        first_human_idx = next(
            (i for i, m in enumerate(conversation) if isinstance(m, HumanMessage)),
            None
        )

        trimmed_conversation = []
        if first_human_idx is not None and first_human_idx < len(conversation) - keep_recent:
            trimmed_conversation = [conversation[first_human_idx]]

        # Add recent messages, ensuring we don't orphan ToolMessages
        target_start = len(conversation) - keep_recent
        safe_start = find_safe_slice_start(conversation, target_start)
        trimmed_conversation.extend(conversation[safe_start:])

        trimmed_count = len(conversation) - len(trimmed_conversation)
        if trimmed_count > 0:
            self._state.total_messages_trimmed += trimmed_count
            logger.info(f"Trimmed {trimmed_count} old messages")

        return system_msgs + trimmed_conversation

    async def ensure_within_limits(
        self,
        messages: List[BaseMessage],
        llm: BaseChatModel,
        summarization_prompt: Optional[str] = None,
        oss_reasoning_level: str = "high",
        max_summary_length: int = 10000,
        force: bool = False,
    ) -> List[BaseMessage]:
        """Ensure messages are within configured limits, summarizing if needed.

        This is the single entry point for context compaction. Call this before
        LLM requests to guarantee context stays within bounds.

        Args:
            messages: Current message history
            llm: LLM for summarization
            summarization_prompt: Optional custom prompt
            oss_reasoning_level: Reasoning level for OSS models
            max_summary_length: Max length for summary
            force: If True, summarize even if thresholds not exceeded

        Returns:
            Messages (possibly compacted) guaranteed to be within limits
        """
        if force or self.should_summarize(messages):
            logger.info(
                f"Context compaction triggered: {len(messages)} messages, "
                f"{self.get_token_count(messages)} tokens"
            )
            return await self.summarize_and_compact(
                messages,
                llm,
                summarization_prompt,
                oss_reasoning_level,
                max_summary_length,
            )
        return messages

    async def summarize_conversation(
        self,
        messages: List[BaseMessage],
        llm: BaseChatModel,
        summarization_prompt: Optional[str] = None,
        oss_reasoning_level: str = "high",
        max_summary_length: int = 10000,
    ) -> str:
        """Generate a summary of the conversation.

        Args:
            messages: Messages to summarize
            llm: LLM to use for summarization
            summarization_prompt: Optional custom prompt
            oss_reasoning_level: Reasoning level for OSS models (low/medium/high)

        Returns:
            Summary string
        """
        # Format messages for summarization
        formatted_parts = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                continue  # Skip system messages
            elif isinstance(msg, HumanMessage):
                formatted_parts.append(f"User: {msg.content[:500]}")
            elif isinstance(msg, AIMessage):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    tool_names = [tc.get("name", "unknown") for tc in msg.tool_calls]
                    formatted_parts.append(f"Assistant: [Called tools: {', '.join(tool_names)}]")
                elif content:
                    formatted_parts.append(f"Assistant: {content[:300]}...")
            elif isinstance(msg, ToolMessage):
                # Just note that a tool was called, don't include full result
                formatted_parts.append(f"[Tool result: {len(msg.content)} chars]")

        # Include all formatted parts for complete context
        conversation_text = "\n".join(formatted_parts)

        # Build prompt from template or fallback
        if summarization_prompt:
            prompt = summarization_prompt.format(
                conversation=conversation_text,
                oss_reasoning_level=oss_reasoning_level,
                max_summary_length=max_summary_length,
            )
        else:
            prompt = f"""Summarize this agent conversation into the required JSON fields.

Conversation:
{conversation_text}"""

        try:
            structured_llm = llm.with_structured_output(ConversationSummary)

            logger.info("Starting structured summarization")
            result: ConversationSummary = await structured_llm.ainvoke([HumanMessage(content=prompt)])

            # Format into readable text
            parts = []
            if result.summary.strip():
                parts.append(f"**Summary:**\n{result.summary.strip()}")
            if result.tasks_completed.strip():
                parts.append(f"**Tasks Completed:**\n{result.tasks_completed.strip()}")
            if result.key_decisions.strip():
                parts.append(f"**Key Decisions:**\n{result.key_decisions.strip()}")
            if result.current_state.strip():
                parts.append(f"**Current State:**\n{result.current_state.strip()}")
            if result.blockers and result.blockers.strip():
                parts.append(f"**Blockers:**\n{result.blockers.strip()}")
            summary = "\n\n".join(parts)

            logger.info(f"Generated structured summary ({len(summary)} chars)")
            # Debug: log tail
            tail = summary[-500:] if len(summary) > 500 else summary
            logger.debug(f"Summary tail:\n{tail}")

            self._state.total_summarizations += 1
            self._state.summaries.append(summary)
            return summary

        except Exception as e:
            logger.error(f"Structured summarization failed: {e}", exc_info=True)
            return f"[Summarization failed: {e}]"

    async def summarize_and_compact(
        self,
        messages: List[BaseMessage],
        llm: BaseChatModel,
        summarization_prompt: Optional[str] = None,
        oss_reasoning_level: str = "high",
        max_summary_length: int = 10000,
    ) -> List[BaseMessage]:
        """Summarize older messages and compact the conversation.

        This is the most aggressive context management strategy.
        Used when other strategies aren't sufficient.

        Args:
            messages: Full message history
            llm: LLM for summarization
            summarization_prompt: Optional custom prompt
            oss_reasoning_level: Reasoning level for OSS models (low/medium/high)

        Returns:
            Compacted message list with summary prepended
        """
        # Separate system messages
        system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
        conversation = [m for m in messages if not isinstance(m, SystemMessage)]

        if len(conversation) <= self.config.keep_recent_messages:
            return messages

        # Find safe slice point that doesn't orphan ToolMessages
        target_start = len(conversation) - self.config.keep_recent_messages
        safe_start = find_safe_slice_start(conversation, target_start)

        # Messages to summarize (older ones) and recent messages to keep
        messages_to_summarize = conversation[:safe_start]
        recent_messages = conversation[safe_start:]

        # Generate summary
        summary = await self.summarize_conversation(
            messages_to_summarize,
            llm,
            summarization_prompt,
            oss_reasoning_level,
            max_summary_length,
        )

        # Guard: if summary is larger than what we're replacing, skip compaction
        summary_tokens = self.get_token_count([SystemMessage(content=summary)])
        original_tokens = self.get_token_count(messages_to_summarize)
        if summary_tokens > original_tokens:
            logger.error(
                f"Summary ({summary_tokens} tokens) larger than original ({original_tokens} tokens) — skipping compaction"
            )
            return messages

        # Create summary as HumanMessage so it appears in conversation
        summary_msg = HumanMessage(
            content=f"[Summary of prior work]\n{summary}"
        )

        # Generate removal markers for all messages being summarized
        # The add_messages reducer will remove these from state
        removal_markers = []
        for msg in messages_to_summarize:
            if hasattr(msg, 'id') and msg.id:
                removal_markers.append(RemoveMessage(id=msg.id))

        logger.info(
            f"Compacted {len(messages)} messages to {len(system_msgs) + 1 + len(recent_messages)} "
            f"(summarized {len(messages_to_summarize)} messages, removing {len(removal_markers)})"
        )

        # Return: removal markers + system messages + summary + recent
        return removal_markers + system_msgs + [summary_msg] + recent_messages

    def create_pre_model_hook(self) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        """Create a pre-model hook for LangGraph integration.

        The hook intercepts state before each LLM call and applies
        context management as needed.

        Returns:
            Callable compatible with LangGraph pre_model_hook
        """
        def pre_model_hook(state: Dict[str, Any]) -> Dict[str, Any]:
            messages = state.get("messages", [])

            # Apply context management
            prepared = self.prepare_messages_for_llm(messages)

            # Log if significant compaction occurred
            if len(prepared) < len(messages):
                logger.debug(
                    f"Pre-model hook: {len(messages)} -> {len(prepared)} messages"
                )

            return {"llm_input_messages": prepared}

        return pre_model_hook


class ToolRetryManager:
    """Manages retry logic for tool execution.

    Implements exponential backoff with configurable retry count.
    Tracks failures per tool for monitoring.
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ):
        """Initialize retry manager.

        Args:
            max_retries: Maximum number of retry attempts
            base_delay: Base delay between retries in seconds
            max_delay: Maximum delay between retries
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._failure_counts: Dict[str, int] = {}
        self._total_retries = 0

    def get_retry_delay(self, attempt: int) -> float:
        """Calculate delay for a given retry attempt.

        Uses exponential backoff with jitter.

        Args:
            attempt: Current attempt number (0-indexed)

        Returns:
            Delay in seconds
        """
        import random

        delay = self.base_delay * (2 ** attempt)
        delay = min(delay, self.max_delay)
        # Add 10% jitter
        jitter = delay * 0.1 * random.random()
        return delay + jitter

    def should_retry(self, tool_name: str, attempt: int) -> bool:
        """Check if a tool call should be retried.

        Args:
            tool_name: Name of the tool that failed
            attempt: Current attempt number

        Returns:
            True if retry should be attempted
        """
        return attempt < self.max_retries

    def record_failure(self, tool_name: str) -> int:
        """Record a tool failure.

        Args:
            tool_name: Name of the tool that failed

        Returns:
            Total failures for this tool
        """
        self._failure_counts[tool_name] = self._failure_counts.get(tool_name, 0) + 1
        return self._failure_counts[tool_name]

    def record_retry(self) -> None:
        """Record that a retry was attempted."""
        self._total_retries += 1

    def get_stats(self) -> Dict[str, Any]:
        """Get retry statistics.

        Returns:
            Dict with failure counts and total retries
        """
        return {
            "failure_counts": self._failure_counts.copy(),
            "total_retries": self._total_retries,
        }


async def write_error_to_workspace(
    workspace_manager: Any,
    error: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """Write error details to workspace for debugging.

    Creates an error report in the workspace output folder.

    Args:
        workspace_manager: Workspace manager instance
        error: Error details dict
        context: Optional additional context

    Returns:
        Path to error file
    """
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    error_path = f"output/error_{timestamp}.md"

    error_content = f"""# Error Report

**Timestamp:** {datetime.now(UTC).isoformat()}
**Error Type:** {error.get('type', 'unknown')}
**Recoverable:** {error.get('recoverable', False)}

## Error Message

{error.get('message', 'No message provided')}

## Stack Trace

```
{error.get('traceback', 'No traceback available')}
```

## Context

"""

    if context:
        for key, value in context.items():
            error_content += f"- **{key}:** {value}\n"
    else:
        error_content += "No additional context available.\n"

    error_content += """

## Recovery Suggestions

1. Check the workspace files for partial results
2. Review the todo list for completed vs pending items
3. Check the archive folder for completed phase summaries
4. Review the error message for actionable information
"""

    try:
        await workspace_manager.write_file(error_path, error_content)
        logger.info(f"Error report written to {error_path}")
        return error_path
    except Exception as e:
        logger.error(f"Failed to write error report: {e}")
        return ""
