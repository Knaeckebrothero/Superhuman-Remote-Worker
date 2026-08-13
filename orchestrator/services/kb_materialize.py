"""Server-side materialisation of ONE knowledge note into a project's KB repo.

Step 3 of ``docs/features/knowledge_base_repo_separation.md``. The agent used
to materialise every note as ``knowledge/<slug>.md`` inside its own workspace
checkout (``_dual_write_note``), which welded the vault to the jobs repo and,
because it was guarded on ``has_git()``, skipped entirely for anything without
git — persistent sessions, lite tiers, repo-less projects — leaving those
notes pathless and therefore invisible to ``kb_read`` / ``kb_search``. The
write moves here instead: one note, one Gitea commit, into whichever repo §5's
``resolve_kb_repo`` picks for the project.

Nothing downstream changes. Files stay canonical, the reindexer still ingests
``knowledge/**/*.md`` from git on its next sweep and sets ``path``, and reads
keep their ``path IS NOT NULL`` gate. Only the *writer* moved, from the agent's
workspace checkout to a commit the orchestrator makes.

By contract this **never raises for an expected condition**: the caller (the
agent's ``kb_write``, via the orchestrator endpoint) must be able to
log-and-continue exactly as the old non-fatal file write did. Every outcome —
including "there is no repo" and "Gitea refused" — comes back as a status dict.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from typing import Any, Optional, Tuple

from services.kb_forge import kb_client_for_repo

# The vault prefix and the repo resolution are both owned by the reindexer:
# the writer and the sweep MUST agree on repo and path or they silently
# diverge (that is the shape of kb_reindex_watermark_never_advances).
from services.kb_reindex import KNOWLEDGE_PREFIX, resolve_kb_repo

logger = logging.getLogger(__name__)

# A slug becomes a repository path (``knowledge/<slug>.md``) and is authored by
# an LLM, so it is untrusted input to a path join. Anything with a separator,
# a leading dot or a traversal segment is refused rather than sanitised —
# silently rewriting a slug would make the file's stem disagree with the
# ``knowledge_index`` row's ``note_id``, and the reindexer matches them by
# stem. Underscores are common in live note ids, hence the permissive body.
_SAFE_SLUG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,199}$")

# OKF reserved basenames: generated navigation/history that the reindexer
# never indexes (``kb_reindex._RESERVED_BASENAMES``). A note written to one of
# these would clobber generated content AND be permanently invisible to every
# reader, so it is refused at the door instead.
_RESERVED_SLUGS = frozenset({"index", "log"})

_STATUS_COMMITTED = "committed"
_STATUS_SKIPPED = "skipped"
_STATUS_FAILED = "failed"


def note_repo_path(slug: str) -> str:
    """The vault-relative path a note materialises to."""
    return f"{KNOWLEDGE_PREFIX}{slug}.md"


def slug_error(slug: str) -> Optional[str]:
    """Operator-readable reason the slug is unusable as a path, else None."""
    candidate = str(slug or "").strip()
    if not candidate:
        return "empty note slug"
    if not _SAFE_SLUG.match(candidate):
        return (
            f"unsafe note slug {candidate!r} — a slug becomes the path "
            f"{KNOWLEDGE_PREFIX}<slug>.md and may only contain letters, "
            "digits, '.', '_' and '-' (no path separators, no leading dot)"
        )
    if candidate.lower() in _RESERVED_SLUGS:
        return (
            f"reserved note slug {candidate!r} — {KNOWLEDGE_PREFIX}"
            f"{candidate}.md is generated OKF navigation and is never indexed"
        )
    return None


def _git_blob_sha(data: bytes) -> str:
    """The git object id this content would have as a blob.

    Lets an unchanged rewrite be recognised from the tree listing we already
    fetched, with no extra API call. A wrong answer here can only ever produce
    a redundant commit (it degrades to "always write"), never a skipped one.
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324


