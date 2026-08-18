"""Server-side materialisation of ONE knowledge note into a project's KB repo.

Step 3 of ``knowledge-base/knowledge/features/knowledge_base_repo_separation.md``. The agent used
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

By contract this **never raises for an expected condition**. Every outcome —
including "there is no repo" and "Gitea refused" — comes back as a status dict,
but mutation callers must fail closed unless that result proves the desired
bytes crossed the canonical repository boundary. The durable intent ledger
keeps a failed write visible and retryable instead of allowing an index-only
success.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

import yaml

from services.kb_forge import kb_client_for_repo
from src.tools.knowledge.gardener import parse_note_md

# The vault prefix and the repo resolution are both owned by the reindexer:
# the writer and the sweep MUST agree on repo and path or they silently
# diverge (that is the shape of kb_reindex_watermark_never_advances).
from services.kb_reindex import (
    KNOWLEDGE_PREFIX,
    index_single_note,
    resolve_kb_repo,
)
from src.tools.knowledge.chunker import embedding_version_for_service

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

# Chunks a note may have and still be indexed inside the write request. Over
# it the note commits and the sweep indexes it: a 127 KB curator dump chunks
# into ~95, and the embedding backend caps a batch at 64 inputs, so inlining
# one would put several round-trips in the caller's request.
_INLINE_MAX_CHUNKS = int(os.getenv("KB_INLINE_INDEX_MAX_CHUNKS", "8"))


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


async def _materialize_knowledge_note_once(
    *,
    postgres_db: Any,
    gitea_client: Any,
    project_id: str,
    slug: str,
    content: str,
    job_id: Optional[str] = None,
    resolved_repo: Any = None,
    repo_client: Any = None,
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

        Never raises. A ``failed`` result means canonical durability was not
        established; callers must not mutate the searchable projection or
        report READY/updated/closed. The enclosing durable intent supplies the
        retry path.
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

    resolved = resolved_repo
    if resolved is None:
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

    if repo_client is None:
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


async def _finish_attempt(
    *,
    postgres_db: Any,
    intent: dict[str, Any],
    outcome: dict[str, Any],
    permanent: bool,
) -> dict[str, Any]:
    canonical = outcome.get("status") == _STATUS_COMMITTED or (
        outcome.get("status") == _STATUS_SKIPPED
        and outcome.get("reason") == "unchanged"
    )
    intent_id = str(intent["id"])
    token = intent.get("attempt_token")
    try:
        recorded = await postgres_db.finish_knowledge_materialization(
            intent_id,
            canonical=canonical,
            permanent=permanent,
            reason=str(outcome.get("reason") or "") or None,
            error=str(outcome.get("reason") or "") or None,
            repo=outcome.get("repo"),
            branch=outcome.get("branch"),
            path=outcome.get("path"),
            operation=outcome.get("operation"),
            attempt_token=str(token) if token else None,
        )
    except Exception as exc:
        logger.error(
            "kb-materialize: result ledger update failed for intent %s: %r",
            intent_id,
            exc,
            exc_info=True,
        )
        return {
            **outcome,
            "status": _STATUS_FAILED,
            "reason": "intent-result-store-error",
            "intent_id": intent_id,
            "canonical_state": "pending_sync",
            "projection_state": "projection_only",
            "retry_state": "retryable",
        }
    if recorded is None:
        return {
            **outcome,
            "status": _STATUS_FAILED,
            "reason": "intent-lease-lost",
            "intent_id": intent_id,
            "canonical_state": "pending_sync",
            "projection_state": "pending",
            "retry_state": "retryable",
        }
    return {
        **outcome,
        "intent_id": intent_id,
        "canonical_state": recorded.get("canonical_state")
        or ("canonical" if canonical else "pending_sync"),
        "projection_state": recorded.get("projection_state") or "pending",
        "retry_state": recorded.get("retry_state")
        or ("none" if canonical else "retryable"),
    }


async def _attempt_content_intent(
    *,
    postgres_db: Any,
    gitea_client: Any,
    intent: dict[str, Any],
    project_id: str,
    slug: str,
    content: str,
    job_id: Optional[str],
) -> dict[str, Any]:
    permanent_reason: str | None = None
    if slug_error(slug):
        permanent_reason = "invalid-slug"
    elif not content.strip():
        permanent_reason = "empty-content"
    else:
        try:
            frontmatter, _ = parse_note_md(content)
            if frontmatter is None:
                permanent_reason = "malformed-frontmatter"
            elif str(frontmatter.get("id") or "") != str(slug).strip():
                permanent_reason = "frontmatter-id-mismatch"
        except ValueError:
            permanent_reason = "malformed-frontmatter"

    if permanent_reason is not None:
        outcome = _result(_STATUS_FAILED, reason=permanent_reason)
    else:
        try:
            outcome = await _materialize_knowledge_note_once(
                postgres_db=postgres_db,
                gitea_client=gitea_client,
                project_id=project_id,
                slug=slug,
                content=content,
                job_id=job_id,
            )
        except Exception as exc:
            logger.error(
                "kb-materialize: unexpected materializer exception for %s/%s: %r",
                project_id,
                slug,
                exc,
                exc_info=True,
            )
            outcome = _result(_STATUS_FAILED, reason="materializer-exception")
    permanent = permanent_reason is not None or outcome.get("reason") in {
        "invalid-slug",
        "empty-content",
    }
    return await _finish_attempt(
        postgres_db=postgres_db,
        intent=intent,
        outcome=outcome,
        permanent=permanent,
    )


def _is_canonical(result: dict[str, Any]) -> bool:
    """Whether the bytes are proven to be on the canonical branch."""
    if result.get("canonical_state") == "canonical":
        return True
    return result.get("status") == _STATUS_COMMITTED or (
        result.get("status") == _STATUS_SKIPPED
        and result.get("reason") in {"unchanged", "already-canonical"}
    )


async def _index_note_inline(
    *,
    postgres_db: Any,
    store: Any,
    embedding_service: Any,
    project_id: str,
    slug: str,
    content: str,
    intent_id: Optional[str],
) -> dict[str, Any]:
    """Index a just-committed note so it is searchable when the write returns.

    Slice A. Without this the row is pathless and chunkless until the next
    sweep, which is post-job or up to 900s away — so an agent could not read
    back the note it had just written.

    Deliberately narrow: one note, no deletes, no reconcile, and never a
    watermark write. The blob SHA stamped here is the one we just committed,
    so the next sweep's tree diff skips this note instead of re-embedding it.

    Never raises. Every failure degrades to "committed but not yet indexed",
    which is exactly the state the sweep already knows how to heal.
    """
    path = note_repo_path(slug)
    try:
        kb_id = uuid.UUID(str(project_id))
    except (TypeError, ValueError):
        return {"indexed": False, "index_reason": "index-error"}

    blob_sha = _git_blob_sha(str(content).encode("utf-8"))
    try:
        embedding_stamp = embedding_version_for_service(embedding_service)
        # Non-blocking: if the sweep owns this KB we defer rather than hold a
        # request open behind a full-vault rebuild. The sweep will index the
        # note in the pass that is already running.
        async with store.try_reindex_lock(kb_id) as claimed:
            if not claimed:
                return {"indexed": False, "index_reason": "reindex-running"}
            outcome = await index_single_note(
                store=store,
                embedding_service=embedding_service,
                kb_id=kb_id,
                path=path,
                text=content,
                blob_sha=blob_sha,
                embedding_stamp=embedding_stamp,
                max_chunks=_INLINE_MAX_CHUNKS,
            )
    except Exception as e:  # noqa: BLE001 — non-fatal by contract
        logger.error(
            "kb-materialize: inline index of %s failed: %r — committed but not "
            "searchable until the next sweep",
            path,
            e,
            exc_info=True,
        )
        return {"indexed": False, "index_reason": "index-error"}

    if outcome.status != "indexed":
        logger.warning(
            "kb-materialize: %s committed but not indexed inline (%s, %d chunks)",
            path,
            outcome.status,
            outcome.chunks,
        )
        return {"indexed": False, "index_reason": outcome.status}

    if intent_id:
        try:
            await postgres_db.finish_knowledge_projection(
                intent_id, project_id=str(project_id), synced=True, error=None
            )
        except Exception as e:  # noqa: BLE001 — canonical truth already persisted
            logger.warning(
                "kb-materialize: projection ledger close failed for intent %s: %r",
                intent_id,
                e,
            )
    return {"indexed": True, "index_reason": None}


async def _materialize_note_canonical(
    *,
    postgres_db: Any,
    gitea_client: Any,
    project_id: str,
    slug: str,
    content: str,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Persist intent, cross canonical git, and return both truth legs."""
    content_text = str(content or "")
    content_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
    try:
        intent = await postgres_db.begin_knowledge_materialization(
            project_id=project_id,
            note_id=str(slug),
            content=content_text,
            content_hash=content_hash,
            job_id=job_id,
        )
    except Exception as exc:
        logger.error(
            "kb-materialize: could not persist retry intent for %s/%s: %r",
            project_id,
            slug,
            exc,
            exc_info=True,
        )
        return {
            **_result(_STATUS_FAILED, reason="intent-store-error"),
            "canonical_state": "failed",
            "projection_state": "projection_only",
            "retry_state": "retryable",
        }
    intent_id = str(intent["id"])
    if str(intent.get("canonical_state")) == "canonical":
        return {
            **_result(
                _STATUS_SKIPPED,
                reason="already-canonical",
                repo=intent.get("repo"),
                branch=intent.get("branch"),
                path=intent.get("path"),
                operation=intent.get("operation"),
            ),
            "intent_id": intent_id,
            "canonical_state": "canonical",
            "projection_state": intent.get("projection_state") or "pending",
            "retry_state": "none",
        }
    if not intent.get("attempt_claimed"):
        return {
            **_result(_STATUS_SKIPPED, reason="attempt-in-progress"),
            "intent_id": intent_id,
            "canonical_state": intent.get("canonical_state") or "pending_sync",
            "projection_state": intent.get("projection_state") or "pending",
            "retry_state": intent.get("retry_state") or "retryable",
        }
    return await _attempt_content_intent(
        postgres_db=postgres_db,
        gitea_client=gitea_client,
        intent=intent,
        project_id=project_id,
        slug=slug,
        content=content_text,
        job_id=job_id,
    )


