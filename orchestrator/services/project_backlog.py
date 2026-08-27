"""Project backlog — the loop's real work pool.

Before this existed, every loop kickoff told the agent to "check the KB for …
the current open backlog" and there was no backlog: each agent re-derived one
by similarity search, every job. This module makes the pool a deterministic,
indexed listing that the orchestrator hands over verbatim.

Two buckets (knowledge-base/knowledge/superpowers/specs/2026-07-26-project-backlog-pipeline-design.md):

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
from dataclasses import dataclass
from datetime import datetime
from typing import Any

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


@dataclass(frozen=True)
class BacklogCursor:
    """Stable position in the backlog's priority/age/id order."""

    priority: int
    created_at: datetime | None
    note_id: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "BacklogCursor":
        return cls(
            priority=int(row.get("priority") or 0),
            created_at=row.get("created_at"),
            note_id=str(row.get("note_id") or ""),
        )


async def fetch_backlog(
    vector_db: Any,
    project_id: str,
    *,
    exclude_note_id: str | None = None,
    limit: int = BACKLOG_INJECTION_LIMIT,
    require_tags: list[str] | None = None,
    after: BacklogCursor | None = None,
    include_counts: bool = True,
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

    ``require_tags`` narrows to tickets carrying ALL of the given tags --
    ``ready_tag()``/``category_tag(...)`` for the auto-pull tick, which needs
    "what may this pool take next", not "what is open". It remains the fourth
    row-query parameter (the keyset cursor follows it) and the third count-query
    parameter, preserving the existing positional contract. The containment
    operator is load-bearing -- ``tags @> ARRAY[...]`` can use the GIN index on
    tags, ``= ANY`` cannot.

    ``after`` is a keyset cursor over the query's complete order. It is used by
    correctness-sensitive Officer scans that must continue past an arbitrary
    first window without duplicates or gaps, including when priority and
    timestamp tie or ``created_at`` is NULL. ``include_counts=False`` avoids
    repeating the unrelated aggregate on every scan page.

    ``ready_at`` rides along on every row. The tick compares it against its
    claiming job's ``created_at`` (one-shot claims, officer_backlog_pools
    §5.3); a row carrying the ``ready`` tag with a NULL here is unauthorized
    and must not be dispatched.
    """
    tag_filter = list(require_tags or [])
    async with vector_db.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT note_id, note_type, title, priority, tags, ready_at,
                   created_at
              FROM knowledge_index
             WHERE project_id = $1::uuid
               AND status = 'active'
               AND note_type IN {_BACKLOG_NOTE_TYPES_SQL}
               AND ($2::text IS NULL OR note_id <> $2)
               AND ($4::text[] = '{{}}' OR tags @> $4::text[])
               AND (
                    NOT $5::boolean
                    OR priority > $6::integer
                    OR (
                        priority = $6::integer
                        AND (
                            ($7::timestamptz IS NOT NULL AND (
                                created_at > $7::timestamptz
                                OR created_at IS NULL
                            ))
                            OR (
                                created_at IS NOT DISTINCT FROM $7::timestamptz
                                AND note_id > $8::text
                            )
                        )
                    )
               )
             ORDER BY priority ASC, created_at ASC NULLS LAST, note_id ASC
             LIMIT $3
            """,
            project_id,
            exclude_note_id,
            limit,
            tag_filter,
            after is not None,
            after.priority if after else 0,
            after.created_at if after else None,
            after.note_id if after else "",
        )
        count_rows = []
        if include_counts:
            count_rows = await conn.fetch(
                f"""
                SELECT priority, COUNT(*) AS n
                  FROM knowledge_index
                 WHERE project_id = $1::uuid
                   AND status = 'active'
                   AND note_type IN {_BACKLOG_NOTE_TYPES_SQL}
                   AND ($2::text IS NULL OR note_id <> $2)
                   AND ($3::text[] = '{{}}' OR tags @> $3::text[])
                 GROUP BY priority
                """,
                project_id,
                exclude_note_id,
                tag_filter,
            )
    return (
        [dict(r) for r in rows],
        {int(r["priority"]): int(r["n"]) for r in count_rows},
    )


