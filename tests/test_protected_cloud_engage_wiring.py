"""Unit tests for protected-cloud engage wiring in ``orchestrator/main.py``.

Task B8 ties Slice A's fail-closed ``engage_ro_mount`` gate to thread create:
a protected thread with a Nextcloud project mount gets engaged once, and a
refusal is recorded on the thread's metadata WITHOUT raising — the session
must still boot (with no cloud mount), never fall back to a live mount.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main

# NOTE: import via the bare ``services.cloud...`` path, matching main.py's own
# import (``from services.cloud.ro_engage import ...`` — see main.py's cloud
# imports and the module-identity note in the docstring below). ``orchestrator``
# is on sys.path as its own root (conftest.py) AND importable as a package
# (``orchestrator.services.cloud.ro_engage``), so the two import spellings
# resolve to two DIFFERENT module objects with two different (non-`is`-equal)
# ``RoEngageRefused`` classes. Importing the "orchestrator."-prefixed spelling
# here made ``except RoEngageRefused`` in main.py never match the instance this
# test's side_effect raises, silently mis-testing the refusal path (it fell
# through to the generic ``except Exception`` branch instead).
from services.cloud.ro_engage import RoEngageRefused


@asynccontextmanager
async def _owned_workspace_lifecycle_lock(*_args, **_kwargs):
    yield True


@pytest.mark.asyncio
async def test_engage_called_for_protected_thread_with_project_mount():
    mount_rows = [{"backend_id": "nextcloud", "cloud_handle": "handle::Proj"}]

    # Mock postgres_db.acquire for the success-path metadata clear
    mock_conn = AsyncMock()
    mock_db_context = AsyncMock()
    mock_db_context.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_db_context.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(main, "_is_protected_cloud_mode_enabled", return_value=True),
        patch.object(main, "engage_ro_mount", new=AsyncMock()) as engage,
        patch.object(main.main_cloud_router, "for_backend") as for_backend,
        patch.object(main.postgres_db, "acquire", return_value=mock_db_context),
    ):
        for_backend.return_value = object()
        await main._engage_protected_cloud_for_thread(
            "thread-1", user_id="user-1", mount_rows=mount_rows, metadata={}
        )
    engage.assert_awaited_once()


@pytest.mark.asyncio
async def test_engage_success_clears_stale_protected_cloud_error():
    """After a successful engage, any stale protected_cloud_error from a
    prior refused/flag-off attempt must be cleared so the attach-time poll
    fallback isn't suppressed on other HA replicas."""
    mount_rows = [{"backend_id": "nextcloud", "cloud_handle": "handle::Proj"}]

    # Mock the postgres_db.acquire() context manager and connection
    mock_conn = AsyncMock()
    mock_db_context = AsyncMock()
    mock_db_context.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_db_context.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(main, "_is_protected_cloud_mode_enabled", return_value=True),
        patch.object(main, "engage_ro_mount", new=AsyncMock()),
        patch.object(main.main_cloud_router, "for_backend") as for_backend,
        patch.object(main.postgres_db, "acquire", return_value=mock_db_context),
    ):
        for_backend.return_value = object()
        await main._engage_protected_cloud_for_thread(
            "thread-1", user_id="user-1", mount_rows=mount_rows, metadata={}
        )

    # Verify the metadata-key-delete statement was executed
    mock_conn.execute.assert_awaited_once()
    args = mock_conn.execute.await_args[0]
    assert "- 'protected_cloud_error'" in args[0]
    assert args[1] == "thread-1"


