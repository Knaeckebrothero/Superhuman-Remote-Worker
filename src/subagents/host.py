"""``ParentHost`` — what a child needs from whoever spawned it (U3 B.13).

The subagent runtime never assumes a worker: everything parent-specific is
behind this protocol so a persistent session (U5) is a drop-in. The worker
implementation (``WorkerHost``) is built in ``src/agent.py`` (WP2); tests and
simple parents use :class:`SimpleParentHost`.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


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


def _default_drain_check() -> bool:
    """The worker's drain intent (dual-mode heartbeat), read lazily so this
    module never imports the API layer; ``False`` when the seam is absent."""
    try:
        from src.api.dual_app import is_drain_requested

        return bool(is_drain_requested())
    except Exception:
        return False


@dataclass
class WorkerHost:
    """The :class:`ParentHost` of a worker JOB (U3 WP2, plan B.13).

    Built by ``UniversalAgent._setup_job_tools`` from the job's identity and
    live objects; the per-batch facts (the durable message list, the audit
    metadata, the context probe, the admission fence) are read off the
    parent's ``ToolContext``, where the graph stamps them — so the host is
    always current without the graph knowing about hosts.
    """

    job_id: str
    agent_type: str
    tool_context: Any
    thread_id: Optional[str] = None
    user_id: Optional[str] = None
    auxiliary_llm: Optional[Any] = None
    live_llm_config: Optional[Any] = None
    postgres: Optional[Any] = None
    #: Overrides the context's ``provider_admission`` callable when set
    #: (tests); ``None`` = the context's fence, else the dual-app drain seam.
    admission_fn: Optional[Callable[[], bool]] = None
    events: List[str] = field(default_factory=list)

    @property
    def audit_metadata(self) -> Dict[str, Any]:
        """The parent graph's ``state["metadata"]`` as stamped for the current
        batch; the job metadata the tools carry until the first batch."""
        stamped = getattr(self.tool_context, "_parent_audit_metadata", None)
        if isinstance(stamped, dict):
            return stamped
        fallback = getattr(self.tool_context, "_job_metadata", None)
        return dict(fallback) if isinstance(fallback, dict) else {}

    def provider_admission(self) -> bool:
        fence = self.admission_fn
        if fence is None:
            fence = getattr(self.tool_context, "provider_admission", None)
        if not callable(fence):
            return not _default_drain_check()
        try:
            return bool(fence())
        except Exception:
            logger.warning(
                "subagent host: admission fence raised — closing", exc_info=True
            )
            return False

    def context_probe(self) -> Optional[ContextProbe]:
        probe = getattr(self.tool_context, "parent_context_probe", None)
        if not callable(probe):
            return None
        try:
            return probe()
        except Exception:
            logger.debug("subagent host: context probe failed", exc_info=True)
            return None

    def fork_source(self) -> List[Any]:
        source = getattr(self.tool_context, "_fork_source", None)
        return list(source or [])

    def enqueue_event(self, text: str) -> None:
        # U4 wires Lane B delivery; a foreground parent only records it.
        self.events.append(text)

    @classmethod
    def from_context(cls, tool_context: Any, **overrides: Any) -> "WorkerHost":
        """A host from what a parent ``ToolContext`` already carries (the
        fallback the tool uses when agent.py has not installed one)."""
        config = getattr(tool_context, "config", None) or {}
        job_meta = getattr(tool_context, "_job_metadata", None) or {}
        values: Dict[str, Any] = {
            "job_id": str(
                overrides.pop("job_id", None)
                or getattr(tool_context, "job_id", None)
                or job_meta.get("job_id")
                or ""
            ),
            "agent_type": str(
                overrides.pop("agent_type", None) or config.get("agent_id") or "worker"
            ),
            "tool_context": tool_context,
            "thread_id": getattr(tool_context, "thread_id", None),
            "user_id": getattr(tool_context, "user_id", None),
            "auxiliary_llm": getattr(tool_context, "auxiliary_llm", None),
            "live_llm_config": getattr(tool_context, "_llm_config", None),
            "postgres": getattr(tool_context, "postgres_db", None),
        }
        values.update(overrides)
        return cls(**values)


__all__ = ["ContextProbe", "ParentHost", "SimpleParentHost", "WorkerHost"]
