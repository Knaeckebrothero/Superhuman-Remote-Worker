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
import logging
import os
import posixpath
import re
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from src.tools.knowledge.chunker import embed_note_chunks, embedding_version

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

# The vault root within a project's jobs repo (slice 1 dual-write target).
KNOWLEDGE_PREFIX = "knowledge/"

# OKF reserved filenames — generated navigation/history, never indexed
# (mirrors gardener._RESERVED; index.md carries no frontmatter by spec).
_RESERVED_BASENAMES = {"index.md", "log.md"}

# CHECK-constraint vocabularies from vector/0001 — frontmatter is human-editable,
# so unknown values map to safe defaults instead of failing the row INSERT.
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
}
VALID_STATUSES = {"active", "resolved", "superseded", "archived"}
_DEFAULT_NOTE_TYPE = "learning"
_DEFAULT_STATUS = "active"

# knowledge_index.note_id is VARCHAR(100); superseded_by VARCHAR(100),
# confidence VARCHAR(20).
_NOTE_ID_MAX = 100
_CONFIDENCE_MAX = 20

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def knowledge_blob_map(tree: List[Dict[str, str]]) -> Dict[str, str]:
    """Filter a recursive ``list_tree`` result to indexable knowledge notes.

    Keeps ``knowledge/**/*.md`` blobs, drops the reserved generated files
    (``index.md`` / ``log.md``). Returns ``{path: blob_sha}`` — the same shape
    ``KnowledgeStore.get_indexed_blob_shas`` returns, so the two sides diff
    directly.
    """
    result: Dict[str, str] = {}
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        path = str(entry.get("path", ""))
        if not path.startswith(KNOWLEDGE_PREFIX) or not path.endswith(".md"):
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
    confidence/status/superseded_by), hardened for human-authored files: a
    missing frontmatter block derives the id from the filename stem and the
    title from the first H1; values outside the CHECK-constraint vocabularies
    (valid_note_type / valid_note_status) fall back to safe defaults rather
    than failing the INSERT.
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


async def reindex_kb(
    *,
    gitea_client: Any,
    store: Any,
    embedding_service: Any,
    kb_id: uuid.UUID,
    repo_name: str,
    branch: str = "main",
    force_full: bool = False,
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

    Runs are serialized per KB (see :func:`_kb_lock`); distinct KBs proceed
    concurrently.

    Returns a summary dict: ``status`` (``no-head`` / ``up-to-date`` /
    ``tree-fetch-failed`` / ``completed`` / ``partial``), ``indexed_commit``,
    ``full``, ``upserted``, ``deleted``, ``skipped``, ``errors``.
    """
    async with _kb_lock(kb_id):
        return await _reindex_kb_unlocked(
            gitea_client=gitea_client,
            store=store,
            embedding_service=embedding_service,
            kb_id=kb_id,
            repo_name=repo_name,
            branch=branch,
            force_full=force_full,
        )


async def _reindex_kb_unlocked(
    *,
    gitea_client: Any,
    store: Any,
    embedding_service: Any,
    kb_id: uuid.UUID,
    repo_name: str,
    branch: str = "main",
    force_full: bool = False,
) -> Dict[str, Any]:
    """The reindex cycle body — call through :func:`reindex_kb` (per-KB lock)."""
    head = await gitea_client.get_branch_head_sha(repo_name, branch)
    if not head:
        logger.warning("kb_reindex[%s]: no HEAD for %s@%s", kb_id, repo_name, branch)
        return {
            "status": "no-head",
            "indexed_commit": None,
            "full": False,
            "upserted": 0,
            "deleted": 0,
            "skipped": 0,
            "errors": 0,
        }

    current_version = embedding_version(
        embedding_service.model, embedding_service.expected_dimensions
    )
    wm = await store.get_watermark(kb_id)
    if (
        wm is not None
        and wm.indexed_commit == head
        and wm.pipeline_version == current_version
        and not force_full
    ):
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

    tree = await gitea_client.list_tree(repo_name, head)
    if tree is None:
        logger.warning("kb_reindex[%s]: tree fetch failed at %s", kb_id, head)
        return {
            "status": "tree-fetch-failed",
            "indexed_commit": None,
            "full": full,
            "upserted": 0,
            "deleted": 0,
            "skipped": 0,
            "errors": 0,
        }

    current_map = knowledge_blob_map(tree)
    indexed_map = await store.get_indexed_blob_shas(kb_id)
    upsert_paths, delete_paths = plan_reindex(indexed_map, current_map, full=full)

    upserted = skipped = errors = 0
    for path in upsert_paths:
        try:
            text = await gitea_client.get_file_content(repo_name, path, ref=head)
            if text is None:
                logger.warning("kb_reindex[%s]: fetch failed for %s", kb_id, path)
                errors += 1
                continue
            try:
                fm, body = parse_note_md(text)
            except ValueError as exc:
                # Malformed frontmatter — lint's problem, not the reindexer's.
                logger.warning("kb_reindex[%s]: skipping %s: %s", kb_id, path, exc)
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
            # Claim any pre-slice-3 row for this slug so the (kb_id, path)
            # upsert can't unique-violate on uq_knowledge_project_note.
            await store.adopt_legacy_row(kb_id, fields["note_id"], path)
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
            )
            await store.replace_note_chunks(
                note_row=note_row,
                kb_id=kb_id,
                chunks=chunk_rows,
                embedding_version=current_version,
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
            await store.stamp_note_indexed(note_row, current_map[path], current_version)
            upserted += 1
        except Exception as exc:
            logger.warning("kb_reindex[%s]: error on %s: %s", kb_id, path, exc)
            errors += 1

    deleted = 0
    for path in delete_paths:
        try:
            if await store.delete_kb_note(kb_id, path):
                deleted += 1
        except Exception as exc:
            logger.warning("kb_reindex[%s]: delete error on %s: %s", kb_id, path, exc)
            errors += 1

    if errors == 0:
        await store.upsert_watermark(
            kb_id=kb_id,
            repo_name=repo_name,
            branch=branch,
            indexed_commit=head,
            pipeline_version=current_version,
        )
        status = "completed"
    else:
        status = "partial"

    logger.info(
        "kb_reindex[%s]: %s at %s (full=%s upserted=%d deleted=%d "
        "skipped=%d errors=%d)",
        kb_id,
        status,
        head[:12],
        full,
        upserted,
        deleted,
        skipped,
        errors,
    )
    return {
        "status": status,
        "indexed_commit": head,
        "full": full,
        "upserted": upserted,
        "deleted": deleted,
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
    """One sweep: bring every project KB index up to its repo HEAD.

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
