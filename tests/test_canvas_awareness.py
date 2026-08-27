"""Lane-free REST/SSE contract for durable Canvas editor awareness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
import pytest

from routers import canvases
from services.canvas_awareness import (
    CanvasAwarenessConflict,
    CanvasAwarenessEditor,
    CanvasAwarenessMutation,
)


_THREAD_ID = "a3333333-3333-3333-3333-333333333333"
_SOURCE_VERSION = "sha256:" + "a" * 64


def _request(*, disconnects: list[bool] | None = None) -> MagicMock:
    request = MagicMock()
    request.cookies = {"srw_session": "opaque"}
    request.headers = {}
    request.is_disconnected = AsyncMock(side_effect=disconnects or [True])
    return request


async def _response_chunks(response) -> list[str]:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return chunks


def test_awareness_routes_publish_the_exact_lane_free_contract() -> None:
    routes = {
        (method, route.path)
        for route in canvases.router.routes
        for method in (route.methods or set())
    }
    prefix = "/api/persistent/threads/{thread_id}/canvases/main/awareness"
    assert ("PUT", f"{prefix}/{{editing_session_id}}") in routes
    assert ("GET", f"{prefix}/stream") in routes


@pytest.mark.asyncio
async def test_put_awareness_is_owner_gated_and_lane_free(monkeypatch) -> None:
    db = object()
    request = _request()
    owner = AsyncMock(return_value=({"id": "owner"}, {"id": _THREAD_ID}))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=15)
    mutate = AsyncMock(
        return_value=CanvasAwarenessMutation(
            applied=True,
            sender_id="b3333333-3333-4333-8333-333333333333",
            sequence=7,
            state="editing",
            expires_at=expires_at,
        )
    )
    monkeypatch.setattr(canvases, "_get_db", lambda: db)
    monkeypatch.setattr(canvases, "require_thread_owner", owner)
    monkeypatch.setattr(canvases, "mutate_canvas_awareness", mutate)

    result = await canvases.put_main_canvas_awareness(
        _THREAD_ID,
        "editor_tab_1",
        canvases.CanvasAwarenessRequest(
            sequence=7,
            state="editing",
            path="output/report.md",
            presentation_revision=3,
            source_version=_SOURCE_VERSION,
        ),
        request,
    )

    assert result.model_dump() == {
        "applied": True,
        "sender_id": "b3333333-3333-4333-8333-333333333333",
        "sequence": 7,
        "state": "editing",
        "expires_at": expires_at,
    }
    owner.assert_awaited_once_with(request, db, _THREAD_ID)
    mutate.assert_awaited_once_with(
        db,
        thread_id=_THREAD_ID,
        editing_session_id="editor_tab_1",
        sequence=7,
        state="editing",
        path="output/report.md",
        presentation_revision=3,
        source_version=_SOURCE_VERSION,
        ttl_seconds=canvases.CANVAS_AWARENESS_TTL_SECONDS,
    )
    assert "execution_lane" not in inspect.getsource(canvases.put_main_canvas_awareness)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("canvas_awareness_sequence_reused", 409),
        ("canvas_awareness_capacity_exhausted", 503),
    ],
)
async def test_put_awareness_maps_conflicts(
    monkeypatch, code: str, status: int
) -> None:
    monkeypatch.setattr(canvases, "_get_db", lambda: object())
    monkeypatch.setattr(
        canvases, "require_thread_owner", AsyncMock(return_value=({}, {}))
    )
    monkeypatch.setattr(
        canvases,
        "mutate_canvas_awareness",
        AsyncMock(side_effect=CanvasAwarenessConflict(code, "conflict")),
    )

    with pytest.raises(HTTPException) as exc:
        await canvases.put_main_canvas_awareness(
            _THREAD_ID,
            "editor_tab_1",
            canvases.CanvasAwarenessRequest(
                sequence=1,
                state="editing",
                path="output/report.md",
                presentation_revision=1,
                source_version=_SOURCE_VERSION,
            ),
            _request(),
        )
    assert exc.value.status_code == status
    assert exc.value.detail["code"] == code


@pytest.mark.asyncio
async def test_stream_emits_complete_named_snapshots_without_ids(monkeypatch) -> None:
    db = object()
    request = _request(disconnects=[False, False, True])
    owner = AsyncMock(return_value=({"id": "owner"}, {"id": _THREAD_ID}))
    editor = CanvasAwarenessEditor(
        sender_id="b3333333-3333-4333-8333-333333333333",
        editing_session_id="editor_tab_1",
        path="output/report.md",
        presentation_revision=3,
        source_version=_SOURCE_VERSION,
        sequence=7,
        ttl_ms=14_000,
    )
    fetch = AsyncMock(side_effect=[(editor,), ()])
    cleanup = AsyncMock(return_value=0)
    monkeypatch.setattr(canvases, "_get_db", lambda: db)
    monkeypatch.setattr(canvases, "require_thread_owner", owner)
    monkeypatch.setattr(canvases, "fetch_canvas_awareness_snapshot", fetch)
    monkeypatch.setattr(canvases, "cleanup_canvas_awareness", cleanup)
    monkeypatch.setattr(canvases, "_CANVAS_AWARENESS_STREAM_POLL_S", 0)

    response = await canvases.stream_main_canvas_awareness(_THREAD_ID, request)
    chunks = await _response_chunks(response)

    assert chunks[0] == ": open\n\n"
    events = [chunk for chunk in chunks if chunk.startswith("event:")]
    assert len(events) == 2
    assert events[0].startswith("event: canvas_awareness\ndata: ")
    assert '"canvas_id":"main"' in events[0]
    assert '"editing_session_id":"editor_tab_1"' in events[0]
    assert '"ttl_ms":14000' in events[0]
    assert events[1] == (
        'event: canvas_awareness\ndata: {"canvas_id":"main","editors":[]}\n\n'
    )
    assert all(
        not any(line.startswith("id:") for line in chunk.splitlines())
        for chunk in chunks
    )
    assert "thread_events" not in inspect.getsource(
        canvases.stream_main_canvas_awareness
    )
    owner.assert_awaited_once_with(request, db, _THREAD_ID)
    cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_reauthorizes_and_sends_separate_idle_keepalive(
    monkeypatch,
) -> None:
    db = object()
    request = _request(disconnects=[False, False, True])
    owner = AsyncMock(return_value=({"id": "owner"}, {"id": _THREAD_ID}))
    fetch = AsyncMock(return_value=())
    monkeypatch.setattr(canvases, "_get_db", lambda: db)
    monkeypatch.setattr(canvases, "require_thread_owner", owner)
    monkeypatch.setattr(canvases, "fetch_canvas_awareness_snapshot", fetch)
    monkeypatch.setattr(canvases, "cleanup_canvas_awareness", AsyncMock(return_value=0))
    monkeypatch.setattr(canvases, "_CANVAS_AWARENESS_STREAM_POLL_S", 0)
    monkeypatch.setattr(canvases, "_CANVAS_AWARENESS_STREAM_REAUTH_S", 0)
    monkeypatch.setattr(canvases, "_CANVAS_AWARENESS_STREAM_KEEPALIVE_S", 0)

    response = await canvases.stream_main_canvas_awareness(_THREAD_ID, request)
    chunks = await _response_chunks(response)

    assert chunks == [
        ": open\n\n",
        'event: canvas_awareness\ndata: {"canvas_id":"main","editors":[]}\n\n',
        ": keepalive\n\n",
    ]
    # One admission check plus one fresh owner check in every polling cycle.
    assert owner.await_count == 3
    assert fetch.await_count == 2
    assert all(
        not any(line.startswith("id:") for line in chunk.splitlines())
        for chunk in chunks
    )


@pytest.mark.asyncio
async def test_stream_closes_when_periodic_owner_check_fails(monkeypatch) -> None:
    db = object()
    request = _request(disconnects=[False])
    owner = AsyncMock(
        side_effect=[
            ({"id": "owner"}, {"id": _THREAD_ID}),
            HTTPException(status_code=403, detail="owner changed"),
        ]
    )
    fetch = AsyncMock(return_value=())
    monkeypatch.setattr(canvases, "_get_db", lambda: db)
    monkeypatch.setattr(canvases, "require_thread_owner", owner)
    monkeypatch.setattr(canvases, "fetch_canvas_awareness_snapshot", fetch)
    monkeypatch.setattr(canvases, "cleanup_canvas_awareness", AsyncMock(return_value=0))
    monkeypatch.setattr(canvases, "_CANVAS_AWARENESS_STREAM_REAUTH_S", 0)

    response = await canvases.stream_main_canvas_awareness(_THREAD_ID, request)
    assert await _response_chunks(response) == [": open\n\n"]
    assert owner.await_count == 2
    fetch.assert_not_awaited()


def test_awareness_modules_have_no_journal_allocator_or_lane_branch() -> None:
    from services import canvas_awareness

    source = inspect.getsource(canvas_awareness)
    route_source = inspect.getsource(canvases.stream_main_canvas_awareness)
    assert "thread_events" not in source
    assert "append_system_frame" not in source
    assert "execution_lane" not in source
    assert "execution_lane" not in route_source
