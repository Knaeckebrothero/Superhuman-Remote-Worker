"""Context window management for long-running agents.

This module provides utilities for managing LLM context windows during extended
agent operations. It implements the pre_model_hook pattern from LangGraph to
automatically trim and summarize conversation history when it exceeds thresholds.
"""

import logging
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage
from langchain_core.messages.utils import trim_messages

logger = logging.getLogger(__name__)


def count_tokens_approximately(messages: List[BaseMessage]) -> int:
    """Approximate token count for a list of messages.

    Uses a simple character-based estimation (4 chars per token average).
    For more accurate counts, use tiktoken with the specific model.

    Args:
        messages: List of LangChain messages

    Returns:
        Approximate token count
    """
    total_chars = sum(len(m.content) if isinstance(m.content, str) else 0 for m in messages)
    return total_chars // 4


@dataclass
class ContextConfig:
    """Configuration for context window management."""
    compaction_threshold_tokens: int = 100_000
    summarization_trigger_tokens: int = 128_000
    keep_raw_turns: int = 3
    max_output_tokens: int = 80_000


class ContextManager:
    """Manages context window for long-running agents.

    Implements intelligent context trimming and summarization to prevent
    context window overflow during extended agent operations.

    The manager uses a pre_model_hook pattern compatible with LangGraph
    to intercept messages before they're sent to the LLM.

    Example:
        ```python
        context_mgr = ContextManager(config=ContextConfig())

        # Create the agent graph with the pre-model hook
        graph = StateGraph(AgentState)
        agent = create_react_agent(
            llm,
            tools,
            pre_model_hook=context_mgr.create_pre_model_hook()
        )
        ```
    """

    def __init__(
        self,
        config: Optional[ContextConfig] = None,
        workspace: Optional[Any] = None,
        token_counter: Optional[Callable[[List[BaseMessage]], int]] = None
    ):
        """Initialize the context manager.

        Args:
            config: Context management configuration
            workspace: Optional workspace for storing summaries
            token_counter: Optional custom token counting function
        """
        self.config = config or ContextConfig()
        self.workspace = workspace
        self.token_counter = token_counter or count_tokens_approximately
        self._compaction_count = 0

    def create_pre_model_hook(self) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        """Create a pre-model hook for LangGraph agents.

        The hook intercepts the agent state before each LLM call and
        trims the message history if it exceeds the compaction threshold.

        Returns:
            Callable that processes state and returns modified llm_input_messages
        """
        def pre_model_hook(state: Dict[str, Any]) -> Dict[str, Any]:
            messages = state.get("messages", [])
            token_count = self.token_counter(messages)

            if token_count > self.config.compaction_threshold_tokens:
                logger.info(
                    f"Context compaction triggered: {token_count} tokens "
                    f"(threshold: {self.config.compaction_threshold_tokens})"
                )

                trimmed = self._trim_messages(messages)
                self._compaction_count += 1

                return {"llm_input_messages": trimmed}

            return {"llm_input_messages": messages}

        return pre_model_hook

    def _trim_messages(self, messages: List[BaseMessage]) -> List[BaseMessage]:
        """Trim messages to fit within context limits.

        Preserves:
        - System messages (always kept)
        - Most recent N conversation turns
        - Important tool call results

        Args:
            messages: Full message history

        Returns:
            Trimmed message list
        """
        # Separate system messages from conversation
        system_messages = [m for m in messages if isinstance(m, SystemMessage)]
        conversation = [m for m in messages if not isinstance(m, SystemMessage)]

        if not conversation:
            return messages

        # Use LangChain's trim_messages for intelligent trimming
        trimmed_conversation = trim_messages(
            conversation,
            strategy="last",
            token_counter=self.token_counter,
            max_tokens=self.config.max_output_tokens,
            start_on="human",
            end_on=("human", "tool"),
            include_system=False,
        )

        # Reconstruct with system messages first
        result = system_messages + trimmed_conversation

        logger.info(
            f"Trimmed context from {len(messages)} to {len(result)} messages"
        )

        return result

    def should_compact(self, messages: List[BaseMessage]) -> bool:
        """Check if context needs compaction.

        Args:
            messages: Current message history

        Returns:
            True if compaction is needed
        """
        return self.token_counter(messages) > self.config.compaction_threshold_tokens

    def get_token_count(self, messages: List[BaseMessage]) -> int:
        """Get current token count for messages.

        Args:
            messages: Messages to count

        Returns:
            Token count
        """
        return self.token_counter(messages)

    @property
    def compaction_count(self) -> int:
        """Number of times context has been compacted in this session."""
        return self._compaction_count

    def create_summary_message(self, summary: str) -> SystemMessage:
        """Create a summary message to prepend to trimmed context.

        Args:
            summary: Summary text of prior conversation

        Returns:
            SystemMessage containing the summary
        """
        return SystemMessage(content=f"[Summary of prior conversation]\n{summary}")