async def fetch_ticket_state(
    vector_db: Any, project_id: str, note_id: str
) -> dict[str, Any] | None:
    """Status/tags/ready_at for ONE ticket, whatever its status.

    ``fetch_backlog`` only ever returns ``active`` rows, which is right for a
    work pool and wrong for the executor disposition gate: that gate has to
    distinguish "the officer closed this ticket" (resolved/archived — no longer
    active, so invisible to the pool query) from "nobody has looked at it yet".
    Returns None when the note does not exist.
    """
    async with vector_db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT project_id, note_id, note_type, title, status, priority,
                   tags, ready_at
              FROM knowledge_index
             WHERE project_id = $1::uuid AND note_id = $2
             LIMIT 1
            """,
            project_id,
            note_id,
        )
    return dict(row) if row else None


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
    claims: dict[str, str] | None = None,
) -> str:
    """Render the kickoff's backlog block. Pure — no I/O, no DB.

    Shape (order is load-bearing: the per-priority totals lead, because with a
    hard cap a large pool otherwise hides its own tail and the idea bucket
    silently becomes write-only)::

        PROJECT BACKLOG — 34 open: 12 high, 15 normal, 7 low (showing top 20)
        IN PROGRESS: [high] issue-deploy-docs — Deployment docs missing
          [high]   feature  feature-rag-boundary — Permission-aware RAG boundary
          [normal] issue    issue-login-timeout — Login times out · claimed by job 4f2a91c8
          … 18 more

    ``claims`` maps ``note_id -> job id`` for tickets a job already carries.
    Claim state is DERIVED — it lives on the job row and is never written back
    to the note — so the caller resolves it and this function only renders it.
    Showing it matters because the pool is read by agents that would otherwise
    re-pick work already in flight; the marker is the same signal a human gets
    from an assignee avatar on a tracker card.

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
        note_id = row.get("note_id")
        claimed_by = (claims or {}).get(str(note_id))
        # Short id: the officer reads this to decide whether to look at the
        # job, and the full uuid would push the title off the line.
        suffix = f" · claimed by job {str(claimed_by)[:8]}" if claimed_by else ""
        lines.append(
            f"  [{word}]".ljust(12)
            + f"{row.get('note_type', ''):<9}"
            + f"{note_id} — {row.get('title') or ''}".rstrip()
            + suffix
        )

    remainder = total - len(rows)
    if remainder > 0:
        lines.append(f"  … {remainder} more (use kb_list to see them)")

    return "\n".join(lines)


_STATUS_LINE = re.compile(r"^status:.*$", re.MULTILINE)

# _rewrite_status outcomes. All three can hand back byte-identical markdown, so
# the returned text alone cannot tell them apart — which is exactly how an
# idempotent re-close came to be logged as malformed frontmatter
# (knowledge-history/done/backlog_close_mislabels_idempotent_reclose.md).
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
    silent (knowledge-base/knowledge/features/knowledge_base_repo_separation.md §5a, §10).

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

    The canonical file is crossed first and the searchable index follows only
    after that succeeds. Returns True only when both legs and their durable
    convergence record succeed. A repository, frontmatter, projection, or
    ledger failure therefore leaves executor disposition unresolved instead of
    reporting a close that reindex could later resurrect.

    ``postgres_db`` is only needed to resolve which repo holds the vault (see
    ``_resolve_note_repo``); callers that have the handle should pass it, and
    it is late-bound off ``main`` when they don't.
    """

    async def _permit() -> None:
        if authority_check is not None:
            await authority_check()

    await _permit()
    if postgres_db is None:
        from main import postgres_db as app_postgres_db

        postgres_db = app_postgres_db

    from services.kb_materialize import materialize_knowledge_metadata_update

    try:
        materialization = await materialize_knowledge_metadata_update(
            postgres_db=postgres_db,
            gitea_client=gitea,
            project_id=project_id,
            slug=note_id,
            status=new_status,
        )
        await _permit()
        canonical_status = materialization.get("canonical_status")
        canonical_ok = (
            materialization.get("canonical_state") == "canonical"
            and materialization.get("canonical_metadata_complete") is True
            and canonical_status is not None
        )
        if not canonical_ok:
            logger.error(
                "backlog: canonical close refused for %s (%s): state=%s reason=%s",
                note_id,
                new_status,
                materialization.get("canonical_state"),
                materialization.get("reason"),
            )
            return False
    except Exception as exc:
        if authority_check is not None:
            from services.project_loop_atomic import ProjectLoopHandoffAuthorityLost

            if isinstance(exc, ProjectLoopHandoffAuthorityLost):
                raise
        logger.warning(
            "backlog: canonical close failed for %s (%s); projection untouched",
            note_id,
            new_status,
            exc_info=True,
        )
        return False

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
                str(canonical_status),
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
                if materialization.get("intent_id"):
                    await postgres_db.finish_knowledge_projection(
                        str(materialization["intent_id"]),
                        project_id=project_id,
                        synced=False,
                        error="index close matched no ticket row",
                    )
                return False
        if materialization.get("intent_id"):
            recorded = await postgres_db.finish_knowledge_projection(
                str(materialization["intent_id"]),
                project_id=project_id,
                synced=True,
            )
            if recorded is None:
                logger.error(
                    "backlog: canonical/index close succeeded for %s but the "
                    "projection ledger did not converge",
                    note_id,
                )
                return False
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
        try:
            if materialization.get("intent_id"):
                await postgres_db.finish_knowledge_projection(
                    str(materialization["intent_id"]),
                    project_id=project_id,
                    synced=False,
                    error=str(exc),
                )
        except Exception:
            logger.exception("backlog: failed to record projection failure")
        return False

    return True