def _commit_message(slug: str, job_id: Optional[str]) -> str:
    """Commit text naming the note and the job that wrote it.

    §3's *"which job changed what note when"*: the subject carries the short
    id for ``git log --oneline`` readability, the body the full UUID so
    attribution stays machine-recoverable.
    """
    job = str(job_id or "").strip()
    if not job:
        return f"kb: {slug}"
    return f"kb: {slug} (job {job[:8]})\n\njob: {job}"


def _result(
    status: str,
    *,
    reason: Optional[str] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    path: Optional[str] = None,
    operation: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "repo": repo,
        "branch": branch,
        "path": path,
        "operation": operation,
    }


async def _path_exists(
    gitea_client: Any, repo_name: str, branch: str, path: str
) -> Tuple[Optional[bool], Optional[str]]:
    """``(exists, blob_sha)`` for ``path`` on ``branch``; ``(None, None)``
    when the tree could not be read at all.

    Same probe as the curated merge (``services.project_loops``): read the
    target ref's tree once and decide the ``change_files`` operation against
    it, because ``create`` on an existing path is a Gitea 422. ``None`` is
    deliberately distinct from ``False`` — an unreadable tree is a guess, and
    the caller flips the operation on refusal rather than pretending it knew.
    """
    try:
        tree = await gitea_client.list_tree(repo_name, branch)
    except Exception as e:  # noqa: BLE001 — a probe failure is not fatal
        logger.warning(
            "kb-materialize: tree read raised for %s@%s: %r", repo_name, branch, e
        )
        return None, None
    if tree is None:
        return None, None
    for entry in tree:
        if entry.get("type") == "blob" and str(entry.get("path")) == path:
            return True, entry.get("sha")
    return False, None


