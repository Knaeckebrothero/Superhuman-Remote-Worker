"""OKF KB tree-diff reindexer — files → chunk-granular pgvector index (slice 3 PR3).

The composition layer over slice-3 PR1 (schema + store surface) and PR2 (chunker
+ embed pipeline): read the KB's git tree at HEAD, diff it against what the index
already holds (per-row ``blob_sha``), parse the changed notes with the gardener's
``parse_note_md``, chunk + embed them, persist through ``KnowledgeStore``, and
advance the per-KB watermark — only at the end, and only on a clean run, so an
interrupted or failed reindex self-heals on the next pass (§5: "git is the
Merkle tree").

Design: docs/features/okf_knowledge_base.md §5 / §5.1 / §11 slice-3 PR3.

Dependencies are injected (gitea client, KnowledgeStore, EmbeddingService) so the
flow is unit-testable and trigger sites (post-merge, job-start, leader-gated
sweeper, the MCP operator hatch) stay thin.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import posixpath
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from src.tools.knowledge.chunker import (
    embed_note_chunks,
    embedding_version_for_service,
    note_centroid,
)

# _internal_link_targets is the same body-markdown link parser the dead-link lint
# rule uses (external URLs / anchors / images excluded, `.md` basename returned) —
# reused here so the link table and the linter agree on what a "link" is.
from src.tools.knowledge.gardener import _internal_link_targets, parse_note_md

logger = logging.getLogger(__name__)

# Sweeper cadence. Coarse by design: the post-merge trigger covers the loop's
# hot write path instantly; the sweep only catches out-of-band edits (human
# pushes, recovered partial reindexes), and the up-to-date short-circuit makes
# a no-op check one HEAD fetch + one watermark row per KB.
SWEEP_TICK_SECONDS = int(os.getenv("KB_REINDEX_SWEEP_SECONDS", "900"))

# How often (in successfully stamped notes) to persist a progress counter during
# a run. Coarse on purpose: bounds the extra watermark writes on a large first
# index while keeping the Cockpit progress bar visibly moving.
_PROGRESS_BUMP_EVERY = 25

# The vault root within a project's jobs repo (slice 1 dual-write target).
KNOWLEDGE_PREFIX = "knowledge/"

# Bump when OKF parsing semantics change independently of the chunker. The
# watermark pipeline stamp also includes the normalized root, while chunk rows
# retain the embedding-only version used to filter query vectors.
OKF_PARSER_VERSION = "okf-parser-v1"

# OKF reserved filenames — generated navigation/history, never indexed
# (mirrors gardener._RESERVED; index.md carries no frontmatter by spec).
_RESERVED_BASENAMES = {"index.md", "log.md"}

# CHECK-constraint vocabularies from vector/0001 + vector/0013 — frontmatter is
# human-editable, so unknown values map to safe defaults instead of failing the
# row INSERT. Canonical copy: src/services/knowledge_graph.py NOTE_TYPES (not
# importable here — the orchestrator image has no agent deps).
VALID_NOTE_TYPES = {
    "goal",
    "plan",
    "decision",
    "learning",
    "code",
    "source",
    "question",
    "state",
    "retrospective",
    "datasource",
    "feature",
    "issue",
    "idea",
}
VALID_STATUSES = {"active", "resolved", "superseded", "archived"}
_DEFAULT_NOTE_TYPE = "learning"
_DEFAULT_STATUS = "active"

# Canonical copy: src/services/knowledge_graph.py PRIORITY_RANKS (not importable
# here — the orchestrator image has no agent deps).
PRIORITY_RANKS = {"high": 0, "normal": 1, "low": 2}
_DEFAULT_PRIORITY_RANK = 1

# knowledge_index.note_id is VARCHAR(100); superseded_by VARCHAR(100),
# confidence VARCHAR(20).
_NOTE_ID_MAX = 100
_CONFIDENCE_MAX = 20

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def normalize_root_path(root_path: Optional[str]) -> str:
    """Normalize an already-validated repository-relative OKF root."""
    root = str(root_path or "").strip().replace("\\", "/").strip("/")
    return "/".join(part for part in root.split("/") if part not in ("", "."))


def reindex_pipeline_version(embedding_stamp: str, root_path: Optional[str]) -> str:
    """Watermark stamp for embedding, parser, and root-path semantics."""
    root = normalize_root_path(root_path)
    scope = hashlib.sha256(f"{OKF_PARSER_VERSION}\0{root}".encode()).hexdigest()[:12]
    return f"{embedding_stamp}:{OKF_PARSER_VERSION}:{scope}"


def knowledge_blob_map(
    tree: List[Dict[str, str]], root_path: Optional[str] = "knowledge"
) -> Dict[str, str]:
    """Filter a recursive ``list_tree`` result to indexable knowledge notes.

    Keeps ``knowledge/**/*.md`` blobs, drops the reserved generated files
    (``index.md`` / ``log.md``). Returns ``{path: blob_sha}`` — the same shape
    ``KnowledgeStore.get_indexed_blob_shas`` returns, so the two sides diff
    directly.
    """
    root = normalize_root_path(root_path)
    prefix = f"{root}/" if root else ""
    result: Dict[str, str] = {}
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        path = str(entry.get("path", ""))
        if prefix and not path.startswith(prefix):
            continue
        if not path.endswith(".md"):
            continue
        if posixpath.basename(path) in _RESERVED_BASENAMES:
            continue
        result[path] = str(entry.get("sha", ""))
    return result


def plan_reindex(
    indexed: Dict[str, str],
    current: Dict[str, str],
    full: bool = False,
) -> Tuple[List[str], List[str]]:
    """Diff the indexed blob map against the git tree's.

    Returns ``(upsert_paths, delete_paths)``, both sorted for determinism.
    A path whose indexed ``blob_sha`` matches the tree is skipped — the per-row
    self-heal that makes re-runs after an interrupted reindex cheap. ``full``
    re-upserts everything in the tree (pipeline-version bump / operator rebuild);
    deletes are computed the same way in both modes.
    """
    upserts = sorted(
        path for path, sha in current.items() if full or indexed.get(path) != sha
    )
    deletes = sorted(path for path in indexed if path not in current)
    return upserts, deletes


def note_fields(path: str, fm: Optional[Dict[str, Any]], body: str) -> Dict[str, Any]:
    """Map a parsed note file onto ``upsert_kb_note`` arguments.

    The inverse of ``_render_note_md``'s frontmatter (id/type/tags/keywords/
    confidence/status/priority/superseded_by), hardened for human-authored
    files: a missing frontmatter block derives the id from the filename stem
    and the title from the first H1; values outside the CHECK-constraint
    vocabularies (valid_note_type / valid_note_status) — and unrecognised
    ``priority`` words — fall back to safe defaults rather than failing the
    INSERT.

    ``priority`` is the one field that does NOT follow that "unknown ->
    default" rule (fix round 2, Finding 3): an *absent* ``priority`` key maps
    to ``None`` ("this file carries no opinion — leave the stored rank
    alone"), never to ``_DEFAULT_PRIORITY_RANK``. Every pre-existing note
    (and any human edit that just doesn't touch the line) lacks this key, and
    ``upsert_kb_note`` runs on every merge and via the sweeper — defaulting
    it would silently stamp "normal" over a real priority on the very next
    reindex. An *invalid* value (a typo) is different: the value is present,
    just unparseable, so it still falls back to ``_DEFAULT_PRIORITY_RANK``
    rather than propagating garbage or failing the row.
    """
    fm = fm or {}

    stem = posixpath.basename(path)
    if stem.endswith(".md"):
        stem = stem[: -len(".md")]
    note_id = str(fm.get("id") or stem)[:_NOTE_ID_MAX]

    m = _H1_RE.search(body or "")
    title = m.group(1) if m else note_id

    note_type = str(fm.get("type", "")).strip().lower()
    if note_type not in VALID_NOTE_TYPES:
        note_type = _DEFAULT_NOTE_TYPE

    status = str(fm.get("status", "")).strip().lower()
    if status not in VALID_STATUSES:
        status = _DEFAULT_STATUS

    raw_priority = fm.get("priority")
    if raw_priority is None:
        # Absent from frontmatter, or an explicit YAML null — not "unknown,
        # use the default" like type/status above. See the docstring note.
        priority: Optional[int] = None
    else:
        priority = PRIORITY_RANKS.get(
            str(raw_priority).strip().lower(), _DEFAULT_PRIORITY_RANK
        )

    def _as_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return [str(value)]

    confidence = fm.get("confidence")
    superseded_by = fm.get("superseded_by")

    return {
        "note_id": note_id,
        "title": title,
        "note_type": note_type,
        "status": status,
        "priority": priority,
        "tags": _as_list(fm.get("tags")),
        "keywords": _as_list(fm.get("keywords")),
        "confidence": str(confidence)[:_CONFIDENCE_MAX] if confidence else None,
        "superseded_by": str(superseded_by)[:_NOTE_ID_MAX] if superseded_by else None,
    }


_kb_locks: Dict[uuid.UUID, asyncio.Lock] = {}


def _kb_lock(kb_id: uuid.UUID) -> asyncio.Lock:
    """Per-KB serialization for reindex runs (PR3.1).

    Two post-merge triggers ~30s apart ran concurrent full rebuilds against the
    same KB on dev (interleaved chunk delete+insert batches). All in-process
    entry points (post-merge trigger, sweeper tick, operator endpoint) funnel
    through :func:`reindex_kb`, so an asyncio lock suffices — the sweeper is
    leader-gated and loop advances run on the leader. The second replica's
    operator endpoint can still race in theory; the up-to-date short-circuit
    under the lock makes the overlap window one HEAD read.
    """
    return _kb_locks.setdefault(kb_id, asyncio.Lock())


@asynccontextmanager
async def kb_index_lock(store: Any, kb_id: uuid.UUID, *, wait: bool = False):
    """Serialize all mutations of one KB index locally and across replicas.

    Routine reindex requests use the non-blocking advisory claim and report
    ``already-indexing`` when another replica owns it. Datasource deletion uses
    ``wait=True`` because it must run *after* any mutation already in flight and
    keep the claim until both vector cleanup and app-row deletion are complete.

    Narrow mock stores used by legacy unit tests have neither concrete lock
    method; they still receive the in-process serialization contract.
    """
    async with _kb_lock(kb_id):
        if wait:
            lock_method = getattr(type(store), "reindex_lock", None)
            if lock_method is not None:
                async with store.reindex_lock(kb_id) as conn:
                    yield conn
                return
            yield None
            return

        claim_method = getattr(type(store), "try_reindex_lock", None)
        if claim_method is not None:
            async with store.try_reindex_lock(kb_id) as claimed:
                yield bool(claimed)
            return
        yield True


def _empty_summary(
    status: str,
    *,
    errors: int = 0,
    indexed_commit: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "indexed_commit": indexed_commit,
        "full": False,
        "upserted": 0,
        "deleted": 0,
        "skipped": 0,
        "errors": errors,
    }


async def _set_reindex_status(
    store: Any,
    kb_id: uuid.UUID,
    status: str,
    *,
    source_label: str,
    branch: Optional[str],
    source_head: Optional[str] = None,
    last_error: Optional[str] = None,
) -> None:
    """Best-effort operational state; never compromise index durability."""
    try:
        await store.set_watermark_status(
            kb_id,
            status,
            source_head=source_head,
            last_error=(last_error or "")[:2000] or None,
            repo_name=source_label[:255],
            branch=(branch or "")[:255] or None,
        )
    except Exception as exc:
        logger.warning("kb_reindex[%s]: status update skipped: %s", kb_id, exc)


async def reindex_kb(
    *,
    store: Any,
    embedding_service: Any,
    kb_id: uuid.UUID,
    source: Any = None,
    root_path: Optional[str] = None,
    source_label: Optional[str] = None,
    # Legacy native-project arguments. They remain supported so post-merge,
    # MCP, and existing callers can migrate independently.
    gitea_client: Any = None,
    repo_name: Optional[str] = None,
    branch: Optional[str] = "main",
    force_full: bool = False,
    is_active: Optional[Callable[[], Awaitable[bool]]] = None,
) -> Dict[str, Any]:
    """Bring a KB's chunk-granular index up to the repo's HEAD.

    The watermark cycle (§5): read HEAD → short-circuit if the watermark already
    covers it under the current pipeline version → diff the git tree's
    ``{path: blob_sha}`` map against the index's → per changed note:
    fetch @HEAD, ``parse_note_md``, chunk + embed (PR2), adopt any legacy row,
    upsert the note row UNSTAMPED, replace its chunks (PR1), then stamp
    ``blob_sha``/``embedding_version`` → remove deleted notes → advance the
    watermark. Embed-before-write and stamp-after-chunks ordering per note: a
    note whose embedding OR chunk write failed keeps a stale/NULL ``blob_sha``,
    so the next run retries it.

    Full rebuild (``full=True`` in the plan) when the pipeline version changed
    (new model/dims/chunker), when there is no watermark yet (first index of a
    legacy corpus — also when legacy row adoption happens), or on ``force_full``
    (the ``kb reindex --full`` operator hatch).

    Honesty rules: the watermark advances ONLY on a zero-error run (``status:
    completed``); any fetch/embed/persist error leaves it untouched (``partial``)
    and the per-row ``blob_sha`` self-heal makes the retry cheap. Unparseable
    notes are *skipped*, not errors — a malformed file is ``kb_lint``'s problem
    and must not wedge the watermark forever. Legacy pathless rows whose file no
    longer exists are invisible to the diff and left for the offline frontmatter
    audit (§11 migration-hygiene gate).

    Runs are serialized per KB (see :func:`kb_index_lock`); distinct KBs proceed
    concurrently.

    Returns a summary dict: ``status`` (``no-head`` / ``up-to-date`` /
    ``tree-fetch-failed`` / ``completed`` / ``partial`` / ``source-deleted``),
    ``indexed_commit``, ``full``, ``upserted``, ``deleted``, ``skipped``,
    ``errors``.
    """
    if source is None:
        if gitea_client is None or not repo_name:
            raise ValueError("reindex_kb requires a git source")
        from .kb_git_source import GiteaKnowledgeGitSource

        source = GiteaKnowledgeGitSource(
            gitea_client,
            repo_name,
            branch=branch or "main",
            label=source_label or repo_name,
        )
        if root_path is None:
            root_path = "knowledge"
    elif root_path is None:
        root_path = ""

    label = str(source_label or source.label or "knowledge-repository")
    normalized_root = normalize_root_path(root_path)

    async with kb_index_lock(store, kb_id) as claimed:
        if not claimed:
            return _empty_summary("already-indexing")
        # External datasource work lists can become stale while they wait for
        # an in-flight rebuild or deletion. Re-check application ownership only
        # after both mutation locks are held, before even writing a status row.
        if is_active is not None and not await is_active():
            return _empty_summary("source-deleted")
        return await _reindex_kb_unlocked(
            source=source,
            store=store,
            embedding_service=embedding_service,
            kb_id=kb_id,
            source_label=label,
            branch=branch,
            root_path=normalized_root,
            force_full=force_full,
        )


async def _reindex_kb_unlocked(
    *,
    source: Any,
    store: Any,
    embedding_service: Any,
    kb_id: uuid.UUID,
    source_label: str,
    branch: Optional[str],
    root_path: str,
    force_full: bool = False,
) -> Dict[str, Any]:
    """The reindex cycle body — call through :func:`reindex_kb` (per-KB lock)."""
    # Read the durable as-of marker before contacting the source. Every failure
    # summary must report the commit the index still covers, never the uncommitted
    # source HEAD (or a misleading None when a prior usable snapshot exists).
    wm = await store.get_watermark(kb_id)
    previous_indexed_commit = wm.indexed_commit if wm is not None else None
    try:
        head = await source.get_head()
    except Exception as exc:
        detail = str(exc)[-2000:] or "Git HEAD read failed"
        await _set_reindex_status(
            store,
            kb_id,
            "failed",
            source_label=source_label,
            branch=branch,
            last_error=detail,
        )
        logger.warning(
            "kb_reindex[%s]: HEAD read failed for %s: %s", kb_id, source_label, detail
        )
        return _empty_summary(
            "source-failed", errors=1, indexed_commit=previous_indexed_commit
        )
    if not head:
        await _set_reindex_status(
            store,
            kb_id,
            "failed",
            source_label=source_label,
            branch=branch,
            last_error="Repository branch has no resolvable HEAD",
        )
        logger.warning("kb_reindex[%s]: no HEAD for %s", kb_id, source_label)
        return _empty_summary("no-head", indexed_commit=previous_indexed_commit)

    embedding_stamp = embedding_version_for_service(embedding_service)
    current_version = reindex_pipeline_version(embedding_stamp, root_path)
    if (
        wm is not None
        and wm.indexed_commit == head
        and wm.pipeline_version == current_version
        and not force_full
    ):
        await _set_reindex_status(
            store,
            kb_id,
            "ready",
            source_label=source_label,
            branch=branch,
            source_head=head,
        )
        return {
            "status": "up-to-date",
            "indexed_commit": head,
            "full": False,
            "upserted": 0,
            "deleted": 0,
            "skipped": 0,
            "errors": 0,
        }

    full = force_full or wm is None or wm.pipeline_version != current_version

    await _set_reindex_status(
        store,
        kb_id,
        "indexing",
        source_label=source_label,
        branch=branch,
        source_head=head,
    )
    try:
        async with source.snapshot(head) as snapshot:
            return await _reindex_snapshot(
                snapshot=snapshot,
                store=store,
                embedding_service=embedding_service,
                kb_id=kb_id,
                source_label=source_label,
                branch=branch,
                root_path=root_path,
                head=head,
                embedding_stamp=embedding_stamp,
                pipeline_version=current_version,
                full=full,
                previous_indexed_commit=previous_indexed_commit,
            )
    except Exception as exc:
        detail = str(exc)[-2000:] or "Git snapshot failed"
        await _set_reindex_status(
            store,
            kb_id,
            "failed",
            source_label=source_label,
            branch=branch,
            source_head=head,
            last_error=detail,
        )
        logger.warning(
            "kb_reindex[%s]: snapshot failed for %s: %s",
            kb_id,
            source_label,
            detail,
        )
        return _empty_summary(
            "source-failed", errors=1, indexed_commit=previous_indexed_commit
        )


async def _reindex_snapshot(
    *,
    snapshot: Any,
    store: Any,
    embedding_service: Any,
    kb_id: uuid.UUID,
    source_label: str,
    branch: Optional[str],
    root_path: str,
    head: str,
    embedding_stamp: str,
    pipeline_version: str,
    full: bool,
    previous_indexed_commit: Optional[str],
) -> Dict[str, Any]:
    """Index one immutable source snapshot, which owns all blob reads."""

    try:
        tree = await snapshot.list_tree()
    except Exception as exc:
        detail = str(exc)[-2000:] or "Git tree read failed"
        await _set_reindex_status(
            store,
            kb_id,
            "failed",
            source_label=source_label,
            branch=branch,
            source_head=head,
            last_error=detail,
        )
        logger.warning("kb_reindex[%s]: tree fetch failed at %s", kb_id, head)
        return {
            "status": "tree-fetch-failed",
            "indexed_commit": previous_indexed_commit,
            "full": full,
            "upserted": 0,
            "deleted": 0,
            "skipped": 0,
            "errors": 1,
        }

    current_map = knowledge_blob_map(tree, root_path)
    indexed_map = await store.get_indexed_blob_shas(kb_id)
    upsert_paths, delete_paths = plan_reindex(indexed_map, current_map, full=full)

    # Reset determinate-progress counters for this run so Cockpit can render a
    # bar. notes_total is the per-run work set (changed notes on an incremental
    # run, the whole vault on a full rebuild). Best-effort: never fail on it.
    notes_total = len(upsert_paths)
    try:
        await store.update_index_progress(kb_id, 0, notes_total)
    except Exception as exc:
        logger.debug("kb_reindex[%s]: progress reset skipped: %s", kb_id, exc)

    upserted = skipped = errors = 0
    invalid_paths: List[str] = []
    for path in upsert_paths:
        try:
            text = await snapshot.get_file(path)
            if text is None:
                logger.warning("kb_reindex[%s]: fetch failed for %s", kb_id, path)
                errors += 1
                continue
            try:
                fm, body = parse_note_md(text)
            except ValueError as exc:
                # Malformed frontmatter — lint's problem, not the reindexer's.
                # Remove any prior indexed version of this path: serving stale
                # content after the canonical file became invalid would be less
                # honest than omitting it until a later commit repairs the note.
                logger.warning("kb_reindex[%s]: skipping %s: %s", kb_id, path, exc)
                invalid_paths.append(path)
                skipped += 1
                continue
            fields = note_fields(path, fm, body)
            # Embed BEFORE writing: a failed embed leaves the stale blob_sha in
            # place, so the next run retries this note.
            chunk_rows, _ = await embed_note_chunks(
                body,
                title=fields["title"],
                note_type=fields["note_type"],
                tags=fields["tags"],
                embedding_service=embedding_service,
            )
            # Claim a pre-slice-3 row or move the stable OKF id on a Git rename,
            # so the path-keyed upsert cannot collide with the old row's
            # uq_knowledge_project_note identity.
            await store.adopt_legacy_row(
                kb_id,
                fields["note_id"],
                path,
                movable_paths=delete_paths,
            )
            # Upsert UNSTAMPED (blob_sha/embedding_version NULL): the stamp
            # means "chunks durable" and lands only after replace_note_chunks,
            # so a chunk-write failure keeps the note in the next run's diff.
            note_row = await store.upsert_kb_note(
                kb_id=kb_id,
                note_id=fields["note_id"],
                path=path,
                title=fields["title"],
                note_type=fields["note_type"],
                content=body,
                blob_sha=None,
                embedding_version=None,
                status=fields["status"],
                confidence=fields["confidence"],
                tags=fields["tags"],
                keywords=fields["keywords"],
                superseded_by=fields["superseded_by"],
                priority=fields["priority"],
            )
            await store.replace_note_chunks(
                note_row=note_row,
                kb_id=kb_id,
                chunks=chunk_rows,
                embedding_version=embedding_stamp,
            )
            # Rewrite the note's outbound link edges (the kg-less kb_related
            # backend). Before the stamp, so a link-write failure keeps the note
            # in the next run's diff — same durable-then-stamp invariant as chunks.
            await store.replace_note_links(
                source_note_row=note_row,
                kb_id=kb_id,
                source_id=fields["note_id"],
                targets=_internal_link_targets(body),
            )
            # Whole-note centroid (PR4d): the mean of this note's chunk vectors,
            # written back onto the note row atomically with the stamp so
            # find_near_duplicate_pairs (which filters embedding IS NOT NULL) sees
            # reindexed notes again.
            centroid = note_centroid([c["embedding"] for c in chunk_rows])
            await store.stamp_note_indexed(
                note_row, current_map[path], embedding_stamp, centroid=centroid
            )
            upserted += 1
            # Throttled determinate-progress bump (best-effort).
            if upserted % _PROGRESS_BUMP_EVERY == 0:
                try:
                    await store.update_index_progress(kb_id, upserted)
                except Exception as exc:
                    logger.debug(
                        "kb_reindex[%s]: progress bump skipped: %s", kb_id, exc
                    )
        except Exception as exc:
            logger.warning("kb_reindex[%s]: error on %s: %s", kb_id, path, exc)
            errors += 1

    deleted = 0
    for path in sorted(set(delete_paths + invalid_paths)):
        try:
            if await store.delete_kb_note(kb_id, path):
                deleted += 1
        except Exception as exc:
            logger.warning("kb_reindex[%s]: delete error on %s: %s", kb_id, path, exc)
            errors += 1

    # Reconcile orphaned provisional rows (R-1): pathless active rows the tree can
    # never adopt (failed/squashed commit, slug mismatch, create-then-delete).
    # current_map holds EVERY knowledge file in the tree, so the slug set is
    # complete even on an incremental run. Non-fatal and outside the error count —
    # a hygiene pass must never wedge the watermark or fail the reindex.
    reconciled = 0
    try:
        tree_slugs = [posixpath.basename(p)[: -len(".md")] for p in current_map]
        reconciled = await store.reconcile_orphans(
            project_id=kb_id, tree_slugs=tree_slugs
        )
    except Exception as exc:
        logger.warning("kb_reindex[%s]: reconcile pass skipped: %s", kb_id, exc)

    if errors == 0:
        await store.upsert_watermark(
            kb_id=kb_id,
            repo_name=source_label[:255],
            branch=branch,
            indexed_commit=head,
            pipeline_version=pipeline_version,
            source_head=head,
            status="ready",
            last_error=None,
        )
        status = "completed"
    else:
        status = "partial"
        await _set_reindex_status(
            store,
            kb_id,
            "partial",
            source_label=source_label,
            branch=branch,
            source_head=head,
            last_error=f"{errors} note operation(s) failed; retry scheduled",
        )

    # Final progress reflects what durably landed this run so the bar doesn't
    # stall between the last throttled bump and completion (best-effort).
    try:
        await store.update_index_progress(kb_id, upserted, notes_total)
    except Exception as exc:
        logger.debug("kb_reindex[%s]: final progress write skipped: %s", kb_id, exc)

    logger.info(
        "kb_reindex[%s]: %s at %s (full=%s upserted=%d deleted=%d "
        "reconciled=%d skipped=%d errors=%d)",
        kb_id,
        status,
        head[:12],
        full,
        upserted,
        deleted,
        reconciled,
        skipped,
        errors,
    )
    return {
        "status": status,
        "indexed_commit": head if errors == 0 else previous_indexed_commit,
        "full": full,
        "upserted": upserted,
        "deleted": deleted,
        "reconciled": reconciled,
        "skipped": skipped,
        "errors": errors,
    }


async def resolve_kb_repo(
    postgres_db: Any, project_id: str
) -> Optional[Tuple[str, str]]:
    """Resolve a project's KB vault location: ``(repo_name, branch)``.

    The vault lives under ``knowledge/`` in the project's jobs repo (slice-1
    dual-write target); the first jobs-role repo wins. ``None`` when the project
    has no jobs repo — nothing to index.
    """
    repos = await postgres_db.get_project_repositories(project_id, role="jobs")
    if not repos:
        return None
    first = repos[0]
    return str(first.get("name")), str(first.get("branch") or "main")


async def kb_sweep_tick(
    *,
    postgres_db: Any,
    store: Any,
    gitea_client: Any,
    embedding_service: Any,
    reindex_fn: Callable[..., Awaitable[Dict[str, Any]]] = reindex_kb,
) -> int:
    """One sweep: refresh native project and external datasource KBs.

    The work list is every jobs-role project repo. Per-KB failures are logged
    and skipped — one broken repo must not starve the rest. Returns the number
    of KBs that actually did work (``up-to-date`` checks don't count).
    """
    rows = await postgres_db.fetch(
        """
        SELECT DISTINCT ON (project_id) project_id, name, branch
        FROM project_repositories
        WHERE role = 'jobs'
        ORDER BY project_id, created_at ASC
        """
    )
    worked = 0
    for row in rows:
        project_id = row["project_id"]
        try:
            result = await reindex_fn(
                gitea_client=gitea_client,
                store=store,
                embedding_service=embedding_service,
                kb_id=project_id,
                repo_name=str(row["name"]),
                branch=str(row["branch"] or "main"),
            )
            if result.get("status") not in ("up-to-date", "no-head"):
                worked += 1
        except Exception:
            logger.exception("kb_sweep: reindex failed for project %s", project_id)
    # External KB datasources are indexed once under datasources.id, regardless
    # of how many projects/jobs select them. ``list_datasources`` decrypts the
    # credential field only inside this orchestrator process; the source keeps
    # those credentials out of argv, logs, status, and agent payloads.
    try:
        external_rows = await postgres_db.list_datasources(ds_type="kb", limit=10000)
    except Exception:
        logger.exception("kb_sweep: failed to enumerate external KB datasources")
        external_rows = []
    if not isinstance(external_rows, (list, tuple)):
        external_rows = []  # compatibility with narrow/mocked DB facades

    if external_rows:
        from .kb_datasources import reindex_kb_datasource

    for datasource in external_rows:
        datasource_id = datasource.get("id")
        try:

            async def datasource_is_active(
                datasource_id: Any = datasource_id,
            ) -> bool:
                current = await postgres_db.get_datasource(str(datasource_id))
                return bool(current and current.get("type") == "kb")

            result = await reindex_kb_datasource(
                datasource,
                store=store,
                embedding_service=embedding_service,
                is_active=datasource_is_active,
                reindex_fn=reindex_fn,
            )
            if result.get("status") not in (
                "up-to-date",
                "no-head",
                "already-indexing",
                "source-deleted",
            ):
                worked += 1
        except Exception:
            logger.exception(
                "kb_sweep: reindex failed for datasource %s", datasource_id
            )
    return worked


async def kb_reindex_sweeper_loop(
    postgres_db: Any,
    store: Any,
    gitea_client: Any,
    shutdown_event: asyncio.Event,
    *,
    embedding_service_factory: Callable[[], Awaitable[Any]],
) -> None:
    """Periodic KB index freshness sweep (leader-gated by the caller).

    Mirrors ``project_loop_sweeper_loop``'s tick + shutdown-aware wait. The
    embedding service is re-resolved every tick via the injected factory so
    catalog changes (Admin → Models) take effect without a restart; a tick with
    no resolvable embedding service is skipped loudly — a keyless reindex could
    only write vectorless rows, and honesty beats coverage.
    """
    logger.info("KB reindex sweeper started (tick=%ds)", SWEEP_TICK_SECONDS)
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=SWEEP_TICK_SECONDS)
            break  # shutdown requested
        except asyncio.TimeoutError:
            pass  # tick due
        try:
            svc = await embedding_service_factory()
            if svc is None:
                logger.warning(
                    "kb_sweep: no embedding service resolvable (catalog or env) "
                    "— skipping tick"
                )
                continue
            worked = await kb_sweep_tick(
                postgres_db=postgres_db,
                store=store,
                gitea_client=gitea_client,
                embedding_service=svc,
            )
            if worked:
                logger.info("kb_sweep: %d KB(s) reindexed", worked)
        except Exception:
            logger.exception("kb_sweep: tick failed")
    logger.info("KB reindex sweeper stopped")
