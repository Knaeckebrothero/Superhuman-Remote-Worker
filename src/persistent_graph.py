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
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from .core.context import (
    ContextManager,
    extract_summary_text,
    repair_tool_call_arguments,
    repair_tool_pairing,
    scrub_history_tool_call_arguments,
)
from .core.summarizer import count_text_tokens
from .core.workspace_backend import WorkspaceUnavailableError
from .core.workspace_injection import find_tail_injection_anchor
from .llm.exceptions import ContextOverflowError
from .llm.reasoning_chat import (
    _STREAM_REASONING_SINK,
    extract_reasoning_text_from_block,
)
from .llm.response_guards import (
    coerce_to_ai_message,
    finalize_streamed_response,
    strip_removal_markers,
)
from .services.image_content import (
    extract_image_tags,
    make_multimodal_user_message,
    resolve_image_max_edge,
)

logger = logging.getLogger(__name__)


def _injection_anchor_index(messages: List[BaseMessage]) -> int:
    """Index at which to insert transient context-injection pairs — the tail.

    The injected pairs change every turn (fresh retrieval per request), and
    provider prompt caches match on a strict left-to-right prefix: the legacy
    after-the-first-user-turn anchor placed the churning block ahead of the
    whole conversation history, invalidating the cached prefix at that point
    on every request — the entire history was re-processed at full input
    price each turn. Anchoring at the tail keeps the history prefix
    byte-identical between turns so the cache is reused; only the small
    injected block (past the divergence point anyway) is processed fresh.

    Gemini constraint (unchanged from the legacy anchor's rationale):
    Gemini's native API rejects a function-call turn that does not
    immediately follow a user or function-response turn — it 400s with
    "Please ensure that function call turn comes immediately after a user
    turn or after a function response turn." The turn loop only reaches the
    LLM with a history ending in the newest ``HumanMessage`` (start of turn)
    or the ``ToolMessage``s of the previous inner-loop iteration, so the
    tail normally IS such a position; the shared anchor walks back past any
    trailing bare model turn for degenerate/restored histories. See
    ``find_tail_injection_anchor``.
    """
    return find_tail_injection_anchor(messages)


def _inject_context_pairs(
    prepared: List[BaseMessage],
    manager_injection: List[BaseMessage],
    memory_block: str,
    knowledge_block: str,
    citation_feedback_block: str = "",
    *,
    product_guide_turn_boundary: str = "",
) -> int:
    """Insert transient memory/knowledge/citation context pairs into ``prepared``.

    Mutates ``prepared`` in place and returns the number of messages inserted.
    The pairs are anchored at the tail (see ``_injection_anchor_index``) so the
    stable history prefix stays byte-identical between turns for provider
    prompt caches, while remaining valid for providers that enforce
    function-call turn ordering (Gemini). When the managed App Guide is live,
    a runtime-owned HumanMessage follows the durable turn and any transient
    block. This restores the current user request as the final instruction
    even on calls without recalled context. Memory, knowledge, and
    citation-feedback injection failures are non-fatal — the turn proceeds
    without that context.

    The same message objects may be reused across inner-loop iterations; pair
    ids are only prefix-checked downstream.
    """
    injected_count = 0
    base_inject_idx = _injection_anchor_index(prepared)

    if manager_injection:
        prepared[base_inject_idx:base_inject_idx] = manager_injection
        injected_count += len(manager_injection)

    if memory_block:
        try:
            from .core.memory_injection import create_memory_injection_messages

            mem_ai, mem_tool = create_memory_injection_messages(memory_block)
            # Front of the injection zone, before the manager pairs (legacy
            # order preserved).
            prepared.insert(base_inject_idx, mem_ai)
            prepared.insert(base_inject_idx + 1, mem_tool)
            injected_count += 2
        except Exception as e:
            logger.warning(f"Memory injection failed (non-fatal): {e}")

    if knowledge_block:
        try:
            from .core.knowledge_injection import (
                create_knowledge_injection_messages,
            )

            kb_ai, kb_tool = create_knowledge_injection_messages(knowledge_block)
            # After all prior injections.
            prepared.insert(base_inject_idx + injected_count, kb_ai)
            prepared.insert(base_inject_idx + injected_count + 1, kb_tool)
            injected_count += 2
        except Exception as e:
            logger.warning(f"Knowledge injection failed (non-fatal): {e}")

    if citation_feedback_block:
        try:
            from .core.citation_feedback_injection import (
                create_citation_feedback_injection_messages,
            )

            cit_ai, cit_tool = create_citation_feedback_injection_messages(
                citation_feedback_block
            )
            # After memory/knowledge — matches the worker injection order.
            prepared.insert(base_inject_idx + injected_count, cit_ai)
            prepared.insert(base_inject_idx + injected_count + 1, cit_tool)
            injected_count += 2
        except Exception as e:
            logger.warning(f"Citation feedback injection failed (non-fatal): {e}")

    if product_guide_turn_boundary:
        # A HumanMessage is deliberate: several providers/models give a
        # trailing conversation answer or synthetic tool result more weight
        # than the earlier system floor and current user request. Keep any
        # function-call/result pairs valid, then finish the ephemeral request
        # with a current-digest instruction. The boundary is never written to
        # durable history.
        prepared.insert(
            base_inject_idx + injected_count,
            HumanMessage(content=product_guide_turn_boundary),
        )
        injected_count += 1

    return injected_count


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

# additional_kwargs key by which a message declares the thread_messages.role it
# should be PERSISTED under, when its LangChain type is only a carrier. An
# injected system notice travels as HumanMessage — so this loop, the turn
# reconciler's backwards walk, and the model's context all stay unchanged — but
# must persist as role='event' rather than as a user bubble. Defined here (the
# lower layer) so the transport and the loop cannot drift apart on the spelling.
PERSIST_ROLE_KEY = "_srw_persist_role"


def _user_facing_turn_error(e: BaseException) -> str:
    """Map a turn-killing exception to a message worth showing the user.

    The cockpit sanitizes raw backend strings into a generic "something went
    wrong", so anything actionable has to be phrased here. Context overflows
    are the common deterministic case: the capture client surfaces them as a
    synthetic 413 with ``code: context_overflow``
    (session_silent_failure_audit.md #3), or — defensively — as a typed
    ContextOverflowError.
    """
    cause = getattr(e, "__cause__", None)
    if isinstance(e, WorkspaceUnavailableError) or isinstance(
        cause, WorkspaceUnavailableError
    ):
        return (
            "Your workspace became unavailable and is being recovered. "
            "Resend your message in a moment to reconnect."
        )
    overflow = next(
        (x for x in (e, cause) if isinstance(x, ContextOverflowError)), None
    )
    if overflow is not None:
        return (
            f"The conversation no longer fits the model's context window "
            f"({overflow.token_count:,} tokens vs a {overflow.limit:,}-token "
            f"limit) and compaction could not shrink it. Start a new session "
            f"for this task, or switch to a larger-context model."
        )
    if "context_overflow" in str(e):
        return (
            "The conversation no longer fits the model's context window and "
            f"compaction could not shrink it. ({e}) Start a new session for "
            "this task, or switch to a larger-context model."
        )
    return str(e)


