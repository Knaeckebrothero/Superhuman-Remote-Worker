"""KB prefilter lane — deterministic retirement of unreachable nursery notes.

knowledge-base/knowledge/features/kb_gardening_retire_consolidate_purge.md, G7
rule R4 (tier T1: staged, reversible).

The loop's `learning` / `retrospective` / `state` notes are the nursery: 71 %
of a mature corpus, and most of it links only to other nursery notes. This
lane runs the GC mark-and-sweep the design measured on the real dump (E1b):
roots are the active durable notes, marking follows links out of active
notes, and an active nursery note nothing durable reaches — even transitively
— is **archived** (status flip in the file via the metadata materializer, row
flipped to match, `invalidated_at` stamped). No model call, no bytes removed:
the note stays readable by slug, drops out of search and injection, and is one
status flip from restored. The purge lane removes the file only after its own
grace period, and only if nothing links to it by then.

Off by default (``KB_PREFILTER_ENABLED``); bounded per KB per tick.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any, Optional

from orchestrator.services.kb_materialize import materialize_knowledge_metadata_update

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str) -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, floor: int = 0) -> int:
    try:
        return max(floor, int(str(os.getenv(name, str(default))).strip()))
    except (TypeError, ValueError):
        return default


def prefilter_enabled() -> bool:
    return _env_flag("KB_PREFILTER_ENABLED", "false")


def prefilter_min_age() -> timedelta:
    """A note must be at least this old before reachability is judged — a
    fresh retrospective has not had a chance to be cited yet."""
    return timedelta(days=_env_int("KB_PREFILTER_MIN_AGE_DAYS", 7, floor=1))


def prefilter_max_per_tick() -> int:
    return _env_int("KB_PREFILTER_MAX_PER_TICK", 25, floor=1)


#: Roots of the mark phase: an active note of these types keeps everything it
#: reaches alive. Mirrors the retirement guard's `_RETIRE_ROOT_TYPES` plus
#: `source` (a cited external source is durable evidence).
ROOT_TYPES = (
    "decision",
    "goal",
    "plan",
    "charter",
    "feature",
    "issue",
    "idea",
    "code",
    "question",
    "source",
)
#: What the lane may archive. Everything else is out of scope by construction.
NURSERY_TYPES = ("learning", "retrospective", "state")
#: Never touched, whatever the graph says.
PROTECTED_TAGS = ("pinned", "ready", "parallel-safe")


def _canonical(result: dict[str, Any]) -> bool:
    return result.get("canonical_state") == "canonical" or (
        result.get("status") == "committed"
        or (result.get("status") == "skipped" and result.get("reason") == "unchanged")
    )


async def prefilter_kb_tick(
    *,
    postgres_db: Any,
    store: Any,
    gitea_client: Any,
    kb_id: Any,
    min_age: Optional[timedelta] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """Archive this KB's unreachable nursery notes. One tick, bounded.

    Per candidate: commit `status: archived` into the file (ledgered metadata
    materializer, git first), then flip the row and stamp `invalidated_at`.
    A file the materializer cannot rewrite is skipped and counted, never
    flipped row-only — the file stays the truth. Returns counts; never raises.
    """
    # Explicit None checks: timedelta(0) is falsy and means "no minimum age".
    min_age = prefilter_min_age() if min_age is None else min_age
    limit = prefilter_max_per_tick() if limit is None else max(1, int(limit))
    counts = {"candidates": 0, "archived": 0, "unchanged": 0, "failed": 0}
    try:
        candidates = await store.list_unreachable_nursery(
            kb_id,
            root_types=list(ROOT_TYPES),
            nursery_types=list(NURSERY_TYPES),
            protected_tags=list(PROTECTED_TAGS),
            min_age=min_age,
            limit=limit,
        )
    except Exception:
        logger.exception("kb_prefilter[%s]: could not enumerate candidates", kb_id)
        return counts
    counts["candidates"] = len(candidates)
    archived_slugs: list[str] = []
    for row in candidates:
        slug = str(row.get("note_id") or "")
        if not slug:
            continue
        try:
            result = await materialize_knowledge_metadata_update(
                postgres_db=postgres_db,
                gitea_client=gitea_client,
                project_id=str(kb_id),
                slug=slug,
                status="archived",
            )
        except Exception:
            logger.exception("kb_prefilter[%s]: archive raised for %s", kb_id, slug)
            counts["failed"] += 1
            continue
        if not _canonical(result):
            counts["failed"] += 1
            logger.warning(
                "kb_prefilter[%s]: %s not archived: %s/%s",
                kb_id,
                slug,
                result.get("status"),
                result.get("reason"),
            )
            continue
        if result.get("status") == "skipped" and result.get("reason") == "unchanged":
            counts["unchanged"] += 1
        else:
            counts["archived"] += 1
            archived_slugs.append(slug)
        try:
            await store.set_note_status(kb_id, slug, "archived", invalidated=True)
        except Exception:
            logger.warning(
                "kb_prefilter[%s]: row flip failed for %s (sweep will converge it)",
                kb_id,
                slug,
                exc_info=True,
            )
    if counts["candidates"]:
        # The run report (G4): every retirement traceable to its tick.
        logger.info(
            "kb_prefilter[%s]: R4 unreachable-nursery — candidates=%d archived=%d "
            "unchanged=%d failed=%d (min_age=%dd, cap=%d) | %s",
            kb_id,
            counts["candidates"],
            counts["archived"],
            counts["unchanged"],
            counts["failed"],
            min_age.days,
            limit,
            ", ".join(archived_slugs) or "-",
        )
    return counts


__all__ = [
    "NURSERY_TYPES",
    "PROTECTED_TAGS",
    "ROOT_TYPES",
    "prefilter_enabled",
    "prefilter_kb_tick",
    "prefilter_max_per_tick",
    "prefilter_min_age",
]
