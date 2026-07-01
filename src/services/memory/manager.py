"""MemoryManager — the single seam for all agent memory.

Design: docs/features/agent_memory_overhaul.md §2.1. Both graphs hold
exactly one of these; neither touches RecallStore/KnowledgeStore directly
(Phase-1 acceptance). The manager is a binder (P6): ``from_config``
resolves named plugins from the registry and wires the pipeline — it
holds no memory logic itself.

Error philosophy (the aux-outage lesson): assemble() and capture() never
raise into the graphs — memory is an enhancement, a broken plugin must
not kill a turn — but every contained failure is logged with the
exception type and recorded in AssembleStats.errors. Loud degradation,
never silent.
"""

import asyncio
import logging
from typing import Any, List, Optional, Set, Tuple

from .registry import resolve_memory_plugin
from .types import (
    AssembleRequest,
    AssembleStats,
    Candidate,
    CaptureEvent,
    InjectionBlock,
    MemoryPayload,
    MemoryRuntime,
    Scored,
    _StopWatch,
)

logger = logging.getLogger(__name__)

#: (name, instance) — names kept for stats, errors, and status surfaces.
NamedPlugin = Tuple[str, Any]


class MemoryManager:
    """Single seam for all memory: assembly (read) + capture (write)."""

    def __init__(
        self,
        runtime: MemoryRuntime,
        *,
        retrievers: Optional[List[NamedPlugin]] = None,
        scorers: Optional[List[NamedPlugin]] = None,
        policies: Optional[List[NamedPlugin]] = None,
        writers: Optional[List[NamedPlugin]] = None,
        extensions: Optional[List[NamedPlugin]] = None,
    ) -> None:
        self.runtime = runtime
        self._retrievers = retrievers or []
        self._scorers = scorers or []
        self._policies = policies or []
        self._writers = writers or []
        self._extensions = extensions or []
        #: Strong refs to detached capture() tasks (capture_nowait). Without
        #: this the event loop only holds a weak ref and a long-running task
        #: (the chunked pre_compaction extraction) can be GC'd mid-flight.
        self._bg_tasks: Set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Binding
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: Any, runtime: MemoryRuntime) -> "MemoryManager":
        """Bind the pipeline declared in ``memory.pipeline`` (P6).

        Args:
            cfg: MemoryConfig (duck-typed: needs ``.pipeline`` with
                retrievers/scorers/policies/writers/extensions name lists;
                an absent/None pipeline binds an empty no-op manager).
            runtime: Shared dependency bundle handed to every factory.

        Raises:
            UnknownMemoryPluginError: a configured name isn't registered —
                misconfiguration fails at bind time, not mid-turn.
        """
        pipeline = getattr(cfg, "pipeline", None)
        if runtime.memory_config is None:
            runtime.memory_config = cfg

        def _bind(kind: str, names: List[str]) -> List[NamedPlugin]:
            bound: List[NamedPlugin] = []
            for name in names:
                spec = resolve_memory_plugin(kind, name)
                bound.append((name, spec.factory(runtime)))
            return bound

        manager = cls(
            runtime,
            retrievers=_bind("retriever", getattr(pipeline, "retrievers", []) or []),
            scorers=_bind("scorer", getattr(pipeline, "scorers", []) or []),
            policies=_bind("policy", getattr(pipeline, "policies", []) or []),
            writers=_bind("writer", getattr(pipeline, "writers", []) or []),
            extensions=_bind("extension", getattr(pipeline, "extensions", []) or []),
        )
        logger.info(
            "MemoryManager bound: %s",
            manager.pipeline_summary(),
        )
        return manager

    def pipeline_summary(self) -> dict:
        """Bound plugin names per stage — status surfaces + bind logging."""
        return {
            "retrievers": [name for name, _ in self._retrievers],
            "scorers": [name for name, _ in self._scorers],
            "policies": [name for name, _ in self._policies],
            "writers": [name for name, _ in self._writers],
            "extensions": [name for name, _ in self._extensions],
        }

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    async def assemble(self, req: AssembleRequest) -> MemoryPayload:
        """Run the read pipeline: retrievers → scorers → policies → blocks.

        Order-preserving by default: with no scorers/policies bound, items
        flow through in retriever order (the legacy stores already return
        ranked results). Stage failures are contained per plugin — a
        failing scorer/policy passes items through unchanged, a failing
        retriever contributes nothing — and recorded in stats.errors.
        Never raises.
        """
        watch = _StopWatch()
        stats = AssembleStats()

        try:
            candidates: List[Candidate] = []
            for name, retriever in self._retrievers:
                try:
                    found = await retriever.retrieve(req) or []
                    for candidate in found:
                        if candidate.retriever is None:
                            candidate.retriever = name
                    stats.per_retriever[name] = len(found)
                    candidates.extend(found)
                except Exception as e:
                    self._record_failure(stats, "retriever", name, e)
                    stats.per_retriever[name] = 0
            stats.candidates_total = len(candidates)

            items: List[Scored] = [Scored(candidate=c) for c in candidates]

            for name, scorer in self._scorers:
                try:
                    items = await scorer.score(req, items)
                except Exception as e:
                    self._record_failure(stats, "scorer", name, e)

            for name, policy in self._policies:
                try:
                    items = await policy.apply(req, items)
                except Exception as e:
                    self._record_failure(stats, "policy", name, e)

            blocks = self._render_blocks(req, items)

            stats.injected_total = sum(len(b.items) for b in blocks)
            stats.tokens_injected = sum(b.token_count for b in blocks)
            stats.blocks = len(blocks)
            stats.latency_ms = watch.elapsed_ms()
            return MemoryPayload(blocks=blocks, stats=stats)

        except Exception as e:
            # Kernel bug backstop — memory must never kill a turn.
            self._record_failure(stats, "assemble", "kernel", e)
            stats.latency_ms = watch.elapsed_ms()
            return MemoryPayload(blocks=[], stats=stats)

    def _render_blocks(
        self, req: AssembleRequest, items: List[Scored]
    ) -> List[InjectionBlock]:
        """Turn the final selection into ready-to-inject blocks.

        Injection message mechanics live here (manager-internal, not a
        plugin stage), transplanted unchanged from the legacy worker
        execute path: memory → RecallStore.assemble_memory_block + the
        synthetic ``recall_memories`` pair; knowledge → KnowledgeStore.
        assemble_knowledge_block + the synthetic ``kb_search`` pair. The
        exact call shapes matter for byte-equivalence: ``model=`` only —
        the legacy calls never pass a budget (the memory list is already
        budget-fit by the store; the KB block is uncapped, B5/Phase 3).
        Lazy imports match the legacy call-site style.
        """
        blocks: List[InjectionBlock] = []
        by_kind: dict = {}
        for scored in items:
            by_kind.setdefault(scored.candidate.kind, []).append(scored)

        for kind, group in by_kind.items():
            records = [
                s.candidate.record for s in group if s.candidate.record is not None
            ]
            content = ""
            messages: List[Any] = []

            if kind == "memory" and records:
                from src.core.memory_injection import create_memory_injection_messages
                from src.services.recall_store import RecallStore

                content = RecallStore.assemble_memory_block(records, model=req.model)
                if content:
                    messages = list(create_memory_injection_messages(content))
            elif kind == "knowledge" and records:
                from src.core.knowledge_injection import (
                    create_knowledge_injection_messages,
                )
                from src.services.knowledge_store import KnowledgeStore

                content = KnowledgeStore.assemble_knowledge_block(
                    records, model=req.model
                )
                if content:
                    messages = list(create_knowledge_injection_messages(content))
            elif kind not in ("memory", "knowledge"):
                logger.warning(
                    "No renderer for injection kind '%s' — "
                    "block carries provenance only",
                    kind,
                )

            blocks.append(
                InjectionBlock(
                    kind=kind,
                    content=content,
                    messages=messages,
                    token_count=sum(s.candidate.token_count for s in group),
                    items=[
                        {
                            "retriever": s.candidate.retriever,
                            "bucket": s.candidate.bucket,
                            "score": s.score,
                            "token_count": s.candidate.token_count,
                            "record_id": str(getattr(s.candidate.record, "id", None)),
                        }
                        for s in group
                    ],
                )
            )
        return blocks

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    async def capture(self, event: CaptureEvent) -> None:
        """Dispatch a lifecycle event to subscribed writers.

        Awaitable but safe to fire-and-forget (asyncio.create_task) —
        never raises; per-writer failures are contained and logged with
        the exception type.
        """
        for name, writer in self._writers:
            try:
                kinds = writer.event_kinds
                if event.kind not in kinds:
                    continue
            except Exception as e:
                logger.warning(
                    "Memory writer '%s' has a broken event_kinds: %s: %s",
                    name,
                    type(e).__name__,
                    e,
                )
                continue
            try:
                await writer.on_event(event)
            except Exception as e:
                logger.warning(
                    "Memory writer '%s' failed on %s (non-fatal): %s: %s",
                    name,
                    event.kind,
                    type(e).__name__,
                    e,
                )

    def capture_nowait(self, event: CaptureEvent) -> "asyncio.Task":
        """Fire ``capture(event)`` as a ref-held detached task.

        The graphs use this for the ``pre_compaction`` snapshot so a compaction
        proceeds immediately while the chunked extraction runs behind it. The
        task is kept in ``self._bg_tasks`` (and removed on completion) so the
        event loop can't GC a long-running extraction mid-flight — the bare
        ``create_task(capture(...))`` at the legacy call sites holds no ref.
        """
        task = asyncio.create_task(self.capture(event))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def drain_background(self, timeout: Optional[float] = None) -> int:
        """Await in-flight ``capture_nowait`` tasks; returns how many were pending.

        Called at worker job-end (OQ-C) so a ``pre_compaction`` extraction
        scheduled just before the job freezes gets a chance to persist. Bounded
        by ``timeout`` — a hung aux endpoint must not wedge job completion; on
        timeout the still-running tasks stay detached (best-effort) and the job
        proceeds. ``capture()`` never raises, so gathering is always clean.
        """
        pending = [t for t in self._bg_tasks if not t.done()]
        if not pending:
            return 0
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "drain_background: %d memory task(s) still running after %ss; "
                "leaving detached",
                sum(1 for t in pending if not t.done()),
                timeout,
            )
        return len(pending)

    # ------------------------------------------------------------------
    # Model-facing extensions
    # ------------------------------------------------------------------

    def extension_tools(self) -> List[Any]:
        """Tools contributed by bound extensions (may be empty — P0)."""
        tools: List[Any] = []
        for name, extension in self._extensions:
            try:
                tools.extend(extension.tools() or [])
            except Exception as e:
                logger.warning(
                    "Memory extension '%s' failed to provide tools: %s: %s",
                    name,
                    type(e).__name__,
                    e,
                )
        return tools

    # ------------------------------------------------------------------

    @staticmethod
    def _record_failure(
        stats: AssembleStats, stage: str, name: str, e: Exception
    ) -> None:
        stats.errors.append(f"{stage}:{name}: {type(e).__name__}: {e}")
        logger.warning(
            "Memory %s '%s' failed (contained): %s: %s",
            stage,
            name,
            type(e).__name__,
            e,
        )