def _maybe_estimate_reasoning_tokens(turn_metrics: dict, reasoning_text: str) -> None:
    """Backfill a reasoning-token figure from captured reasoning *text*.

    Some models stream/emit reasoning content but no provider reasoning-token
    count (gemma via the vLLM router folds reasoning into ``output_tokens``).
    When ``turn_metrics`` carries no ``reasoning_tokens`` but we hold the
    reasoning text, tokenize it ourselves and mark it estimated. The estimate is
    a SUBSET of ``output_tokens`` (never additive). Mutates ``turn_metrics`` in
    place; a provider-reported count or empty text is a no-op.
    """
    if turn_metrics.get("reasoning_tokens"):
        return
    if not reasoning_text:
        return
    est = count_text_tokens(reasoning_text, turn_metrics.get("model"))
    if est > 0:
        # Reasoning is a subset of output_tokens; our tiktoken estimate uses a
        # different tokenizer than the model, so clamp it so the UI never shows
        # reasoning larger than the output it lives inside.
        out = turn_metrics.get("output_tokens")
        if isinstance(out, int) and out > 0:
            est = min(est, out)
        turn_metrics["reasoning_tokens"] = est
        turn_metrics["reasoning_estimated"] = True


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
    # True when the turn stopped on an unanswered permission gate. The tool
    # calls are neither run nor refused — the durable pending request resumes
    # them once the user answers.
    awaiting_permission: bool = False


class PermissionOutcome(str, Enum):
    """Outcome of a supervised permission gate.

    A gate is a *question to the user*, so "no answer yet" is a third,
    distinct state — collapsing it into DECLINED fabricates a refusal the
    user never made (the model then concludes it was denied and abandons
    real work). See
    docs/issues/supervised_parallel_gates_timeout_fabricates_denial.md.
    """

    APPROVED = "approved"
    DECLINED = "declined"  # the user actually said no
    NO_ANSWER = "no_answer"  # never answered — NOT consent, NOT a refusal

    @classmethod
    def coerce(cls, value: Any) -> "PermissionOutcome":
        """Normalize a callback result, tolerating legacy bools."""
        if isinstance(value, cls):
            return value
        return cls.APPROVED if value else cls.DECLINED


@dataclass
class PersistentLoopCallbacks:
    """Callbacks wiring the loop to the transport layer (WebSocket).

    All callbacks are async. The loop is transport-agnostic — it only
    communicates through these callbacks.
    """

    # Wait for the next user message (blocks until available). Returns either
    # a plain string (sentinels, legacy callers) or a dict
    # ``{"content": str, "id": str}`` whose id is the thread_messages row the
    # accept-time persist already wrote (session_silent_failure_audit.md #1).
    get_user_input: Callable[[], Awaitable[Any]]

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

    # Ask client for permission to run a tool. Returns a PermissionOutcome —
    # or, for legacy callers, a plain bool (True == APPROVED, False ==
    # DECLINED). tool_call_id lets the transport correlate the decision back
    # to a specific call so it can be persisted with the rest of the turn.
    #
    # NO_ANSWER is NOT a denial: the gate was never answered (timed out, or
    # the approval card never reached the browser). The loop parks the turn
    # instead of telling the model the user refused — see
    # docs/issues/supervised_parallel_gates_timeout_fabricates_denial.md.
    permission_check: Callable[
        [str, Dict[str, Any], str], Awaitable[Union["PermissionOutcome", bool]]
    ]

    # Notify client of turn lifecycle events
    on_turn_start: Callable[[int], Awaitable[None]]
    on_turn_complete: Callable[[int, Optional[dict]], Awaitable[None]]

    # Stream a thinking/reasoning chunk to the client. Accepts an optional
    # ``message_id`` kwarg correlating the frame to the AI message it belongs
    # to, so the client can dedupe a reasoning frame replayed after history
    # already painted the bubble (the gemma "reasoning duplicates on replay"
    # bug). Older callers taking only ``content`` still work.
    on_thinking: Callable[..., Awaitable[None]]

    # Notify client of errors. Accepts an optional ``turn_id`` kwarg so the
    # transport can close the failed turn in the UI and persist the error
    # (session_silent_failure_audit.md #2); older transports ignore it.
    on_error: Callable[..., Awaitable[None]]

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

    # Notify the client that a workspace upgrade is available. Fires for BOTH a
    # sandbox sudo intercept asking for a VM (freeze_type=vm_upgrade_required)
    # and a lite agent's request_workspace_upgrade asking for a sandbox
    # (freeze_type=workspace_upgrade_required, workspace_tier_upgrade.md §4.2 S5).
    # The freeze_data carries freeze_type + (target_tier|command) + reason.
    on_workspace_upgrade_needed: Optional[
        Callable[[Dict[str, Any]], Awaitable[None]]
    ] = None

    # Deprecated alias for on_workspace_upgrade_needed (the original sudo→VM
    # name). Kept so older constructors / tests keep working; reconciled into
    # on_workspace_upgrade_needed by __post_init__.
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

    # Audit one main-LLM call (messages, response, metrics) to the
    # llm_requests trail. Sync callback — the transport schedules its own
    # background write (session_silent_failure_audit.md #14).
    archive_llm_call: Optional[Callable[..., None]] = None

    # Live token telemetry, fired after each main-LLM call with that call's
    # usage ({input_tokens, output_tokens, reasoning_tokens?, model?,
    # ctx_limit_tokens}). input_tokens of the latest call ≈ current context
    # size, which drives the cockpit's CTX gauge
    # (docs/features/context_summarization_rework.md S5). Optional.
    on_usage: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None

    # Set by the transport alongside a "hard" interrupt so the loop can cancel
    # a blocked LLM / auxiliary await immediately — the cooperative
    # check_interrupt poll can't fire while the turn is parked in a network
    # read. Raced against the streaming and compaction awaits only; never
    # against tool execution (which must run to completion to avoid leaking
    # side effects — that path stays cooperative). Optional: None ⇒
    # cooperative-only interrupts (back-compat for callers that don't set it).
    hard_interrupt_event: Optional[asyncio.Event] = None

    # Tell the client to DROP the in-progress reasoning bubble for a message id.
    # Used by the empty-response retry to REPLACE a dead-end reasoning stream
    # (the model streamed reasoning then emitted no answer) with the retry's
    # reasoning + answer, rather than leaving the stale bubble and appending the
    # answer underneath. Optional: None ⇒ no live replace (back-compat for
    # tests/older transports); the retry still runs, the stale bubble just
    # lingers until the next rerender (the persisted row is already the retry's,
    # so a reload is coherent regardless). Takes a ``message_id`` kwarg matching
    # the id on_thinking stamped on the reasoning frames. See
    # docs/issues/session_empty_response_gpt5_codex_stop.md.
    on_thinking_reset: Optional[Callable[..., Awaitable[None]]] = None

    def __post_init__(self) -> None:
        # Back-compat: callers that still pass the deprecated on_vm_upgrade_needed
        # get it promoted to the generalized on_workspace_upgrade_needed the loop
        # actually reads (workspace_tier_upgrade.md §4.2 S5).
        if (
            self.on_workspace_upgrade_needed is None
            and self.on_vm_upgrade_needed is not None
        ):
            self.on_workspace_upgrade_needed = self.on_vm_upgrade_needed


