"""The Legate note channel — knowledge-base/knowledge/features/officer_legate_channel.md.

A note is direction from the Legate reaching an officer from outside the
cockpit. Two properties are load-bearing and each has a test here:

* The caller is always told which of the three deliveries happened (live
  inject, durable queue, deferred under a conference hold). A note reported as
  "sent" when it only reached a table is worse than an error.
* A note never coalesces with another note. The wake outbox dedups on
  (thread, source, dedup_key) while pending, so every note carries a fresh key
  — two directives in one minute are two directives.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from services import session_wake
from src.shared.pinned_session_identity import PinnedSessionBinding

THREAD_ID = "6ce5bc4c-0000-4000-8000-000000000001"
AGENT_ID = "f898a7dd-0000-4000-8000-000000000002"
PROJECT_ID = "a572e4a0-0000-4000-8000-000000000003"
RUNTIME_GENERATION = "b683f5b1-0000-4000-8000-000000000004"
ATTACH_TOKEN = "c79406c2-0000-4000-8000-000000000005"
POD_UID = "d8a517d3-0000-4000-8000-000000000006"

NOTE = "[Legate note — Legate via MCP, 2026-08-17 09:00 UTC]\n\nStop the theme work."

# A conference hold carries the conference thread; a maintenance hold
# deliberately carries no thread_id (main.py hold_project_officer).
CONFERENCE_HOLD = {"thread_id": "conf-1", "kind": "conference"}
MAINTENANCE_HOLD = {"kind": "maintenance", "note": "migrating the cluster"}


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def json(self):
        if self.status_code in {200, 202}:
            return {"delivery_state": "admitted"}
        return {}


class _FakeAsyncClient:
    posts: list = []
    next_status = 200

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.posts.append((url, json))
        return _FakeResponse(_FakeAsyncClient.next_status)


def _thread(*, hold: dict | None = None, agent_id: str | None = AGENT_ID) -> dict:
    officer: dict = {"enabled": True}
    if hold is not None:
        officer["hold"] = hold
    return {
        "id": THREAD_ID,
        "user_id": "u",
        "status": "active",
        "agent_id": agent_id,
        "execution_lane": "pinned",
        "runtime_generation": RUNTIME_GENERATION,
        "runtime_attach_token": ATTACH_TOKEN if agent_id is not None else None,
        "runtime_retirement_token": None,
        "project_id": PROJECT_ID,
        "title": "Centurion — Better Resavio",
        "metadata": {"config_override": {"officer": officer}},
    }


def _agent(**over) -> dict:
    a = {
        "id": AGENT_ID,
        "thread_id": THREAD_ID,
        "status": "session",
        "pod_ip": "10.1.2.3",
        "pod_port": 8001,
        "hostname": "srw-officer-agent",
        "pod_uid": POD_UID,
    }
    a.update(over)
    return a


def _binding(agent: dict | None) -> PinnedSessionBinding | None:
    if agent is None:
        return None
    return PinnedSessionBinding(
        thread_id=THREAD_ID,
        runtime_generation=RUNTIME_GENERATION,
        agent_id=AGENT_ID,
        runtime_attach_token=ATTACH_TOKEN,
        agent_hostname=str(agent["hostname"]),
        pod_namespace="srw",
        pod_uid=str(agent["pod_uid"]),
        pod_ip=str(agent["pod_ip"]),
        pod_port=int(agent["pod_port"]),
        agent_status=str(agent["status"]),
    )


def _db(*, agent=None) -> SimpleNamespace:
    return SimpleNamespace(
        get_agent=AsyncMock(return_value=agent),
        get_pinned_session_binding=AsyncMock(return_value=_binding(agent)),
        enqueue_session_wake_event=AsyncMock(return_value=True),
        save_thread_message=AsyncMock(),
    )


@pytest.fixture(autouse=True)
def _reset_http(monkeypatch):
    _FakeAsyncClient.posts = []
    _FakeAsyncClient.next_status = 200
    monkeypatch.setattr(session_wake.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(session_wake, "probe_ready", AsyncMock(return_value=True))


@pytest.mark.asyncio
async def test_a_live_officer_receives_the_note_on_his_input_queue():
    db = _db(agent=_agent())

    assert await session_wake.deliver_officer_note(db, _thread(), NOTE) == "live"

    url, body = _FakeAsyncClient.posts[0]
    assert url == "http://10.1.2.3:8001/api/input"
    assert body["content"] == NOTE
    assert body["role"] == "event"
    UUID(body["delivery_id"])
    db.enqueue_session_wake_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_note_with_no_live_pod_is_queued_verbatim_for_the_next_wake():
    db = _db(agent=None)

    assert await session_wake.deliver_officer_note(db, _thread(), NOTE) == "queued"

    kwargs = db.enqueue_session_wake_event.await_args.kwargs
    assert kwargs["source"] == "legate"
    assert kwargs["payload"]["message"] == NOTE
    UUID(kwargs["payload"]["_delivery_id"])
    assert kwargs["project_id"] == PROJECT_ID


@pytest.mark.asyncio
async def test_refused_live_note_queues_the_same_delivery_identity():
    db = _db(agent=_agent())
    _FakeAsyncClient.next_status = 503

    assert await session_wake.deliver_officer_note(db, _thread(), NOTE) == "queued"

    posted_id = _FakeAsyncClient.posts[0][1]["delivery_id"]
    queued_id = db.enqueue_session_wake_event.await_args.kwargs["payload"][
        "_delivery_id"
    ]
    assert queued_id == posted_id
    UUID(queued_id)


@pytest.mark.asyncio
async def test_a_note_under_a_conference_hold_queues_without_injecting():
    """The conference is the single writer; the note waits for the brief wake."""
    db = _db(agent=_agent())

    assert (
        await session_wake.deliver_officer_note(db, _thread(hold=CONFERENCE_HOLD), NOTE)
        == "held"
    )

    assert _FakeAsyncClient.posts == []
    db.enqueue_session_wake_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_queued_notes_never_coalesce():
    db = _db(agent=None)

    await session_wake.deliver_officer_note(db, _thread(), "first")
    await session_wake.deliver_officer_note(db, _thread(), "second")

    keys = {
        call.kwargs["dedup_key"]
        for call in db.enqueue_session_wake_event.await_args_list
    }
    assert len(keys) == 2


@pytest.mark.asyncio
async def test_a_rejected_live_inject_falls_back_to_the_durable_queue():
    """A pod that is up but has no loop attached answers 503; the note survives."""
    db = _db(agent=_agent())
    _FakeAsyncClient.next_status = 503

    assert await session_wake.deliver_officer_note(db, _thread(), NOTE) == "queued"

    db.enqueue_session_wake_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_maintenance_hold_still_lets_the_legate_through():
    """He was stood down BY the Legate; the note is how the Legate redirects him."""
    db = _db(agent=_agent())

    assert (
        await session_wake.deliver_officer_note(
            db, _thread(hold=MAINTENANCE_HOLD), NOTE
        )
        == "live"
    )
    assert _FakeAsyncClient.posts


@pytest.mark.asyncio
async def test_a_held_officer_with_no_live_pod_is_reported_held_not_queued():
    """The drain skips held threads, so 'queued' would overpromise delivery."""
    db = _db(agent=None)

    assert (
        await session_wake.deliver_officer_note(
            db, _thread(hold=MAINTENANCE_HOLD), NOTE
        )
        == "held"
    )
    db.enqueue_session_wake_event.assert_awaited_once()


# =========================================================================
# The endpoint — POST /api/projects/{project_id}/officer/note
# =========================================================================

from unittest.mock import MagicMock  # noqa: E402

import main as orch_main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from main import OfficerNoteRequest, send_project_officer_note  # noqa: E402


@pytest.fixture
def endpoint_db(monkeypatch):
    db = SimpleNamespace(
        get_officer_thread_for_project=AsyncMock(return_value=_thread()),
        get_pending_officer_timer=AsyncMock(return_value=None),
        enqueue_session_wake_event=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(orch_main, "postgres_db", db)
    return db


@pytest.fixture
def as_project_owner(monkeypatch):
    gate = AsyncMock(
        return_value=(
            {"id": "u1", "display_name": "Legate", "auth_method": "mcp"},
            {"name": "Better Resavio"},
        )
    )
    monkeypatch.setattr(orch_main, "require_project_owner", gate)
    return gate


@pytest.fixture
def delivery(monkeypatch):
    stub = AsyncMock(return_value="live")
    monkeypatch.setattr(orch_main, "_deliver_officer_note", stub)
    return stub


@pytest.mark.asyncio
async def test_the_note_endpoint_sits_behind_project_owner(
    monkeypatch, endpoint_db, delivery
):
    monkeypatch.setattr(
        orch_main,
        "require_project_owner",
        AsyncMock(side_effect=HTTPException(status_code=403, detail="owner required")),
    )
    with pytest.raises(HTTPException) as exc:
        await send_project_officer_note(
            MagicMock(), PROJECT_ID, OfficerNoteRequest(message="hello")
        )
    assert exc.value.status_code == 403
    delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_vacant_post_409s_instead_of_swallowing_the_note(
    endpoint_db, as_project_owner, delivery
):
    endpoint_db.get_officer_thread_for_project = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc:
        await send_project_officer_note(
            MagicMock(), PROJECT_ID, OfficerNoteRequest(message="hello")
        )
    assert exc.value.status_code == 409
    assert "vacant" in str(exc.value.detail).lower()
    delivery.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "x" * 8001])
async def test_an_unusable_message_400s(endpoint_db, as_project_owner, delivery, bad):
    with pytest.raises(HTTPException) as exc:
        await send_project_officer_note(
            MagicMock(), PROJECT_ID, OfficerNoteRequest(message=bad)
        )
    assert exc.value.status_code == 400
    delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_delivered_text_names_its_author_and_carries_the_message(
    endpoint_db, as_project_owner, delivery
):
    """An assistant-composed note must not read as words the human typed."""
    result = await send_project_officer_note(
        MagicMock(), PROJECT_ID, OfficerNoteRequest(message="Cut the theme work.")
    )

    text = delivery.await_args.args[2]
    assert text.startswith("[Legate note — Legate via MCP")
    assert text.endswith("Cut the theme work.")
    assert result["delivered"] == "live"
    assert result["thread_id"] == THREAD_ID


@pytest.mark.asyncio
async def test_a_queued_note_reports_when_he_will_read_it(
    endpoint_db, as_project_owner, delivery
):
    delivery.return_value = "queued"
    endpoint_db.get_pending_officer_timer = AsyncMock(
        return_value={"fire_at": "2026-08-17T09:30:00+00:00"}
    )

    result = await send_project_officer_note(
        MagicMock(), PROJECT_ID, OfficerNoteRequest(message="Report on the board.")
    )

    assert result["delivered"] == "queued"
    assert result["next_wake_at"] == "2026-08-17T09:30:00+00:00"


def test_the_legate_routes_are_wired():
    from main import app

    registered = {
        (method, getattr(route, "path", ""))
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
    }
    assert ("POST", "/api/projects/{project_id}/officer/note") in registered
    assert ("GET", "/api/officers") in registered


@pytest.mark.asyncio
async def test_a_live_attempt_that_raises_still_queues_the_note(monkeypatch):
    """The transport is allowed to fail; losing the Legate's words is not."""
    db = _db(agent=_agent())
    monkeypatch.setattr(
        session_wake, "probe_ready", AsyncMock(side_effect=RuntimeError("pod gone"))
    )

    assert await session_wake.deliver_officer_note(db, _thread(), NOTE) == "queued"
    db.enqueue_session_wake_event.assert_awaited_once()


def test_the_fallback_renderer_still_carries_the_note_itself():
    """When the sitrep build fails, the minimal renderer is what he reads."""
    text = session_wake._format_officer_wake(
        [
            {
                "source": "legate",
                "dedup_key": "ab12cd34",
                "payload": {"message": NOTE},
            }
        ]
    )

    assert NOTE.split("\n\n")[1] in text
    assert "ab12cd34" not in text
