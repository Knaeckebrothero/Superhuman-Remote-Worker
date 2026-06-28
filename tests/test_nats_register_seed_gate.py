"""daemon-register IDE seed is leader-gated (HA / M2-L4).

agent.vm.*.register fans out to both replicas (no queue group). The IDE-config
SSH seed must run on exactly one — the leader (which always receives the
broadcast), so the leader-gated dispatch poke is unaffected. Mock-only.

Imports the FLATTENED is_leader (from services.leader_election) so the test
toggles the same Event _on_daemon_register reads — conftest puts orchestrator/
on sys.path, and the package-prefixed orchestrator.services.leader_election
would be a *different* module object.
"""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock

from services.leader_election import is_leader
from services.nats_bridge import NatsBridge


class FakeMsg:
    def __init__(self, payload: dict):
        self.data = json.dumps(payload).encode()


def _payload():
    return {"job_id": "job-1", "ip": "100.64.0.9", "hostname": "vm1", "pid": 42}


@pytest.fixture
def bridge():
    b = NatsBridge()
    b._db = None              # _set_vm_context no-ops when _db is None
    b._on_vm_ready = None     # skip the dispatch poke
    b._thread_vm_ids = set()  # job-1 takes the job (not thread) path
    return b


@pytest.mark.asyncio
async def test_seeds_on_leader(bridge):
    bridge._seed_vm_ide_config = AsyncMock()
    is_leader.set()
    try:
        await bridge._on_daemon_register(FakeMsg(_payload()))
        await asyncio.sleep(0)        # let the create_task run
    finally:
        is_leader.clear()
    bridge._seed_vm_ide_config.assert_called_once()


@pytest.mark.asyncio
async def test_skips_seed_on_follower(bridge):
    bridge._seed_vm_ide_config = AsyncMock()
    is_leader.clear()                 # follower
    await bridge._on_daemon_register(FakeMsg(_payload()))
    await asyncio.sleep(0)
    bridge._seed_vm_ide_config.assert_not_called()
