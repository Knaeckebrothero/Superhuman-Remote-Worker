"""Persistent Agent — Interactive Loop.

Implements the while(tool_call) execution loop for persistent interactive
agents. No LangGraph, no phase alternation, no todos — just a plain async
loop that waits for user input, calls the LLM with tools, executes tool
calls (with permission checks), and repeats.

Reuses the same shared infrastructure as the worker graph:
- ContextManager for token counting and compaction
- Transient injection for memory and knowledge
- AuxiliaryLLM for summarization
- load_tools / ToolContext for tool loading
"""

import asyncio
import logging
import time
import uuid as _uuid
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

from .core.context import ContextManager, extract_summary_text, repair_tool_pairing
from .llm.reasoning_chat import extract_reasoning_text_from_block
from .llm.response_guards import (
    coerce_to_ai_message,
    finalize_streamed_response,
    strip_removal_markers,
)
from .services.image_content import extract_image_tags, make_multimodal_user_message

logger = logging.getLogger(__name__)


def _sanitize_ai_response(response: AIMessage) -> AIMessage:
    """Normalize AI message for Responses API compatibility.

    OpenRouter (and other non-OpenAI providers) may return ``null`` for
    message/block IDs in the Responses API output format.  When these
    messages are later included in the ``input`` array of a subsequent
    Responses API call, langchain-openai emits ``"id": null`` which the
    API rejects with a 400.

    This helper ensures every AIMessage has a valid ``id`` and that list-
    style content blocks carry a non-null ``id`` so the round-trip is
    valid.
    """
    if not isinstance(response, AIMessage):
        return response

    if not response.id:
        response.id = f"msg_{_uuid.uuid4().hex[:24]}"

    if isinstance(response.content, list):
        for block in response.content:
            if isinstance(block, dict) and block.get("id") is None:
                block["id"] = response.id

    return response


def _visible_content_len(content: Any) -> int:
    """Length of user-visible text across supported message content shapes."""
    if isinstance(content, str):
        return len(content.strip())
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block.strip())
            elif isinstance(block, dict) and block.get("text"):
                total += len(str(block["text"]).strip())
        return total
    return 0


def _ensure_msg_id(msg: Any) -> Any:
    """Stamp a stable id on a message that lacks one, at creation time.

    Every persisted ``thread_messages`` row needs a deterministic key so that
    incremental persistence and the turn-complete reconciliation pass upsert
    onto one row (``ON CONFLICT (id)``) instead of duplicating, and so a
    crash-resumed tail keeps the same ids. ``AIMessage`` already gets an id in
    ``_sanitize_ai_response``; ``HumanMessage``/``ToolMessage`` do not, so we
    stamp them here. Uses the same ``msg_`` prefix for a uniform id space.
    See docs/issues/persistent_session_midturn_message_loss.md.
    """
    if getattr(msg, "id", None) is None:
        msg.id = f"msg_{_uuid.uuid4().hex[:24]}"
    return msg


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

    # Notify client with tool result. The trailing ``is_error`` kwarg signals
    # tool-execution failures (tool not found, exception during ainvoke). The
    # transport may use it to render the call with an error treatment instead
    # of the success styling. Defaults to False for backwards compatibility
    # with older callers.
    on_tool_result: Callable[..., Awaitable[None]]

    # Ask client for permission to run a tool (returns True if approved).
    # tool_call_id lets the transport correlate the decision back to a
    # specific call so it can be persisted with the rest of the turn.
    permission_check: Callable[[str, Dict[str, Any], str], Awaitable[bool]]

    # Notify client of turn lifecycle events
    on_turn_start: Callable[[int], Awaitable[None]]
    on_turn_complete: Callable[[int, Optional[dict]], Awaitable[None]]

    # Stream a thinking/reasoning chunk to the client
    on_thinking: Callable[[str], Awaitable[None]]

    # Notify client of errors
    on_error: Callable[[str], Awaitable[None]]

    # Check if an interrupt was requested (non-blocking). Returns the
    # interrupt mode ("hard" | "graceful") or None if no interrupt is
    # pending. One-shot: reading consumes the flag.
    #
    # - "hard": cancel the in-flight LLM stream immediately, drop the
    #   partial AIMessage (don't append to messages). Set when the
    #   interrupt POST lands while no tool is mid-`ainvoke`.
    # - "graceful": let the current tool / stream finish, then exit at
    #   the next turn boundary. Set when the interrupt POST lands while
    #   a tool is mid-`ainvoke`. The accumulated partial AIMessage is
    #   preserved in messages.
    #
    # Legacy callers returning bool are accepted: any truthy non-None
    # value behaves like "graceful" (preserves partial response).
    check_interrupt: Callable[[], Optional[str]]

    # Notify client that a VM upgrade is needed (sudo detected, optional)
    on_vm_upgrade_needed: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None

    # Notify the transport that automatic summarization compacted the context,
    # so it can persist a display marker + show a banner. Optional.
    # Args: (summary_text, before_count, after_count).
    on_context_compacted: Optional[Callable[[str, int, int], Awaitable[None]]] = None

    # Persist a single message the instant the loop produces it (incremental
    # durability — a mid-turn crash keeps the tail instead of discarding the
    # whole in-flight turn). Called per AIMessage / ToolMessage as it is
    # appended to history; the turn-complete save reconciles (fills in
    # turn-level metrics / approval decisions via an idempotent upsert).
    # Optional: None ⇒ persist only at turn-complete (back-compat).
    persist_message: Optional[Callable[[Any], Awaitable[None]]] = None

    # Set by the transport alongside a "hard" interrupt so the loop can cancel
    # a blocked LLM / auxiliary await immediately — the cooperative
    # check_interrupt poll can't fire while the turn is parked in a network
    # read. Raced against the streaming and compaction awaits only; never
    # against tool execution (which must run to completion to avoid leaking
    # side effects — that path stays cooperative). Optional: None ⇒
    # cooperative-only interrupts (back-compat for callers that don't set it).
    hard_interrupt_event: Optional[asyncio.Event] = None


