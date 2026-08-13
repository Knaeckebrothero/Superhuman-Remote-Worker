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

import base64
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from .kb_forge import kb_client_for_repo
from .kb_git_source import GiteaKnowledgeGitSource

logger = logging.getLogger(__name__)

# Canonical copy: src/services/knowledge_graph.py (not importable here — the
# orchestrator image has no agent deps; see kb_reindex.py for the same pattern).
PRIORITY_WORDS: dict[int, str] = {0: "high", 1: "normal", 2: "low"}

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
             ORDER BY priority ASC, created_at ASC NULLS LAST, note_id ASC
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

    ``in_progress`` with no ``priority`` key (or an explicit ``None``) renders
    with the ``[...]`` tag OMITTED entirely (``IN PROGRESS: note-id — Title``)
    rather than defaulting to a guessed rank (fix round 1, Finding 2): the
    caller — the loop's own campaign, not a KB row — has no real priority to
    report for its in-progress initiative, and this block exists to be the
    one place an agent can trust about the backlog. Asserting a value nobody
    read would undermine exactly that. Contrast a *pool row*, which always
    carries a real ``priority`` from the query and always gets the tag.
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
        priority = in_progress.get("priority")
        # is-not-None, not truthiness: rank 0 (high) must still get its tag.
        tag = f"[{_priority_word(priority)}] " if priority is not None else ""
        lines.append(
            f"IN PROGRESS: {tag}"
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


_STATUS_LINE = re.compile(r"^status:.*$", re.MULTILINE)

# _rewrite_status outcomes. All three can hand back byte-identical markdown, so
# the returned text alone cannot tell them apart — which is exactly how an
# idempotent re-close came to be logged as malformed frontmatter
# (docs/done/backlog_close_mislabels_idempotent_reclose.md).
_REWRITTEN = "rewritten"  # the status line changed (or one was inserted)
_ALREADY_SET = "already_set"  # the line was found and already holds the target
_NOT_REWRITABLE = "not_rewritable"  # no frontmatter, or no closing `---`


def _rowcount(command_status: Any) -> int:
    """Parse asyncpg's ``conn.execute()`` status tag (``"UPDATE 1"``) into a
    row count. Returns -1 (never 0) on anything unparseable, so a shape this
    doesn't recognise can never be mistaken for the real zero-rows case."""
    try:
        return int(str(command_status).strip().rsplit(" ", 1)[-1])
    except ValueError:
        return -1


def _rewrite_status(markdown: str, new_status: str) -> tuple[str, str]:
    """Replace the frontmatter ``status:`` line, or insert one if absent.

    Returns ``(markdown, outcome)``: ``_REWRITTEN`` (the text differs and wants
    writing), ``_ALREADY_SET`` (a well-formed status line already holds
    ``new_status`` — nothing to write, which is a *success*), or
    ``_NOT_REWRITABLE`` (no frontmatter block at all, or one with no closing
    ``---``). The outcome is reported by the branch that took it rather than
    re-derived by the caller from the text, so there stays exactly one
    frontmatter parser here and nothing to drift out of step with it.

    Deliberately a line rewrite rather than a YAML round-trip: the note is a
    human-editable document and reserializing it would reformat everything the
    author wrote.
    """
    if not markdown.startswith("---"):
        return markdown, _NOT_REWRITABLE
    head, sep, tail = markdown[3:].partition("\n---")
    if not sep:
        return markdown, _NOT_REWRITABLE
    if _STATUS_LINE.search(head):
        rewritten = _STATUS_LINE.sub(f"status: {new_status}", head, count=1)
        if rewritten == head:
            # Byte-identical because the line already reads `status: <target>`
            # — the frontmatter is present and well-formed, not missing.
            return markdown, _ALREADY_SET
        head = rewritten
    else:
        head = head.rstrip("\n") + f"\nstatus: {new_status}\n"
    return "---" + head + sep + tail, _REWRITTEN


async def _resolve_note_repo(project_id: str, postgres_db: Any) -> Any | None:
    """The descriptor for the repo holding this project's notes.

    Routed through ``kb_reindex.resolve_kb_repo`` — the one resolver the KB
    sweep and the note write path also use — rather than re-deriving the
    jobs-repo name from the project id here. A second copy of that rule is
    exactly how the mirror and the reindexer come to target different repos
    once a project has its own ``knowledge`` repo, and that divergence is
    silent (docs/features/knowledge_base_repo_separation.md §5a, §10).

    ``postgres_db`` is late-bound off ``main`` when the caller passes none: it
    is a module global built during orchestrator startup, and importing
    ``main`` at call time rather than import time avoids the circular import
    (same pattern as services/sitrep.py). ``None`` when the project has no KB
    repo at all — there is no file to mirror to, only the index.
    """
    if postgres_db is None:
        try:
            import main as orchestrator_main  # late import: avoid circular

            postgres_db = getattr(orchestrator_main, "postgres_db", None)
        except Exception:
            postgres_db = None
        if postgres_db is None:
            return None

    from .kb_reindex import resolve_kb_repo  # late import: avoid circular

    return await resolve_kb_repo(postgres_db, str(project_id))


async def _read_note_file(
    repo_client: Any, repo_ref: Any, file_path: str
) -> tuple[str | None, str | None]:
    """Return note text and its blob SHA without adding a GitHub file GET."""
    if repo_ref.forge == "gitea":
        return await repo_client.get_file_content(repo_ref.repo, file_path), None

    source = GiteaKnowledgeGitSource(
        repo_client,
        repo_ref.repo,
        branch=repo_ref.branch,
        label=repo_ref.repo,
    )
    head = await source.get_head()
    if not head:
        return None, None
    async with source.snapshot(head) as snapshot:
        current = await snapshot.get_file(file_path)
    if current is None:
        return None, None
    tree = await repo_client.list_tree(repo_ref.repo, head)
    if tree is None:
        return current, None
    blob_sha = next(
        (
            str(entry.get("sha"))
            for entry in tree
            if entry.get("type") == "blob" and entry.get("path") == file_path
        ),
        None,
    )
    return current, blob_sha


async def _write_note_file(
    repo_client: Any,
    repo_ref: Any,
    file_path: str,
    content: str,
    message: str,
    blob_sha: str | None,
) -> bool:
    """Write one backlog note while preserving Gitea's established call."""
    if repo_ref.forge == "gitea":
        return bool(
            await repo_client.create_or_update_file(
                repo_ref.repo,
                file_path,
                content,
                message,
            )
        )
    if not blob_sha:
        # The GitHub contents API requires this for an update. Refuse rather
        # than add a second, hidden location/content lookup rule.
        return False
    return bool(
        await repo_client.change_files(
            repo_ref.repo,
            repo_ref.branch,
            [
                {
                    "path": file_path,
                    "content_b64": base64.b64encode(content.encode("utf-8")).decode(
                        "ascii"
                    ),
                    "operation": "update",
                    "sha": blob_sha,
                }
            ],
            message=message,
        )
    )


async def close_backlog_ticket(
    vector_db: Any,
    gitea: Any,
    project_id: str,
    note_id: str,
    new_status: str,
    *,
    postgres_db: Any = None,
    authority_check: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Mirror a ticket's closed status to the note file AND the index row.

    The database (campaign + campaign_history) stays authoritative for what the
    loop did; this only keeps the pool and the human-readable note in step.
    Best-effort by contract: a disposition must never fail because a mirror
    write failed. Returns True when the durable (file) mirror is *correct* —
    the write landed, or the note already carried the target status and there
    was nothing to write (an idempotent re-close, reachable through the
    torn-advance heal window or a human editing the note first). It returns
    False when there was no status line to rewrite at all (no frontmatter
    block, or one with no closing ``---``): that file was never really
    touched, and claiming success there would hide exactly the case that most
    needs a human to look at the note by hand.

    ``postgres_db`` is only needed to resolve which repo holds the vault (see
    ``_resolve_note_repo``); callers that have the handle should pass it, and
    it is late-bound off ``main`` when they don't.
    """
    file_path = f"knowledge/{note_id}.md"
    durable_ok = False
    repo_name: str | None = None

    async def _permit() -> None:
        if authority_check is not None:
            await authority_check()

    await _permit()
    try:
        repo_ref = await _resolve_note_repo(project_id, postgres_db)
        await _permit()
        if repo_ref is None:
            logger.info(
                "backlog: project %s has no KB repo — %s → %s is an index-only close",
                project_id,
                note_id,
                new_status,
            )
            current, blob_sha = None, None
            repo_client = None
        else:
            repo_name = repo_ref.repo
            repo_client = await kb_client_for_repo(postgres_db, gitea, repo_ref)
            current, blob_sha = await _read_note_file(repo_client, repo_ref, file_path)
        await _permit()
        if current:
            updated, outcome = _rewrite_status(current, new_status)
            if outcome == _ALREADY_SET:
                # Not a failure and not a wrong cause: the mirror is already
                # exactly what this close would have written.
                logger.debug(
                    "backlog: %s already at status: %s — durable mirror "
                    "already correct, nothing to write",
                    file_path,
                    new_status,
                )
                durable_ok = True
            elif outcome == _NOT_REWRITABLE:
                logger.warning(
                    "backlog: %s has no rewritable frontmatter status line "
                    "(missing or malformed frontmatter) — %s → %s left the "
                    "file untouched; index-only close",
                    file_path,
                    note_id,
                    new_status,
                )
            else:  # _REWRITTEN — the only outcome whose text differs
                await _permit()
                durable_ok = bool(
                    await _write_note_file(
                        repo_client,
                        repo_ref,
                        file_path,
                        updated,
                        f"backlog: {note_id} → {new_status}",
                        blob_sha,
                    )
                )
                await _permit()
        elif repo_name is not None:
            logger.info(
                "backlog: note file %s not found in %s — index-only close",
                file_path,
                repo_name,
            )
    except Exception as exc:
        if authority_check is not None:
            from services.project_loop_atomic import ProjectLoopHandoffAuthorityLost

            if isinstance(exc, ProjectLoopHandoffAuthorityLost):
                raise
        logger.warning(
            "backlog: file mirror failed for %s (%s) — the next kb_reindex will "
            "restore the pool entry and the overseer will see it again",
            note_id,
            new_status,
            exc_info=True,
        )

    try:
        await _permit()
        async with vector_db.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE knowledge_index
                   SET status = $3, modified_at = NOW()
                 WHERE project_id = $1::uuid
                   AND note_id = $2
                   AND note_type = ANY($4::text[])
                """,
                project_id,
                note_id,
                new_status,
                list(BACKLOG_NOTE_TYPES),
            )
            await _permit()
            if _rowcount(result) == 0:
                # UPDATE 0: either no such note, or one that exists but isn't
                # a ticket type (e.g. a `decision` note mistakenly filed as a
                # campaign initiative — B1). Look up what's actually there so
                # the warning names the mismatch instead of just the symptom.
                actual_type = await conn.fetchval(
                    "SELECT note_type FROM knowledge_index "
                    "WHERE project_id = $1::uuid AND note_id = $2",
                    project_id,
                    note_id,
                )
                await _permit()
                logger.warning(
                    "backlog: index close matched 0 rows for note_id=%s "
                    "(project %s, wanted status=%s) — stored note_type is "
                    "%r, expected one of %s; a close that closes nothing "
                    "must not be silent",
                    note_id,
                    project_id,
                    new_status,
                    actual_type,
                    BACKLOG_NOTE_TYPES,
                )
    except Exception as exc:
        if authority_check is not None:
            from services.project_loop_atomic import ProjectLoopHandoffAuthorityLost

            if isinstance(exc, ProjectLoopHandoffAuthorityLost):
                raise
        logger.warning(
            "backlog: index mirror failed for %s (%s)",
            note_id,
            new_status,
            exc_info=True,
        )

    return durable_ok
