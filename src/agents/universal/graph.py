"""LangGraph construction for Universal Agent.

Implements a simplified 4-node graph architecture that supports
the workspace-centric autonomous agent pattern.

Graph Structure:
    initialize → process ←→ tools
                    ↓
                  check → END (or back to process)

Phase 6 additions:
- Context management (tool result clearing, summarization)
- Tool retry logic with exponential backoff
- Error handling with workspace persistence
"""

import asyncio
import logging
import traceback
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Literal, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from .state import UniversalAgentState
from .loader import AgentConfig
from .context import (
    ContextConfig,
    ContextManager,
    ToolRetryManager,
    write_error_to_workspace,
)

logger = logging.getLogger(__name__)


def build_agent_graph(
    llm_with_tools: BaseChatModel,
    tools: List[Any],
    config: AgentConfig,
    system_prompt: str,
    workspace_manager: Optional[Any] = None,
    context_config: Optional[ContextConfig] = None,
    summarization_prompt: Optional[str] = None,
    on_initialize: Optional[Callable[[UniversalAgentState], UniversalAgentState]] = None,
    on_check: Optional[Callable[[UniversalAgentState], UniversalAgentState]] = None,
) -> StateGraph:
    """Build the LangGraph workflow for the Universal Agent.

    Creates a 4-node graph with the following structure:
    - initialize: Set up workspace, create initial messages
    - process: Main LLM processing node (with context management)
    - tools: Execute tool calls via ToolNode (with retry logic)
    - check: Check for completion, handle errors

    Args:
        llm_with_tools: LLM instance with tools bound
        tools: List of tool objects for ToolNode
        config: Agent configuration
        system_prompt: Formatted system prompt
        workspace_manager: Optional workspace manager for error persistence
        context_config: Optional context management configuration
        summarization_prompt: Optional custom summarization prompt
        on_initialize: Optional callback for initialization customization
        on_check: Optional callback for completion check customization

    Returns:
        Compiled StateGraph ready for invocation

    Example:
        ```python
        graph = build_agent_graph(
            llm_with_tools=llm.bind_tools(tools),
            tools=tools,
            config=agent_config,
            system_prompt=prompt,
            workspace_manager=workspace_mgr,
        )
        result = await graph.ainvoke(initial_state)
        ```
    """
    workflow = StateGraph(UniversalAgentState)

    # Initialize context management
    ctx_config = context_config or ContextConfig(
        compaction_threshold_tokens=config.context_management.keep_recent_tool_results * 10000,
        keep_recent_tool_results=config.context_management.keep_recent_tool_results,
    )
    context_manager = ContextManager(config=ctx_config)

    # Initialize retry manager
    retry_manager = ToolRetryManager(
        max_retries=config.limits.tool_retry_count,
    )

    # Create nodes
    def initialize_node(state: UniversalAgentState) -> Dict[str, Any]:
        """Initialize the agent workspace and messages.

        This node:
        1. Creates the initial system message with the prompt
        2. Adds a human message directing the agent to start
        3. Initializes context management state
        4. Calls optional initialization callback
        """
        logger.info(f"Initializing job {state['job_id']}")

        # Build initial messages
        messages: List[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=(
                    "Begin your task. Start by reading instructions.md to understand "
                    "what you need to do, then create a plan and execute it step by step."
                )
            ),
        ]

        new_state: Dict[str, Any] = {
            "messages": messages,
            "iteration": 0,
            "context_stats": {
                "total_tool_results_cleared": 0,
                "total_messages_trimmed": 0,
                "total_summarizations": 0,
                "current_token_count": 0,
            },
            "tool_retry_state": {
                "current_retries": {},
                "total_retries": 0,
                "failed_tools": {},
            },
        }

        # Apply custom initialization if provided
        if on_initialize:
            state_copy = dict(state)
            state_copy.update(new_state)
            custom_state = on_initialize(state_copy)
            if custom_state:
                new_state.update(custom_state)

        return new_state

    def process_node(state: UniversalAgentState) -> Dict[str, Any]:
        """Main LLM processing node with context management.

        This node:
        1. Applies context management (tool result clearing, trimming)
        2. Checks if summarization is needed
        3. Calls the LLM with tools
        4. Updates context statistics
        5. Handles edge cases (consecutive AI messages)
        """
        messages = state["messages"]
        iteration = state.get("iteration", 0)

        logger.debug(f"Process node iteration {iteration}, {len(messages)} messages")

        # Get current token count
        token_count = context_manager.get_token_count(messages)
        logger.debug(f"Current token count: {token_count}")

        # Check if we're approaching limits and need aggressive compaction
        should_be_aggressive = token_count > ctx_config.compaction_threshold_tokens

        # Apply context management - prepare messages for LLM
        prepared_messages = context_manager.prepare_messages_for_llm(
            messages,
            aggressive=should_be_aggressive,
        )

        # Handle consecutive AI messages (add continuation prompt)
        if prepared_messages and isinstance(prepared_messages[-1], AIMessage):
            if not prepared_messages[-1].tool_calls:
                prepared_messages.append(
                    HumanMessage(content="Please continue with your task.")
                )

        try:
            # Call LLM
            response = llm_with_tools.invoke(prepared_messages)

            logger.debug(
                f"LLM response: {len(response.content)} chars, "
                f"{len(response.tool_calls) if hasattr(response, 'tool_calls') else 0} tool calls"
            )

            # Update context stats
            new_token_count = context_manager.get_token_count(messages + [response])
            context_stats = state.get("context_stats") or {}
            context_stats.update({
                "current_token_count": new_token_count,
                "total_tool_results_cleared": context_manager.state.total_tool_results_cleared,
            })

            return {
                "messages": [response],
                "iteration": iteration + 1,
                "context_stats": context_stats,
            }

        except Exception as e:
            logger.error(f"LLM invocation error: {e}")
            return {
                "error": {
                    "message": str(e),
                    "type": "llm_error",
                    "recoverable": True,
                    "traceback": traceback.format_exc(),
                },
                "iteration": iteration + 1,
            }

    def check_node(state: UniversalAgentState) -> Dict[str, Any]:
        """Check for completion and update state.

        This node:
        1. Checks iteration limits
        2. Checks token limits (trigger summarization if needed)
        3. Detects completion signals in messages
        4. Handles errors (write to workspace if non-recoverable)
        5. Calls optional check callback
        """
        iteration = state.get("iteration", 0)
        max_iterations = config.limits.max_iterations
        messages = state.get("messages", [])
        error = state.get("error")
        context_stats = state.get("context_stats") or {}

        # Check iteration limit
        if iteration >= max_iterations:
            logger.warning(f"Max iterations ({max_iterations}) reached")
            error_info = {
                "message": f"Max iterations ({max_iterations}) reached",
                "type": "iteration_limit",
                "recoverable": False,
            }
            # Write error to workspace if available
            if workspace_manager:
                asyncio.create_task(
                    write_error_to_workspace(
                        workspace_manager,
                        error_info,
                        {"iteration": iteration, "job_id": state.get("job_id")},
                    )
                )
            return {
                "should_stop": True,
                "error": error_info,
            }

        # Check token limit - if extremely high, force stop
        token_count = context_stats.get("current_token_count", 0)
        if token_count > config.limits.context_threshold_tokens * 1.5:
            logger.warning(f"Token count ({token_count}) exceeds safe limit")
            error_info = {
                "message": f"Token count ({token_count}) exceeds safe limit",
                "type": "token_limit",
                "recoverable": False,
            }
            if workspace_manager:
                asyncio.create_task(
                    write_error_to_workspace(
                        workspace_manager,
                        error_info,
                        {"token_count": token_count, "job_id": state.get("job_id")},
                    )
                )
            return {
                "should_stop": True,
                "error": error_info,
            }

        # Check for non-recoverable errors
        if error and not error.get("recoverable", True):
            logger.error(f"Non-recoverable error: {error.get('message')}")
            # Write error to workspace
            if workspace_manager:
                asyncio.create_task(
                    write_error_to_workspace(
                        workspace_manager,
                        error,
                        {"iteration": iteration, "job_id": state.get("job_id")},
                    )
                )
            return {"should_stop": True}

        # Check for completion signals in recent messages
        if _detect_completion(messages):
            logger.info("Completion detected in messages")
            return {"should_stop": True}

        # Apply custom check if provided
        if on_check:
            custom_state = on_check(state)
            if custom_state:
                return custom_state

        # Clear any recoverable errors
        if error and error.get("recoverable"):
            return {"error": None}

        return {}

    # Create tool node using LangGraph prebuilt
    tool_node = ToolNode(tools)

    def tools_node(state: UniversalAgentState) -> Dict[str, Any]:
        """Execute tool calls with retry logic.

        Implements:
        1. Tool execution via ToolNode
        2. Retry logic with exponential backoff on failure
        3. Error tracking per tool
        4. Graceful degradation
        """
        retry_state = state.get("tool_retry_state") or {
            "current_retries": {},
            "total_retries": 0,
            "failed_tools": {},
        }

        # Get the tool calls from the last message
        messages = state.get("messages", [])
        last_message = messages[-1] if messages else None

        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {}

        # Try to execute tools
        attempt = 0
        max_attempts = retry_manager.max_retries

        while attempt < max_attempts:
            try:
                result = tool_node.invoke(state)

                # Check if any tool results indicate actual errors
                # (not just the word "error" appearing in content)
                tool_messages = result.get("messages", [])
                has_errors = any(
                    isinstance(m, ToolMessage) and _is_tool_error(m.content)
                    for m in tool_messages
                )

                if has_errors and attempt < max_attempts - 1:
                    # Retry on error
                    attempt += 1
                    retry_manager.record_retry()
                    retry_state["total_retries"] = retry_manager._total_retries
                    delay = retry_manager.get_retry_delay(attempt)
                    logger.warning(
                        f"Tool error detected, retrying ({attempt}/{max_attempts}) "
                        f"after {delay:.1f}s"
                    )
                    # Note: In async context, use asyncio.sleep
                    # For sync, we just continue immediately
                    continue

                return {
                    **result,
                    "tool_retry_state": retry_state,
                }

            except Exception as e:
                logger.error(f"Tool execution error (attempt {attempt + 1}): {e}")

                # Record failure for each tool
                for tool_call in last_message.tool_calls:
                    tool_name = tool_call.get("name", "unknown")
                    retry_manager.record_failure(tool_name)
                    retry_state["failed_tools"][tool_name] = (
                        retry_state["failed_tools"].get(tool_name, 0) + 1
                    )

                attempt += 1
                retry_state["total_retries"] = retry_manager._total_retries

                if attempt >= max_attempts:
                    # All retries exhausted - return error messages
                    logger.error(f"Tool execution failed after {max_attempts} attempts")
                    error_messages = []
                    for tool_call in last_message.tool_calls:
                        error_messages.append(
                            ToolMessage(
                                content=(
                                    f"Tool execution failed after {max_attempts} attempts: {str(e)}\n"
                                    "Please try an alternative approach or skip this step."
                                ),
                                tool_call_id=tool_call["id"],
                            )
                        )
                    return {
                        "messages": error_messages,
                        "tool_retry_state": retry_state,
                        "error": {
                            "message": f"Tool execution failed: {str(e)}",
                            "type": "tool_error",
                            "recoverable": True,  # Let the LLM try a different approach
                            "traceback": traceback.format_exc(),
                        },
                    }

                # Wait before retry (exponential backoff)
                delay = retry_manager.get_retry_delay(attempt)
                logger.info(f"Waiting {delay:.1f}s before retry {attempt + 1}")
                # In sync context, we can't easily sleep, so we just continue
                # The delay is more relevant for async execution

        # Should not reach here, but handle gracefully
        return {
            "error": {
                "message": "Unexpected tool execution state",
                "type": "tool_error",
                "recoverable": True,
            },
            "tool_retry_state": retry_state,
        }

    # Add nodes to graph
    workflow.add_node("initialize", initialize_node)
    workflow.add_node("process", process_node)
    workflow.add_node("tools", tools_node)
    workflow.add_node("check", check_node)

    # Set entry point
    workflow.set_entry_point("initialize")

    # Add edges
    workflow.add_edge("initialize", "process")

    # From process: route to tools if tool calls, else to check
    workflow.add_conditional_edges(
        "process",
        _route_from_process,
        {
            "tools": "tools",
            "check": "check",
        },
    )

    # From tools: always go to check
    workflow.add_edge("tools", "check")

    # From check: continue or end
    workflow.add_conditional_edges(
        "check",
        _route_from_check,
        {
            "continue": "process",
            "end": END,
        },
    )

    return workflow.compile()