# Sentinel returned by _safe_anext on stream exhaustion. Avoids letting
# StopAsyncIteration escape a coroutine wrapped in a Task, where it interacts
# badly with the Future machinery.
_STREAM_DONE = object()

# Transient-LLM-error retry budget for a session turn. Worker jobs get this from
# `config.limits.llm_inproc_retries`; a session turn is interactive, so the ceiling
# is deliberately lower — a user is watching, and a turn that cannot recover in a
# few seconds is better surfaced than silently retried for a minute.
_SESSION_LLM_MAX_ATTEMPTS = 3
# Exponential base: sleeps _BASE * 2**attempt between attempts (2s, 4s).
_SESSION_LLM_RETRY_BASE_DELAY = 2.0


# Classifications worth another attempt. Deliberately the complement of the
# fail-fast verdicts (`permanent`, `quota_exhausted`, `cooldown`): those are
# states no retry inside a turn can fix, and the worker path already fails fast
# on them for reasons documented in `_classify_llm_error`.
_SESSION_RETRYABLE_CLASSIFICATIONS = frozenset(
    {"transient", "rate_limit", "auth_unavailable"}
)


def _is_context_overflow(error: BaseException) -> bool:
    """True if ``error`` is a deterministic "request too big" failure.

    `_classify_llm_error`'s catch-all verdict is `transient`, and the synthetic
    413 that `reasoning_chat` raises for a pre-flight overflow carries a status
    the classifier has no rule for — so it lands on that catch-all. Retrying it
    re-sends the identical oversized body, which is precisely the retry storm
    docs/issues/session_silent_failure_audit.md #3 removed. Detected three ways
    because the overflow reaches us typed, wrapped, or as the synthetic 413.
    """
    for candidate in (error, getattr(error, "__cause__", None)):
        if isinstance(candidate, ContextOverflowError):
            return True
        body = getattr(candidate, "body", None)
        if isinstance(body, dict):
            err_obj = body.get("error")
            if isinstance(err_obj, dict) and err_obj.get("code") == "context_overflow":
                return True
    return "context_overflow" in str(error)


def _is_retryable_llm_error(error: BaseException) -> bool:
    """True if the shared classifier says this LLM failure is worth retrying.

    Sessions and worker jobs deliberately call the *same* `_classify_llm_error`
    so a given provider failure gets one verdict product-wide — the worker path
    accumulated that triage across several incidents (see its docstring) and
    sessions had none of it.

    Imported lazily: `src.graph` is a heavy module and nothing else in the
    session path needs it at import time.
    """
    if _is_context_overflow(error):
        return False

    from .graph import _classify_llm_error

    return _classify_llm_error(error) in _SESSION_RETRYABLE_CLASSIFICATIONS


