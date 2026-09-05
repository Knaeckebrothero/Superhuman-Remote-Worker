"""VM-ready IDE seed is leader-gated (HA / M2-L4).

agent.vm.*.register fans out to both replicas (no queue group). The IDE-config
seed task must be spawned on exactly one replica, when the leader promotes the
VM to ready on the daemon's readiness evidence (there is no orchestrator-side
SSH probe — the orchestrator has no tailnet route; see knowledge-base/knowledge/issues/
vm_ssh_readiness_probe_unroutable_from_orchestrator.md). Mock-only.

Imports the FLATTENED is_leader (from services.leader_election) so the test
toggles the same Event _on_daemon_register reads — conftest puts orchestrator/
on sys.path, and the package-prefixed orchestrator.services.leader_election
would be a *different* module object.
"""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock

from orchestrator.services.leader_election import is_leader
from orchestrator.services.nats_bridge import NatsBridge


class FakeMsg:
    def __init__(self, payload: dict):
        self.data = json.dumps(payload).encode()


def _payload():
    return {
        "job_id": "job-1",
        "ip": "100.64.0.9",
        "hostname": "vm1",
        "pid": 42,
        "ssh_ready": True,
    }


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("VM_MODE", "external")
    b = NatsBridge()
    b._db = AsyncMock()
    b._db.merge_vm_context = AsyncMock(return_value=True)
    b._db.merge_vm_context_if_provision_generation = AsyncMock(return_value=True)
    b._db.get_vm_provision_generation = AsyncMock(
        return_value="11111111-1111-4111-8111-111111111111"
    )
    b._on_vm_ready = None  # skip the dispatch poke
    # job-1 takes the job (not thread) path. Routing is a DB lookup, so these
    # must be explicit — a bare AsyncMock get_thread returns a truthy mock.
    b._db.get_thread = AsyncMock(return_value=None)
    b._db.get_job = AsyncMock(return_value={"id": "job-1", "user_id": "u1"})
    return b


@pytest.mark.asyncio
async def test_seeds_on_leader(bridge):
    bridge._seed_vm_ide_config = AsyncMock()
    is_leader.set()
    try:
        await bridge._on_daemon_register(FakeMsg(_payload()))
        await asyncio.sleep(0)  # let the seed task run
    finally:
        is_leader.clear()
    bridge._seed_vm_ide_config.assert_called_once()


@pytest.mark.asyncio
async def test_skips_seed_on_follower(bridge):
    bridge._seed_vm_ide_config = AsyncMock()
    is_leader.clear()  # follower
    await bridge._on_daemon_register(FakeMsg(_payload()))
    await asyncio.sleep(0)
    bridge._seed_vm_ide_config.assert_not_called()
    bridge._db.merge_vm_context_if_provision_generation.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_seed_while_not_ready(bridge):
    """A not-ready register (ssh_ready=False) must not spawn the seed task."""
    bridge._seed_vm_ide_config = AsyncMock()
    is_leader.set()
    try:
        await bridge._on_daemon_register(FakeMsg({**_payload(), "ssh_ready": False}))
        await asyncio.sleep(0)
    finally:
        is_leader.clear()
    bridge._seed_vm_ide_config.assert_not_called()
    bridge._db.merge_vm_context_if_provision_generation.assert_awaited_once()
