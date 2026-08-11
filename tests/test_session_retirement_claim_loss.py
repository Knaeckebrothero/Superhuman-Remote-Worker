import json
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.shared.session_retirement import (
    active_claim_authority,
    acknowledge_session_claim_quiesced,
    claim_loss_hold,
    stateless_retirement_authority,
    stateless_retirement_release_authorized,
    stateless_settled_retirement_authority,
    unresolved_claim_losses,
    update_stateless_claim_status,
)


THREAD_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class _Txn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Conn:
    def transaction(self):
        return _Txn()


def _db(metadata, queue):
    conn = _Conn()

    async def _fetchrow(sql, *_args):
        if "FROM threads" in sql:
            return {
                "status": "active",
                "execution_lane": "stateless",
                "metadata": metadata,
            }
        if "FROM run_queue" in sql:
            return queue
        raise AssertionError(sql)

    async def _fetchval(sql, *_args):
        if "UPDATE threads" in sql:
            return THREAD_ID
        if "UPDATE run_queue" in sql:
            return _args[2]
        raise AssertionError(sql)

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetchval = AsyncMock(side_effect=_fetchval)
    return conn


def _metadata(*, uid="uid-a", intended="queued"):
    return {
        "kept": True,
        "_stateless_claim_losses": {
            "8": {"pod": "pod-a", "pod_uid": uid, "quiesced": False}
        },
        "_stateless_claim_loss_hold": {
            "lease_token": 9,
            "intended_state": intended,
            "attempts_since_completion": 2,
            "queued_at": "2026-08-11T12:00:00+00:00",
            "run_after": "2026-08-11T12:00:03+00:00",
        },
    }


def _queue(*, input_seq=4, consumed_seq=4):
    return {
        "state": "parked",
        "lease_token": 9,
        "attempts_since_completion": 2,
        "input_seq": input_seq,
        "consumed_seq": consumed_seq,
        "control_input_seq": 0,
        "control_consumed_seq": 0,
    }


@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
def test_present_falsey_loss_ledger_is_never_absence(value):
    with pytest.raises(RuntimeError, match="claim-loss ledger"):
        unresolved_claim_losses({"_stateless_claim_losses": value})


@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
def test_present_falsey_active_claim_is_never_absence(value):
    with pytest.raises(RuntimeError, match="active claim"):
        active_claim_authority({"_stateless_active_claim": value})


@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
def test_present_falsey_claim_hold_is_never_absence(value):
    with pytest.raises(RuntimeError, match="claim-loss hold"):
        claim_loss_hold({"_stateless_claim_loss_hold": value})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker_key",
    [
        "_stateless_workspace_retirement_pending",
        "_stateless_claim_retirement",
        "_stateless_claim_loss_hold",
        "_stateless_claim_losses",
    ],
)
@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
async def test_present_stop_marker_refuses_claim_status_without_queue_write(
    marker_key, value
):
    conn = _db({marker_key: value}, _queue())

    assert (
        await update_stateless_claim_status(
            conn,
            thread_id=THREAD_ID,
            lease_token=8,
            status="active",
        )
        is False
    )
    assert conn.fetchrow.await_count == 1
    conn.fetchval.assert_not_awaited()


def _retirement_metadata() -> dict:
    generation = "11111111-1111-4111-8111-111111111111"
    runtime = "22222222-2222-4222-8222-222222222222"
    fingerprint = "SHA256:trusted"
    ack = {
        "kind": "protocol",
        "terminal_token": 8,
        "workspace_generation": generation,
        "endpoint_generation": generation,
        "runtime_incarnation": runtime,
        "host_key_fingerprint": fingerprint,
    }
    return {
        "_stateless_workspace_retirement_pending": True,
        "_stateless_claim_retirement": {
            "terminal_token": 8,
            "claimant_quiesced": True,
            "shell_retirement_required": True,
            "resident_cleanup_required": True,
            "residents_retired": True,
            "residents_retired_by": "protocol",
            "remote_retired": True,
            "remote_retired_by": "protocol",
            "permanent": False,
            "workspace_generation": generation,
            "endpoint_generation": generation,
            "runtime_incarnation": runtime,
            "host_key_fingerprint": fingerprint,
        },
        "_stateless_resident_retirement_ack": dict(ack),
        "_stateless_shell_retirement_ack": dict(ack),
        "_workspace_binding": {
            "generation": generation,
            "ssh_host_key_fingerprint": fingerprint,
        },
        "workspace_container": {
            "_canvas_workspace_generation": generation,
            "_runtime_incarnation": runtime,
        },
    }


