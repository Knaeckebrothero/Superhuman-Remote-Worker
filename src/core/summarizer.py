"""Aux-budgeted rolling-fold summarization engine.

Design: knowledge-base/knowledge/features/context_summarization_rework.md (slices S1+S2).

Replaces the recursive map-reduce path (``_recursive_summarize``) and the
unstructured fallback that used to live in
``ContextManager._single_pass_summarize``. One algorithm:

1. ``plan()``   — measure the formatted conversation with a real tokenizer and
   pack it into chunks sized for the *summarizer's own* context window
   (``AuxiliaryLLM.max_context_tokens``), never the main model's.
2. ``run()``    — sequentially fold: ``summary_i = summarize(summary_{i-1} +
   chunk_i)``. Every call is within-budget by construction; passes are linear
   (``ceil(input/chunk_budget)``), so the engine scales to arbitrarily large
   inputs without recursion depth limits.

Robustness comes from bounded retries with backoff on the *same* structured
call — there is no fallback to a second, differently-shaped summarizer. On
exhaustion the engine raises :class:`SummarizationFailed` and the caller keeps
the original history (never a placeholder).

Progress is emitted through an optional async callback so the transport layer
decides what to do with it (persistent sessions broadcast SSE frames; worker
agents log).
"""

import asyncio
import logging
import math
from typing import Any, Awaitable, Callable, Dict, List, Optional

from openai import BadRequestError
from pydantic import ValidationError

from src.core.chunk_planner import (
    DEFAULT_AUX_WINDOW,
    SCHEMA_OVERHEAD_TOKENS,
    Chunk,
    ChunkPlan,
    ChunkPlanner,
    SummarizationFailed,
    count_text_tokens,
)
from src.core.context import IdentityAnchor
from src.core.llm_retry import NO_RETRY

logger = logging.getLogger(__name__)

# Retry policy per fold call — rides out transient aux-endpoint failures
# (503 flap, ReadTimeout) without falling back to a different algorithm.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (5.0, 15.0)  # sleep after attempt 1, attempt 2

ProgressCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


def format_structured_summary(result: Any) -> str:
    """Render a ConversationSummary-shaped object into readable summary text.

    Moved from ``ContextManager._single_pass_summarize``; duck-typed so the
    engine doesn't import the schema class.
    """
    parts: List[str] = []

    def _text(attr: str) -> str:
        value = getattr(result, attr, None)
        return value.strip() if isinstance(value, str) else ""

    if _text("summary"):
        parts.append(f"**Summary:**\n{_text('summary')}")
    if _text("tasks_completed"):
        parts.append(f"**Tasks Completed:**\n{_text('tasks_completed')}")
    if _text("tasks_in_progress"):
        parts.append(f"**Tasks In Progress:**\n{_text('tasks_in_progress')}")
    if _text("key_decisions"):
        parts.append(f"**Key Decisions:**\n{_text('key_decisions')}")
    if _text("current_state"):
        parts.append(f"**Current State:**\n{_text('current_state')}")
    if _text("blockers"):
        parts.append(f"**Blockers:**\n{_text('blockers')}")
    if _text("critical_facts"):
        parts.append(f"**Critical Facts:**\n{_text('critical_facts')}")
    if _text("state_changes"):
        parts.append(f"**State Changes:**\n{_text('state_changes')}")
    if _text("pinned_instructions"):
        parts.append(f"**Pinned Instructions:**\n{_text('pinned_instructions')}")

    identity_anchor = getattr(result, "identity_anchor", None)
    if identity_anchor:
        if isinstance(identity_anchor, dict):
            identity_anchor = IdentityAnchor(**identity_anchor)

        if isinstance(identity_anchor, IdentityAnchor):
            anchor_parts = []
            if identity_anchor.agent_role:
                anchor_parts.append(f"Role: {identity_anchor.agent_role}")
            if identity_anchor.current_task:
                anchor_parts.append(f"Task: {identity_anchor.current_task}")
            if identity_anchor.active_constraints:
                anchor_parts.append(
                    "Constraints: " + "; ".join(identity_anchor.active_constraints)
                )
            if anchor_parts:
                parts.append("**Identity Anchor:**\n" + "\n".join(anchor_parts))
        elif isinstance(identity_anchor, str) and identity_anchor.strip():
            parts.append(f"**Identity Anchor:**\n{identity_anchor.strip()}")

    return "\n\n".join(parts)


