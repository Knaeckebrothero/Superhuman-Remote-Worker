"""OC-02/BP-09 shared runtime actor authorization contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from orchestrator.services import runtime_actor as service
from src.shared.runtime_actor import (
    RUNTIME_ACTOR_BOOTSTRAP_HEADER,
    RUNTIME_ACTOR_HEADER,
    SENSITIVE_KNOWLEDGE_HUMAN_ROLE_POLICY,
    RuntimeActorContext,
)


PROJECT_A = str(uuid4())
PROJECT_B = str(uuid4())
OFFICER_THREAD = str(uuid4())
SUCCESSOR_THREAD = str(uuid4())
USER_ID = str(uuid4())
ACCESS_TOKEN = "sra_" + ("A" * 43)
AGENT_ID = str(uuid4())


def _request(*values: str) -> MagicMock:
    request = MagicMock()
    request.method = "POST"
    request.headers = Headers(
        raw=[
            (RUNTIME_ACTOR_HEADER.lower().encode(), value.encode()) for value in values
        ]
    )
    request.url.path = "/api/runtime-actors/authorize"
    request.client = None
    return request


def _audit_db() -> MagicMock:
    db = MagicMock()
    db.record_security_event = AsyncMock()
    return db


def _bootstrap_request(*values: str) -> MagicMock:
    request = MagicMock()
    request.headers = Headers(
        raw=[
            (RUNTIME_ACTOR_BOOTSTRAP_HEADER.lower().encode(), value.encode())
            for value in values
        ]
    )
    return request


def _actor(
    *,
    kind: str = "officer",
    project_id: str = PROJECT_A,
    role: str | None = "owner",
    thread_id: str | None = OFFICER_THREAD,
    incarnation: int | None = 0,
) -> RuntimeActorContext:
    return RuntimeActorContext(
        caller_kind=kind,
        project_id=project_id,
        project_role=role,
        thread_id=thread_id,
        officer_incarnation=incarnation,
        user_id=USER_ID,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ((), "missing_credential"),
        (("bad-token",), "malformed_credential"),
        ((ACCESS_TOKEN, ACCESS_TOKEN), "duplicate_credential"),
        ((ACCESS_TOKEN, ""), "duplicate_credential"),
    ],
)
async def test_bad_actor_credentials_fail_closed_and_are_audited(headers, code):
    db = _audit_db()
    with pytest.raises(HTTPException) as exc:
        await service.authorize_runtime_actor_request(
            db,
            _request(*headers),
            action="officer_message",
            project_id=PROJECT_A,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == code
    db.record_security_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_generic_internal_caller_cannot_acknowledge_without_runtime_actor():
    db = _audit_db()
    with pytest.raises(HTTPException) as exc:
        await service.authorize_runtime_actor_request(
            db,
            _request(),
            action="redispatch_livelock_ack",
            project_id=PROJECT_A,
        )
    assert exc.value.detail["code"] == "missing_credential"
    db.record_security_event.assert_awaited_once()


@pytest.mark.parametrize(
    ("values", "code"),
    [
        (("bad-bootstrap",), "malformed_bootstrap"),
        (("srb_" + ("A" * 43), ""), "duplicate_bootstrap"),
    ],
)
def test_malformed_or_duplicate_bootstrap_is_never_an_actor_credential(values, code):
    with pytest.raises(service.RuntimeActorCredentialError) as exc:
        service.request_bootstrap_token(_bootstrap_request(*values))
    assert exc.value.code == code


@pytest.mark.asyncio
async def test_expired_actor_credential_fails_closed_and_audits_actor():
    db = _audit_db()
    actor = _actor()
    error = service.RuntimeActorCredentialError(
        "expired_credential", "expired", actor=actor
    )
    with patch.object(service, "_actor_for_access", AsyncMock(side_effect=error)):
        with pytest.raises(HTTPException) as exc:
            await service.authorize_runtime_actor_request(
                db,
                _request(ACCESS_TOKEN),
                action="officer_message",
                project_id=PROJECT_A,
            )
    assert exc.value.detail["code"] == "expired_credential"
    assert exc.value.detail["actor"]["thread_id"] == OFFICER_THREAD
    db.record_security_event.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "officer_message",
        "redispatch_livelock_ack",
        "machine_tags",
        "charter",
    ],
)
async def test_project_a_credential_cannot_act_on_project_b(action):
    db = _audit_db()
    actor = _actor(project_id=PROJECT_A)
    current = AsyncMock(return_value=actor)
    with (
        patch.object(service, "_actor_for_access", AsyncMock(return_value=actor)),
        patch.object(service, "_current_actor", current),
    ):
        with pytest.raises(HTTPException) as exc:
            await service.authorize_runtime_actor_request(
                db,
                _request(ACCESS_TOKEN),
                action=action,
                project_id=PROJECT_B,
            )
    assert exc.value.detail["code"] == "project_scope_mismatch"
    current.assert_not_awaited()
    db.record_security_event.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "role", "allowed"),
    [
        ("worker", None, False),
        ("human", "viewer", False),
        ("human", "editor", False),
        ("human", "owner", True),
        ("human", "admin", True),
        ("officer", "owner", True),
        ("conference", "viewer", False),
        ("conference", "editor", False),
        ("conference", "owner", True),
        ("conference", "admin", True),
    ],
)
async def test_sensitive_knowledge_policy_matrix(kind, role, allowed):
    db = _audit_db()
    actor = _actor(
        kind=kind,
        role=role,
        thread_id=None if kind == "worker" else OFFICER_THREAD,
        incarnation=0 if kind == "officer" else None,
    )
    with (
        patch.object(service, "_actor_for_access", AsyncMock(return_value=actor)),
        patch.object(service, "_current_actor", AsyncMock(return_value=actor)),
    ):
        if allowed:
            result = await service.authorize_runtime_actor_request(
                db,
                _request(ACCESS_TOKEN),
                action="machine_tags",
                project_id=PROJECT_A,
            )
            assert result is actor
            db.record_security_event.assert_not_awaited()
        else:
            with pytest.raises(HTTPException) as exc:
                await service.authorize_runtime_actor_request(
                    db,
                    _request(ACCESS_TOKEN),
                    action="machine_tags",
                    project_id=PROJECT_A,
                )
            assert exc.value.detail["code"] == "project_role_denied"
            db.record_security_event.assert_awaited_once()


def test_named_human_policy_constant_documents_safe_default():
    assert dict(SENSITIVE_KNOWLEDGE_HUMAN_ROLE_POLICY) == {
        "admin": True,
        "owner": True,
        "editor": False,
        "viewer": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["worker", "human", "conference"])
async def test_only_current_officer_runtime_may_acknowledge_redispatch_trip(kind):
    db = _audit_db()
    actor = _actor(
        kind=kind,
        thread_id=None if kind == "worker" else OFFICER_THREAD,
        incarnation=None,
    )
    with (
        patch.object(service, "_actor_for_access", AsyncMock(return_value=actor)),
        patch.object(service, "_current_actor", AsyncMock(return_value=actor)),
    ):
        with pytest.raises(HTTPException) as exc:
            await service.authorize_runtime_actor_request(
                db,
                _request(ACCESS_TOKEN),
                action="redispatch_livelock_ack",
                project_id=PROJECT_A,
            )
    assert exc.value.detail["code"] == "officer_required"
    db.record_security_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_current_officer_runtime_may_acknowledge_redispatch_trip():
    db = _audit_db()
    actor = _actor()
    with (
        patch.object(service, "_actor_for_access", AsyncMock(return_value=actor)),
        patch.object(service, "_current_actor", AsyncMock(return_value=actor)),
    ):
        result = await service.authorize_runtime_actor_request(
            db,
            _request(ACCESS_TOKEN),
            action="redispatch_livelock_ack",
            project_id=PROJECT_A,
        )
    assert result is actor
    db.record_security_event.assert_not_awaited()


class _CurrentStateDB:
    def __init__(self) -> None:
        self.post = {
            "thread_id": OFFICER_THREAD,
            "incarnations": [],
        }
        self.record_security_event = AsyncMock()

    async def get_thread(self, thread_id):
        return {
            "id": thread_id,
            "status": "active",
            "project_id": PROJECT_A,
            "user_id": USER_ID,
            "metadata": {},
        }

    async def list_thread_mounts(self, _thread_id):
        return [
            {
                "mount_kind": "project",
                "source_ref": PROJECT_A,
            }
        ]

    async def get_user(self, _user_id):
        return {"id": USER_ID, "is_admin": False}

    async def get_user_role_in_project(self, _project_id, _user_id):
        return "owner"

    async def get_project_officer(self, _project_id):
        return self.post


@pytest.mark.asyncio
async def test_recommission_invalidates_old_incarnation_immediately():
    db = _CurrentStateDB()
    old_actor = _actor()

    current = await service._current_actor(db, old_actor)
    assert current.caller_kind == "officer"

    # Decommission/recommission changes the authoritative post immediately;
    # the still-unexpired old credential no longer validates.
    db.post = {
        "thread_id": SUCCESSOR_THREAD,
        "incarnations": [{"thread_id": OFFICER_THREAD}],
    }
    with pytest.raises(service.RuntimeActorCredentialError) as exc:
        await service._current_actor(db, old_actor)
    assert exc.value.code == "runtime_not_current"

    with patch.object(service, "_actor_for_access", AsyncMock(return_value=old_actor)):
        with pytest.raises(HTTPException) as denied:
            await service.authorize_runtime_actor_request(
                db,
                _request(ACCESS_TOKEN),
                action="redispatch_livelock_ack",
                project_id=PROJECT_A,
            )
    assert denied.value.detail["code"] == "runtime_not_current"
    assert denied.value.detail["actor"]["thread_id"] == OFFICER_THREAD
    db.record_security_event.assert_awaited_once()


# ---------------------------------------------------------------------------
# Sliding refresh window — knowledge/issues/
# officer_runtime_grant_expires_after_24h_and_dies_silently.md
# ---------------------------------------------------------------------------


class _FakeConn:
    """Records execute() calls so the test can assert on the UPDATE issued."""

    def __init__(self, row):
        self._row = row
        self.executed: list[tuple] = []

    async def fetchrow(self, *args, **kwargs):
        return self._row

    async def fetchval(self, *args, **kwargs):
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"

    def transaction(self):
        conn = self

        class _Txn:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Txn()


def _refresh_db(conn):
    db = MagicMock()
    db.record_security_event = AsyncMock()

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    db.acquire = MagicMock(return_value=_Acquire())
    return db


def _grant_row(*, caller_kind="officer", thread_id=OFFICER_THREAD):
    from datetime import datetime, timedelta, timezone

    return {
        "id": str(uuid4()),
        "caller_kind": caller_kind,
        "user_id": USER_ID,
        "project_id": PROJECT_A,
        "project_role": "owner",
        "thread_id": thread_id,
        "officer_incarnation": 0,
        "agent_id": AGENT_ID if caller_kind == "officer" else None,
        "credential_generation": 1,
        "refresh_token_hash": service._digest("srr_" + "A" * 43),
        "previous_refresh_token_hash": None,
        "previous_refresh_valid_until": None,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
        # Valid, but close to the wall — the case that used to kill the officer.
        "refresh_expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "revoked_at": None,
    }


def _refresh_request():
    from src.shared.runtime_actor import RUNTIME_ACTOR_REFRESH_HEADER

    request = MagicMock()
    request.method = "POST"
    request.headers = Headers(
        raw=[(RUNTIME_ACTOR_REFRESH_HEADER.lower().encode(), b"srr_" + b"A" * 43)]
    )
    request.url.path = "/api/runtime-actors/refresh"
    request.client = None
    return request


@pytest.mark.asyncio
async def test_refresh_slides_the_window_for_a_live_thread():
    """An officer that keeps working must not hit an absolute wall.

    The grant's lifetime is an IDLE timeout, not a fixed lease: reaching the
    mint proves `_current_actor` already re-derived authority from a thread that
    is not ended, and that liveness is what licenses the extension.
    """
    from datetime import datetime, timezone

    row = _grant_row()
    conn = _FakeConn(row)
    db = _refresh_db(conn)
    before = row["refresh_expires_at"]

    with (
        patch.object(
            service,
            "_lock_officer_authority_for_grant",
            AsyncMock(
                return_value=(
                    {"project_id": PROJECT_A, "state": {}},
                    {"id": OFFICER_THREAD},
                    {"id": AGENT_ID},
                    row,
                    "owner",
                )
            ),
        ),
        patch.object(
            service,
            "_insert_access_token",
            AsyncMock(return_value=("sra_" + "B" * 43, datetime.now(timezone.utc))),
        ),
    ):
        actor = await service.refresh_runtime_actor_request(db, _refresh_request())

    assert actor.refresh_expires_at is not None
    assert actor.refresh_expires_at > before, (
        "a successful refresh on a live thread must push the wall forward"
    )
    sql = " ".join(s for s, _ in conn.executed)
    assert "refresh_expires_at" in sql, "the UPDATE must persist the new expiry"


@pytest.mark.asyncio
async def test_refresh_does_not_slide_a_worker_grant():
    """Workers are job-scoped and have no thread liveness to justify sliding."""
    from datetime import datetime, timezone

    row = _grant_row(caller_kind="worker", thread_id=None)
    conn = _FakeConn(row)
    db = _refresh_db(conn)
    before = row["refresh_expires_at"]

    with (
        patch.object(service, "_current_actor", AsyncMock(side_effect=lambda d, a: a)),
        patch.object(
            service,
            "_insert_access_token",
            AsyncMock(return_value=("sra_" + "B" * 43, datetime.now(timezone.utc))),
        ),
    ):
        actor = await service.refresh_runtime_actor_request(db, _refresh_request())

    assert actor.refresh_expires_at == before, "worker grants keep their fixed wall"
    sql = " ".join(s for s, _ in conn.executed)
    assert "refresh_expires_at" not in sql


# ---------------------------------------------------------------------------
# Heartbeat liveness slide — the same issue seen from the other side. The
# refresh-path slide above only runs when the runtime needs an access token for
# a PRIVILEGED call, which keys the idle timeout on MUTATIONS rather than on
# liveness: a busy officer is safe and a quiet one starves. Officer 6ce5bc4c
# woke every 10 minutes for 24h reading SITREPs, made no privileged call, and
# hit the wall while demonstrably alive.
# ---------------------------------------------------------------------------


class _LivenessConn:
    """In-memory ``runtime_actor_grants`` mirroring the production predicate.

    ``fetch`` applies the same filter the production SELECT issues, so the
    tests exercise real behaviour rather than SQL text. Each test ALSO asserts
    its guard is present in the issued SQL, so deleting a clause from the query
    fails a test instead of silently widening the slide.
    """

    def __init__(self, grants):
        self.grants = grants
        self.fetched: list[tuple] = []
        self.executed: list[tuple] = []

    @property
    def select_sql(self) -> str:
        return " ".join(sql for sql, _ in self.fetched)

    async def fetch(self, sql, *args):
        from datetime import datetime, timedelta, timezone

        self.fetched.append((sql, args))
        thread_id, below_seconds = args
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(seconds=below_seconds)
        return [
            grant
            for grant in self.grants
            if str(grant["thread_id"]) == str(thread_id)
            and grant["revoked_at"] is None
            and grant["caller_kind"] != "worker"
            and now < grant["refresh_expires_at"] < horizon
        ]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        grant_id, next_expires_at = args
        for grant in self.grants:
            if grant["id"] == grant_id and grant["revoked_at"] is None:
                grant["refresh_expires_at"] = next_expires_at
        return "UPDATE 1"


class _LivenessDB(_CurrentStateDB):
    """Durable state for ``_current_actor`` plus the grant table."""

    def __init__(self, grants, *, thread_status: str = "active") -> None:
        super().__init__()
        self.conn = _LivenessConn(grants)
        self.thread_status = thread_status

    async def get_thread(self, thread_id):
        thread = await super().get_thread(thread_id)
        thread["status"] = self.thread_status
        return thread

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


def _liveness_grant(
    *,
    expires_in_seconds: int,
    caller_kind: str = "officer",
    thread_id: str | None = OFFICER_THREAD,
    revoked: bool = False,
):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return {
        "id": uuid4(),
        "caller_kind": caller_kind,
        "user_id": USER_ID,
        "project_id": PROJECT_A,
        "project_role": "owner",
        "thread_id": thread_id,
        "officer_incarnation": 0,
        "refresh_expires_at": now + timedelta(seconds=expires_in_seconds),
        "revoked_at": now if revoked else None,
    }


@pytest.mark.asyncio
async def test_heartbeat_slides_a_grant_inside_the_throttle_window():
    """Liveness alone — no privileged call — must push the wall forward."""
    from datetime import datetime, timedelta, timezone

    grant = _liveness_grant(expires_in_seconds=3600)
    db = _LivenessDB([grant])
    before = grant["refresh_expires_at"]

    slid = await service.slide_thread_grant_on_liveness(db, OFFICER_THREAD)

    assert slid is True
    assert grant["refresh_expires_at"] > before
    assert grant["refresh_expires_at"] > datetime.now(timezone.utc) + timedelta(
        seconds=service.REFRESH_TTL_SECONDS - 60
    )
    assert len(db.conn.executed) == 1
    assert "refresh_expires_at = $2" in db.conn.executed[0][0]
    # Stamp what the refresh path stamps, so both slides look alike in the row.
    assert "last_refreshed_at = now()" in db.conn.executed[0][0]
    # Belt-and-braces against a grant expiring between the SELECT and the
    # UPDATE — even that race must not revive it.
    assert "refresh_expires_at > now()" in db.conn.executed[0][0]


@pytest.mark.asyncio
async def test_heartbeat_does_not_write_when_the_grant_is_far_from_expiry():
    """The throttle: ~one write per grant per half-TTL, not one per beat."""

    grant = _liveness_grant(expires_in_seconds=service.REFRESH_TTL_SECONDS - 60)
    db = _LivenessDB([grant])
    before = grant["refresh_expires_at"]

    slid = await service.slide_thread_grant_on_liveness(db, OFFICER_THREAD)

    assert slid is False
    assert grant["refresh_expires_at"] == before
    assert db.conn.executed == [], "a far-from-expiry grant must cost no write"
    assert "make_interval(secs => $2::int)" in db.conn.select_sql


@pytest.mark.asyncio
async def test_heartbeat_never_slides_a_worker_grant():
    """Workers are job-scoped; the refresh path excludes them identically."""

    grant = _liveness_grant(expires_in_seconds=3600, caller_kind="worker")
    db = _LivenessDB([grant])
    before = grant["refresh_expires_at"]

    slid = await service.slide_thread_grant_on_liveness(db, OFFICER_THREAD)

    assert slid is False
    assert grant["refresh_expires_at"] == before
    assert db.conn.executed == []
    assert "caller_kind <> 'worker'" in db.conn.select_sql


@pytest.mark.asyncio
async def test_heartbeat_never_slides_a_revoked_grant():
    """Revocation is immediate and a heartbeat must not soften it."""

    grant = _liveness_grant(expires_in_seconds=3600, revoked=True)
    db = _LivenessDB([grant])
    before = grant["refresh_expires_at"]

    slid = await service.slide_thread_grant_on_liveness(db, OFFICER_THREAD)

    assert slid is False
    assert grant["refresh_expires_at"] == before
    assert db.conn.executed == []
    assert "revoked_at IS NULL" in db.conn.select_sql


@pytest.mark.asyncio
async def test_heartbeat_never_revives_an_already_expired_grant():
    """Expiry stays TERMINAL: this fix prevents reaching the wall, it does not
    resurrect a credential that already hit it."""

    grant = _liveness_grant(expires_in_seconds=-60)
    db = _LivenessDB([grant])
    before = grant["refresh_expires_at"]

    slid = await service.slide_thread_grant_on_liveness(db, OFFICER_THREAD)

    assert slid is False
    assert grant["refresh_expires_at"] == before
    assert db.conn.executed == []
    assert "refresh_expires_at > now()" in db.conn.select_sql


@pytest.mark.asyncio
async def test_heartbeat_does_not_slide_a_grant_whose_thread_ended():
    """A live POD is not a live THREAD. The slide reuses ``_current_actor``,
    so it inherits ``derive_runtime_actor``'s refusal of an ended thread."""

    grant = _liveness_grant(expires_in_seconds=3600)
    db = _LivenessDB([grant], thread_status="ended")
    before = grant["refresh_expires_at"]

    slid = await service.slide_thread_grant_on_liveness(db, OFFICER_THREAD)

    assert slid is False
    assert grant["refresh_expires_at"] == before
    assert db.conn.executed == []


