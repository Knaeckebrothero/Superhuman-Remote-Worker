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
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from orchestrator.services.cloud_staging.manifest import (
    MAX_MANIFEST_JSON_BYTES,
    derive_manifest,
)
from orchestrator.services.cloud_staging.source_identity import (
    ProtectedMountSourceIdentity,
)
from orchestrator.services.blocking_effect import joined_blocking_call
from orchestrator.services.ssh_helpers import _scan_pinned_host_key, build_agent_ssh_cmd
from orchestrator.services.subprocess_effect import (
    SubprocessOutputLimit,
    communicate_bounded,
)
from orchestrator.services.vm_remote_operation import (
    VMRemoteOperationLease,
    VMRemoteOperationUnavailable,
    claim_vm_remote_operation,
)
from shared.workspace_contract import (
    vm_mode_from_env,
    workspace_contract_authority_identity,
)

logger = logging.getLogger(__name__)

# sha256 of empty input — what ``stage_signature_cmd()`` hashes to when the
# upperdir has no entries (nothing staged, or everything since undone).
_EMPTY_SIGNATURE = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# Hard cap on the streamed upperdir tar — above the 8 GiB upperdir quota
# (main.py:_PROTECTED_UPPERDIR_QUOTA_BYTES) so a runaway session aborts the
# stage instead of ballooning the orchestrator's local disk.
_STAGE_MAX_BYTES = 9 * 1024**3

_SSH_CAPTURE_TIMEOUT_S = 30.0
_SSH_STREAM_TIMEOUT_S = 30 * 60.0
_SSH_CHILD_TERM_GRACE_S = 0.5
_SSH_CHILD_KILL_GRACE_S = 2.0
_SSH_STDERR_TAIL_BYTES = 16 * 1024
_SSH_CAPTURE_STDOUT_BYTES = 4096

# Per-thread debounce: threads currently mid-stage. See module docstring.
_inflight: set[str] = set()


def staging_tar_key(
    thread_id: str, staged_summary: dict[str, Any] | None = None
) -> str:
    if isinstance(staged_summary, dict) and isinstance(
        staged_summary.get("tar_key"), str
    ):
        return staged_summary["tar_key"]
    return f"cloud-staging/{thread_id}/upper.tar"


def staging_manifest_key(
    thread_id: str, staged_summary: dict[str, Any] | None = None
) -> str:
    if isinstance(staged_summary, dict) and isinstance(
        staged_summary.get("manifest_key"), str
    ):
        return staged_summary["manifest_key"]
    return f"cloud-staging/{thread_id}/manifest.json"


def immutable_staging_keys(
    *,
    thread_id: str,
    runtime_generation: str,
    workspace_generation: str,
    epoch: int,
    source_binding_sha256: str,
    tar_sha256: str,
    claim_token: int | None = None,
) -> tuple[str, str]:
    """Content-addressed keys prevent a stale PUT overwriting a successor."""

    generation = str(UUID(runtime_generation))
    workspace = str(UUID(workspace_generation))
    if len(source_binding_sha256) != 64 or any(
        ch not in "0123456789abcdef" for ch in source_binding_sha256
    ):
        raise ValueError("staging source digest is malformed")
    if len(tar_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in tar_sha256):
        raise ValueError("staging tar digest is malformed")
    claim_component = ""
    if claim_token is not None:
        if isinstance(claim_token, bool) or int(claim_token) <= 0:
            raise ValueError("staging claim token is malformed")
        claim_component = f"/vm-claim-{int(claim_token)}"
    prefix = (
        f"cloud-staging/{thread_id}/{generation}/{workspace}/"
        f"{int(epoch)}/{source_binding_sha256}{claim_component}/{tar_sha256}"
    )
    return f"{prefix}/upper.tar", f"{prefix}/manifest.json"


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


