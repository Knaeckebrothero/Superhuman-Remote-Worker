"""Boundary cases for R1.B05's root lane: session preparation and workspace delivery.

Three properties that the extraction could plausibly have broken and that
nothing else in the suite would have caught:

1. The new agent-workspace route resolves its collaborators from the
   application handling the request, not from a process-wide lookup. While
   that lookup was a module global, two FastAPI applications in one process
   shared one set no matter which was answering.
2. The single-flight registry is one object per application build, and its
   eviction is identity-checked — a newer schedule for the same thread must
   survive the previous task's completion callback.
3. The moved bodies still resolve their collaborators through ``main`` at call
   time, so an existing ``patch("orchestrator.main._x")`` reaches them. This is
   the failure §P3 of the batch note describes: a wrapper in ``main`` does not
   intercept a call made *inside* a service, so a patch can silently do nothing
   while the test still passes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import orchestrator.main as orch_main
from orchestrator.routers import agent_thread_workspace
from orchestrator.services import stateless_workspace_scheduler

_THREAD_ID = "a1111111-1111-1111-1111-111111111111"


def _request(dependencies: Any) -> SimpleNamespace:
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                thread_workspace_delivery_dependencies_factory=lambda: dependencies
            )
        ),
    )


class _RecordingLock:
    def __init__(self, seen: list[str], name: str) -> None:
        self._seen = seen
        self._name = name

    def __call__(self, thread_id: str) -> "_RecordingLock":
        self._thread_id = thread_id
        return self

    async def __aenter__(self) -> None:
        self._seen.append(self._name)

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_route_uses_the_store_of_the_application_handling_the_request():
    """Two applications, two stores; each request must take its own lock."""

    seen: list[str] = []
    locked: list[str] = []

    def _dependencies(name: str) -> Any:
        async def _payload(thread_id: str, **_kwargs: Any) -> dict[str, Any]:
            locked.append(f"{name}:{thread_id}")
            return {"store": name}

        return SimpleNamespace(
            store=SimpleNamespace(thread_datasource_lock=_RecordingLock(seen, name)),
            require_internal=AsyncMock(),
            _payload=_payload,
        )

    first = _dependencies("A")
    second = _dependencies("B")

    async def _call(dependencies: Any) -> dict[str, Any]:
        import orchestrator.services.thread_workspace_delivery as delivery

        original = delivery.agent_get_thread_workspace_locked
        delivery.agent_get_thread_workspace_locked = dependencies._payload
        try:
            return await agent_thread_workspace.agent_get_thread_workspace(
                _request(dependencies), _THREAD_ID
            )
        finally:
            delivery.agent_get_thread_workspace_locked = original

    # Interleaved A -> B -> A: a process-wide lookup would answer with one of
    # them three times.
    assert await _call(first) == {"store": "A"}
    assert await _call(second) == {"store": "B"}
    assert await _call(first) == {"store": "A"}
    assert seen == ["A", "B", "A"]
    assert locked == [
        f"A:{_THREAD_ID}",
        f"B:{_THREAD_ID}",
        f"A:{_THREAD_ID}",
    ]


@pytest.mark.asyncio
async def test_route_refuses_before_it_touches_the_store():
    """The internal-key guard runs before the datasource lock is taken."""

    seen: list[str] = []

    class _Refused(Exception):
        pass

    async def _guard(_request: Any) -> None:
        raise _Refused

    dependencies = SimpleNamespace(
        store=SimpleNamespace(thread_datasource_lock=_RecordingLock(seen, "A")),
        require_internal=_guard,
    )
    with pytest.raises(_Refused):
        await agent_thread_workspace.agent_get_thread_workspace(
            _request(dependencies), _THREAD_ID
        )
    assert seen == []


@pytest.mark.asyncio
async def test_the_ensure_registry_is_one_object_and_evicts_by_identity():
    registry = stateless_workspace_scheduler.StatelessWorkspaceEnsureRegistry()
    release_first = asyncio.Event()
    release_second = asyncio.Event()

    async def _work(release: asyncio.Event) -> None:
        await release.wait()

    first = asyncio.create_task(_work(release_first))
    registry.register(_THREAD_ID, first)
    assert registry.get(_THREAD_ID) is first

    # A newer registration must survive the older task's completion callback.
    # Two events, deliberately: one shared event would finish both tasks and
    # the assertion below would hold for a registry that cleared the slot
    # unconditionally, which is the very thing it is here to refuse.
    second = asyncio.create_task(_work(release_second))
    registry.register(_THREAD_ID, second)
    release_first.set()
    await first
    await asyncio.sleep(0)
    assert registry.get(_THREAD_ID) is second

    release_second.set()
    await second
    await asyncio.sleep(0)
    assert registry.get(_THREAD_ID) is None
    assert registry.in_flight() == {}


def test_main_builds_exactly_one_ensure_registry():
    """The registry is shared state: rebuilding it per call would not
    single-flight anything."""
    first = orch_main._stateless_workspace_schedule_dependencies().registry
    second = orch_main._stateless_workspace_schedule_dependencies().registry
    assert first is second is orch_main._stateless_workspace_ensure_registry


@pytest.mark.asyncio
async def test_a_main_patch_still_reaches_the_moved_body(monkeypatch):
    """`patch("orchestrator.main._thread_project_ids")` must steer the service.

    The dependency object is rebuilt per call from ``main``'s namespace, which
    is the only reason the existing suites that patch these names keep working
    after the bodies moved. A factory that captured the collaborator at import
    would leave every one of them green and inert.
    """
    sentinel = ["11111111-1111-1111-1111-111111111111"]
    calls: list[str] = []

    async def _stub(thread_id: str) -> list[str]:
        calls.append(thread_id)
        return sentinel

    monkeypatch.setattr(orch_main, "_thread_project_ids", _stub)
    dependencies = orch_main._thread_workspace_delivery_dependencies()
    assert dependencies.thread_project_ids is _stub
    assert await dependencies.thread_project_ids(_THREAD_ID) is sentinel
    assert calls == [_THREAD_ID]

    attach = orch_main._session_attach_payload_dependencies()
    assert attach.thread_project_ids is _stub
