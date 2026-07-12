"""Turn-end stage service — protected cloud mode Slice C (design §5).

At turn end (and on the idle-drain path) the orchestrator SSHes into the
protected session's workspace pod, fingerprints the fuse-overlayfs upperdir,
and — only if it changed since the last stage — streams the upperdir tar to
S3 and derives a diff manifest (``services.cloud_staging.manifest``) against
the mount's etag baseline. Both blobs live under deterministic keys
(``staging_tar_key`` / ``staging_manifest_key``) so the review/apply path
(Tasks 5/8/10) can locate them without round-tripping through this module.

Debounced per-thread via a module-level ``_inflight`` set: turn-end staging
and the idle-drain sweep can race for the same thread, and a second SSH+tar
pass while the first is still streaming would just waste bandwidth and could
step on the same temp files.

Never raises: every entry point either returns a ``{"skipped": <reason>}``
no-op dict, a success dict, or ``None`` on hard failure (logged here).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from services.cloud_staging.manifest import derive_manifest
from services.ssh_helpers import build_agent_ssh_cmd

logger = logging.getLogger(__name__)

# sha256 of empty input — what ``stage_signature_cmd()`` hashes to when the
# upperdir has no entries (nothing staged, or everything since undone).
_EMPTY_SIGNATURE = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# Hard cap on the streamed upperdir tar — above the 8 GiB upperdir quota
# (main.py:_PROTECTED_UPPERDIR_QUOTA_BYTES) so a runaway session aborts the
# stage instead of ballooning the orchestrator's local disk.
_STAGE_MAX_BYTES = 9 * 1024**3

_SSH_CAPTURE_TIMEOUT_S = 30.0

# Per-thread debounce: threads currently mid-stage. See module docstring.
_inflight: set[str] = set()


def staging_tar_key(thread_id: str) -> str:
    return f"cloud-staging/{thread_id}/upper.tar"


def staging_manifest_key(thread_id: str) -> str:
    return f"cloud-staging/{thread_id}/manifest.json"


def stage_signature_cmd() -> str:
    """Cheap content fingerprint of the upperdir — no tar/network cost.

    Hashes (relpath, type, size, mtime) for every entry, sorted, so any
    write/delete/rename changes the digest. Empty upperdir hashes to
    ``_EMPTY_SIGNATURE`` (sha256 of empty input).
    """
    return (
        "find /home/agent-host/.overlay/upper -mindepth 1 "
        "-printf '%P|%y|%s|%T@\\n' 2>/dev/null | sort | sha256sum | cut -d' ' -f1"
    )


def stage_tar_cmd() -> str:
    """Plain (uncompressed) tar of the upperdir — the manifest deriver and
    the diff/apply path (Task 5+) read it back with ``tarfile``, so no zstd."""
    return (
        "tar --xattrs --xattrs-include='*' --acls -C /home/agent-host/.overlay "
        "-cf - upper"
    )


def _resolve_workspace_ssh(metadata: dict) -> tuple[str, int] | None:
    """Resolve the workspace SSH host/port from thread metadata.

    Source: ``workspace_suspension.py``'s ``suspend_thread_workspace``
    (host/port extraction at :467-472) and its module-level
    ``_resolve_ssh_port`` (:25-35) — container/pod workspaces
    (``workspace_container``) default to port 30022; true VM contexts
    (``vm``) default to 22. Returns ``None`` when neither context yields a
    host (no workspace provisioned / already torn down).
    """
    ws_ctx = metadata.get("workspace_container") or {}
    vm_ctx = metadata.get("vm") or {}

    host = ws_ctx.get("pod_ip") or ws_ctx.get("host") or vm_ctx.get("ssh_host")
    if not host:
        return None

    port = int(ws_ctx.get("port", 30022)) if ws_ctx else int(vm_ctx.get("ssh_port", 22))
    return host, port


def _parse_metadata(thread: dict | None) -> dict:
    """Thread ``metadata`` comes back from asyncpg as a raw JSON string."""
    metadata = (thread or {}).get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}
    return metadata


# =============================================================================
# Subprocess seams (monkeypatched in tests — see module docstring on why)
# =============================================================================


async def _run_ssh_capture(cmd: list[str], *, timeout: float) -> bytes | None:
    """Run ``cmd``, capturing stdout. Returns ``None`` on spawn/timeout/rc!=0."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        logger.error("stage: failed to spawn ssh capture: %s", e)
        return None

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("stage: ssh capture timed out after %ss", timeout)
        return None

    if proc.returncode != 0:
        logger.warning(
            "stage: ssh capture failed (rc=%s): %s",
            proc.returncode,
            stderr.decode(errors="replace")[:500],
        )
        return None

    return stdout