def _resolve_workspace_ssh(
    metadata: dict, *, allow_vm_suspending: bool = False
) -> tuple[str, int] | None:
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

    selected = workspace_contract_authority_identity(
        {
            "context": metadata,
            "config_override": metadata.get("config_override"),
        },
        vm_mode=vm_mode_from_env(),
        allow_vm_suspending=allow_vm_suspending,
    )
    if selected is None:
        # Compatibility for the historical local/container-only capture path.
        # Never perform the same fallback to VM coordinates: VM remote effects
        # require a selected contract and a durable receipt.
        configured = (
            (metadata.get("config_override") or {}).get("workspace") or {}
        ).get("backend")
        if configured is not None or vm_ctx:
            return None
        host = ws_ctx.get("pod_ip") or ws_ctx.get("host")
        if not host:
            return None
        return str(host), int(ws_ctx.get("port") or 30022)
    if selected[0] == "vm":
        host = vm_ctx.get("ssh_host")
        if not host:
            return None
        return str(host), int(vm_ctx.get("ssh_port") or 22)
    if selected[0] != "sandbox":
        return None

    host = ws_ctx.get("pod_ip") or ws_ctx.get("host")
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


def _sha256_file(path: str) -> str:
    """Streamed sha256 of a file in 1 MiB chunks (synchronous, run in a
    thread) — the staged tar can be GBs, so never read it whole into memory."""
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


# =============================================================================
# Subprocess seams (monkeypatched in tests — see module docstring on why)
# =============================================================================


async def _bounded_stop_and_reap(process: asyncio.subprocess.Process) -> None:
    """Terminate then kill an owned SSH child, deferring cancellation."""

    async def _stop() -> None:
        if process.returncode is not None:
            await process.wait()
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), _SSH_CHILD_TERM_GRACE_S)
            return
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), _SSH_CHILD_KILL_GRACE_S)
        except (asyncio.TimeoutError, ProcessLookupError):
            logger.error("stage: SSH child did not reap within the bounded cleanup")

    cleanup = asyncio.create_task(_stop())
    cancellation: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancellation = exc
    cleanup.result()
    if cancellation is not None:
        raise cancellation


async def _stop_preserving_primary(
    process: asyncio.subprocess.Process, primary: BaseException
) -> None:
    try:
        await _bounded_stop_and_reap(process)
    except BaseException as cleanup_error:
        if not isinstance(cleanup_error, asyncio.CancelledError):
            logger.warning(
                "stage: SSH cleanup failed while preserving %s",
                type(primary).__name__,
                exc_info=True,
            )


async def _spawn_owned_ssh_child(cmd: list[str]) -> asyncio.subprocess.Process:
    """Do not let cancellation between fork and return orphan the child."""

    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )
    cancellation: asyncio.CancelledError | None = None
    while not spawn.done():
        try:
            await asyncio.shield(spawn)
        except asyncio.CancelledError as exc:
            cancellation = exc
    try:
        process = spawn.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation
        raise
    if cancellation is not None:
        await _stop_preserving_primary(process, cancellation)
        raise cancellation
    return process


async def _stderr_tail(reader: asyncio.StreamReader) -> bytes:
    tail = bytearray()
    while True:
        chunk = await reader.read(64 * 1024)
        if not chunk:
            return bytes(tail)
        tail.extend(chunk)
        if len(tail) > _SSH_STDERR_TAIL_BYTES:
            del tail[: len(tail) - _SSH_STDERR_TAIL_BYTES]


