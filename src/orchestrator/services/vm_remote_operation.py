"""Durable exact-runtime authority for orchestrator-to-VM remote I/O."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import os
from typing import Any
from uuid import UUID, uuid4

from orchestrator.services.blocking_effect import joined_async_call
from shared.workspace_contract import (
    vm_mode_from_env,
    workspace_contract_authority_identity,
)


logger = logging.getLogger(__name__)

VM_REMOTE_OPERATION_PROTOCOL_VERSION = 1
_VM_REMOTE_OPERATION_PROTOCOL_ENV = "VM_REMOTE_OPERATION_PROTOCOL_ENABLED"


class VMRemoteOperationLeaseLost(RuntimeError):
    """The exact durable claim stopped being renewable during an effect."""


class _LeaseHeartbeat:
    def __init__(self, renew: Any, *, interval_seconds: float) -> None:
        self._renew = renew
        self._interval_seconds = interval_seconds
        self._owner: asyncio.Task[Any] | None = None
        self._task: asyncio.Task[None] | None = None
        self._lost = False

    async def __aenter__(self) -> "_LeaseHeartbeat":
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("VM remote-operation heartbeat requires an asyncio task")
        self._owner = owner
        if not await self._renew():
            self._lost = True
            raise VMRemoteOperationLeaseLost(
                "VM remote-operation authority changed before external effects"
            )
        self._task = asyncio.create_task(self._run())
        return self

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                renewed = await self._renew()
            except Exception:
                renewed = None
            if renewed:
                continue
            self._lost = True
            if self._owner is not None:
                self._owner.cancel()
            return

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        cancellation: asyncio.CancelledError | None = None
        if self._task is not None:
            task = self._task

            async def _stop() -> None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

            # A second cancellation of the lease owner must not leave the
            # renewal task alive after the enclosing operation releases its
            # durable claim.
            try:
                await joined_async_call(_stop())
            except asyncio.CancelledError as error:
                # joined_async_call already proved the heartbeat terminal.
                # Defer propagation until a concurrent lease-loss signal has
                # been converted to the typed authority error.
                cancellation = error
        if self._lost and (
            exc_type is asyncio.CancelledError or cancellation is not None
        ):
            raise VMRemoteOperationLeaseLost(
                "VM remote-operation authority changed during external effects"
            ) from None
        if cancellation is not None:
            raise cancellation
        return False


def vm_remote_operation_protocol_enabled() -> bool:
    """Return whether this replica was rolled into the forward-only protocol."""

    return os.environ.get(_VM_REMOTE_OPERATION_PROTOCOL_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class VMRemoteOperationUnavailable(RuntimeError):
    """No exact, renewable authority exists for the requested VM operation."""


@dataclass(frozen=True, slots=True)
class VMRuntimeIdentity:
    owner_kind: str
    owner_id: str
    workspace_tier: str
    workspace_contract_digest: str
    workspace_generation: str
    vm_uid: str
    launcher_pod_uid: str
    ssh_host: str
    ssh_port: int
    ssh_host_key_fingerprint: str


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    import json

    value = row.get("context" if "context" in row else "metadata") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return value if isinstance(value, dict) else {}


def _identity_from_row(
    row: dict[str, Any],
    *,
    owner_kind: str,
    owner_id: str,
    operation_kind: str,
) -> VMRuntimeIdentity:
    metadata = _metadata(row)
    owner_contract = {
        "context": metadata,
        "config_override": (
            row.get("config_override")
            if owner_kind == "job"
            else metadata.get("config_override")
        ),
    }
    contract_identity = workspace_contract_authority_identity(
        owner_contract,
        vm_mode=vm_mode_from_env(),
        allow_vm_suspending=operation_kind
        in {"cloud_stage", "ide_settings", "ide_profile", "snapshot_capture"},
    )
    if contract_identity is None or contract_identity[0] != "vm":
        raise VMRemoteOperationUnavailable("VM is not the selected workspace contract")
    workspace_tier, workspace_contract_digest = contract_identity
    vm = metadata.get("vm")
    if isinstance(vm, dict) and vm.get("_suspend_remote_io_closed"):
        raise VMRemoteOperationUnavailable("VM suspension has closed remote I/O")
    allowed_statuses = (
        {"ready", "suspending"}
        if operation_kind
        in {"cloud_stage", "ide_settings", "ide_profile", "snapshot_capture"}
        else {"ready"}
    )
    if not isinstance(vm, dict) or vm.get("status") not in allowed_statuses:
        raise VMRemoteOperationUnavailable("VM is not ready")
    try:
        generation = str(UUID(str(vm.get("provision_generation"))))
        launcher_uid = str(UUID(str(vm.get("active_pod_uid"))))
        port = int(vm.get("ssh_port"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise VMRemoteOperationUnavailable("VM identity is malformed") from exc
    vm_uid = vm.get("vm_uid")
    host = vm.get("ssh_host")
    fingerprint = vm.get("ssh_host_key_fingerprint")
    if (
        vm.get("identity_authenticated") is not True
        or str(vm.get("identity_provision_generation") or "") != generation
        or not isinstance(vm_uid, str)
        or not vm_uid
        or vm_uid != vm_uid.strip()
        or len(vm_uid) > 256
        or any(ch.isspace() for ch in vm_uid)
        or not isinstance(host, str)
        or not host
        or host != host.strip()
        or len(host) > 512
        or any(ch.isspace() for ch in host)
        or not 1 <= port <= 65535
        or not isinstance(fingerprint, str)
        or not fingerprint.startswith("SHA256:")
        or len(fingerprint) > 128
        or any(ch.isspace() for ch in fingerprint)
    ):
        raise VMRemoteOperationUnavailable("VM identity is unavailable")
    return VMRuntimeIdentity(
        owner_kind=owner_kind,
        owner_id=owner_id,
        workspace_tier=workspace_tier,
        workspace_contract_digest=workspace_contract_digest,
        workspace_generation=generation,
        vm_uid=vm_uid,
        launcher_pod_uid=launcher_uid,
        ssh_host=host,
        ssh_port=port,
        ssh_host_key_fingerprint=fingerprint,
    )


def _attestation_matches(identity: VMRuntimeIdentity, attestation: Any) -> bool:
    return bool(
        attestation is not None
        and attestation.workspace_generation == identity.workspace_generation
        and attestation.runtime_incarnation == identity.launcher_pod_uid
        and attestation.launcher_pod_uid == identity.launcher_pod_uid
        and attestation.vm_uid == identity.vm_uid
        and attestation.host == identity.ssh_host
        and int(attestation.port) == identity.ssh_port
        and attestation.ssh_host_key_fingerprint == identity.ssh_host_key_fingerprint
    )


class VMRemoteOperationLease:
    """One database-time lease paired to repeated signed controller proof."""

    def __init__(
        self,
        *,
        db: Any,
        provisioner: Any,
        identity: VMRuntimeIdentity,
        receipt: dict[str, Any],
        operation_kind: str,
        claimant: str,
        lease_seconds: int,
    ) -> None:
        self.db = db
        self.provisioner = provisioner
        self.identity = identity
        self.receipt = receipt
        self.operation_kind = operation_kind
        self.claimant = claimant
        self.lease_seconds = lease_seconds
        self._serial = asyncio.Lock()
        self._heartbeat: _LeaseHeartbeat | None = None
        self._authority_lost = False

    async def revalidate(self) -> tuple[str, int, str] | None:
        """Freshly attest the VM, then renew the exact durable claim token."""

        async with self._serial:
            try:
                proof_timeout = max(5.0, min(30.0, self.lease_seconds / 4))
                async with asyncio.timeout(proof_timeout):
                    attestation = await self.provisioner.attest_workspace_runtime(
                        self.identity.owner_id,
                        entity_type=self.identity.owner_kind,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._authority_lost = True
                return None
            if not _attestation_matches(self.identity, attestation):
                self._authority_lost = True
                return None
            try:
                async with asyncio.timeout(proof_timeout):
                    renewed = await self.db.renew_vm_remote_operation(
                        str(self.receipt["id"]),
                        claim_token=int(self.receipt["claim_token"]),
                        claimant=self.claimant,
                        lease_seconds=self.lease_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._authority_lost = True
                return None
            if not renewed:
                self._authority_lost = True
                return None
            self.receipt = renewed
            return (
                self.identity.ssh_host,
                self.identity.ssh_port,
                self.identity.ssh_host_key_fingerprint,
            )

    async def _heartbeat_renew(self) -> tuple[str, int, str] | None:
        """Remember lease loss even when a nested remote helper swallows cancel."""

        renewed = await self.revalidate()
        if renewed is None:
            self._authority_lost = True
        return renewed

    async def __aenter__(self) -> "VMRemoteOperationLease":
        # The caller may hold this object for an arbitrary interval between
        # claim and ``async with``.  Reprove the exact token explicitly at the
        # context boundary before the generic heartbeat owns renewal.  (The
        # heartbeat deliberately performs its own first renewal too.)
        result_kind = "abandoned"
        try:
            live = await self.revalidate()
            if live is None:
                result_kind = "replaced"
                raise VMRemoteOperationLeaseLost(
                    "VM remote-operation authority changed before external effects"
                )
            interval = max(10.0, min(60.0, self.lease_seconds / 3))
            self._heartbeat = _LeaseHeartbeat(
                self._heartbeat_renew,
                interval_seconds=interval,
            )
            await self._heartbeat.__aenter__()
        except BaseException:
            # __aexit__ is not invoked when __aenter__ fails. Own settlement
            # here so cancellation after the durable claim cannot leave an
            # apparently-live operation racing its immediate retry.
            try:
                await joined_async_call(
                    self.db.settle_vm_remote_operation(
                        str(self.receipt["id"]),
                        claim_token=int(self.receipt["claim_token"]),
                        claimant=self.claimant,
                        result_kind=result_kind,
                    )
                )
            except asyncio.CancelledError:
                # The join completed before re-raising cancellation; preserve
                # the original admission cancellation without a false warning.
                pass
            except BaseException:
                logger.warning(
                    "VM remote-operation cancelled admission could not be settled",
                    exc_info=True,
                )
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        heartbeat = self._heartbeat
        heartbeat_error: BaseException | None = None
        if heartbeat is not None:
            try:
                await heartbeat.__aexit__(exc_type, exc, traceback)
            except BaseException as error:  # preserve lease-loss semantics
                heartbeat_error = error
        if self._authority_lost and heartbeat_error is None:
            # Some transport wrappers deliberately catch CancelledError to
            # reap a child process and return a typed result.  That cleanup
            # must not turn a lost durable lease into a successful receipt.
            heartbeat_error = VMRemoteOperationLeaseLost(
                "VM remote-operation authority changed during external effects"
            )

        # A heartbeat renewal is only a bounded promise into the future.  Stop
        # the task first, then require one fresh controller + owner-row proof at
        # the exact success boundary.  Without this check an event-loop pause
        # could let database time expire (and a successor reclaim the token)
        # between the last heartbeat and an otherwise normal return.
        if exc_type is None and heartbeat_error is None:
            try:
                live = await self.revalidate()
            except BaseException as error:
                live = None
                heartbeat_error = error
            if live is None and heartbeat_error is None:
                heartbeat_error = VMRemoteOperationLeaseLost(
                    "VM remote-operation authority changed before settlement"
                )

        result = (
            "succeeded" if exc_type is None and heartbeat_error is None else "failed"
        )
        settlement_error: BaseException | None = None
        settled = False
        try:
            settled = await joined_async_call(
                self.db.settle_vm_remote_operation(
                    str(self.receipt["id"]),
                    claim_token=int(self.receipt["claim_token"]),
                    claimant=self.claimant,
                    result_kind=result,
                    owner_kind=self.identity.owner_kind,
                    owner_id=self.identity.owner_id,
                    workspace_tier=self.identity.workspace_tier,
                    workspace_contract_digest=self.identity.workspace_contract_digest,
                    workspace_generation=self.identity.workspace_generation,
                    vm_uid=self.identity.vm_uid,
                    launcher_pod_uid=self.identity.launcher_pod_uid,
                    ssh_host=self.identity.ssh_host,
                    ssh_port=self.identity.ssh_port,
                    ssh_host_key_fingerprint=(self.identity.ssh_host_key_fingerprint),
                    operation_kind=self.operation_kind,
                )
            )
        except BaseException as error:
            settlement_error = error

        # Never replace a caller's real exception with cleanup/settlement
        # failure.  With no original exception, however, an uncommitted success
        # receipt is authority loss and the caller must not report success.
        if exc_type is not None:
            if settlement_error is not None:
                logger.warning(
                    "VM remote-operation failure settlement did not commit",
                    exc_info=(
                        type(settlement_error),
                        settlement_error,
                        settlement_error.__traceback__,
                    ),
                )
            if exc_type is asyncio.CancelledError and heartbeat_error is not None:
                raise heartbeat_error
            return False
        if heartbeat_error is not None:
            raise heartbeat_error
        if settlement_error is not None:
            raise VMRemoteOperationLeaseLost(
                "VM remote-operation success settlement failed"
            ) from settlement_error
        if not settled:
            raise VMRemoteOperationLeaseLost(
                "VM remote-operation authority changed before success committed"
            )
        return False


async def claim_vm_remote_operation(
    *,
    db: Any,
    provisioner: Any,
    owner_id: str,
    owner_kind: str,
    operation_kind: str,
    lease_seconds: int = 300,
) -> VMRemoteOperationLease:
    """Freshly attest, durably claim, and re-attest one VM operation."""

    # This flag is delivered only after Helm's Recreate cutover has removed
    # every predecessor.  Keep this check before owner reads, controller calls,
    # or network-derived identity so a dark rollout performs no remote work.
    if not vm_remote_operation_protocol_enabled():
        raise VMRemoteOperationUnavailable("VM remote-operation protocol is not active")
    try:
        protocol_active = await db.activate_vm_remote_operation_protocol(
            protocol_version=VM_REMOTE_OPERATION_PROTOCOL_VERSION,
            activated_by="orchestrator-vm-remote-v1",
        )
    except Exception as exc:
        raise VMRemoteOperationUnavailable(
            "VM remote-operation protocol is unavailable"
        ) from exc
    if protocol_active is not True:
        raise VMRemoteOperationUnavailable(
            "VM remote-operation protocol is unavailable"
        )
    if owner_kind not in {"job", "thread"}:
        raise VMRemoteOperationUnavailable("VM owner kind is invalid")
    try:
        row = (
            await db.get_job(owner_id)
            if owner_kind == "job"
            else await db.get_thread(owner_id)
        )
    except Exception as exc:
        raise VMRemoteOperationUnavailable("VM owner is unavailable") from exc
    if not isinstance(row, dict):
        raise VMRemoteOperationUnavailable("VM owner is unavailable")
    identity = _identity_from_row(
        row,
        owner_kind=owner_kind,
        owner_id=owner_id,
        operation_kind=operation_kind,
    )
    try:
        async with asyncio.timeout(30.0):
            attestation = await provisioner.attest_workspace_runtime(
                owner_id,
                entity_type=owner_kind,
            )
    except Exception as exc:
        raise VMRemoteOperationUnavailable("VM runtime attestation failed") from exc
    if not _attestation_matches(identity, attestation):
        raise VMRemoteOperationUnavailable("VM runtime changed during attestation")
    claimant = f"vm-remote:{operation_kind}:{uuid4()}"
    claim_task = asyncio.create_task(
        db.claim_vm_remote_operation(
            owner_id,
            protocol_version=VM_REMOTE_OPERATION_PROTOCOL_VERSION,
            owner_kind=owner_kind,
            operation_kind=operation_kind,
            workspace_tier=identity.workspace_tier,
            workspace_contract_digest=identity.workspace_contract_digest,
            workspace_generation=identity.workspace_generation,
            vm_uid=identity.vm_uid,
            launcher_pod_uid=identity.launcher_pod_uid,
            ssh_host=identity.ssh_host,
            ssh_port=identity.ssh_port,
            ssh_host_key_fingerprint=identity.ssh_host_key_fingerprint,
            claimant=claimant,
            lease_seconds=lease_seconds,
        )
    )
    cancellation: asyncio.CancelledError | None = None
    while not claim_task.done():
        try:
            await asyncio.shield(claim_task)
        except asyncio.CancelledError as error:
            # A transaction can commit between server execution and delivery
            # of its result. Join it so we know the exact token to retire.
            cancellation = error
        except Exception:
            if cancellation is None:
                raise
    if cancellation is not None:
        try:
            cancelled_receipt = claim_task.result()
        except BaseException:
            cancelled_receipt = None
        if isinstance(cancelled_receipt, dict):
            try:
                await joined_async_call(
                    db.settle_vm_remote_operation(
                        str(cancelled_receipt["id"]),
                        claim_token=int(cancelled_receipt["claim_token"]),
                        claimant=claimant,
                        result_kind="abandoned",
                    )
                )
            except asyncio.CancelledError:
                pass
            except BaseException:
                logger.warning(
                    "VM remote-operation cancelled database claim could not be settled",
                    exc_info=True,
                )
        raise cancellation
    receipt = claim_task.result()
    if not receipt:
        raise VMRemoteOperationUnavailable("VM remote operation is already leased")
    lease = VMRemoteOperationLease(
        db=db,
        provisioner=provisioner,
        identity=identity,
        receipt=receipt,
        operation_kind=operation_kind,
        claimant=claimant,
        lease_seconds=lease_seconds,
    )
    # Close the claim-after-controller-read window before callers can perform
    # network I/O. __aenter__ repeats this and starts the renewal heartbeat.
    try:
        admitted = await lease.revalidate()
    except BaseException:
        try:
            await joined_async_call(
                db.settle_vm_remote_operation(
                    str(receipt["id"]),
                    claim_token=int(receipt["claim_token"]),
                    claimant=claimant,
                    result_kind="abandoned",
                )
            )
        except asyncio.CancelledError:
            pass
        except BaseException:
            logger.warning(
                "VM remote-operation cancelled claim could not be settled",
                exc_info=True,
            )
        raise
    if admitted is None:
        await joined_async_call(
            db.settle_vm_remote_operation(
                str(receipt["id"]),
                claim_token=int(receipt["claim_token"]),
                claimant=claimant,
                result_kind="replaced",
            )
        )
        raise VMRemoteOperationUnavailable("VM runtime changed before admission")
    return lease


__all__ = [
    "VMRemoteOperationLease",
    "VMRemoteOperationLeaseLost",
    "VMRemoteOperationUnavailable",
    "VMRuntimeIdentity",
    "VM_REMOTE_OPERATION_PROTOCOL_VERSION",
    "claim_vm_remote_operation",
    "vm_remote_operation_protocol_enabled",
]
