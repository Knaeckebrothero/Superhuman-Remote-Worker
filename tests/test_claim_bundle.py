"""GET /internal/units/{unit_id}/claim-bundle (stateless_agents.md §5.6, M4).

The pinned executor-facing contract: internal-key transport auth PLUS live
lease proof ((unit_id, lease_token) must match state='leased' with the exact
token, one SELECT that also reads the watermarks), then the attach payload
assembled by the SAME `_assemble_session_attach_payload` the pinned-lane
sender uses. Error taxonomy: 401 bad internal key; 403 generic on any lease
mismatch (no enumeration oracle); 404 absent unit row; 409 non-session unit /
wrong lane / assembly refused.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

UNIT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
POD_NAME = "stateless-agent-1"
POD_UID = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"

LEASED_ROW = {
    "unit_kind": "session_turn",
    "state": "leased",
    "lease_token": 7,
    "leased_by": POD_NAME,
    "input_seq": 41,
    "consumed_seq": 12,
    "thread_status": "created",
    "thread_lane": "stateless",
    "thread_metadata": {},
}


class _AsyncCM:
    def __init__(self, value=None):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class FakeDB:
    def __init__(self, *, run_queue_row, thread, job=None):
        self._row = run_queue_row
        self._thread = thread
        self._job = job
        self.conn = MagicMock()
        self.conn.transaction = lambda: _AsyncCM()

        async def _fetchrow(sql, *_args):
            if "FROM run_queue AS queue LEFT JOIN threads" in sql:
                return self._row
            if "SELECT status::text AS status, execution_lane, metadata" in sql:
                return self._thread
            if "SELECT state, lease_token, leased_by FROM run_queue" in sql:
                return self._row
            raise AssertionError(f"unexpected fetchrow SQL: {sql}")

        self.conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        self.conn.fetchval = AsyncMock(return_value=True)
        self.datasource_lock_calls = []

    def acquire(self):
        return _AsyncCM(self.conn)

    async def get_thread(self, tid):
        return self._thread

    async def get_job(self, jid):
        return self._job

    def thread_datasource_lock(self, tid):
        self.datasource_lock_calls.append(tid)
        return _AsyncCM()


def _thread(**over):
    thread = {
        "id": UNIT_ID,
        "user_id": "user-1",
        "project_id": None,
        "execution_lane": "stateless",
        "status": "created",
        "config_name": "session_base",
        "metadata": {
            "config_override": {
                "llm": {"model": "m"},
                "workspace": {"backend": "virtual"},
            }
        },
    }
    thread.update(over)
    return thread


def _patch(monkeypatch, orch_main, db, *, attach="SENTINEL"):
    monkeypatch.setattr(orch_main, "require_internal", AsyncMock())
    monkeypatch.setattr(orch_main, "postgres_db", db)
    monkeypatch.setattr(orch_main, "_thread_project_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        orch_main, "_thread_has_knowledge_scope", AsyncMock(return_value=False)
    )
    inject = AsyncMock(side_effect=lambda co, **kw: co)
    monkeypatch.setattr(orch_main, "_inject_thread_dispatch_credentials", inject)
    assembly = AsyncMock(
        return_value=(
            {
                "thread_id": UNIT_ID,
                "config_override": {"llm": {"model": "m"}},
                "resolved_config": None,
                "project_ids": [],
                "datasources": None,
                "config_name": "session_base",
            }
            if attach == "SENTINEL"
            else attach
        )
    )
    monkeypatch.setattr(orch_main, "_assemble_session_attach_payload", assembly)
    return inject, assembly


@pytest.mark.asyncio
async def test_happy_path_returns_watermarks_and_shared_assembly(monkeypatch):
    from orchestrator import main as orch_main

    db = FakeDB(run_queue_row=dict(LEASED_ROW), thread=_thread())
    inject, assembly = _patch(monkeypatch, orch_main, db)

    out = await orch_main.internal_unit_claim_bundle(
        UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
    )

    # One SELECT validated the lease AND carried the watermarks.
    assert db.conn.fetchrow.await_count == 3
    assert "run_queue" in db.conn.fetchrow.await_args_list[0].args[0]
    assert out["unit_id"] == UNIT_ID
    assert out["thread_id"] == UNIT_ID
    assert out["unit_kind"] == "session_turn"
    assert out["execution_lane"] == "stateless"
    assert out["watermarks"] == {"input_seq": 41, "consumed_seq": 12}
    # attach came from the ONE shared assembly, called under the datasource
    # lock with the credential-injected override + canonical config name.
    assembly.assert_awaited_once()
    assert assembly.await_args.args == (UNIT_ID,)
    assert assembly.await_args.kwargs["config_name"]  # canonicalized, non-empty
    inject.assert_awaited_once()
    assert db.datasource_lock_calls == [UNIT_ID]
    assert out["attach"]["thread_id"] == UNIT_ID
    assert set(out["attach"]) == {
        "thread_id",
        "config_override",
        "resolved_config",
        "project_ids",
        "datasources",
        "config_name",
    }


@pytest.mark.asyncio
async def test_session_bundle_stolen_during_slow_assembly_is_rejected(monkeypatch):
    """No attach credentials cross the response boundary after a token steal."""

    from orchestrator import main as orch_main

    db = FakeDB(run_queue_row=dict(LEASED_ROW), thread=_thread())
    _, assembly = _patch(monkeypatch, orch_main, db)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_assembly(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"thread_id": UNIT_ID, "datasources": {"secret": "in-flight"}}

    assembly.side_effect = _slow_assembly
    request_task = asyncio.create_task(
        orch_main.internal_unit_claim_bundle(UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID)
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    # The reaper/successor owns a newer token by the time assembly returns.
    db.conn.fetchval.return_value = False
    release.set()

    with pytest.raises(HTTPException) as exc:
        await request_task

    assert exc.value.status_code == 403
    assert exc.value.detail == "Lease validation failed"
    assert db.conn.fetchval.await_count == 1


@pytest.mark.asyncio
async def test_protected_cloud_flip_during_assembly_blocks_final_credentials(
    monkeypatch,
):
    from orchestrator import main as orch_main

    db = FakeDB(run_queue_row=dict(LEASED_ROW), thread=_thread())
    _, assembly = _patch(monkeypatch, orch_main, db)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_assembly(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"thread_id": UNIT_ID, "datasources": {"secret": "in-flight"}}

    assembly.side_effect = _slow_assembly
    request_task = asyncio.create_task(
        orch_main.internal_unit_claim_bundle(UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID)
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    db._thread["metadata"] = {
        **db._thread["metadata"],
        "protected_cloud": True,
    }
    release.set()

    with pytest.raises(HTTPException) as exc:
        await request_task
    assert exc.value.status_code == 403
    assert exc.value.detail == "Lease validation failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["enabled", "conference"])
@pytest.mark.parametrize("value", [None, 0, "", [], {}, "yes", 1, True])
async def test_malformed_or_pinned_session_class_refuses_claim_credentials(
    monkeypatch,
    field,
    value,
):
    from orchestrator import main as orch_main

    thread = _thread()
    thread["metadata"]["config_override"]["officer"] = {field: value}
    db = FakeDB(run_queue_row=dict(LEASED_ROW), thread=thread)
    _patch(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException) as exc:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )
    assert exc.value.status_code in {403, 409}
    db.conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_class_flip_during_assembly_blocks_final_credentials(
    monkeypatch,
):
    from orchestrator import main as orch_main

    db = FakeDB(run_queue_row=dict(LEASED_ROW), thread=_thread())
    _, assembly = _patch(monkeypatch, orch_main, db)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_assembly(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"thread_id": UNIT_ID, "datasources": {"secret": "in-flight"}}

    assembly.side_effect = _slow_assembly
    request_task = asyncio.create_task(
        orch_main.internal_unit_claim_bundle(UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID)
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    db._thread["metadata"]["config_override"]["officer"] = {"enabled": "yes"}
    release.set()

    with pytest.raises(HTTPException) as exc:
        await request_task
    assert exc.value.status_code == 403
    assert exc.value.detail == "Lease validation failed"


@pytest.mark.asyncio
async def test_token_mismatch_and_not_leased_are_one_generic_403(monkeypatch):
    from orchestrator import main as orch_main

    # Wrong token on a live lease.
    db = FakeDB(run_queue_row=dict(LEASED_ROW), thread=_thread())
    _patch(monkeypatch, orch_main, db)
    with pytest.raises(HTTPException) as exc_token:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 6, POD_NAME, POD_UID
        )
    assert exc_token.value.status_code == 403

    # Right token but the row is no longer leased (stolen/completed).
    row = dict(LEASED_ROW, state="queued")
    db2 = FakeDB(run_queue_row=row, thread=_thread())
    _patch(monkeypatch, orch_main, db2)
    with pytest.raises(HTTPException) as exc_state:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )
    assert exc_state.value.status_code == 403

    # Single generic detail: the two cases must be indistinguishable.
    assert exc_token.value.detail == exc_state.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker_key",
    [
        "_stateless_workspace_retirement_pending",
        "_stateless_claim_retirement",
        "_stateless_claim_loss_hold",
        "_stateless_claim_losses",
    ],
)
@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
async def test_present_falsey_stop_marker_refuses_credentials(
    monkeypatch, marker_key, value
):
    from orchestrator import main as orch_main

    row = dict(
        LEASED_ROW,
        thread_metadata={marker_key: value},
    )
    db = FakeDB(run_queue_row=row, thread=_thread())
    _patch(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException) as exc:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_absent_unit_row_404(monkeypatch):
    from orchestrator import main as orch_main

    db = FakeDB(run_queue_row=None, thread=_thread())
    _patch(monkeypatch, orch_main, db)
    with pytest.raises(HTTPException) as exc:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )
    assert exc.value.status_code == 404

    # Malformed unit id short-circuits to the same 404 (no DataError 500).
    with pytest.raises(HTTPException) as exc2:
        await orch_main.internal_unit_claim_bundle(
            "not-a-uuid", MagicMock(), 7, POD_NAME, POD_UID
        )
    assert exc2.value.status_code == 404


@pytest.mark.asyncio
async def test_wrong_lane_and_non_session_kind_409(monkeypatch):
    from orchestrator import main as orch_main

    # Pinned-lane thread behind a leased session unit.
    db = FakeDB(run_queue_row=dict(LEASED_ROW), thread=_thread(execution_lane="pinned"))
    _patch(monkeypatch, orch_main, db)
    with pytest.raises(HTTPException) as exc:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )
    assert exc.value.status_code == 409

    # Vanished thread → same 409 family.
    db2 = FakeDB(run_queue_row=dict(LEASED_ROW), thread=None)
    _patch(monkeypatch, orch_main, db2)
    with pytest.raises(HTTPException) as exc2:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )
    assert exc2.value.status_code == 409

    # Non-session unit kinds carry no attach bundle.
    row = dict(LEASED_ROW, unit_kind="bg_task")
    db3 = FakeDB(run_queue_row=row, thread=_thread())
    _patch(monkeypatch, orch_main, db3)
    with pytest.raises(HTTPException) as exc3:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )
    assert exc3.value.status_code == 409


@pytest.mark.asyncio
async def test_worker_bundle_reuses_job_start_builder_and_rechecks_lease(monkeypatch):
    from orchestrator import main as orch_main

    row = dict(LEASED_ROW, unit_kind="worker_batch")
    job = {
        "id": UNIT_ID,
        "execution_lane": "stateless",
        "context": {
            "workspace_container": {
                "status": "ready",
                "provisioner": "k8s",
                "pod_ip": "10.0.0.8",
            },
            "worker_batch_target_wall_seconds": 360,
            "worker_batch_iteration_cap": 9,
        },
    }
    db = FakeDB(run_queue_row=row, thread=None, job=job)
    monkeypatch.setattr(orch_main, "require_internal", AsyncMock())
    monkeypatch.setattr(orch_main, "postgres_db", db)
    built = orch_main.JobStartRequest(job_id=UNIT_ID, description="work")
    builder = AsyncMock(return_value=built)
    monkeypatch.setattr(orch_main, "_build_job_start_request", builder)
    inherit = AsyncMock(return_value=("proceed", None))
    monkeypatch.setattr(orch_main, "_resolve_subjob_inherited_workspace", inherit)

    out = await orch_main.internal_unit_claim_bundle(
        UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
    )

    inherit.assert_awaited_once_with(job)
    builder.assert_awaited_once_with(job, persist_dispatch_state=False)
    db.conn.fetchval.assert_awaited_once()
    assert "state = 'leased'" in db.conn.fetchval.await_args.args[0]
    assert out == {
        "unit_id": UNIT_ID,
        "job_id": UNIT_ID,
        "unit_kind": "worker_batch",
        "execution_lane": "stateless",
        "job": {
            "job_id": UNIT_ID,
            "description": "work",
            "config_name": "worker_base",
        },
        "batch": {
            "target_wall_seconds": 360.0,
            "iteration_cap": 9,
            "min_wall_seconds": 300.0,
        },
    }


@pytest.mark.asyncio
async def test_worker_bundle_stolen_during_assembly_is_rejected(monkeypatch):
    from orchestrator import main as orch_main

    row = dict(LEASED_ROW, unit_kind="worker_batch")
    job = {
        "id": UNIT_ID,
        "execution_lane": "stateless",
        "context": {
            "workspace_container": {
                "status": "ready",
                "provisioner": "k8s",
                "host": "workspace.example",
            }
        },
    }
    db = FakeDB(run_queue_row=row, thread=None, job=job)
    db.conn.fetchval.return_value = False
    monkeypatch.setattr(orch_main, "require_internal", AsyncMock())
    monkeypatch.setattr(orch_main, "postgres_db", db)
    monkeypatch.setattr(
        orch_main,
        "_build_job_start_request",
        AsyncMock(
            return_value=orch_main.JobStartRequest(
                job_id=UNIT_ID, description="secret-bearing"
            )
        ),
    )
    monkeypatch.setattr(
        orch_main,
        "_resolve_subjob_inherited_workspace",
        AsyncMock(return_value=("proceed", None)),
    )

    with pytest.raises(HTTPException) as exc:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Lease validation failed"


@pytest.mark.asyncio
async def test_stateless_bundle_refusal_never_mutates_job_status(monkeypatch):
    from orchestrator import main as orch_main

    db = MagicMock()
    db.update_job_status = AsyncMock()
    monkeypatch.setattr(orch_main, "postgres_db", db)
    monkeypatch.setattr(
        orch_main,
        "_resolve_authorized_job_datasources",
        AsyncMock(side_effect=HTTPException(status_code=409, detail="revoked")),
    )

    built = await orch_main._build_job_start_request(
        {
            "id": UNIT_ID,
            "description": "work",
            "context": {},
            "config_override": {"workspace": {"backend": "sandbox"}},
        },
        persist_dispatch_state=False,
    )

    assert built is None
    db.update_job_status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sandbox", "vm", "future-tier", None])
async def test_non_lite_workspace_refused_before_attach_assembly(monkeypatch, backend):
    from orchestrator import main as orch_main

    metadata = (
        {"config_override": {"workspace": {"backend": backend}}}
        if backend is not None
        else {}
    )
    db = FakeDB(
        run_queue_row=dict(LEASED_ROW),
        thread=_thread(metadata=metadata),
    )
    inject, assembly = _patch(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException) as exc:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )

    assert exc.value.status_code == 409
    assert "virtual/none" in str(exc.value.detail)
    inject.assert_not_awaited()
    assembly.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "physical_evidence",
    [
        {"workspace_container": {"status": "ready", "pod_ip": "10.0.0.8"}},
        {"vm": {"status": "provisioning", "provision_generation": "g1"}},
    ],
    ids=["sandbox-upgrade", "vm-upgrade"],
)
async def test_upgraded_lite_claim_refused_before_credentials_or_assembly(
    monkeypatch, physical_evidence
):
    from orchestrator import main as orch_main

    metadata = {
        "config_override": {"workspace": {"backend": "virtual"}},
        **physical_evidence,
    }
    db = FakeDB(
        run_queue_row=dict(LEASED_ROW),
        thread=_thread(metadata=metadata),
    )
    inject, assembly = _patch(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException) as exc:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )

    assert exc.value.status_code == 409
    assert "virtual/none" in str(exc.value.detail)
    inject.assert_not_awaited()
    assembly.assert_not_awaited()
    assert db.datasource_lock_calls == []


@pytest.mark.asyncio
async def test_assembly_refusal_is_generic_409(monkeypatch):
    from orchestrator import main as orch_main

    db = FakeDB(run_queue_row=dict(LEASED_ROW), thread=_thread())
    _patch(monkeypatch, orch_main, db, attach=None)
    with pytest.raises(HTTPException) as exc:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, MagicMock(), 7, POD_NAME, POD_UID
        )
    assert exc.value.status_code == 409
    # Generic reason — must not leak which fail-closed rule refused.
    assert "grant" not in str(exc.value.detail).lower()
    assert "datasource" not in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_internal_auth_failure_is_401_before_any_lookup(monkeypatch):
    """With the REAL require_internal and no/wrong X-Internal-Key the endpoint
    401s before touching the queue or the thread."""
    from orchestrator import main as orch_main

    db = FakeDB(run_queue_row=dict(LEASED_ROW), thread=_thread())
    monkeypatch.setattr(orch_main, "postgres_db", db)

    request = MagicMock()
    request.headers = {"X-Internal-Key": ""}
    with pytest.raises(HTTPException) as exc:
        await orch_main.internal_unit_claim_bundle(
            UNIT_ID, request, 7, POD_NAME, POD_UID
        )
    assert exc.value.status_code == 401
    db.conn.fetchrow.assert_not_awaited()