def is_overflow_error(exc: BaseException) -> bool:
    """True when the error chain indicates a context-window overflow.

    These are deterministic (a planned call that overflows will overflow on
    every retry), so the fold loop must NOT retry them. Recognizes the typed
    ``ContextOverflowError`` anywhere in the cause chain, the synthetic
    HTTP 413 the capture client returns (``code: context_overflow`` — see
    ``src/llm/reasoning_chat.py``), and the AuxiliaryLLM pre-flight guard.
    """
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        if name in ("ContextOverflowError", "AuxInputTooLarge"):
            return True
        if getattr(current, "status_code", None) == 413:
            return True
        if (
            isinstance(current, BadRequestError)
            and str(getattr(current, "code", "") or "").lower() == "invalid_json_schema"
        ):
            return True
        if isinstance(current, ValidationError):
            return True
        text = str(current)
        if "context_overflow" in text or "exceeds limit of" in text:
            return True
        current = current.__cause__ or current.__context__
    return False


def _describe_exc(exc: Optional[BaseException]) -> str:
    """Readable exception text — names the type when ``str()`` is empty.

    ``asyncio.TimeoutError`` (and other arg-less exceptions) stringify to "",
    which logged as the infamous ``failed ()`` during the 5dbb5770 incident,
    hiding that the aux model was timing out on base64-laden folds.
    """
    if exc is None:
        return "unknown error"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class SummarizationEngine:
    """Plan-then-fold summarization sized for the auxiliary model's window."""

    def __init__(
        self,
        auxiliary,
        *,
        summarization_prompt: Optional[str] = None,
        max_summary_length: int = 10000,
        call_timeout: float = 240.0,
        progress_cb: Optional[ProgressCallback] = None,
        counting_model: Optional[str] = None,
        token_counter: Optional[Callable[[str], int]] = None,
    ):
        """
        Args:
            auxiliary: AuxiliaryLLM instance. Its ``max_context_tokens``
                (resolved from the aux model's settings at construction) is
                the budgeting authority.
            summarization_prompt: Pre-rendered prompt template (may be None).
            max_summary_length: Max summary length in characters (the
                structured task's contract); also bounds the running summary.
            call_timeout: Per fold-call timeout in seconds. Replaces the old
                single 600s blob — N passes get N bounded calls.
            progress_cb: Optional async ``(event_name, params)`` callback.
            counting_model: Model name for tokenizer selection (best effort).
            token_counter: Override for text token counting (tests).
        """
        self.auxiliary = auxiliary
        self.summarization_prompt = summarization_prompt
        self.max_summary_length = max_summary_length
        self.call_timeout = call_timeout
        self.progress_cb = progress_cb
        self.counting_model = counting_model
        self._token_counter = token_counter

        window = getattr(auxiliary, "max_context_tokens", None)
        if not window or window <= 0:
            logger.warning(
                "SummarizationEngine: auxiliary model window unknown — "
                f"falling back to conservative {DEFAULT_AUX_WINDOW} tokens"
            )
            window = DEFAULT_AUX_WINDOW
        self.aux_window = int(window)

        # Output budget: the summary is bounded by max_summary_length chars.
        # ~3 chars/token is deliberately conservative (reserves more room).
        self.output_budget = max(1_000, math.ceil(max_summary_length / 3))
        self._overhead = self._measure_overhead()

        # The fold subtracts the output budget twice — once for the model's
        # output, once because the rolling summary rides along every chunk — so
        # both reserves are ``output_budget``. No overlap: sequential coherence
        # covers chunk seams. These arguments make the planner byte-for-byte
        # identical to the pre-extraction packer.
        self._planner = ChunkPlanner(
            self.aux_window,
            overhead_tokens=self._overhead,
            output_reserve=self.output_budget,
            carry_reserve=self.output_budget,
            overlap_ratio=0.0,
            token_counter=self._token_counter,
            counting_model=self.counting_model,
        )

    # ------------------------------------------------------------------
    # Budget + plan
    # ------------------------------------------------------------------

    def _count(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter(text)
        return count_text_tokens(text, self.counting_model)

    def _measure_overhead(self) -> int:
        """Measure the system prompt's token cost (+ schema allowance)."""
        try:
            from src.services.auxiliary import SummarizeTask

            probe = SummarizeTask(
                conversation_text="",
                summarization_prompt=self.summarization_prompt or "",
                max_summary_length=self.max_summary_length,
            )
            prompt_tokens = self._count(probe.system_prompt)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"Prompt overhead probe failed, using fixed value: {e}")
            prompt_tokens = 3_000
        return prompt_tokens + SCHEMA_OVERHEAD_TOKENS

    @property
    def chunk_budget(self) -> int:
        """Max input tokens of conversation text per fold call (delegated)."""
        return self._planner.chunk_budget

    def plan(self, formatted_parts: List[str]) -> ChunkPlan:
        """Pack formatted conversation parts into within-budget fold chunks.

        Thin delegator to the shared :class:`ChunkPlanner` (pure, deterministic,
        no LLM calls). Oversized single parts are hard-split so no chunk exceeds
        the budget.

        Raises:
            SummarizationFailed: ``aux_window_too_small`` when the window
                cannot fit even the prompt + reserves.
        """
        return self._planner.plan(formatted_parts)

    # ------------------------------------------------------------------
    # Fold loop
    # ------------------------------------------------------------------

    async def run(
        self,
        plan: ChunkPlan,
        *,
        seed_summary: Optional[str] = None,
        focus: Optional[str] = None,
    ) -> str:
        """Execute the fold loop over a plan; returns the final summary.

        Args:
            plan: Output of :meth:`plan`.
            seed_summary: Prior summary text to incorporate (rolling-summary
                continuation — replaces the old "prepend old summary
                messages" pattern).
            focus: Optional user-provided compaction focus (``/compact
                <focus>``), honored in every fold call.

        Raises:
            SummarizationFailed: when any pass exhausts retries (the caller
                must keep the original messages).
            asyncio.CancelledError: passed through untouched so hard
                interrupts keep working.
        """
        if not plan.chunks:
            raise SummarizationFailed("empty_plan", "Nothing to summarize")

        summary = seed_summary
        for chunk in plan.chunks:
            fold_segments: List[str] = []
            if summary:
                fold_segments.append(f"Prior Summary: {summary}")
            if focus:
                fold_segments.append(f"User compaction focus: {focus}")
            fold_segments.append(chunk.text)
            fold_text = "\n".join(fold_segments)

            await self._emit_progress(plan, chunk, attempt=1, out_tokens=None)
            summary = await self._call_with_retries(fold_text, plan, chunk)
            await self._emit_progress(
                plan, chunk, attempt=1, out_tokens=self._count(summary)
            )

        return summary or ""

    async def _call_with_retries(
        self, fold_text: str, plan: ChunkPlan, chunk: Chunk
    ) -> str:
        last_error: Optional[BaseException] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await self._call_once(fold_text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if is_overflow_error(e):
                    # Deterministic — a planned call must never overflow.
                    logger.error(
                        f"Summarization pass {chunk.index}/{plan.n_passes} "
                        f"overflowed despite planning ({chunk.tokens} tokens, "
                        f"budget {plan.chunk_budget}, window {plan.aux_window}): {e}"
                    )
                    raise SummarizationFailed(
                        "aux_overflow",
                        str(e),
                        pass_index=chunk.index,
                        n_passes=plan.n_passes,
                    ) from e
                last_error = e
                if attempt == MAX_ATTEMPTS:
                    break
                backoff = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                logger.warning(
                    f"Summarization pass {chunk.index}/{plan.n_passes} attempt "
                    f"{attempt}/{MAX_ATTEMPTS} failed ({_describe_exc(e)}); "
                    f"retrying in {backoff}s"
                )
                await self._emit_progress(
                    plan, chunk, attempt=attempt + 1, out_tokens=None
                )
                await asyncio.sleep(backoff)

        logger.error(
            f"Summarization pass {chunk.index}/{plan.n_passes} failed after "
            f"{MAX_ATTEMPTS} attempts: {_describe_exc(last_error)}"
        )
        raise SummarizationFailed(
            "aux_unavailable",
            str(last_error),
            pass_index=chunk.index,
            n_passes=plan.n_passes,
        ) from last_error

    async def _call_once(self, conversation_text: str) -> str:
        """One structured summarization call. No fallback variants."""
        from src.services.auxiliary import SummarizeTask

        task = SummarizeTask(
            conversation_text=conversation_text,
            summarization_prompt=self.summarization_prompt or "",
            max_summary_length=self.max_summary_length,
        )
        # NO_RETRY: the fold loop above already owns the retry for this call
        # (MAX_ATTEMPTS + BACKOFF_SECONDS + the is_overflow_error gate). Letting
        # AuxiliaryLLM add its own would double the provider calls per fold and
        # hide the first failure from `_emit_progress`, so the cockpit would show
        # attempt 1 twice instead of 1 then 2.
        result = await self.auxiliary.chain(
            task, timeout=self.call_timeout, retry_policy=NO_RETRY
        )
        return format_structured_summary(result)

    async def _emit_progress(
        self,
        plan: ChunkPlan,
        chunk: Chunk,
        *,
        attempt: int,
        out_tokens: Optional[int],
    ) -> None:
        if self.progress_cb is None:
            return
        try:
            await self.progress_cb(
                "compaction.progress",
                {
                    "pass": chunk.index,
                    "n_passes": plan.n_passes,
                    "first_msg": chunk.first_part,
                    "last_msg": chunk.last_part,
                    "in_tokens": chunk.tokens,
                    "out_tokens": out_tokens,
                    "stage": "summarizing",
                    "attempt": attempt,
                },
            )
        except Exception as e:  # progress must never break the fold
            logger.debug(f"Compaction progress emit failed (non-fatal): {e}")
