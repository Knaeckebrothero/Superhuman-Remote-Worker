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
import time
import traceback
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Literal, Optional

from src.agents.shared.llm_archiver import get_archiver

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
        job_id = state.get("job_id", "unknown")
        logger.info(f"Initializing job {job_id}")

        # Audit: job initialization
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="initialize",
                node_name="initialize",
                iteration=0,
                data={
                    "state": {
                        "has_metadata": bool(state.get("metadata")),
                        "document_path": state.get("metadata", {}).get("document_path"),
                    }
                },
                metadata=state.get("metadata"),
            )

        # Build initial messages with metadata
        metadata = state.get("metadata", {})

        # Build context from metadata
        context_parts = []
        if metadata.get("document_path"):
            context_parts.append(f"**Document to process:** `{metadata['document_path']}`")
        if metadata.get("prompt"):
            context_parts.append(f"**Task:** {metadata['prompt']}")
        if metadata.get("requirement_id"):
            context_parts.append(f"**Requirement ID:** {metadata['requirement_id']}")

        context_block = ""
        if context_parts:
            context_block = "\n\n## Job Context\n" + "\n".join(context_parts) + "\n\n"

        initial_message = (
            "Begin your task. Start by reading instructions.md to understand "
            "what you need to do, then create a plan and execute it step by step."
            f"{context_block}"
        )

        messages: List[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=initial_message),
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
            job_id = state.get("job_id", "unknown")

            # Audit: LLM call start
            auditor = get_archiver()
            if auditor:
                auditor.audit_step(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    step_type="llm_call",
                    node_name="process",
                    iteration=iteration,
                    data={
                        "llm": {
                            "model": config.llm.model,
                            "input_message_count": len(prepared_messages),
                        },
                        "state": {
                            "message_count": len(messages),
                            "token_count": token_count,
                        }
                    },
                    metadata=state.get("metadata"),
                )

            # Call LLM with timing
            logger.info(f"Calling LLM ({config.llm.model})...")
            start_time = time.time()
            response = llm_with_tools.invoke(prepared_messages)
            latency_ms = int((time.time() - start_time) * 1000)

            tool_call_count = len(response.tool_calls) if hasattr(response, 'tool_calls') and response.tool_calls else 0
            logger.info(
                f"LLM response: {len(response.content)} chars, "
                f"{tool_call_count} tool calls, {latency_ms}ms"
            )

            # Archive to MongoDB (existing llm_requests collection)
            if auditor:
                auditor.archive(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    messages=prepared_messages,
                    response=response,
                    model=config.llm.model,
                    latency_ms=latency_ms,
                    iteration=iteration,
                    metadata=state.get("metadata"),
                )

                # Audit: LLM response
                tool_calls_preview = []
                if hasattr(response, 'tool_calls') and response.tool_calls:
                    for tc in response.tool_calls:
                        tool_calls_preview.append({
                            "name": tc.get("name", "unknown"),
                            "call_id": tc.get("id", ""),
                        })

                auditor.audit_step(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    step_type="llm_response",
                    node_name="process",
                    iteration=iteration,
                    data={
                        "llm": {
                            "model": config.llm.model,
                            "response_content_preview": response.content[:500] if response.content else "",
                            "tool_calls": tool_calls_preview,
                            "metrics": {
                                "output_chars": len(response.content) if response.content else 0,
                                "tool_call_count": tool_call_count,
                            }
                        }
                    },
                    latency_ms=latency_ms,
                    metadata=state.get("metadata"),
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

            # Audit: LLM error
            auditor = get_archiver()
            if auditor:
                auditor.audit_step(
                    job_id=state.get("job_id", "unknown"),
                    agent_type=config.agent_id,
                    step_type="error",
                    node_name="process",
                    iteration=iteration,
                    data={
                        "error": {
                            "type": "llm_error",
                            "message": str(e)[:500],
                            "recoverable": True,
                        }
                    },
                    metadata=state.get("metadata"),
                )

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
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        max_iterations = config.limits.max_iterations
        messages = state.get("messages", [])
        error = state.get("error")
        context_stats = state.get("context_stats") or {}

        # Helper to audit check decisions
        def audit_check(decision: str, reason: str, should_stop: bool):
            auditor = get_archiver()
            if auditor:
                auditor.audit_step(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    step_type="check",
                    node_name="check",
                    iteration=iteration,
                    data={
                        "check": {
                            "decision": decision,
                            "reason": reason,
                            "should_stop": should_stop,
                        },
                        "state": {
                            "message_count": len(messages),
                            "token_count": context_stats.get("current_token_count", 0),
                            "has_error": bool(error),
                        }
                    },
                    metadata=state.get("metadata"),
                )

        # Check iteration limit
        if iteration >= max_iterations:
            logger.warning(f"Max iterations ({max_iterations}) reached")
            audit_check("stop", "iteration_limit_reached", True)
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
                        {"iteration": iteration, "job_id": job_id},
                    )
                )
            return {
                "should_stop": True,
                "error": error_info,
                "iteration": iteration,
            }

        # Check token limit - if extremely high, force stop
        token_count = context_stats.get("current_token_count", 0)
        if token_count > config.limits.context_threshold_tokens * 1.5:
            logger.warning(f"Token count ({token_count}) exceeds safe limit")
            audit_check("stop", "token_limit_exceeded", True)
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
                        {"token_count": token_count, "job_id": job_id},
                    )
                )
            return {
                "should_stop": True,
                "error": error_info,
                "iteration": iteration,
            }

        # Check for non-recoverable errors
        if error and not error.get("recoverable", True):
            logger.error(f"Non-recoverable error: {error.get('message')}")
            audit_check("stop", "non_recoverable_error", True)
            # Write error to workspace
            if workspace_manager:
                asyncio.create_task(
                    write_error_to_workspace(
                        workspace_manager,
                        error,
                        {"iteration": iteration, "job_id": job_id},
                    )
                )
            return {"should_stop": True, "iteration": iteration}

        # Check for completion signals in recent messages
        if _detect_completion(messages, workspace=workspace_manager):
            logger.info("Completion detected in messages")
            audit_check("stop", "completion_detected", True)
            return {"should_stop": True, "iteration": iteration}

        # Apply custom check if provided
        if on_check:
            custom_state = on_check(state)
            if custom_state:
                audit_check("custom", "custom_check_triggered", custom_state.get("should_stop", False))
                return custom_state

        # Clear any recoverable errors
        if error and error.get("recoverable"):
            audit_check("continue", "recoverable_error_cleared", False)
            return {"error": None, "iteration": iteration}

        audit_check("continue", "no_stop_condition", False)
        return {"iteration": iteration}

    # Create tool node using LangGraph prebuilt
    tool_node = ToolNode(tools)

    async def tools_node(state: UniversalAgentState) -> Dict[str, Any]:
        """Execute tool calls with retry logic.

        Implements:
        1. Tool execution via ToolNode
        2. Retry logic with exponential backoff on failure
        3. Error tracking per tool
        4. Graceful degradation
        """
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
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

        # Extract tool call info for auditing
        tool_calls_info = []
        for tc in last_message.tool_calls:
            tool_calls_info.append({
                "name": tc.get("name", "unknown"),
                "call_id": tc.get("id", ""),
                "args": tc.get("args", {}),
            })

        # Audit: tool calls before execution
        auditor = get_archiver()
        if auditor:
            for tc_info in tool_calls_info:
                auditor.audit_tool_call(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    iteration=iteration,
                    tool_name=tc_info["name"],
                    call_id=tc_info["call_id"],
                    arguments=tc_info["args"],
                    metadata=state.get("metadata"),
                )

        # Try to execute tools
        attempt = 0
        max_attempts = retry_manager.max_retries

        while attempt < max_attempts:
            try:
                start_time = time.time()
                result = await tool_node.ainvoke(state)
                execution_time_ms = int((time.time() - start_time) * 1000)

                # Check if any tool results indicate actual errors
                # (not just the word "error" appearing in content)
                tool_messages = result.get("messages", [])
                has_errors = any(
                    isinstance(m, ToolMessage) and _is_tool_error(m.content)
                    for m in tool_messages
                )

                # Audit: tool results after execution
                if auditor:
                    # Match results to original calls by tool_call_id
                    call_id_to_info = {tc["call_id"]: tc for tc in tool_calls_info}
                    for msg in tool_messages:
                        if isinstance(msg, ToolMessage):
                            call_id = getattr(msg, "tool_call_id", "")
                            tc_info = call_id_to_info.get(call_id, {})
                            is_error = _is_tool_error(msg.content) if msg.content else False
                            auditor.audit_tool_result(
                                job_id=job_id,
                                agent_type=config.agent_id,
                                iteration=iteration,
                                tool_name=tc_info.get("name", getattr(msg, "name", "unknown")),
                                call_id=call_id,
                                result=msg.content if msg.content else "",
                                success=not is_error,
                                latency_ms=execution_time_ms // max(len(tool_messages), 1),
                                error=msg.content[:500] if is_error else None,
                                metadata=state.get("metadata"),
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
                    await asyncio.sleep(delay)
                    continue

                return {
                    **result,
                    "tool_retry_state": retry_state,
                }

            except Exception as e:
                logger.error(f"Tool execution error (attempt {attempt + 1}): {e}")

                # Audit: tool execution exception
                if auditor:
                    for tc_info in tool_calls_info:
                        auditor.audit_tool_result(
                            job_id=job_id,
                            agent_type=config.agent_id,
                            iteration=iteration,
                            tool_name=tc_info["name"],
                            call_id=tc_info["call_id"],
                            result="",
                            success=False,
                            latency_ms=0,
                            error=f"Exception: {str(e)[:500]}",
                            metadata=state.get("metadata"),
                        )

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

                    # Audit: final tool failure
                    if auditor:
                        auditor.audit_step(
                            job_id=job_id,
                            agent_type=config.agent_id,
                            step_type="error",
                            node_name="tools",
                            iteration=iteration,
                            data={
                                "error": {
                                    "type": "tool_error",
                                    "message": f"Tool execution failed after {max_attempts} attempts: {str(e)[:500]}",
                                    "tools": [tc["name"] for tc in tool_calls_info],
                                    "recoverable": True,
                                }
                            },
                            metadata=state.get("metadata"),
                        )

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
                await asyncio.sleep(delay)

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


def _detect_completion(messages: List[BaseMessage], workspace=None) -> bool:
    """Detect completion signals in messages.

    Priority order:
    1. Check if output/completion.json exists (explicit tool signal)
    2. Check for tool write confirmations (mark_complete output)
    3. Fall back to phrase matching (legacy support, reduced scope)

    Args:
        messages: Message list to check
        workspace: Optional workspace manager for file existence check

    Returns:
        True if completion detected
    """
    if not messages:
        return False

    # Priority 1: Check if completion file exists
    if workspace:
        try:
            if workspace.file_exists("output/completion.json"):
                logger.debug("Completion detected: output/completion.json exists")
                return True
        except Exception:
            pass  # Workspace not available, continue to other checks

    # Priority 2: Check recent tool messages for mark_complete output
    recent_tool_messages = [
        m for m in messages[-5:]
        if isinstance(m, ToolMessage)
    ]

    for msg in recent_tool_messages:
        content_lower = msg.content.lower()
        # mark_complete tool returns this exact pattern
        if "wrote file: output/completion.json" in content_lower:
            logger.debug("Completion detected: mark_complete tool output")
            return True

    # Priority 3: Legacy phrase matching (for backwards compatibility)
    # Only check last 3 AI messages to reduce false positives
    recent_ai_messages = [
        m for m in messages[-5:]
        if isinstance(m, AIMessage) and m.content
    ][-3:]

    # Reduced, more specific phrase list to minimize false positives
    completion_phrases = [
        "task is complete",
        "task has been completed",
        "work is complete",
        "successfully completed all",
        "finished processing all",
    ]

    for msg in recent_ai_messages:
        content_lower = msg.content.lower()
        for phrase in completion_phrases:
            if phrase in content_lower:
                logger.debug(f"Completion detected: phrase '{phrase}'")
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