async def materialize_knowledge_note(
    *,
    postgres_db: Any,
    gitea_client: Any,
    project_id: str,
    slug: str,
    content: str,
    job_id: Optional[str] = None,
    store: Any = None,
    embedding_service: Any = None,
) -> dict[str, Any]:
    """Commit one note and, when an indexer is supplied, index it inline.

    Two legs, in this order and no other: the commit is the write, the index
    is derived from it. A note is never indexed unless the bytes are proven
    canonical, so the searchable projection can never claim something git does
    not have.

    ``store``/``embedding_service`` are optional so the service stays testable
    without a vector DB, and so a deployment with no resolvable embedding
    service degrades to the pre-Slice-A behaviour (commit now, sweep indexes
    later) instead of failing the write.
    """
    result = await _materialize_note_canonical(
        postgres_db=postgres_db,
        gitea_client=gitea_client,
        project_id=project_id,
        slug=slug,
        content=content,
        job_id=job_id,
    )
    if not _is_canonical(result):
        return {**result, "indexed": False, "index_reason": "not-canonical"}
    if store is None or embedding_service is None:
        return {**result, "indexed": False, "index_reason": "no-indexer"}
    index_state = await _index_note_inline(
        postgres_db=postgres_db,
        store=store,
        embedding_service=embedding_service,
        project_id=project_id,
        slug=slug,
        content=str(content or ""),
        intent_id=result.get("intent_id"),
    )
    return {**result, **index_state}