@pytest.mark.parametrize("malformed", [None, 0, 1, "false", "yes", [], {}])
@pytest.mark.parametrize(
    "field",
    [
        "claimant_quiesced",
        "shell_retirement_required",
        "resident_cleanup_required",
        "residents_retired",
        "remote_retired",
        "permanent",
    ],
)
def test_retirement_booleans_are_exact_json_booleans(field, malformed):
    metadata = _retirement_metadata()
    metadata["_stateless_claim_retirement"][field] = malformed

    with pytest.raises(RuntimeError, match=field):
        stateless_retirement_authority(metadata)


def test_release_authority_binds_endpoint_generation_and_ack():
    metadata = _retirement_metadata()
    assert stateless_retirement_release_authorized(metadata)["terminal_token"] == 8

    metadata["_stateless_shell_retirement_ack"]["endpoint_generation"] = (
        "33333333-3333-4333-8333-333333333333"
    )
    with pytest.raises(RuntimeError, match="shell retirement acknowledgement"):
        stateless_retirement_release_authorized(metadata)


def test_release_authority_remains_retryable_after_exact_context_cleanup():
    metadata = _retirement_metadata()
    metadata["workspace_container"].update(
        {"status": "deleted", "_runtime_incarnation": None}
    )

    assert stateless_retirement_release_authorized(metadata)["terminal_token"] == 8

    metadata["workspace_container"]["_canvas_workspace_generation"] = "drifted"
    with pytest.raises(RuntimeError, match="workspace authority changed"):
        stateless_retirement_release_authorized(metadata)


def test_loss_ledger_rejects_noncanonical_token_aliases():
    with pytest.raises(RuntimeError, match="noncanonical"):
        unresolved_claim_losses(
            {
                "_stateless_claim_losses": {
                    "01": {"pod": "pod-a", "pod_uid": "uid-a", "quiesced": False}
                }
            }
        )


@pytest.mark.parametrize("malformed", [None, False, 0, "", [], {}])
def test_present_falsey_settled_tombstone_is_never_absence(malformed):
    with pytest.raises(RuntimeError, match="settled retirement"):
        stateless_settled_retirement_authority(
            {"_stateless_workspace_retirement_settled": malformed}
        )


def test_pending_workspace_absence_proof_is_unsupported():
    metadata = _retirement_metadata()
    marker = metadata["_stateless_claim_retirement"]
    marker.update(
        {
            "workspace_absence_proven": True,
            "runtime_incarnation": None,
            "shell_retirement_required": False,
            "resident_cleanup_required": False,
            "residents_retired": True,
            "remote_retired": True,
        }
    )
    metadata.pop("_stateless_resident_retirement_ack")
    metadata.pop("_stateless_shell_retirement_ack")

    with pytest.raises(RuntimeError, match="absence proof is unsupported"):
        stateless_retirement_authority(metadata)


def test_settled_workspace_absence_proof_is_unsupported():
    with pytest.raises(RuntimeError, match="absence proof is unsupported"):
        stateless_settled_retirement_authority(
            {
                "_stateless_workspace_retirement_settled": {
                    "terminal_token": 0,
                    "cleanup_complete": True,
                    "permanent": False,
                    "backing_id": None,
                    "runtime_incarnation": None,
                    "snapshot_restore_required": False,
                    "workspace_absence_proven": True,
                }
            }
        )


@pytest.mark.asyncio
async def test_exact_uid_ack_removes_debt_and_restores_intended_state():
    conn = _db(_metadata(), _queue())

    assert await acknowledge_session_claim_quiesced(
        conn,
        thread_id=THREAD_ID,
        previous_lease_token=8,
        leased_by="pod-a",
        pod_uid="uid-a",
    )

    thread_update = conn.fetchval.await_args_list[0]
    stored = json.loads(thread_update.args[3])
    assert stored == {"kept": True}
    queue_update = conn.fetchval.await_args_list[1]
    assert queue_update.args[3:7] == (
        "queued",
        2,
        "2026-08-11T12:00:03+00:00",
        "2026-08-11T12:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_same_name_replacement_uid_cannot_ack_old_claim():
    conn = _db(_metadata(), _queue())

    assert not await acknowledge_session_claim_quiesced(
        conn,
        thread_id=THREAD_ID,
        previous_lease_token=8,
        leased_by="pod-a",
        pod_uid="uid-replacement",
    )
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_human_while_held_requeues_an_intended_done_row():
    conn = _db(_metadata(intended="done"), _queue(input_seq=5, consumed_seq=4))

    assert await acknowledge_session_claim_quiesced(
        conn,
        thread_id=THREAD_ID,
        previous_lease_token=8,
        leased_by="pod-a",
        pod_uid="uid-a",
    )

    queue_update = conn.fetchval.await_args_list[1]
    assert queue_update.args[3] == "queued"