def _session_llm_retry_delay(attempt: int, error: BaseException) -> float:
    """Backoff before the next attempt, floored by any provider Retry-After."""
    delay = _SESSION_LLM_RETRY_BASE_DELAY * (2**attempt)
    try:
        from .graph import _extract_rate_limit_delay

        provider_delay = _extract_rate_limit_delay(error)
    except Exception:  # pragma: no cover - defensive
        provider_delay = None
    if provider_delay is not None:
        delay = max(delay, provider_delay)
    return delay


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
    get_current_context: Optional[Callable[[], tuple]] = None,
    get_current_system_prompt: Optional[Callable[[], str]] = None,
    memory_extraction_prompt: str = "",
    memory_service: Optional[Any] = None,
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
        get_current_system_prompt: Optional callback returning the session's
            current system prompt — re-read each turn so a live rebuild
            (tool-group toggle, workspace upgrade) reaches messages[0]
            instead of only the tool binding.
        memory_extraction_prompt: Matrix-resolved prompt for the background
            memory-extraction task, threaded from session setup (MemoryConfig
            carries no prompt attribute — docs/issues/memory_bugs.md B1).
        memory_service: MemoryManager seam (src.services.memory) — when
            bound (memory.manager.enabled), the in-loop extraction and the
            per-turn retrieval/injection route through it instead of the
            direct-store paths (memory overhaul Phase 1 cutover).
    """
    # Build tool lookup map
    tool_map: Dict[str, Any] = {tool.name: tool for tool in tools}
    turn_count = initial_turn_count
    llm_timeout = getattr(config.llm, "timeout", 600) or 600

    # Memory extraction cadence. Deliberately a direct attribute read: a
    # getattr fallback here is what let the phantom `extraction_interval`
    # key hide for months (docs/issues/memory_bugs.md B1c).
    memory_config = getattr(config, "memory", None)
    extraction_interval = memory_config.observer_interval if memory_config else 5
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

        # Dict-shaped items carry the id the accept-time persist wrote the
        # row under (session_silent_failure_audit.md #1) — reusing it makes
        # every later write an upsert onto that row instead of a duplicate.
        input_msg_id: Optional[str] = None
        input_persist_role: Optional[str] = None
        if isinstance(user_input, dict):
            input_msg_id = user_input.get("id")
            # System-injected input (e.g. a worker job this session created
            # finished) persists under its own transcript role. Carried through
            # so the turn-start reconcile below re-writes the SAME role the
            # accept-time persist used, instead of flipping the row to 'human'.
            input_persist_role = user_input.get("role")
            user_input = user_input.get("content", "")

        if not user_input or user_input == INTERRUPT_SENTINEL:
            continue

        # Refresh after receiving input so config changes made during the wait
        # (e.g. model hot-swap via config.update, plan mode toggle) are picked
        # up before the turn executes. Refreshing before the wait captures a
        # stale LLM reference when the user changes models while idle.
        if get_current_tools:
            new_llm, new_tools = get_current_tools()
            llm_with_tools = new_llm
            tool_map = {tool.name: tool for tool in new_tools}
        # Same for the session-held context manager, config, and auxiliary —
        # the loop captured them by value at task creation, so a hot-swap that
        # REPLACES the session objects (aux rebuild, future manager rebuilds)
        # or updates config-derived values (CTX-gauge window, limits) is
        # invisible without this re-read. The manager itself is additionally
        # updated in place (update_limits) so mid-turn references stay fresh.
        if get_current_context:
            context_manager, config, auxiliary_llm = get_current_context()
        # Live config changes rebuild session.system_prompt (tool-group
        # toggles, workspace upgrades), but messages[0] was written once at
        # loop start — mutate it in place (preserving message identity for
        # persistence) so the model actually sees the rebuilt prompt.
        if get_current_system_prompt:
            current_prompt = get_current_system_prompt()
            if (
                current_prompt
                and messages
                and isinstance(messages[0], SystemMessage)
                and messages[0].content != current_prompt
            ):
                messages[0].content = current_prompt
                logger.info(
                    "System prompt refreshed live (%d chars)", len(current_prompt)
                )

        turn_count += 1
        turn_id = turn_count
        user_msg = HumanMessage(content=user_input)
        if input_msg_id:
            user_msg.id = input_msg_id
        if input_persist_role and input_persist_role != "human":
            # Persist-role only — the message stays a HumanMessage so the turn
            # walk in _save_turn_ai_messages (backwards until the first
            # HumanMessage) keeps finding the turn boundary.
            user_msg.additional_kwargs[PERSIST_ROLE_KEY] = input_persist_role
        messages.append(_ensure_msg_id(user_msg))

        await callbacks.on_turn_start(turn_id)
        # Reconcile the accept-time row (turn_number was a guess there) — or
        # create it for inputs that bypassed the REST/WS accept path. Upsert
        # by message id either way.
        if callbacks.persist_message is not None:
            await callbacks.persist_message(user_msg)
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
                memory_service=memory_service,
            )
            tool_calls_this_turn = result.tool_calls_made
        except asyncio.CancelledError:
            logger.info(f"Turn {turn_id} cancelled")
            return
        except Exception as e:
            logger.exception(f"Error in turn {turn_id}")
            # turn_id lets the transport close the still-open turn in the UI
            # and persist the failure so it survives reload
            # (session_silent_failure_audit.md #2).
            await callbacks.on_error(_user_facing_turn_error(e), turn_id=turn_id)

        # Memory extraction every N turns (fire-and-forget).
        # Manager path (memory overhaul Phase 1): one turn_end capture —
        # the persistent_interval_extractor writer reproduces the elapsed
        # gate, the fixed window, and the extraction call below.
        if memory_service is not None:
            from .services.memory import CaptureEvent

            asyncio.create_task(
                memory_service.capture(
                    CaptureEvent(
                        kind="turn_end",
                        messages=messages,
                        turn_count=turn_count,
                    )
                )
            )
        elif (
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
                        memory_extraction_prompt=memory_extraction_prompt,
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


async def _stream_response_blocks(
    response: Any,
    callbacks: "PersistentLoopCallbacks",
    *,
    message_id: Optional[str] = None,
) -> tuple[str, bool]:
    """Stream a non-streamed AIMessage's content to the client.

    Mirrors the inline block-walk the live loop runs for the empty-tool-args
    ainvoke retry (text → on_token, reasoning/thinking blocks → on_thinking).
    Used by the empty-response retry to re-emit the retry's reasoning + answer
    after a thinking.reset, keyed to ``message_id`` so the new reasoning lands in
    (replaces) the same bubble. Returns ``(accumulated_text, emitted_reasoning)``
    — the caller flips ``reasoning_streamed`` on the latter so the post-stream
    fallback doesn't double-emit.
    """
    text_out = ""
    emitted_reasoning = False
    content = getattr(response, "content", None)
    if not content:
        return text_out, emitted_reasoning
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    text_out += text
                    await callbacks.on_token(text)
            elif isinstance(block, dict) and block.get("type") == "thinking":
                thinking = block.get("thinking", "")
                if thinking:
                    await callbacks.on_thinking(thinking, message_id=message_id)
                    emitted_reasoning = True
            elif isinstance(block, dict) and block.get("type") == "reasoning":
                reasoning = extract_reasoning_text_from_block(block)
                if reasoning:
                    await callbacks.on_thinking(reasoning, message_id=message_id)
                    emitted_reasoning = True
            elif isinstance(block, str) and block:
                text_out += block
                await callbacks.on_token(block)
    elif isinstance(content, str):
        text_out = content
        await callbacks.on_token(content)
    return text_out, emitted_reasoning


async def _emit_reasoning_content(response, callbacks, *, message_id) -> bool:
    """Emit ``additional_kwargs.reasoning_content`` as a thinking frame.

    Called on the ainvoke retry/fallback paths right after the response
    arrives and BEFORE its answer text is emitted, so the reasoning precedes
    the prose it produced (reasoning models never think after answering — a
    trailing frame is a broadcast artifact, see
    docs/issues/persistent_chat_reasoning_after_answer_and_replay_duplication.md).

    ``reasoning_content`` is only ever set by the non-streaming capture path
    (``_post_process_result``), which also flattens the message content to a
    plain string — so this can never double-emit against typed reasoning
    content blocks. Keyed to the turn's ``ai_msg_id``: ``_has_reasoning``
    guarantees the post-stream id pin, so the frame key matches the persisted
    row key. Returns True when a frame was emitted.
    """
    rc = (getattr(response, "additional_kwargs", None) or {}).get("reasoning_content")
    if not (rc and isinstance(rc, str)):
        return False
    await callbacks.on_thinking(rc, message_id=message_id)
    return True


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
    memory_service: Optional[Any] = None,
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
    citation_feedback_block = ""

    from .core.knowledge_injection import selected_knowledge_bindings
    from .core.skill_resolution import (
        managed_product_guide_turn_boundary as _product_guide_turn_boundary,
    )

    kb_bindings = selected_knowledge_bindings(tool_context)
    product_guide_turn_nudge = _product_guide_turn_boundary(
        getattr(config, "extra", {}).get("_resolved_skills")
        if isinstance(getattr(config, "extra", {}), dict)
        else None,
        list(tool_map),
    )

    # Memory/knowledge retrieval with timeout — must never block the LLM call
    _RETRIEVAL_TIMEOUT = 5  # seconds

    # MemoryManager seam read path (memory overhaul Phase 1 cutover): one
    # assemble() replaces the two direct-store blocks below, which stay
    # byte-identical for the flag-off path (pinned by
    # tests/test_memory_persistent_equivalence.py). The per-store 5 s guard
    # lives in the manager's runtime (retrieval_timeout).
    manager_injection: List[BaseMessage] = []
    if memory_service is not None:
        from .services.memory import AssembleRequest
        from .services.memory.plugins.legacy import build_persistent_query_text
        from .services.memory.query import build_digest_query_text

        # Unified request digest (§4) behind memory.query.digest; legacy
        # last-user-message query while the flag is off.
        _query_cfg = getattr(config.memory, "query", None)
        if _query_cfg is not None and _query_cfg.digest:
            _query_text = build_digest_query_text(
                messages,
                None,
                window=_query_cfg.digest_window,
                max_chars_per_message=_query_cfg.digest_max_chars_per_message,
            )
        else:
            _query_text = build_persistent_query_text(messages)
        _payload = await memory_service.assemble(
            AssembleRequest(
                query_text=_query_text,
                model=getattr(config.llm, "model", None),
            )
        )
        manager_injection = _payload.messages()
        if kb_bindings:
            # Multi-KB retrieval below owns the knowledge budget. Preserve the
            # manager's memory messages while fencing its legacy note-level KB
            # retriever to prevent duplicate native injection.
            manager_injection = [
                message
                for block in _payload.blocks
                if block.kind != "knowledge"
                for message in block.messages
            ]
        for _block in _payload.blocks:
            if _block.kind == "memory" and _block.items:
                logger.debug(
                    f"Memory injection: {len(_block.items)} memories retrieved"
                )
            elif _block.kind == "knowledge" and _block.items:
                logger.debug(
                    f"Knowledge injection: {len(_block.items)} notes retrieved"
                )

    if memory_service is None and recall_store:
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
    if knowledge_store and kb_bindings:
        try:
            kb_context = ""
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage):
                    kb_context = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                    break

            from .core.knowledge_injection import retrieve_bound_knowledge
            from .services.knowledge_store import KnowledgeStore as _KS

            selection = await retrieve_bound_knowledge(
                knowledge_store,
                kb_bindings,
                kb_context,
                timeout=_RETRIEVAL_TIMEOUT,
            )
            if selection.notes:
                knowledge_block = _KS.assemble_knowledge_block(
                    selection.notes,
                    model=getattr(config.llm, "model", None),
                    bindings=selection.bindings,
                    external_watermarks=selection.external_watermarks,
                )
                logger.debug(
                    "Knowledge injection: %s notes retrieved by binding=%s",
                    len(selection.notes),
                    selection.counts_by_binding,
                )
        except Exception as e:
            logger.warning(
                "Knowledge retrieval failed (non-fatal): %s: %s",
                type(e).__name__,
                e,
            )
    elif memory_service is None and knowledge_store and effective_pids:
        try:
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

    # Citation verification feedback (Phase 2b / D4 — persistent parity with the
    # worker's _inject_transient_messages): surface still-failed citations so the
    # agent can correct them. DB-driven and job-scoped (list_citations defaults to
    # this session's job_id == thread_id), recomputed once per turn so it
    # self-resolves — editing a citation resets it to pending → re-verifies →
    # drops out next turn. Only runs after citation activity (the engine is lazily
    # created on first cite/source registration). Injected on the ephemeral
    # per-call copy below, so it never enters the durable history or a summary.
    _cit_engine = (
        getattr(tool_context, "citation_engine", None) if tool_context else None
    )
    if _cit_engine is not None:
        try:
            _failed_cites = await asyncio.wait_for(
                _cit_engine.list_citations(verification_status="failed"),
                timeout=_RETRIEVAL_TIMEOUT,
            )
            if _failed_cites:
                from .core.citation_feedback_injection import format_failed_citations

                citation_feedback_block = format_failed_citations(_failed_cites)
                logger.debug(
                    f"Citation feedback: {len(_failed_cites)} failed citation(s) "
                    "to surface"
                )
        except asyncio.TimeoutError:
            logger.warning("Citation feedback retrieval timed out — skipping injection")
        except Exception as e:
            logger.warning(
                "Citation feedback retrieval failed (non-fatal): %s: %s",
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

        # Context compaction if needed — on the DURABLE session list, not the
        # per-call copy: a real compaction is adopted into `messages` once, so
        # the next LLM call starts from [summary + recent] instead of
        # re-summarizing from scratch on every call (the ephemeral-prepared
        # design caused exactly that thrash once a session crossed the
        # threshold). The transient memory/knowledge injections happen on the
        # per-call copy below, AFTER compaction, so they are never folded into
        # a durable summary. The full conversation always stays in
        # thread_messages — only the in-memory working set shrinks.
        # Raced against a hard interrupt: the summarization LLM call here is
        # the worst offender for parking the turn (a hung endpoint can hold it
        # for the full auxiliary timeout), so a hard "stop" must be able to
        # tear it down at once.
        # Memory extraction before compaction (persistent): if this call is about
        # to summarize, snapshot the slice ensure_within_limits will evict and
        # mine it for durable memories before the lossy summary replaces it
        # (docs/done/memory_extraction_before_compaction.md). Fire-and-forget
        # so compaction latency is unchanged; no phase concept in a session →
        # phase=0 (matches the turn_end capture).
        if memory_service is not None and context_manager.should_summarize(messages):
            from .services.memory import CaptureEvent

            keep_recent = context_manager.config.keep_recent_messages
            evicted = (
                list(messages[:-keep_recent]) if keep_recent > 0 else list(messages)
            )
            if evicted:
                memory_service.capture_nowait(
                    CaptureEvent(kind="pre_compaction", messages=evicted, phase=0)
                )

        pre_compact_len = len(messages)
        compaction_runs_before = getattr(context_manager, "compaction_runs", 0)
        bounded, compact_interrupted = await _await_or_hard_interrupt(
            context_manager.ensure_within_limits(
                messages,
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
        # them too (src/graph.py).
        bounded = strip_removal_markers(bounded)

        # Did the manager actually summarize on this call? The run counter is
        # the authoritative signal — the old length heuristic false-fired on
        # stray RemoveMessage markers (duplicate-banner bug, 2026-06-12).
        compaction_runs_after = getattr(context_manager, "compaction_runs", 0)
        if (
            isinstance(compaction_runs_before, int)
            and isinstance(compaction_runs_after, int)
            and compaction_runs_after > compaction_runs_before
        ):
            # Adopt the compacted history durably, surface it for display,
            # then commit + push the workspace to Gitea as a checkpoint.
            messages[:] = bounded
            summary_text = extract_summary_text(messages)
            if summary_text and callbacks.on_context_compacted:
                try:
                    await callbacks.on_context_compacted(
                        summary_text, pre_compact_len, len(messages)
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
                                f"Auto-compaction checkpoint ({pre_compact_len} → {len(messages)} msgs)"
                            )
                        git_mgr.push()
                    except Exception as e:
                        logger.debug(
                            f"Git push on auto-compaction failed (non-fatal): {e}"
                        )

        # Per-call working copy: substitution/elision results from a
        # non-summarizing bound stay ephemeral, and the transient injections
        # below never touch the durable list.
        prepared = list(bounded)

        # Transient context injection (memory / knowledge / MemoryManager
        # seam). Anchored at the TAIL — after the conversation — so the stable
        # history prefix stays byte-identical between turns and provider
        # prompt caches reuse it (the block changes every turn; placed ahead
        # of the history it broke the cache for the whole conversation each
        # request). The anchor still satisfies providers that enforce
        # function-call turn ordering (Gemini rejects a function-call turn not
        # preceded by a user/function-response turn): it sits after the last
        # Human/Tool message, which is normally the very end of the history.
        # See _injection_anchor_index. The same message objects may be reused
        # each inner-loop iteration; pair ids are only prefix-checked
        # downstream.
        _inject_context_pairs(
            prepared,
            manager_injection,
            memory_block,
            knowledge_block,
            citation_feedback_block,
            product_guide_turn_boundary=product_guide_turn_nudge,
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
        # Backstop: scrub malformed tool-call arguments already persisted in
        # history (docs/features/outbound_message_hygiene.md) — MiniMax
        # validates historical tool calls on input and 400s otherwise.
        prepared = scrub_history_tool_call_arguments(prepared)

        # --- LLM call with streaming ---
        response_content = ""
        response: Optional[AIMessage] = None
        llm_start = time.monotonic()

        # Pre-allocate this LLM call's message id so every reasoning frame we
        # broadcast (live deltas below, or the post-stream fallback) shares a
        # stable key with the thread_messages row it lands in. The client
        # dedupes a reasoning frame replayed after history already painted the
        # bubble by this id (gemma "reasoning duplicates on replay" bug). See
        # docs/issues/persistent_chat_reasoning_after_answer_and_replay_duplication.md
        ai_msg_id = f"msg_{_uuid.uuid4().hex[:24]}"
        # Set by the live reasoning sink below; gates the post-stream fallback
        # so each reasoning blob is emitted exactly once.
        reasoning_streamed = False
        # True once reasoning reached the client OUTSIDE the sink this LLM
        # call — the in-stream typed-block arms (Responses/Anthropic) and the
        # early ainvoke-path emissions set it (sink-streamed reasoning is
        # tracked separately via reasoning_streamed). Gates the
        # post-stream fallback and the empty-response-retry reset. Kept
        # separate from reasoning_streamed on purpose: reasoning_streamed
        # also drives the response.id pin below, and pinning a msg_ id onto
        # a LIST-content message makes langchain-core's
        # _convert_from_v03_ai_message misread it as legacy format and ship
        # the fabricated id in round-tripped item ids (Responses/Anthropic
        # provider ids are round-trip-critical).
        reasoning_emitted = False
        # Accumulate streamed reasoning text so we can derive a token estimate
        # for models that stream reasoning but report no provider reasoning-token
        # count (gemma via the vLLM router folds it into output_tokens).
        _reasoning_buf: list = []
        _loop = asyncio.get_running_loop()
        # Keep strong refs to fire-and-forget broadcast tasks so they aren't
        # GC'd mid-flight (CPython drops weakly-held tasks).
        _reasoning_tasks: set = set()

        def _on_reasoning_delta(text: str) -> None:
            # Called synchronously from the SSE tap as reasoning bytes arrive
            # (before the answer tokens, for gemma-style models). Schedule the
            # async broadcast on the running loop; the body is a cheap enqueue
            # so ordering vs. the awaited answer tokens holds.
            nonlocal reasoning_streamed
            if not text:
                return
            reasoning_streamed = True
            _reasoning_buf.append(text)
            task = _loop.create_task(callbacks.on_thinking(text, message_id=ai_msg_id))
            _reasoning_tasks.add(task)
            task.add_done_callback(_reasoning_tasks.discard)

        _sink_token = _STREAM_REASONING_SINK.set(_on_reasoning_delta)
        try:
            try:
                # Try astream for token-by-token streaming
                chunks = []
                # Clean per-chunk finish_reason: the merged response doubles it to
                # "lengthlength" on OpenRouter-direct (§7.1), so keep the last
                # non-empty per-chunk value. Defined before astream so the
                # ainvoke-fallback path leaves it None → falls back to merged meta.
                stream_finish_reason: Any = None
                # Holds the interrupt mode ("hard"|"graceful") if the loop
                # below broke early; None otherwise. Legacy bool callbacks
                # land as True here and are treated as "graceful".
                streaming_interrupted: Any = None
                # Manual iteration (vs. `async for`) so a hard interrupt can
                # cancel a hung chunk read mid-stream instead of waiting for
                # the next chunk to arrive before the cooperative check below.
                _stream = llm_with_tools.astream(prepared)
                _aiter = _stream.__aiter__()
                _llm_attempt = 0
                while True:
                    try:
                        chunk, _stream_status = await _stream_next_or_hard_interrupt(
                            _aiter, callbacks.hard_interrupt_event
                        )
                    except Exception as chunk_err:
                        # A provider failure *inside* an already-200 SSE body:
                        # the openai SDK raises a bare APIError here (base class,
                        # no status_code) and its own max_retries no longer
                        # applies because the response body had already started.
                        # Worker jobs classify these and retry; sessions used to
                        # push the raw provider string at the user (incident
                        # 2026-07-25, session b1758f38). Same classifier now.
                        #
                        # Only safe while nothing has reached the client — a
                        # retry restarts the stream from scratch, so replaying
                        # after tokens were painted would duplicate them.
                        _nothing_shown = not (
                            response_content or reasoning_streamed or reasoning_emitted
                        )
                        if (
                            _llm_attempt + 1 < _SESSION_LLM_MAX_ATTEMPTS
                            and _nothing_shown
                            and _is_retryable_llm_error(chunk_err)
                        ):
                            _delay = _session_llm_retry_delay(_llm_attempt, chunk_err)
                            logger.warning(
                                "Transient LLM stream error (attempt %d/%d), "
                                "retrying in %.1fs: %s: %s",
                                _llm_attempt + 1,
                                _SESSION_LLM_MAX_ATTEMPTS,
                                _delay,
                                type(chunk_err).__name__,
                                chunk_err,
                            )
                            await asyncio.sleep(_delay)
                            _llm_attempt += 1
                            # Restart cleanly: drop partial stream state so the
                            # fresh attempt cannot merge with the dead one.
                            chunks = []
                            stream_finish_reason = None
                            response = None
                            _reasoning_buf.clear()
                            _stream = llm_with_tools.astream(prepared)
                            _aiter = _stream.__aiter__()
                            continue
                        raise
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
                    _chunk_meta = getattr(chunk, "response_metadata", None) or {}
                    if _chunk_meta.get("finish_reason"):
                        stream_finish_reason = _chunk_meta["finish_reason"]
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
                                        reasoning_emitted = True
                                elif (
                                    isinstance(block, dict)
                                    and block.get("type") == "reasoning"
                                ):
                                    reasoning = extract_reasoning_text_from_block(block)
                                    if reasoning:
                                        await callbacks.on_thinking(reasoning)
                                        reasoning_emitted = True
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
                    # Reasoning first: the non-streaming capture path parks it
                    # in additional_kwargs, so emit it before the answer text.
                    if await _emit_reasoning_content(
                        response, callbacks, message_id=ai_msg_id
                    ):
                        reasoning_emitted = True
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
                                        reasoning_emitted = True
                                elif (
                                    isinstance(block, dict)
                                    and block.get("type") == "reasoning"
                                ):
                                    reasoning = extract_reasoning_text_from_block(block)
                                    if reasoning:
                                        await callbacks.on_thinking(reasoning)
                                        reasoning_emitted = True
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
                # A context overflow is deterministic — retrying via ainvoke
                # sends the identical oversized request again. Surface the
                # typed cause instead of misreporting "streaming not
                # supported" (session_silent_failure_audit.md #3). The
                # capture client now returns a synthetic 413 (APIStatusError),
                # but unwrap a legacy __cause__ wrap too, defensively.
                if isinstance(
                    getattr(stream_err, "__cause__", None), ContextOverflowError
                ):
                    raise stream_err.__cause__ from None
                if "ResponseNotRead" in err_name or "APIConnectionError" in err_name:
                    logger.info(
                        f"Streaming not supported ({err_name}), falling back to ainvoke"
                    )
                    response = await llm_with_tools.ainvoke(prepared)
                    # Reasoning first: the non-streaming capture path parks it
                    # in additional_kwargs, so emit it before the answer text.
                    if await _emit_reasoning_content(
                        response, callbacks, message_id=ai_msg_id
                    ):
                        reasoning_emitted = True
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
                                        reasoning_emitted = True
                                elif (
                                    isinstance(block, dict)
                                    and block.get("type") == "reasoning"
                                ):
                                    reasoning = extract_reasoning_text_from_block(block)
                                    if reasoning:
                                        await callbacks.on_thinking(reasoning)
                                        reasoning_emitted = True
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
        finally:
            # Stop routing reasoning deltas to this turn's sink — every exit
            # path (return, raise, fall-through) must clear it so a stale
            # closure can't fire on the next turn's stream.
            _STREAM_REASONING_SINK.reset(_sink_token)

        # Extract per-turn metrics from response metadata. Streaming providers
        # often leave response_metadata.token_usage empty — the aggregated
        # chunk carries LangChain's normalized ``usage_metadata`` instead, so
        # read both (verified live on k3d: gemma via the vLLM router reports
        # usage only through usage_metadata).
        llm_latency_ms = int((time.monotonic() - llm_start) * 1000)
        turn_metrics: Optional[dict] = None
        meta = getattr(response, "response_metadata", None) or {}
        token_usage = meta.get("token_usage", {}) or {}
        usage_md = getattr(response, "usage_metadata", None) or {}
        if response is not None and (token_usage or usage_md):
            usage_details = usage_md.get("output_token_details") or {}
            input_details = usage_md.get("input_token_details") or {}
            turn_metrics = {
                "input_tokens": token_usage.get("input_tokens")
                or token_usage.get("prompt_tokens")
                or usage_md.get("input_tokens"),
                "output_tokens": token_usage.get("output_tokens")
                or token_usage.get("completion_tokens")
                or usage_md.get("output_tokens"),
                "reasoning_tokens": token_usage.get("reasoning_tokens")
                or usage_details.get("reasoning"),
                # Cached prompt tokens. LangChain normalizes both Chat Completions
                # and the Responses API (codex/gpt-5.x) to input_token_details.
                # cache_read; the raw token_usage path is a fallback for providers
                # that surface prompt_tokens_details but no usage_metadata.
                "cached_tokens": input_details.get("cache_read")
                or (token_usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens"
                ),
                "latency_ms": llm_latency_ms,
                "model": meta.get("model_name"),
            }
            turn_metrics = {k: v for k, v in turn_metrics.items() if v is not None}

            # Backfill a reasoning-token estimate for models that surface
            # reasoning *text* but no provider reasoning-token count (gemma via
            # the vLLM router folds it into output_tokens). We hold the streamed
            # reasoning text (or the post-hoc reasoning_content), so tokenize it
            # ourselves — a SUBSET of output_tokens, flagged estimated.
            _reasoning_text = "".join(_reasoning_buf) or (
                (getattr(response, "additional_kwargs", None) or {}).get(
                    "reasoning_content"
                )
                or ""
            )
            _maybe_estimate_reasoning_tokens(turn_metrics, _reasoning_text)

            # Anchor the compaction trigger on the real provider input_tokens
            # (context_token_accounting.md S1). Guarded so test stubs and
            # empty-usage turns are no-ops.
            _record_usage = getattr(context_manager, "record_provider_usage", None)
            if _record_usage is not None:
                _record_usage(turn_metrics.get("input_tokens"))

        # Audit the call. Sessions previously wrote no llm_requests rows at
        # all — job agents were auditable, session hangs were not
        # (session_silent_failure_audit.md #14). The callback schedules its
        # own background write; failures are non-fatal by contract.
        if callbacks.archive_llm_call is not None and response is not None:
            try:
                callbacks.archive_llm_call(
                    prepared,
                    response,
                    turn_metrics or {"latency_ms": llm_latency_ms},
                )
            except Exception as e:
                logger.debug(f"LLM call archive failed (non-fatal): {e}")

        # Live token telemetry for the cockpit's usage panel (same numbers as
        # the audit row — one accumulator, two sinks).
        if callbacks.on_usage is not None and turn_metrics:
            try:
                await callbacks.on_usage(
                    {
                        **turn_metrics,
                        "ctx_limit_tokens": config.limits.model_max_context_tokens,
                        # The absolute token count at which auto-compaction
                        # fires (limits.context_threshold_tokens → ContextConfig).
                        # The cockpit anchors its ctx gauge + colour ramp on this,
                        # not the raw model window, so "danger" means compaction
                        # is imminent rather than an arbitrary % of the window.
                        "compaction_threshold_tokens": getattr(
                            config.limits, "context_threshold_tokens", None
                        ),
                    }
                )
            except Exception as e:
                logger.debug(f"usage callback failed (non-fatal): {e}")

        if response is None:
            return TurnResult(
                turn_id=0,
                messages_added=messages_added,
                tool_calls_made=tool_calls_made,
                error="Empty LLM response",
            )

        # Output-cap truncation detection: reasoning shares max_output_tokens, so
        # a finish_reason=length turn means the budget was exhausted. Prefer the
        # clean per-chunk value; tolerant substring covers the "lengthlength"
        # merge (§7.1). Resolve the cap once for the surfaced messages below.
        from src.core.loader import _is_output_truncated, _resolve_max_output_tokens

        _finish_reason = stream_finish_reason or meta.get("finish_reason")
        _is_length = _is_output_truncated(_finish_reason)
        _output_cap = (
            _resolve_max_output_tokens(config.llm, config.limits)
            if _is_length
            else None
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
            # Shared terminal placeholder, used when nothing better can be shown.
            _empty_msg = (
                "⚠ The model returned an empty response. "
                "Please try again or switch models."
            )
            if refusal:
                logger.warning("Model refusal: %s", refusal)
                response_content = f"⚠ The model declined to respond: {refusal}"
                await callbacks.on_token(response_content)
            elif _is_length:
                # Reasoning consumed the entire output budget before any answer
                # (reasoning shares max_output_tokens). Fail loud — a plain retry
                # re-truncates; let the user rewind/regenerate or raise the cap.
                logger.warning(
                    "Output truncated (finish_reason=length, cap=%s): "
                    "reasoning-only, no answer",
                    _output_cap,
                )
                response_content = (
                    f"⚠ The model used its entire output budget ({_output_cap} "
                    "tokens) on reasoning and was cut off before answering. "
                    "Rewind/regenerate, lower the reasoning level, or raise the "
                    "model's output cap."
                )
                await callbacks.on_token(response_content)
            elif getattr(llm_with_tools, "reasoning", None):
                # Bounded auto-retry for reasoning/codex models. A gpt-5.x turn
                # via the Codex proxy occasionally streams finish_reason=stop with
                # zero content deltas and no tool call — a rare (~0.7 %),
                # non-deterministic upstream event we could not reproduce across
                # ~1026 synthetic calls (docs/issues/
                # session_empty_response_gpt5_codex_stop.md). One ainvoke retry
                # almost always succeeds and, being non-streaming, hits a
                # different proxy/SDK translation path. Single attempt — a
                # straight-line call, not a loop, so it is hard-bounded.
                logger.info(
                    "Empty streamed response on a reasoning model — "
                    "retrying once via ainvoke"
                )
                retry: Optional[AIMessage] = None
                try:
                    retry = await asyncio.wait_for(
                        llm_with_tools.ainvoke(prepared), timeout=llm_timeout
                    )
                except Exception as retry_err:
                    logger.warning(
                        "Empty-response ainvoke retry failed: %s",
                        type(retry_err).__name__,
                    )
                retry_extra = (
                    (getattr(retry, "additional_kwargs", None) or {})
                    if retry is not None
                    else {}
                )
                retry_tools = (
                    getattr(retry, "tool_calls", None) if retry is not None else None
                )
                if retry is not None and (
                    getattr(retry, "content", None) or retry_tools
                ):
                    # Retry produced something. Replace the dead-end reasoning
                    # bubble (if one streamed live) with the retry's reasoning +
                    # answer rather than appending under the stale one.
                    if (
                        reasoning_streamed or reasoning_emitted
                    ) and callbacks.on_thinking_reset is not None:
                        # Drain the in-flight reasoning broadcasts first so a late
                        # delta can't repaint AFTER the reset. The streaming sink
                        # was already cleared in the finally above, so this set is
                        # closed — it can't grow while we await it.
                        if _reasoning_tasks:
                            await asyncio.gather(
                                *_reasoning_tasks, return_exceptions=True
                            )
                        # In-stream block frames are UNKEYED (no message_id), so
                        # only an unkeyed reset can clear them — it removes every
                        # still-streaming thought of the active turn, which here
                        # is exactly attempt-1's dead-end reasoning. Sink-only
                        # frames stay keyed to ai_msg_id as before.
                        await callbacks.on_thinking_reset(
                            message_id=None if reasoning_emitted else ai_msg_id
                        )
                        reasoning_streamed = False
                        reasoning_emitted = False
                    # Reasoning first: a retry that went through ainvoke carries
                    # its reasoning in additional_kwargs (flattened content), so
                    # emit it before the answer text instead of leaving it to
                    # the post-stream fallback (which would trail the answer).
                    if await _emit_reasoning_content(
                        retry, callbacks, message_id=ai_msg_id
                    ):
                        reasoning_emitted = True
                    response_content, _retry_reasoned = await _stream_response_blocks(
                        retry, callbacks, message_id=ai_msg_id
                    )
                    if _retry_reasoned:
                        reasoning_streamed = True
                    response = retry
                    # The retry may itself be empty / a refusal → placeholder.
                    if not response_content and not retry_tools:
                        retry_refusal = retry_extra.get("refusal")
                        response_content = (
                            f"⚠ The model declined to respond: {retry_refusal}"
                            if retry_refusal
                            else _empty_msg
                        )
                        await callbacks.on_token(response_content)
                else:
                    # Retry also empty (or raised). Keep attempt-1's reasoning
                    # visible (no reset) and show the placeholder.
                    retry_refusal = retry_extra.get("refusal")
                    response_content = (
                        f"⚠ The model declined to respond: {retry_refusal}"
                        if retry_refusal
                        else _empty_msg
                    )
                    await callbacks.on_token(response_content)
            else:
                response_content = _empty_msg
                await callbacks.on_token(response_content)

        # Coerce a streamed chunk to a concrete AIMessage before it enters
        # history (the next turn's request serialization rejects raw chunk
        # types), then sanitize for Responses API compatibility (null IDs
        # from OpenRouter).
        response = _sanitize_ai_response(coerce_to_ai_message(response))

        # Reasoning delivered via additional_kwargs.reasoning_content comes from
        # Chat Completions models (gemma/DeepSeek/OpenRouter) — that API is
        # stateless and never round-trips message ids, so pinning the row id to
        # our pre-allocated ai_msg_id is safe. We pin whenever there is/was
        # reasoning to broadcast so the live (or fallback) frame and this
        # persisted row share the dedupe key. Responses/Anthropic thinking
        # blocks keep their provider id (round-trip-critical) and aren't keyed.
        _extra = getattr(response, "additional_kwargs", None) or {}
        _reasoning = _extra.get("reasoning_content")
        _has_reasoning = bool(_reasoning and isinstance(_reasoning, str))
        if reasoning_streamed or _has_reasoning:
            response.id = ai_msg_id
        # Last-resort fallback: emit reasoning that neither the live sink nor
        # any in-stream/early emission already delivered (a plain streamed
        # message that arrived carrying reasoning_content). The ainvoke paths
        # emit reasoning_content early (before their text) and flip
        # reasoning_emitted, so this can no longer trail the answer for them.
        # Now that we're past sanitize, message_id matches the persisted row.
        if _has_reasoning and not reasoning_streamed and not reasoning_emitted:
            await callbacks.on_thinking(_reasoning, message_id=ai_msg_id)
        if (
            response_content
            and not getattr(response, "tool_calls", None)
            and _visible_content_len(response.content) == 0
        ):
            response.content = response_content

        # Output-cap truncation WITH visible content (truncated mid-answer): the
        # partial is real work — keep it, but surface the cut on the PERSISTED
        # message so the turn isn't silently treated as complete. The empty-content
        # writeback above won't fire here; append to string content directly (rare
        # block/list content streams the notice live but isn't mutated).
        if _is_length and _visible_content_len(response.content) > 0:
            _trunc_notice = (
                f"\n\n⚠ Output truncated at the model's limit ({_output_cap} "
                "tokens). Rewind/regenerate or raise the model's output cap to "
                "continue."
            )
            if isinstance(response.content, str):
                response.content += _trunc_notice
            await callbacks.on_token(_trunc_notice)

        # Repair/scrub malformed tool-call arguments before the response
        # becomes durable state — a raw unparseable call in history poisons
        # every later request (docs/features/outbound_message_hygiene.md).
        response = repair_tool_call_arguments(response)

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

            # Permission check. Three-state: an unanswered gate is neither
            # consent nor refusal — never fabricate a decision the user did
            # not make (see
            # docs/issues/supervised_parallel_gates_timeout_fabricates_denial.md).
            outcome = PermissionOutcome.coerce(
                await callbacks.permission_check(tool_name, tool_args, tool_call_id)
            )

            if outcome is PermissionOutcome.DECLINED:
                messages.append(
                    _ensure_msg_id(
                        ToolMessage(
                            content="User declined this tool call.",
                            tool_call_id=tool_call_id,
                        )
                    )
                )
                messages_added += 1
                await _persist(messages[-1])
                continue

            if outcome is PermissionOutcome.NO_ANSWER:
                # The gate was never answered (TTL elapsed, or the approval
                # card never reached the browser). Park the turn: write NO
                # ToolMessage, leave this call and every call after it
                # un-run, and let the durable pending row resume the work
                # when the user actually answers.
                logger.info(
                    "Permission gate unanswered for tool %s (%s) — parking turn; "
                    "%d call(s) left ungated",
                    tool_name,
                    tool_call_id,
                    len(response.tool_calls) - i,
                )
                return TurnResult(
                    turn_id=0,
                    messages_added=messages_added,
                    tool_calls_made=tool_calls_made,
                    awaiting_permission=True,
                )

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
            except WorkspaceUnavailableError:
                # Dead workspace mid-turn: propagate so the turn handler surfaces
                # a clean recovery message via on_error, instead of flattening it
                # into a retryable ToolMessage (the ~39-min spin).
                raise
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
                            max_edge=resolve_image_max_edge(config),
                        )
                    )
                )
                messages_added += 1
                await _persist(messages[-1])

            await callbacks.on_tool_result(
                tool_name, cleaned_str, tool_call_id, is_error=is_error
            )

            # Check for a freeze request — a sudo intercept asking for a VM
            # (vm_upgrade_required) or a lite agent's request_workspace_upgrade
            # asking for a sandbox (workspace_upgrade_required, §4.2 S5). Both
            # surface as an upgrade OFFER; the agent only requests, never flips
            # the tier (§4.4 Sec-4).
            if tool_context and callbacks.on_workspace_upgrade_needed:
                freeze_req = tool_context.consume_freeze_request()
                if freeze_req and freeze_req.get("freeze_type") in (
                    "vm_upgrade_required",
                    "workspace_upgrade_required",
                ):
                    await callbacks.on_workspace_upgrade_needed(freeze_req)

        # Continue the inner loop — LLM sees tool results on next iteration

    return TurnResult(
        turn_id=0,
        messages_added=messages_added,
        tool_calls_made=tool_calls_made,
        metrics=turn_metrics,
    )
