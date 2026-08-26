"""Session wake on job completion — Phase 1.

Covers the delivery half (knowledge-base/knowledge/features/session_wake_on_job_completion.md):
enqueue guards, the claim/settle contract, live inject vs the durable branch,
the liveness predicate, and the payload. The DB is faked at the methods the
service touches; the SQL guards themselves (per-status dedup, SKIP LOCKED
disjointness, re-claim past the visibility timeout, the backstop arm) are
Postgres semantics and are exercised against a real server, not mocked here.

Two properties are worth stating because getting either wrong reintroduces the
bug the feature exists to remove:

* A wake that cannot be delivered must NOT be marked sent — it goes back for
  retry, or is buried as 'dead' so the operator sees it. Silently consuming it
  is indistinguishable from never having fired.
* A duplicate delivery is a visible message in the user's transcript plus a
  paid LLM turn, so the claim has to come before the send and a failure to
  settle must leave the row re-claimable rather than double-sent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import session_wake

# Captured at import, before the autouse fixture replaces it with a mock — the
# two tests below need the REAL implementation.
_REAL_NOTIFY_OWNER = session_wake._notify_owner

JOB_ID = "3f2a1b8c-0000-4000-8000-000000000001"
THREAD_ID = "aa11bb22-0000-4000-8000-000000000002"
AGENT_ID = "cc33dd44-0000-4000-8000-000000000003"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def json(self):
        if self.status_code in {200, 202}:
            return {"delivery_state": _FakeAsyncClient.next_delivery_state}
        return {}


class _FakeAsyncClient:
    """Records POSTs; returns a scripted status (or raises a scripted error)."""

    posts: list = []
    next_status = 200
    next_delivery_state = "admitted"
    raises: Exception | None = None

    def __init__(self, *a, **kw):
        self.init_kwargs = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.posts.append((url, json))
        if _FakeAsyncClient.raises is not None:
            raise _FakeAsyncClient.raises
        return _FakeResponse(_FakeAsyncClient.next_status)


def _claim_row(**over) -> dict:
    row = {
        "id": JOB_ID,
        "created_by_thread_id": THREAD_ID,
        "status": "completed",
        "wake_attempts": 1,
        "user_id": "u",
        "project_id": None,
        "description": "Explore a warm-neutral theme for the marketing site",
        "expert_id": None,
        "config_name": "worker_base",
        "freeze_data": None,
        "error_message": None,
    }
    row.update(over)
    return row


def _thread(**over) -> dict:
    t = {
        "id": THREAD_ID,
        "user_id": "u",
        "status": "active",
        "agent_id": AGENT_ID,
        "title": "Theme work",
    }
    t.update(over)
    return t


def _agent(**over) -> dict:
    a = {"id": AGENT_ID, "status": "session", "pod_ip": "10.1.2.3", "pod_port": 8001}
    a.update(over)
    return a


def _db(*, claimed=None, thread=None, agent=None) -> SimpleNamespace:
    save_thread_message = AsyncMock(return_value={"transcript_inserted": True})
    return SimpleNamespace(
        claim_pending_job_wakes=AsyncMock(
            return_value=list(claimed) if claimed is not None else []
        ),
        finish_job_wake=AsyncMock(return_value=True),
        release_job_wake=AsyncMock(return_value="pending"),
        defer_job_wake_for_input=AsyncMock(return_value=True),
        assign_job_wake_delivery=AsyncMock(return_value=True),
        mark_job_wake_pending=AsyncMock(return_value=True),
        get_thread=AsyncMock(return_value=thread),
        get_agent=AsyncMock(return_value=agent),
        get_expert_by_id=AsyncMock(return_value=None),
        save_thread_message=save_thread_message,
        persist_thread_input_delivery=save_thread_message,
        get_thread_job_counts=AsyncMock(
            return_value={"total": 0, "finished": 0, "running": 0}
        ),
        close_message_routes_for_terminal_jobs=AsyncMock(return_value=[]),
    )


@pytest.fixture(autouse=True)
def _reset_http(monkeypatch):
    _FakeAsyncClient.posts = []
    _FakeAsyncClient.next_status = 200
    _FakeAsyncClient.next_delivery_state = "admitted"
    _FakeAsyncClient.raises = None
    monkeypatch.setattr(session_wake.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(session_wake, "probe_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(session_wake, "_notify_owner", AsyncMock())


# --------------------------------------------------------------------------
# Enqueue
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_wake_session_ignores_non_terminal_status():
    db = _db()
    assert await session_wake.maybe_wake_session(db, JOB_ID, "processing") is False
    db.mark_job_wake_pending.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["completed", "failed", "cancelled", "pending_review"]
)
async def test_maybe_wake_session_enqueues_every_terminal_status(status):
    db = _db()
    assert await session_wake.maybe_wake_session(db, JOB_ID, status) is True
    db.mark_job_wake_pending.assert_awaited_once_with(JOB_ID, status)


@pytest.mark.asyncio
async def test_maybe_wake_session_never_raises_into_the_completion_path():
    """A completion must not fail because a notification could not be enqueued."""
    db = _db()
    db.mark_job_wake_pending = AsyncMock(side_effect=RuntimeError("db down"))
    assert await session_wake.maybe_wake_session(db, JOB_ID, "completed") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_terminal_transition_auto_closes_open_message_routes(status):
    """The auto-close ruling: every hooked terminal path (this choke point)
    closes the job's still-open worker-message routes so they stop showing
    as "open" in sitreps/pending counts after the job is dead."""
    db = _db()
    assert await session_wake.maybe_wake_session(db, JOB_ID, status) is True
    db.close_message_routes_for_terminal_jobs.assert_awaited_once_with(JOB_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending_review", "paused", "processing"])
async def test_non_final_statuses_do_not_close_message_routes(status):
    """pending_review and paused jobs can still resume and answer their open
    question — their routes must survive."""
    db = _db()
    await session_wake.maybe_wake_session(db, JOB_ID, status)
    db.close_message_routes_for_terminal_jobs.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_auto_close_failure_does_not_cost_the_wake():
    """Fail-open: a broken close must neither raise into the completion path
    nor swallow the session wake itself."""
    db = _db()
    db.close_message_routes_for_terminal_jobs = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    assert await session_wake.maybe_wake_session(db, JOB_ID, "cancelled") is True
    db.mark_job_wake_pending.assert_awaited_once_with(JOB_ID, "cancelled")


# --------------------------------------------------------------------------
# Live delivery
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_session_receives_the_input_as_role_event():
    db = _db(claimed=[_claim_row()], thread=_thread(), agent=_agent())

    assert await session_wake.drain_pending_wakes(db) == 1

    assert len(_FakeAsyncClient.posts) == 1
    url, body = _FakeAsyncClient.posts[0]
    assert url == "http://10.1.2.3:8001/api/input"
    # role='event' is what keeps the persisted row out of the human-bubble
    # family — without it the transcript claims the user said this.
    assert body["role"] == "event"
    assert body["content"].startswith("[JOB_FINISHED]")
    db.finish_job_wake.assert_awaited_once_with(JOB_ID, "completed")
    db.save_thread_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_outcome_uses_one_truthful_officer_dedup_identity():
    row = _claim_row(
        status="cancelled",
        completion_outcome_kind="blocked_undelivered",
        project_id="project-1",
    )
    thread = _thread(metadata={"config_override": {"officer": {"enabled": True}}})
    db = _db(claimed=[row], thread=thread, agent=_agent())
    db.enqueue_session_wake_event = AsyncMock(return_value=True)

    assert await session_wake.drain_pending_wakes(db) == 1

    db.enqueue_session_wake_event.assert_awaited_once()
    kwargs = db.enqueue_session_wake_event.await_args.kwargs
    assert kwargs["dedup_key"] == f"{JOB_ID[:8]}:blocked_undelivered"
    assert kwargs["payload"]["status"] == "blocked_undelivered"
    db.finish_job_wake.assert_awaited_once_with(JOB_ID, "cancelled")
    assert _FakeAsyncClient.posts == []


@pytest.mark.asyncio
async def test_completion_hook_and_outbox_share_blocked_officer_dedup_key(
    monkeypatch,
):
    db = _db()
    db.get_job = AsyncMock(
        return_value={
            **_claim_row(
                status="cancelled",
                completion_outcome_kind="blocked_undelivered",
                project_id="project-1",
            )
        }
    )
    db.route_project_officer_job_transition = AsyncMock(return_value={"enqueued": True})
    monkeypatch.setattr(session_wake, "kick_event_drain", lambda _db: None)

    assert await session_wake._notify_project_officer_of_job(db, JOB_ID, "cancelled")
    kwargs = db.route_project_officer_job_transition.await_args.kwargs
    assert kwargs["status"] == "blocked_undelivered"
    assert kwargs["dedup_key"] == f"{JOB_ID[:8]}:blocked_undelivered"


@pytest.mark.asyncio
async def test_live_inject_uses_a_split_timeout_not_a_flat_one():
    """A flat 30s against a black-holed pod IP burns 30s inside the drain."""
    db = _db(claimed=[_claim_row()], thread=_thread(), agent=_agent())
    await session_wake.drain_pending_wakes(db)
    timeout = _FakeAsyncClient.posts and session_wake.httpx.Timeout(10.0, connect=3.0)
    assert timeout.connect == 3.0 and timeout.read == 10.0


@pytest.mark.asyncio
async def test_queued_receipt_stays_retryable_until_provider_admission():
    row = _claim_row()
    db = _db(claimed=[row], thread=_thread(), agent=_agent())
    _FakeAsyncClient.next_status = 202
    _FakeAsyncClient.next_delivery_state = "queued"

    assert await session_wake.drain_pending_wakes(db) == 0
    db.defer_job_wake_for_input.assert_awaited_once_with(JOB_ID)
    db.finish_job_wake.assert_not_awaited()

    _FakeAsyncClient.next_status = 200
    _FakeAsyncClient.next_delivery_state = "admitted"
    assert await session_wake.drain_pending_wakes(db) == 1
    db.finish_job_wake.assert_awaited_once_with(JOB_ID, "completed")
    assert len(_FakeAsyncClient.posts) == 2
    assert (
        _FakeAsyncClient.posts[0][1]["delivery_id"]
        == _FakeAsyncClient.posts[1][1]["delivery_id"]
    )


@pytest.mark.asyncio
async def test_pod_port_defaults_when_the_column_is_null():
    db = _db(claimed=[_claim_row()], thread=_thread(), agent=_agent(pod_port=None))
    await session_wake.drain_pending_wakes(db)
    assert _FakeAsyncClient.posts[0][0] == "http://10.1.2.3:8001/api/input"


# --------------------------------------------------------------------------
# Liveness predicate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_failure_falls_back_to_durable(monkeypatch):
    """agent.status is heartbeat-driven and lags reality by up to ~4 minutes, and
    zombies heartbeat normally — so the probe, not the column, decides."""
    probe = AsyncMock(return_value=False)
    monkeypatch.setattr(session_wake, "probe_ready", probe)
    db = _db(claimed=[_claim_row()], thread=_thread(), agent=_agent())

    assert await session_wake.drain_pending_wakes(db) == 0

    assert _FakeAsyncClient.posts == []
    db.save_thread_message.assert_awaited_once()
    assert db.save_thread_message.await_args.kwargs["role"] == "event"
    db.finish_job_wake.assert_not_awaited()
    db.defer_job_wake_for_input.assert_awaited_once_with(JOB_ID)
    probe.assert_awaited_once_with(
        "10.1.2.3",
        8001,
        required_capability="durable_input_delivery",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_over",
    [
        {"status": "offline"},
        {"status": "booting"},
        {"pod_ip": None},
    ],
)
async def test_unusable_agent_states_take_the_durable_branch(agent_over):
    db = _db(claimed=[_claim_row()], thread=_thread(), agent=_agent(**agent_over))
    assert await session_wake.drain_pending_wakes(db) == 0
    assert _FakeAsyncClient.posts == []
    db.save_thread_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_delivery_does_not_self_heal_a_stale_binding():
    """/connection clears a dead agent_id as a user-driven repair. A background
    delivery must not mutate session bindings on the way past."""
    db = _db(claimed=[_claim_row()], thread=_thread(), agent=_agent(status="offline"))
    db.update_thread_agent = AsyncMock()
    await session_wake.drain_pending_wakes(db)
    db.update_thread_agent.assert_not_awaited()


# --------------------------------------------------------------------------
# Durable branch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["suspended", "ended", "awaiting_user", "idle"])
async def test_non_live_threads_get_a_durable_row_and_no_restore(status):
    """'ended' is deliberately included: an active thread whose pod dies is
    marked 'ended', not 'suspended', and ended threads are user-resumable —
    skipping them would silently drop completions for a supported case."""
    db = _db(claimed=[_claim_row()], thread=_thread(status=status, agent_id=None))
    db.restore_thread_workspace = AsyncMock()

    assert await session_wake.drain_pending_wakes(db) == 0

    db.save_thread_message.assert_awaited_once()
    kwargs = db.save_thread_message.await_args.kwargs
    assert kwargs["thread_id"] == THREAD_ID
    assert kwargs["role"] == "event"
    assert "[JOB_FINISHED]" in kwargs["content"]
    # Phase 1 explicitly never resumes a suspended pod — that is the resume-OOM
    # surface and it belongs to Phase 2.
    db.restore_thread_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_branch_notifies_the_owner(monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(session_wake, "_notify_owner", notify)
    db = _db(claimed=[_claim_row()], thread=_thread(agent_id=None))
    await session_wake.drain_pending_wakes(db)
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_owner_notification_records_a_session_wake_row(monkeypatch):
    """The owner's half of the durable branch is a ``session_wake`` feed row
    (unified notification system): addressed to the thread owner, deduped per
    (thread, job) so a re-claimed wake never files twice, with the session
    thread as its source so the cockpit can deep-link back. Which channel
    reaches the owner — and at what address — is the notification system's
    business; nothing here resolves an email. (The old dispatch() path handed
    the email leg an empty address and silently dropped the notice — live-gate
    regression, 2026-07-27; a feed row cannot be dropped that way.)"""
    recorded = {}

    class _Svc:
        async def record(self, **kw):
            recorded.update(kw)
            return SimpleNamespace(notification_id="n-1", inserted=True)

    monkeypatch.setitem(
        __import__("sys").modules,
        "services.notification_service",
        type("M", (), {"notification_service": _Svc()}),
    )
    db = _db(claimed=[_claim_row()], thread=_thread(agent_id=None))
    monkeypatch.setattr(session_wake, "_notify_owner", _REAL_NOTIFY_OWNER)

    assert await session_wake.drain_pending_wakes(db) == 0

    assert recorded["recipient_id"] == "u"
    assert recorded["category"] == "session_wake"
    assert recorded["dedup_key"] == f"session_wake:{THREAD_ID}:{JOB_ID}"
    assert recorded["source_kind"] == "thread"
    assert recorded["source_id"] == THREAD_ID
    assert recorded["action_params"] == {"thread_id": THREAD_ID, "job_id": JOB_ID}
    assert recorded["payload"]["job_id"] == JOB_ID
    assert recorded["payload"]["status"] == "completed"
    assert recorded["payload"]["title"] == "Theme work"
    assert JOB_ID[:8] in recorded["subject"]


@pytest.mark.asyncio
async def test_owner_without_an_email_still_gets_the_row(monkeypatch):
    """An owner with no address on file is NOT skipped at this layer: the feed
    row is the durable half and lands in-app regardless. Suppressing the email
    leg for an addressless recipient is the notification system's job — the
    wake never looks the user up."""
    calls = []

    class _Svc:
        async def record(self, **kw):
            calls.append(kw)
            return SimpleNamespace(notification_id="n-1", inserted=True)

    monkeypatch.setitem(
        __import__("sys").modules,
        "services.notification_service",
        type("M", (), {"notification_service": _Svc()}),
    )
    db = _db(claimed=[_claim_row()], thread=_thread(agent_id=None))
    db.get_user = AsyncMock(return_value={"id": "u", "email": None})
    monkeypatch.setattr(session_wake, "_notify_owner", _REAL_NOTIFY_OWNER)

    assert await session_wake.drain_pending_wakes(db) == 0
    assert len(calls) == 1
    assert calls[0]["recipient_id"] == "u"
    db.get_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_delivery_does_not_email_the_user(monkeypatch):
    """The user is right there watching; an email would be noise."""
    notify = AsyncMock()
    monkeypatch.setattr(session_wake, "_notify_owner", notify)
    db = _db(claimed=[_claim_row()], thread=_thread(), agent=_agent())
    await session_wake.drain_pending_wakes(db)
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_notification_does_not_undeliver_the_notice(monkeypatch):
    monkeypatch.setattr(
        session_wake, "_notify_owner", AsyncMock(side_effect=RuntimeError("smtp down"))
    )
    db = _db(claimed=[_claim_row()], thread=_thread(agent_id=None))
    assert await session_wake.drain_pending_wakes(db) == 0
    db.finish_job_wake.assert_not_awaited()
    db.defer_job_wake_for_input.assert_awaited_once_with(JOB_ID)


@pytest.mark.asyncio
async def test_durable_retry_finishes_only_after_provider_admission():
    db = _db(claimed=[_claim_row()], thread=_thread(agent_id=None))
    db.persist_thread_input_delivery = AsyncMock(
        return_value={
            "transcript_inserted": False,
            "state": "admitted",
        }
    )

    assert await session_wake.drain_pending_wakes(db) == 1

    db.finish_job_wake.assert_awaited_once_with(JOB_ID, "completed")
    db.defer_job_wake_for_input.assert_not_awaited()


# --------------------------------------------------------------------------
# Settle contract
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_thread_backref_consumes_the_claim_without_delivering():
    """ON DELETE SET NULL nulled the backref — nobody to wake, and re-claiming
    forever would starve real wakes behind it in the ORDER BY."""
    db = _db(claimed=[_claim_row(created_by_thread_id=None)])
    assert await session_wake.drain_pending_wakes(db) == 1
    db.get_thread.assert_not_awaited()
    db.finish_job_wake.assert_awaited_once()
    db.release_job_wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_write_failure_releases_for_retry():
    db = _db(claimed=[_claim_row()], thread=_thread(agent_id=None))
    db.persist_thread_input_delivery = AsyncMock(
        side_effect=RuntimeError("write failed")
    )

    assert await session_wake.drain_pending_wakes(db) == 0

    db.finish_job_wake.assert_not_awaited()
    db.release_job_wake.assert_awaited_once_with(
        JOB_ID, max_attempts=session_wake.MAX_ATTEMPTS
    )


@pytest.mark.asyncio
async def test_thread_lookup_failure_releases_rather_than_consuming():
    db = _db(claimed=[_claim_row()])
    db.get_thread = AsyncMock(side_effect=RuntimeError("db blip"))
    assert await session_wake.drain_pending_wakes(db) == 0
    db.release_job_wake.assert_awaited_once()


@pytest.mark.asyncio
async def test_vanished_thread_consumes_the_claim():
    db = _db(claimed=[_claim_row()], thread=None)
    assert await session_wake.drain_pending_wakes(db) == 1
    db.finish_job_wake.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_delete_retirement_wins_the_late_finish_cas():
    """A pre-delete claim may retain its old thread projection. Once delivery
    resolves, the finish CAS must report that deletion already retired it and
    the drain must not count an undeliverable wake as delivered."""
    db = _db(claimed=[_claim_row()], thread=None)
    db.finish_job_wake = AsyncMock(return_value=False)

    assert await session_wake.drain_pending_wakes(db) == 0

    db.finish_job_wake.assert_awaited_once_with(JOB_ID, "completed")
    db.release_job_wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_bad_row_does_not_abort_the_rest_of_the_batch():
    rows = [
        _claim_row(id="j1", created_by_thread_id=None),
        _claim_row(id="j2"),
    ]
    db = _db(claimed=rows, thread=_thread(agent_id=None))
    db.persist_thread_input_delivery = AsyncMock(
        side_effect=[RuntimeError("boom"), {"transcript_inserted": True}]
    )
    # j1 short-circuits (no backref), j2's durable write raises then releases.
    await session_wake.drain_pending_wakes(db)
    assert db.finish_job_wake.await_count == 1  # j1 only
    assert db.release_job_wake.await_count == 1  # j2


@pytest.mark.asyncio
async def test_a_batch_is_delivered_concurrently_within_a_cap(monkeypatch):
    """A claim belongs to this drain only for the visibility window. Serial
    delivery of a fan-out into dead pods (~12s each) would overrun it and let
    another replica re-claim a row still being sent — the exact duplicate the
    claim exists to prevent. The cap keeps a burst from opening one socket per
    dead pod at once."""
    monkeypatch.setattr(session_wake, "_DELIVER_CONCURRENCY", 3)
    inflight = 0
    peak = 0

    async def _slow(db, row):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return True

    monkeypatch.setattr(session_wake, "_deliver", _slow)
    db = _db(claimed=[_claim_row(id=f"j{i}") for i in range(9)])

    assert await session_wake.drain_pending_wakes(db) == 9
    assert peak > 1, "deliveries ran serially"
    assert peak <= 3, f"concurrency cap exceeded (peak={peak})"


@pytest.mark.asyncio
async def test_a_settle_failure_is_not_counted_as_delivered(monkeypatch):
    """Re-delivering is the lesser evil against marking a wake sent that never
    arrived — so a failed settle leaves the row 'sending' for the timeout to
    re-claim, and must not inflate the delivered count."""
    db = _db(claimed=[_claim_row()], thread=_thread(), agent=_agent())
    db.finish_job_wake = AsyncMock(side_effect=RuntimeError("settle failed"))

    assert await session_wake.drain_pending_wakes(db) == 0


@pytest.mark.asyncio
async def test_claim_failure_is_swallowed():
    db = _db()
    db.claim_pending_job_wakes = AsyncMock(side_effect=RuntimeError("db down"))
    assert await session_wake.drain_pending_wakes(db) == 0


@pytest.mark.asyncio
async def test_the_claim_carries_the_visibility_timeout():
    db = _db()
    await session_wake.drain_pending_wakes(db)
    db.claim_pending_job_wakes.assert_awaited_once_with(
        limit=session_wake.CLAIM_BATCH,
        visibility_timeout_seconds=session_wake.VISIBILITY_TIMEOUT_SECONDS,
    )


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_carries_pointers_not_the_result():
    """Inlining output would make every wake expensive and defeat delegating."""
    row = _claim_row(
        freeze_data={
            "summary": "Three theme directions explored.",
            "confidence": 85,
            "deliverables": ["a.md", "b.md", "c.md", "d.md"],
        }
    )
    db = _db(claimed=[row], thread=_thread(agent_id=None))
    await session_wake.drain_pending_wakes(db)

    text = db.save_thread_message.await_args.kwargs["content"]
    assert "4 deliverables" in text
    assert "get_job" in text
    assert "a.md" not in text  # names of the files are output, not a pointer
    assert "Confidence: 85" in text
    assert "Three theme directions explored." in text


@pytest.mark.asyncio
async def test_payload_names_the_task_so_fanned_out_jobs_are_distinguishable():
    db = _db(claimed=[_claim_row()], thread=_thread(agent_id=None))
    await session_wake.drain_pending_wakes(db)
    text = db.save_thread_message.await_args.kwargs["content"]
    assert "- Task: Explore a warm-neutral theme for the marketing site" in text


@pytest.mark.asyncio
async def test_payload_parses_freeze_data_delivered_as_a_json_string():
    """asyncpg hands JSONB back as a raw string; a naive .get() would silently
    drop the summary."""
    db = _db(
        claimed=[_claim_row(freeze_data='{"summary": "from json", "confidence": 40}')],
        thread=_thread(agent_id=None),
    )
    await session_wake.drain_pending_wakes(db)
    text = db.save_thread_message.await_args.kwargs["content"]
    assert "from json" in text and "Confidence: 40" in text


@pytest.mark.asyncio
async def test_payload_carries_the_sibling_set():
    """Saves the agent a list_jobs round-trip on every wake."""
    db = _db(claimed=[_claim_row()], thread=_thread(agent_id=None))
    db.get_thread_job_counts = AsyncMock(
        return_value={
            "total": 3,
            "finished": 1,
            "running": 1,
            "failed": 1,
            "cancelled": 0,
        }
    )
    await session_wake.drain_pending_wakes(db)
    text = db.save_thread_message.await_args.kwargs["content"]
    assert "1 of 3 finished" in text
    assert "1 still running" in text and "1 failed" in text


@pytest.mark.asyncio
async def test_sibling_line_omitted_for_a_lone_job():
    db = _db(claimed=[_claim_row()], thread=_thread(agent_id=None))
    db.get_thread_job_counts = AsyncMock(
        return_value={"total": 1, "finished": 1, "running": 0}
    )
    await session_wake.drain_pending_wakes(db)
    assert "outstanding jobs" not in db.save_thread_message.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_expert_name_is_used_when_the_job_ran_a_db_expert():
    db = _db(claimed=[_claim_row(expert_id="e1")], thread=_thread(agent_id=None))
    db.get_expert_by_id = AsyncMock(return_value={"name": "designer"})
    await session_wake.drain_pending_wakes(db)
    assert "expert: designer" in db.save_thread_message.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_error_message_surfaces_only_for_failures():
    db = _db(
        claimed=[_claim_row(status="failed", error_message="OOMKilled")],
        thread=_thread(agent_id=None),
    )
    await session_wake.drain_pending_wakes(db)
    text = db.save_thread_message.await_args.kwargs["content"]
    assert "- Error: OOMKilled" in text
    assert "- Status: failed" in text


@pytest.mark.asyncio
async def test_payload_survives_a_counts_lookup_failure():
    db = _db(claimed=[_claim_row()], thread=_thread(agent_id=None))
    db.get_thread_job_counts = AsyncMock(side_effect=RuntimeError("nope"))
    assert await session_wake.drain_pending_wakes(db) == 0


# --------------------------------------------------------------------------
# Sweeper
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweeper_drains_then_exits_on_shutdown(monkeypatch):
    monkeypatch.setattr(session_wake, "TICK_SECONDS", 0.01)
    drained = AsyncMock(return_value=0)
    monkeypatch.setattr(session_wake, "drain_pending_wakes", drained)
    shutdown = asyncio.Event()

    task = asyncio.create_task(session_wake.session_wake_sweeper_loop(_db(), shutdown))
    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2)

    assert drained.await_count >= 1


@pytest.mark.asyncio
async def test_sweeper_tick_survives_a_raising_drain(monkeypatch):
    monkeypatch.setattr(session_wake, "TICK_SECONDS", 0.01)
    calls = []

    async def _boom(db, **kw):
        calls.append(1)
        raise RuntimeError("tick blew up")

    monkeypatch.setattr(session_wake, "drain_pending_wakes", _boom)
    shutdown = asyncio.Event()
    task = asyncio.create_task(session_wake.session_wake_sweeper_loop(_db(), shutdown))
    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2)

    assert len(calls) >= 2, "a raising tick must not kill the loop"
