"""Data vocabulary for the MemoryManager seam.

Design: docs/features/agent_memory_overhaul.md §2.1 (this module is the
"Phase 1 refines" of that sketch). The types here are deliberately
behaviour-free — they carry data between the two graphs and the plugin
pipeline; all logic lives in the manager and the registered plugins.

Refinements vs the §2.1 sketch, locked for Phase 1:
- ``Retriever.retrieve(req)`` takes the whole request instead of
  ``(query, bucket, k)`` — v1 buckets are layered over the already
  scope-bound stores (§2.2), so per-bucket fan-out would be ceremony
  until Phase 6 materializes real buckets.
- ``CaptureEvent`` carries no scope refs — the manager (and the stores it
  binds) is constructed per job/session, already scoped.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from langchain_core.messages import BaseMessage

# ---------------------------------------------------------------------------
# Capture (write side)
# ---------------------------------------------------------------------------

CaptureKind = Literal[
    "turn_end",
    "phase_boundary",
    "compaction",
    "session_end",
    "idle_archive",
    "todo_complete",
]

#: All valid CaptureEvent kinds — writers subscribe to a subset of these.
CAPTURE_KINDS: frozenset = frozenset(
    (
        "turn_end",
        "phase_boundary",
        "compaction",
        "session_end",
        "idle_archive",
        "todo_complete",
    )
)


@dataclass
class CaptureEvent:
    """A lifecycle event the write side reacts to.

    One event vocabulary replaces the scattered extraction/curation call
    sites (worker interval + phase boundary, persistent loop interval,
    session-end/idle-archive teardown, todo_complete queuing).
    """

    kind: CaptureKind  # validated against CAPTURE_KINDS in __post_init__
    messages: List[BaseMessage] = field(default_factory=list)
    phase: int = 0
    turn_start: Optional[int] = None
    turn_end: Optional[int] = None
    #: Call-site extras that don't warrant a field yet (e.g. todo content).
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in CAPTURE_KINDS:
            raise ValueError(
                f"Unknown CaptureEvent kind '{self.kind}'. "
                f"Valid kinds: {sorted(CAPTURE_KINDS)}"
            )


# ---------------------------------------------------------------------------
# Assemble (read side)
# ---------------------------------------------------------------------------


@dataclass
class TaskFrame:
    """Worker-side task context for query formation (None for persistent)."""

    top_todo: Optional[str] = None
    phase_number: int = 0
    is_strategic: bool = False


@dataclass
class BucketRef:
    """A named, scoped memory collection (§2.2).

    v1 layers buckets over the existing job_id/project_id columns — the
    stores are scope-bound at construction, so these are descriptive until
    the Phase-6 bucket_id migration. ``scope`` holds the layering keys
    (e.g. {"job_id": ..., "project_id": ...}).
    """

    name: str  # session | project | personal | shared/custom
    scope: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class Query:
    """Retrieval query, shared across retrievers in one assemble pass.

    ``embedding`` is a lazily-populated cache so multiple dense-channel
    consumers embed the query text exactly once.
    """

    text: str
    embedding: Optional[List[float]] = None


@dataclass
class AssembleRequest:
    """Input to MemoryManager.assemble() — built by the graphs per LLM call."""

    query_text: str
    task_frame: Optional[TaskFrame] = None
    budget_tokens: int = 10000
    buckets: List[BucketRef] = field(default_factory=list)
    #: Main-LLM model name; the legacy block assemblers take it for token
    #: counting (RecallStore.assemble_memory_block(model=...)).
    model: Optional[str] = None


@dataclass
class Candidate:
    """One retrieved item, before scoring.

    ``record`` keeps the original store dataclass (MemoryRecord/KnowledgeRecord)
    so the legacy block assemblers can render it unchanged in the
    transplant; ``kind`` routes it to the right injection block.
    """

    kind: str  # "memory" | "knowledge"
    text: str
    token_count: int = 0
    record: Any = None
    bucket: Optional[str] = None
    retriever: Optional[str] = None
    #: Per-channel raw scores (e.g. {"rrf": 0.031}) — scorer/stats input.
    channel_scores: Dict[str, float] = field(default_factory=dict)


@dataclass
class Scored:
    """A candidate with its (current) combined score — scorers refine this."""

    candidate: Candidate
    score: float = 0.0


@dataclass
class InjectionBlock:
    """One ready-to-inject unit of the assembled payload.

    ``messages`` holds the synthetic tool-call pair exactly as today
    (memory_inject_/knowledge_inject_ prefixes, excluded from
    summarization) — the mechanics move inside the manager unchanged.
    """

    kind: str  # "memory" | "knowledge"
    content: str = ""  # rendered block text (assemble_*_block output)
    messages: List[BaseMessage] = field(default_factory=list)
    token_count: int = 0
    #: Provenance of what got injected (ids/sources/scores) — feeds stats,
    #: the cockpit memory panel, and eventually the learned-scorer flywheel.
    items: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AssembleStats:
    """What one assemble() pass considered, scored, and injected.

    First-class by design: simultaneously the eval-harness tap, the
    cockpit "why did it say that" surface, and (later) training data.
    """

    candidates_total: int = 0
    per_retriever: Dict[str, int] = field(default_factory=dict)
    injected_total: int = 0
    tokens_injected: int = 0
    blocks: int = 0
    latency_ms: float = 0.0
    #: Contained plugin failures, as "stage:name: ExcType: message" strings —
    #: assemble never raises, but it never fails silently either.
    errors: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Telemetry/audit view (archiver audit_step data, harness rows)."""
        return {
            "candidates_total": self.candidates_total,
            "per_retriever": dict(self.per_retriever),
            "injected_total": self.injected_total,
            "tokens_injected": self.tokens_injected,
            "blocks": self.blocks,
            "latency_ms": round(self.latency_ms, 2),
            "errors": list(self.errors),
            **({"extra": dict(self.extra)} if self.extra else {}),
        }


@dataclass
class MemoryPayload:
    """Output of MemoryManager.assemble()."""

    blocks: List[InjectionBlock] = field(default_factory=list)
    stats: AssembleStats = field(default_factory=AssembleStats)

    def messages(self) -> List[BaseMessage]:
        """All injection messages in block order — what the graphs splice in."""
        out: List[BaseMessage] = []
        for block in self.blocks:
            out.extend(block.messages)
        return out


@dataclass
class MemoryRuntime:
    """Bind-time dependency bundle handed to every plugin factory.

    The two graphs hold different handles (worker: tool_context-backed
    knowledge store + todo manager; persistent: session-scoped stores),
    so everything is optional — a factory that requires a missing handle
    must raise at bind time, not limp at call time.

    Stores arrive already scope-bound (job_id/project_id at construction),
    which is why neither AssembleRequest nor CaptureEvent re-carry scope.
    """

    recall_store: Any = None
    knowledge_store: Any = None
    auxiliary_llm: Any = None
    memory_config: Any = None  # src.core.loader.MemoryConfig
    extraction_prompt: Optional[str] = None
    job_id: Optional[str] = None
    project_id: Optional[str] = None
    project_ids: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


class _StopWatch:
    """Tiny monotonic timer for AssembleStats.latency_ms."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000.0
