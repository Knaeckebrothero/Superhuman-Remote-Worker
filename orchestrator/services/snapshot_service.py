"""Snapshot Service — S3-backed environment snapshot management.

Captures, stores, and retrieves agent VM/pod environment snapshots in
S3-compatible object storage (MinIO, AWS S3, etc.). Snapshots enable
on-demand IDE sessions and environment-aware job resume.

Architecture:
  - Each entity's snapshots live under ``s3://<bucket>/<entity_type>/<uuid>/``
    (``entity_type`` is ``jobs`` or ``threads``)
  - Phase-boundary snapshots are stored per-phase under ``phases/phase_<n>/``
  - The top-level ``manifest.json`` + ``env.tar.zst`` always point to the latest

Selection logic:
  - S3_ENDPOINT configured → S3 features enabled
  - No S3_ENDPOINT → gracefully disabled (all methods return None/False)
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from services import resolve_ssh_key_path

logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False
    boto3 = None  # type: ignore[assignment]
    BotoConfig = None  # type: ignore[misc,assignment]
    ClientError = Exception  # type: ignore[misc,assignment]


class SnapshotService:
    """S3-backed snapshot management for agent environments.

    All methods are async-safe (blocking S3 calls run in thread pool).
    Gracefully degrades when S3 is not configured or unavailable.
    """

    def __init__(self) -> None:
        self._s3: Any = None
        self._db: Any = None
        self._bucket: str = ""
        self._available: bool = False

    @property
    def is_available(self) -> bool:
        """True if S3 is configured and reachable."""
        return self._available

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def connect(self, db: Any) -> None:
        """Initialize the S3 client and ensure the bucket exists.

        Args:
            db: PostgresDB instance for job context updates.
        """
        self._db = db

        endpoint = os.environ.get("S3_ENDPOINT", "")
        if not endpoint or not BOTO3_AVAILABLE:
            # Warning, not info: everything downstream of this degrades
            # silently. Name the casualties so an operator reading startup logs
            # can tell this apart from a healthy deployment.
            reason = (
                "boto3 not installed" if not BOTO3_AVAILABLE else "S3_ENDPOINT not set"
            )
            logger.warning(
                "Snapshot service disabled (%s) — workspace snapshots will not "
                "capture, the virtual workspace tier is unwired, and Canvas "
                "presentations will NOT survive their workspace. Set "
                "s3.endpoint, or leave garage.enabled unset to run the bundled "
                "object store.",
                reason,
            )
            return

        access_key = os.environ.get("SNAPSHOT_S3_ACCESS_KEY_ID", "")
        secret_key = os.environ.get("SNAPSHOT_S3_SECRET_ACCESS_KEY", "")
        self._bucket = os.environ.get("S3_BUCKET", "srw-snapshots")
        region = os.environ.get("S3_REGION", "us-east-1")

        try:
            self._s3 = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                config=BotoConfig(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )

            # Ensure bucket exists (auto-create for dev)
            await asyncio.to_thread(self._ensure_bucket)
            self._available = True
            logger.info(
                "Snapshot service ready: endpoint=%s bucket=%s",
                endpoint,
                self._bucket,
            )
        except Exception as e:
            logger.warning("Snapshot service: S3 connection failed — disabled: %s", e)
            self._s3 = None
            self._available = False

    def _ensure_bucket(self) -> None:
        """Create the bucket if it doesn't exist (synchronous, run in thread)."""
        try:
            self._s3.head_bucket(Bucket=self._bucket)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchBucket"):
                self._s3.create_bucket(Bucket=self._bucket)
                logger.info("Created S3 bucket: %s", self._bucket)
            else:
                raise

    # =========================================================================
    # Upload / Capture
    # =========================================================================

    async def upload_snapshot(
        self,
        job_id: str,
        tar_path: str,
        manifest: dict[str, Any],
        phase_number: Optional[int] = None,
        entity_type: str = "jobs",
    ) -> bool:
        """Upload a snapshot tarball + manifest to S3.

        Args:
            job_id: Job or thread UUID.
            tar_path: Local path to the env.tar.zst file.
            manifest: Manifest dict (will be serialized to JSON).
            phase_number: If set, store under phases/phase_<n>/ as well.
            entity_type: S3 prefix namespace ("jobs" or "threads").

        Returns:
            True if upload succeeded.
        """
        if not self._available:
            return False

        prefix = f"{entity_type}/{job_id}"
        phase_prefix = (
            f"{prefix}/phases/phase_{phase_number}"
            if phase_number is not None
            else None
        )

        try:
            # Compute checksum
            sha256 = await asyncio.to_thread(self._compute_sha256, tar_path)
            manifest["checksum_sha256"] = sha256

            manifest_bytes = json.dumps(manifest, indent=2).encode()

            # Upload tarball + manifest to phase-specific prefix
            if phase_prefix:
                await asyncio.to_thread(
                    self._s3.upload_file,
                    tar_path,
                    self._bucket,
                    f"{phase_prefix}/env.tar.zst",
                )
                await asyncio.to_thread(
                    self._s3.put_object,
                    Bucket=self._bucket,
                    Key=f"{phase_prefix}/manifest.json",
                    Body=manifest_bytes,
                    ContentType="application/json",
                )

            # Always update top-level (latest) snapshot
            await asyncio.to_thread(
                self._s3.upload_file,
                tar_path,
                self._bucket,
                f"{prefix}/env.tar.zst",
            )
            await asyncio.to_thread(
                self._s3.put_object,
                Bucket=self._bucket,
                Key=f"{prefix}/manifest.json",
                Body=manifest_bytes,
                ContentType="application/json",
            )

            # Post-upload integrity check (§C2): confirm the canonical
            # object actually landed with the expected size before
            # advertising "available" — a truncated/partial multipart
            # upload must never be trusted. Cheap (HEAD only); the deep
            # hash re-check is verify_snapshot's job at reclaim time, not a
            # per-upload cost. "When present": some manifests omit
            # size_compressed_bytes, and there's nothing to compare
            # against in that case, so the check is skipped rather than
            # failing closed on absence.
            expected_size = manifest.get("size_compressed_bytes")
            if expected_size:
                head = await asyncio.to_thread(
                    self._s3.head_object,
                    Bucket=self._bucket,
                    Key=f"{prefix}/env.tar.zst",
                )
                actual_size = head["ContentLength"]
                if actual_size != expected_size:
                    logger.error(
                        "Snapshot upload size mismatch for %s %s: s3=%s manifest=%s",
                        entity_type.rstrip("s"),
                        job_id,
                        actual_size,
                        expected_size,
                    )
                    await self._set_snapshot_context(
                        job_id,
                        {
                            "status": "capture_failed",
                            "error": (
                                f"post-upload size mismatch (s3={actual_size} "
                                f"manifest={expected_size})"
                            ),
                        },
                        entity_type=entity_type,
                    )
                    return False

            # Update entity context
            await self._set_snapshot_context(
                job_id,
                {
                    "status": "available",
                    "source_type": manifest.get("source_type", "vm"),
                    "created_at": manifest.get(
                        "created_at", datetime.now(timezone.utc).isoformat()
                    ),
                    "size_compressed_bytes": manifest.get("size_compressed_bytes", 0),
                    "phase_number": phase_number,
                    "checksum_sha256": sha256,
                },
                entity_type=entity_type,
            )

            logger.info(
                "Snapshot uploaded: %s=%s phase=%s size=%s",
                entity_type.rstrip("s"),
                job_id,
                phase_number,
                manifest.get("size_compressed_bytes", "?"),
            )
            return True

        except Exception as e:
            logger.error(
                "Snapshot upload failed for %s %s: %s",
                entity_type.rstrip("s"),
                job_id,
                e,
            )
            await self._set_snapshot_context(
                job_id,
                {
                    "status": "capture_failed",
                    "error": str(e),
                },
                entity_type=entity_type,
            )
            return False

    # =========================================================================
    # Content-addressed blobs (Phase 3, D7 — cited cloud-document snapshots)
    # =========================================================================

    def _object_exists(self, key: str) -> bool:
        """True if an object exists at ``key`` (synchronous, run in a thread)."""
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    async def save_blob(
        self,
        data: bytes,
        *,
        prefix: str = "citations",
        content_type: str = "application/octet-stream",
    ) -> Optional[str]:
        """Store raw bytes content-addressed under ``<prefix>/<sha[:2]>/<sha>``.

        Used to snapshot the original bytes of a cited cloud document so the
        citation has a durable "view the original" backup (the agent has no S3
        credentials, so it reaches this via an orchestrator endpoint). Returns
        the object key, or ``None`` when the store is unavailable or the write
        fails. Idempotent: identical bytes hash to the same key, and an existing
        object is not re-uploaded (a cheap HEAD precedes the PUT).
        """
        if not self._available or not data:
            return None
        sha = hashlib.sha256(data).hexdigest()
        key = f"{prefix}/{sha[:2]}/{sha}"
        try:
            if not await asyncio.to_thread(self._object_exists, key):
                await asyncio.to_thread(
                    self._s3.put_object,
                    Bucket=self._bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type or "application/octet-stream",
                )
            return key
        except Exception as e:
            logger.error("save_blob failed (key=%s): %s", key, e)
            return None

    async def put_blob(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> bool:
        """Store raw bytes at an explicit ``key``.

        Unlike ``save_blob`` (content-addressed, dedup-by-hash), callers pick
        the key — used by the job log archive, where the key encodes
        pod + timestamp and is stamped onto job/thread rows for retrieval.
        """
        if not self._available or not data:
            return False
        try:
            await asyncio.to_thread(
                self._s3.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type or "application/octet-stream",
            )
            return True
        except Exception as e:
            logger.error("put_blob failed (key=%s): %s", key, e)
            return False

    async def get_blob(self, key: str) -> Optional[bytes]:
        """Fetch the raw bytes for a blob ``key``; ``None`` if missing/unavailable."""
        if not self._available or not key:
            return None
        try:
            resp = await asyncio.to_thread(
                self._s3.get_object, Bucket=self._bucket, Key=key
            )
            return await asyncio.to_thread(resp["Body"].read)
        except Exception as e:
            logger.debug("get_blob miss (key=%s): %s", key, e)
            return None

    async def upload_blob_file(self, key: str, local_path: str) -> bool:
        """Upload a local file to an arbitrary bucket key (staging tars).

        Unlike ``save_blob``, this targets an explicit ``key`` rather than a
        content-addressed one — callers (e.g. cloud_staging.stage) need a
        deterministic, overwritable location.
        """
        if not self._available:
            return False
        try:
            await asyncio.to_thread(self._s3.upload_file, local_path, self._bucket, key)
            return True
        except Exception as e:
            logger.error(f"S3 upload_blob_file failed for {key}: {e}")
            return False

    async def delete_blob(self, key: str) -> bool:
        """Delete the object at an arbitrary bucket ``key``."""
        if not self._available:
            return False
        try:
            await asyncio.to_thread(
                self._s3.delete_object, Bucket=self._bucket, Key=key
            )
            return True
        except Exception as e:
            logger.error(f"S3 delete_blob failed for {key}: {e}")
            return False

    async def capture_vm_snapshot(
        self,
        job_id: str,
        ssh_host: str,
        ssh_port: int,
        phase_number: Optional[int] = None,
        source_type: str = "vm",
        agent_config: str = "worker_base",
        entity_type: str = "jobs",
        work_marker: Optional[int] = None,
    ) -> bool:
        """Capture a VM environment snapshot via SSH tar and upload to S3.

        SSHs into the VM, tars key directories with zstd compression,
        streams to a local temp file, then uploads to S3.

        Args:
            job_id: Job or thread UUID.
            ssh_host: VM SSH host.
            ssh_port: VM SSH port.
            phase_number: Current phase number (for per-phase storage).
            source_type: "vm" or "pod".
            agent_config: Agent config name for manifest.
            entity_type: S3 prefix namespace ("jobs" or "threads").

        Returns:
            True if capture + upload succeeded.
        """
        if not self._available:
            return False

        from services.ssh_helpers import orchestrator_can_reach

        if not orchestrator_can_reach(ssh_host):
            # Tailnet target (VM workspace) — SSH from the orchestrator would
            # black-hole. Skip visibly instead of hanging on a doomed connect;
            # snapshots are not supported on the VM backend (see docs/issues/
            # vm_ssh_readiness_probe_unroutable_from_orchestrator.md).
            logger.info(
                "Skipping snapshot capture for %s %s (%s:%d): orchestrator "
                "has no route to tailnet targets",
                entity_type,
                job_id,
                ssh_host,
                ssh_port,
            )
            await self._set_snapshot_context(
                job_id,
                {
                    "status": "capture_skipped",
                    "error": "unroutable tailnet target from orchestrator",
                },
                entity_type=entity_type,
            )
            return False

        import tempfile

        await self._set_snapshot_context(
            job_id, {"status": "capturing"}, entity_type=entity_type
        )

        tar_path = None
        try:
            # Create temp file for the tarball
            with tempfile.NamedTemporaryFile(
                suffix=".tar.zst", delete=False, prefix=f"snapshot_{job_id[:8]}_"
            ) as tmp:
                tar_path = tmp.name

            # Directories to capture (exclude patterns handled by tar)
            include_dirs = [
                "/home/agent-host/",
                "/usr/local/",
            ]
            exclude_patterns = [
                # System/build caches
                "--exclude=/var/cache/*",
                "--exclude=/tmp/*",
                "--exclude=*.pyc",
                "--exclude=__pycache__",
                "--exclude=node_modules/.cache",
                "--exclude=.git/objects",
                # Workspace content re-cloned/regenerated on restore
                "--exclude=*/repos/*",
                "--exclude=*/node_modules/*",
            ]

            # Build SSH tar command. --xattrs/--acls so fuse-overlayfs opaque-dir
            # xattrs + whiteouts survive capture/restore (protected cloud mode,
            # design §11.3). The capture roots already EXCLUDE the merged overlay
            # mount (it lives at /cloud/merged, outside /home/agent-host) and
            # INCLUDE the upperdir at /home/agent-host/.overlay/upper.
            pipeline = (
                'tar --xattrs --xattrs-include="*" --acls -cf - '
                f"{' '.join(exclude_patterns)} {' '.join(include_dirs)} 2>/dev/null "
                "| zstd -1 -T0"
            )
            # A shell pipeline's exit code is only the LAST stage's (zstd) —
            # a failing/truncated tar upstream would be masked and a partial
            # archive accepted as good. bash -c makes PIPESTATUS available
            # (pipefail alone can't distinguish tar rc==1 from rc>=2) so both
            # stage codes collapse to one honest code: 0 = clean, 1 = tar
            # warned ("file changed as we read it" — routine on a live
            # workspace, NOT a failure), 2 = fatal (truncated tar or any
            # zstd failure). The -c body is single-quoted, so
            # --xattrs-include uses \"*\" (double quotes still block glob
            # expansion — tar still receives the literal '*'). The exclude/
            # include lists must never contain a single quote — the
            # command-shape test asserts this by counting quotes in the
            # final command.
            # PIPESTATUS must be snapshotted into an array in ONE command
            # before anything reads it: a bare assignment (e.g.
            # `__t=${PIPESTATUS[0]}`) is itself a simple command and
            # immediately resets PIPESTATUS to its own exit status, so a
            # second assignment reading PIPESTATUS[1] would see it already
            # clobbered (silently re-masking every zstd failure). Capturing
            # `"${PIPESTATUS[@]}"` into `__ps` first avoids that.
            tar_cmd = (
                "bash -c '" + pipeline + "; "
                '__ps=("${PIPESTATUS[@]}"); __t=${__ps[0]}; __z=${__ps[1]}; '
                'if [ "$__z" -ne 0 ] || [ "$__t" -ge 2 ]; then exit 2; '
                'elif [ "$__t" -eq 1 ]; then exit 1; else exit 0; fi\''
            )
            key_path = resolve_ssh_key_path()
            if not key_path:
                logger.warning(
                    "No SSH key available for snapshot capture (%s %s)",
                    entity_type.rstrip("s"),
                    job_id,
                )
            ssh_cmd = [
                "ssh",
                *(["-i", key_path] if key_path else []),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=10",
                "-p",
                str(ssh_port),
                f"agent-host@{ssh_host}",
                tar_cmd,
            ]

            # Run SSH tar → local file
            max_size = (
                int(os.environ.get("SNAPSHOT_MAX_SIZE_GB", "10")) * 1024 * 1024 * 1024
            )
            process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            total_bytes = 0
            with open(tar_path, "wb") as f:
                while True:
                    chunk = await process.stdout.read(1024 * 1024)  # 1 MB chunks
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > max_size:
                        process.kill()
                        logger.warning(
                            "Snapshot too large for %s %s (>%s GB), aborting",
                            entity_type.rstrip("s"),
                            job_id,
                            os.environ.get("SNAPSHOT_MAX_SIZE_GB", "10"),
                        )
                        await self._set_snapshot_context(
                            job_id,
                            {
                                "status": "capture_failed",
                                "error": "Snapshot exceeds size limit",
                            },
                            entity_type=entity_type,
                        )
                        return False
                    f.write(chunk)

            await process.wait()

            # Honest accept gate: tar rc==1 ("file changed as we read it") is
            # a routine warning on a live workspace, not a failure — only
            # rc>=2 (fatal tar error or any zstd failure, per the PIPESTATUS
            # discrimination above) or a totally empty stream mean the
            # archive is not trustworthy.
            if process.returncode >= 2 or total_bytes == 0:
                stderr = (await process.stderr.read()).decode(errors="replace")
                logger.error(
                    "Snapshot capture failed for %s %s (rc=%d, bytes=%d): %s",
                    entity_type.rstrip("s"),
                    job_id,
                    process.returncode,
                    total_bytes,
                    stderr[:500],
                )
                await self._set_snapshot_context(
                    job_id,
                    {
                        "status": "capture_failed",
                        "error": f"SSH tar/zstd failed (rc={process.returncode})",
                    },
                    entity_type=entity_type,
                )
                return False
            if process.returncode == 1:
                logger.warning(
                    "Snapshot capture for %s %s: tar reported files changed "
                    "during read (rc=1) — archive is complete, accepting",
                    entity_type.rstrip("s"),
                    job_id,
                )

            # Collect package manifests via SSH
            env_info = await self._collect_environment_info(ssh_host, ssh_port)

            # Build manifest
            manifest: dict[str, Any] = {
                "version": 1,
                "job_id": job_id,
                "source_type": source_type,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "agent_config": agent_config,
                "compression": "zstd",
                "size_compressed_bytes": total_bytes,
                "capture_method": "ssh_tar",
                "captured_paths": include_dirs,
                "environment": env_info,
                "restore": {
                    "min_cpu": 2,
                    "min_memory": "4Gi",
                    "disk_size": "20G",
                    "estimated_boot_seconds": 25,
                },
            }

            if phase_number is not None:
                manifest["phase_number"] = phase_number

            # Upload to S3
            uploaded = await self.upload_snapshot(
                job_id=job_id,
                tar_path=tar_path,
                manifest=manifest,
                phase_number=phase_number,
                entity_type=entity_type,
            )
            # Record the work-marker (turn count at capture) into the workspace
            # context so the lifecycle reaper's is_dirty can tell whether new
            # work has happened since this snapshot. Written under
            # workspace_container — the same key is_dirty reads.
            if uploaded and work_marker is not None and self._db is not None:
                marker = {"last_snapshot_turns": work_marker}
                try:
                    if entity_type == "threads":
                        await self._db.merge_thread_workspace_context(job_id, marker)
                    else:
                        await self._db.merge_workspace_container_context(job_id, marker)
                except Exception:
                    logger.exception("Failed to stamp work-marker for %s", job_id)
            return uploaded

        except Exception as e:
            logger.error(
                "Snapshot capture failed for %s %s: %s",
                entity_type.rstrip("s"),
                job_id,
                e,
                exc_info=True,
            )
            await self._set_snapshot_context(
                job_id,
                {
                    "status": "capture_failed",
                    "error": str(e),
                },
                entity_type=entity_type,
            )
            return False
        finally:
            # Clean up temp file
            if tar_path:
                try:
                    os.unlink(tar_path)
                except OSError:
                    pass

    async def _collect_environment_info(
        self, ssh_host: str, ssh_port: int
    ) -> dict[str, Any]:
        """Collect package manifests from the VM via SSH.

        Returns a dict suitable for the manifest's ``environment`` key.
        Non-fatal: returns partial info on failure.
        """
        info: dict[str, Any] = {}

        commands = {
            "pip_freeze": "pip freeze 2>/dev/null",
            "node_version": "node --version 2>/dev/null",
            "python_version": "python3 --version 2>/dev/null | awk '{print $2}'",
        }

        key_path = resolve_ssh_key_path()
        for key, cmd in commands.items():
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh",
                    *(["-i", key_path] if key_path else []),
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "ConnectTimeout=5",
                    "-p",
                    str(ssh_port),
                    f"agent-host@{ssh_host}",
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                output = stdout.decode(errors="replace").strip()
                if output:
                    if key == "pip_freeze":
                        info[key] = output.splitlines()
                    else:
                        info[key] = output
            except Exception:
                pass

        return info

    # =========================================================================
    # Download / Retrieve
    # =========================================================================

    async def get_manifest(
        self,
        job_id: str,
        phase_number: Optional[int] = None,
        entity_type: str = "jobs",
    ) -> Optional[dict[str, Any]]:
        """Retrieve the manifest for a snapshot.

        Args:
            job_id: Job or thread UUID.
            phase_number: If set, get the phase-specific manifest.
            entity_type: S3 prefix namespace ("jobs" or "threads").

        Returns:
            Parsed manifest dict, or None if not found.
        """
        if not self._available:
            return None

        if phase_number is not None:
            key = f"{entity_type}/{job_id}/phases/phase_{phase_number}/manifest.json"
        else:
            key = f"{entity_type}/{job_id}/manifest.json"

        try:
            response = await asyncio.to_thread(
                self._s3.get_object,
                Bucket=self._bucket,
                Key=key,
            )
            body = await asyncio.to_thread(response["Body"].read)
            return json.loads(body)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchKey":
                return None
            logger.error("Failed to get manifest for job %s: %s", job_id, e)
            return None

    async def download_snapshot(
        self,
        job_id: str,
        dest_path: str,
        phase_number: Optional[int] = None,
        entity_type: str = "jobs",
    ) -> bool:
        """Download a snapshot tarball from S3.

        Args:
            job_id: Job or thread UUID.
            dest_path: Local path to write the tarball.
            phase_number: If set, download the phase-specific snapshot.
            entity_type: S3 prefix namespace ("jobs" or "threads").

        Returns:
            True if download succeeded.
        """
        if not self._available:
            return False

        if phase_number is not None:
            key = f"{entity_type}/{job_id}/phases/phase_{phase_number}/env.tar.zst"
        else:
            key = f"{entity_type}/{job_id}/env.tar.zst"

        try:
            await asyncio.to_thread(
                self._s3.download_file,
                self._bucket,
                key,
                dest_path,
            )
            return True
        except ClientError as e:
            logger.error("Failed to download snapshot for job %s: %s", job_id, e)
            return False

    async def list_phase_snapshots(self, job_id: str) -> list[dict[str, Any]]:
        """List all phase snapshots for a job.

        Returns:
            List of manifest dicts, sorted by phase number.
        """
        if not self._available:
            return []

        prefix = f"jobs/{job_id}/phases/"
        manifests = []

        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self._bucket, Prefix=prefix)

            for page in await asyncio.to_thread(lambda: list(pages)):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith("manifest.json"):
                        try:
                            manifest = await self.get_manifest(
                                job_id,
                                phase_number=int(
                                    obj["Key"].split("/phase_")[1].split("/")[0]
                                ),
                            )
                            if manifest:
                                manifests.append(manifest)
                        except (ValueError, IndexError):
                            pass

        except ClientError as e:
            logger.error("Failed to list phase snapshots for job %s: %s", job_id, e)

        return sorted(manifests, key=lambda m: m.get("phase_number", 0))

    # =========================================================================
    # Verify (C2 — reclaim gate)
    # =========================================================================

    async def verify_snapshot(
        self,
        job_id: str,
        *,
        entity_type: str = "jobs",
        deep: Optional[bool] = None,
    ) -> tuple[bool, str]:
        """Confirm a snapshot is durably and correctly stored in S3.

        The single gate a later task (C4) calls before the irreversible
        ``delete_workspace_pvc`` — FAIL-SAFE by design: anything
        unverifiable (no manifest, missing object, size mismatch, deep
        verify with no stored checksum, hash mismatch) returns
        ``(False, reason)``; only an all-good result returns
        ``(True, "ok")``. A buggy ``True`` here would let a caller destroy
        the only remaining copy of unrecoverable data.

        Two checks:
          - size (always, cheap): ``HeadObject`` content-length vs.
            ``manifest.size_compressed_bytes`` (skipped if the manifest
            doesn't carry a size).
          - hash (when ``deep``, default from ``SNAPSHOT_VERIFY_DEEP``): a
            streamed re-hash of the S3 object compared against
            ``manifest.checksum_sha256`` — this is the only integrity
            oracle. Never compare the object's ``ETag``: these archives are
            multipart-uploaded, so the ETag is
            ``md5(concat(part-md5s))-N``, not the object's hash.

        Args:
            job_id: Job or thread UUID.
            entity_type: S3 prefix namespace ("jobs" or "threads").
            deep: Force the hash re-check on/off. ``None`` (default) reads
                ``SNAPSHOT_VERIFY_DEEP`` (default ``"true"``).

        Returns:
            ``(ok, reason)`` — ``reason`` is always a short human-readable
            string, e.g. ``"ok"``, ``"no manifest"``, ``"object missing"``.
        """
        if not self._available:
            return False, "s3 unavailable"

        manifest = await self.get_manifest(job_id, entity_type=entity_type)
        if not manifest:
            return False, "no manifest"

        want_sha = manifest.get("checksum_sha256")
        want_size = manifest.get("size_compressed_bytes")
        key = f"{entity_type}/{job_id}/env.tar.zst"

        try:
            head = await asyncio.to_thread(
                self._s3.head_object, Bucket=self._bucket, Key=key
            )
        except ClientError:
            return False, "object missing"

        if want_size and head["ContentLength"] != want_size:
            return False, (
                f"size mismatch (s3={head['ContentLength']} manifest={want_size})"
            )

        if deep is None:
            deep = os.environ.get("SNAPSHOT_VERIFY_DEEP", "true").lower() == "true"

        if deep:
            if not want_sha:
                # FAIL-SAFE: no stored checksum to compare against means
                # this snapshot can't be proven intact — never treat
                # "nothing to check" as "checked and fine".
                return False, "no checksum in manifest (unverifiable)"
            got = await asyncio.to_thread(self._streaming_sha256_from_s3, key)
            if got != want_sha:
                return False, "sha256 mismatch"

        return True, "ok"

    # =========================================================================
    # Delete / GC
    # =========================================================================

    async def delete_snapshot(self, job_id: str, entity_type: str = "jobs") -> bool:
        """Delete all snapshots for an entity from S3.

        Returns:
            True if deletion succeeded (or nothing to delete).
        """
        if not self._available:
            return False

        prefix = f"{entity_type}/{job_id}/"

        try:
            # List all objects under the job prefix
            paginator = self._s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self._bucket, Prefix=prefix)

            objects_to_delete = []
            for page in await asyncio.to_thread(lambda: list(pages)):
                for obj in page.get("Contents", []):
                    objects_to_delete.append({"Key": obj["Key"]})

            if not objects_to_delete:
                return True

            # Delete in batches of 1000 (S3 limit)
            for i in range(0, len(objects_to_delete), 1000):
                batch = objects_to_delete[i : i + 1000]
                await asyncio.to_thread(
                    self._s3.delete_objects,
                    Bucket=self._bucket,
                    Delete={"Objects": batch},
                )

            # Clear snapshot context
            await self._set_snapshot_context(
                job_id, {"status": "deleted"}, entity_type=entity_type
            )

            logger.info(
                "Deleted %d snapshot objects for %s %s",
                len(objects_to_delete),
                entity_type.rstrip("s"),
                job_id,
            )
            return True

        except ClientError as e:
            logger.error("Failed to delete snapshot for job %s: %s", job_id, e)
            return False

    async def get_snapshot_status(self, job_id: str) -> dict[str, Any]:
        """Get snapshot status for a job, combining DB context and S3.

        Returns:
            Dict with status, source_type, created_at, size, pinned, etc.
        """
        if not self._db:
            return {"status": "unavailable"}

        try:
            job = await self._db.get_job(job_id)
            if not job:
                return {"status": "unavailable"}

            ctx = job.get("context") or {}
            if isinstance(ctx, str):
                ctx = json.loads(ctx)

            snapshot_ctx = ctx.get("snapshot", {})
            status = snapshot_ctx.get("status", "none")

            if status in ("available", "capturing"):
                # Enrich with manifest data from S3 if available
                manifest = await self.get_manifest(job_id)
                if manifest:
                    return {
                        "status": status,
                        "source_type": manifest.get("source_type", "unknown"),
                        "created_at": manifest.get("created_at"),
                        "size_compressed_bytes": manifest.get("size_compressed_bytes"),
                        "pinned": snapshot_ctx.get("pinned", False),
                        "phase_number": snapshot_ctx.get("phase_number"),
                        "checksum_sha256": manifest.get("checksum_sha256"),
                        "environment_summary": {
                            "python_packages": len(
                                manifest.get("environment", {}).get("pip_freeze", [])
                            ),
                            "python_version": manifest.get("environment", {}).get(
                                "python_version"
                            ),
                            "node_version": manifest.get("environment", {}).get(
                                "node_version"
                            ),
                        },
                    }

            return {
                "status": status if status != "none" else "unavailable",
                "error": snapshot_ctx.get("error"),
            }

        except Exception as e:
            logger.error("Failed to get snapshot status for job %s: %s", job_id, e)
            return {"status": "unavailable", "error": str(e)}

    # =========================================================================
    # Pin
    # =========================================================================

    async def toggle_pin(self, job_id: str) -> bool:
        """Toggle pin state on a snapshot. Returns new pinned value."""
        if not self._db:
            return False

        job = await self._db.get_job(job_id)
        if not job:
            return False

        ctx = job.get("context") or {}
        if isinstance(ctx, str):
            ctx = json.loads(ctx)

        current = ctx.get("snapshot", {}).get("pinned", False)
        new_value = not current
        await self._set_snapshot_context(job_id, {"pinned": new_value})
        return new_value

    # =========================================================================
    # Garbage Collection
    # =========================================================================

    async def run_gc(self) -> dict[str, int]:
        """Run snapshot garbage collection.

        1. List all job snapshots in S3
        2. Cross-reference with job records in PostgreSQL
        3. Apply retention rules (age, pin status)
        4. Move expired snapshots to gc/pending_delete/
        5. Purge items in pending_delete/ older than 7 days

        Returns:
            Dict with counts: soft_deleted, purged, errors.
        """
        if not self._available or not self._db:
            return {"soft_deleted": 0, "purged": 0, "errors": 0}

        retention_days = int(os.environ.get("SNAPSHOT_RETENTION_DAYS", "90"))
        grace_days = 7
        now = datetime.now(timezone.utc)
        stats = {"soft_deleted": 0, "purged": 0, "errors": 0}

        try:
            # Phase 1: Soft-delete expired snapshots
            await self._gc_soft_delete(now, retention_days, stats)

            # Phase 2: Purge items past the grace period
            await self._gc_purge(now, grace_days, stats)

        except Exception:
            logger.exception("Error during snapshot GC")
            stats["errors"] += 1

        if stats["soft_deleted"] or stats["purged"]:
            logger.info(
                "Snapshot GC complete: soft_deleted=%d, purged=%d, errors=%d",
                stats["soft_deleted"],
                stats["purged"],
                stats["errors"],
            )

        return stats

    async def _gc_soft_delete(
        self, now: datetime, retention_days: int, stats: dict[str, int]
    ) -> None:
        """Move expired snapshots to gc/pending_delete/."""
        cutoff = now - timedelta(days=retention_days)

        # List all job snapshot manifests
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self._bucket, Prefix="jobs/")

            for page in await asyncio.to_thread(lambda: list(pages)):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    # Only check top-level manifests (not phase sub-manifests)
                    if not key.endswith("/manifest.json") or "/phases/" in key:
                        continue

                    # Extract job_id from key: jobs/<uuid>/manifest.json
                    parts = key.split("/")
                    if len(parts) < 3:
                        continue
                    job_id = parts[1]

                    try:
                        # Check if pinned
                        job = await self._db.get_job(job_id)
                        if job:
                            ctx = job.get("context") or {}
                            if isinstance(ctx, str):
                                ctx = json.loads(ctx)
                            if ctx.get("snapshot", {}).get("pinned", False):
                                continue

                        # Check age from manifest
                        manifest = await self.get_manifest(job_id)
                        if not manifest:
                            continue

                        created_at = manifest.get("created_at", "")
                        if not created_at:
                            continue

                        created = datetime.fromisoformat(created_at)
                        if created > cutoff:
                            continue

                        # Soft-delete: move to gc/pending_delete/
                        await self._soft_delete_snapshot(job_id, now)
                        stats["soft_deleted"] += 1

                    except Exception as e:
                        logger.warning("GC error for job %s: %s", job_id, e)
                        stats["errors"] += 1

        except ClientError as e:
            logger.error("GC listing failed: %s", e)
            stats["errors"] += 1

    async def _gc_purge(
        self, now: datetime, grace_days: int, stats: dict[str, int]
    ) -> None:
        """Purge items in gc/pending_delete/ past the grace period."""
        cutoff = now - timedelta(days=grace_days)

        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self._bucket, Prefix="gc/pending_delete/")

            objects_to_purge = []
            for page in await asyncio.to_thread(lambda: list(pages)):
                for obj in page.get("Contents", []):
                    if (
                        obj.get("LastModified")
                        and obj["LastModified"].replace(tzinfo=timezone.utc) < cutoff
                    ):
                        objects_to_purge.append({"Key": obj["Key"]})

            if not objects_to_purge:
                return

            for i in range(0, len(objects_to_purge), 1000):
                batch = objects_to_purge[i : i + 1000]
                await asyncio.to_thread(
                    self._s3.delete_objects,
                    Bucket=self._bucket,
                    Delete={"Objects": batch},
                )

            stats["purged"] += len(objects_to_purge)

        except ClientError as e:
            logger.error("GC purge failed: %s", e)
            stats["errors"] += 1

    async def _soft_delete_snapshot(self, job_id: str, now: datetime) -> None:
        """Move a job's snapshots from jobs/ to gc/pending_delete/."""
        src_prefix = f"jobs/{job_id}/"
        dst_prefix = f"gc/pending_delete/{job_id}/"

        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self._bucket, Prefix=src_prefix)

            for page in await asyncio.to_thread(lambda: list(pages)):
                for obj in page.get("Contents", []):
                    src_key = obj["Key"]
                    dst_key = src_key.replace(src_prefix, dst_prefix, 1)

                    # Copy to gc/
                    await asyncio.to_thread(
                        self._s3.copy_object,
                        Bucket=self._bucket,
                        CopySource={"Bucket": self._bucket, "Key": src_key},
                        Key=dst_key,
                    )

                    # Delete original
                    await asyncio.to_thread(
                        self._s3.delete_object,
                        Bucket=self._bucket,
                        Key=src_key,
                    )

            # Update job context
            await self._set_snapshot_context(
                job_id,
                {
                    "status": "gc_deleted",
                    "gc_deleted_at": now.isoformat(),
                },
            )

            logger.info("Soft-deleted snapshot for job %s", job_id)

        except ClientError as e:
            logger.error("Soft-delete failed for job %s: %s", job_id, e)
            raise

    # =========================================================================
    # Storage Stats
    # =========================================================================

    async def get_storage_stats(self) -> dict[str, Any]:
        """Get aggregate snapshot storage statistics.

        Returns:
            Dict with total_snapshots, total_size_bytes, gc_pending_count, etc.
        """
        if not self._available:
            return {"available": False}

        stats: dict[str, Any] = {
            "available": True,
            "total_snapshots": 0,
            "total_size_bytes": 0,
            "gc_pending_count": 0,
            "gc_pending_size_bytes": 0,
        }

        try:
            paginator = self._s3.get_paginator("list_objects_v2")

            # Count active snapshots
            pages = paginator.paginate(Bucket=self._bucket, Prefix="jobs/")
            for page in await asyncio.to_thread(lambda: list(pages)):
                for obj in page.get("Contents", []):
                    if (
                        obj["Key"].endswith("/manifest.json")
                        and "/phases/" not in obj["Key"]
                    ):
                        stats["total_snapshots"] += 1
                    stats["total_size_bytes"] += obj.get("Size", 0)

            # Count GC pending
            pages = paginator.paginate(Bucket=self._bucket, Prefix="gc/pending_delete/")
            for page in await asyncio.to_thread(lambda: list(pages)):
                for obj in page.get("Contents", []):
                    stats["gc_pending_count"] += 1
                    stats["gc_pending_size_bytes"] += obj.get("Size", 0)

        except ClientError as e:
            logger.error("Failed to get storage stats: %s", e)
            stats["error"] = str(e)

        return stats

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _set_snapshot_context(
        self, entity_id: str, updates: dict, entity_type: str = "jobs"
    ) -> None:
        """Atomically merge updates into the entity's snapshot context key."""
        if not self._db:
            return

        try:
            if entity_type == "threads":
                await self._db.merge_thread_snapshot_context(entity_id, updates)
            else:
                await self._db.merge_snapshot_context(entity_id, updates)
        except Exception:
            logger.exception(
                "Failed to update snapshot context for %s %s",
                entity_type.rstrip("s"),
                entity_id,
            )

    @staticmethod
    def _compute_sha256(file_path: str) -> str:
        """Compute SHA-256 hash of a file (synchronous, run in thread)."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _streaming_sha256_from_s3(self, key: str) -> str:
        """SHA-256 of an S3 object, streamed in 1 MB chunks (O(1) memory).

        Synchronous (run via ``asyncio.to_thread``) — mirrors
        ``_compute_sha256`` but reads the S3 object body instead of a local
        file, so a possibly multi-GB object is never buffered in memory at
        once. Backs ``verify_snapshot``'s deep check; never use the
        object's ``ETag`` in its place — these archives are
        multipart-uploaded, so the ETag is ``md5(concat(part-md5s))-N``,
        not the object's hash.
        """
        h = hashlib.sha256()
        resp = self._s3.get_object(Bucket=self._bucket, Key=key)
        body = resp["Body"]
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            h.update(chunk)
        return h.hexdigest()


# Module-level singleton
snapshot_service = SnapshotService()