async def _run_ssh_capture(cmd: list[str], *, timeout: float) -> bytes | None:
    """Run ``cmd``, capturing stdout. Returns ``None`` on spawn/timeout/rc!=0."""
    try:
        proc = await _spawn_owned_ssh_child(cmd)
    except OSError as e:
        logger.error("stage: failed to spawn ssh capture: %s", e)
        return None

    try:
        stdout, stderr = await communicate_bounded(
            proc,
            timeout=timeout,
            stdout_limit=_SSH_CAPTURE_STDOUT_BYTES,
            stderr_limit=_SSH_STDERR_TAIL_BYTES,
        )
    except TimeoutError:
        logger.warning("stage: ssh capture timed out after %ss", timeout)
        return None
    except SubprocessOutputLimit:
        logger.warning("stage: ssh capture exceeded its output cap")
        return None
    except BaseException as exc:
        await _stop_preserving_primary(proc, exc)
        raise

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
        process = await _spawn_owned_ssh_child(cmd)
    except OSError as e:
        logger.error("stage: failed to spawn ssh tar: %s", e)
        return False

    stderr_task = asyncio.create_task(_stderr_tail(process.stderr))
    total_bytes = 0
    succeeded = False
    try:
        async with asyncio.timeout(_SSH_STREAM_TIMEOUT_S):
            with open(dest_path, "wb") as f:
                while True:
                    chunk = await process.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > _STAGE_MAX_BYTES:
                        await _bounded_stop_and_reap(process)
                        await stderr_task
                        logger.warning(
                            "stage: upperdir tar exceeds cap (%d bytes), aborting",
                            _STAGE_MAX_BYTES,
                        )
                        return False
                    f.write(chunk)
            await process.wait()
        stderr = (await stderr_task).decode(errors="replace")
        if process.returncode != 0:
            logger.error(
                "stage: ssh tar failed (rc=%s): %s", process.returncode, stderr[-500:]
            )
            return False
        succeeded = True
        return True
    except TimeoutError:
        await _bounded_stop_and_reap(process)
        await asyncio.gather(stderr_task, return_exceptions=True)
        logger.warning("stage: SSH tar stream timed out")
        return False
    except BaseException as exc:
        await _stop_preserving_primary(process, exc)
        await asyncio.gather(stderr_task, return_exceptions=True)
        raise
    finally:
        if not succeeded:
            try:
                os.unlink(dest_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("stage: could not remove partial tar")


async def _run_authorized_ssh_capture(
    authority: dict[str, Any] | None,
    *,
    ssh_host: str,
    ssh_port: int,
    remote_cmd: str,
) -> bytes | None:
    """Run one read against the exact attested workspace host key."""

    if authority is None:
        return await _run_ssh_capture(
            build_agent_ssh_cmd(ssh_host, ssh_port, remote_cmd),
            timeout=_SSH_CAPTURE_TIMEOUT_S,
        )
    fingerprint = str(authority.get("workspace_ssh_host_key_fingerprint") or "")
    known_host, error = await _scan_pinned_host_key(ssh_host, ssh_port, fingerprint)
    if known_host is None:
        logger.warning("stage: pinned SSH host-key refusal: %s", error[:200])
        return None
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="ascii", prefix="stage-known-host-", delete=True
    ) as known_hosts:
        known_hosts.write(known_host + "\n")
        known_hosts.flush()
        return await _run_ssh_capture(
            build_agent_ssh_cmd(
                ssh_host,
                ssh_port,
                remote_cmd,
                known_hosts_path=known_hosts.name,
                batch_mode=True,
            ),
            timeout=_SSH_CAPTURE_TIMEOUT_S,
        )


async def _stream_authorized_tar(
    authority: dict[str, Any] | None,
    *,
    ssh_host: str,
    ssh_port: int,
    dest_path: str,
) -> bool:
    if authority is None:
        return await _stream_tar_to_file(
            build_agent_ssh_cmd(ssh_host, ssh_port, stage_tar_cmd()), dest_path
        )
    fingerprint = str(authority.get("workspace_ssh_host_key_fingerprint") or "")
    known_host, error = await _scan_pinned_host_key(ssh_host, ssh_port, fingerprint)
    if known_host is None:
        logger.warning("stage: pinned SSH host-key refusal: %s", error[:200])
        return False
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="ascii", prefix="stage-known-host-", delete=True
    ) as known_hosts:
        known_hosts.write(known_host + "\n")
        known_hosts.flush()
        return await _stream_tar_to_file(
            build_agent_ssh_cmd(
                ssh_host,
                ssh_port,
                stage_tar_cmd(),
                known_hosts_path=known_hosts.name,
                batch_mode=True,
            ),
            dest_path,
        )