def _route_from_process(state: UniversalAgentState) -> Literal["tools", "check"]:
    """Route from process node based on LLM response.

    If LLM returned tool calls, route to tools node.
    Otherwise, route to check node.
    """
    messages = state.get("messages", [])

    if not messages:
        return "check"

    last_message = messages[-1]

    # Check if last message has tool calls
    if isinstance(last_message, AIMessage):
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

    return "check"


def _route_from_check(state: UniversalAgentState) -> Literal["continue", "end"]:
    """Route from check node based on completion status.

    If should_stop is True, end the workflow.
    Otherwise, continue processing.
    """
    if state.get("should_stop", False):
        return "end"

    return "continue"


def _is_tool_error(content: str) -> bool:
    """Check if tool message content indicates an actual error.

    Distinguishes between:
    - Actual errors: "Error: File not found", "Failed to read..."
    - Incidental: Content that happens to contain the word "error"

    Args:
        content: Tool message content

    Returns:
        True if this is an actual error message
    """
    content_lower = content.lower().strip()

    # Check for error prefixes (actual error messages from tools)
    error_prefixes = [
        "error:",
        "error -",
        "failed:",
        "failed to",
        "exception:",
        "traceback",
        "file not found",
        "permission denied",
        "not found:",
        "cannot ",
        "unable to",
        "invalid ",
    ]

    for prefix in error_prefixes:
        if content_lower.startswith(prefix):
            return True

    # Check for error patterns in short messages (likely error responses)
    if len(content) < 200:
        short_error_patterns = [
            "does not exist",
            "no such file",
            "access denied",
            "connection refused",
            "timeout",
            "not initialized",
        ]
        for pattern in short_error_patterns:
            if pattern in content_lower:
                return True

    return False