@pytest.mark.asyncio
async def test_engage_refusal_records_error_and_does_not_raise():
    mount_rows = [{"backend_id": "nextcloud", "cloud_handle": "handle::Proj"}]
    recorded: list[str] = []
    with (
        patch.object(main, "_is_protected_cloud_mode_enabled", return_value=True),
        patch.object(
            main, "engage_ro_mount", new=AsyncMock(side_effect=RoEngageRefused("floor"))
        ),
        patch.object(main.main_cloud_router, "for_backend", return_value=object()),
        patch.object(
            main,
            "_record_protected_error",
            new=AsyncMock(side_effect=lambda tid, msg: recorded.append(msg)),
        ),
    ):
        # must NOT raise — a refusal is recorded, the session boots with no mount
        await main._engage_protected_cloud_for_thread(
            "thread-1", user_id="user-1", mount_rows=mount_rows, metadata={}
        )
    assert recorded and "refused" in recorded[0]


# ---------------------------------------------------------------------------
# Task B10 — F-I1: engage-vs-attach race (module-level task registry)
# ---------------------------------------------------------------------------

_ACTIVE_NC_ROW = {
    "backend": "nextcloud",
    "reader_id": "srw-reader-abc",
    "credentials": "app-pass-xyz",
    "webdav_url": "https://nc.internal/remote.php/dav/files/srw-reader-abc/Proj/",
    "auth_kind": "basic",
    "status": "active",
}


@pytest.mark.asyncio
async def test_schedule_protected_engage_registers_and_clears_task():
    """_schedule_protected_engage must register the task in
    _protected_engage_tasks immediately (before it runs) and clear the slot
    once it completes — the GC hazard + registry lookup this whole wave
    depends on."""
    with patch.object(
        main, "_engage_protected_cloud_for_thread", new=AsyncMock()
    ) as engage:
        task = main._schedule_protected_engage(
            "thread-reg", user_id="user-1", mount_rows=[]
        )
        assert main._protected_engage_tasks.get("thread-reg") is task
        await task
    assert "thread-reg" not in main._protected_engage_tasks
    engage.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_agent_cloud_mount_awaits_inflight_engage_task_then_returns_payload(
    monkeypatch,
):
    """Registry await path: a task is already registered for this thread_id
    (create-time engage still in flight) when the attach path asks for the
    mount. The first lookup finds nothing; awaiting the registered task lets
    it finish, and the RE-lookup afterward finds the row it just wrote."""
    from main import _build_agent_cloud_mount

    monkeypatch.setenv("CLOUD_WORKSPACE_DRIVER", "rclone_mount")
    monkeypatch.delenv("CLOUD_RCLONE_ALLOW_CONTAINER", raising=False)

    async def _noop() -> None:
        return None

    task = asyncio.create_task(_noop())
    main._protected_engage_tasks["thread-1"] = task

    calls = {"n": 0}

    async def _get_row(_tid):
        calls["n"] += 1
        # Pre-await: engage hasn't written the row yet. Post-await: it has.
        return None if calls["n"] == 1 else _ACTIVE_NC_ROW

    try:
        with (
            patch("main._is_protected_cloud_mode_enabled", return_value=True),
            patch(
                "main.postgres_db.get_ro_mount_by_thread",
                new=AsyncMock(side_effect=_get_row),
            ),
        ):
            payload = await _build_agent_cloud_mount(
                {"id": "thread-1"},
                mount_rows=[],
                metadata={
                    "protected_cloud": True,
                    "workspace_container": {"status": "ready", "pod_ip": "10.42.0.10"},
                },
            )
    finally:
        main._protected_engage_tasks.pop("thread-1", None)
        if not task.done():
            task.cancel()

    assert payload is not None
    assert payload["protected"] is True
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_build_agent_cloud_mount_inflight_task_timeout_still_fails_closed(
    monkeypatch,
):
    """A registered task that never finishes in time must not hang the
    attach path forever, and must never surface a mount after the bounded
    wait — fail-closed even on a timeout, not just an outright refusal."""
    from main import _build_agent_cloud_mount

    monkeypatch.setenv("CLOUD_WORKSPACE_DRIVER", "rclone_mount")
    monkeypatch.delenv("CLOUD_RCLONE_ALLOW_CONTAINER", raising=False)

    async def _hang() -> None:
        await asyncio.Event().wait()  # never completes on its own

    task = asyncio.create_task(_hang())
    main._protected_engage_tasks["thread-1"] = task

    try:
        with (
            patch("main._is_protected_cloud_mode_enabled", return_value=True),
            patch(
                "main.postgres_db.get_ro_mount_by_thread",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "main.asyncio.wait_for",
                new=AsyncMock(side_effect=asyncio.TimeoutError()),
            ),
        ):
            payload = await _build_agent_cloud_mount(
                {"id": "thread-1"},
                mount_rows=[],
                metadata={
                    "protected_cloud": True,
                    "workspace_container": {"status": "ready", "pod_ip": "10.42.0.10"},
                },
            )
    finally:
        main._protected_engage_tasks.pop("thread-1", None)
        task.cancel()

    assert payload is None


