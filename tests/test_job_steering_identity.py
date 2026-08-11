"""Unit tests for durable stateless-worker steering identities and acks."""

from unittest.mock import AsyncMock

import pytest

from src.shared.job_steering import (
    CheckpointSteeringAcker,
    context_delivery_key,
    queued_reply_key,
)


def test_explicit_reply_id_is_authoritative() -> None:
    first = {
        "id": "2f1b1e5b-bd38-46cf-858c-9cdf9c349600",
        "thread_id": "officer",
        "timestamp": "2026-08-10T01:02:03+00:00",
        "message": "first body",
    }
    edited_copy = {**first, "message": "different body"}

    assert queued_reply_key(first) == ("id:2f1b1e5b-bd38-46cf-858c-9cdf9c349600")
    assert queued_reply_key(edited_copy) == queued_reply_key(first)


def test_legacy_reply_key_is_deterministic_and_content_sensitive() -> None:
    first = {
        "thread_id": "officer",
        "timestamp": "2026-08-10T01:02:03+00:00",
        "message": "first body",
    }
    same = dict(reversed(list(first.items())))
    newer_same_thread = {
        **first,
        "timestamp": "2026-08-10T01:03:03+00:00",
        "message": "newer body",
    }

    assert queued_reply_key(first).startswith("legacy:")
    assert queued_reply_key(same) == queued_reply_key(first)
    assert queued_reply_key(newer_same_thread) != queued_reply_key(first)


def test_consumption_annotations_do_not_change_legacy_identity() -> None:
    reply = {
        "thread_id": "officer",
        "timestamp": "2026-08-10T01:02:03+00:00",
        "message": "body",
    }
    consumed = {
        **reply,
        "consumed_at": "2026-08-10T01:04:03+00:00",
        "consumed_checkpoint_id": "1f0d",
        "consumed_checkpoint_step": 7,
    }

    assert queued_reply_key(consumed) == queued_reply_key(reply)


def test_context_delivery_id_distinguishes_identical_repeat() -> None:
    first = context_delivery_key(
        "feedback", "try again", delivery_id="delivery-1", companion="review"
    )
    repeated = context_delivery_key(
        "feedback", "try again", delivery_id="delivery-2", companion="review"
    )

    assert first != repeated
    assert context_delivery_key("feedback", "try again", companion="review") == (
        context_delivery_key("feedback", "try again", companion="review")
    )


def _checkpoint(checkpoint_id: str = "cp-1") -> dict:
    return {
        "id": checkpoint_id,
        "channel_values": {
            "delivered_guidance_ids": ["g-1"],
            "delivered_reply_keys": ["id:r-1"],
            "delivered_feedback_keys": ["feedback:id:f-1"],
            "delivered_delegation_keys": ["delegation:id:d-1"],
        },
    }


@pytest.mark.asyncio
async def test_post_commit_acker_sends_checkpoint_proof_and_suppresses_success() -> (
    None
):
    client = AsyncMock()
    client.ack_job_guidance.return_value = True
    acker = CheckpointSteeringAcker("job-1", client)

    await acker(
        {},
        _checkpoint(),
        {"step": 4},
        {"configurable": {"checkpoint_id": "cp-1"}},
    )
    await acker(
        {},
        _checkpoint("cp-2"),
        {"step": 5},
        {"configurable": {"checkpoint_id": "cp-2"}},
    )

    client.ack_job_guidance.assert_awaited_once_with(
        "job-1",
        guidance_ids=["g-1"],
        reply_keys=["id:r-1"],
        feedback_keys=["feedback:id:f-1"],
        delegation_keys=["delegation:id:d-1"],
        checkpoint_id="cp-1",
    )


@pytest.mark.asyncio
async def test_failed_post_commit_ack_retries_on_successor_checkpoint() -> None:
    client = AsyncMock()
    client.ack_job_guidance.side_effect = [False, True]
    acker = CheckpointSteeringAcker("job-1", client)

    await acker(
        {},
        _checkpoint("cp-1"),
        {"step": 4},
        {"configurable": {"checkpoint_id": "cp-1"}},
    )
    await acker(
        {},
        _checkpoint("cp-2"),
        {"step": 5},
        {"configurable": {"checkpoint_id": "cp-2"}},
    )

    assert client.ack_job_guidance.await_count == 2
    assert client.ack_job_guidance.await_args_list[1].kwargs["checkpoint_id"] == "cp-2"


@pytest.mark.asyncio
async def test_end_reclaim_reconciles_failed_last_ack_without_another_checkpoint() -> (
    None
):
    client = AsyncMock()
    client.ack_job_guidance.side_effect = [False, True]
    first = CheckpointSteeringAcker("job-1", client)

    assert not await first.reconcile_values(
        _checkpoint("cp-end")["channel_values"],
        checkpoint_id="cp-end",
    )

    successor = CheckpointSteeringAcker("job-1", client)
    assert await successor.reconcile_values(
        _checkpoint("cp-end")["channel_values"],
        checkpoint_id="cp-end",
    )
    assert client.ack_job_guidance.await_count == 2
    assert client.ack_job_guidance.await_args_list[1].kwargs["checkpoint_id"] == (
        "cp-end"
    )


@pytest.mark.asyncio
async def test_no_absorbed_entries_means_no_ack() -> None:
    client = AsyncMock()
    acker = CheckpointSteeringAcker("job-1", client)

    await acker(
        {},
        {"id": "cp-1", "channel_values": {}},
        {"step": 1},
        {"configurable": {"checkpoint_id": "cp-1"}},
    )

    client.ack_job_guidance.assert_not_awaited()
