"""``ParentHost`` — what a child needs from whoever spawned it (U3 B.13).

The subagent runtime never assumes a worker: everything parent-specific is
behind this protocol so a persistent session (U5) is a drop-in. The worker
implementation (``WorkerHost``) is built in ``src/agent.py`` (WP2); tests and
simple parents use :class:`SimpleParentHost`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Protocol,
    runtime_checkable,
)


class ContextProbe(NamedTuple):
    """A snapshot of the PARENT's context accounting, read at envelope time.

    ``last_provider_input_tokens`` / ``current_token_count`` mirror
    ``ContextManager.state``; the trigger the compaction logic uses is
    ``max(local, anchor)`` and the envelope's headroom is measured against
    ``compaction_threshold_tokens`` the same way (B.5).
    """

    last_provider_input_tokens: Optional[int]
    current_token_count: int
    compaction_threshold_tokens: int
    model_max_context_tokens: int


@runtime_checkable
class ParentHost(Protocol):
    """The parent seen from a child (worker job today, session in U5)."""

    #: Parent job id — the audit / metering identity every child row hangs off.
    job_id: str
    #: Parent session thread (U5); ``None`` for a worker job.
    thread_id: Optional[str]
    #: Owning user (forwarded on agent→orchestrator calls); may be ``None``.
    user_id: Optional[str]
    #: Parent ``agent_id`` — the ``agent_type`` column of the child's audit rows.
    agent_type: str
    #: The parent's ``AuxiliaryLLM`` (compaction summariser); ``None`` = the
    #: summarizer fast-fails and the child keeps its raw history.
    auxiliary_llm: Optional[Any]
    #: The parent's LIVE ``LLMConfig`` (dispatch-time credentials, a fallback
    #: model swap) — what an ``inherit`` roster entry actually runs on.
    live_llm_config: Optional[Any]
    #: Agent-side ``PostgresDB`` pool for the ledger (WP3); ``None`` in tests.
    postgres: Optional[Any]

    def provider_admission(self) -> bool:
        """Synchronous fence: ``False`` once the parent is freezing/draining —
        the child then ends at its next provider boundary without spend."""
        ...

    def context_probe(self) -> Optional[ContextProbe]:
        """The parent's current context accounting (``None`` = unknown)."""
        ...

    def fork_source(self) -> List[Any]:
        """The parent's DURABLE message list to seed a ``fork=true`` child from
        (the compacted history, never a prepared/transient copy)."""
        ...

    def enqueue_event(self, text: str) -> None:
        """Deliver a ``role=event`` notice to the parent (U4 background
        delivery); a foreground parent may ignore it."""
        ...


@dataclass
class SimpleParentHost:
    """A plain :class:`ParentHost` built from values and callables.

    The worker's ``WorkerHost`` (WP2) wires the graph's fences and probes into
    the same shape; tests hand this one static values.
    """

    job_id: str
    agent_type: str = "worker"
    thread_id: Optional[str] = None
    user_id: Optional[str] = None
    auxiliary_llm: Optional[Any] = None
    live_llm_config: Optional[Any] = None
    postgres: Optional[Any] = None
    #: The parent's audit ``metadata`` (``state["metadata"]`` in the graph);
    #: merged under the child's ``subagent_*`` keys on every tool audit row.
    audit_metadata: Dict[str, Any] = field(default_factory=dict)
    admission_fn: Optional[Callable[[], bool]] = None
    probe_fn: Optional[Callable[[], Optional[ContextProbe]]] = None
    fork_source_fn: Optional[Callable[[], List[Any]]] = None
    events: List[str] = field(default_factory=list)

    def provider_admission(self) -> bool:
        if self.admission_fn is None:
            return True
        try:
            return bool(self.admission_fn())
        except Exception:
            return False

    def context_probe(self) -> Optional[ContextProbe]:
        if self.probe_fn is None:
            return None
        try:
            return self.probe_fn()
        except Exception:
            return None

    def fork_source(self) -> List[Any]:
        if self.fork_source_fn is None:
            return []
        return list(self.fork_source_fn() or [])

    def enqueue_event(self, text: str) -> None:
        self.events.append(text)


__all__ = ["ContextProbe", "ParentHost", "SimpleParentHost"]
