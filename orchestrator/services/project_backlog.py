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

# The note-type filter, pre-rendered as a SQL literal `IN (...)` list rather
# than bound as `= ANY($n::text[])`. Fix round 1, Finding 1: measured on
# pgvector/pgvector:pg15 (303k rows / 300 projects, ANALYZEd), a bound array
# parameter is fine under the default `auto` plan_cache_mode, but under
# `force_generic_plan` (a real GUC -- effectively what a reused prepared
# statement can settle into once cost estimates favour a generic plan) the
# planner cannot prove `note_type = ANY($n)` implies idx_knowledge_backlog's
# literal `note_type IN ('feature','issue','idea')` partial predicate, and
# falls back to idx_knowledge_project(project_id) + Filter + an explicit
# Sort. The literal form holds under every plan mode. BACKLOG_NOTE_TYPES is a
# hardcoded module constant, never user input, so inlining it is not an
# injection surface -- but it must be derived here, once, and never
# hand-typed a second time: a second copy can silently drift from the tuple
# and the index would go quietly unused again.
_BACKLOG_NOTE_TYPES_SQL = "(" + ", ".join(f"'{t}'" for t in BACKLOG_NOTE_TYPES) + ")"

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
    ``idx_knowledge_backlog`` -- the note-type filter is inlined as a literal
    (``_BACKLOG_NOTE_TYPES_SQL``) rather than bound as ``= ANY($n)``, which is
    what keeps the partial index reachable under every plan mode (fix round
    1, Finding 1). Binding this shifts ``exclude_note_id``/``limit`` down to
    ``$2``/``$3`` (row query) and ``$2`` (count query) -- there is no longer a
    ``$2`` slot for the note-type list.
    """
    async with vector_db.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT note_id, note_type, title, priority
              FROM knowledge_index
             WHERE project_id = $1::uuid
               AND status = 'active'
               AND note_type IN {_BACKLOG_NOTE_TYPES_SQL}
               AND ($2::text IS NULL OR note_id <> $2)
             ORDER BY priority ASC, created_at ASC
             LIMIT $3
            """,
            project_id,
            exclude_note_id,
            limit,
        )
        count_rows = await conn.fetch(
            f"""
            SELECT priority, COUNT(*) AS n
              FROM knowledge_index
             WHERE project_id = $1::uuid
               AND status = 'active'
               AND note_type IN {_BACKLOG_NOTE_TYPES_SQL}
               AND ($2::text IS NULL OR note_id <> $2)
             GROUP BY priority
            """,
            project_id,
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
        IN PROGRESS: [high] issue-deploy-docs — Deployment docs missing
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
