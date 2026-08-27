"""Knowledge ingestion verdicts — the KB write-path adjudicator (slice 2 PR2).

The mirror of ``src/services/memory/ingestion.py`` for knowledge-base writes.
When the curator's ``verdict`` knob is on, each curation candidate is compared
against its nearest currently-active KB neighbours (fetched via
``KnowledgeStore.find_similar_many``) and the auxiliary LLM decides
ADD / UPDATE / SUPERSEDE / DISCARD — the gate that stops the F33/F38 curator
noise before any ``kb_write``/``kb_update`` lands.

Two differences from the memory service, both from the 2026-07-03 code audit
(design §3): the verdict prompt is passed at **event time** (not loaded once at
attach time, so ``config.update`` is honoured in persistent sessions), and a
**content-hash pre-filter** (cognee's pattern) short-circuits exact duplicates
before the LLM. The memory cost guard is preserved: no neighbour above the
similarity floor → straight ADD, zero LLM calls. ``adjudicate`` never raises —
an aux outage or malformed verdict degrades to a conservative ADD, so a write is
never lost to a verdict failure.

The verdict schema and the chain-mode task live in ``src.services.auxiliary``
alongside the other aux tasks (``KnowledgeVerdict`` / ``KnowledgeVerdictTask``).
"""

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class KnowledgeVerdictService:
    """Adjudicates a candidate KB note against its nearest active neighbours.

    Holds the cost-guard knobs (``top_k`` neighbours to consider, the
    ``review_floor`` similarity below which the store returns no neighbour and
    the service straight-adds). ``adjudicate`` is fail-safe: it degrades to a
    conservative ADD on aux error or a malformed verdict.
    """

    def __init__(self, auxiliary_llm: Any, config: Any):
        self._aux = auxiliary_llm
        self.top_k = int(getattr(config, "verdict_top_k", 5))
        self.review_floor = float(getattr(config, "review_floor", 0.6))

    @staticmethod
    def _content_hash(content: str) -> str:
        """SHA-256 of content — matches ``KnowledgeStore._content_hash``."""
        return hashlib.sha256((content or "").encode()).hexdigest()

    async def adjudicate(
        self,
        *,
        content: str,
        neighbours: List[Any],
        prompt: str,
    ) -> "Any":
        """Return a KnowledgeVerdict for ``content`` vs the ``neighbours``.

        ``neighbours`` is the ordered active-neighbour list (closest first) from
        ``KnowledgeStore.find_similar_many`` — each record may carry a transient
        ``.similarity`` and a ``.content_hash``. ``prompt`` is resolved by the
        caller at event time.
        """
        from src.services.auxiliary import KnowledgeVerdict, KnowledgeVerdictTask

        # Cost guard: no near-duplicate above the floor → straight ADD, zero LLM.
        if not neighbours:
            return KnowledgeVerdict(action="ADD", reason="no-similar-neighbours")

        # Content-hash pre-filter (cognee): an exact-duplicate candidate never
        # reaches the LLM — DISCARD against the identical neighbour.
        candidate_hash = self._content_hash(content)
        for i, rec in enumerate(neighbours, start=1):
            if getattr(rec, "content_hash", None) == candidate_hash:
                return KnowledgeVerdict(
                    action="DISCARD",
                    target_indices=[i],
                    reason="exact-duplicate (content hash match)",
                )

        formatted = []
        for rec in neighbours:
            created = getattr(rec, "created_at", None)
            formatted.append(
                {
                    "content": getattr(rec, "content", "") or "",
                    "title": getattr(rec, "title", None),
                    "similarity": getattr(rec, "similarity", None),
                    "age": created.date().isoformat() if created else None,
                }
            )

        task = KnowledgeVerdictTask(
            candidate_content=content,
            neighbours=formatted,
            prompt=prompt,
        )
        try:
            verdict = await self._aux.chain(task)
        except Exception as e:  # never let a verdict failure lose a write
            logger.warning(
                "Knowledge verdict failed (non-fatal, defaulting to ADD): %s: %s",
                type(e).__name__,
                e,
            )
            return KnowledgeVerdict(action="ADD", reason="verdict-error-fallback")

        if not isinstance(verdict, KnowledgeVerdict):
            return KnowledgeVerdict(action="ADD", reason="verdict-shape-fallback")
        return verdict


@dataclass
class GateDecision:
    """The result of gating one candidate write.

    ``verdict`` is the raw :class:`KnowledgeVerdict`; ``targets`` are the
    neighbour records its ``target_indices`` resolve to (out-of-range indices
    dropped), so the caller acts on note slugs rather than re-deriving them.
    """

    verdict: Any
    targets: List[Any] = field(default_factory=list)


async def gate_candidate(
    service: KnowledgeVerdictService,
    knowledge_store: Any,
    project_id: Any,
    *,
    content: str,
    prompt: str,
) -> GateDecision:
    """Run the full gate for one candidate note: neighbours → verdict → targets.

    Embeds the candidate, fetches its active neighbours via
    ``KnowledgeStore.find_similar_many`` (bounded by the service's ``top_k`` and
    ``review_floor`` — an empty result is the cost guard that yields a straight
    ADD), adjudicates, and resolves ``target_indices`` back to neighbour records.
    Pure orchestration — the caller (``kb_write``) applies the decision.
    """
    embedding = await knowledge_store.embedding_service.embed(content)
    neighbours = await knowledge_store.find_similar_many(
        project_id,
        embedding,
        k=service.top_k,
        min_similarity=service.review_floor,
    )
    verdict = await service.adjudicate(
        content=content, neighbours=neighbours, prompt=prompt
    )
    targets = [
        neighbours[i - 1] for i in verdict.target_indices if 1 <= i <= len(neighbours)
    ]
    return GateDecision(verdict=verdict, targets=targets)


def build_knowledge_verdict_service(
    auxiliary_llm: Any,
    curator_config: Any,
) -> Optional[KnowledgeVerdictService]:
    """Build a KnowledgeVerdictService when the curator's verdict gate is on.

    The KB analog of ``maybe_attach_ingestion_verdict``. Returns None — leaving
    the curator on its legacy ungated write path — when the gate is off, there
    is no config, or there is no aux LLM to run the verdict (the gate must fail
    loud-OFF, never silently retire). Unlike the memory helper it does not hang
    the service on a store attribute: the consumer is the curator orchestration,
    which calls ``adjudicate`` per candidate.
    """
    if curator_config is None or not getattr(curator_config, "verdict", False):
        return None
    if auxiliary_llm is None:
        logger.warning(
            "curate_knowledge.verdict enabled but no auxiliary LLM — knowledge "
            "verdicts disabled; curator stays on its legacy ungated write path."
        )
        return None
    service = KnowledgeVerdictService(auxiliary_llm, curator_config)
    logger.info(
        "Knowledge ingestion verdicts enabled (top_k=%d, review_floor=%.2f)",
        service.top_k,
        service.review_floor,
    )
    return service
