"""Tests for run_when_leader failure loudness (no Postgres needed).

Split from test_leader_election.py because these are pure-asyncio unit tests —
that file needs testcontainers for the advisory-lock semantics.

Motivation (live-diagnosed on dev 2026-07-05): the KB reindex sweeper's loop
factory raised at call time (import error) and the wrapper task died SILENTLY —
no log line, nothing awaited the task until shutdown. A singleton loop that will
never run again must scream, not vanish.
"""

import asyncio
import logging

import pytest

from orchestrator.services import leader_election


@pytest.fixture(autouse=True)
def _reset_leadership():
    leader_election.is_leader.clear()
    yield
    leader_election.is_leader.clear()


@pytest.mark.asyncio
async def test_factory_failure_logs_and_reraises(caplog):
    """make_coro raising at call time is logged at ERROR before propagating."""

    def bad_factory(se):
        raise ImportError("No module named 'neo4j'")

    shutdown = asyncio.Event()
    leader_election.is_leader.set()
    with caplog.at_level(logging.ERROR, logger=leader_election.logger.name):
        with pytest.raises(ImportError):
            await leader_election.run_when_leader(
                bad_factory, shutdown, poll_seconds=0.02
            )
    assert any(
        r.name == leader_election.logger.name and "factory" in r.message.lower()
        for r in caplog.records
    ), "loop-factory failure was not logged"


@pytest.mark.asyncio
async def test_loop_crash_is_logged(caplog):
    """A wrapped loop that dies on its own is logged (it is re-created while
    leadership holds — the log is what makes a crash-loop visible)."""
    crashes = []

    async def crashing_loop(se):
        crashes.append(1)
        raise RuntimeError("tick exploded")

    shutdown = asyncio.Event()
    leader_election.is_leader.set()
    wrapper = asyncio.create_task(
        leader_election.run_when_leader(crashing_loop, shutdown, poll_seconds=0.02)
    )
    with caplog.at_level(logging.ERROR, logger=leader_election.logger.name):
        await asyncio.sleep(0.2)
        shutdown.set()
        await asyncio.wait_for(wrapper, timeout=5)
    assert len(crashes) >= 1
    assert any(
        r.name == leader_election.logger.name and "crash" in r.message.lower()
        for r in caplog.records
    ), "loop crash was not logged by leader_election"
