"""The officer's notify_user endpoint after the unified notification system
(slice 3): three urgencies, two of them feed rows, no per-officer page budget
and no digest ring.

``page`` → ``_dispatch_officer_page(..., severity="high")``; ``digest`` →
``severity="low"``; ``log`` touches nothing. The response echoes the recorded
row's id so the tool can say what happened; a failed feed write is a 503, not
a silent downgrade.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from orchestrator.routers import agent_officer as agent_officer_router
from orchestrator.schemas.officer_post import OfficerNotifyRequest
from orchestrator.services import officer_paging
from orchestrator.services.officer_paging import OfficerPagingDependencies

THREAD_ID = str(uuid.uuid4())


def _thread(*, officer=True):
    return {
        "id": THREAD_ID,
        "user_id": "legate-1",
        "project_id": str(uuid.uuid4()),
        "metadata": {"config_override": {"officer": {"enabled": officer}}},
    }


DEPS = OfficerPagingDependencies(store=MagicMock(), notifier=MagicMock())


@pytest.fixture
def wired(monkeypatch):
    """The internal-key gate lives on the router and the page dispatch is a
    sibling in the paging module — both are patched where they are read now."""
    monkeypatch.setattr(agent_officer_router, "require_internal", AsyncMock())
    monkeypatch.setattr(
        DEPS, "store", MagicMock(get_thread=AsyncMock(return_value=_thread()))
    )
    dispatch = AsyncMock(return_value="n-1")
    monkeypatch.setattr(officer_paging, "dispatch_officer_page", dispatch)
    return dispatch


def _request():
    request = MagicMock()
    request.app.state.officer_paging_dependencies_factory = lambda: DEPS
    return request


async def _call(
    urgency, message="Capacity exhausted with work queued", subject="Capacity"
):
    body = OfficerNotifyRequest(message=message, urgency=urgency, subject=subject)
    return await agent_officer_router.agent_officer_notify(_request(), THREAD_ID, body)


class TestUrgencies:
    @pytest.mark.asyncio
    async def test_page_is_a_high_row(self, wired):
        out = await _call("page")
        assert out == {"delivered": "page", "notification_id": "n-1"}
        wired.assert_awaited_once()
        args, kwargs = wired.await_args
        assert args[1] == THREAD_ID and args[2] == "Capacity"
        # The dependency object travels as a keyword now; everything else the
        # dispatch is told must still be exactly these two.
        kwargs = dict(kwargs)
        assert kwargs.pop("dependencies") is DEPS
        assert kwargs == {"category": "officer_question", "severity": "high"}

    @pytest.mark.asyncio
    async def test_digest_is_a_low_row(self, wired):
        out = await _call("digest")
        assert out == {"delivered": "digest", "notification_id": "n-1"}
        assert wired.await_args.kwargs["severity"] == "low"

    @pytest.mark.asyncio
    async def test_log_records_nothing(self, wired):
        assert await _call("log") == {"delivered": "log"}
        wired.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_budget_many_pages_all_land(self, wired):
        for _ in range(5):
            assert (await _call("page"))["delivered"] == "page"
        assert wired.await_count == 5
        assert {c.kwargs["severity"] for c in wired.await_args_list} == {"high"}


class TestRejections:
    @pytest.mark.asyncio
    async def test_empty_message_is_400(self, wired):
        with pytest.raises(HTTPException) as e:
            await _call("page", message="   ")
        assert e.value.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_urgency_is_400(self, wired):
        with pytest.raises(HTTPException) as e:
            await _call("shout")
        assert e.value.status_code == 400

    @pytest.mark.asyncio
    async def test_feed_write_failure_is_503_not_a_downgrade(self, wired):
        wired.return_value = None
        with pytest.raises(HTTPException) as e:
            await _call("page")
        assert e.value.status_code == 503

    @pytest.mark.asyncio
    async def test_non_officer_thread_is_409(self, wired):
        DEPS.store.get_thread = AsyncMock(return_value=_thread(officer=False))
        with pytest.raises(HTTPException) as e:
            await _call("page")
        assert e.value.status_code == 409

    @pytest.mark.asyncio
    async def test_missing_thread_is_404(self, wired):
        DEPS.store.get_thread = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as e:
            await _call("page")
        assert e.value.status_code == 404