def metadata_mutation_content(
    *,
    status: str | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "status": status,
            "add_tags": [str(tag) for tag in (add_tags or [])],
            "remove_tags": [str(tag) for tag in (remove_tags or [])],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_metadata_fields(content: str, slug: str) -> dict[str, Any]:
    """Return the exact projection metadata encoded in canonical markdown.

    Metadata mutation callers must project what Git contains, not reconstruct
    it from the request that happened to create an intent.  ``ready_at`` is
    normalized here so every projection consumer receives one typed contract
    and no database adapter is asked to infer an ISO string's timestamp type.
    """
    frontmatter, _ = parse_note_md(content)
    if frontmatter is None or str(frontmatter.get("id") or "") != slug:
        raise ValueError("canonical note has invalid frontmatter")

    raw_tags = frontmatter.get("tags") or []
    if not isinstance(raw_tags, list):
        raise ValueError("canonical note tags must be a list")

    raw_ready_at = frontmatter.get("ready_at")
    if raw_ready_at is None:
        ready_at = None
    elif isinstance(raw_ready_at, datetime):
        ready_at = raw_ready_at
    elif isinstance(raw_ready_at, str):
        try:
            ready_at = datetime.fromisoformat(raw_ready_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("canonical ready_at must be an ISO timestamp") from exc
    else:
        raise ValueError("canonical ready_at must be a timestamp")
    if ready_at is not None and ready_at.tzinfo is None:
        ready_at = ready_at.replace(tzinfo=timezone.utc)

    status = frontmatter.get("status")
    return {
        "canonical_status": str(status) if status is not None else None,
        "canonical_tags": [str(tag) for tag in raw_tags],
        "canonical_ready_at": ready_at,
        "canonical_metadata_complete": True,
    }


async def _read_canonical_metadata(
    *,
    postgres_db: Any,
    gitea_client: Any,
    project_id: str,
    slug: str,
) -> dict[str, Any]:
    """Reread canonical truth for an intent another actor already committed.

    A scheduled retry can win between a failed HTTP request and its client
    retry.  The durable intent then proves the mutation crossed Git, but it
    does not contain the complete, current tag set.  Reading the note avoids
    destructive defaults such as ``tags=[]`` or ``ready_at=NULL`` and also
    handles a later canonical mutation honestly.
    """
    try:
        resolved = await resolve_kb_repo(postgres_db, project_id)
        if resolved is None:
            return _result(_STATUS_FAILED, reason="no-repo")
        repo_client = await kb_client_for_repo(postgres_db, gitea_client, resolved)
        from .project_backlog import _read_note_file

        current, _ = await _read_note_file(repo_client, resolved, note_repo_path(slug))
        if current is None:
            return _result(
                _STATUS_FAILED,
                reason="canonical-file-missing",
                repo=resolved.repo,
                branch=resolved.branch,
                path=note_repo_path(slug),
                operation="metadata-update",
            )
        metadata = _canonical_metadata_fields(current, slug)
        return {
            **_result(
                _STATUS_SKIPPED,
                reason="already-canonical",
                repo=resolved.repo,
                branch=resolved.branch,
                path=note_repo_path(slug),
                operation="metadata-update",
            ),
            **metadata,
        }
    except ValueError:
        return _result(_STATUS_FAILED, reason="malformed-frontmatter")
    except Exception as exc:
        logger.error(
            "kb-materialize: canonical metadata reread failed for %s/%s: %r",
            project_id,
            slug,
            exc,
            exc_info=True,
        )
        return _result(_STATUS_FAILED, reason="canonical-read-error")


async def _attempt_metadata_intent(
    *,
    postgres_db: Any,
    gitea_client: Any,
    intent: dict[str, Any],
    project_id: str,
    slug: str,
    mutation: dict[str, Any],
) -> dict[str, Any]:
    status = mutation.get("status")
    add_tags = mutation.get("add_tags") or []
    remove_tags = mutation.get("remove_tags") or []
    permanent = False
    rendered: str | None = None
    try:
        resolved = await resolve_kb_repo(postgres_db, project_id)
        if resolved is None:
            outcome = _result(_STATUS_FAILED, reason="no-repo")
        else:
            repo_client = await kb_client_for_repo(postgres_db, gitea_client, resolved)
            from .project_backlog import (
                _ALREADY_SET,
                _NOT_REWRITABLE,
                _read_note_file,
                _rewrite_status,
            )

            current, _ = await _read_note_file(
                repo_client, resolved, note_repo_path(slug)
            )
            if current is None:
                outcome = _result(_STATUS_FAILED, reason="canonical-file-missing")
                permanent = True
            else:
                frontmatter, body = parse_note_md(current)
                if frontmatter is None or str(frontmatter.get("id") or "") != slug:
                    outcome = _result(_STATUS_FAILED, reason="malformed-frontmatter")
                    permanent = True
                else:
                    if status is not None and not add_tags and not remove_tags:
                        rendered, rewrite = _rewrite_status(current, str(status))
                        if rewrite == _NOT_REWRITABLE:
                            outcome = _result(
                                _STATUS_FAILED, reason="malformed-frontmatter"
                            )
                            permanent = True
                            rendered = current
                        elif rewrite == _ALREADY_SET:
                            outcome = _result(
                                _STATUS_SKIPPED,
                                reason="unchanged",
                                repo=resolved.repo,
                                branch=resolved.branch,
                                path=note_repo_path(slug),
                                operation="metadata-update",
                            )
                            rendered = current
                        else:
                            outcome = {}
                    else:
                        desired = dict(frontmatter)
                        if status is not None:
                            desired["status"] = status
                        tags = [str(tag) for tag in (desired.get("tags") or [])]
                        remove = {str(tag).strip().lower() for tag in remove_tags}
                        tags = [
                            tag for tag in tags if tag.strip().lower() not in remove
                        ]
                        existing = {tag.strip().lower() for tag in tags}
                        for tag in add_tags:
                            normalized = str(tag).strip().lower()
                            if normalized and normalized not in existing:
                                tags.append(normalized)
                                existing.add(normalized)
                        desired["tags"] = tags
                        prior_tags = {
                            str(tag).strip().lower()
                            for tag in (frontmatter.get("tags") or [])
                        }
                        if "ready" in existing and "ready" not in prior_tags:
                            desired["ready_at"] = datetime.now(timezone.utc).isoformat()
                        elif "ready" not in existing:
                            desired.pop("ready_at", None)
                        rendered = (
                            "---\n"
                            + yaml.safe_dump(
                                desired, sort_keys=False, allow_unicode=True
                            ).rstrip()
                            + "\n---\n"
                            + body
                        )
                        outcome = {}
                    if not outcome:
                        outcome = await _materialize_knowledge_note_once(
                            postgres_db=postgres_db,
                            gitea_client=gitea_client,
                            project_id=project_id,
                            slug=slug,
                            content=rendered,
                            resolved_repo=resolved,
                            repo_client=repo_client,
                        )
    except ValueError:
        outcome = _result(_STATUS_FAILED, reason="malformed-frontmatter")
        permanent = True
    except Exception as exc:
        logger.error(
            "kb-materialize: canonical metadata mutation failed for %s/%s: %r",
            project_id,
            slug,
            exc,
            exc_info=True,
        )
        outcome = _result(_STATUS_FAILED, reason="canonical-read-error")
    if rendered and (
        outcome.get("status") == _STATUS_COMMITTED
        or (
            outcome.get("status") == _STATUS_SKIPPED
            and outcome.get("reason") == "unchanged"
        )
    ):
        try:
            outcome.update(_canonical_metadata_fields(rendered, slug))
        except ValueError:
            # The Git boundary may already have been crossed.  Preserve that
            # canonical truth in the ledger, but never let a caller project
            # guessed metadata from an incomplete result.
            outcome["canonical_metadata_complete"] = False
    return await _finish_attempt(
        postgres_db=postgres_db,
        intent=intent,
        outcome=outcome,
        permanent=permanent,
    )


async def materialize_knowledge_metadata_update(
    *,
    postgres_db: Any,
    gitea_client: Any,
    project_id: str,
    slug: str,
    status: str | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
) -> dict[str, Any]:
    payload = metadata_mutation_content(
        status=status, add_tags=add_tags, remove_tags=remove_tags
    )
    content_hash = "metadata:" + hashlib.sha256(payload.encode()).hexdigest()
    try:
        intent = await postgres_db.begin_knowledge_materialization(
            project_id=project_id,
            note_id=slug,
            content=payload,
            content_hash=content_hash,
            operation="metadata-update",
        )
    except Exception as exc:
        logger.error(
            "kb-materialize: metadata retry intent failed for %s/%s: %r",
            project_id,
            slug,
            exc,
            exc_info=True,
        )
        return {
            **_result(_STATUS_FAILED, reason="intent-store-error"),
            "canonical_state": "failed",
            "projection_state": "projection_only",
            "retry_state": "retryable",
        }
    if str(intent.get("canonical_state")) == "canonical":
        canonical = await _read_canonical_metadata(
            postgres_db=postgres_db,
            gitea_client=gitea_client,
            project_id=project_id,
            slug=slug,
        )
        return {
            **canonical,
            "intent_id": str(intent["id"]),
            "canonical_state": "canonical",
            "projection_state": intent.get("projection_state") or "pending",
            "retry_state": "none",
        }
    if not intent.get("attempt_claimed"):
        return {
            **_result(_STATUS_SKIPPED, reason="attempt-in-progress"),
            "intent_id": str(intent["id"]),
            "canonical_state": intent.get("canonical_state") or "pending_sync",
            "projection_state": intent.get("projection_state") or "pending",
            "retry_state": intent.get("retry_state") or "retryable",
        }
    return await _attempt_metadata_intent(
        postgres_db=postgres_db,
        gitea_client=gitea_client,
        intent=intent,
        project_id=project_id,
        slug=slug,
        mutation=json.loads(payload),
    )


async def retry_knowledge_materialization_intent(
    *, postgres_db: Any, gitea_client: Any, intent: dict[str, Any]
) -> dict[str, Any]:
    operation = str(intent.get("operation") or "")
    if operation == "metadata-update":
        try:
            mutation = json.loads(str(intent.get("content") or "{}"))
        except (TypeError, ValueError):
            mutation = {}
        return await _attempt_metadata_intent(
            postgres_db=postgres_db,
            gitea_client=gitea_client,
            intent=intent,
            project_id=str(intent["project_id"]),
            slug=str(intent["note_id"]),
            mutation=mutation,
        )
    return await _attempt_content_intent(
        postgres_db=postgres_db,
        gitea_client=gitea_client,
        intent=intent,
        project_id=str(intent["project_id"]),
        slug=str(intent["note_id"]),
        content=str(intent["content"]),
        job_id=str(intent["job_id"]) if intent.get("job_id") else None,
    )
