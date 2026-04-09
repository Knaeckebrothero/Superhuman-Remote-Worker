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
            if not BOTO3_AVAILABLE:
                logger.info("Snapshot service: boto3 not installed — disabled")
            else:
                logger.info("Snapshot service: S3_ENDPOINT not set — disabled")
            return

        access_key = os.environ.get("S3_ACCESS_KEY", "")
        secret_key = os.environ.get("S3_SECRET_KEY", "")
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

    async def capture_vm_snapshot(
        self,
        job_id: str,
        ssh_host: str,
        ssh_port: int,
        phase_number: Optional[int] = None,
        source_type: str = "vm",
        agent_config: str = "defaults",
        entity_type: str = "jobs",
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

            # Build SSH tar command
            tar_cmd = (
                f"tar -cf - {' '.join(exclude_patterns)} {' '.join(include_dirs)} 2>/dev/null"
                " | zstd -1 -T0"
            )
            ssh_cmd = [
                "ssh",
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

            if process.returncode != 0 and total_bytes == 0:
                stderr = (await process.stderr.read()).decode(errors="replace")
                logger.error(
                    "SSH tar failed for %s %s: %s",
                    entity_type.rstrip("s"),
                    job_id,
                    stderr[:500],
                )
                await self._set_snapshot_context(
                    job_id,
                    {
                        "status": "capture_failed",
                        "error": f"SSH tar failed (rc={process.returncode})",
                    },
                    entity_type=entity_type,
                )
                return False

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
            return await self.upload_snapshot(
                job_id=job_id,
                tar_path=tar_path,
                manifest=manifest,
                phase_number=phase_number,
                entity_type=entity_type,
            )

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

        for key, cmd in commands.items():
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh",
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


# Module-level singleton
snapshot_service = SnapshotService()
