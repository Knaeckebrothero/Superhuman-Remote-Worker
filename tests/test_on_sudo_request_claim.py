"""on_sudo_request claim branching (HA / M2-L4).

The handler must: drop silently when it loses the claim (winner owns it), deny
when the insert genuinely errors (so the daemon doesn't hang), and otherwise
proceed as before. Mock-only — no Postgres.
"""

import json

import pytest
from unittest.mock import AsyncMock

from services.sudo_gate import SudoGateService


class FakeMsg:
    def __init__(self, payload: dict, reply):
        self.data = json.dumps(payload).encode()
        self.reply = reply
        self.responded = None

    async def respond(self, data: bytes):
        self.responded = data


def _payload():
    return {
        "job_id": "job-1",
        "vm_id": "vm1",
        "command": "ls",
        "argv": ["-la"],
        "user": "agent",
        "runas_user": "root",
        "cwd": "/",
    }


@pytest.mark.asyncio
async def test_drops_on_lost_claim():
    g = SudoGateService()
    g._insert_request = AsyncMock(return_value=None)  # lost the claim
    g._evaluate_auto_rules = AsyncMock()
    g._broadcast_sse = AsyncMock()
    g._nats_reply = AsyncMock()
    msg = FakeMsg(_payload(), reply="_INBOX.x")

    await g.on_sudo_request(msg)

    g._evaluate_auto_rules.assert_not_awaited()
    g._broadcast_sse.assert_not_awaited()
    g._nats_reply.assert_not_awaited()  # NOT a denial — the winner responds
    assert msg.responded is None
    assert "_INBOX.x" not in g._pending_msgs


@pytest.mark.asyncio
async def test_denies_on_db_error():
    g = SudoGateService()
    g._insert_request = AsyncMock(side_effect=RuntimeError("db down"))
    g._evaluate_auto_rules = AsyncMock()
    g._nats_reply = AsyncMock()
    msg = FakeMsg(_payload(), reply="_INBOX.y")

    await g.on_sudo_request(msg)

    g._nats_reply.assert_awaited_once()  # denied so the daemon doesn't hang
    assert g._nats_reply.await_args.args[1] is False  # approved=False
    g._evaluate_auto_rules.assert_not_awaited()


@pytest.mark.asyncio
async def test_winner_with_no_automatch_broadcasts():
    g = SudoGateService()
    g._insert_request = AsyncMock(return_value="req-1")  # won the claim
    g._evaluate_auto_rules = AsyncMock(return_value=None)  # no auto-rule match
    g._broadcast_sse = AsyncMock()
    msg = FakeMsg(_payload(), reply="_INBOX.z")

    await g.on_sudo_request(msg)

    insert = g._insert_request.await_args.kwargs
    assert insert["job_id"] == "job-1"
    assert insert.get("thread_id") is None
    assert insert.get("request_id") is None
    g._broadcast_sse.assert_awaited_once()
    assert g._pending_msgs.get("req-1") is msg
