"""Mode B job export — copy a job's deliverables into a shared cloud folder.

Extracted verbatim from ``orchestrator.main`` (R1.B04 lane C). Four
properties are load-bearing and moved unchanged:

* **The folder name is deterministic.** ``_job_export_folder_name`` must map
  a job to the same segment forever, because the endpoint is re-syncable and
  re-derives the name to find the folder again. A job that already carries a
  handle reuses it instead, so a naming-scheme change cannot strand an
  existing share.
* **Copy failures are fail-soft only where the original made them so.** A
  declared-but-missing deliverable is logged and skipped; a missing file in
  the ``output/`` fallback is a 502. Everything else — an unexpected error,
  a cloud error — is a 502 with the count that actually landed.
* **The job is stamped only after a successful copy**, so a retry is safe.
* **An unshared folder is still a success.** ``ensure_user`` returning no id
  is loud in the log and reported as ``shared: false``, not raised: the files
  did land, and the user's first cloud login makes them visible.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from orchestrator.services.cloud import CloudBackendError, SessionFolderHandle

logger = logging.getLogger(__name__)

_EXPORT_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_EXPORT_SLUG_MAX = 40


@dataclass(frozen=True)
class JobExportDependencies:
    """Collaborators for one export, resolved per invocation.

    ``store``, ``forge`` and ``cloud_router`` are rebound during ``lifespan``;
    ``resolve_job_repo`` reads the store, so the application rebuilds this
    dataclass per request rather than capturing it at import.
    """

    store: Any
    forge: Any
    cloud_router: Any
    resolve_job_repo: Callable[[str], Awaitable[tuple[str, str | None]]]


def _job_export_folder_name(job_id: str, description: str | None) -> str:
    """Human-readable, stable folder name for a job's Mode B export.

    A cloud root full of ``job-a6fa6f2a9101`` folders is unnavigable after a
    handful of exports, so lead with a slug of the job's prompt and keep a short
    id suffix for uniqueness (two jobs can share a description) — e.g.
    ``you-maintain-a-daily-digest-a6fa6f2a``.

    Must stay **deterministic**: the endpoint is re-syncable and re-derives this
    name to find the folder again. ``description`` is fixed at job creation, so
    the same job always maps to the same name. Output is restricted to
    ``[a-z0-9-]``, which is a safe single path segment for every backend
    (both build ``sessions/<name>`` over WebDAV).
    """
    short = job_id.replace("-", "")
    # splitlines() on a blank string yields [], so index defensively.
    lines = (description or "").strip().splitlines()
    first_line = lines[0] if lines else ""
    # Fold accents so "Führe" slugs to "fuhre", not "f-hre".
    folded = (
        unicodedata.normalize("NFKD", first_line)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = _EXPORT_SLUG_STRIP.sub("-", folded.lower()).strip("-")
    if len(slug) > _EXPORT_SLUG_MAX:
        slug = slug[:_EXPORT_SLUG_MAX].rsplit("-", 1)[0].strip("-")
    if not slug:
        # Descriptions are NOT NULL but can be punctuation-only in theory.
        return f"job-{short[:12]}"
    return f"{slug}-{short[:8]}"


def _common_dir_prefix(paths: list[str]) -> str:
    """Longest whole-segment directory prefix shared by every path.

    ``["output/digest.md"]`` -> ``"output"``;
    ``["repo/src/a.py", "repo/tests/b.py"]`` -> ``"repo"``;
    ``["spec.yaml", "repo/a.py"]`` -> ``""``.

    Segment-wise, so ``["out/a.md", "output/b.md"]`` shares nothing rather than
    the string prefix ``"out"``. The last component of each path is the
    filename and never counts.
    """
    if not paths:
        return ""
    common = paths[0].split("/")[:-1]
    for path in paths[1:]:
        segments = path.split("/")[:-1]
        keep = 0
        while (
            keep < len(common)
            and keep < len(segments)
            and common[keep] == segments[keep]
        ):
            keep += 1
        common = common[:keep]
        if not common:
            break
    return "/".join(common)


async def export_job_to_shared_folder(
    *,
    job_id: str,
    user: dict[str, Any],
    authorized_job: dict[str, Any],
    dependencies: JobExportDependencies,
) -> dict[str, Any]:
    """Mode B of the job cloud workflow — copy a job's deliverables into a
    shared cloud folder ("Open cloud folder") and return its browser URL.

    Valid for ``completed`` or ``pending_review`` jobs whose project has **no**
    main-cloud folder (loose jobs and default-project / no-cloud-folder jobs);
    jobs whose project *does* have a cloud folder go through the Mode A
    diff-review flow instead. Copies the agent's declared deliverables (from
    ``freeze_data.deliverables``; falls back to ``output/`` for jobs without a
    deliverables list) into a per-job session-style cloud folder shared with the
    calling user. Workspace-relative paths are kept **except** for the leading
    directories every deliverable shares, which are collapsed
    (``_common_dir_prefix``) so a lone ``output/digest.md`` opens as
    ``digest.md`` instead of hiding a level down.

    Re-syncable: a repeat call overwrites the same folder and re-stamps
    ``exported_at`` as "last synced at" (e.g. after resume-with-feedback). The
    folder name is derived deterministically from the job, and a job that has
    been exported before reuses its stored handle. v1 overwrites in place and
    does not prune files removed between syncs — note that a re-sync after the
    deliverable set changes can therefore leave files from the previous shape
    behind, since the collapsed prefix is recomputed per call.

    See knowledge-history/done/job_cloud_export.md §3.2.
    """
    job = authorized_job

    # Status gate — completed or in-review. In-review (pending_review) export
    # lets the user preview the deliverables in the cloud before approving;
    # failed/cancelled/in-flight jobs have no stable output to copy.
    if job.get("status") not in ("completed", "pending_review"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job is in status '{job.get('status')}'; "
                "only completed or in-review jobs can be exported."
            ),
        )

    # Routing gate — only jobs whose project has a main-cloud folder use the
    # Mode A diff flow. Loose jobs AND default-project / no-cloud-folder jobs
    # fall through to Mode B here (mirrors the seed gate in
    # services/job_cloud_baseline.py). ``project_has_cloud_folder`` is computed
    # by the projects LEFT JOIN in postgres.get_job.
    if job.get("project_has_cloud_folder"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Job's project has a cloud folder — use the diff-review "
                "(accept/reject) flow instead of shared-folder export."
            ),
        )

    # No idempotency refusal: this endpoint is re-syncable. The folder id is
    # derived deterministically from the job id below, so a repeat call
    # overwrites the same folder (e.g. after resume-with-feedback) and
    # re-stamps ``exported_at`` as "last synced at".

    # Fresh loose-job export folder — no project/thread row to pin to yet,
    # so resolve via the owner seam (returns the active backend today;
    # per-org under multi-tenancy). Issue 16, knowledge-base/knowledge/issues/main_cloud.md.
    backend = dependencies.cloud_router.for_owner(user)
    if not backend.is_initialized:
        raise HTTPException(status_code=503, detail="Cloud backend not available.")
    if not dependencies.forge.is_initialized:
        raise HTTPException(status_code=503, detail="Gitea not available.")

    repo_name, branch = await dependencies.resolve_job_repo(job_id)

    # 1) Provision shared folder. A job that has been exported before keeps the
    #    folder it already has — re-deriving the name would strand the old
    #    folder (and its share) whenever the naming scheme changes, which it did
    #    once already when these went from `job-<uuid>` to slugged names.
    previous_handle = job.get("exported_folder_handle")
    if previous_handle:
        folder_session_id = (
            SessionFolderHandle.from_db(previous_handle, backend=backend.backend_id)
            .native_id.rstrip("/")
            .split("/")[-1]
        )
    else:
        folder_session_id = _job_export_folder_name(job_id, job.get("description"))
    try:
        folder_handle = await backend.ensure_session_folder(
            session_id=folder_session_id
        )
        resolved_user_id = await backend.ensure_user(
            sub=user.get("keycloak_sub") or "",
            issuer=getattr(backend, "_keycloak_issuer", "") or "",
            email=user.get("email"),
            display_name=user.get("display_name"),
            preferred_username=user.get("preferred_username"),
        )
        if resolved_user_id:
            await backend.share_session_folder(folder_handle, resolved_user_id)
        else:
            # Not fatal — the files still land in the agent's cloud home and a
            # later re-sync shares them once the account exists. But it IS the
            # difference between "the folder opens" and "the folder 404s", so
            # say so loudly here and hand the caller a `shared: false` it can
            # surface. Backends that cannot provision accounts themselves
            # (Nextcloud: user_oidc materialises the account on the user's
            # first browser login) resolve to None until the user has signed
            # in to the cloud at least once.
            logger.warning(
                "Mode B export: no %s account for user %s (email=%r) — folder "
                "%s created but NOT shared; the user must sign in to the cloud "
                "once before it becomes visible to them",
                backend.backend_id,
                user.get("id"),
                user.get("email"),
                folder_session_id,
            )
    except CloudBackendError as e:
        logger.exception("Mode B export: folder provisioning failed for job %s", job_id)
        raise HTTPException(
            status_code=502,
            detail=f"Cloud folder provisioning failed: {e}",
        ) from e

    # 2) Copy the job's deliverables from Gitea → cloud, bytes-faithful via
    #    get_file_bytes so binary outputs (PDFs, images) survive the round trip
    #    and the declared workspace-relative paths are preserved. The agent's
    #    declared deliverables (validated non-empty at freeze time) are the
    #    curated result set — for code jobs they live under ``repo/`` and the
    #    workspace root, not ``output/``. Jobs without a deliverables list
    #    (older jobs) fall back to copying ``output/`` wholesale. Per-file read
    #    failures in the deliverables path are logged and skipped (fail-soft) so
    #    one missing artifact doesn't sink the whole export; ``files_copied``
    #    reflects what actually landed.
    files_copied = 0

    # Declared deliverables from freeze_data (JSONB may arrive as a str).
    freeze_data = job.get("freeze_data")
    if isinstance(freeze_data, str):
        try:
            freeze_data = json.loads(freeze_data)
        except (json.JSONDecodeError, TypeError):
            freeze_data = None
    deliverables: list[str] = []
    if isinstance(freeze_data, dict) and isinstance(
        freeze_data.get("deliverables"), list
    ):
        deliverables = [
            str(p).strip() for p in freeze_data["deliverables"] if str(p).strip()
        ]

    async def _list_tree(src_dir: str) -> list[str]:
        """Repo-relative paths of every file under ``src_dir``, recursively."""
        found: list[str] = []
        entries = await dependencies.forge.list_contents(repo_name, src_dir, ref=branch)
        for entry in entries or []:
            entry_type = entry.get("type")
            if entry_type == "dir":
                found.extend(await _list_tree(entry["path"]))
            elif entry_type == "file":
                found.append(entry["path"])
        return found

    async def _copy(rel: str, dest: str, *, required: bool) -> None:
        nonlocal files_copied
        file_bytes = await dependencies.forge.get_file_bytes(repo_name, rel, ref=branch)
        if file_bytes is None:
            if required:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to read '{rel}' from Gitea.",
                )
            # Declared but absent from the repo (e.g. a directory entry or a
            # workspace-only artifact). Skip fail-soft rather than 502.
            logger.warning(
                "Mode B export: deliverable %r not found in repo %s; skipping",
                rel,
                repo_name,
            )
            return
        await backend.put_session_file(folder_handle, path=dest, content=file_bytes)
        files_copied += 1

    try:
        if deliverables:
            # Declared paths are workspace-root-relative, matching the job's
            # Gitea repo layout. Reject path escapes defensively.
            sources: list[str] = []
            for declared in deliverables:
                rel = declared.lstrip("/")
                if not rel or ".." in rel.split("/"):
                    logger.warning(
                        "Mode B export: skipping unsafe deliverable path %r", declared
                    )
                    continue
                sources.append(rel)
            # A declared-but-missing deliverable is not fatal (see _copy).
            required = False
        else:
            # No declared deliverables (older jobs) — copy output/ wholesale.
            sources = await _list_tree("output")
            required = True

        # Collapse the wrapper directories every file shares, so a job whose one
        # deliverable is output/digest.md lands as digest.md at the folder root
        # rather than behind two clicks. Structure that actually distinguishes
        # files survives — repo/src/a.py + repo/tests/b.py only lose `repo/`.
        prefix = _common_dir_prefix(sources)
        for rel in sources:
            await _copy(
                rel,
                rel[len(prefix) + 1 :] if prefix else rel,
                required=required,
            )
    except HTTPException:
        raise
    except CloudBackendError as e:
        logger.exception(
            "Mode B export: cloud upload failed for job %s (copied %d)",
            job_id,
            files_copied,
        )
        raise HTTPException(
            status_code=502,
            detail=f"File copy to cloud failed after {files_copied} files: {e}",
        ) from e
    except Exception as e:
        logger.exception("Mode B export: unexpected failure for job %s", job_id)
        raise HTTPException(
            status_code=502,
            detail=f"Export failed: {e}",
        ) from e

    # 3) Stamp the job — only on success so retries are safe.
    await dependencies.store.update_job_exported_folder(
        job_id, handle=folder_handle.to_db()
    )

    return {
        "job_id": job_id,
        "files_copied": files_copied,
        # False = copied, but the folder is not visible to the caller yet (see
        # the ensure_user miss above). The cockpit turns this into a distinct
        # toast instead of claiming an unqualified success.
        "shared": bool(resolved_user_id),
        "folder": {
            "name": folder_session_id,
            # Where the caller will actually find it: the share lands at the
            # root of their own cloud drive, so the folder name IS the path.
            # Sent explicitly so the cockpit doesn't have to assume that.
            "path": f"/{folder_session_id}",
            "browser_url": backend.get_session_folder_browser_url(folder_handle),
            "webdav_url": backend.get_session_folder_webdav_url(folder_handle),
        },
    }