@pytest.mark.asyncio
async def test_heartbeat_does_not_slide_a_grant_the_refresh_path_would_refuse():
    """Recommission invalidates the old incarnation for BOTH slide paths."""

    grant = _liveness_grant(expires_in_seconds=3600)
    db = _LivenessDB([grant])
    db.post = {
        "thread_id": SUCCESSOR_THREAD,
        "incarnations": [{"thread_id": OFFICER_THREAD}],
    }

    slid = await service.slide_thread_grant_on_liveness(db, OFFICER_THREAD)

    assert slid is False
    assert db.conn.executed == []


@pytest.mark.asyncio
async def test_a_quiet_officer_alive_for_days_never_loses_its_grant():
    """Regression for the real failure.

    Officer 6ce5bc4c spent 24h waking every 10 minutes, reading a SITREP and
    sleeping. That makes no privileged call, so the refresh path never ran, so
    the grant never slid and died at the 24h wall while the thread was alive —
    after which the officer burned wake cycles being refused
    ``expired_credential``.

    Simulated on the throttle rather than in real time: each tick moves every
    expiry one tick closer, which is exactly "time passed", and the only thing
    that happens on a tick is a heartbeat.
    """
    from datetime import datetime, timedelta, timezone

    tick_seconds = 600  # the officer's wake cadence
    days = 3
    ticks = (days * 24 * 3600) // tick_seconds

    grant = _liveness_grant(expires_in_seconds=service.REFRESH_TTL_SECONDS)
    db = _LivenessDB([grant])

    for tick in range(1, ticks + 1):
        for row in db.conn.grants:
            row["refresh_expires_at"] -= timedelta(seconds=tick_seconds)
        await service.slide_thread_grant_on_liveness(db, OFFICER_THREAD)
        elapsed_hours = tick * tick_seconds / 3600
        assert grant["refresh_expires_at"] > datetime.now(timezone.utc), (
            f"grant expired after {elapsed_hours:.1f}h of an alive, quiet "
            "thread — the officer is now being refused expired_credential"
        )

    # Write amplification: the throttle allows roughly two writes per grant per
    # day. A band, not an exact count — which tick crosses the half-TTL
    # boundary depends on sub-second real-clock drift during the loop.
    writes_per_day = len(db.conn.executed) / days
    assert 1 <= writes_per_day <= 3, (
        f"expected ~2 writes/grant/day, got {writes_per_day:.1f}"
    )