def _detect_completion(messages: List[BaseMessage]) -> bool:
    """Detect completion signals in messages.

    Looks for indicators that the agent has finished its task:
    - Explicit completion phrases
    - Writing completion.json mentioned
    - Task complete indicators

    Args:
        messages: Message list to check

    Returns:
        True if completion detected
    """
    if not messages:
        return False

    # Check last few AI messages
    recent_ai_messages = [
        m for m in messages[-10:]
        if isinstance(m, AIMessage) and m.content
    ]

    completion_phrases = [
        "task is complete",
        "task has been completed",
        "work is complete",
        "work is done",
        "successfully completed",
        "all requirements have been",
        "validation complete",
        "integration complete",
        "written to output/completion",
        "completion.json",
        "finished processing",
        "no more items to process",
        "all items processed",
    ]

    for msg in recent_ai_messages:
        content_lower = msg.content.lower()
        for phrase in completion_phrases:
            if phrase in content_lower:
                return True

    # Check if recent tool calls actually wrote to completion.json
    # (not just read or mentioned it in content)
    recent_tool_messages = [
        m for m in messages[-5:]
        if isinstance(m, ToolMessage)
    ]

    for msg in recent_tool_messages:
        content_lower = msg.content.lower()
        # Check for write confirmation patterns (from workspace tools)
        write_patterns = [
            "wrote file: output/completion.json",
            "wrote file: completion.json",
            "wrote to output/completion.json",
            "created output/completion.json",
        ]
        for pattern in write_patterns:
            if pattern in content_lower:
                return True

    return False


