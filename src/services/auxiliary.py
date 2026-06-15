"""AuxiliaryLLM — Unified support task system.

Provides two execution modes for background/support LLM tasks:

- **Chain mode** (`chain()`): Single LLM call with structured output.
  System prompt + context → Pydantic model. No tools, no loop.
  For tasks that just need reasoning over provided context.

- **Agent mode** (`agent()`): Short-lived tool loop with structured output.
  The LLM can make tool calls (search KB, read files, write notes),
  then a final structured-output call produces the result.
  Capped iterations. Not a full job — no workspace, no todos, no phases.

All tasks use `with_structured_output()` for reliable structured returns.

See docs/features/auxiliary.md for the full design document.
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Type

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.core.archiver import LLMArchiver

logger = logging.getLogger(__name__)


# =============================================================================
# Output schemas (Pydantic models for with_structured_output)
# =============================================================================


class ExtractedMemory(BaseModel):
    """A single extracted memory from conversation."""

    content: str = Field(description="The insight (1-3 sentences, self-contained)")
    summary: str = Field(description="One-line summary (under 100 chars)")
    keywords: List[str] = Field(description="Relevant terms for search (3-8 keywords)")
    importance: float = Field(
        description="How useful is this for future work? (0.0-1.0)",
        ge=0.0,
        le=1.0,
    )
    type: str = Field(
        description="One of: factual, procedural, error_solution, vocabulary, relational"
    )
    retrieval_messages: List[str] = Field(
        default_factory=list,
        description=(
            "3-5 synthetic trigger phrases representing situations where this "
            "memory should be retrieved. Phrased as what the agent would be "
            "saying or encountering when it needs this information."
        ),
    )


class ExtractedMemories(BaseModel):
    """Structured output for memory extraction."""

    memories: List[ExtractedMemory] = Field(
        description="List of extracted memories. Empty list if nothing noteworthy."
    )


class CurationResult(BaseModel):
    """Structured output for knowledge curation (agent mode)."""

    notes_created: int = Field(description="Number of new knowledge notes created")
    notes_updated: int = Field(description="Number of existing notes updated")
    summary: str = Field(description="Brief summary of what was curated")


class AssemblyAction(BaseModel):
    """A single TTL adjustment made by the assembler."""

    memory_id: str = Field(description="UUID of the memory acted on")
    action: str = Field(description="'boost' or 'deprecate'")
    turns: int = Field(description="Number of turns to adjust TTL by")
    reason: str = Field(description="Why this adjustment was made")


class AssemblyResult(BaseModel):
    """Structured output for memory assembly (agent mode)."""

    actions_taken: List[AssemblyAction] = Field(
        default_factory=list,
        description="List of TTL adjustments made. Empty if no changes needed.",
    )
    gaps_identified: List[str] = Field(
        default_factory=list,
        description="Missing knowledge areas where no relevant memory exists",
    )
    summary: str = Field(description="Brief summary of assembly review")


class IngestionVerdict(BaseModel):
    """Structured output for the ingestion verdict (overhaul Phase 4).

    The adjudicator compares a newly-extracted candidate memory against its
    nearest currently-valid neighbours and decides what to do with it.
    ``target_indices`` are 1-based positions in the numbered neighbour list
    shown to the model (NOT database ids — echoing UUIDs is error-prone); the
    store maps them back to rows.
    """

    action: str = Field(
        description=(
            "One of: ADD (genuinely new information — keep the candidate, "
            "touch nothing), NOOP (already captured by a neighbour — discard "
            "the candidate), UPDATE (the candidate is the SAME fact with a "
            "changed/corrected value — keep it and retire the stale "
            "neighbours in target_indices), MERGE (the candidate and "
            "neighbours are complementary parts of one fact — store "
            "merged_content and retire the neighbours in target_indices)."
        )
    )
    target_indices: List[int] = Field(
        default_factory=list,
        description=(
            "1-based indices of the neighbour memories this verdict acts on. "
            "Required for UPDATE/MERGE (the rows to retire) and NOOP (the row "
            "the candidate duplicates). Empty for ADD."
        ),
    )
    merged_content: Optional[str] = Field(
        default=None,
        description=(
            "For MERGE only: the single combined fact (1-3 self-contained "
            "sentences) that replaces the candidate and the retired neighbours."
        ),
    )
    reason: str = Field(description="One-line justification for the verdict")


# NOTE: ConversationSummary (the summarization output schema) lives in
# src/core/context.py because it's tightly coupled with the compaction
# formatting logic there. SummarizeTask imports it from there.


# =============================================================================
# Task base classes
# =============================================================================


class AuxTask(ABC):
    """Base class for chain-mode tasks.

    Subclasses define the system prompt, context assembly, and output schema.
    The AuxiliaryLLM handles execution via with_structured_output().
    """

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """System prompt for the LLM call."""
        ...

    @abstractmethod
    def build_context(self) -> str:
        """Build the user message content from task inputs."""
        ...

    @property
    @abstractmethod
    def output_schema(self) -> Type[BaseModel]:
        """Pydantic model class for structured output."""
        ...


class AuxAgentTask(AuxTask):
    """Base class for agent-mode tasks (adds tool access).

    Agent tasks run a short tool loop, then a final structured-output call
    produces the result. The output_schema defines what the final call returns.
    """

    @abstractmethod
    def get_tools(self) -> list:
        """Return the list of LangChain tools available to this task."""
        ...


# =============================================================================
# Task implementations
# =============================================================================


class ExtractMemoriesTask(AuxTask):
    """Extract memories from a conversation segment.

    Chain mode task. Prompt loaded from config/prompts/ via the prompt matrix.
    """

    def __init__(self, messages: List[BaseMessage], prompt: str, phase: int = 0):
        self.messages = messages
        self._prompt = prompt
        self.phase = phase

    @property
    def system_prompt(self) -> str:
        return self._prompt

    def build_context(self) -> str:
        return _format_messages_for_extraction(self.messages)

    @property
    def output_schema(self) -> Type[BaseModel]:
        return ExtractedMemories


class IngestionVerdictTask(AuxTask):
    """Adjudicate a candidate memory against its nearest neighbours.

    Chain-mode task (overhaul Phase 4). Prompt loaded from config/prompts/.
    ``neighbours`` is the ordered list shown to the model — each item a dict
    with ``content`` and an optional ``age`` / ``similarity`` annotation; the
    1-based display index is the position in this list.
    """

    def __init__(
        self,
        candidate_content: str,
        neighbours: List[Dict[str, Any]],
        prompt: str,
    ):
        self.candidate_content = candidate_content
        self.neighbours = neighbours
        self._prompt = prompt

    @property
    def system_prompt(self) -> str:
        return self._prompt

    def build_context(self) -> str:
        lines = ["NEW candidate memory:", self.candidate_content, ""]
        lines.append("Existing similar memories (currently valid):")
        for i, n in enumerate(self.neighbours, start=1):
            meta_parts = []
            if n.get("similarity") is not None:
                meta_parts.append(f"similarity {float(n['similarity']):.2f}")
            if n.get("age"):
                meta_parts.append(str(n["age"]))
            meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
            lines.append(f"[{i}]{meta} {n.get('content', '')}")
        lines.append("")
        lines.append(
            "Decide ADD / UPDATE / MERGE / NOOP. For UPDATE/MERGE/NOOP put the "
            "relevant [n] numbers in target_indices."
        )
        return "\n".join(lines)

    @property
    def output_schema(self) -> Type[BaseModel]:
        return IngestionVerdict


class SummarizeTask(AuxTask):
    """Summarize a conversation segment into structured fields.

    Chain mode task invoked per fold pass by
    ``src.core.summarizer.SummarizationEngine``.
    Prompt loaded from config/prompts/ via the prompt matrix.

    The output schema is ConversationSummary from src/core/context.py.
    """

    def __init__(
        self,
        conversation_text: str,
        summarization_prompt: str,
        max_summary_length: int = 10000,
    ):
        self.conversation_text = conversation_text
        self._summarization_prompt = summarization_prompt
        self.max_summary_length = max_summary_length

    @property
    def system_prompt(self) -> str:
        # The template has {conversation} and {max_summary_length} placeholders.
        # Strip the conversation placeholder — it goes in build_context().
        # Render max_summary_length into the instructions.
        from collections import defaultdict

        rendered = self._summarization_prompt.format_map(
            defaultdict(
                str,
                conversation="",  # Will be sent as HumanMessage
                max_summary_length=str(self.max_summary_length),
            )
        )
        # Clean up the empty "Conversation:" section left by the placeholder
        rendered = rendered.replace("\nConversation:\n\n\n", "\n")
        return rendered.strip()

    def build_context(self) -> str:
        return (
            f"Summarize the following conversation. "
            f"Keep the total summary under {self.max_summary_length} tokens. "
            f"Weight recent messages more heavily — the end of the transcript "
            f"is the active context.\n\n"
            f"{self.conversation_text}"
        )

    @property
    def output_schema(self) -> Type[BaseModel]:
        from src.core.context import ConversationSummary

        return ConversationSummary


class CurateKnowledgeTask(AuxAgentTask):
    """Extract knowledge notes from phase artifacts.

    Agent mode task that replaces the curator subjob.
    Prompt loaded from config/prompts/ via the prompt matrix.
    Uses kb_search, kb_write, kb_update, kb_read tools.
    """

    def __init__(
        self,
        phase_data: str,
        workspace_md: str,
        plan_md: str,
        existing_notes: List[str],
        kb_tools: list,
        prompt: str,
    ):
        self.phase_data = phase_data
        self.workspace_md = workspace_md
        self.plan_md = plan_md
        self.existing_notes = existing_notes
        self._kb_tools = kb_tools
        self._prompt = prompt

    @property
    def system_prompt(self) -> str:
        return self._prompt

    def build_context(self) -> str:
        parts = [
            "## Phase Artifacts",
            self.phase_data,
            "",
            "## Current Workspace",
            self.workspace_md,
            "",
            "## Current Plan",
            self.plan_md,
        ]
        if self.existing_notes:
            parts.extend(
                [
                    "",
                    "## Existing Knowledge (check before writing duplicates)",
                    "\n".join(self.existing_notes),
                ]
            )
        return "\n".join(parts)

    @property
    def output_schema(self) -> Type[BaseModel]:
        return CurationResult

    def get_tools(self) -> list:
        return self._kb_tools


class AssembleMemoriesTask(AuxAgentTask):
    """Review recent conversation and curate memory TTLs.

    Agent mode task (counterpart to ExtractMemoriesTask). Searches
    the memory DB for relevant missing memories and adjusts TTLs:
    boost relevant ones, deprecate stale ones.

    Prompt loaded from config/prompts/ via the prompt matrix.
    Uses memory_search, memory_boost, memory_deprecate tools.
    """

    def __init__(
        self,
        recent_context: str,
        current_injection: str,
        assembler_tools: list,
        prompt: str,
    ):
        self.recent_context = recent_context
        self.current_injection = current_injection
        self._tools = assembler_tools
        self._prompt = prompt

    @property
    def system_prompt(self) -> str:
        return self._prompt

    def build_context(self) -> str:
        parts = [
            "## Recent Conversation Context",
            self.recent_context,
            "",
            "## Currently Injected Memories",
            self.current_injection if self.current_injection else "(none)",
            "",
            "Review whether the right memories are being surfaced. "
            "Search for missing relevant memories and boost them. "
            "Deprecate pinned memories that are no longer relevant to the current work.",
        ]
        return "\n".join(parts)

    @property
    def output_schema(self) -> Type[BaseModel]:
        return AssemblyResult

    def get_tools(self) -> list:
        return self._tools


# =============================================================================
# AuxiliaryLLM — the unified executor
# =============================================================================

# Maps task class names to call_type values for archiving
_TASK_CALL_TYPES = {
    "SummarizeTask": "summarization",
    "ExtractMemoriesTask": "memory_extraction",
    "AssembleMemoriesTask": "memory_assembly",
    "CurateKnowledgeTask": "knowledge_curation",
}


def _get_model_name(llm: BaseChatModel) -> str:
    """Extract model name from a LangChain chat model."""
    for attr in ("model_name", "model"):
        if hasattr(llm, attr):
            return getattr(llm, attr)
    return "unknown"


@dataclass
class _TaskHealth:
    """Outcome counters for one auxiliary task family (e.g. memory_extraction)."""

    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    last_error_type: Optional[str] = None
    last_failure_at: Optional[float] = None  # epoch seconds
    last_success_at: Optional[float] = None


class AuxHealth:
    """Tracks auxiliary-task call outcomes so silent failures become visible.

    Auxiliary tasks (memory extraction/curation/assembly, session title
    generation) are deliberately *non-fatal*: their callers swallow exceptions
    so a degraded auxiliary model never fails a job or a chat turn. The cost is
    invisibility — on 2026-06-03 the auxiliary backend was unreachable for ~3
    days and produced no user- or operator-facing signal (no memories, no
    titles), because every failure logged at WARNING and was dropped.

    This tracker turns *sustained* failure into a single, alertable ERROR and
    exposes a snapshot via the agent status endpoint. It never changes control
    flow — callers still swallow and continue — and is itself best-effort.

    See docs/issues/surface_silent_aux_failures.md.
    """

    #: Consecutive failures (across any task) before the degraded ERROR fires.
    ESCALATE_AFTER = 3
    #: While degraded, re-emit the ERROR every Nth further failure so the alert
    #: stays live without flooding the log.
    REPEAT_EVERY = 20

    def __init__(self, model: str = "unknown") -> None:
        self.model = model
        self._tasks: Dict[str, _TaskHealth] = {}
        self._consecutive_failures = 0
        self._degraded = False

    def _task(self, task: str) -> _TaskHealth:
        t = self._tasks.get(task)
        if t is None:
            t = _TaskHealth()
            self._tasks[task] = t
        return t

    def record_success(self, task: str) -> None:
        """Record a successful auxiliary call; clears any degraded state."""
        t = self._task(task)
        t.successes += 1
        t.consecutive_failures = 0
        t.last_success_at = time.time()
        self._consecutive_failures = 0
        if self._degraded:
            self._degraded = False
            logger.error(
                "AUXILIARY MODEL RECOVERED: model=%s — task '%s' succeeded; "
                "memory/curation/titles resume.",
                self.model,
                task,
            )

    def record_failure(self, task: str, exc: BaseException) -> None:
        """Record a failed auxiliary call; escalates once it becomes sustained."""
        t = self._task(task)
        t.failures += 1
        t.consecutive_failures += 1
        t.last_error = str(exc)[:300]
        t.last_error_type = type(exc).__name__
        t.last_failure_at = time.time()
        self._consecutive_failures += 1

        n = self._consecutive_failures
        should_log = False
        if not self._degraded and n >= self.ESCALATE_AFTER:
            self._degraded = True
            should_log = True
        elif self._degraded and (n - self.ESCALATE_AFTER) % self.REPEAT_EVERY == 0:
            should_log = True
        if should_log:
            logger.error(
                "AUXILIARY MODEL DEGRADED: model=%s — %d consecutive auxiliary "
                "failures (latest task '%s': %s: %s). Memory extraction, "
                "knowledge curation and session titles are silently disabled "
                "until the auxiliary model is reachable again.",
                self.model,
                n,
                task,
                t.last_error_type,
                t.last_error,
            )

    @property
    def degraded(self) -> bool:
        return self._degraded

    def snapshot(self) -> Dict[str, Any]:
        """JSON-serializable health summary for status endpoints."""
        return {
            "model": self.model,
            "degraded": self._degraded,
            "consecutive_failures": self._consecutive_failures,
            "tasks": {
                name: {
                    "successes": t.successes,
                    "failures": t.failures,
                    "consecutive_failures": t.consecutive_failures,
                    "last_error_type": t.last_error_type,
                    "last_error": t.last_error,
                    "last_failure_at": t.last_failure_at,
                    "last_success_at": t.last_success_at,
                }
                for name, t in self._tasks.items()
            },
        }

    def heartbeat_summary(self) -> Dict[str, Any]:
        """Compact health projection carried on the agent → orchestrator heartbeat.

        Smaller than :meth:`snapshot`: just the degraded flag, the aggregate
        failure count, the model, and which tasks are currently failing (with
        their last error type). The orchestrator persists ``degraded`` to
        ``agents.aux_degraded`` (drives the admin badge) and stashes the rest in
        ``agents.metadata.aux`` for the badge tooltip. Per-task success counters
        and timestamps stay in :meth:`snapshot` / the ``/status`` endpoint.

        Always includes ``degraded`` (even when False) so a recovered agent's
        heartbeat clears the persisted flag.
        """
        failing = {
            name: {
                "consecutive_failures": t.consecutive_failures,
                "last_error_type": t.last_error_type,
            }
            for name, t in self._tasks.items()
            if t.consecutive_failures > 0
        }
        return {
            "degraded": self._degraded,
            "consecutive_failures": self._consecutive_failures,
            "model": self.model,
            "failing_tasks": failing,
        }


class AuxInputTooLarge(Exception):
    """Task input exceeds the auxiliary model's context window.

    Typed and deterministic — callers must NOT retry (the input doesn't
    shrink). Summarization avoids this via the SummarizationEngine's
    chunk planning; other aux tasks (memory extraction, titles) fail fast
    here instead of overflowing at the HTTP transport.
    See docs/features/context_summarization_rework.md (S1).
    """

    def __init__(self, tokens: int, limit: int, task_name: str = "unknown"):
        self.tokens = tokens
        self.limit = limit
        self.task_name = task_name
        super().__init__(
            f"Auxiliary task {task_name} input is ~{tokens:,} tokens, exceeding "
            f"the auxiliary model's {limit:,}-token context window"
        )


class AuxiliaryLLM:
    """Unified support task execution with chain and agent modes.

    All tasks use with_structured_output() for reliable structured returns.

    Args:
        llm: The support model (e.g. gpt-oss-120b, or main LLM as fallback)
        max_context_tokens: The support model's own context window, resolved
            from its settings at construction. Budgeting authority for the
            SummarizationEngine and the chain() pre-flight guard. None
            disables the guard (window unknown).
    """

    def __init__(
        self,
        llm: BaseChatModel,
        max_iterations: int = 15,
        timeout: float = 120.0,
        archiver: Optional["LLMArchiver"] = None,
        job_id: Optional[str] = None,
        agent_type: Optional[str] = None,
        max_context_tokens: Optional[int] = None,
    ):
        self.llm = llm
        self.max_iterations = max_iterations
        self.timeout = timeout
        self.max_context_tokens = max_context_tokens
        self._archiver = archiver
        self._job_id = job_id
        self._agent_type = agent_type or "unknown"
        #: Observability for silent non-fatal failures (see AuxHealth).
        self.health = AuxHealth(model=_get_model_name(llm))

    def set_job_context(
        self,
        archiver: Optional["LLMArchiver"],
        job_id: str,
        agent_type: str,
    ) -> None:
        """Set archiver and job context for logging. Called at job start."""
        self._archiver = archiver
        self._job_id = job_id
        self._agent_type = agent_type

    async def chain(self, task: AuxTask, timeout: Optional[float] = None) -> BaseModel:
        """Single LLM call: system prompt + context -> structured output.

        For tasks that need reasoning but no tool access.

        Args:
            task: AuxTask instance with system_prompt, build_context(), output_schema
            timeout: Per-call timeout override (seconds). Defaults to
                ``self.timeout``. Lets a long task (conversation summarization)
                request a larger budget without widening the short interactive
                default that protects every other aux task.

        Returns:
            Pydantic model instance matching task.output_schema

        Raises:
            asyncio.TimeoutError: If the LLM call exceeds timeout
        """
        structured_llm = self.llm.with_structured_output(
            task.output_schema, include_raw=True
        )
        messages = [
            SystemMessage(content=task.system_prompt),
            HumanMessage(content=task.build_context()),
        ]

        # Pre-flight: fail fast (typed, non-retryable) when the input cannot
        # fit the auxiliary model's own window, instead of overflowing at the
        # HTTP transport. Memory-extraction calls were shipping 951k-token
        # payloads to a 131k model (session_silent_failure_audit.md #7).
        if self.max_context_tokens:
            from src.core.summarizer import count_text_tokens

            input_tokens = count_text_tokens(messages[0].content) + count_text_tokens(
                messages[1].content
            )
            if input_tokens > self.max_context_tokens:
                task_name = task.__class__.__name__
                error = AuxInputTooLarge(
                    input_tokens, self.max_context_tokens, task_name
                )
                self.health.record_failure(task_name, error)
                raise error

        start = time.monotonic()
        raw_result = await self._invoke_aux(
            asyncio.wait_for(
                structured_llm.ainvoke(messages),
                timeout=timeout if timeout is not None else self.timeout,
            ),
            task=task,
            messages=messages,
            start=start,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        parsed = raw_result["parsed"]
        raw_response = raw_result["raw"]

        logger.debug(
            f"AuxiliaryLLM.chain completed: {task.__class__.__name__} -> "
            f"{type(parsed).__name__}"
        )

        self._archive_call(task, messages, raw_response, latency_ms)

        return parsed

    async def agent(self, task: AuxAgentTask) -> BaseModel:
        """Short-lived agent loop: system prompt + tools -> structured result.

        Runs a tool loop capped at max_iterations, then makes one final
        structured-output call to produce the result.

        Args:
            task: AuxAgentTask with system_prompt, build_context(),
                  output_schema, get_tools()

        Returns:
            Pydantic model instance matching task.output_schema

        Raises:
            asyncio.TimeoutError: If any individual LLM call exceeds timeout
        """
        tools = task.get_tools()
        from src.services.guardrails import apply_guardrails_to_tools

        tools = apply_guardrails_to_tools(tools, model=_get_model_name(self.llm))
        llm_with_tools = self.llm.bind_tools(tools)
        tool_map = {t.name: t for t in tools}

        messages: List[BaseMessage] = [
            SystemMessage(content=task.system_prompt),
            HumanMessage(content=task.build_context()),
        ]

        start = time.monotonic()
        tool_calls_made = 0
        for iteration in range(self.max_iterations):
            response = await self._invoke_aux(
                asyncio.wait_for(
                    llm_with_tools.ainvoke(messages),
                    timeout=self.timeout,
                ),
                task=task,
                messages=messages,
                start=start,
            )
            messages.append(response)

            if not response.tool_calls:
                # LLM is done with tool calls
                break

            # Execute tool calls
            for tool_call in response.tool_calls:
                tool_name = tool_call.get("name", "unknown")
                tool_args = tool_call.get("args", {})
                tool_id = tool_call.get("id", "")

                try:
                    tool_fn = tool_map.get(tool_name)
                    if tool_fn is None:
                        result = f"Error: Unknown tool '{tool_name}'"
                    else:
                        result = await tool_fn.ainvoke(tool_args)
                        if not isinstance(result, str):
                            result = str(result)
                    tool_calls_made += 1
                except Exception as e:
                    result = f"Error executing {tool_name}: {e}"
                    logger.warning(f"AuxiliaryLLM.agent: tool {tool_name} failed: {e}")

                messages.append(
                    ToolMessage(
                        content=result,
                        tool_call_id=tool_id,
                    )
                )

        iterations_used = iteration + 1

        logger.info(
            f"AuxiliaryLLM.agent completed: {task.__class__.__name__}, "
            f"{tool_calls_made} tool calls in {iterations_used} iterations"
        )

        # Final structured-output call to get the result
        structured_llm = self.llm.with_structured_output(
            task.output_schema, include_raw=True
        )
        messages.append(
            HumanMessage(
                content="Summarize what you accomplished in the required output format."
            )
        )

        raw_result = await self._invoke_aux(
            asyncio.wait_for(
                structured_llm.ainvoke(messages),
                timeout=self.timeout,
            ),
            task=task,
            messages=messages,
            start=start,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        parsed = raw_result["parsed"]
        raw_response = raw_result["raw"]

        self._archive_call(
            task,
            messages,
            raw_response,
            latency_ms,
            auxiliary_metadata={
                "iterations": iterations_used,
                "tool_calls_made": tool_calls_made,
            },
        )

        return parsed

    def _archive_call(
        self,
        task: AuxTask,
        messages: List[BaseMessage],
        response: AIMessage,
        latency_ms: int,
        auxiliary_metadata: Optional[dict] = None,
    ) -> None:
        """Archive an auxiliary LLM call. Fire-and-forget — never raises."""
        if not self._archiver or not self._job_id:
            return

        try:
            task_class = task.__class__.__name__
            call_type = _TASK_CALL_TYPES.get(task_class, "auxiliary")

            meta = {"task_class": task_class}
            if auxiliary_metadata:
                meta.update(auxiliary_metadata)

            self._archiver.archive(
                job_id=self._job_id,
                agent_type=self._agent_type,
                messages=messages,
                response=response,
                model=_get_model_name(self.llm),
                latency_ms=latency_ms,
                call_type=call_type,
                auxiliary_metadata=meta,
            )
        except Exception as e:
            logger.warning(
                f"Failed to archive auxiliary call ({task.__class__.__name__}): {e}"
            )

    def _archive_error(
        self,
        task: AuxTask,
        messages: List[BaseMessage],
        exc: BaseException,
        latency_ms: int,
    ) -> None:
        """Archive a FAILED auxiliary call so it surfaces in the debug view.

        Auxiliary failures are swallowed as non-fatal by their callers, so
        without this they leave no llm_requests row at all (unlike main-loop
        failures, which surface via the job's error state). Fire-and-forget.
        """
        if not self._archiver or not self._job_id:
            return
        try:
            task_class = task.__class__.__name__
            call_type = _TASK_CALL_TYPES.get(task_class, "auxiliary")
            self._archiver.archive_error(
                job_id=self._job_id,
                agent_type=self._agent_type,
                messages=messages,
                model=_get_model_name(self.llm),
                error=str(exc),
                error_type=type(exc).__name__,
                latency_ms=latency_ms,
                call_type=call_type,
                auxiliary_metadata={"task_class": task_class},
            )
        except Exception as e:
            logger.warning(
                f"Failed to archive auxiliary error ({task.__class__.__name__}): {e}"
            )

    async def _invoke_aux(
        self,
        awaitable,
        *,
        task: AuxTask,
        messages: List[BaseMessage],
        start: float,
    ):
        """Await an auxiliary LLM call; archive an error row + re-raise on failure.

        Centralizes the failure path for chain() and agent() so a failed
        auxiliary call is recorded to llm_requests before the exception
        propagates to the (swallowing) caller.
        """
        try:
            return await awaitable
        except Exception as exc:
            self._archive_error(
                task, messages, exc, int((time.monotonic() - start) * 1000)
            )
            raise


# =============================================================================
# Memory extraction helper
# =============================================================================

# Max messages to include in a single observation window
_MAX_OBSERVATION_WINDOW = 40


async def extract_and_store_memories(
    auxiliary_llm: "AuxiliaryLLM",
    recall_store,
    messages: List[BaseMessage],
    memory_extraction_prompt: str,
    phase: int = 0,
    source_turn_start: Optional[int] = None,
    source_turn_end: Optional[int] = None,
) -> int:
    """Extract memories via AuxiliaryLLM and store them in RecallStore.

    Runs the ExtractMemoriesTask in chain mode and stores each result.

    Args:
        auxiliary_llm: AuxiliaryLLM instance for extraction
        recall_store: RecallStore instance for storage
        messages: Conversation messages to extract from
        memory_extraction_prompt: System prompt for memory extraction
        phase: Current phase number
        source_turn_start: Start turn for windowed extraction (optional)
        source_turn_end: End turn for windowed extraction (optional)

    Returns:
        Number of memories successfully stored
    """
    try:
        # Cap the message window
        if len(messages) > _MAX_OBSERVATION_WINDOW:
            messages = messages[-_MAX_OBSERVATION_WINDOW:]

        if not messages:
            return 0

        task = ExtractMemoriesTask(
            messages=messages, prompt=memory_extraction_prompt, phase=phase
        )
        result = await auxiliary_llm.chain(task)

        stored_count = 0
        for mem in result.memories:
            try:
                mem_id = await recall_store.store(
                    content=mem.content,
                    summary=mem.summary,
                    keywords=mem.keywords,
                    importance=mem.importance,
                    memory_type=mem.type,
                    source="observer",
                    source_turn_start=source_turn_start,
                    source_turn_end=source_turn_end,
                    source_phase=phase,
                    retrieval_messages=mem.retrieval_messages or None,
                )
                if mem_id:
                    stored_count += 1
            except Exception as e:
                logger.warning(
                    "Memory extraction: failed to store memory: %s: %s",
                    type(e).__name__,
                    e,
                )

        logger.info(
            f"Memory extraction: extracted {len(result.memories)}, "
            f"stored {stored_count} (phase {phase})"
        )
        auxiliary_llm.health.record_success("memory_extraction")
        return stored_count

    except Exception as e:
        auxiliary_llm.health.record_failure("memory_extraction", e)
        # Include the type — bare openai exceptions can format as "" (B1
        # follow-up; same fix as persistent_graph retrieval logging).
        logger.warning(
            "Memory extraction failed (non-fatal): %s: %s", type(e).__name__, e
        )
        return 0


async def curate_and_store_knowledge(
    auxiliary_llm: "AuxiliaryLLM",
    tool_context: Any,
    phase_data: str,
    workspace_md: str,
    plan_md: str,
    curation_prompt: str,
) -> Optional["CurationResult"]:
    """Run inline knowledge curation via AuxiliaryLLM agent mode.

    Replaces the curator subjob. Extracts knowledge notes from phase artifacts
    and writes them to the project knowledge base (Neo4j + pgvector).

    Args:
        auxiliary_llm: AuxiliaryLLM instance
        tool_context: ToolContext with knowledge_graph and knowledge_store
        phase_data: Formatted phase context (archive path, completed todos)
        workspace_md: Current workspace.md content
        plan_md: Current plan.md content
        curation_prompt: System prompt for knowledge curation

    Returns:
        CurationResult on success, None on failure or if KB not available
    """
    try:
        kg = tool_context.knowledge_graph
        ks = tool_context.knowledge_store
        project_id = tool_context.project_id

        if not kg or not ks or not project_id:
            return None

        # Get existing notes for duplicate-aware context
        existing_notes = []
        try:
            notes = kg.list_notes(project_id=project_id, limit=50)
            existing_notes = [
                f"- {n.get('id', '?')}: {n.get('title', '?')} ({n.get('type', '?')})"
                for n in notes
            ]
        except Exception as e:
            logger.debug(f"Could not fetch existing notes: {e}")

        # Create KB tools for the curation agent
        from src.tools.knowledge.knowledge_tools import create_kb_tools

        kb_tools = create_kb_tools(tool_context)

        task = CurateKnowledgeTask(
            phase_data=phase_data,
            workspace_md=workspace_md,
            plan_md=plan_md,
            existing_notes=existing_notes,
            kb_tools=kb_tools,
            prompt=curation_prompt,
        )

        result = await auxiliary_llm.agent(task)

        logger.info(
            f"Inline curation complete: {result.notes_created} created, "
            f"{result.notes_updated} updated — {result.summary}"
        )
        auxiliary_llm.health.record_success("knowledge_curation")
        return result

    except Exception as e:
        auxiliary_llm.health.record_failure("knowledge_curation", e)
        logger.warning(
            "Inline curation failed (non-fatal): %s: %s", type(e).__name__, e
        )
        return None


def _should_extract_memories(
    turn_count: int,
    interval: int,
    last_observed_turn: int,
) -> bool:
    """Check if memory extraction should run on this turn.

    Args:
        turn_count: Current turn count
        interval: Extraction interval (every N turns)
        last_observed_turn: Last turn when extraction ran

    Returns:
        True if extraction should run
    """
    if turn_count <= 0:
        return False
    if turn_count <= last_observed_turn:
        return False
    return turn_count % interval == 0


def _should_assemble_memories(
    turn_count: int,
    interval: int,
    last_assembled_turn: int,
) -> bool:
    """Check if memory assembler should run on this turn.

    Args:
        turn_count: Current turn count
        interval: Assembly interval (every N turns)
        last_assembled_turn: Last turn when assembler ran

    Returns:
        True if assembler should run
    """
    if turn_count <= 0:
        return False
    if turn_count <= last_assembled_turn:
        return False
    return turn_count % interval == 0


async def assemble_memories(
    auxiliary_llm: "AuxiliaryLLM",
    recall_store,
    messages: List[BaseMessage],
    current_injection_text: str,
    memory_assembler_prompt: str,
) -> Optional["AssemblyResult"]:
    """Run the memory assembler to review and adjust memory TTLs.

    Counterpart to extract_and_store_memories. While the extractor
    creates new memories, the assembler curates existing ones by
    adjusting their TTLs (boost relevant, deprecate stale).

    Args:
        auxiliary_llm: AuxiliaryLLM instance for agent-mode execution
        recall_store: RecallStore instance (passed to assembler tools)
        messages: Recent conversation messages for context
        current_injection_text: Currently injected memory block text
        memory_assembler_prompt: System prompt for memory assembly

    Returns:
        AssemblyResult on success, None on failure
    """
    try:
        # Cap the message window
        if len(messages) > _MAX_OBSERVATION_WINDOW:
            messages = messages[-_MAX_OBSERVATION_WINDOW:]

        if not messages:
            return None

        recent_context = _format_messages_for_extraction(messages)

        from src.services.assembler_tools import create_assembler_tools

        assembler_tools = create_assembler_tools(recall_store)

        task = AssembleMemoriesTask(
            recent_context=recent_context,
            current_injection=current_injection_text,
            assembler_tools=assembler_tools,
            prompt=memory_assembler_prompt,
        )

        result = await auxiliary_llm.agent(task)

        actions_count = len(result.actions_taken) if result.actions_taken else 0
        gaps_count = len(result.gaps_identified) if result.gaps_identified else 0
        logger.info(
            f"Memory assembly: {actions_count} TTL adjustments, "
            f"{gaps_count} gaps identified — {result.summary}"
        )
        auxiliary_llm.health.record_success("memory_assembly")
        return result

    except Exception as e:
        auxiliary_llm.health.record_failure("memory_assembly", e)
        logger.warning(
            "Memory assembly failed (non-fatal): %s: %s", type(e).__name__, e
        )
        return None


# =============================================================================
# Helpers
# =============================================================================


def _format_messages_for_extraction(messages: List[BaseMessage]) -> str:
    """Format messages into readable text for the extraction LLM.

    Filters out injection messages (workspace, memory, instruction)
    to focus on actual conversation content.
    """
    from src.core.workspace_injection import is_workspace_injection_message

    lines = []
    for msg in messages:
        if is_workspace_injection_message(msg):
            continue

        role = _get_message_role(msg)
        content = msg.content if hasattr(msg, "content") else ""

        if not content or not content.strip():
            continue

        # Truncate very long tool results
        if isinstance(msg, ToolMessage) and len(content) > 1000:
            content = content[:1000] + "... [truncated]"

        lines.append(f"[{role}] {content}")

    return "\n\n".join(lines)


def _get_message_role(msg: BaseMessage) -> str:
    """Get a human-readable role label for a message."""
    if isinstance(msg, HumanMessage):
        return "User"
    elif isinstance(msg, AIMessage):
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_names = ", ".join(tc.get("name", "?") for tc in msg.tool_calls)
            return f"Agent (calls: {tool_names})"
        return "Agent"
    elif isinstance(msg, ToolMessage):
        return "Tool Result"
    return "System"