def _authority_matches(
    authority: dict[str, Any], thread: dict[str, Any] | None, row: dict[str, Any] | None
) -> bool:
    if not thread or not row:
        return False
    metadata = _parse_metadata(thread)
    return bool(
        str(thread.get("runtime_generation") or "")
        == str(authority.get("runtime_generation") or "")
        and str(thread.get("runtime_retirement_token") or "")
        == str(authority.get("runtime_retirement_token") or "")
        and str(thread.get("agent_id") or "") == str(authority.get("agent_id") or "")
        and str(thread.get("runtime_attach_token") or "")
        == str(authority.get("runtime_attach_token") or "")
        and (metadata.get("workspace_container") or {})
        == (authority.get("workspace") or {})
        and (metadata.get("_workspace_binding") or {})
        == (authority.get("workspace_binding") or {})
        and str(row.get("id") or "") == str(authority.get("mount_row_id") or "")
        and str(row.get("runtime_generation") or "")
        == str(authority.get("runtime_generation") or "")
        and str(row.get("engage_attempt") or "")
        == str(authority.get("engage_attempt") or "")
        and str(row.get("source_binding_sha256") or "")
        == str(authority.get("source_binding_sha256") or "")
        and int(row.get("staged_epoch") or 0)
        == int(authority.get("expected_staged_epoch") or 0)
        and str(row.get("status") or "") == "active"
    )


async def _still_authorized(
    postgres_db: Any, thread_id: str, authority: dict[str, Any] | None
) -> bool:
    if authority is None:
        return True
    thread, row = await asyncio.gather(
        postgres_db.get_thread(thread_id),
        postgres_db.get_ro_mount_by_thread(thread_id),
    )
    return _authority_matches(authority, thread, row)


# =============================================================================
# Entry point
# =============================================================================


async def stage_thread_cloud_diff(
    *,
    thread_id: str,
    postgres_db: Any,
    snapshot_service: Any,
    authority: dict[str, Any] | None = None,
    vm_provisioner: Any = None,
) -> dict | None:
    """Stage the protected session's upperdir diff to S3, if it changed.

    Returns ``{"epoch": int, "counts": {...}}`` on a successful push,
    ``{"skipped": "<reason>"}`` on a no-op (``not_protected`` /
    ``no_active_mount`` / ``no_workspace`` / ``in_flight`` / ``unchanged`` /
    ``empty``), or ``None`` on hard failure (logged, never raises).
    """
    inflight_key = (
        thread_id
        if authority is None
        else ":".join(
            (
                thread_id,
                str(authority.get("runtime_generation") or ""),
                str(authority.get("workspace_generation") or ""),
                str(authority.get("expected_staged_epoch") or ""),
            )
        )
    )
    if inflight_key in _inflight:
        return {"skipped": "in_flight"}

    _inflight.add(inflight_key)
    try:
        return await _stage_thread_cloud_diff(
            thread_id=thread_id,
            postgres_db=postgres_db,
            snapshot_service=snapshot_service,
            authority=authority,
            vm_provisioner=vm_provisioner,
        )
    except Exception:
        logger.exception("stage: unhandled error staging thread %s", thread_id)
        return None
    finally:
        _inflight.discard(inflight_key)


