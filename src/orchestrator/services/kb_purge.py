"""KB purge lane — physical removal of long-retired notes.

knowledge-base/knowledge/features/kb_gardening_retire_consolidate_purge.md, G2.

Agents never remove bytes: ``kb_delete`` and the converge pass only *retire*
(status ``archived`` / ``superseded``), which hides a note from search and
injection while the file stays in the repository. This module is the
deterministic second phase: once a retired note has sat untouched for the
grace period **and** nothing active links to it, its file is removed with a
compare-and-swap commit and its index rows dropped. Git history keeps the
bytes; recovery is a revert.

Off by default (``KB_PURGE_ENABLED``). Rides the reindex sweep, runs per KB
after that KB's reindex, and is bounded per tick so one bloated vault cannot
turn a sweep into a deletion storm.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any, Optional

from orchestrator.services.kb_materialize import materialize_knowledge_note_delete

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str) -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, floor: int = 0) -> int:
    try:
        return max(floor, int(str(os.getenv(name, str(default))).strip()))
    except (TypeError, ValueError):
        return default


def purge_enabled() -> bool:
    """Whether the purge lane runs at all. Opt-in: a wrong retirement is
    reversible with one status flip only while the file is still there."""
    return _env_flag("KB_PURGE_ENABLED", "false")


def purge_grace() -> timedelta:
    """How long a note must have been retired before it is purged."""
    return timedelta(days=_env_int("KB_PURGE_GRACE_DAYS", 14, floor=1))


def purge_max_per_tick() -> int:
    """Blast-radius cap per KB per sweep."""
    return _env_int("KB_PURGE_MAX_PER_TICK", 25, floor=1)


#: Never purged, whatever their status: the charter is the project's identity
#: and tickets/reports carry pipeline history the officer reads back.
PURGE_EXCLUDED_TYPES = ("charter", "report", "feature", "issue", "idea")


async def purge_kb_tick(
    *,
    postgres_db: Any,
    store: Any,
    gitea_client: Any,
    kb_id: Any,
    grace: Optional[timedelta] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """Purge this KB's long-retired, unreferenced notes. One tick, bounded.

    Candidates come from :meth:`KnowledgeStore.list_purge_candidates`, which
    already applies the three-signal rule (retired ≥ grace, no active inbound
    link, not an excluded type). Each candidate is removed through the ledgered
    delete op with ``expected_blob_sha`` = the blob the row was indexed from,
    so a note edited (or un-archived and re-indexed) after enumeration is
    refused, not purged. Returns counts; never raises.
    """
    # Explicit None checks: timedelta(0) is falsy, and a caller passing it
    # means "no grace" (tests, operator tooling), not "use the default".
    grace = purge_grace() if grace is None else grace
    limit = purge_max_per_tick() if limit is None else max(1, int(limit))
    counts = {"candidates": 0, "purged": 0, "absent": 0, "refused": 0, "failed": 0}
    try:
        candidates = await store.list_purge_candidates(
            kb_id, grace=grace, limit=limit, excluded_types=list(PURGE_EXCLUDED_TYPES)
        )
    except Exception:
        logger.exception("kb_purge[%s]: could not enumerate candidates", kb_id)
        return counts
    counts["candidates"] = len(candidates)
    for row in candidates:
        slug = str(row.get("note_id") or "")
        if not slug:
            continue
        reason = (
            f"purge: status={row.get('status')} for >= {grace.days}d, "
            "no active note links to it"
        )
        try:
            result = await materialize_knowledge_note_delete(
                postgres_db=postgres_db,
                gitea_client=gitea_client,
                project_id=str(kb_id),
                slug=slug,
                reason=reason,
                expected_blob_sha=row.get("blob_sha"),
                store=store,
            )
        except Exception:
            logger.exception("kb_purge[%s]: delete raised for %s", kb_id, slug)
            counts["failed"] += 1
            continue
        status, why = result.get("status"), result.get("reason")
        if status == "committed":
            counts["purged"] += 1
            logger.info("kb_purge[%s]: removed %s (%s)", kb_id, slug, reason)
        elif status == "skipped" and why in {"absent", "already-canonical"}:
            counts["absent"] += 1
        elif why == "precondition-failed":
            counts["refused"] += 1
            logger.info(
                "kb_purge[%s]: %s changed since enumeration — left alone", kb_id, slug
            )
        else:
            counts["failed"] += 1
            logger.warning(
                "kb_purge[%s]: %s not purged: %s/%s", kb_id, slug, status, why
            )
    if counts["candidates"]:
        logger.info(
            "kb_purge[%s]: candidates=%d purged=%d absent=%d refused=%d failed=%d",
            kb_id,
            counts["candidates"],
            counts["purged"],
            counts["absent"],
            counts["refused"],
            counts["failed"],
        )
    return counts


__all__ = [
    "PURGE_EXCLUDED_TYPES",
    "purge_enabled",
    "purge_grace",
    "purge_kb_tick",
    "purge_max_per_tick",
]
