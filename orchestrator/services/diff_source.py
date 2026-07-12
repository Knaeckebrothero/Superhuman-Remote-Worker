"""``DiffSource`` protocol + ``GiteaDiffSource``/``UpperdirDiffSource``.

The two ``/api/jobs/{job_id}/diff`` endpoints (main.py) originally fetched
diff data inline from Gitea. This module extracts that data-fetch behind a
small seam so the same endpoints can serve either Mode A (Gitea) or
protected cloud mode (Slice C) diffs without touching endpoint gate logic.

``GiteaDiffSource`` is the Mode A implementation: it diffs two Gitea tree
snapshots (baseline commit .. branch head) and reads per-file content via
Gitea's contents API — text only, per the v1 baseline-seed limitation (see
``job_cloud_baseline.py`` module docstring). ``binary``/``old_binary``/
``new_binary`` always read ``False`` here; a byte-aware source (upperdir)
sets them per-file.

``UpperdirDiffSource`` is the protected cloud mode (Slice C) implementation
(Task 7): summary from the S3-staged manifest (``services.cloud_staging``,
Tasks 3/4), new-side bytes from the staged upperdir tar, old-side bytes from
the live cloud backend (the same read path ``export_job_to_shared_folder``
uses). See its class docstring for the mandatory tar/manifest content-binding
check.

See docs/done/job_cloud_export.md §5 and the Task 6 SDD brief
(.superpowers/sdd/task-6-brief.md) for the shape contract — Tasks 7/8/10
depend on these dataclasses staying exactly as defined.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from typing import Any

from services.cloud.errors import CloudBackendError
from services.cloud_staging.stage import staging_manifest_key, staging_tar_key

# Sentinel for the summary/manifest/tar memo fields below: distinguishes "not
# computed yet" from a computed-and-cached ``None`` (no diff available).
_UNSET: Any = object()


@dataclass(frozen=True)
class DiffEntrySummary:
    path: str
    status: str  # "added" | "modified" | "deleted"
    binary: bool = False


@dataclass(frozen=True)
class DiffSummary:
    files: list[DiffEntrySummary]
    meta: dict[str, Any]  # source-specific: gitea -> baseline_commit/head_commit;
    # upperdir -> epoch/staged_at/counts


@dataclass(frozen=True)
class DiffFileContent:
    path: str
    status: str
    old_content: str | None
    new_content: str | None
    old_binary: bool = False
    new_binary: bool = False


class GiteaDiffSource:
    """Mode A diff source: Gitea trees at baseline..branch (text-only).

    ``summary()`` is memoized per instance: the endpoints call it once
    for gating and again (via ``file()``) for content, and an instance
    is scoped to a single request — one Gitea tree walk each, not two.
    """

    def __init__(self, *, job: dict, gitea_client: Any):
        self._job = job
        self._gitea = gitea_client
        self._summary_cache: DiffSummary | None = _UNSET

    async def summary(self) -> DiffSummary | None:
        if self._summary_cache is not _UNSET:
            return self._summary_cache
        from services.job_cloud_baseline import get_diff_summary

        s = await get_diff_summary(job=self._job, gitea_client=self._gitea)
        if s is None:
            self._summary_cache = None
            return None
        self._summary_cache = DiffSummary(
            files=[
                DiffEntrySummary(path=f["path"], status=f["status"]) for f in s["files"]
            ],
            meta={
                "baseline_commit": s["baseline_commit"],
                "head_commit": s["head_commit"],
            },
        )
        return self._summary_cache

    async def file(self, path: str) -> DiffFileContent | None:
        s = await self.summary()
        if s is None:
            return None
        entry = next((f for f in s.files if f.path == path), None)
        if entry is None:
            return None
        repo = self._job.get("repo_name")
        baseline = self._job.get("cloud_diff_baseline_commit")
        branch = self._job.get("branch_name") or "main"
        old = new = None
        if entry.status in ("modified", "deleted"):
            old = await self._gitea.get_file_content(repo, path, ref=baseline)
        if entry.status in ("modified", "added"):
            new = await self._gitea.get_file_content(repo, path, ref=branch)
        return DiffFileContent(
            path=path, status=entry.status, old_content=old, new_content=new
        )


class UpperdirDiffSource:
    """Protected cloud mode (Slice C) diff source: staged upperdir review.

    ``summary()`` comes from the S3-staged manifest (``services.cloud_staging``
    Tasks 3/4); new-side bytes from the staged ``upper.tar``; old-side bytes
    from the live cloud backend via the same ``get_project_folder_file_bytes``
    read path ``export_job_to_shared_folder`` uses. One instance is scoped to
    a single request: the tar is downloaded and content-verified at most once
    (first touch, from either ``summary()`` or ``file()``/``raw_new_bytes()``),
    and ``summary()`` is memoized like ``GiteaDiffSource``.

    **Content binding** (multi-replica torn-pair defense, design §5 addendum
    2026-07-12): the manifest and tar are two independent S3 objects at
    deterministic keys (``staging_manifest_key``/``staging_tar_key``),
    written by two non-atomic PUTs (``cloud_staging.stage``). With two
    orchestrator replicas, two concurrent stagings for the same thread can
    interleave those PUTs, leaving a manifest at the deterministic key that
    doesn't describe the tar sitting next to it. Every read here verifies
    ``sha256(tar_bytes) == manifest["tar_sha256"]`` before trusting either;
    on mismatch or an absent hash, the whole staging is treated as missing —
    both ``summary()`` and ``file()`` return ``None``. A manifest must never
    be served against a tar it doesn't describe.
    """

    def __init__(
        self,
        *,
        thread_id: str,
        mount_row: dict,
        backend: Any,
        handle: Any,
        snapshot_service: Any,
    ):
        self._thread_id = thread_id
        self._mount_row = mount_row
        self._backend = backend
        self._handle = handle
        self._snapshot_service = snapshot_service
        self._manifest_cache: dict[str, Any] | None = _UNSET
        self._summary_cache: DiffSummary | None = _UNSET
        # Populated as a side effect of a successful content-binding check
        # inside ``_load_and_bind_manifest``; stays ``_UNSET`` if the
        # manifest is missing/unbound, so it never opens a mismatched tar.
        self._tar_cache: tarfile.TarFile | None = _UNSET

    async def _get_manifest(self) -> dict[str, Any] | None:
        if self._manifest_cache is not _UNSET:
            return self._manifest_cache
        self._manifest_cache = await self._load_and_bind_manifest()
        return self._manifest_cache

    async def _load_and_bind_manifest(self) -> dict[str, Any] | None:
        if self._mount_row.get("staged_summary") is None:
            return None
        raw = await self._snapshot_service.get_blob(
            staging_manifest_key(self._thread_id)
        )
        if raw is None:
            return None
        try:
            manifest = json.loads(raw)
        except (ValueError, TypeError):
            return None

        expected_sha = manifest.get("tar_sha256")
        if not expected_sha:
            return None
        tar_bytes = await self._snapshot_service.get_blob(
            staging_tar_key(self._thread_id)
        )
        if tar_bytes is None:
            return None
        if hashlib.sha256(tar_bytes).hexdigest() != expected_sha:
            return None

        self._tar_cache = tarfile.open(fileobj=io.BytesIO(tar_bytes))
        return manifest

    async def _get_tar(self) -> tarfile.TarFile | None:
        await self._get_manifest()
        return None if self._tar_cache is _UNSET else self._tar_cache

    async def _tar_member_bytes(self, path: str) -> bytes | None:
        tar = await self._get_tar()
        if tar is None:
            return None
        try:
            member = tar.extractfile(f"upper/{path}")
        except KeyError:
            return None
        if member is None:
            return None
        return member.read()

    async def summary(self) -> DiffSummary | None:
        if self._summary_cache is not _UNSET:
            return self._summary_cache
        manifest = await self._get_manifest()
        if manifest is None:
            self._summary_cache = None
            return None
        self._summary_cache = DiffSummary(
            files=[
                DiffEntrySummary(
                    path=e["path"], status=e["status"], binary=e.get("binary", False)
                )
                for e in manifest.get("entries", [])
            ],
            meta={
                "epoch": manifest.get("epoch"),
                "staged_at": manifest.get("staged_at"),
                "counts": manifest.get("counts"),
            },
        )
        return self._summary_cache

    async def file(self, path: str) -> DiffFileContent | None:
        s = await self.summary()
        if s is None:
            return None
        entry = next((f for f in s.files if f.path == path), None)
        if entry is None:
            return None

        new_content: str | None = None
        new_binary = False
        if entry.status in ("added", "modified"):
            raw_new = await self._tar_member_bytes(path)
            if raw_new is not None:
                if entry.binary:
                    new_binary = True
                else:
                    try:
                        new_content = raw_new.decode("utf-8")
                    except UnicodeDecodeError:
                        new_binary = True

        old_content: str | None = None
        old_binary = False
        if entry.status in ("modified", "deleted"):
            raw_old: bytes | None
            try:
                raw_old = await self._backend.get_project_folder_file_bytes(
                    self._handle, path=path
                )
            except CloudBackendError:
                # Review UI shows "unavailable"; apply never uses old bytes.
                raw_old = None
            if raw_old is not None:
                try:
                    old_content = raw_old.decode("utf-8")
                except UnicodeDecodeError:
                    old_binary = True

        return DiffFileContent(
            path=path,
            status=entry.status,
            old_content=old_content,
            new_content=new_content,
            old_binary=old_binary,
            new_binary=new_binary,
        )

    async def raw_new_bytes(self, path: str) -> bytes | None:
        """Undecoded new-side tar member bytes — apply (Task 10) is byte-true."""
        return await self._tar_member_bytes(path)
