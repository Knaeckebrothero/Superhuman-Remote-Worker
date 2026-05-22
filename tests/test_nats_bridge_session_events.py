"""Tests for the session.events.* NATS subscription that re-broadcasts
notification events to the SSE feed with a payload-level thread_id filter."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_session_event_with_matching_thread_id_broadcasts(monkeypatch):
    """When the payload thread_id matches the pod's bound thread,
    the event is forwarded to notification_feed.broadcast."""
    from orchestrator.services.nats_bridge import NatsBridge

    bridge = NatsBridge(url="nats://test")
    db = AsyncMock()
    db.get_thread.return_value = {"id": "t1", "user_id": "u1", "agent_id": "agent-xyz"}

    feed = MagicMock()
    monkeypatch.setattr("orchestrator.services.nats_bridge.notification_feed", feed)
    bridge._db = db

    msg = MagicMock()
    msg.subject = "session.events.t1"
    msg.data = json.dumps(
        {
            "thread_id": "t1",
            "method": "permission.request",
            "params": {"tool": "shell", "args": "rm -rf /"},
        }
    ).encode()

    await bridge._on_session_event(msg)

    feed.broadcast.assert_called_once()
    call_user_id = feed.broadcast.call_args.args[0]
    assert call_user_id == "u1"


@pytest.mark.asyncio
async def test_session_event_with_mismatched_thread_id_dropped(monkeypatch):
    """If the payload claims a different thread_id than the subject,
    the event is dropped (defense-in-depth filter)."""
    from orchestrator.services.nats_bridge import NatsBridge

    bridge = NatsBridge(url="nats://test")
    db = AsyncMock()
    db.get_thread.return_value = {"id": "t1", "user_id": "u1"}

    feed = MagicMock()
    monkeypatch.setattr("orchestrator.services.nats_bridge.notification_feed", feed)
    bridge._db = db

    msg = MagicMock()
    msg.subject = "session.events.t1"
    msg.data = json.dumps(
        {
            "thread_id": "OTHER-thread",
            "method": "permission.request",
            "params": {},
        }
    ).encode()

    await bridge._on_session_event(msg)

    feed.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_session_event_with_unknown_thread_dropped(monkeypatch):
    """If the thread doesn't exist in DB, drop the event."""
    from orchestrator.services.nats_bridge import NatsBridge

    bridge = NatsBridge(url="nats://test")
    db = AsyncMock()
    db.get_thread.return_value = None

    feed = MagicMock()
    monkeypatch.setattr("orchestrator.services.nats_bridge.notification_feed", feed)
    bridge._db = db

    msg = MagicMock()
    msg.subject = "session.events.nonexistent"
    msg.data = json.dumps({"thread_id": "nonexistent", "method": "x"}).encode()

    await bridge._on_session_event(msg)

    feed.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_session_event_with_invalid_json_dropped(monkeypatch):
    """Garbage payload doesn't crash the handler."""
    from orchestrator.services.nats_bridge import NatsBridge

    bridge = NatsBridge(url="nats://test")
    feed = MagicMock()
    monkeypatch.setattr("orchestrator.services.nats_bridge.notification_feed", feed)

    msg = MagicMock()
    msg.subject = "session.events.t1"
    msg.data = b"not-json-at-all"

    await bridge._on_session_event(msg)  # Must not raise.
    feed.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_session_event_with_malformed_subject_dropped(monkeypatch):
    """Subject not matching session.events.{tid} is dropped."""
    from orchestrator.services.nats_bridge import NatsBridge

    bridge = NatsBridge(url="nats://test")
    feed = MagicMock()
    monkeypatch.setattr("orchestrator.services.nats_bridge.notification_feed", feed)

    msg = MagicMock()
    msg.subject = "wrong.shape"
    msg.data = json.dumps({"thread_id": "t1", "method": "permission.request"}).encode()

    await bridge._on_session_event(msg)
    feed.broadcast.assert_not_called()
