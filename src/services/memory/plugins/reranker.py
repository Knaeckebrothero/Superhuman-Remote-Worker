"""Reranker scorer (memory overhaul Phase 3, slice 2).

Reorders memory candidates by query relevance via an external rerank
endpoint (Cohere-shaped ``POST {base_url}/rerank`` — served for
qwen3-reranker-8b by the same router as the auxiliary model, so the
plugin defaults to the auxiliary transport and needs no extra
credential plumbing).

Motivated by the Phase-2 baseline finding: hybrid retrieval surfaces
the evidence memory at ~rank 13 of ~122 injected items on the seam arm
(eval/memory runs `seam_s20` vs `flat_s100`) — injection order is what
the model reads, so ordering IS retrieval quality.

Scope discipline:
- Only ``kind == "memory"`` items are reranked; knowledge items render
  to their own block and pass through in original order.
- TTL-pinned items (``record.remaining_turns > 0``) stay ahead of the
  reranked tail by default (``memory.reranker.keep_pinned_first``) —
  the pinned tier is the recency working set; replacing it is the
  bounded-core policy's job, not the scorer's.
- At most ``memory.reranker.top_k`` candidates go to the endpoint; the
  remainder keeps its original (hybrid) order behind the reranked head.
- Any transport/shape failure raises — the manager's per-plugin
  containment passes items through unchanged and records the error in
  ``AssembleStats.errors``. The scorer never partially reorders.
"""

import logging
from typing import Any, List, Optional, Tuple

from src.services.memory.registry import register_memory_plugin
from src.services.memory.types import AssembleRequest, Scored

logger = logging.getLogger(__name__)


def _is_pinned(item: Scored) -> bool:
    record = item.candidate.record
    return bool(getattr(record, "remaining_turns", 0) or 0)


class RerankerScorer:
    """Scorer protocol implementation over a Cohere-shaped /rerank route."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: Optional[str],
        top_k: int = 64,
        timeout: float = 10.0,
        keep_pinned_first: bool = True,
        client: Optional[Any] = None,
    ) -> None:
        if not base_url:
            raise ValueError("reranker needs a base_url (or an auxiliary base_url)")
        self.model = model
        self.endpoint = base_url.rstrip("/") + "/rerank"
        self.api_key = api_key
        self.top_k = top_k
        self.timeout = timeout
        self.keep_pinned_first = keep_pinned_first
        self._client = client  # injectable for tests; lazily built otherwise

    def _http_client(self) -> Any:
        if self._client is None:
            import httpx

            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(headers=headers, timeout=self.timeout)
        return self._client

    async def _rerank(
        self, query: str, documents: List[str]
    ) -> List[Tuple[int, float]]:
        """Call the endpoint; return (index, relevance_score) pairs."""
        response = await self._http_client().post(
            self.endpoint,
            json={"model": self.model, "query": query, "documents": documents},
        )
        response.raise_for_status()
        payload = response.json()
        return [
            (int(r["index"]), float(r["relevance_score"])) for r in payload["results"]
        ]

    async def score(self, req: AssembleRequest, items: List[Scored]) -> List[Scored]:
        if not req.query_text:
            return items

        head: List[Scored] = []  # pinned memory items, original order
        eligible: List[Scored] = []  # memory items to rerank
        others: List[Scored] = []  # knowledge/other kinds, original order
        for item in items:
            if item.candidate.kind != "memory":
                others.append(item)
            elif self.keep_pinned_first and _is_pinned(item):
                head.append(item)
            else:
                eligible.append(item)

        to_rank = eligible[: self.top_k]
        tail = eligible[self.top_k :]
        if len(to_rank) < 2:
            return items

        ranked = await self._rerank(
            req.query_text, [item.candidate.text or "" for item in to_rank]
        )
        if len(ranked) != len(to_rank):
            raise ValueError(
                f"rerank returned {len(ranked)} results for {len(to_rank)} documents"
            )

        # Apply only after a fully valid response — no partial reorder.
        reordered = []
        for index, relevance in sorted(ranked, key=lambda r: r[1], reverse=True):
            item = to_rank[index]
            item.score = relevance
            item.candidate.channel_scores["rerank"] = relevance
            reordered.append(item)

        return [*head, *reordered, *tail, *others]


@register_memory_plugin(
    "scorer",
    "reranker",
    description="Cross-encoder rerank of memory candidates against the "
    "query (Cohere-shaped /rerank route; defaults to the auxiliary "
    "endpoint's transport)",
)
def _build_reranker(runtime: Any) -> RerankerScorer:
    cfg = getattr(runtime.memory_config, "reranker", None)
    aux = runtime.auxiliary_config
    if cfg is None:
        raise ValueError("memory.reranker config section missing")
    base_url = cfg.base_url or getattr(aux, "base_url", None)
    api_key = cfg.api_key or getattr(aux, "api_key", None)
    return RerankerScorer(
        model=cfg.model,
        base_url=base_url,
        api_key=api_key,
        top_k=cfg.top_k,
        timeout=cfg.timeout,
        keep_pinned_first=cfg.keep_pinned_first,
    )