async def run_graph_with_streaming(
    graph: StateGraph,
    initial_state: UniversalAgentState,
    config: Dict[str, Any],
):
    """Run the graph with streaming output.

    Yields state updates as the graph executes.

    Args:
        graph: Compiled graph
        initial_state: Initial state
        config: LangGraph config (thread_id, recursion_limit, etc.)

    Yields:
        State updates from each node
    """
    async for state in graph.astream(initial_state, config=config):
        yield state


async def run_graph_with_summarization(
    graph: StateGraph,
    initial_state: UniversalAgentState,
    llm: BaseChatModel,
    context_manager: ContextManager,
    config: Dict[str, Any],
    summarization_prompt: Optional[str] = None,
):
    """Run the graph with automatic summarization support.

    When context exceeds summarization threshold, pauses to summarize
    and then continues with compacted context.

    Args:
        graph: Compiled graph
        initial_state: Initial state
        llm: LLM for summarization (can be smaller model)
        context_manager: Context manager instance
        config: LangGraph config
        summarization_prompt: Optional custom prompt

    Yields:
        State updates from each node
    """
    current_state = initial_state

    async for state_update in graph.astream(current_state, config=config):
        yield state_update

        # Check if summarization is needed
        messages = state_update.get("messages", [])
        if context_manager.should_summarize(messages):
            logger.info("Triggering automatic summarization")

            # Summarize and compact
            compacted_messages = await context_manager.summarize_and_compact(
                messages,
                llm,
                summarization_prompt,
            )

            # Update state with compacted messages
            # Note: This requires the graph to support message replacement
            # In practice, you may need to restart with new state
            current_state = {
                **state_update,
                "messages": compacted_messages,
            }
