"""Mode A baseline seeding + diff capture for the job cloud workflow.

See ``docs/done/job_cloud_export.md`` §3 for the design. This module
owns the cloud→Gitea seed at job-start and the Gitea-diff capture at
job-completion. Both run from the orchestrator side; the agent stays
unchanged (it picks the seeded ``projects/<slug>/`` up via its existing
clone-on-start of the job's Gitea repo).

The seed is async via ``asyncio.create_task`` from the job-create
handler. While it runs, the job carries
``context.cloud_baseline = {state: 'seeding'}``; the dispatcher gate
in ``get_dispatchable_jobs`` skips jobs in that state so the agent
never starts on an incomplete baseline.

v1 limitations (deferred to v2):

* Text files only — anything that isn't UTF-8-decodable is logged and
  skipped from the baseline. Binary files in the project folder will
  not appear in the diff. Acceptable for the thesis/document workflows
  this feature primarily targets; revisit when first user hits it.
* OpenCloud-only — Nextcloud's ``list_project_folder`` /
  ``get_project_folder_file_bytes`` are stubs that raise
  ``NOT_SUPPORTED``. Adding Group Folders PROPFIND is a clean follow-up.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .cloud import (
    CloudBackendError,
    MainCloudRouter,
    ProjectFolderEntry,
    ProjectFolderHandle,
)

logger = logging.getLogger(__name__)


def _job_context(job: dict[str, Any]) -> dict[str, Any]:
    """Return ``job['context']`` as a dict.

    asyncpg returns JSONB columns as Python strings (no codec is
    registered project-wide), so callers manually ``json.loads`` it.
    Mirrors the pattern in ``orchestrator/main.py`` everywhere ``context``
    is read.
    """
    raw = job.get("context")
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return raw if isinstance(raw, dict) else {}


def slugify_project_name(name: str) -> str:
    """Same algorithm as ``main._slugify_mount_name`` — kept here to
    avoid the circular import. Workspace-safe slug for a project name.
    """
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    return out or "project"


_BASELINE_COMMIT_MESSAGE = (
    "Mode A baseline: seed project folder for job {job_short}\n\n"
    "Seeded by orchestrator from project '{project_name}' cloud folder. "
    "This is the diff baseline; agent edits on top become the diff at "
    "job completion. See docs/done/job_cloud_export.md."
)


async def seed_project_folder_baseline(
    *,
    job_id: str,
    project: dict[str, Any],
    repo_name: str,
    branch: str | None,
    postgres_db: Any,
    gitea_client: Any,
    main_cloud_router: MainCloudRouter,
) -> None:
    """Walk the project's cloud folder, push files into Gitea, stamp baseline.

    Sets ``jobs.cloud_diff_baseline_commit`` on success.
    Updates ``jobs.context.cloud_baseline.state`` to ``'seeding'`` while
    running, then ``'ready'`` or ``'failed'`` at termination. The
    dispatcher gate watches that field.

    Idempotency: if ``cloud_diff_baseline_commit`` is already set, this
    is a no-op (already seeded). Safe to invoke twice (e.g. on a retry).
    """
    job_short = job_id[:8]
    project_name = str(project.get("name") or "project")
    slug = slugify_project_name(project_name)
    target_subpath = f"projects/{slug}"

    async def _set_state(
        state: str,
        *,
        error: str | None = None,
        entries: dict[str, str] | None = None,
    ) -> None:
        """Merge ``cloud_baseline`` state at the top level. The merge is
        a JSONB ``||`` so we replace the whole ``cloud_baseline`` object
        each call; pass ``entries`` to persist the path→etag map.
        """
        payload: dict[str, Any] = {"state": state}
        if error:
            payload["error"] = error
        if entries is not None:
            payload["entries"] = entries
        try:
            await postgres_db.merge_job_context(job_id, {"cloud_baseline": payload})
        except Exception:
            logger.exception(
                "Mode A: failed to update baseline state for job %s", job_id
            )

    try:
        # Idempotency: a re-run after a successful seed should no-op.
        existing = await postgres_db.get_job(job_id)
        if existing and existing.get("cloud_diff_baseline_commit"):
            await _set_state("ready")
            return

        await _set_state("seeding")

        # Resolve the cloud handle.
        handle_db = project.get("main_cloud_folder_handle")
        backend_id = project.get("main_cloud_backend")
        if not handle_db or not backend_id:
            logger.info(
                "Mode A: job %s — project %s has no cloud folder; "
                "skipping baseline seed (will run as loose job).",
                job_short,
                project_name,
            )
            await _set_state("ready")
            return

        try:
            backend = main_cloud_router.for_backend(backend_id)
        except Exception as e:
            logger.warning(
                "Mode A: job %s — backend %s not available; skipping seed (%s)",
                job_short,
                backend_id,
                e,
            )
            await _set_state("failed", error=f"backend unavailable: {e}")
            return

        if not getattr(backend, "is_initialized", False):
            logger.warning(
                "Mode A: job %s — backend %s not initialized; skipping seed",
                job_short,
                backend_id,
            )
            await _set_state("failed", error="backend not initialized")
            return

        handle = ProjectFolderHandle.from_db(str(handle_db), backend=backend_id)

        # 1. Enumerate everything under the project folder.
        try:
            entries: list[ProjectFolderEntry] = await backend.list_project_folder(
                handle
            )
        except CloudBackendError as e:
            logger.warning(
                "Mode A: job %s — list_project_folder failed (%s); skipping seed",
                job_short,
                e,
            )
            await _set_state("failed", error=f"list_project_folder: {e}")
            return

        # 2. Filter to text files only. Walk dirs are no-ops in git.
        files = [e for e in entries if not e.is_dir]
        # Path → etag map of every file we observed in the cloud at seed
        # time. Persisted into context so the accept-time external-mod
        # check can compare against it. We capture ALL files here, not
        # just the UTF-8 ones we seed into Gitea — a binary file the
        # agent never sees can still be evidence of an external edit.
        entries_map: dict[str, str] = {e.path: e.etag for e in files}
        if not files:
            logger.info(
                "Mode A: job %s — project folder is empty; no baseline files",
                job_short,
            )
            # Still record a baseline commit hash so completion-time diff
            # has something to diff against (the empty tree).
            # We do this by capturing the current HEAD of the job branch.
            head_sha = await _read_head_commit(gitea_client, repo_name, branch)
            if head_sha:
                await postgres_db.update_job_cloud_diff(
                    job_id, baseline_commit=head_sha
                )
            await _set_state("ready", entries=entries_map)
            return

        seeded = 0
        skipped_binary = 0
        for entry in files:
            try:
                blob = await backend.get_project_folder_file_bytes(
                    handle, path=entry.path
                )
            except CloudBackendError as e:
                logger.warning(
                    "Mode A: job %s — failed to fetch %s (%s); skipping",
                    job_short,
                    entry.path,
                    e,
                )
                continue
            # v1: text files only. Skip anything that isn't valid UTF-8.
            try:
                content_text = blob.decode("utf-8")
            except UnicodeDecodeError:
                skipped_binary += 1
                logger.debug(
                    "Mode A: job %s — skipping binary %s (%d bytes)",
                    job_short,
                    entry.path,
                    len(blob),
                )
                continue
            gitea_path = f"{target_subpath}/{entry.path}"
            ok = await gitea_client.create_or_update_file(
                repo_name,
                gitea_path,
                content_text,
                _BASELINE_COMMIT_MESSAGE.format(
                    job_short=job_short, project_name=project_name
                ),
                branch=branch,
            )
            if ok:
                seeded += 1
            else:
                logger.warning(
                    "Mode A: job %s — gitea write failed for %s",
                    job_short,
                    gitea_path,
                )

        # 3. Capture the head of the branch as the baseline commit.
        baseline_sha = await _read_head_commit(gitea_client, repo_name, branch)
        if not baseline_sha:
            logger.warning(
                "Mode A: job %s — could not read HEAD after seeding; "
                "baseline commit hash not recorded",
                job_short,
            )
            await _set_state("failed", error="could not read HEAD after seed")
            return

        await postgres_db.update_job_cloud_diff(job_id, baseline_commit=baseline_sha)
        logger.info(
            "Mode A: job %s — seeded %d file(s), skipped %d binary, baseline=%s",
            job_short,
            seeded,
            skipped_binary,
            baseline_sha[:8],
        )
        await _set_state("ready", entries=entries_map)
    except Exception as e:
        logger.exception(
            "Mode A: job %s — baseline seed unexpectedly failed", job_short
        )
        await _set_state("failed", error=str(e))


async def _read_head_commit(
    gitea_client: Any, repo_name: str, branch: str | None
) -> str | None:
    """Return the SHA of HEAD on ``branch`` (or ``main`` if ``None``).

    Uses Gitea's ``GET /branches/{branch}`` endpoint, which reliably
    returns the head commit regardless of branch name (including names
    containing ``/`` like ``job/abc123``). ``get_commits`` would NOT
    work here — its ``git/commits`` endpoint is for specific commit
    SHAs, not branch lookups, and returns 404 for branch names.
    """
    return await gitea_client.get_branch_head_sha(repo_name, branch or "main")


def fire_baseline_seed(
    *,
    job_id: str,
    project: dict[str, Any],
    repo_name: str,
    branch: str | None,
    postgres_db: Any,
    gitea_client: Any,
    main_cloud_router: MainCloudRouter,
) -> asyncio.Task:
    """Fire-and-forget wrapper around :func:`seed_project_folder_baseline`.

    Used from the job-create endpoint where we don't want to block the
    user's request while the cloud folder walk runs. Returns the task
    so the caller can attach error logging or await in tests.
    """
    return asyncio.create_task(
        seed_project_folder_baseline(
            job_id=job_id,
            project=project,
            repo_name=repo_name,
            branch=branch,
            postgres_db=postgres_db,
            gitea_client=gitea_client,
            main_cloud_router=main_cloud_router,
        )
    )


async def _diff_files_by_tree(
    *,
    gitea_client: Any,
    repo_name: str,
    baseline: str,
    head: str,
) -> list[dict[str, str]] | None:
    """Compute the file-level Mode A diff via Gitea tree comparison.

    Gitea 1.22's ``compare/{base}...{head}.diff`` returns 404
    ``BaseNotExist`` for raw commit SHAs (only branches/tags work
    there — see gitea#19797 + linked issues). Tree comparison gives us
    the same triage cheaply: list ``git/trees/{sha}?recursive=true`` at
    both ends, build ``{path: blob_sha}`` maps, set-diff. Result is
    scoped to ``projects/<slug>/*`` — anything outside isn't the
    agent's project-folder edit.

    Returns:
        List of ``{path, status}`` (``added`` / ``modified`` /
        ``deleted``), already filtered to ``projects/`` paths. ``None``
        if a tree fetch failed.
    """
    base_tree = await gitea_client.list_tree(repo_name, baseline)
    head_tree = await gitea_client.list_tree(repo_name, head)
    if base_tree is None or head_tree is None:
        return None

    def _blobs(tree: list[dict[str, str]]) -> dict[str, str]:
        return {
            str(e.get("path", "")): str(e.get("sha", ""))
            for e in tree
            if e.get("type") == "blob" and e.get("path")
        }

    base_blobs = _blobs(base_tree)
    head_blobs = _blobs(head_tree)
    files: list[dict[str, str]] = []
    for path in sorted(set(base_blobs.keys()) | set(head_blobs.keys())):
        if not path.startswith("projects/"):
            continue
        in_base = path in base_blobs
        in_head = path in head_blobs
        if in_base and in_head:
            if base_blobs[path] != head_blobs[path]:
                files.append({"path": path, "status": "modified"})
        elif in_head:
            files.append({"path": path, "status": "added"})
        else:
            files.append({"path": path, "status": "deleted"})
    return files


async def capture_diff_for_mode_a_job(
    *,
    job: dict[str, Any],
    postgres_db: Any,
    gitea_client: Any,
) -> bool:
    """At job completion, compute the Mode A diff.

    Reads the job's ``cloud_diff_baseline_commit`` and the current HEAD
    of its branch, asks Gitea for tree listings at both ends, and
    decides whether the job should land in ``pending_review`` (any
    file under ``projects/`` changed) or proceed to ``completed``
    (empty diff or not a Mode A job).

    Returns ``True`` if a non-empty diff was captured and
    ``jobs.diff_status`` was set to ``'pending'``; ``False`` otherwise.
    The caller is responsible for the status transition itself — this
    helper only flips the ``diff_status`` flag, which the caller uses
    to override the new_status returned by ``determine_job_status``.
    """
    job_id = str(job["id"])
    baseline = job.get("cloud_diff_baseline_commit")
    if not baseline:
        return False
    repo_name = job.get("repo_name")
    branch = job.get("branch_name")
    if not repo_name:
        logger.debug(
            "Mode A: job %s has baseline but no repo_name; skipping diff capture",
            job_id,
        )
        return False
    head = await _read_head_commit(gitea_client, repo_name, branch)
    if not head:
        logger.warning("Mode A: job %s — couldn't read HEAD for diff capture", job_id)
        return False
    if head == baseline:
        return False
    files = await _diff_files_by_tree(
        gitea_client=gitea_client,
        repo_name=repo_name,
        baseline=baseline,
        head=head,
    )
    if not files:
        logger.info(
            "Mode A: job %s — no project-folder changes between baseline=%s "
            "and head=%s; skipping diff capture",
            job_id,
            baseline[:8],
            head[:8],
        )
        return False
    await postgres_db.update_job_cloud_diff(job_id, diff_status="pending")
    logger.info(
        "Mode A: job %s — captured %d-file diff baseline=%s head=%s; "
        "status -> pending_review",
        job_id,
        len(files),
        baseline[:8],
        head[:8],
    )
    return True


async def get_diff_summary(
    *,
    job: dict[str, Any],
    gitea_client: Any,
) -> dict[str, Any] | None:
    """Build a file-tree summary of the Mode A diff for a job.

    Returns ``{"baseline_commit": ..., "head_commit": ..., "files":
    [{"path": ..., "status": "added"|"modified"|"deleted"}]}`` or
    ``None`` if the job has no baseline / no repo. Per-file diff
    content is served lazily via the sibling endpoint.
    """
    baseline = job.get("cloud_diff_baseline_commit")
    if not baseline:
        return None
    repo_name = job.get("repo_name")
    branch = job.get("branch_name")
    if not repo_name:
        return None
    head = await _read_head_commit(gitea_client, repo_name, branch)
    if not head or head == baseline:
        return {
            "baseline_commit": baseline,
            "head_commit": head or baseline,
            "files": [],
        }
    files = await _diff_files_by_tree(
        gitea_client=gitea_client,
        repo_name=repo_name,
        baseline=baseline,
        head=head,
    )
    return {
        "baseline_commit": baseline,
        "head_commit": head,
        "files": files or [],
    }


def _strip_project_prefix(gitea_path: str, slug: str) -> str | None:
    """``projects/<slug>/sub/file.md`` → ``sub/file.md``.

    Returns ``None`` if the path is outside the slug — defensive guard
    against a diff that somehow includes paths from a different slug.
    """
    prefix = f"projects/{slug}/"
    if not gitea_path.startswith(prefix):
        return None
    return gitea_path[len(prefix) :]


async def detect_external_mods(
    *,
    job: dict[str, Any],
    project: dict[str, Any],
    main_cloud_router: MainCloudRouter,
) -> list[dict[str, str]]:
    """Check whether the cloud folder has been modified since seed.

    Compares the path→etag map persisted in
    ``context.cloud_baseline.entries`` against a fresh PROPFIND of the
    project folder, scoped to paths the agent's diff actually touches.
    Files the user didn't ask the agent to change are deliberately not
    checked — they can't conflict with the accept apply.

    Returns a list of ``{path, kind}`` dicts (one per diverged path),
    where ``kind`` is ``etag_mismatch`` / ``missing_at_cloud`` /
    ``unexpected_at_cloud``. Empty list means the apply is safe.

    Caller is responsible for fetching the diff summary and threading
    the affected paths in via ``affected_cloud_paths``.
    """
    baseline_entries = _job_context(job).get("cloud_baseline", {}).get("entries") or {}
    handle_db = project.get("main_cloud_folder_handle")
    backend_id = project.get("main_cloud_backend")
    if not handle_db or not backend_id:
        # No cloud folder → can't externally mod. Treat as clean.
        return []
    try:
        backend = main_cloud_router.for_backend(backend_id)
    except Exception:
        return []
    if not getattr(backend, "is_initialized", False):
        # Backend unavailable — caller decides whether that's fatal.
        # For external-mod detection we err on "we can't tell," so
        # return an empty list. The apply call will fail loudly when
        # it can't talk to the backend, which is the right place to
        # surface that.
        return []
    handle = ProjectFolderHandle.from_db(str(handle_db), backend=backend_id)
    try:
        live_entries: list[ProjectFolderEntry] = await backend.list_project_folder(
            handle
        )
    except CloudBackendError:
        # Same logic as above — apply will fail with the real reason.
        return []
    live_map = {e.path: e.etag for e in live_entries if not e.is_dir}
    diverged: list[dict[str, str]] = []
    # 1. Every baseline path that's now missing OR mismatched.
    for path, baseline_etag in baseline_entries.items():
        live_etag = live_map.get(path)
        if live_etag is None:
            diverged.append({"path": path, "kind": "missing_at_cloud"})
        elif live_etag != baseline_etag:
            diverged.append({"path": path, "kind": "etag_mismatch"})
    # 2. Paths that appeared in the cloud since seed.
    for path in live_map.keys():
        if path not in baseline_entries:
            diverged.append({"path": path, "kind": "unexpected_at_cloud"})
    return diverged


async def apply_diff_to_cloud(
    *,
    job: dict[str, Any],
    project: dict[str, Any],
    gitea_client: Any,
    main_cloud_router: MainCloudRouter,
) -> dict[str, Any]:
    """Walk the Mode A diff and write each change back to the cloud.

    For each file in the diff under ``projects/<slug>/``:

    * ``added`` / ``modified``: GET ``new_content`` from Gitea at HEAD
      and PUT to the cloud folder (creating parents as needed).
    * ``deleted``: DELETE from the cloud folder (``if_exists=True``).

    Returns ``{applied: int, deleted: int, errors: [...]}`` — partial
    failures don't abort the walk so the user can see which files made
    it across and which didn't. v2 may want to be transactional, but
    v1 fail-soft matches the "best-effort apply" semantics of WebDAV.
    """
    job_id = str(job["id"])
    job_short = job_id[:8]
    repo_name = job.get("repo_name")
    branch = job.get("branch_name") or "main"
    project_name = str(project.get("name") or "project")
    slug = slugify_project_name(project_name)
    if not repo_name:
        return {"applied": 0, "deleted": 0, "errors": ["job has no repo_name"]}

    handle_db = project.get("main_cloud_folder_handle")
    backend_id = project.get("main_cloud_backend")
    if not handle_db or not backend_id:
        return {
            "applied": 0,
            "deleted": 0,
            "errors": ["project has no cloud folder"],
        }
    try:
        backend = main_cloud_router.for_backend(backend_id)
    except Exception as e:
        return {"applied": 0, "deleted": 0, "errors": [f"backend unavailable: {e}"]}
    if not getattr(backend, "is_initialized", False):
        return {"applied": 0, "deleted": 0, "errors": ["backend not initialized"]}

    handle = ProjectFolderHandle.from_db(str(handle_db), backend=backend_id)
    baseline = job.get("cloud_diff_baseline_commit")
    if not baseline:
        return {"applied": 0, "deleted": 0, "errors": ["job has no baseline commit"]}
    head = await _read_head_commit(gitea_client, repo_name, branch)
    if not head:
        return {"applied": 0, "deleted": 0, "errors": ["could not read HEAD"]}

    files = await _diff_files_by_tree(
        gitea_client=gitea_client,
        repo_name=repo_name,
        baseline=baseline,
        head=head,
    )
    if files is None:
        return {
            "applied": 0,
            "deleted": 0,
            "errors": ["could not read git tree at baseline or head"],
        }
    applied = 0
    deleted = 0
    errors: list[str] = []
    for entry in files:
        gitea_path = entry["path"]
        status = entry["status"]
        rel = _strip_project_prefix(gitea_path, slug)
        if rel is None:
            # Skip paths outside the slug — shouldn't happen but log.
            logger.info(
                "Mode A: job %s — skipping diff path outside slug %r: %s",
                job_short,
                slug,
                gitea_path,
            )
            continue
        try:
            if status == "deleted":
                await backend.delete_project_folder_file(
                    handle, path=rel, if_exists=True
                )
                deleted += 1
            else:
                # added or modified — fetch new content from Gitea HEAD.
                new_content = await gitea_client.get_file_content(
                    repo_name, gitea_path, ref=head
                )
                if new_content is None:
                    errors.append(f"{gitea_path}: file missing in Gitea HEAD")
                    continue
                blob = (
                    new_content.encode("utf-8")
                    if isinstance(new_content, str)
                    else new_content
                )
                await backend.put_project_folder_file_bytes(
                    handle, path=rel, content=blob
                )
                applied += 1
        except CloudBackendError as e:
            errors.append(f"{gitea_path}: {e}")
        except Exception as e:
            errors.append(f"{gitea_path}: {e}")
    logger.info(
        "Mode A: job %s — applied %d, deleted %d, errors=%d",
        job_short,
        applied,
        deleted,
        len(errors),
    )
    return {"applied": applied, "deleted": deleted, "errors": errors}
