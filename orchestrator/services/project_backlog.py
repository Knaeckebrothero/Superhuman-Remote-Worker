"""Project backlog — the loop's real work pool.

Before this existed, every loop kickoff told the agent to "check the KB for …
the current open backlog" and there was no backlog: each agent re-derived one
by similarity search, every job. This module makes the pool a deterministic,
indexed listing that the orchestrator hands over verbatim.

Two buckets (docs/superpowers/specs/2026-07-26-project-backlog-pipeline-design.md):

* **pool** — notes of type feature/issue/idea with ``status='active'``,
  ordered by priority then age.
* **in progress** — the loop's existing ``campaign``, whose
  ``initiative_note_id`` is the ticket. A ticket being worked on keeps
  ``status='active'``; "in progress" is derived, never written to the note.
  The pool query therefore EXCLUDES the campaign's initiative.

Priority is a LABEL. Nothing here (or anywhere) may gate, refuse or reorder
work because of it — it only sorts what the agent is shown.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Canonical copy: src/services/knowledge_graph.py (not importable here — the
# orchestrator image has no agent deps; see kb_reindex.py for the same pattern).
PRIORITY_WORDS: dict[int, str] = {0: "high", 1: "normal", 2: "low"}
DEFAULT_PRIORITY_RANK = 1

BACKLOG_NOTE_TYPES: tuple[str, ...] = ("feature", "issue", "idea")

# How many tickets ride in a kickoff. The counts line (see render_backlog_block)
# is what keeps this cap from hiding the tail.
BACKLOG_INJECTION_LIMIT = 20


async def fetch_backlog(
    vector_db: Any,
    project_id: str,
    *,
    exclude_note_id: str | None = None,
    limit: int = BACKLOG_INJECTION_LIMIT,
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Return ``(rows, counts_by_rank)`` for a project's open ticket pool.

    ``exclude_note_id`` drops the in-progress campaign's initiative so the
    overseer is never offered something already underway. Both queries hit
    ``idx_knowledge_backlog``.
    """
    async with vector_db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT note_id, note_type, title, priority
              FROM knowledge_index
             WHERE project_id = $1::uuid
               AND status = 'active'
               AND note_type = ANY($2::text[])
               AND ($3::text IS NULL OR note_id <> $3)
             ORDER BY priority ASC, created_at ASC
             LIMIT $4
            """,
            project_id,
            list(BACKLOG_NOTE_TYPES),
            exclude_note_id,
            limit,
        )
        count_rows = await conn.fetch(
            """
            SELECT priority, COUNT(*) AS n
              FROM knowledge_index
             WHERE project_id = $1::uuid
               AND status = 'active'
               AND note_type = ANY($2::text[])
               AND ($3::text IS NULL OR note_id <> $3)
             GROUP BY priority
            """,
            project_id,
            list(BACKLOG_NOTE_TYPES),
            exclude_note_id,
        )
    return (
        [dict(r) for r in rows],
        {int(r["priority"]): int(r["n"]) for r in count_rows},
    )


def _priority_word(rank: Any) -> str:
    try:
        return PRIORITY_WORDS.get(int(rank), "normal")
    except (TypeError, ValueError):
        return "normal"


def render_backlog_block(
    rows: list[dict[str, Any]],
    counts: dict[int, int],
    *,
    in_progress: dict[str, Any] | None = None,
    limit: int = BACKLOG_INJECTION_LIMIT,
) -> str:
    """Render the kickoff's backlog block. Pure — no I/O, no DB.

    Shape (order is load-bearing: the per-priority totals lead, because with a
    hard cap a large pool otherwise hides its own tail and the idea bucket
    silently becomes write-only)::

        PROJECT BACKLOG — 34 open: 12 high, 15 normal, 7 low (showing top 20)
        IN PROGRESS: [high] issue-deploy-docs-missing — Deployment docs missing
          [high]   feature  feature-rag-boundary — Permission-aware RAG boundary
          … 18 more
    """
    total = sum(counts.values())
    breakdown = ", ".join(
        f"{counts[rank]} {word}"
        for rank, word in sorted(PRIORITY_WORDS.items())
        if counts.get(rank)
    )
    header = f"PROJECT BACKLOG — {total} open"
    if breakdown:
        header += f": {breakdown}"
    if total > len(rows):
        header += f" (showing top {limit})"

    lines = [header]

    if in_progress:
        lines.append(
            f"IN PROGRESS: [{_priority_word(in_progress.get('priority'))}] "
            f"{in_progress.get('note_id')} — {in_progress.get('title') or ''}".rstrip()
            .rstrip("—")
            .rstrip()
        )
    else:
        lines.append("IN PROGRESS: (none)")

    if not rows:
        lines.append(
            "  (the pool is empty — file feature/issue/idea notes with kb_write "
            "so the next iterations have a real queue to work from)"
        )
        return "\n".join(lines)

    for row in rows:
        word = _priority_word(row.get("priority"))
        lines.append(
            f"  [{word}]".ljust(12)
            + f"{row.get('note_type', ''):<9}"
            + f"{row.get('note_id')} — {row.get('title') or ''}".rstrip()
        )

    remainder = total - len(rows)
    if remainder > 0:
        lines.append(f"  … {remainder} more (use kb_list to see them)")

    return "\n".join(lines)