async def materialize_knowledge_note(
    *,
    postgres_db: Any,
    gitea_client: Any,
    project_id: str,
    slug: str,
    content: str,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Commit one rendered note to ``knowledge/<slug>.md`` in the project's KB repo.

    Args:
        postgres_db: App-DB facade, passed straight to ``resolve_kb_repo``.
        gitea_client: ``GiteaClient`` (or any object with ``list_tree`` /
            ``change_files``).
        project_id: Project whose KB repo receives the note.
        slug: Note id — the file stem the reindexer matches rows on.
        content: Fully rendered OKF markdown. Rendering stays with the caller;
            the orchestrator does not know OKF's note format.
        job_id: Writing job, for per-job commit attribution (§3). Optional —
            persistent sessions have no job.

    Returns:
        ``{status, reason, repo, branch, path, operation}``. ``status`` is one
        of:

        * ``committed`` — the note is on the branch as of this call.
        * ``skipped`` — nothing was written and nothing needed to be:
          ``no-repo`` (the project has no Gitea repo at all — the equivalent
          of today's ``has_git()`` skip) or ``unchanged`` (the identical bytes
          are already committed; writing again would add a no-op commit to the
          very history §3 wants to be readable).
        * ``failed`` — the note is NOT materialised. ``reason`` is one of
          ``invalid-slug``, ``empty-content``, ``resolve-error``,
          ``commit-refused``, ``commit-error``.

        Never raises. A ``failed`` result is the caller's cue to log and carry
        on: the ``knowledge_index`` row is already written, and rewriting the
        note re-attempts materialisation (§10 — the reindexer is idempotent,
        so recovery is a rewrite, not repair tooling).
    """
    bad_slug = slug_error(slug)
    if bad_slug:
        logger.error(
            "kb-materialize: refusing note for project %s — %s", project_id, bad_slug
        )
        return _result(_STATUS_FAILED, reason="invalid-slug")

    slug = str(slug).strip()
    path = note_repo_path(slug)

    if not str(content or "").strip():
        logger.error(
            "kb-materialize: refusing empty body for %s (project %s)", path, project_id
        )
        return _result(_STATUS_FAILED, reason="empty-content", path=path)

    try:
        resolved = await resolve_kb_repo(postgres_db, project_id)
    except Exception as e:  # noqa: BLE001 — resolution is not the caller's problem
        logger.error(
            "kb-materialize: KB repo resolution failed for project %s: %r",
            project_id,
            e,
            exc_info=True,
        )
        return _result(_STATUS_FAILED, reason="resolve-error", path=path)

    if not resolved:
        # No jobs repo and no knowledge repo — a repo-less project. The old
        # write path skipped these on has_git(); so do we, cleanly.
        logger.debug(
            "kb-materialize: project %s has no KB repo — skipping %s",
            project_id,
            path,
        )
        return _result(_STATUS_SKIPPED, reason="no-repo", path=path)

    repo_name, branch = resolved.repo, resolved.branch
    branch = branch or "main"
    body = str(content).encode("utf-8")

    try:
        repo_client = await kb_client_for_repo(postgres_db, gitea_client, resolved)
    except Exception:  # noqa: BLE001 — credential/config failures stay non-fatal
        logger.error(
            "kb-materialize: no usable %s client for project %s",
            resolved.forge,
            project_id,
            exc_info=True,
        )
        return _result(
            _STATUS_FAILED,
            reason="client-error",
            repo=repo_name,
            branch=branch,
            path=path,
        )

    exists, blob_sha = await _path_exists(repo_client, repo_name, branch, path)
    if exists and blob_sha and blob_sha == _git_blob_sha(body):
        logger.debug(
            "kb-materialize: %s@%s already holds %s byte-for-byte — no commit",
            repo_name,
            branch,
            path,
        )
        return _result(
            _STATUS_SKIPPED,
            reason="unchanged",
            repo=repo_name,
            branch=branch,
            path=path,
        )

    # ``create`` on an existing path is a 422 and ``update`` on a missing one
    # is refused too, so the operation is chosen per file against the target
    # tree — the curated merge's rule (services.project_loops). Two cases the
    # tree cannot settle: it was unreadable (exists is None), or a concurrent
    # writer changed the path between the probe and the commit. Both surface
    # identically as a refusal, so a refusal is retried ONCE with the opposite
    # operation. §10: a missed materialisation leaves the note invisible to
    # every reader, which is worth one extra call to avoid.
    operation = "update" if exists else "create"
    message = _commit_message(slug, job_id)
    payload = {"path": path, "content_b64": base64.b64encode(body).decode("ascii")}
    if blob_sha:
        # GitHub's contents API requires the current blob SHA for an update.
        # GiteaClient deliberately ignores this extra duck-typed field.
        payload["sha"] = blob_sha

    for attempt, op in enumerate(
        (operation, "create" if operation == "update" else "update")
    ):
        try:
            committed = await repo_client.change_files(
                repo_name, branch, [{**payload, "operation": op}], message=message
            )
        except Exception as e:  # noqa: BLE001 — non-fatal by contract
            logger.error(
                "kb-materialize: commit of %s to %s@%s raised: %r",
                path,
                repo_name,
                branch,
                e,
                exc_info=True,
            )
            return _result(
                _STATUS_FAILED,
                reason="commit-error",
                repo=repo_name,
                branch=branch,
                path=path,
                operation=op,
            )
        if committed:
            if attempt:
                logger.info(
                    "kb-materialize: %s landed on the '%s' retry (tree said %s)",
                    path,
                    op,
                    exists,
                )
            logger.info(
                "kb-materialize: %s → %s@%s (%s%s)",
                path,
                repo_name,
                branch,
                op,
                f", job {str(job_id)[:8]}" if job_id else "",
            )
            return _result(
                _STATUS_COMMITTED,
                repo=repo_name,
                branch=branch,
                path=path,
                operation=op,
            )

    logger.error(
        "kb-materialize: Gitea refused %s on %s@%s as both '%s' and its "
        "opposite — the note is in the index but NOT readable until it is "
        "rewritten",
        path,
        repo_name,
        branch,
        operation,
    )
    return _result(
        _STATUS_FAILED,
        reason="commit-refused",
        repo=repo_name,
        branch=branch,
        path=path,
        operation=operation,
    )
