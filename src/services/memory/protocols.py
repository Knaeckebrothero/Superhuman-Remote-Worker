"""Plugin protocols for the MemoryManager pipeline.

Design: docs/features/agent_memory_overhaul.md §2.1. Plugins are bound by
``MemoryManager.from_config()`` from the registry (registry.py); each
factory receives the shared ``MemoryRuntime`` and returns an object
satisfying one of these protocols.

The protocols are intentionally chain-shaped: scorers and policies take
the current item list and return a (possibly re-scored, filtered, or
extended) list, so composition order in ``memory.pipeline`` is the
execution order.
"""

from typing import Any, FrozenSet, List, Protocol, runtime_checkable

from .types import AssembleRequest, Candidate, CaptureEvent, Scored


@runtime_checkable
class Retriever(Protocol):
    """Produces candidates for one assemble pass.

    The request carries query text, task frame, buckets, and budget; v1
    retrievers wrap already scope-bound stores and may ignore buckets
    (see types.py module docstring for the sketch refinement rationale).
    """

    async def retrieve(self, req: AssembleRequest) -> List[Candidate]: ...


@runtime_checkable
class Scorer(Protocol):
    """Refines the combined score of each item (RRF, reranker, ensemble…)."""

    async def score(
        self, req: AssembleRequest, items: List[Scored]
    ) -> List[Scored]: ...


@runtime_checkable
class Policy(Protocol):
    """Transforms the final selection (budgets, pinning, gates, dedup).

    Policies may drop items (budget/gate), reorder, or prepend
    guaranteed items (TTL pinning / always-inject core).
    """

    async def apply(
        self, req: AssembleRequest, items: List[Scored]
    ) -> List[Scored]: ...


@runtime_checkable
class Writer(Protocol):
    """Reacts to capture events (extraction, curation, dedup, GC…).

    ``event_kinds`` declares the subscribed subset of CAPTURE_KINDS; the
    manager only dispatches matching events. Writers run on the aux LLM
    and must treat failures as their own — the manager contains and logs
    them, but a writer that can retry/queue should.
    """

    @property
    def event_kinds(self) -> FrozenSet[str]: ...

    async def on_event(self, event: CaptureEvent) -> None: ...


@runtime_checkable
class MemoryExtension(Protocol):
    """Model-facing driver (optional): contributes memory tools.

    Progressive enhancement for capable model families (recall, pin,
    page, correct) — capability-gated per model family; the passive
    pipeline never depends on these (P0).
    """

    def tools(self) -> List[Any]: ...
