"""Ingestion verdicts — the write-path adjudicator (overhaul Phase 4).

When ``memory.ingestion.enabled`` is set, ``RecallStore.store()`` stops
folding near-duplicates with a blind cosine-0.85 merge and instead asks the
auxiliary LLM what to do with each candidate relative to its nearest
currently-valid neighbours: ADD / UPDATE / MERGE / NOOP. UPDATE and MERGE
retire the superseded rows (bi-temporal ``valid_to``) so default retrieval
stops serving the stale fact — the washing-machine fix (design P3, §4).

This module is the seam-side glue. The store stays storage-only and calls an
injected, duck-typed ``ingestion_verdict`` object; ``maybe_attach_*`` builds
that object where the store, the aux LLM, and the config already meet (the
three manager-construction sites). The verdict schema and the chain-mode task
live in ``src.services.auxiliary`` alongside the other aux tasks.
"""

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class IngestionVerdictService:
    """Adjudicates a candidate memory against its nearest neighbours.

    Holds the cost-guard knobs (``top_k`` neighbours to consider, the
    ``review_floor`` similarity below which the store skips the LLM entirely)
    so the store reads them from one place. ``adjudicate`` never raises — an
    aux outage or a malformed verdict degrades to a conservative ADD (keep the
    candidate, retire nothing), so a write is never lost to a verdict failure.
    """

    def __init__(self, auxiliary_llm: Any, prompt: str, config: Any):
        self._aux = auxiliary_llm
        self._prompt = prompt
        self.top_k = int(getattr(config, "verdict_top_k", 5))
        self.review_floor = float(getattr(config, "review_floor", 0.6))

    async def adjudicate(
        self,
        *,
        content: str,
        summary: Optional[str],
        keywords: Optional[List[str]],
        similar: List[Any],
    ) -> "Any":
        """Return an IngestionVerdict for ``content`` vs the ``similar`` records.

        ``similar`` is the ordered neighbour list (closest first); each record
        may carry a transient ``.similarity`` set by ``find_similar_many``.
        """
        from src.services.auxiliary import IngestionVerdict, IngestionVerdictTask

        neighbours = []
        for rec in similar:
            created = getattr(rec, "created_at", None)
            neighbours.append(
                {
                    "content": getattr(rec, "content", "") or "",
                    "similarity": getattr(rec, "similarity", None),
                    "age": created.date().isoformat() if created else None,
                }
            )

        task = IngestionVerdictTask(
            candidate_content=content,
            neighbours=neighbours,
            prompt=self._prompt,
        )
        try:
            verdict = await self._aux.chain(task)
        except Exception as e:  # never let a verdict failure lose a write
            logger.warning(
                "Ingestion verdict failed (non-fatal, defaulting to ADD): %s: %s",
                type(e).__name__,
                e,
            )
            return IngestionVerdict(action="ADD", reason="verdict-error-fallback")

        if not isinstance(verdict, IngestionVerdict):
            return IngestionVerdict(action="ADD", reason="verdict-shape-fallback")
        return verdict


def maybe_attach_ingestion_verdict(
    recall_store: Any,
    auxiliary_llm: Any,
    memory_config: Any,
) -> Optional[IngestionVerdictService]:
    """Wire an IngestionVerdictService onto ``recall_store`` when enabled.

    Called at the manager-construction sites (worker graph builder, persistent
    ``_setup_memory``, eval harness ``build_manager``). No-ops — returning
    None and leaving the store on its legacy cosine-dedup path — when the
    feature is off, the store is absent, or there is no aux LLM to run the
    verdict (the verdict must fail loud-OFF, never silently retire).
    """
    ingestion = getattr(memory_config, "ingestion", None)
    if ingestion is None or not getattr(ingestion, "enabled", False):
        return None
    if recall_store is None or auxiliary_llm is None:
        if recall_store is not None and auxiliary_llm is None:
            logger.warning(
                "memory.ingestion.enabled but no auxiliary LLM — ingestion "
                "verdicts disabled; store stays on legacy cosine dedup."
            )
        return None

    from src.utils.config import load_prompt

    prompt = load_prompt("memory_ingestion_verdict.txt")
    service = IngestionVerdictService(auxiliary_llm, prompt, ingestion)
    recall_store.ingestion_verdict = service
    logger.info(
        "Ingestion verdicts enabled (top_k=%d, review_floor=%.2f)",
        service.top_k,
        service.review_floor,
    )
    return service
