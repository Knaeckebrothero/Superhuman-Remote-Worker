"""Aux-budgeted rolling-fold summarization engine.

Design: docs/features/context_summarization_rework.md (slices S1+S2).

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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import tiktoken

    TIKTOKEN_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    TIKTOKEN_AVAILABLE = False

# Fraction of the summarizer's window usable after safety margin. Covers
# tokenizer disagreement between our counter and the endpoint's, message
# framing overhead, and provider-side additions.
SAFETY_MARGIN = 0.85

# Fixed allowance for the structured-output schema the SDK appends to the
# request (tool/JSON-schema definition for ConversationSummary).
SCHEMA_OVERHEAD_TOKENS = 2_000

# Retry policy per fold call — rides out transient aux-endpoint failures
# (503 flap, ReadTimeout) without falling back to a different algorithm.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (5.0, 15.0)  # sleep after attempt 1, attempt 2

# Conservative chars-per-token used only when tiktoken is unavailable.
# 3.5 overestimates token counts for typical English/code, which errs toward
# smaller (safer) chunks.
_FALLBACK_CHARS_PER_TOKEN = 3.5

# Reserved tokens per split piece: the "[part i/j …]" marker plus
# tokenizer-boundary inflation from cutting mid-token.
_SPLIT_MARKER_RESERVE_TOKENS = 64

# Floor when the auxiliary window is unknown: matches the historical
# conservative default base (see LimitsConfig.model_max_context_tokens).
DEFAULT_AUX_WINDOW = 100_000

ProgressCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


class SummarizationFailed(Exception):
    """Summarization could not produce a summary; callers keep raw history.

    ``reason`` is machine-readable and lands in the ``compaction.failed``
    event: ``aux_unavailable`` (retries exhausted), ``aux_overflow``
    (a planned call still overflowed — a plan bug, never retried),
    ``aux_window_too_small`` (window can't fit prompt + output budget).
    """

    def __init__(
        self,
        reason: str,
        message: str = "",
        pass_index: int = 0,
        n_passes: int = 0,
    ):
        self.reason = reason
        self.pass_index = pass_index
        self.n_passes = n_passes
        super().__init__(message or reason)


@dataclass
class ChunkSpec:
    """One fold pass: a contiguous slice of formatted conversation parts."""

    index: int  # 1-based pass number
    first_part: int  # 1-based ordinal of the first conversation part
    last_part: int  # 1-based ordinal of the last conversation part
    text: str
    tokens: int


@dataclass
class SummarizationPlan:
    """Deterministic chunking plan computed before any LLM call."""

    chunks: List[ChunkSpec] = field(default_factory=list)
    total_tokens: int = 0
    chunk_budget: int = 0
    aux_window: int = 0

    @property
    def n_passes(self) -> int:
        return len(self.chunks)

    def describe(self) -> List[Dict[str, int]]:
        """Compact plan representation for the ``compaction.started`` event."""
        return [
            {
                "pass": c.index,
                "first_msg": c.first_part,
                "last_msg": c.last_part,
                "tokens": c.tokens,
            }
            for c in self.chunks
        ]


_ENCODING_CACHE: Dict[str, Any] = {}


def count_text_tokens(text: str, model: Optional[str] = None) -> int:
    """Count tokens in a plain string with tiktoken, conservative fallback.

    The summarization *plan* must use real token counts: the historical
    ``len(text) // 4`` estimate let a 951k-token conversation slip past a
    943k gate (it under-counts), straight into a 131k summarizer.
    """
    if not text:
        return 0
    if TIKTOKEN_AVAILABLE:
        key = model or "_default"
        encoding = _ENCODING_CACHE.get(key)
        if encoding is None:
            try:
                encoding = tiktoken.encoding_for_model(model) if model else None
            except (KeyError, ValueError):
                encoding = None
            if encoding is None:
                encoding = tiktoken.get_encoding("cl100k_base")
            _ENCODING_CACHE[key] = encoding
        try:
            return len(encoding.encode(text, disallowed_special=()))
        except Exception:  # pragma: no cover - defensive
            pass
    return math.ceil(len(text) / _FALLBACK_CHARS_PER_TOKEN)


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
            anchor_parts = []
            if identity_anchor.get("agent_role"):
                anchor_parts.append(f"Role: {identity_anchor['agent_role']}")
            if identity_anchor.get("current_task"):
                anchor_parts.append(f"Task: {identity_anchor['current_task']}")
            if identity_anchor.get("active_constraints"):
                constraints = identity_anchor["active_constraints"]
                if isinstance(constraints, list):
                    anchor_parts.append("Constraints: " + "; ".join(constraints))
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
        """Max input tokens of conversation text per fold call.

        ``safe_input`` leaves room for prompt overhead and the model's output;
        the running summary (bounded by ``output_budget``) rides along with
        every chunk, so it is subtracted once more.
        """
        safe_input = (
            math.floor(self.aux_window * SAFETY_MARGIN)
            - self._overhead
            - self.output_budget
        )
        return safe_input - self.output_budget

    def plan(self, formatted_parts: List[str]) -> SummarizationPlan:
        """Pack formatted conversation parts into within-budget chunks.

        Pure and deterministic — no LLM calls. Oversized single parts (one
        giant tool result) are hard-split so no chunk can exceed the budget.

        Raises:
            SummarizationFailed: ``aux_window_too_small`` when the window
                cannot fit even the prompt + output budget.
        """
        budget = self.chunk_budget
        if budget < 1_000:
            raise SummarizationFailed(
                "aux_window_too_small",
                f"Auxiliary window {self.aux_window} cannot fit prompt overhead "
                f"({self._overhead}) + output budget ({self.output_budget})",
            )

        # Expand oversized parts into sub-parts that each fit the budget.
        expanded: List[tuple] = []  # (part_ordinal, text, tokens)
        for ordinal, part in enumerate(formatted_parts, start=1):
            tokens = self._count(part)
            if tokens <= budget:
                expanded.append((ordinal, part, tokens))
                continue
            # Size pieces against a reduced budget so the marker prefix and
            # mid-token cut inflation can't push a piece over the real budget.
            effective = max(budget - _SPLIT_MARKER_RESERVE_TOKENS, 1)
            n_pieces = math.ceil(tokens / effective)
            # Split by chars proportionally; re-measure each piece.
            piece_chars = math.ceil(len(part) / n_pieces)
            for i in range(n_pieces):
                piece = part[i * piece_chars : (i + 1) * piece_chars]
                if not piece:
                    continue
                marked = f"[part {i + 1}/{n_pieces} of an oversized message]\n{piece}"
                expanded.append((ordinal, marked, self._count(marked)))

        chunks: List[ChunkSpec] = []
        current_parts: List[str] = []
        current_tokens = 0
        current_first: Optional[int] = None
        current_last: Optional[int] = None
        total_tokens = 0

        def _flush() -> None:
            nonlocal current_parts, current_tokens, current_first, current_last
            if not current_parts:
                return
            chunks.append(
                ChunkSpec(
                    index=len(chunks) + 1,
                    first_part=current_first or 1,
                    last_part=current_last or (current_first or 1),
                    text="\n".join(current_parts),
                    tokens=current_tokens,
                )
            )
            current_parts = []
            current_tokens = 0
            current_first = None
            current_last = None

        for ordinal, text, tokens in expanded:
            total_tokens += tokens
            if current_tokens + tokens > budget and current_parts:
                _flush()
            if current_first is None:
                current_first = ordinal
            current_last = ordinal
            current_parts.append(text)
            current_tokens += tokens
        _flush()

        return SummarizationPlan(
            chunks=chunks,
            total_tokens=total_tokens,
            chunk_budget=budget,
            aux_window=self.aux_window,
        )

    # ------------------------------------------------------------------
    # Fold loop
    # ------------------------------------------------------------------

    async def run(
        self,
        plan: SummarizationPlan,
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
        self, fold_text: str, plan: SummarizationPlan, chunk: ChunkSpec
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
        result = await self.auxiliary.chain(task, timeout=self.call_timeout)
        return format_structured_summary(result)

    async def _emit_progress(
        self,
        plan: SummarizationPlan,
        chunk: ChunkSpec,
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
