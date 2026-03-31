"""Persistent Agent — Interactive Loop.

Implements the while(tool_call) execution loop for persistent interactive
agents. No LangGraph, no phase alternation, no todos — just a plain async
loop that waits for user input, calls the LLM with tools, executes tool
calls (with permission checks), and repeats.

Reuses the same shared infrastructure as the worker graph:
- ContextManager for token counting and compaction
- Transient injection for workspace.md
- AuxiliaryLLM for summarization
- load_tools / ToolContext for tool loading
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from .core.context import ContextManager

logger = logging.getLogger(__name__)

# Sentinel values for user input queue
INTERRUPT_SENTINEL = "__INTERRUPT__"
APPROVE_SENTINEL = "__APPROVE__"
DENY_SENTINEL = "__DENY__"


class IdleTimeoutError(Exception):
    """Raised when the user has been idle beyond the configured timeout."""

    pass


@dataclass
class TurnResult:
    """Result of a single interactive turn."""

    turn_id: int
    messages_added: int
    tool_calls_made: int
    interrupted: bool = False
    error: Optional[str] = None
    metrics: Optional[dict] = None


@dataclass
class PersistentLoopCallbacks:
    """Callbacks wiring the loop to the transport layer (WebSocket).

    All callbacks are async. The loop is transport-agnostic — it only
    communicates through these callbacks.
    """

    # Wait for the next user message (blocks until available)
    get_user_input: Callable[[], Awaitable[str]]

    # Stream a token chunk to the client
    on_token: Callable[[str], Awaitable[None]]

    # Notify client that a tool is about to execute
    on_tool_start: Callable[[str, Dict[str, Any], str], Awaitable[None]]

    # Notify client with tool result
    on_tool_result: Callable[[str, str, str], Awaitable[None]]

    # Ask client for permission to run a tool (returns True if approved)
    permission_check: Callable[[str, Dict[str, Any]], Awaitable[bool]]

    # Notify client of turn lifecycle events
    on_turn_start: Callable[[int], Awaitable[None]]
    on_turn_complete: Callable[[int, Optional[dict]], Awaitable[None]]

    # Notify client of errors
    on_error: Callable[[str], Awaitable[None]]

    # Check if an interrupt was requested (non-blocking)
    check_interrupt: Callable[[], bool]

    # Notify client that a VM upgrade is needed (sudo detected, optional)
    on_vm_upgrade_needed: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None


async def run_persistent_loop(
        llm_with_tools: BaseChatModel,
        tools: List[Any],
        context_manager: ContextManager,
        config: Any,
        system_prompt: str,
        callbacks: PersistentLoopCallbacks,
        messages: List[BaseMessage],
        auxiliary_llm: Optional[Any] = None,
        workspace_content: Optional[Callable[[], str]] = None,
        recall_store: Optional[Any] = None,
        knowledge_store: Optional[Any] = None,
        project_id: Optional[str] = None,
        project_ids: Optional[List[str]] = None,
        tool_context: Optional[Any] = None,
        initial_turn_count: int = 0,
) -> None:
    """Run the persistent interactive agent loop.

    This is the core execution engine for persistent agents. It runs
    forever (or until cancelled), alternating between waiting for user
    input and executing agent turns.

    Args:
        llm_with_tools: LLM with tools bound
        tools: List of tool objects (for direct invocation)
        context_manager: For token counting and compaction
        config: AgentConfig
        system_prompt: Pre-built system prompt string
        callbacks: Transport callbacks (WebSocket I/O)
        messages: Mutable message list (persisted across turns)
        auxiliary_llm: For summarization during compaction
        workspace_content: Callable returning current workspace.md content
        recall_store: RecallStore instance for memory injection/extraction
        knowledge_store: KnowledgeStore instance for knowledge injection
        project_id: Project UUID string for scoped knowledge queries (backward compat)
        project_ids: List of project UUID strings for multi-project sessions
    """
    # Build tool lookup map
    tool_map: Dict[str, Any] = {tool.name: tool for tool in tools}
    turn_count = initial_turn_count
    llm_timeout = getattr(config.llm, "timeout", 600) or 600

    # Memory extraction config
    memory_config = getattr(config, "memory", None)
    extraction_interval = getattr(memory_config, "extraction_interval", 5) if memory_config else 5
    extraction_prompt = getattr(memory_config, "extraction_prompt", "") if memory_config else ""
    _last_extraction_turn = 0

    # Send system prompt as first message if not already present
    if not messages or not isinstance(messages[0], SystemMessage):
        messages.insert(0, SystemMessage(content=system_prompt))

    logger.info(
        f"Persistent loop started with {len(tools)} tools, "
        f"system prompt {len(system_prompt)} chars"
    )

    while True:
        # --- Wait for user input ---
        try:
            user_input = await callbacks.get_user_input()
        except asyncio.CancelledError:
            logger.info("Persistent loop cancelled while waiting for input")
            return
        except IdleTimeoutError:
            logger.info("Persistent loop exiting due to idle timeout")
            raise  # Propagate to loop_task

        if user_input == INTERRUPT_SENTINEL:
            continue

        turn_count += 1
        turn_id = turn_count
        messages.append(HumanMessage(content=user_input))

        await callbacks.on_turn_start(turn_id)
        tool_calls_this_turn = 0

        result = None
        try:
            result = await _execute_turn(
                llm_with_tools=llm_with_tools,
                tool_map=tool_map,
                context_manager=context_manager,
                messages=messages,
                callbacks=callbacks,
                llm_timeout=llm_timeout,
                auxiliary_llm=auxiliary_llm,
                workspace_content=workspace_content,
                config=config,
                recall_store=recall_store,
                knowledge_store=knowledge_store,
                project_id=project_id,
                project_ids=project_ids,
                tool_context=tool_context,
            )
            tool_calls_this_turn = result.tool_calls_made
        except asyncio.CancelledError:
            logger.info(f"Turn {turn_id} cancelled")
            return
        except Exception as e:
            logger.exception(f"Error in turn {turn_id}")
            await callbacks.on_error(str(e))

        # Memory extraction every N turns (fire-and-forget)
        if (
                recall_store
                and auxiliary_llm
                and extraction_interval > 0
                and (turn_count - _last_extraction_turn) >= extraction_interval
        ):
            _last_extraction_turn = turn_count
            try:
                from .services.auxiliary import extract_and_store_memories

                asyncio.create_task(
                    extract_and_store_memories(
                        auxiliary_llm=auxiliary_llm,
                        recall_store=recall_store,
                        messages=messages,
                        memory_extraction_prompt=extraction_prompt,
                        source_turn_start=turn_count - extraction_interval,
                        source_turn_end=turn_count,
                    )
                )
                logger.debug(f"Memory extraction triggered at turn {turn_count}")
            except Exception as e:
                logger.warning(f"Memory extraction failed (non-fatal): {e}")

        turn_metrics = result.metrics if result else None
        await callbacks.on_turn_complete(turn_id, turn_metrics)

        # Auto-commit workspace changes after tool-executing turns
        if tool_calls_this_turn > 0 and tool_context:
            ws_mgr = getattr(tool_context, 'workspace_manager', None)
            git_mgr = getattr(ws_mgr, 'git_manager', None) if ws_mgr else None
            if git_mgr and git_mgr.is_active:
                try:
                    if git_mgr.has_uncommitted_changes():
                        git_mgr.commit(f"Auto-commit after turn {turn_id}")
                    if turn_count % 5 == 0:
                        git_mgr.push()
                except Exception as e:
                    logger.debug(f"Git auto-commit failed (non-fatal): {e}")

        logger.info(
            f"Turn {turn_id} complete: {tool_calls_this_turn} tool calls, "
            f"{len(messages)} total messages"
        )


async def _execute_turn(
        llm_with_tools: BaseChatModel,
        tool_map: Dict[str, Any],
        context_manager: ContextManager,
        messages: List[BaseMessage],
        callbacks: PersistentLoopCallbacks,
        llm_timeout: float,
        auxiliary_llm: Optional[Any],
        workspace_content: Optional[Callable[[], str]],
        config: Any,
        recall_store: Optional[Any] = None,
        knowledge_store: Optional[Any] = None,
        project_id: Optional[str] = None,
        project_ids: Optional[List[str]] = None,
        tool_context: Optional[Any] = None,
) -> TurnResult:
    """Execute a single turn: LLM call -> tool calls -> repeat until done.

    A turn ends when the LLM produces a response with no tool calls,
    or when the user interrupts.
    """
    tool_calls_made = 0
    messages_added = 0

    # --- Memory retrieval (once per turn, before the inner loop) ---
    memory_block = ""
    knowledge_block = ""

    # Memory/knowledge retrieval with timeout — must never block the LLM call
    _RETRIEVAL_TIMEOUT = 5  # seconds

    if recall_store:
        try:
            await asyncio.wait_for(recall_store.decrement_ttl(), timeout=_RETRIEVAL_TIMEOUT)
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"TTL decrement failed (non-fatal): {e}")

        try:
            context_text = ""
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage):
                    context_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                    break

            memories = await asyncio.wait_for(
                recall_store.retrieve(context_text), timeout=_RETRIEVAL_TIMEOUT
            )
            if memories:
                from .services.recall_store import RecallStore as _RS
                memory_block = _RS.assemble_memory_block(memories)
                logger.debug(f"Memory injection: {len(memories)} memories retrieved")
        except asyncio.TimeoutError:
            logger.warning("Memory retrieval timed out — skipping injection")
        except Exception as e:
            logger.warning(f"Memory retrieval failed (non-fatal): {e}")

    effective_pids = project_ids or ([project_id] if project_id else [])
    if knowledge_store and effective_pids:
        try:
            import uuid as _uuid
            kb_context = ""
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage):
                    kb_context = msg.content if isinstance(msg.content, str) else str(msg.content)
                    break

            kb_notes = await asyncio.wait_for(
                knowledge_store.hybrid_search(
                    project_ids=[_uuid.UUID(p) for p in effective_pids],
                    query=kb_context,
                    match_count=5,
                ),
                timeout=_RETRIEVAL_TIMEOUT,
            )
            if kb_notes:
                from .services.knowledge_store import KnowledgeStore as _KS
                knowledge_block = _KS.assemble_knowledge_block(kb_notes)
                logger.debug(f"Knowledge injection: {len(kb_notes)} notes retrieved")
        except asyncio.TimeoutError:
            logger.warning("Knowledge retrieval timed out — skipping injection")
        except Exception as e:
            logger.warning(f"Knowledge retrieval failed (non-fatal): {e}")

    while True:
        # Check for interrupt before LLM call
        if callbacks.check_interrupt():
            return TurnResult(
                turn_id=0,
                messages_added=messages_added,
                tool_calls_made=tool_calls_made,
                interrupted=True,
            )

        # Inject workspace.md as transient system context if available
        prepared = list(messages)
        if workspace_content:
            ws_content = workspace_content()
            if ws_content:
                # Insert workspace context after system message
                ws_msg = SystemMessage(
                    content=f"<workspace_memory>\n{ws_content}\n</workspace_memory>"
                )
                # Find insertion point (after system message, before conversation)
                insert_idx = 1 if prepared and isinstance(prepared[0], SystemMessage) else 0
                prepared.insert(insert_idx, ws_msg)

        # Inject memory and knowledge as transient tool-call pairs
        if memory_block:
            try:
                from .core.memory_injection import create_memory_injection_messages
                mem_ai, mem_tool = create_memory_injection_messages(memory_block)
                # Insert after workspace injection, before conversation
                inject_idx = 2 if workspace_content and prepared and len(prepared) > 1 and isinstance(prepared[1],
                                                                                                      SystemMessage) else 1
                prepared.insert(inject_idx, mem_ai)
                prepared.insert(inject_idx + 1, mem_tool)
            except Exception as e:
                logger.warning(f"Memory injection failed (non-fatal): {e}")

        if knowledge_block:
            try:
                from .core.knowledge_injection import create_knowledge_injection_messages
                kb_ai, kb_tool = create_knowledge_injection_messages(knowledge_block)
                # Insert after memory injection
                inject_idx = len(prepared) - len(messages) + (
                    1 if prepared and isinstance(prepared[0], SystemMessage) else 0)
                prepared.insert(inject_idx, kb_ai)
                prepared.insert(inject_idx + 1, kb_tool)
            except Exception as e:
                logger.warning(f"Knowledge injection failed (non-fatal): {e}")

        # Context compaction if needed
        pre_compact_len = len(prepared)
        prepared = await context_manager.ensure_within_limits(
            prepared,
            auxiliary_llm,
            max_summary_length=getattr(
                config.context_management, "max_summary_length", 10000
            ),
        )

        # Auto-compaction happened — commit + push workspace to Gitea as checkpoint
        if len(prepared) < pre_compact_len and tool_context:
            ws_mgr = getattr(tool_context, 'workspace_manager', None)
            git_mgr = getattr(ws_mgr, 'git_manager', None) if ws_mgr else None
            if git_mgr and git_mgr.is_active:
                try:
                    if git_mgr.has_uncommitted_changes():
                        git_mgr.commit(
                            f"Auto-compaction checkpoint ({pre_compact_len} → {len(prepared)} msgs)"
                        )
                    git_mgr.push()
                except Exception as e:
                    logger.debug(f"Git push on auto-compaction failed (non-fatal): {e}")

        # --- LLM call with streaming (fallback to ainvoke for reasoning models) ---
        response_content = ""
        response: Optional[AIMessage] = None
        llm_start = time.monotonic()

        try:
            try:
                # Try astream for token-by-token streaming
                chunks = []
                streaming_interrupted = False
                async for chunk in llm_with_tools.astream(prepared):
                    chunks.append(chunk)
                    # Extract and stream text content
                    if hasattr(chunk, "content") and chunk.content:
                        content = chunk.content
                        # Anthropic returns content as list of dicts
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text:
                                        response_content += text
                                        await callbacks.on_token(text)
                                elif isinstance(block, str) and block:
                                    response_content += block
                                    await callbacks.on_token(block)
                        elif isinstance(content, str) and content:
                            response_content += content
                            await callbacks.on_token(content)

                    # Check for mid-stream interrupt
                    if callbacks.check_interrupt():
                        logger.info("Interrupt received during LLM streaming")
                        streaming_interrupted = True
                        break

                # Concatenate all chunks into final response
                if chunks:
                    response = chunks[0]
                    for chunk in chunks[1:]:
                        response = response + chunk

                # Handle mid-stream interruption
                if streaming_interrupted:
                    if response:
                        # Strip incomplete tool calls from partial response
                        if hasattr(response, "tool_calls"):
                            response.tool_calls = []
                        if hasattr(response, "invalid_tool_calls"):
                            response.invalid_tool_calls = []
                        messages.append(response)
                        messages_added += 1
                    return TurnResult(
                        turn_id=0,
                        messages_added=messages_added,
                        tool_calls_made=tool_calls_made,
                        interrupted=True,
                    )

            except Exception as stream_err:
                # Fallback to ainvoke when streaming fails
                # (e.g. ReasoningCapturingClient can't handle stream=True)
                err_name = type(stream_err).__name__
                if "ResponseNotRead" in err_name or "APIConnectionError" in err_name:
                    logger.info(f"Streaming not supported ({err_name}), falling back to ainvoke")
                    response = await llm_with_tools.ainvoke(prepared)
                    # Stream the complete response as a single chunk
                    if hasattr(response, "content") and response.content:
                        content = response.content
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text:
                                        response_content += text
                                        await callbacks.on_token(text)
                                elif isinstance(block, str) and block:
                                    response_content += block
                                    await callbacks.on_token(block)
                        elif isinstance(content, str) and content:
                            response_content = content
                            await callbacks.on_token(content)
                else:
                    raise

        except asyncio.TimeoutError:
            error_msg = f"LLM call timed out after {llm_timeout}s"
            logger.error(error_msg)
            await callbacks.on_error(error_msg)
            return TurnResult(
                turn_id=0,
                messages_added=messages_added,
                tool_calls_made=tool_calls_made,
                error=error_msg,
            )

        # Extract per-turn metrics from response metadata
        llm_latency_ms = int((time.monotonic() - llm_start) * 1000)
        turn_metrics: Optional[dict] = None
        if response and hasattr(response, "response_metadata") and response.response_metadata:
            meta = response.response_metadata
            token_usage = meta.get("token_usage", {})
            turn_metrics = {
                "input_tokens": token_usage.get("input_tokens") or token_usage.get("prompt_tokens"),
                "output_tokens": token_usage.get("output_tokens") or token_usage.get("completion_tokens"),
                "reasoning_tokens": token_usage.get("reasoning_tokens"),
                "latency_ms": llm_latency_ms,
                "model": meta.get("model_name"),
            }
            turn_metrics = {k: v for k, v in turn_metrics.items() if v is not None}

        if response is None:
            return TurnResult(
                turn_id=0,
                messages_added=messages_added,
                tool_calls_made=tool_calls_made,
                error="Empty LLM response",
            )

        # Add AI response to message history
        messages.append(response)
        messages_added += 1

        # No tool calls? Turn is done.
        if not hasattr(response, "tool_calls") or not response.tool_calls:
            break

        # --- Execute tool calls ---
        for i, tool_call in enumerate(response.tool_calls):
            # Check for interrupt before each tool
            if callbacks.check_interrupt():
                logger.info(f"Interrupt received before tool {tool_call['name']}")
                for remaining in response.tool_calls[i:]:
                    messages.append(
                        ToolMessage(
                            content="Interrupted by user.",
                            tool_call_id=remaining["id"],
                        )
                    )
                    messages_added += 1
                return TurnResult(
                    turn_id=0,
                    messages_added=messages_added,
                    tool_calls_made=tool_calls_made,
                    interrupted=True,
                )

            tool_name = tool_call["name"]
            tool_args = tool_call.get("args", {})
            tool_call_id = tool_call["id"]

            # Permission check
            approved = await callbacks.permission_check(tool_name, tool_args)
            if not approved:
                messages.append(
                    ToolMessage(
                        content="User denied this tool call.",
                        tool_call_id=tool_call_id,
                    )
                )
                messages_added += 1
                continue

            # Notify client
            await callbacks.on_tool_start(tool_name, tool_args, tool_call_id)

            # Execute tool
            tool = tool_map.get(tool_name)
            if tool is None:
                error_result = f"Tool '{tool_name}' not found"
                messages.append(
                    ToolMessage(content=error_result, tool_call_id=tool_call_id)
                )
                await callbacks.on_tool_result(tool_name, error_result, tool_call_id)
                messages_added += 1
                continue

            try:
                result = await tool.ainvoke(tool_args)
                result_str = str(result) if result is not None else ""
            except Exception as e:
                logger.warning(f"Tool {tool_name} failed: {e}")
                result_str = f"Tool execution error: {e}"

            messages.append(
                ToolMessage(content=result_str, tool_call_id=tool_call_id)
            )
            messages_added += 1
            tool_calls_made += 1

            await callbacks.on_tool_result(tool_name, result_str, tool_call_id)

            # Check for freeze request (e.g. sudo intercept → VM upgrade)
            if tool_context and callbacks.on_vm_upgrade_needed:
                freeze_req = tool_context.consume_freeze_request()
                if freeze_req and freeze_req.get("freeze_type") == "vm_upgrade_required":
                    await callbacks.on_vm_upgrade_needed(freeze_req)

        # Continue the inner loop — LLM sees tool results on next iteration

    return TurnResult(
        turn_id=0,
        messages_added=messages_added,
        tool_calls_made=tool_calls_made,
        metrics=turn_metrics,
    )