# Sentinel returned by _safe_anext on stream exhaustion. Avoids letting
# StopAsyncIteration escape a coroutine wrapped in a Task, where it interacts
# badly with the Future machinery.
_STREAM_DONE = object()


async def _safe_anext(aiter: Any) -> Any:
    """``__anext__`` that returns ``_STREAM_DONE`` on exhaustion instead of
    raising StopAsyncIteration, so the call can be wrapped in a Task safely."""
    try:
        return await aiter.__anext__()
    except StopAsyncIteration:
        return _STREAM_DONE


async def _stream_next_or_hard_interrupt(
    aiter: Any, hard_event: Optional[asyncio.Event]
) -> tuple[Any, str]:
    """Pull the next chunk from an LLM stream, abandoning the read if a hard
    interrupt fires first.

    Returns ``(chunk, status)`` where status is one of:
      - ``"chunk"``     — ``chunk`` is the next streamed item.
      - ``"stop"``      — the stream is exhausted.
      - ``"interrupt"`` — a hard interrupt fired; the in-flight read was
                          cancelled so a hung network read is torn down at once.
    """
    if hard_event is None:
        result = await _safe_anext(aiter)
        return (None, "stop") if result is _STREAM_DONE else (result, "chunk")

    nxt = asyncio.ensure_future(_safe_anext(aiter))
    intr = asyncio.ensure_future(hard_event.wait())
    try:
        await asyncio.wait({nxt, intr}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not intr.done():
            intr.cancel()
        try:
            await intr
        except asyncio.CancelledError:
            pass

    if nxt.done() and not nxt.cancelled():
        result = nxt.result()
        return (None, "stop") if result is _STREAM_DONE else (result, "chunk")

    # Interrupt won the race — cancel the in-flight chunk read.
    nxt.cancel()
    try:
        await nxt
    except asyncio.CancelledError:
        pass
    return None, "interrupt"


async def _await_or_hard_interrupt(
    coro: Awaitable[Any], hard_event: Optional[asyncio.Event]
) -> tuple[Any, bool]:
    """Await ``coro``, abandoning it if ``hard_event`` fires first.

    Returns ``(result, interrupted)``. On interrupt the in-flight coroutine is
    cancelled so a blocked network read (LLM / summarization) is torn down at
    once instead of parking until its own timeout. Used only for LLM /
    auxiliary awaits — never for tool execution.
    """
    if hard_event is None:
        return await coro, False

    op = asyncio.ensure_future(coro)
    intr = asyncio.ensure_future(hard_event.wait())
    try:
        await asyncio.wait({op, intr}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not intr.done():
            intr.cancel()
        try:
            await intr
        except asyncio.CancelledError:
            pass

    if op.done() and not op.cancelled():
        return op.result(), False  # re-raises coro's exception, if any

    op.cancel()
    try:
        await op
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    return None, True


async def run_persistent_loop(
    llm_with_tools: BaseChatModel,
    tools: List[Any],
    context_manager: ContextManager,
    config: Any,
    system_prompt: str,
    callbacks: PersistentLoopCallbacks,
    messages: List[BaseMessage],
    auxiliary_llm: Optional[Any] = None,
    recall_store: Optional[Any] = None,
    knowledge_store: Optional[Any] = None,
    project_id: Optional[str] = None,
    project_ids: Optional[List[str]] = None,
    tool_context: Optional[Any] = None,
    initial_turn_count: int = 0,
    get_current_tools: Optional[Callable[[], tuple]] = None,
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
        recall_store: RecallStore instance for memory injection/extraction
        knowledge_store: KnowledgeStore instance for knowledge injection
        project_id: Project UUID string for scoped knowledge queries (backward compat)
        project_ids: List of project UUID strings for multi-project sessions
        get_current_tools: Optional callback returning (llm_with_tools, tools) —
            called at the start of each turn to pick up tool set changes
            (e.g. plan mode toggle).
    """
    # Build tool lookup map
    tool_map: Dict[str, Any] = {tool.name: tool for tool in tools}
    turn_count = initial_turn_count
    llm_timeout = getattr(config.llm, "timeout", 600) or 600

    # Memory extraction config
    memory_config = getattr(config, "memory", None)
    extraction_interval = (
        getattr(memory_config, "extraction_interval", 5) if memory_config else 5
    )
    extraction_prompt = (
        getattr(memory_config, "extraction_prompt", "") if memory_config else ""
    )
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

        # Refresh after receiving input so config changes made during the wait
        # (e.g. model hot-swap via config.update, plan mode toggle) are picked
        # up before the turn executes. Refreshing before the wait captures a
        # stale LLM reference when the user changes models while idle.
        if get_current_tools:
            new_llm, new_tools = get_current_tools()
            llm_with_tools = new_llm
            tool_map = {tool.name: tool for tool in new_tools}

        turn_count += 1
        turn_id = turn_count
        messages.append(_ensure_msg_id(HumanMessage(content=user_input)))

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

        # Auto-commit workspace changes after tool-executing turns, then push
        # so the remote — which the version-history UI reads — stays current.
        # The push runs on EVERY turn (no turn-count throttle) and regardless
        # of whether this turn used tools, so commits can't be stranded locally
        # when a session ends on a no-tool turn. has_unpushed_commits() is a
        # local-ref check, so turns with nothing to push skip the network.
        # Commit/push failures are surfaced (warning) rather than swallowed:
        # unpushed commits live only on the workspace pod until a push succeeds.
        if tool_context:
            ws_mgr = getattr(tool_context, "workspace_manager", None)
            git_mgr = getattr(ws_mgr, "git_manager", None) if ws_mgr else None
            if git_mgr and git_mgr.is_active:
                if tool_calls_this_turn > 0:
                    try:
                        if git_mgr.has_uncommitted_changes():
                            if not git_mgr.commit(f"Auto-commit after turn {turn_id}"):
                                logger.warning(
                                    f"Turn {turn_id}: workspace auto-commit failed"
                                )
                    except Exception:
                        logger.warning(
                            f"Turn {turn_id}: workspace auto-commit raised",
                            exc_info=True,
                        )
                try:
                    if git_mgr.has_unpushed_commits():
                        if not git_mgr.push():
                            logger.warning(
                                f"Turn {turn_id}: workspace git push failed — "
                                "unpushed commits remain only on the workspace "
                                "pod and will not appear in the version history "
                                "until a later push succeeds"
                            )
                except Exception:
                    logger.warning(
                        f"Turn {turn_id}: workspace git push raised",
                        exc_info=True,
                    )

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

    async def _persist(msg: Any) -> None:
        """Persist a message the instant it's appended to history.

        Incremental durability: a crash mid-turn keeps everything produced so
        far instead of discarding the whole in-flight turn. No-op when the
        transport didn't wire ``persist_message``; the turn-complete save
        reconciles either way.
        """
        if callbacks.persist_message is not None:
            await callbacks.persist_message(msg)

    # --- Memory retrieval (once per turn, before the inner loop) ---
    memory_block = ""
    knowledge_block = ""

    # Memory/knowledge retrieval with timeout — must never block the LLM call
    _RETRIEVAL_TIMEOUT = 5  # seconds

    if recall_store:
        try:
            await asyncio.wait_for(
                recall_store.decrement_ttl(), timeout=_RETRIEVAL_TIMEOUT
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"TTL decrement failed (non-fatal): {e}")

        try:
            context_text = ""
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage):
                    context_text = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                    break

            memories = await asyncio.wait_for(
                recall_store.retrieve(context_text), timeout=_RETRIEVAL_TIMEOUT
            )
            if memories:
                from .services.recall_store import RecallStore as _RS

                memory_block = _RS.assemble_memory_block(
                    memories, model=getattr(config.llm, "model", None)
                )
                logger.debug(f"Memory injection: {len(memories)} memories retrieved")
        except asyncio.TimeoutError:
            logger.warning("Memory retrieval timed out — skipping injection")
        except Exception as e:
            # Log the exception type so this stops being guesswork — bare
            # `e` for openai.APIConnectionError formats as "Connection error."
            # with no detail. See
            # docs/issues/persistent_graph_misleading_embedding_connection_error.md
            logger.warning(
                "Memory retrieval failed (non-fatal): %s: %s",
                type(e).__name__,
                e,
            )

    effective_pids = project_ids or ([project_id] if project_id else [])
    if knowledge_store and effective_pids:
        try:
            import uuid as _uuid

            kb_context = ""
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage):
                    kb_context = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
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

                knowledge_block = _KS.assemble_knowledge_block(
                    kb_notes, model=getattr(config.llm, "model", None)
                )
                logger.debug(f"Knowledge injection: {len(kb_notes)} notes retrieved")
        except asyncio.TimeoutError:
            logger.warning("Knowledge retrieval timed out — skipping injection")
        except Exception as e:
            # See sibling memory-retrieval handler above for rationale.
            logger.warning(
                "Knowledge retrieval failed (non-fatal): %s: %s",
                type(e).__name__,
                e,
            )

    while True:
        # Check for interrupt before LLM call
        if callbacks.check_interrupt():
            return TurnResult(
                turn_id=0,
                messages_added=messages_added,
                tool_calls_made=tool_calls_made,
                interrupted=True,
            )

        prepared = list(messages)

        # Inject memory and knowledge as transient tool-call pairs
        if memory_block:
            try:
                from .core.memory_injection import create_memory_injection_messages

                mem_ai, mem_tool = create_memory_injection_messages(memory_block)
                # Insert after the system message, before conversation
                inject_idx = (
                    1 if prepared and isinstance(prepared[0], SystemMessage) else 0
                )
                prepared.insert(inject_idx, mem_ai)
                prepared.insert(inject_idx + 1, mem_tool)
            except Exception as e:
                logger.warning(f"Memory injection failed (non-fatal): {e}")

        if knowledge_block:
            try:
                from .core.knowledge_injection import (
                    create_knowledge_injection_messages,
                )

                kb_ai, kb_tool = create_knowledge_injection_messages(knowledge_block)
                # Insert after memory injection
                inject_idx = (
                    len(prepared)
                    - len(messages)
                    + (1 if prepared and isinstance(prepared[0], SystemMessage) else 0)
                )
                prepared.insert(inject_idx, kb_ai)
                prepared.insert(inject_idx + 1, kb_tool)
            except Exception as e:
                logger.warning(f"Knowledge injection failed (non-fatal): {e}")

        # Context compaction if needed. Raced against a hard interrupt: the
        # summarization LLM call here is the worst offender for parking the
        # turn (a hung endpoint can hold it for the full auxiliary timeout),
        # so a hard "stop" must be able to tear it down at once.
        pre_compact_len = len(prepared)
        prepared, compact_interrupted = await _await_or_hard_interrupt(
            context_manager.ensure_within_limits(
                prepared,
                auxiliary_llm,
                max_summary_length=getattr(
                    config.context_management, "max_summary_length", 10000
                ),
            ),
            callbacks.hard_interrupt_event,
        )
        if compact_interrupted:
            logger.info("Hard interrupt during context compaction — ending turn")
            callbacks.check_interrupt()  # consume the flag + clear the event
            return TurnResult(
                turn_id=0,
                messages_added=messages_added,
                tool_calls_made=tool_calls_made,
                interrupted=True,
            )
        # ensure_within_limits returns a LangGraph reducer delta (RemoveMessage
        # markers + summary + fresh copies). This loop has no reducer to apply
        # them; left in, the markers reach _convert_message_to_dict and raise
        # "Got unknown type" → "malformed response". The worker graph strips
        # them too (src/graph.py). Strip before the compaction-checkpoint check
        # so len(prepared) reflects the real compacted message count.
        prepared = strip_removal_markers(prepared)

        # Auto-compaction happened this turn — surface it for display, then
        # commit + push the workspace to Gitea as a checkpoint.
        if len(prepared) < pre_compact_len:
            summary_text = extract_summary_text(prepared)
            if summary_text and callbacks.on_context_compacted:
                try:
                    await callbacks.on_context_compacted(
                        summary_text, pre_compact_len, len(prepared)
                    )
                except Exception as e:
                    logger.debug(f"on_context_compacted failed (non-fatal): {e}")
            if tool_context:
                ws_mgr = getattr(tool_context, "workspace_manager", None)
                git_mgr = getattr(ws_mgr, "git_manager", None) if ws_mgr else None
                if git_mgr and git_mgr.is_active:
                    try:
                        if git_mgr.has_uncommitted_changes():
                            git_mgr.commit(
                                f"Auto-compaction checkpoint ({pre_compact_len} → {len(prepared)} msgs)"
                            )
                        git_mgr.push()
                    except Exception as e:
                        logger.debug(
                            f"Git push on auto-compaction failed (non-fatal): {e}"
                        )

        # Repair tool-call pairing before the LLM call. Compaction thrash, an
        # interrupted turn, or streamed parallel-tool corruption (langchain
        # #34660) can leave a function_call_output without its function_call
        # (or vice versa); the Responses API rejects that with a 400
        # "No tool call found for function call output ...". The worker graph
        # sanitizes at the same point (src/graph.py:867); the resume path
        # repairs on restore (persistent_app). This is the equivalent guard for
        # the live turn loop, which previously had none.
        prepared = repair_tool_pairing(prepared)

        # --- LLM call with streaming ---
        response_content = ""
        response: Optional[AIMessage] = None
        llm_start = time.monotonic()

        try:
            try:
                # Try astream for token-by-token streaming
                chunks = []
                # Holds the interrupt mode ("hard"|"graceful") if the loop
                # below broke early; None otherwise. Legacy bool callbacks
                # land as True here and are treated as "graceful".
                streaming_interrupted: Any = None
                # Manual iteration (vs. `async for`) so a hard interrupt can
                # cancel a hung chunk read mid-stream instead of waiting for
                # the next chunk to arrive before the cooperative check below.
                _stream = llm_with_tools.astream(prepared)
                _aiter = _stream.__aiter__()
                while True:
                    chunk, _stream_status = await _stream_next_or_hard_interrupt(
                        _aiter, callbacks.hard_interrupt_event
                    )
                    if _stream_status == "interrupt":
                        logger.info(
                            "Hard interrupt during LLM streaming — cancelling stream"
                        )
                        callbacks.check_interrupt()  # consume flag + clear event
                        streaming_interrupted = "hard"
                        break
                    if _stream_status == "stop":
                        break
                    chunks.append(chunk)
                    # Extract and stream text content
                    if hasattr(chunk, "content") and chunk.content:
                        content = chunk.content
                        # Anthropic returns content as list of dicts
                        if isinstance(content, list):
                            for block in content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "text"
                                ):
                                    text = block.get("text", "")
                                    if text:
                                        response_content += text
                                        await callbacks.on_token(text)
                                elif (
                                    isinstance(block, dict)
                                    and block.get("type") == "thinking"
                                ):
                                    thinking = block.get("thinking", "")
                                    if thinking:
                                        await callbacks.on_thinking(thinking)
                                elif (
                                    isinstance(block, dict)
                                    and block.get("type") == "reasoning"
                                ):
                                    reasoning = extract_reasoning_text_from_block(block)
                                    if reasoning:
                                        await callbacks.on_thinking(reasoning)
                                elif isinstance(block, str) and block:
                                    response_content += block
                                    await callbacks.on_token(block)
                        elif isinstance(content, str) and content:
                            response_content += content
                            await callbacks.on_token(content)

                    # Check for mid-stream interrupt. Capture the mode so
                    # the partial-response handler below can drop (hard)
                    # vs keep (graceful) the AIMessage. Use truthy check —
                    # legacy callers returning bool False count as "no
                    # interrupt" alongside the new-API None.
                    interrupt_mode = callbacks.check_interrupt()
                    if interrupt_mode:
                        logger.info(
                            "Interrupt received during LLM streaming (mode=%s)",
                            interrupt_mode,
                        )
                        streaming_interrupted = interrupt_mode
                        break

                # Concatenate all chunks into final response
                if chunks:
                    response = chunks[0]
                    for chunk in chunks[1:]:
                        response = response + chunk

                # Streaming bug workaround: some Responses API endpoints
                # don't send function_call_arguments.delta events, so
                # streamed tool calls end up with empty args {}.  Detect
                # this and retry with ainvoke to get correct args.
                # Only applies to Responses API models (reasoning attr).
                if (
                    response
                    and getattr(llm_with_tools, "reasoning", None)
                    and getattr(response, "tool_calls", None)
                    and any(not tc.get("args") for tc in response.tool_calls)
                ):
                    logger.info(
                        "Streaming produced tool calls with empty args — "
                        "retrying with ainvoke"
                    )
                    response_content = ""
                    response = await asyncio.wait_for(
                        llm_with_tools.ainvoke(prepared),
                        timeout=llm_timeout,
                    )
                    content = getattr(response, "content", None)
                    if content:
                        if isinstance(content, list):
                            for block in content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "text"
                                ):
                                    text = block.get("text", "")
                                    if text:
                                        response_content += text
                                        await callbacks.on_token(text)
                                elif (
                                    isinstance(block, dict)
                                    and block.get("type") == "thinking"
                                ):
                                    thinking = block.get("thinking", "")
                                    if thinking:
                                        await callbacks.on_thinking(thinking)
                                elif (
                                    isinstance(block, dict)
                                    and block.get("type") == "reasoning"
                                ):
                                    reasoning = extract_reasoning_text_from_block(block)
                                    if reasoning:
                                        await callbacks.on_thinking(reasoning)
                                elif isinstance(block, str) and block:
                                    response_content += block
                                    await callbacks.on_token(block)
                        elif isinstance(content, str) and content:
                            response_content = content
                            await callbacks.on_token(content)
                    else:
                        extra = getattr(response, "additional_kwargs", None) or {}
                        refusal = extra.get("refusal")
                        logger.warning(
                            "ainvoke retry returned empty content "
                            "(type=%s, has_tool_calls=%s, additional_kwargs=%s)",
                            type(content).__name__,
                            bool(getattr(response, "tool_calls", None)),
                            list(extra.keys()),
                        )
                        if refusal:
                            logger.warning("Model refusal: %s", refusal)
                            response_content = (
                                f"⚠ The model declined to respond: {refusal}"
                            )
                            await callbacks.on_token(response_content)
                        elif not getattr(response, "tool_calls", None):
                            from src.services.guardrails import format_nudge

                            response_content = format_nudge(
                                "empty_response_recovery",
                                model=getattr(config.llm, "model", None),
                            )
                            await callbacks.on_token(response_content)

                # Handle mid-stream interruption. "hard" drops the partial
                # AIMessage entirely (user said cancel-immediately, the
                # half-typed assistant text shouldn't live in history).
                # Any other truthy mode (graceful, or a legacy bool True)
                # preserves the partial response so the work is visible.
                if streaming_interrupted:
                    if streaming_interrupted == "hard":
                        logger.info(
                            "Hard interrupt: dropping partial AIMessage "
                            "(%d chars accumulated)",
                            len(response_content),
                        )
                    elif response:
                        # graceful (or legacy bool True): strip incomplete
                        # tool calls from partial response and keep it — but
                        # only if it carries real content. An empty partial is
                        # a raw streaming chunk that, left in history, makes the
                        # next turn's request serialization raise "Got unknown
                        # type" (see persistent_session_empty_chunk_history_
                        # corruption). finalize_streamed_response coerces the
                        # chunk to a concrete AIMessage and drops it if empty.
                        if hasattr(response, "tool_calls"):
                            response.tool_calls = []
                        if hasattr(response, "invalid_tool_calls"):
                            response.invalid_tool_calls = []
                        final = finalize_streamed_response(response)
                        if final is not None:
                            messages.append(final)
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
                    logger.info(
                        f"Streaming not supported ({err_name}), falling back to ainvoke"
                    )
                    response = await llm_with_tools.ainvoke(prepared)
                    # Stream the complete response as a single chunk
                    content = getattr(response, "content", None)
                    if content:
                        if isinstance(content, list):
                            for block in content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "text"
                                ):
                                    text = block.get("text", "")
                                    if text:
                                        response_content += text
                                        await callbacks.on_token(text)
                                elif (
                                    isinstance(block, dict)
                                    and block.get("type") == "thinking"
                                ):
                                    thinking = block.get("thinking", "")
                                    if thinking:
                                        await callbacks.on_thinking(thinking)
                                elif (
                                    isinstance(block, dict)
                                    and block.get("type") == "reasoning"
                                ):
                                    reasoning = extract_reasoning_text_from_block(block)
                                    if reasoning:
                                        await callbacks.on_thinking(reasoning)
                                elif isinstance(block, str) and block:
                                    response_content += block
                                    await callbacks.on_token(block)
                        elif isinstance(content, str) and content:
                            response_content = content
                            await callbacks.on_token(content)
                    else:
                        extra = getattr(response, "additional_kwargs", None) or {}
                        refusal = extra.get("refusal")
                        logger.warning(
                            "ainvoke fallback returned empty content "
                            "(type=%s, has_tool_calls=%s, additional_kwargs=%s)",
                            type(content).__name__,
                            bool(getattr(response, "tool_calls", None)),
                            list(extra.keys()),
                        )
                        if refusal:
                            logger.warning("Model refusal: %s", refusal)
                            response_content = (
                                f"⚠ The model declined to respond: {refusal}"
                            )
                            await callbacks.on_token(response_content)
                        else:
                            response_content = (
                                "⚠ The model returned an empty response. "
                                "Please try again or switch models."
                            )
                            await callbacks.on_token(response_content)
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
        if (
            response
            and hasattr(response, "response_metadata")
            and response.response_metadata
        ):
            meta = response.response_metadata
            token_usage = meta.get("token_usage", {})
            turn_metrics = {
                "input_tokens": token_usage.get("input_tokens")
                or token_usage.get("prompt_tokens"),
                "output_tokens": token_usage.get("output_tokens")
                or token_usage.get("completion_tokens"),
                "reasoning_tokens": token_usage.get("reasoning_tokens"),
                "latency_ms": llm_latency_ms,
                "model": meta.get("model_name"),
            }
            turn_metrics = {k: v for k, v in turn_metrics.items() if v is not None}

        # Send reasoning from additional_kwargs if not already streamed
        # (covers DeepSeek, OpenRouter, and other non-Anthropic reasoning models)
        if response:
            extra = getattr(response, "additional_kwargs", None) or {}
            reasoning = extra.get("reasoning_content")
            if reasoning and isinstance(reasoning, str):
                await callbacks.on_thinking(reasoning)

        if response is None:
            return TurnResult(
                turn_id=0,
                messages_added=messages_added,
                tool_calls_made=tool_calls_made,
                error="Empty LLM response",
            )

        # Detect streaming that produced no visible content and no tool calls
        if not response_content and (
            not hasattr(response, "tool_calls") or not response.tool_calls
        ):
            extra = getattr(response, "additional_kwargs", None) or {}
            refusal = extra.get("refusal")
            logger.warning(
                "Streaming produced empty content "
                "(type=%s, has_tool_calls=%s, additional_kwargs=%s)",
                type(getattr(response, "content", None)).__name__,
                bool(getattr(response, "tool_calls", None)),
                list(extra.keys()),
            )
            if refusal:
                logger.warning("Model refusal: %s", refusal)
                response_content = f"⚠ The model declined to respond: {refusal}"
                await callbacks.on_token(response_content)
            else:
                response_content = (
                    "⚠ The model returned an empty response. "
                    "Please try again or switch models."
                )
                await callbacks.on_token(response_content)

        # Coerce a streamed chunk to a concrete AIMessage before it enters
        # history (the next turn's request serialization rejects raw chunk
        # types), then sanitize for Responses API compatibility (null IDs
        # from OpenRouter).
        response = _sanitize_ai_response(coerce_to_ai_message(response))
        if (
            response_content
            and not getattr(response, "tool_calls", None)
            and _visible_content_len(response.content) == 0
        ):
            response.content = response_content

        # Add AI response to message history
        messages.append(response)
        messages_added += 1
        # Persist the LLM step immediately — it carries the reasoning + tool
        # calls and is the expensive bit to lose on a mid-turn crash.
        await _persist(response)

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
                        _ensure_msg_id(
                            ToolMessage(
                                content="Interrupted by user.",
                                tool_call_id=remaining["id"],
                            )
                        )
                    )
                    messages_added += 1
                    await _persist(messages[-1])
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
            approved = await callbacks.permission_check(
                tool_name, tool_args, tool_call_id
            )
            if not approved:
                messages.append(
                    _ensure_msg_id(
                        ToolMessage(
                            content="User denied this tool call.",
                            tool_call_id=tool_call_id,
                        )
                    )
                )
                messages_added += 1
                await _persist(messages[-1])
                continue

            # Notify client
            await callbacks.on_tool_start(tool_name, tool_args, tool_call_id)

            # Execute tool
            tool = tool_map.get(tool_name)
            if tool is None:
                error_result = f"Tool '{tool_name}' not found"
                messages.append(
                    _ensure_msg_id(
                        ToolMessage(content=error_result, tool_call_id=tool_call_id)
                    )
                )
                await callbacks.on_tool_result(
                    tool_name, error_result, tool_call_id, is_error=True
                )
                messages_added += 1
                await _persist(messages[-1])
                continue

            is_error = False
            try:
                result = await tool.ainvoke(tool_args)
                result_str = str(result) if result is not None else ""
            except Exception as e:
                logger.warning(f"Tool {tool_name} failed: {e}")
                result_str = f"Tool execution error: {e}"
                is_error = True

            # Multimodal image delivery: if the tool result embedded a
            # `<image_data>` / `<page_image>` tag, strip it and attach the
            # image as a real provider content block on a follow-up
            # HumanMessage so multimodal primary models actually see it.
            cleaned_str, extracted_images = extract_image_tags(result_str)

            messages.append(
                _ensure_msg_id(
                    ToolMessage(content=cleaned_str, tool_call_id=tool_call_id)
                )
            )
            messages_added += 1
            tool_calls_made += 1
            await _persist(messages[-1])

            if extracted_images:
                messages.append(
                    _ensure_msg_id(
                        make_multimodal_user_message(
                            text=(f"Image content from tool call {tool_call_id}:"),
                            images=extracted_images,
                        )
                    )
                )
                messages_added += 1
                await _persist(messages[-1])

            await callbacks.on_tool_result(
                tool_name, cleaned_str, tool_call_id, is_error=is_error
            )

            # Check for freeze request (e.g. sudo intercept → VM upgrade)
            if tool_context and callbacks.on_vm_upgrade_needed:
                freeze_req = tool_context.consume_freeze_request()
                if (
                    freeze_req
                    and freeze_req.get("freeze_type") == "vm_upgrade_required"
                ):
                    await callbacks.on_vm_upgrade_needed(freeze_req)

        # Continue the inner loop — LLM sees tool results on next iteration

    return TurnResult(
        turn_id=0,
        messages_added=messages_added,
        tool_calls_made=tool_calls_made,
        metrics=turn_metrics,
    )