class SummarizingContextManager(ContextManager):
    """Context manager that creates summaries of discarded context.

    Extends ContextManager to generate LLM-based summaries of conversation
    history before trimming, preserving important context information.

    Note: This requires an LLM instance to generate summaries.
    """

    def __init__(
        self,
        llm: Any,
        config: Optional[ContextConfig] = None,
        workspace: Optional[Any] = None,
        token_counter: Optional[Callable[[List[BaseMessage]], int]] = None
    ):
        """Initialize summarizing context manager.

        Args:
            llm: LLM instance for generating summaries
            config: Context management configuration
            workspace: Optional workspace for storing summaries
            token_counter: Optional custom token counting function
        """
        super().__init__(config, workspace, token_counter)
        self.llm = llm
        self._summaries: List[str] = []

    async def summarize_and_trim(
        self,
        messages: List[BaseMessage]
    ) -> List[BaseMessage]:
        """Summarize older messages and trim context.

        Creates a summary of messages that will be trimmed, then performs
        the trimming operation, prepending the summary.

        Args:
            messages: Full message history

        Returns:
            Trimmed messages with summary prepended
        """
        token_count = self.token_counter(messages)

        if token_count <= self.config.compaction_threshold_tokens:
            return messages

        # Identify messages that will be trimmed
        trimmed = self._trim_messages(messages)
        messages_to_summarize = messages[:-len(trimmed) + 1] if len(trimmed) < len(messages) else []

        if messages_to_summarize:
            # Generate summary
            summary = await self._generate_summary(messages_to_summarize)
            self._summaries.append(summary)

            # Store summary in workspace if available
            if self.workspace:
                await self.workspace.save_summary(summary)

            # Prepend summary to trimmed messages
            summary_msg = self.create_summary_message(summary)

            # Find where to insert (after system messages)
            insert_idx = 0
            for i, m in enumerate(trimmed):
                if isinstance(m, SystemMessage):
                    insert_idx = i + 1
                else:
                    break

            result = trimmed[:insert_idx] + [summary_msg] + trimmed[insert_idx:]
            return result

        return trimmed

    async def _generate_summary(self, messages: List[BaseMessage]) -> str:
        """Generate a summary of the given messages.

        Args:
            messages: Messages to summarize

        Returns:
            Summary string
        """
        # Format messages for summarization
        formatted = []
        for m in messages:
            if isinstance(m, HumanMessage):
                formatted.append(f"User: {m.content}")
            elif isinstance(m, AIMessage):
                content = m.content if isinstance(m.content, str) else str(m.content)
                formatted.append(f"Assistant: {content[:500]}...")
            elif hasattr(m, 'content'):
                formatted.append(f"[Tool/Other]: {str(m.content)[:200]}...")

        conversation_text = "\n".join(formatted[-50:])  # Last 50 messages max

        summary_prompt = f"""Summarize the key points from this conversation segment.
Focus on:
1. What tasks were attempted
2. What decisions were made
3. What information was discovered
4. Current progress state

Conversation:
{conversation_text}

Summary:"""

        response = await self.llm.ainvoke([HumanMessage(content=summary_prompt)])
        return response.content if isinstance(response.content, str) else str(response.content)

    @property
    def summaries(self) -> List[str]:
        """All summaries generated during this session."""
        return self._summaries.copy()
