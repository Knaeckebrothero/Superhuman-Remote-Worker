"""Unit boundaries for renewable, exact-identity VM remote operations."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from orchestrator.services.blocking_effect import joined_async_call
from orchestrator.services.vm_remote_operation import (
    VMRemoteOperationLease,
    VMRemoteOperationLeaseLost,
    VMRemoteOperationUnavailable,
    VMRuntimeIdentity,
    _LeaseHeartbeat,
    claim_vm_remote_operation,
)

GENERATION = str(uuid4())
VM_UID = "vm-authority-a"
LAUNCHER_UID = str(uuid4())
FINGERPRINT = "SHA256:" + "A" * 43
CONTRACT_DIGEST = "b" * 64


@pytest.fixture(autouse=True)
def _enable_protocol(monkeypatch):
    monkeypatch.setenv("VM_REMOTE_OPERATION_PROTOCOL_ENABLED", "true")


def _row(**vm_overrides):
    vm = {
        "status": "ready",
        "provision_generation": GENERATION,
        "identity_provision_generation": GENERATION,
        "identity_authenticated": True,
        "vm_uid": VM_UID,
        "active_pod_uid": LAUNCHER_UID,
        "ssh_host": "192.0.2.9",
        "ssh_port": 22,
        "ssh_host_key_fingerprint": FINGERPRINT,
    }
    vm.update(vm_overrides)
    return {
        "id": "thread-a",
        "metadata": {
            "config_override": {"workspace": {"backend": "vm"}},
            "vm": vm,
        },
    }


def _attestation(**overrides):
    value = {
        "workspace_generation": GENERATION,
        "runtime_incarnation": LAUNCHER_UID,
        "launcher_pod_uid": LAUNCHER_UID,
        "vm_uid": VM_UID,
        "host": "192.0.2.9",
        "port": 22,
        "ssh_host_key_fingerprint": FINGERPRINT,
    }
    value.update(overrides)
    return SimpleNamespace(**value)


def _db(row=None):
    db = MagicMock()
    db.activate_vm_remote_operation_protocol = AsyncMock(return_value=True)
    db.get_thread = AsyncMock(return_value=row if row is not None else _row())
    db.get_job = AsyncMock()
    db.claim_vm_remote_operation = AsyncMock(
        return_value={"id": str(uuid4()), "claim_token": 7}
    )
    db.renew_vm_remote_operation = AsyncMock(
        return_value={"id": str(uuid4()), "claim_token": 7}
    )
    db.settle_vm_remote_operation = AsyncMock(return_value=True)
    return db


@pytest.mark.asyncio
async def test_protocol_defaults_dark_before_owner_or_controller_io(monkeypatch):
    monkeypatch.delenv("VM_REMOTE_OPERATION_PROTOCOL_ENABLED", raising=False)
    db = _db()
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock()

    with pytest.raises(VMRemoteOperationUnavailable, match="not active"):
        await claim_vm_remote_operation(
            db=db,
            provisioner=provisioner,
            owner_id="thread-a",
            owner_kind="thread",
            operation_kind="thread_upload",
        )

    db.activate_vm_remote_operation_protocol.assert_not_awaited()
    db.get_thread.assert_not_awaited()
    provisioner.attest_workspace_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_protocol_refusal_precedes_owner_or_controller_io():
    db = _db()
    db.activate_vm_remote_operation_protocol = AsyncMock(return_value=False)
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock()

    with pytest.raises(VMRemoteOperationUnavailable, match="unavailable"):
        await claim_vm_remote_operation(
            db=db,
            provisioner=provisioner,
            owner_id="thread-a",
            owner_kind="thread",
            operation_kind="thread_upload",
        )

    db.get_thread.assert_not_awaited()
    provisioner.attest_workspace_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_unattested_row_performs_no_controller_or_ledger_io():
    db = _db(_row(identity_authenticated=False))
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock()

    with pytest.raises(VMRemoteOperationUnavailable, match="identity"):
        await claim_vm_remote_operation(
            db=db,
            provisioner=provisioner,
            owner_id="thread-a",
            owner_kind="thread",
            operation_kind="thread_upload",
        )

    provisioner.attest_workspace_runtime.assert_not_awaited()
    db.claim_vm_remote_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_ready_vm_cannot_override_selected_sandbox_contract():
    row = _row()
    row["metadata"]["config_override"]["workspace"]["backend"] = "sandbox"
    db = _db(row)
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock()

    with pytest.raises(VMRemoteOperationUnavailable, match="selected workspace"):
        await claim_vm_remote_operation(
            db=db,
            provisioner=provisioner,
            owner_id="thread-a",
            owner_kind="thread",
            operation_kind="thread_upload",
        )

    provisioner.attest_workspace_runtime.assert_not_awaited()
    db.claim_vm_remote_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_replacement_after_claim_settles_receipt_before_any_io():
    db = _db()
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(
        side_effect=[_attestation(), _attestation(vm_uid="vm-successor")]
    )

    with pytest.raises(VMRemoteOperationUnavailable, match="before admission"):
        await claim_vm_remote_operation(
            db=db,
            provisioner=provisioner,
            owner_id="thread-a",
            owner_kind="thread",
            operation_kind="thread_upload",
        )

    db.claim_vm_remote_operation.assert_awaited_once()
    db.settle_vm_remote_operation.assert_awaited_once()
    assert db.settle_vm_remote_operation.await_args.kwargs["result_kind"] == (
        "replaced"
    )


@pytest.mark.asyncio
async def test_revalidation_renews_only_the_exact_signed_identity():
    db = _db()
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(return_value=_attestation())

    lease = await claim_vm_remote_operation(
        db=db,
        provisioner=provisioner,
        owner_id="thread-a",
        owner_kind="thread",
        operation_kind="thread_delete",
    )
    assert await lease.revalidate() == ("192.0.2.9", 22, FINGERPRINT)
    assert provisioner.attest_workspace_runtime.await_count == 3
    assert db.renew_vm_remote_operation.await_count == 2
    assert all(
        call.kwargs["claimant"] == lease.claimant
        for call in db.renew_vm_remote_operation.await_args_list
    )


@pytest.mark.asyncio
async def test_suspending_vm_admits_reserved_ide_read_but_not_upload():
    db = _db(_row(status="suspending"))
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(return_value=_attestation())

    lease = await claim_vm_remote_operation(
        db=db,
        provisioner=provisioner,
        owner_id="thread-a",
        owner_kind="thread",
        operation_kind="ide_profile",
    )
    assert lease.identity.ssh_host == "192.0.2.9"

    # Non-suspension kinds ask the canonical resolver without its narrow
    # status projection, so they fail before parsing endpoint fields.
    with pytest.raises(VMRemoteOperationUnavailable, match="selected workspace"):
        await claim_vm_remote_operation(
            db=db,
            provisioner=provisioner,
            owner_id="thread-a",
            owner_kind="thread",
            operation_kind="thread_upload",
        )


@pytest.mark.asyncio
async def test_cancelled_database_claim_joins_and_retires_committed_token():
    db = _db()
    started = asyncio.Event()
    release = asyncio.Event()
    receipt = {"id": str(uuid4()), "claim_token": 71}

    async def delayed_claim(*_args, **_kwargs):
        started.set()
        await release.wait()
        return receipt

    db.claim_vm_remote_operation = AsyncMock(side_effect=delayed_claim)
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(return_value=_attestation())
    task = asyncio.create_task(
        claim_vm_remote_operation(
            db=db,
            provisioner=provisioner,
            owner_id="thread-a",
            owner_kind="thread",
            operation_kind="thread_upload",
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    db.settle_vm_remote_operation.assert_awaited_once_with(
        receipt["id"],
        claim_token=71,
        claimant=db.claim_vm_remote_operation.await_args.kwargs["claimant"],
        result_kind="abandoned",
    )


@pytest.mark.asyncio
async def test_controller_endpoint_change_fails_without_renewal():
    db = _db()
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(
        side_effect=[
            _attestation(),
            _attestation(),
            _attestation(host="192.0.2.10"),
        ]
    )

    lease = await claim_vm_remote_operation(
        db=db,
        provisioner=provisioner,
        owner_id="thread-a",
        owner_kind="thread",
        operation_kind="thread_delete",
    )

    assert await lease.revalidate() is None
    # Only the admission revalidation renewed the durable receipt. The
    # mismatched controller observation never extended stale authority.
    assert db.renew_vm_remote_operation.await_count == 1


@pytest.mark.asyncio
async def test_swallowed_heartbeat_cancellation_cannot_settle_success():
    db = _db()
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(
        return_value=_attestation(vm_uid="vm-successor")
    )
    lease = VMRemoteOperationLease(
        db=db,
        provisioner=provisioner,
        identity=VMRuntimeIdentity(
            owner_kind="thread",
            owner_id="thread-a",
            workspace_tier="vm",
            workspace_contract_digest=CONTRACT_DIGEST,
            workspace_generation=GENERATION,
            vm_uid=VM_UID,
            launcher_pod_uid=LAUNCHER_UID,
            ssh_host="192.0.2.9",
            ssh_port=22,
            ssh_host_key_fingerprint=FINGERPRINT,
        ),
        receipt={"id": str(uuid4()), "claim_token": 7},
        operation_kind="thread_upload",
        claimant="vm-remote:test",
        lease_seconds=300,
    )

    class SwallowingHeartbeat:
        async def __aexit__(self, *_args):
            # Models a nested transport which caught the owner's
            # CancelledError while reaping its child and returned normally.
            return False

    assert await lease._heartbeat_renew() is None
    lease._heartbeat = SwallowingHeartbeat()

    with pytest.raises(VMRemoteOperationLeaseLost):
        await lease.__aexit__(None, None, None)

    assert db.settle_vm_remote_operation.await_args.kwargs["result_kind"] == "failed"


def _lease(db, provisioner, *, receipt_id: str | None = None):
    return VMRemoteOperationLease(
        db=db,
        provisioner=provisioner,
        identity=VMRuntimeIdentity(
            owner_kind="thread",
            owner_id="thread-a",
            workspace_tier="vm",
            workspace_contract_digest=CONTRACT_DIGEST,
            workspace_generation=GENERATION,
            vm_uid=VM_UID,
            launcher_pod_uid=LAUNCHER_UID,
            ssh_host="192.0.2.9",
            ssh_port=22,
            ssh_host_key_fingerprint=FINGERPRINT,
        ),
        receipt={"id": receipt_id or str(uuid4()), "claim_token": 7},
        operation_kind="thread_upload",
        claimant="vm-remote:test",
        lease_seconds=300,
    )


@pytest.mark.asyncio
async def test_heartbeat_second_cancel_preserves_typed_lease_loss():
    heartbeat = _LeaseHeartbeat(AsyncMock(return_value=True), interval_seconds=60)
    stopping = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_heartbeat() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            stopping.set()
            await release.wait()

    background = asyncio.create_task(stubborn_heartbeat())
    heartbeat._task = background
    heartbeat._lost = True
    exiting = asyncio.create_task(
        heartbeat.__aexit__(asyncio.CancelledError, asyncio.CancelledError(), None)
    )
    await stopping.wait()

    exiting.cancel()
    await asyncio.sleep(0)
    exiting.cancel()
    assert not background.done()
    release.set()

    with pytest.raises(VMRemoteOperationLeaseLost, match="during external effects"):
        await exiting
    assert background.done()


@pytest.mark.asyncio
async def test_cancelled_remote_effect_retains_lease_until_effect_is_terminal():
    receipt_id = str(uuid4())
    db = _db()
    db.renew_vm_remote_operation = AsyncMock(
        return_value={"id": receipt_id, "claim_token": 7}
    )
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(return_value=_attestation())
    lease = _lease(db, provisioner, receipt_id=receipt_id)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _remote_mutation() -> None:
        started.set()
        await release.wait()

    async def _owner() -> None:
        async with lease:
            await joined_async_call(_remote_mutation())

    task = asyncio.create_task(_owner())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    db.settle_vm_remote_operation.assert_not_awaited()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.settle_vm_remote_operation.await_args.kwargs["result_kind"] == "failed"


@pytest.mark.asyncio
async def test_direct_revalidation_failure_latches_and_cannot_settle_success():
    receipt_id = str(uuid4())
    db = _db()
    db.renew_vm_remote_operation = AsyncMock(
        side_effect=[
            {"id": receipt_id, "claim_token": 7},
            {"id": receipt_id, "claim_token": 7},
            None,
        ]
    )
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(return_value=_attestation())
    lease = _lease(db, provisioner, receipt_id=receipt_id)

    with pytest.raises(VMRemoteOperationLeaseLost, match="during external effects"):
        async with lease:
            assert await lease.revalidate() is None

    assert db.settle_vm_remote_operation.await_args.kwargs["result_kind"] == "failed"


@pytest.mark.asyncio
async def test_database_revalidation_error_latches_authority_loss():
    receipt_id = str(uuid4())
    db = _db()
    db.renew_vm_remote_operation = AsyncMock(
        side_effect=[
            {"id": receipt_id, "claim_token": 7},
            {"id": receipt_id, "claim_token": 7},
            RuntimeError("database unavailable"),
        ]
    )
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(return_value=_attestation())
    lease = _lease(db, provisioner, receipt_id=receipt_id)

    with pytest.raises(VMRemoteOperationLeaseLost, match="during external effects"):
        async with lease:
            assert await lease.revalidate() is None

    assert db.settle_vm_remote_operation.await_args.kwargs["result_kind"] == "failed"


@pytest.mark.asyncio
async def test_normal_exit_requires_final_proof_and_committed_success():
    receipt_id = str(uuid4())
    db = _db()
    db.renew_vm_remote_operation = AsyncMock(
        return_value={"id": receipt_id, "claim_token": 7}
    )
    db.settle_vm_remote_operation = AsyncMock(return_value=False)
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(return_value=_attestation())
    lease = _lease(db, provisioner, receipt_id=receipt_id)

    with pytest.raises(VMRemoteOperationLeaseLost, match="success committed"):
        async with lease:
            pass

    # The explicit context-boundary proof, heartbeat admission proof, and
    # normal-exit proof each renew exact authority.
    assert db.renew_vm_remote_operation.await_count == 3
    settled = db.settle_vm_remote_operation.await_args.kwargs
    assert settled["result_kind"] == "succeeded"
    assert settled["owner_kind"] == "thread"
    assert settled["workspace_generation"] == GENERATION
    assert settled["launcher_pod_uid"] == LAUNCHER_UID


@pytest.mark.asyncio
async def test_failure_settlement_error_does_not_mask_original_exception():
    receipt_id = str(uuid4())
    db = _db()
    db.renew_vm_remote_operation = AsyncMock(
        return_value={"id": receipt_id, "claim_token": 7}
    )
    db.settle_vm_remote_operation = AsyncMock(
        side_effect=RuntimeError("database unavailable")
    )
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(return_value=_attestation())
    lease = _lease(db, provisioner, receipt_id=receipt_id)

    with pytest.raises(ValueError, match="caller failure"):
        async with lease:
            raise ValueError("caller failure")


@pytest.mark.asyncio
async def test_reclaim_between_claim_and_context_entry_performs_no_body_io():
    receipt_id = str(uuid4())
    db = _db()
    db.claim_vm_remote_operation = AsyncMock(
        return_value={"id": receipt_id, "claim_token": 7}
    )
    db.renew_vm_remote_operation = AsyncMock(
        side_effect=[
            {"id": receipt_id, "claim_token": 7},  # claim boundary
            None,  # reclaimed while the caller held the lease object
        ]
    )
    provisioner = MagicMock()
    provisioner.attest_workspace_runtime = AsyncMock(return_value=_attestation())

    lease = await claim_vm_remote_operation(
        db=db,
        provisioner=provisioner,
        owner_id="thread-a",
        owner_kind="thread",
        operation_kind="thread_upload",
    )
    body_ran = False
    with pytest.raises(VMRemoteOperationLeaseLost, match="before external effects"):
        async with lease:
            body_ran = True

    assert body_ran is False
    assert db.settle_vm_remote_operation.await_args.kwargs["result_kind"] == (
        "replaced"
    )