@pytest.mark.asyncio
async def test_build_agent_cloud_mount_skips_poll_when_error_already_recorded(
    monkeypatch,
):
    """No task registered (e.g. HA — a different replica ran create) AND a
    terminal ``protected_cloud_error`` is already on the thread: the engage
    already ran to completion with nothing to wait for, so the poll loop
    must be skipped entirely rather than burning ~9s to rediscover 'no
    row'."""
    from main import _build_agent_cloud_mount

    monkeypatch.setenv("CLOUD_WORKSPACE_DRIVER", "rclone_mount")
    monkeypatch.delenv("CLOUD_RCLONE_ALLOW_CONTAINER", raising=False)
    main._protected_engage_tasks.pop("thread-1", None)

    with (
        patch("main._is_protected_cloud_mode_enabled", return_value=True),
        patch(
            "main.postgres_db.get_ro_mount_by_thread",
            new=AsyncMock(return_value=None),
        ) as get_row,
        patch("main.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        payload = await _build_agent_cloud_mount(
            {"id": "thread-1"},
            mount_rows=[],
            metadata={
                "protected_cloud": True,
                "protected_cloud_error": "protected mode refused: floor",
                "workspace_container": {"status": "ready", "pod_ip": "10.42.0.10"},
            },
        )

    assert payload is None
    sleep.assert_not_awaited()
    assert get_row.await_count == 1


@pytest.mark.asyncio
async def test_build_agent_cloud_mount_poll_finds_row_before_cap(monkeypatch):
    """Poll path (no task, no recorded error): the row can land mid-poll —
    the loop must stop as soon as it does, not always exhaust all 3
    attempts."""
    from main import _build_agent_cloud_mount

    monkeypatch.setenv("CLOUD_WORKSPACE_DRIVER", "rclone_mount")
    monkeypatch.delenv("CLOUD_RCLONE_ALLOW_CONTAINER", raising=False)
    main._protected_engage_tasks.pop("thread-1", None)

    calls = {"n": 0}

    async def _get_row(_tid):
        calls["n"] += 1
        return _ACTIVE_NC_ROW if calls["n"] >= 3 else None

    with (
        patch("main._is_protected_cloud_mode_enabled", return_value=True),
        patch(
            "main.postgres_db.get_ro_mount_by_thread",
            new=AsyncMock(side_effect=_get_row),
        ),
        patch("main.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        payload = await _build_agent_cloud_mount(
            {"id": "thread-1"},
            mount_rows=[],
            metadata={
                "protected_cloud": True,
                "workspace_container": {"status": "ready", "pod_ip": "10.42.0.10"},
            },
        )

    assert payload is not None
    assert payload["protected"] is True
    # Initial lookup (miss) + 2 poll attempts to land the row on the 3rd
    # total call; the 3rd poll sleep never fires because the loop breaks.
    assert calls["n"] == 3
    assert sleep.await_count == 2


# ---------------------------------------------------------------------------
# Task B10 — F-I2: resume re-engage
# ---------------------------------------------------------------------------


def _resume_thread_row(**overrides) -> dict:
    thread = {
        "id": "thread-1",
        "execution_lane": "pinned",
        "user_id": "user-1",
        "status": "ended",
        "metadata": {"protected_cloud": True},
    }
    thread.update(overrides)
    return thread


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_lane", ["stateless", "future-lane"])
async def test_resume_refuses_unavailable_non_pinned_lane(execution_lane):
    """Malformed stateless and unknown lanes fail before lifecycle mutation."""
    from main import resume_thread

    thread = _resume_thread_row(execution_lane=execution_lane)
    db = AsyncMock()
    db.resume_thread = AsyncMock()

    with (
        patch(
            "main.require_thread_owner",
            AsyncMock(return_value=({"id": "user-1"}, thread)),
        ),
        patch("main.postgres_db", db),
        pytest.raises(main.HTTPException) as exc,
    ):
        await resume_thread("thread-1", object())

    assert exc.value.status_code == 409
    db.resume_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_sandbox_resume_skips_registered_agent_and_ensures_workspace():
    """A valid queue-lane resume is topology-neutral and restores only state."""
    from main import resume_thread

    generation = "11111111-1111-4111-8111-111111111111"
    thread = _resume_thread_row(
        execution_lane="stateless",
        main_cloud_session_handle="existing-session-folder",
        main_cloud_share_handle="existing-share",
        metadata={
            "config_override": {"workspace": {"backend": "sandbox"}},
            "workspace_container": {
                "status": "deleted",
                "provisioner": "k8s",
                "pod_ip": None,
                "_canvas_workspace_generation": generation,
                "_runtime_incarnation": None,
                "_snapshot_restore_required": False,
            },
            "_workspace_binding": {
                "generation": generation,
                "kind": "remote",
                "backing_id": "k8s-pvc:agent-workspaces:pvc-uid",
                "ssh_host_key_fingerprint": "SHA256:trusted",
            },
            "_stateless_workspace_retirement_settled": {
                "terminal_token": 8,
                "cleanup_complete": True,
                "permanent": False,
                "backing_id": "k8s-pvc:agent-workspaces:pvc-uid",
                "runtime_incarnation": "22222222-2222-4222-8222-222222222222",
                "snapshot_restore_required": False,
            },
        },
    )
    user = {"id": "user-1"}
    db = MagicMock()
    db.get_user = AsyncMock(return_value=user)
    db.resume_thread = AsyncMock(return_value=True)
    db.get_thread = AsyncMock(return_value=thread)
    db.stateless_session_workspace_ensure_lock = MagicMock(
        side_effect=_owned_workspace_lifecycle_lock
    )
    db.begin_stateless_thread_workspace_retirement = AsyncMock(
        return_value={
            "state": "settled",
            "terminal_token": 8,
            "permanent": False,
            "backing_id": "k8s-pvc:agent-workspaces:pvc-uid",
            "runtime_incarnation": "22222222-2222-4222-8222-222222222222",
            "snapshot_restore_required": False,
            "workspace_absence_proven": False,
            "retry": True,
        }
    )
    db.list_thread_mounts = AsyncMock(return_value=[])
    agent_provisioner = MagicMock(is_available=True)
    agent_provisioner.provision_agent = AsyncMock()
    persistent_provisioner = MagicMock(is_available=True)
    persistent_provisioner.create_agent_pod = AsyncMock()
    find_idle = AsyncMock()
    ensure_workspace = AsyncMock()

    with (
        patch("main.require_thread_owner", AsyncMock(return_value=(user, thread))),
        patch("main.postgres_db", db),
        patch("main._thread_project_ids", AsyncMock(return_value=[])),
        patch("main._revalidate_thread_project_ids", AsyncMock(return_value=[])),
        patch("main._thread_config_drift", AsyncMock(return_value=[])),
        patch("main.agent_provisioner", agent_provisioner),
        patch("main.persistent_provisioner", persistent_provisioner),
        patch("main._find_idle_persistent_agent", find_idle),
        patch("main.ensure_session_workspace", ensure_workspace),
    ):
        result = await resume_thread("thread-1", object())
        await asyncio.sleep(0)

    assert result == {"status": "created", "thread_id": "thread-1"}
    db.resume_thread.assert_awaited_once_with("thread-1")
    find_idle.assert_not_awaited()
    agent_provisioner.provision_agent.assert_not_awaited()
    persistent_provisioner.create_agent_pod.assert_not_awaited()
    ensure_workspace.assert_awaited_once()


@pytest.mark.asyncio
async def test_stateless_resume_refuses_retirement_marker_before_mutation():
    from main import resume_thread

    thread = _resume_thread_row(
        execution_lane="stateless",
        metadata={
            "_stateless_workspace_retirement_pending": True,
            "config_override": {"workspace": {"backend": "sandbox"}},
            "workspace_container": {
                "status": "ready",
                "provisioner": "k8s",
                "pod_ip": "10.42.0.25",
                "port": 30022,
                "pod_name": "ws-thread-111111111111",
                "namespace": "agent-workspaces",
                "_canvas_workspace_generation": (
                    "11111111-1111-4111-8111-111111111111"
                ),
                "_runtime_incarnation": "22222222-2222-4222-8222-222222222222",
            },
            "_workspace_binding": {
                "generation": "11111111-1111-4111-8111-111111111111",
                "kind": "remote",
                "backing_id": "k8s-pvc:agent-workspaces:pvc-uid",
                "ssh_host_key_fingerprint": "SHA256:trusted",
            },
        },
    )
    db = MagicMock()
    db.get_user = AsyncMock(return_value={"id": "user-1"})
    db.resume_thread = AsyncMock(return_value=True)
    db.get_thread = AsyncMock(return_value=thread)
    db.stateless_session_workspace_ensure_lock = MagicMock(
        side_effect=_owned_workspace_lifecycle_lock
    )

    with (
        patch("main.require_thread_owner", AsyncMock(return_value=({}, thread))),
        patch("main.postgres_db", db),
        patch("main._thread_project_ids", AsyncMock(return_value=[])),
        patch("main._revalidate_thread_project_ids", AsyncMock(return_value=[])),
        patch("main._thread_config_drift", AsyncMock(return_value=[])),
        pytest.raises(main.HTTPException) as exc,
    ):
        await resume_thread("thread-1", object())

    assert exc.value.status_code == 503
    db.resume_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_cas_loss_to_retirement_fails_closed():
    from main import resume_thread

    thread = _resume_thread_row(metadata={})
    db = MagicMock()
    db.get_user = AsyncMock(return_value={"id": "user-1"})
    db.resume_thread = AsyncMock(return_value=False)
    db.list_thread_mounts = AsyncMock(return_value=[])

    with (
        patch("main.require_thread_owner", AsyncMock(return_value=({}, thread))),
        patch("main.postgres_db", db),
        patch("main._thread_project_ids", AsyncMock(return_value=[])),
        patch("main._revalidate_thread_project_ids", AsyncMock(return_value=[])),
        patch("main._thread_config_drift", AsyncMock(return_value=[])),
        pytest.raises(main.HTTPException) as exc,
    ):
        await resume_thread("thread-1", object())

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_resume_refetch_refuses_lane_changed_while_task_was_scheduled():
    """A pinned entry snapshot is not authority for the background bind."""
    from main import resume_thread

    thread = _resume_thread_row(
        metadata={},
        main_cloud_session_handle="existing-session-folder",
        main_cloud_share_handle="existing-share",
    )
    user = {"id": "user-1"}
    db = MagicMock()
    db.get_user = AsyncMock(return_value=user)
    db.resume_thread = AsyncMock()
    db.list_thread_mounts = AsyncMock(return_value=[])
    db.get_thread = AsyncMock(
        return_value={
            **thread,
            "execution_lane": "stateless",
            "status": "created",
            "agent_id": None,
        }
    )
    lock_cm = AsyncMock()
    lock_cm.__aenter__.return_value = None
    lock_cm.__aexit__.return_value = False
    db.thread_advisory_lock = MagicMock(return_value=lock_cm)
    provisioner = MagicMock(is_available=True)
    provisioner.provision_agent = AsyncMock()
    find_idle = AsyncMock()
    attach = AsyncMock()
    ensure_workspace = AsyncMock()

    with (
        patch("main.require_thread_owner", AsyncMock(return_value=(user, thread))),
        patch("main.postgres_db", db),
        patch("main._thread_project_ids", AsyncMock(return_value=[])),
        patch("main._revalidate_thread_project_ids", AsyncMock(return_value=[])),
        patch("main._thread_config_drift", AsyncMock(return_value=[])),
        patch("main._is_protected_cloud_mode_enabled", return_value=False),
        patch("main.agent_provisioner", provisioner),
        patch("main.persistent_provisioner", MagicMock(is_available=False)),
        patch("main._find_idle_persistent_agent", find_idle),
        patch("main._send_session_attach", attach),
        patch("main._await_late_cloud_setup", AsyncMock()),
        patch("main.ensure_session_workspace", ensure_workspace),
    ):
        result = await resume_thread("thread-1", object())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert result == {"status": "created", "thread_id": "thread-1"}
    find_idle.assert_not_awaited()
    attach.assert_not_awaited()
    provisioner.provision_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_refetches_lane_after_failed_pool_reservation():
    """A warm-attach refusal cannot fall through after a concurrent flip."""
    from main import resume_thread

    thread = _resume_thread_row(
        metadata={},
        main_cloud_session_handle="existing-session-folder",
        main_cloud_share_handle="existing-share",
    )
    user = {"id": "user-1"}
    pinned = {**thread, "status": "created", "agent_id": None}
    stateless = {**pinned, "execution_lane": "stateless"}
    db = MagicMock()
    db.get_user = AsyncMock(return_value=user)
    db.resume_thread = AsyncMock()
    db.list_thread_mounts = AsyncMock(return_value=[])
    db.get_thread = AsyncMock(side_effect=[pinned, stateless])
    lock_cm = AsyncMock()
    lock_cm.__aenter__.return_value = None
    lock_cm.__aexit__.return_value = False
    db.thread_advisory_lock = MagicMock(return_value=lock_cm)
    provisioner = MagicMock(is_available=True)
    provisioner.provision_agent = AsyncMock()
    idle = {
        "id": "agent-pool",
        "hostname": "pool-1",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
    }
    attach = AsyncMock(return_value=False)

    with (
        patch("main.require_thread_owner", AsyncMock(return_value=(user, thread))),
        patch("main.postgres_db", db),
        patch("main._thread_project_ids", AsyncMock(return_value=[])),
        patch("main._revalidate_thread_project_ids", AsyncMock(return_value=[])),
        patch("main._thread_config_drift", AsyncMock(return_value=[])),
        patch("main._thread_has_knowledge_scope", AsyncMock(return_value=False)),
        patch("main._inject_thread_dispatch_credentials", AsyncMock(return_value={})),
        patch("main._is_protected_cloud_mode_enabled", return_value=False),
        patch("main.agent_provisioner", provisioner),
        patch("main.persistent_provisioner", MagicMock(is_available=False)),
        patch("main._find_idle_persistent_agent", AsyncMock(return_value=idle)),
        patch("main._send_session_attach", attach),
        patch("main._await_late_cloud_setup", AsyncMock()),
        patch("main.ensure_session_workspace", AsyncMock()),
    ):
        result = await resume_thread("thread-1", object())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert result == {"status": "created", "thread_id": "thread-1"}
    attach.assert_awaited_once()
    provisioner.provision_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_schedules_reengage_when_no_active_row():
    """F-I2: end -> resume must re-engage protected cloud mode when the
    Slice A reconciler already revoked the grant (no active
    cloud_ro_mounts row) — otherwise a protected thread stays permanently
    mount-less after its first end/resume cycle."""
    from main import resume_thread

    thread = _resume_thread_row()
    user = {"id": "user-1"}
    db = AsyncMock()
    db.resume_thread = AsyncMock()
    db.list_thread_mounts = AsyncMock(return_value=[])
    db.get_ro_mount_by_thread = AsyncMock(return_value=None)

    with (
        patch("main.require_thread_owner", AsyncMock(return_value=(user, thread))),
        patch("main.postgres_db", db),
        patch("main._thread_project_ids", AsyncMock(return_value=[])),
        patch("main._revalidate_thread_project_ids", AsyncMock(return_value=[])),
        patch("main._is_protected_cloud_mode_enabled", return_value=True),
        patch("main._schedule_protected_engage") as schedule,
        patch("main.agent_provisioner", MagicMock(is_available=False)),
        patch("main.persistent_provisioner", MagicMock(is_available=False)),
        patch("main.ensure_session_workspace", new=AsyncMock()),
    ):
        result = await resume_thread("thread-1", object())

    assert result == {"status": "created", "thread_id": "thread-1"}
    db.get_ro_mount_by_thread.assert_awaited_once_with("thread-1")
    schedule.assert_called_once()
    _, kwargs = schedule.call_args
    assert kwargs["user_id"] == "user-1"
    assert kwargs["mount_rows"] == []


@pytest.mark.asyncio
async def test_resume_skips_reengage_when_active_row_present():
    """No re-engage when a live grant already covers this thread — resume
    must not spam a fresh reader/grant on every reconnect."""
    from main import resume_thread

    thread = _resume_thread_row()
    user = {"id": "user-1"}
    db = AsyncMock()
    db.resume_thread = AsyncMock()
    db.list_thread_mounts = AsyncMock(return_value=[])
    db.get_ro_mount_by_thread = AsyncMock(return_value=_ACTIVE_NC_ROW)

    with (
        patch("main.require_thread_owner", AsyncMock(return_value=(user, thread))),
        patch("main.postgres_db", db),
        patch("main._thread_project_ids", AsyncMock(return_value=[])),
        patch("main._revalidate_thread_project_ids", AsyncMock(return_value=[])),
        patch("main._is_protected_cloud_mode_enabled", return_value=True),
        patch("main._schedule_protected_engage") as schedule,
        patch("main.agent_provisioner", MagicMock(is_available=False)),
        patch("main.persistent_provisioner", MagicMock(is_available=False)),
        patch("main.ensure_session_workspace", new=AsyncMock()),
    ):
        await resume_thread("thread-1", object())

    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_resume_skips_reengage_for_non_protected_thread():
    """Regression guard: an ordinary (non-protected) thread's resume must
    never touch the protected-engage registry."""
    from main import resume_thread

    thread = _resume_thread_row(metadata={})
    user = {"id": "user-1"}
    db = AsyncMock()
    db.resume_thread = AsyncMock()
    db.list_thread_mounts = AsyncMock(return_value=[])
    db.get_ro_mount_by_thread = AsyncMock(return_value=None)

    with (
        patch("main.require_thread_owner", AsyncMock(return_value=(user, thread))),
        patch("main.postgres_db", db),
        patch("main._thread_project_ids", AsyncMock(return_value=[])),
        patch("main._revalidate_thread_project_ids", AsyncMock(return_value=[])),
        patch("main._is_protected_cloud_mode_enabled", return_value=True),
        patch("main._schedule_protected_engage") as schedule,
        patch("main.agent_provisioner", MagicMock(is_available=False)),
        patch("main.persistent_provisioner", MagicMock(is_available=False)),
        patch("main.ensure_session_workspace", new=AsyncMock()),
    ):
        await resume_thread("thread-1", object())

    schedule.assert_not_called()
    db.get_ro_mount_by_thread.assert_not_awaited()
