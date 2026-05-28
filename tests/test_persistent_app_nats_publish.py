"""Tests for NATS publishing of notification events from the agent pod."""

import json
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_emit_publishes_permission_request_to_scoped_subject(monkeypatch):
    """emit_session_event posts to session.events.{oid}.{tid} with payload."""
    monkeypatch.setenv("SESSION_BOUND_THREAD_ID", "t1")
    monkeypatch.setenv("ORCHESTRATOR_ID", "srw-test")

    from src.api import persistent_app

    nc = AsyncMock()
    monkeypatch.setattr(persistent_app, "_nats_client", nc, raising=False)

    await persistent_app.emit_session_event(
        "permission.request",
        {"tool": "shell", "args": "ls"},
    )

    nc.publish.assert_called_once()
    subject = nc.publish.call_args.args[0]
    data = nc.publish.call_args.args[1]
    assert subject == "session.events.srw-test.t1"
    payload = json.loads(data.decode())
    assert payload["thread_id"] == "t1"
    assert payload["method"] == "permission.request"
    assert payload["params"]["tool"] == "shell"
    assert payload["orchestrator_id"] == "srw-test"


@pytest.mark.asyncio
async def test_emit_skips_unmapped_method(monkeypatch):
    """Methods not in the allowlist are not published."""
    monkeypatch.setenv("SESSION_BOUND_THREAD_ID", "t1")
    monkeypatch.setenv("ORCHESTRATOR_ID", "srw-test")

    from src.api import persistent_app

    nc = AsyncMock()
    monkeypatch.setattr(persistent_app, "_nats_client", nc, raising=False)

    await persistent_app.emit_session_event(
        "ai.delta",  # Not a notification method.
        {"content": "hi"},
    )

    nc.publish.assert_not_called()


@pytest.mark.asyncio
async def test_emit_skips_when_thread_id_unset(monkeypatch):
    """No SESSION_BOUND_THREAD_ID → no publish."""
    monkeypatch.delenv("SESSION_BOUND_THREAD_ID", raising=False)
    monkeypatch.setenv("ORCHESTRATOR_ID", "srw-test")

    from src.api import persistent_app

    nc = AsyncMock()
    monkeypatch.setattr(persistent_app, "_nats_client", nc, raising=False)

    await persistent_app.emit_session_event(
        "permission.request",
        {"tool": "shell"},
    )

    nc.publish.assert_not_called()


@pytest.mark.asyncio
async def test_emit_refused_when_orchestrator_id_unset(monkeypatch):
    """ORCHESTRATOR_ID unset → no publish (orchestrator subscribes only to
    scoped subjects; publishing flat would silently drop)."""
    monkeypatch.setenv("SESSION_BOUND_THREAD_ID", "t1")
    monkeypatch.delenv("ORCHESTRATOR_ID", raising=False)

    from src.api import persistent_app

    nc = AsyncMock()
    monkeypatch.setattr(persistent_app, "_nats_client", nc, raising=False)

    await persistent_app.emit_session_event(
        "permission.request",
        {"tool": "shell"},
    )

    nc.publish.assert_not_called()


@pytest.mark.asyncio
async def test_emit_silent_on_publish_failure(monkeypatch):
    """NATS publish exception doesn't propagate (non-fatal)."""
    monkeypatch.setenv("SESSION_BOUND_THREAD_ID", "t1")
    monkeypatch.setenv("ORCHESTRATOR_ID", "srw-test")

    from src.api import persistent_app

    nc = AsyncMock()
    nc.publish.side_effect = RuntimeError("NATS down")
    monkeypatch.setattr(persistent_app, "_nats_client", nc, raising=False)

    # Must not raise.
    await persistent_app.emit_session_event(
        "permission.request",
        {"tool": "shell"},
    )
