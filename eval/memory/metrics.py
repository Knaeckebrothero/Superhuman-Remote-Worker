"""Retrieval metrics: Recall@k / NDCG@k / coverage vs evidence sessions.

The system retrieves *memories*; LongMemEval labels evidence at *session*
granularity (answer_session_ids). The bridge: every stored memory carries
the job_id of the haystack session it was extracted from (the harness
scopes one ingestion job per session), so a ranked memory list collapses
to a ranked session list (first occurrence wins) and the paper's
session-level definitions apply directly.

Abstention questions have no evidence sessions — retrieval metrics are
None for them and excluded from aggregates (they re-enter with the
end-task slice).
"""

import math
from collections import defaultdict
from typing import Dict, FrozenSet, List, Optional, Sequence

#: Default rank cutoffs (paper reports R@5 / NDCG@10 territory).
DEFAULT_KS = (1, 3, 5, 10)


def collapse_to_sessions(ranked_session_ids: Sequence[str]) -> List[str]:
    """Unique session ids in first-occurrence order (memory→session collapse)."""
    seen = set()
    out: List[str] = []
    for sid in ranked_session_ids:
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def recall_at_k(
    ranked_sessions: Sequence[str], evidence: FrozenSet[str], k: int
) -> Optional[float]:
    """1.0 if any evidence session appears in the top-k, else 0.0.

    This is the paper's any-evidence Recall@K. None when there is no
    evidence to find (abstention).
    """
    if not evidence:
        return None
    return 1.0 if any(sid in evidence for sid in ranked_sessions[:k]) else 0.0


def coverage_at_k(
    ranked_sessions: Sequence[str], evidence: FrozenSet[str], k: int
) -> Optional[float]:
    """Fraction of evidence sessions present in the top-k.

    Differs from recall only for multi-evidence questions (multi-session,
    knowledge-update): finding one of three evidence sessions is
    recall 1.0 but coverage 1/3.
    """
    if not evidence:
        return None
    return len(evidence & set(ranked_sessions[:k])) / len(evidence)


def ndcg_at_k(
    ranked_sessions: Sequence[str], evidence: FrozenSet[str], k: int
) -> Optional[float]:
    """Binary-relevance NDCG@k over the collapsed session ranking."""
    if not evidence:
        return None
    dcg = sum(
        1.0 / math.log2(rank + 2)
        for rank, sid in enumerate(ranked_sessions[:k])
        if sid in evidence
    )
    ideal_hits = min(len(evidence), k)
    idcg = sum(1.0 / math.log2(rank + 2) for rank in range(ideal_hits))
    return dcg / idcg if idcg > 0 else 0.0


def first_hit_rank(
    ranked_sessions: Sequence[str], evidence: FrozenSet[str]
) -> Optional[int]:
    """1-based rank of the first evidence session; None if absent/no evidence."""
    if not evidence:
        return None
    for rank, sid in enumerate(ranked_sessions, start=1):
        if sid in evidence:
            return rank
    return None


def question_metrics(
    ranked_sessions: Sequence[str],
    evidence: FrozenSet[str],
    ks: Sequence[int] = DEFAULT_KS,
) -> Dict[str, Optional[float]]:
    """All per-question retrieval metrics as one flat dict."""
    out: Dict[str, Optional[float]] = {}
    for k in ks:
        out[f"recall@{k}"] = recall_at_k(ranked_sessions, evidence, k)
        out[f"coverage@{k}"] = coverage_at_k(ranked_sessions, evidence, k)
        out[f"ndcg@{k}"] = ndcg_at_k(ranked_sessions, evidence, k)
    hit = first_hit_rank(ranked_sessions, evidence)
    out["first_hit_rank"] = float(hit) if hit is not None else None
    return out


def aggregate(rows: List[dict]) -> dict:
    """Aggregate per-question result rows → overall + per-type means.

    Rows are the JSONL dicts the runner emits: each has ``question_type``,
    a ``metrics`` dict (values possibly None), and optional numeric
    ``cost`` entries (tokens_injected, assemble_latency_ms, …) which are
    averaged over all rows including abstentions.
    """

    def _mean_metrics(group: List[dict]) -> dict:
        sums: Dict[str, float] = defaultdict(float)
        counts: Dict[str, int] = defaultdict(int)
        for row in group:
            for name, value in (row.get("metrics") or {}).items():
                if value is not None:
                    sums[name] += value
                    counts[name] += 1
        return {name: round(sums[name] / counts[name], 4) for name in sorted(sums)} | {
            "scored_questions": max(counts.values(), default=0)
        }

    def _mean_costs(group: List[dict]) -> dict:
        sums: Dict[str, float] = defaultdict(float)
        counts: Dict[str, int] = defaultdict(int)
        for row in group:
            for name, value in (row.get("cost") or {}).items():
                if isinstance(value, (int, float)):
                    sums[name] += value
                    counts[name] += 1
        return {name: round(sums[name] / counts[name], 2) for name in sorted(sums)}

    by_type: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        by_type[row.get("question_type", "unknown")].append(row)

    return {
        "questions": len(rows),
        "abstentions": sum(1 for r in rows if r.get("is_abstention")),
        "overall": _mean_metrics(rows),
        "cost": _mean_costs(rows),
        "by_type": {
            qtype: _mean_metrics(group) | {"questions": len(group)}
            for qtype, group in sorted(by_type.items())
        },
    }
