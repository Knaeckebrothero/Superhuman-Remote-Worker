"""Turn→commit mapping wiring (Task 4) + the rewind WS handler (Task 5)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.persistent_graph import PersistentLoopCallbacks


def _minimal_callbacks(**overrides):
    """Build the callbacks dataclass with every required field stubbed."""
    import inspect

    kwargs = {}
    for name, param in inspect.signature(PersistentLoopCallbacks).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        kwargs[name] = AsyncMock()
    kwargs.update(overrides)
    return PersistentLoopCallbacks(**kwargs)


def test_callbacks_accept_on_workspace_commit():
    spy = AsyncMock()
    cbs = _minimal_callbacks(on_workspace_commit=spy)
    assert cbs.on_workspace_commit is spy


def test_on_workspace_commit_defaults_none():
    cbs = _minimal_callbacks()
    assert cbs.on_workspace_commit is None


def test_loop_on_workspace_commit_records_via_conn(monkeypatch):
    from src.api import persistent_app as app_mod

    conn = MagicMock()
    conn.record_turn_commit = AsyncMock()
    session = MagicMock()
    session.postgres_conn = conn
    monkeypatch.setattr(app_mod, "_session", session)
    monkeypatch.setattr(app_mod, "_thread_id", "tid-1")

    asyncio.run(app_mod._loop_on_workspace_commit("sha42"))
    conn.record_turn_commit.assert_awaited_once_with("tid-1", "sha42")


def test_loop_on_workspace_commit_tolerates_no_session(monkeypatch):
    from src.api import persistent_app as app_mod

    monkeypatch.setattr(app_mod, "_session", None)
    asyncio.run(app_mod._loop_on_workspace_commit("sha42"))  # must not raise