async def _stage_thread_cloud_diff(
    *,
    thread_id: str,
    postgres_db: Any,
    snapshot_service: Any,
    authority: dict[str, Any] | None = None,
    vm_provisioner: Any = None,
    _vm_lease: VMRemoteOperationLease | None = None,
) -> dict | None:
    thread = await postgres_db.get_thread(thread_id)
    metadata = _parse_metadata(thread)
    if not metadata.get("protected_cloud"):
        return {"skipped": "not_protected"}

    contract_identity = workspace_contract_authority_identity(
        {
            "context": metadata,
            "config_override": metadata.get("config_override"),
        },
        vm_mode=vm_mode_from_env(),
        allow_vm_suspending=True,
    )
    is_vm = bool(contract_identity and contract_identity[0] == "vm")
    if is_vm and _vm_lease is None:
        if vm_provisioner is None:
            return {"skipped": "vm_capture_authority_unavailable"}
        try:
            lease = await claim_vm_remote_operation(
                db=postgres_db,
                provisioner=vm_provisioner,
                owner_id=thread_id,
                owner_kind="thread",
                operation_kind="cloud_stage",
            )
            async with lease:
                return await _stage_thread_cloud_diff(
                    thread_id=thread_id,
                    postgres_db=postgres_db,
                    snapshot_service=snapshot_service,
                    authority=authority,
                    vm_provisioner=vm_provisioner,
                    _vm_lease=lease,
                )
        except VMRemoteOperationUnavailable:
            return {"skipped": "vm_capture_authority_unavailable"}

    async def _vm_authorized() -> bool:
        if _vm_lease is None:
            return True
        target = await _vm_lease.revalidate()
        return target is not None

    row = await postgres_db.get_ro_mount_by_thread(thread_id)
    if not row or row.get("status") != "active":
        return {"skipped": "no_active_mount"}
    source = ProtectedMountSourceIdentity.from_binding(
        row.get("source_binding"),
        expected_sha256=str(row.get("source_binding_sha256") or ""),
    )
    if source is None:
        return {"skipped": "source_authority_invalid"}

    if authority is not None and not _authority_matches(authority, thread, row):
        return {"skipped": "authority_changed"}
    resolved = _resolve_workspace_ssh(metadata, allow_vm_suspending=True)
    if resolved is None:
        return {"skipped": "no_workspace"}
    ssh_host, ssh_port = resolved

    if _vm_lease is not None and (
        ssh_host,
        ssh_port,
    ) != (_vm_lease.identity.ssh_host, _vm_lease.identity.ssh_port):
        return {"skipped": "vm_capture_authority_changed"}
    ssh_authority = authority
    if _vm_lease is not None:
        if not await _vm_authorized():
            return {"skipped": "vm_capture_authority_changed"}
        ssh_authority = {
            "workspace_ssh_host_key_fingerprint": (
                _vm_lease.identity.ssh_host_key_fingerprint
            )
        }

    raw = await _run_authorized_ssh_capture(
        ssh_authority,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        remote_cmd=stage_signature_cmd(),
    )
    if raw is None:
        logger.warning(
            "stage: signature probe failed for thread %s (%s:%d)",
            thread_id,
            ssh_host,
            ssh_port,
        )
        return None
    signature = raw.decode(errors="replace").strip()
    if not await _vm_authorized():
        return {"skipped": "vm_capture_authority_changed"}

    if signature == _EMPTY_SIGNATURE:
        if authority is not None:
            # Retirement needs an append-once publication receipt even when
            # there are no bytes to review.  Advancing the epoch makes a
            # crash after workspace cleanup distinguishable from a crash
            # before the final stage; no blob is deleted or overwritten.
            published = await postgres_db.publish_ro_mount_staging_exact(
                row["id"],
                thread_id=thread_id,
                expected_runtime_generation=authority["runtime_generation"],
                expected_retirement_token=authority.get("runtime_retirement_token"),
                expected_agent_id=authority.get("agent_id"),
                expected_attach_token=authority.get("runtime_attach_token"),
                expected_workspace=authority["workspace"],
                expected_workspace_binding=authority["workspace_binding"],
                expected_engage_attempt=authority["engage_attempt"],
                expected_source_binding_sha256=source.sha256,
                expected_staged_epoch=authority["expected_staged_epoch"],
                staged_epoch=row["staged_epoch"] + 1,
                staged_summary=None,
                retirement_stage_kind="empty",
            )
            if published is None:
                return {"skipped": "authority_changed"}
            counts = {"added": 0, "modified": 0, "deleted": 0}
            return {
                "skipped": "empty",
                "epoch": row["staged_epoch"] + 1,
                "counts": counts,
                "event": {
                    "thread_id": thread_id,
                    "session_runtime_generation": authority["runtime_generation"],
                    "staged_epoch": row["staged_epoch"] + 1,
                    "file_count": 0,
                    "counts": counts,
                    "mount_id": str(row["id"]),
                },
                "publication": published,
            }
        if _vm_lease is not None:
            if not await _vm_authorized():
                return {"skipped": "vm_capture_authority_changed"}
            published = await postgres_db.publish_vm_ro_mount_staging_exact(
                row["id"],
                thread_id=thread_id,
                operation_id=str(_vm_lease.receipt["id"]),
                claim_token=int(_vm_lease.receipt["claim_token"]),
                claimant=_vm_lease.claimant,
                expected_engage_attempt=str(row.get("engage_attempt") or ""),
                expected_source_binding_sha256=source.sha256,
                expected_staged_epoch=int(row["staged_epoch"]),
                staged_epoch=int(row["staged_epoch"]) + 1,
                staged_summary=None,
            )
            if published is None:
                return {"skipped": "vm_capture_authority_changed"}
            return {
                "skipped": "empty",
                "epoch": int(row["staged_epoch"]) + 1,
                "counts": {"added": 0, "modified": 0, "deleted": 0},
                "publication": published,
            }
        if row.get("staged_summary") is not None:
            if authority is None:
                await snapshot_service.delete_blob(
                    staging_tar_key(thread_id, row.get("staged_summary"))
                )
                await snapshot_service.delete_blob(
                    staging_manifest_key(thread_id, row.get("staged_summary"))
                )
                await postgres_db.update_ro_mount_staging(
                    row["id"],
                    staged_epoch=row["staged_epoch"] + 1,
                    staged_summary=None,
                    expected_engage_attempt=str(row.get("engage_attempt") or ""),
                    expected_source_binding_sha256=source.sha256,
                )
        return {"skipped": "empty"}

    staged_summary = row.get("staged_summary")
    staged_source_matches = bool(
        isinstance(staged_summary, dict)
        and staged_summary.get("source_binding") == source.binding
        and staged_summary.get("source_binding_sha256") == source.sha256
    )
    if (
        staged_summary
        and staged_source_matches
        and staged_summary.get("signature") == signature
    ):
        # The signature says nothing changed since the last successful
        # stage, but that's only trustworthy if the manifest blob this
        # staging depends on is actually still there — it can go missing
        # (e.g. an out-of-band deletion, or a partial earlier failure that
        # still recorded staged_summary) without the signature ever
        # changing. Skipping in that case would leave the review/apply path
        # reading "staged" against nothing. Fall through to a full re-stage
        # instead of trusting the skip.
        if (
            await snapshot_service.get_blob(
                staging_manifest_key(thread_id, staged_summary)
            )
            is not None
        ):
            if authority is None or authority.get("runtime_retirement_token") is None:
                return {"skipped": "unchanged"}
            # A terminal retirement must durably prove that its final probe
            # observed these already-published bytes before resource cleanup.
            # Re-publish only the row identity/epoch; immutable blobs are not
            # copied or overwritten.
            published = await postgres_db.publish_ro_mount_staging_exact(
                row["id"],
                thread_id=thread_id,
                expected_runtime_generation=authority["runtime_generation"],
                expected_retirement_token=authority.get("runtime_retirement_token"),
                expected_agent_id=authority.get("agent_id"),
                expected_attach_token=authority.get("runtime_attach_token"),
                expected_workspace=authority["workspace"],
                expected_workspace_binding=authority["workspace_binding"],
                expected_engage_attempt=authority["engage_attempt"],
                expected_source_binding_sha256=source.sha256,
                expected_staged_epoch=authority["expected_staged_epoch"],
                staged_epoch=row["staged_epoch"],
                staged_summary=staged_summary,
                retirement_stage_kind="unchanged",
            )
            if published is None:
                return {"skipped": "authority_changed"}
            counts = staged_summary.get("counts") or {}
            return {
                "skipped": "unchanged",
                "epoch": row["staged_epoch"],
                "counts": counts,
                "event": {
                    "thread_id": thread_id,
                    "session_runtime_generation": authority["runtime_generation"],
                    "staged_epoch": row["staged_epoch"],
                    "file_count": sum(int(value or 0) for value in counts.values()),
                    "counts": counts,
                    "mount_id": str(row["id"]),
                },
                "publication": published,
            }
        logger.warning(
            "stage: thread %s signature unchanged but manifest blob missing — "
            "re-staging instead of skipping",
            thread_id,
        )

    tar_path: str | None = None
    manifest_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
            tar_path = tmp.name

        if not await _stream_authorized_tar(
            ssh_authority,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            dest_path=tar_path,
        ):
            logger.warning("stage: tar stream failed for thread %s", thread_id)
            return None
        if not await _vm_authorized():
            return {"skipped": "vm_capture_authority_changed"}

        epoch = row["staged_epoch"] + 1
        manifest = await joined_blocking_call(
            derive_manifest,
            tar_path,
            baseline=row.get("etag_baseline") or {},
            epoch=epoch,
            staged_at=datetime.now(timezone.utc).isoformat(),
        )
        manifest["signature"] = signature
        manifest["source_binding"] = source.binding
        manifest["source_binding_sha256"] = source.sha256
        # Content binding (multi-replica torn-pair defense): with 2
        # orchestrator replicas, two concurrent stagings can interleave the
        # two non-atomic S3 PUTs, leaving a manifest at the deterministic key
        # that doesn't describe the tar next to it. Binding the manifest to
        # the exact tar bytes lets readers (UpperdirDiffSource / apply) verify
        # the downloaded tar and treat a mismatch as staging-missing.
        # A hashing failure (e.g. OSError) propagates to the outer catch-all
        # in stage_thread_cloud_diff → logged, returns None (never raises).
        manifest["tar_sha256"] = await joined_blocking_call(_sha256_file, tar_path)

        if authority is not None or _vm_lease is not None:
            runtime_generation = (
                authority["runtime_generation"]
                if authority is not None
                else _vm_lease.identity.launcher_pod_uid
            )
            workspace_generation = (
                authority["workspace_generation"]
                if authority is not None
                else _vm_lease.identity.workspace_generation
            )
            tar_key, manifest_key = immutable_staging_keys(
                thread_id=thread_id,
                runtime_generation=runtime_generation,
                workspace_generation=workspace_generation,
                epoch=epoch,
                source_binding_sha256=source.sha256,
                tar_sha256=manifest["tar_sha256"],
                claim_token=(
                    int(_vm_lease.receipt["claim_token"])
                    if _vm_lease is not None
                    else None
                ),
            )
        else:
            tar_key = staging_tar_key(thread_id)
            manifest_key = staging_manifest_key(thread_id)

        if not await _still_authorized(postgres_db, thread_id, authority):
            return {"skipped": "authority_changed"}
        if not await _vm_authorized():
            return {"skipped": "vm_capture_authority_changed"}
        if not await snapshot_service.upload_blob_file(tar_key, tar_path):
            logger.error("stage: tar upload failed for thread %s", thread_id)
            return None

        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="wb"
        ) as mtmp:
            manifest_path = mtmp.name
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode()
            if len(manifest_bytes) > MAX_MANIFEST_JSON_BYTES:
                logger.warning("stage: manifest exceeds its byte cap")
                return None
            mtmp.write(manifest_bytes)

        if not await _still_authorized(postgres_db, thread_id, authority):
            return {"skipped": "authority_changed"}
        if not await _vm_authorized():
            return {"skipped": "vm_capture_authority_changed"}
        if not await snapshot_service.upload_blob_file(manifest_key, manifest_path):
            logger.error("stage: manifest upload failed for thread %s", thread_id)
            return None

        summary = {
            "counts": manifest["counts"],
            "signature": signature,
            "tar_sha256": manifest["tar_sha256"],
            "source_binding": source.binding,
            "source_binding_sha256": source.sha256,
        }
        # Only the exact-authority path publishes immutable object names.
        # Keep the legacy summary shape stable while mixed-version callers
        # still use the deterministic compatibility keys.
        if authority is not None or _vm_lease is not None:
            summary.update(
                {
                    "tar_key": tar_key,
                    "manifest_key": manifest_key,
                    # The overlay-reset tail must target the runtime that
                    # produced the reviewed bytes, not merely whichever
                    # runtime is current when a long Apply/Reject finishes.
                    "producer": {
                        "runtime_generation": authority["runtime_generation"],
                        "agent_id": authority["agent_id"],
                        "runtime_attach_token": authority["runtime_attach_token"],
                        "workspace_generation": authority["workspace_generation"],
                        "workspace_runtime_incarnation": authority[
                            "workspace_runtime_incarnation"
                        ],
                    }
                    if authority is not None
                    else {
                        "vm_operation_id": str(_vm_lease.receipt["id"]),
                        "vm_claim_token": int(_vm_lease.receipt["claim_token"]),
                        "workspace_generation": _vm_lease.identity.workspace_generation,
                        "launcher_pod_uid": _vm_lease.identity.launcher_pod_uid,
                    },
                }
            )
        if _vm_lease is not None:
            if not await _vm_authorized():
                return {"skipped": "vm_capture_authority_changed"}
            publication = await postgres_db.publish_vm_ro_mount_staging_exact(
                row["id"],
                thread_id=thread_id,
                operation_id=str(_vm_lease.receipt["id"]),
                claim_token=int(_vm_lease.receipt["claim_token"]),
                claimant=_vm_lease.claimant,
                expected_engage_attempt=str(row.get("engage_attempt") or ""),
                expected_source_binding_sha256=source.sha256,
                expected_staged_epoch=int(row["staged_epoch"]),
                staged_epoch=int(manifest["epoch"]),
                staged_summary=summary,
            )
        elif authority is None:
            published = await postgres_db.update_ro_mount_staging(
                row["id"],
                staged_epoch=manifest["epoch"],
                staged_summary=summary,
                expected_engage_attempt=str(row.get("engage_attempt") or ""),
                expected_source_binding_sha256=source.sha256,
            )
            publication = {} if published else None
        else:
            publication = await postgres_db.publish_ro_mount_staging_exact(
                row["id"],
                thread_id=thread_id,
                expected_runtime_generation=authority["runtime_generation"],
                expected_retirement_token=authority.get("runtime_retirement_token"),
                expected_agent_id=authority.get("agent_id"),
                expected_attach_token=authority.get("runtime_attach_token"),
                expected_workspace=authority["workspace"],
                expected_workspace_binding=authority["workspace_binding"],
                expected_engage_attempt=authority["engage_attempt"],
                expected_source_binding_sha256=source.sha256,
                expected_staged_epoch=authority["expected_staged_epoch"],
                staged_epoch=manifest["epoch"],
                staged_summary=summary,
                retirement_stage_kind="uploaded",
            )
        if publication is None:
            return {"skipped": "authority_changed"}
        counts = manifest["counts"]
        result: dict[str, Any] = {
            "epoch": manifest["epoch"],
            "counts": counts,
        }
        if authority is not None:
            result.update(
                {
                    "event": {
                        "thread_id": thread_id,
                        "session_runtime_generation": authority["runtime_generation"],
                        "staged_epoch": manifest["epoch"],
                        "file_count": sum(int(value or 0) for value in counts.values()),
                        "counts": counts,
                        "mount_id": str(row["id"]),
                    },
                    "publication": publication,
                }
            )
        return result
    finally:
        for path in (tar_path, manifest_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
