"""A warm pool pod binds its thread-less bootstrap at attach time.

0161 minted a bootstrap per *pod provisioned for a thread*. A pool agent is
provisioned before any session exists, so it never got one — and because K8s
env is not patchable on a running pod, it could never be given one later. The
result on main dev (2026-08-16) was that whether a commissioned officer could
write machine tags depended on whether an idle agent happened to be free at
attach time. These tests pin the replacement: the pod still proves possession
of a pod-unique secret, and the thread comes from the durable ``agents`` row.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from orchestrator.services import runtime_actor as service


AGENT_ID = str(uuid4())
THREAD_ID = str(uuid4())
OTHER_THREAD = str(uuid4())
POD_BOOTSTRAP = "srb_" + ("P" * 43)


class _Conn:
    """Minimal asyncpg-ish connection driving the two statements under test."""

    def __init__(self, *, bootstrap_row, bound_row):
        self._bootstrap_row = bootstrap_row
        self._bound_row = bound_row
        self.statements: list[str] = []

    async def fetchrow(self, query, *args):
        self.statements.append(query)
        if "runtime_actor_bootstraps" in query:
            return self._bootstrap_row
        if "FROM agents" in query:
            return self._bound_row
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, *args):
        self.statements.append(query)
        return "INSERT 0 1"


def _db(conn: _Conn) -> MagicMock:
    db = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    db.acquire = _acquire
    return db


@pytest.mark.asyncio
async def test_a_pool_pod_binds_the_thread_the_agents_row_says_it_serves(monkeypatch):
    conn = _Conn(bootstrap_row={"token_hash": b"x"}, bound_row={"?column?": 1})
    minted = AsyncMock(return_value="ACTOR")
    monkeypatch.setattr(service, "mint_thread_runtime_actor", minted)

    actor = await service.exchange_runtime_actor_pod_bootstrap(
        _db(conn),
        agent_id=AGENT_ID,
        thread_id=THREAD_ID,
        bootstrap_token=POD_BOOTSTRAP,
    )

    assert actor == "ACTOR"
    assert minted.await_args.kwargs["thread_id"] == THREAD_ID
    assert minted.await_args.kwargs["agent_id"] == AGENT_ID
    # The bootstrap must be matched thread-lessly; a pool pod's secret is not
    # bound to any session until this moment.
    assert "thread_id IS NULL" in conn.statements[0]


@pytest.mark.asyncio
async def test_a_pod_cannot_claim_a_session_it_is_not_bound_to(monkeypatch):
    """The body says OTHER_THREAD; the agents row disagrees, so nothing is minted."""
    conn = _Conn(bootstrap_row={"token_hash": b"x"}, bound_row=None)
    minted = AsyncMock(return_value="ACTOR")
    monkeypatch.setattr(service, "mint_thread_runtime_actor", minted)

    with pytest.raises(service.RuntimeActorCredentialError) as excinfo:
        await service.exchange_runtime_actor_pod_bootstrap(
            _db(conn),
            agent_id=AGENT_ID,
            thread_id=OTHER_THREAD,
            bootstrap_token=POD_BOOTSTRAP,
        )

    assert excinfo.value.code == "runtime_not_current"
    minted.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_or_expired_bootstrap_is_refused(monkeypatch):
    conn = _Conn(bootstrap_row=None, bound_row={"?column?": 1})
    minted = AsyncMock(return_value="ACTOR")
    monkeypatch.setattr(service, "mint_thread_runtime_actor", minted)

    with pytest.raises(service.RuntimeActorCredentialError) as excinfo:
        await service.exchange_runtime_actor_pod_bootstrap(
            _db(conn),
            agent_id=AGENT_ID,
            thread_id=THREAD_ID,
            bootstrap_token=POD_BOOTSTRAP,
        )

    assert excinfo.value.code == "invalid_bootstrap"
    minted.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_identity_alone_buys_nothing(monkeypatch):
    """A caller with the internal key but no pod secret is refused up front."""
    conn = _Conn(bootstrap_row={"token_hash": b"x"}, bound_row={"?column?": 1})
    monkeypatch.setattr(service, "mint_thread_runtime_actor", AsyncMock())

    with pytest.raises(service.RuntimeActorCredentialError) as excinfo:
        await service.exchange_runtime_actor_pod_bootstrap(
            _db(conn),
            agent_id=AGENT_ID,
            thread_id=THREAD_ID,
            bootstrap_token="not-a-bootstrap",
        )

    assert excinfo.value.code == "malformed_bootstrap"
    assert conn.statements == []


@pytest.mark.asyncio
async def test_a_pod_bootstrap_is_minted_thread_less_and_outlives_idle_time():
    conn = _Conn(bootstrap_row=None, bound_row=None)
    token = await service.issue_runtime_actor_pod_bootstrap(_db(conn))

    assert token.startswith("srb_")
    assert "VALUES ($1, NULL, $2)" in conn.statements[0]
    # A pool pod may idle far longer than a dedicated pod takes to boot; a
    # 15-minute TTL would put identity back at the mercy of cluster load.
    assert service.POD_BOOTSTRAP_TTL_SECONDS > service.BOOTSTRAP_TTL_SECONDS
