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

See knowledge-history/done/job_cloud_export.md §5 and the Task 6 SDD brief
(.superpowers/sdd/task-6-brief.md) for the shape contract — Tasks 7/8/10
depend on these dataclasses staying exactly as defined.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass
from typing import Any

from services.cloud.errors import CloudBackendError
from services.cloud_staging.stage import staging_manifest_key, staging_tar_key
from services.cloud_staging.source_identity import ProtectedMountSourceIdentity
from services.blocking_effect import joined_blocking_call
from services.cloud_staging.manifest import (
    MAX_MANIFEST_ENTRIES,
    MAX_MANIFEST_JSON_BYTES,
    MAX_PATH_BYTES,
    MAX_STAGED_FILE_BYTES,
)

# Sentinel for the summary/manifest/tar memo fields below: distinguishes "not
# computed yet" from a computed-and-cached ``None`` (no diff available).
_UNSET: Any = object()


MAX_STAGED_TAR_BYTES = 9 * 1024**3


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    a single request; both the manifest and the tar are memoized (first
    touch, cached for the life of the instance) like ``GiteaDiffSource``.

    **Manifest-only summaries (post-review hardening, 2026-07-12):**
    ``summary()`` reads and validates ONLY the manifest — no tar download, no
    hash. The badge/status endpoint polls this every turn, and a staged
    upperdir tar can be multiple GiB (up to the 9 GiB stage cap); downloading
    and hashing it just to answer "how many files changed" risked OOMing the
    orchestrator (2 GiB memory limit). The tar download + content-binding
    check happen lazily on first actual tar touch instead — ``file()``,
    ``raw_new_bytes()``, or the explicit ``ensure_tar_bound()`` probe — all
    funneled through ``_get_tar()``.

    **Content binding** (multi-replica torn-pair defense, design §5 addendum
    2026-07-12): the manifest and tar are two independent S3 objects at
    deterministic keys (``staging_manifest_key``/``staging_tar_key``),
    written by two non-atomic PUTs (``cloud_staging.stage``). With two
    orchestrator replicas, two concurrent stagings for the same thread can
    interleave those PUTs, leaving a manifest at the deterministic key that
    doesn't describe the tar sitting next to it. ``_get_tar()`` verifies
    ``sha256(tar_bytes) == manifest["tar_sha256"]`` (hashed off-loop via the
    joined blocking-effect helper) before trusting the tar; on mismatch, a missing
    tar blob, or an absent hash, the tar is treated as unusable — ``file()``
    returns ``None`` for any entry that needs new-side bytes,
    ``raw_new_bytes()`` returns ``None``, and ``ensure_tar_bound()`` returns
    ``False``. The failure is cached: once ``_get_tar()`` resolves to
    "unusable" for this instance, it stays that way. ``summary()`` itself
    never fails this way — a valid manifest always yields a summary, even if
    the tar behind it turns out to be torn. Security invariant: apply
    (Task 10) never writes unverified bytes — every write funnels through
    ``raw_new_bytes()``, which sits behind this gate; apply also calls
    ``ensure_tar_bound()`` up front (before any delete/create) so a torn tar
    is caught before any destructive action, not discovered mid-sequence.
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
        # Set by ``_get_tar()`` on first actual tar touch — ``None`` for
        # "attempted and unusable" (missing blob / absent hash / mismatch),
        # a real ``TarFile`` for "attempted and bound", ``_UNSET`` for "not
        # touched yet". ``summary()`` never populates this (manifest-only).
        self._tar_cache: tarfile.TarFile | None = _UNSET
        self._tar_path: str | None = None

    def close(self) -> None:
        if isinstance(self._tar_cache, tarfile.TarFile):
            self._tar_cache.close()
        self._tar_cache = None
        if self._tar_path is not None:
            try:
                os.unlink(self._tar_path)
            except FileNotFoundError:
                pass
            self._tar_path = None

    def __del__(self) -> None:
        self.close()

    async def _get_manifest(self) -> dict[str, Any] | None:
        if self._manifest_cache is not _UNSET:
            return self._manifest_cache
        self._manifest_cache = await self._load_manifest()
        return self._manifest_cache

    async def _load_manifest(self) -> dict[str, Any] | None:
        """Manifest-only read: no tar download, no hash — see class docstring
        ("Manifest-only summaries"). ``summary()`` is the only caller that
        matters for cost; keeping this function tar-free is the whole point.
        """
        staged_summary = self._mount_row.get("staged_summary")
        if not isinstance(staged_summary, dict):
            return None
        source = ProtectedMountSourceIdentity.from_binding(
            self._mount_row.get("source_binding"),
            expected_sha256=str(self._mount_row.get("source_binding_sha256") or ""),
        )
        if source is None or not (
            staged_summary.get("source_binding") == source.binding
            and staged_summary.get("source_binding_sha256") == source.sha256
        ):
            return None
        raw = await self._snapshot_service.get_blob(
            staging_manifest_key(
                self._thread_id, self._mount_row.get("staged_summary")
            ),
            max_bytes=MAX_MANIFEST_JSON_BYTES,
        )
        if raw is None:
            return None
        try:
            manifest = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(manifest, dict) or not (
            manifest.get("source_binding") == source.binding
            and manifest.get("source_binding_sha256") == source.sha256
        ):
            return None
        return manifest

    async def _get_tar(self) -> tarfile.TarFile | None:
        """Download the tar and verify content-binding (design §5 addendum),
        first-touch, cached. Reached only by callers that need actual tar
        bytes — ``file()``, ``raw_new_bytes()``, ``ensure_tar_bound()`` —
        never by ``summary()``. See class docstring.
        """
        if self._tar_cache is not _UNSET:
            return self._tar_cache
        manifest = await self._get_manifest()
        if manifest is None:
            self._tar_cache = None
            return None
        expected_sha = manifest.get("tar_sha256")
        if not expected_sha:
            self._tar_cache = None
            return None
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as temporary:
            tar_path = temporary.name
        downloaded = await self._snapshot_service.download_blob_file(
            staging_tar_key(self._thread_id, self._mount_row.get("staged_summary")),
            tar_path,
            max_bytes=MAX_STAGED_TAR_BYTES,
        )
        if not downloaded:
            try:
                os.unlink(tar_path)
            except FileNotFoundError:
                pass
            self._tar_cache = None
            return None
        actual_sha = await joined_blocking_call(_sha256_file, tar_path)
        if actual_sha != expected_sha:
            os.unlink(tar_path)
            self._tar_cache = None
            return None
        try:
            self._tar_cache = await joined_blocking_call(tarfile.open, tar_path, "r")
        except (tarfile.TarError, OSError):
            os.unlink(tar_path)
            self._tar_cache = None
            return None
        self._tar_path = tar_path
        return self._tar_cache

    async def ensure_tar_bound(self) -> bool:
        """Force tar materialization + content-binding verification without
        reading any particular member.

        Apply (Task 10) must prove the staging is intact BEFORE taking any
        destructive action — reaching the binding gate lazily, per-file, in
        the middle of a delete-then-create sequence would let earlier writes
        land before a later file discovers the tar is torn. Calling this
        once, up front, turns that into a single boolean gate. Cached like
        every other ``_get_tar()`` caller (a subsequent ``file()``/
        ``raw_new_bytes()`` call in the same request reuses the result).
        """
        return await self._get_tar() is not None

    async def _tar_member_bytes(self, path: str) -> bytes | None:
        tar = await self._get_tar()
        if tar is None:
            return None

        def _read() -> bytes | None:
            try:
                info = tar.getmember(f"upper/{path}")
            except KeyError:
                return None
            if not info.isreg() or info.size < 0 or info.size > MAX_STAGED_FILE_BYTES:
                return None
            member = tar.extractfile(info)
            if member is None:
                return None
            data = member.read(MAX_STAGED_FILE_BYTES + 1)
            return data if len(data) <= MAX_STAGED_FILE_BYTES else None

        return await joined_blocking_call(_read)

    async def summary(self) -> DiffSummary | None:
        if self._summary_cache is not _UNSET:
            return self._summary_cache
        manifest = await self._get_manifest()
        if manifest is None:
            self._summary_cache = None
            return None
        entries = manifest.get("entries", [])
        if not isinstance(entries, list) or len(entries) > MAX_MANIFEST_ENTRIES:
            self._summary_cache = None
            return None
        if any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or len(entry["path"].encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES
            or entry.get("status") not in {"added", "modified", "deleted"}
            for entry in entries
        ):
            self._summary_cache = None
            return None
        self._summary_cache = DiffSummary(
            files=[
                DiffEntrySummary(
                    path=e["path"], status=e["status"], binary=e.get("binary", False)
                )
                for e in entries
            ],
            meta={
                "epoch": manifest.get("epoch"),
                "staged_at": manifest.get("staged_at"),
                "counts": manifest.get("counts"),
                "source_binding": manifest.get("source_binding"),
                "source_binding_sha256": manifest.get("source_binding_sha256"),
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
            tar = await self._get_tar()
            if tar is None:
                # Content-binding failed (or the tar blob is gone) — this
                # entry needs new-side bytes and the tar can't be trusted,
                # so treat the whole read as staging-missing rather than
                # silently showing a blank diff for a torn pair. Kept as a
                # protection "at the tar layer" (see class docstring) even
                # though summary() itself no longer fails this way.
                return None
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