async def _stream_tar_to_file(cmd: list[str], dest_path: str) -> bool:
    """Stream an SSH tar command's stdout to ``dest_path`` in 1 MiB chunks.

    Loop shape copied from ``snapshot_service.py:424-454``. Enforces
    ``_STAGE_MAX_BYTES``: kills the child and returns ``False`` if exceeded.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        logger.error("stage: failed to spawn ssh tar: %s", e)
        return False

    total_bytes = 0
    with open(dest_path, "wb") as f:
        while True:
            chunk = await process.stdout.read(1024 * 1024)  # 1 MiB chunks
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > _STAGE_MAX_BYTES:
                process.kill()
                await process.wait()
                logger.warning(
                    "stage: upperdir tar exceeds cap (%d bytes), aborting",
                    _STAGE_MAX_BYTES,
                )
                return False
            f.write(chunk)

    await process.wait()

    if process.returncode != 0 and total_bytes == 0:
        stderr = (await process.stderr.read()).decode(errors="replace")
        logger.error("stage: ssh tar failed (rc=%s): %s", process.returncode, stderr[:500])
        return False

    return True


# =============================================================================
# Entry point
# =============================================================================


async def stage_thread_cloud_diff(
    *, thread_id: str, postgres_db: Any, snapshot_service: Any
) -> dict | None:
    """Stage the protected session's upperdir diff to S3, if it changed.

    Returns ``{"epoch": int, "counts": {...}}`` on a successful push,
    ``{"skipped": "<reason>"}`` on a no-op (``not_protected`` /
    ``no_active_mount`` / ``no_workspace`` / ``in_flight`` / ``unchanged`` /
    ``empty``), or ``None`` on hard failure (logged, never raises).
    """
    if thread_id in _inflight:
        return {"skipped": "in_flight"}

    _inflight.add(thread_id)
    try:
        return await _stage_thread_cloud_diff(
            thread_id=thread_id, postgres_db=postgres_db, snapshot_service=snapshot_service
        )
    except Exception:
        logger.exception("stage: unhandled error staging thread %s", thread_id)
        return None
    finally:
        _inflight.discard(thread_id)


async def _stage_thread_cloud_diff(
    *, thread_id: str, postgres_db: Any, snapshot_service: Any
) -> dict | None:
    thread = await postgres_db.get_thread(thread_id)
    metadata = _parse_metadata(thread)
    if not metadata.get("protected_cloud"):
        return {"skipped": "not_protected"}

    row = await postgres_db.get_ro_mount_by_thread(thread_id)
    if not row or row.get("status") != "active":
        return {"skipped": "no_active_mount"}

    resolved = _resolve_workspace_ssh(metadata)
    if resolved is None:
        return {"skipped": "no_workspace"}
    ssh_host, ssh_port = resolved

    sig_cmd = build_agent_ssh_cmd(ssh_host, ssh_port, stage_signature_cmd())
    raw = await _run_ssh_capture(sig_cmd, timeout=_SSH_CAPTURE_TIMEOUT_S)
    if raw is None:
        logger.warning(
            "stage: signature probe failed for thread %s (%s:%d)",
            thread_id, ssh_host, ssh_port,
        )
        return None
    signature = raw.decode(errors="replace").strip()

    if signature == _EMPTY_SIGNATURE:
        if row.get("staged_summary") is not None:
            await snapshot_service.delete_blob(staging_tar_key(thread_id))
            await snapshot_service.delete_blob(staging_manifest_key(thread_id))
            await postgres_db.update_ro_mount_staging(
                row["id"],
                staged_epoch=row["staged_epoch"] + 1,
                staged_summary=None,
            )
        return {"skipped": "empty"}

    staged_summary = row.get("staged_summary")
    if staged_summary and staged_summary.get("signature") == signature:
        return {"skipped": "unchanged"}

    tar_path: str | None = None
    manifest_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
            tar_path = tmp.name

        tar_cmd = build_agent_ssh_cmd(ssh_host, ssh_port, stage_tar_cmd())
        if not await _stream_tar_to_file(tar_cmd, tar_path):
            logger.warning("stage: tar stream failed for thread %s", thread_id)
            return None

        epoch = row["staged_epoch"] + 1
        manifest = derive_manifest(
            tar_path,
            baseline=row.get("etag_baseline") or {},
            epoch=epoch,
            staged_at=datetime.now(timezone.utc).isoformat(),
        )
        manifest["signature"] = signature

        if not await snapshot_service.upload_blob_file(staging_tar_key(thread_id), tar_path):
            logger.error("stage: tar upload failed for thread %s", thread_id)
            return None

        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="wb"
        ) as mtmp:
            manifest_path = mtmp.name
            mtmp.write(json.dumps(manifest).encode())

        if not await snapshot_service.upload_blob_file(
            staging_manifest_key(thread_id), manifest_path
        ):
            logger.error("stage: manifest upload failed for thread %s", thread_id)
            return None

        await postgres_db.update_ro_mount_staging(
            row["id"],
            staged_epoch=manifest["epoch"],
            staged_summary={"counts": manifest["counts"], "signature": signature},
        )
        return {"epoch": manifest["epoch"], "counts": manifest["counts"]}
    finally:
        for path in (tar_path, manifest_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
