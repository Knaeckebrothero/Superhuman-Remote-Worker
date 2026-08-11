"""Integration seams for the stateless cloud generation fence.

The transport/store algorithms have their own focused tests. These contracts
pin the lifecycle ordering around them so a later refactor cannot pull before
recovering, complete while a PUT is live, or reintroduce an unfenced teardown
push.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import src.api.persistent_app as papp
from src.api.lease_context import LeaseHandle, current_lease
from src.api.turn_executor import StatelessTurnExecutor
from src.persistent_graph import PersistentLoopCallbacks, run_persistent_loop
from src.services.cloud_sync.coordinator import (
    CloudSyncGenerationError,
    MountSync,
    WorkspaceSyncCoordinator,
)
from src.shared.cloud_sync_generations import (
    EMPTY_BASELINE_SHA256,
    CloudSyncRequirement,
)


THREAD_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_GENERATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE_RUNTIME_INCARNATION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LEASE_TOKEN = 47
SCOPE_SHA256 = "a" * 64
MOUNT_ID = "mount:logical-destination"


def _requirement(generation: int = LEASE_TOKEN) -> CloudSyncRequirement:
    return CloudSyncRequirement(
        mount_id=MOUNT_ID,
        required_generation=generation,
        acknowledged_generation=0,
        required_lease_token=generation,
        workspace_generation=WORKSPACE_GENERATION,
        sync_scope_sha256=SCOPE_SHA256,
    )


def _install_lease(token: int = LEASE_TOKEN):
    handle = LeaseHandle()
    handle.update(THREAD_ID, token)
    reset_token = current_lease.set(handle)
    return handle, reset_token


@pytest.fixture(autouse=True)
def _reset_pending_cloud_task():
    papp._pending_cloud_push_task = None
    yield
    task = papp._pending_cloud_push_task
    if task is not None and not task.done():
        task.cancel()
    papp._pending_cloud_push_task = None


@pytest.mark.asyncio
async def test_stateless_start_recovers_then_strict_pulls_then_arms(monkeypatch):
    order: list[str] = []

    async def pull(
        *, strict: bool = False, before_write=None, force_unknown: bool = False
    ):
        assert strict is True
        assert before_write is not None
        assert force_unknown is True
        await before_write()
        order.append("pull")
        return []

    async def capture_generation_baseline():
        order.append("baseline")
        return {}, EMPTY_BASELINE_SHA256

    mount_sync = SimpleNamespace(
        pull=pull,
        capture_generation_baseline=capture_generation_baseline,
    )
    coordinator = WorkspaceSyncCoordinator(
        [
            MountSync(
                mount_id=MOUNT_ID,
                target_path="",
                sync=mount_sync,
                sync_scope_sha256=SCOPE_SHA256,
            )
        ],
        thread_id=THREAD_ID,
        workspace_generation=WORKSPACE_GENERATION,
    )

    async def recover(*_args, **_kwargs):
        order.append("recover")
        return {MOUNT_ID: []}

    coordinator.reconcile_before_pull = AsyncMock(side_effect=recover)
    postgres = object()
    session = SimpleNamespace(postgres_conn=postgres, cloud_sync_requirements={})
    requirement = _requirement()

    async def arm(*_args, **_kwargs):
        order.append("arm")
        return {MOUNT_ID: requirement}

    handle, reset_token = _install_lease()
    monkeypatch.setenv("STATELESS_EXECUTOR", "1")
    try:
        with (
            patch.object(papp, "_session", session),
            patch.object(papp, "_thread_id", THREAD_ID),
            patch(
                "src.shared.cloud_sync_generations.cloud_sync_lease_is_current",
                AsyncMock(return_value=True),
            ),
            patch(
                "src.shared.cloud_sync_generations.load_cloud_sync_requirements",
                AsyncMock(return_value={}),
            ),
            patch(
                "src.shared.cloud_sync_generations.arm_cloud_sync_generations",
                AsyncMock(side_effect=arm),
            ),
            patch.object(papp, "_broadcast"),
        ):
            await papp._prepare_stateless_cloud_sync(coordinator, turn_id=3)
    finally:
        current_lease.reset(reset_token)

    assert handle.active
    assert order == ["recover", "pull", "baseline", "arm"]
    assert session.cloud_sync_requirements == {MOUNT_ID: requirement}


@pytest.mark.asyncio
async def test_stateless_pull_failure_blocks_arm_and_turn_start(monkeypatch):
    async def failed_pull(
        *, strict: bool = False, before_write=None, force_unknown: bool = False
    ):
        assert strict is True
        assert before_write is not None
        assert force_unknown is True
        raise RuntimeError("remote listing failed")

    mount_sync = SimpleNamespace(pull=failed_pull)
    coordinator = WorkspaceSyncCoordinator(
        [
            MountSync(
                mount_id=MOUNT_ID,
                target_path="",
                sync=mount_sync,
                sync_scope_sha256=SCOPE_SHA256,
            )
        ],
        thread_id=THREAD_ID,
        workspace_generation=WORKSPACE_GENERATION,
    )
    coordinator.reconcile_before_pull = AsyncMock(return_value={MOUNT_ID: []})
    session = SimpleNamespace(postgres_conn=object(), cloud_sync_requirements={})
    arm = AsyncMock()
    _handle, reset_token = _install_lease()
    monkeypatch.setenv("STATELESS_EXECUTOR", "1")
    try:
        with (
            patch.object(papp, "_session", session),
            patch.object(papp, "_thread_id", THREAD_ID),
            patch(
                "src.shared.cloud_sync_generations.cloud_sync_lease_is_current",
                AsyncMock(return_value=True),
            ),
            patch(
                "src.shared.cloud_sync_generations.load_cloud_sync_requirements",
                AsyncMock(return_value={}),
            ),
            patch("src.shared.cloud_sync_generations.arm_cloud_sync_generations", arm),
            patch.object(papp.asyncio, "sleep", AsyncMock()),
            patch.object(papp, "_broadcast"),
        ):
            with pytest.raises(
                CloudSyncGenerationError, match="pull failed|pull refused"
            ):
                await papp._prepare_stateless_cloud_sync(coordinator, turn_id=4)
    finally:
        current_lease.reset(reset_token)

    arm.assert_not_awaited()
    assert session.cloud_sync_requirements == {}


@pytest.mark.asyncio
async def test_stateless_turn_refuses_unrecovered_cloud_setup(monkeypatch):
    """A failed late-start retry must not silently run a cloud-backed turn."""
    session = SimpleNamespace(workspace_sync=None, turn_count=0)
    retry = AsyncMock()
    monkeypatch.setenv("STATELESS_EXECUTOR", "1")

    with (
        patch.object(papp, "_session", session),
        patch.object(papp, "_thread_id", THREAD_ID),
        patch.object(papp, "_cloud_sync_retry_pending", True),
        patch.object(papp, "_retry_cloud_sync_start", retry),
        patch.object(papp, "_broadcast"),
    ):
        with pytest.raises(CloudSyncGenerationError, match="remains degraded"):
            await papp._loop_on_turn_start(5)

    retry.assert_awaited_once_with(5)


@pytest.mark.asyncio
async def test_turn_start_failure_ends_loop_before_persist_or_completion_callback():
    """Recovery/pull/arm failure must leave the queue input unanswered.

    ``run_persistent_loop`` owns the exact ordering at issue: the stateless
    executor injects an already-persisted row, the turn-start fence raises, and
    the loop task dies without firing the completion hook that advances the
    queue's consumed watermark. The executor's loop-died -> release behavior is
    covered by ``tests/test_turn_executor.py::TestTurnError``.
    """

    start = AsyncMock(side_effect=CloudSyncGenerationError("recovery failed"))
    complete = AsyncMock()
    persist = AsyncMock()
    llm = MagicMock()
    callbacks = PersistentLoopCallbacks(
        get_user_input=AsyncMock(
            return_value={"content": "queued input", "id": "persisted-row-id"}
        ),
        on_token=AsyncMock(),
        on_thinking=AsyncMock(),
        on_tool_start=AsyncMock(),
        on_tool_result=AsyncMock(),
        permission_check=AsyncMock(return_value=True),
        on_turn_start=start,
        on_turn_complete=complete,
        on_error=AsyncMock(),
        check_interrupt=MagicMock(return_value=False),
        persist_message=persist,
    )

    with pytest.raises(CloudSyncGenerationError, match="recovery failed"):
        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[],
            context_manager=MagicMock(),
            config=SimpleNamespace(
                llm=SimpleNamespace(timeout=1),
                memory=None,
            ),
            system_prompt="system",
            callbacks=callbacks,
            messages=[],
        )

    start.assert_awaited_once_with(1)
    persist.assert_not_awaited()
    complete.assert_not_awaited()
    llm.astream.assert_not_called()


@pytest.mark.asyncio
async def test_turn_end_task_captures_token_and_requirement_snapshot(monkeypatch):
    release = asyncio.Event()
    observed: list[tuple[dict, object]] = []
    original = _requirement()
    sync = SimpleNamespace(workspace_generation=WORKSPACE_GENERATION)
    session = SimpleNamespace(
        postgres_conn=object(),
        messages=[],
        tool_decisions={},
        auxiliary_llm=None,
        workspace_sync=sync,
        cloud_sync_requirements={MOUNT_ID: original},
        overlay_mount_manager=None,
    )

    async def record_task(_sync, _turn_id, *, requirements=None, claim=None) -> None:
        await release.wait()
        observed.append((requirements, claim))

    handle, reset_token = _install_lease()
    monkeypatch.setenv("STATELESS_EXECUTOR", "1")
    try:
        with (
            patch.object(papp, "_session", session),
            patch.object(papp, "_thread_id", THREAD_ID),
            patch.object(papp, "_retire_announced_permission_rows", AsyncMock()),
            patch.object(papp, "_wire_session_aux_archiver"),
            patch.object(papp, "_save_turn_ai_messages", AsyncMock()),
            patch.object(papp, "_auto_title_after_first_turn", AsyncMock()),
            patch.object(papp, "_should_notify_cloud_stage", return_value=False),
            patch.object(papp, "_broadcast"),
            patch.object(papp, "_run_turn_end_cloud_push", record_task),
        ):
            await papp._loop_on_turn_complete_body(8)
            task = papp._pending_cloud_push_task
            assert task is not None and not task.done()

            replacement = _requirement(generation=99)
            session.cloud_sync_requirements[MOUNT_ID] = replacement
            handle.update(THREAD_ID, 88)
            release.set()
            await task
    finally:
        current_lease.reset(reset_token)

    captured_requirements, captured_claim = observed[0]
    assert captured_requirements[MOUNT_ID] is original
    assert captured_claim.lease_token == LEASE_TOKEN
    assert captured_claim.workspace_generation == WORKSPACE_GENERATION
    # The shared handle is intentionally mutable for affinity, but the task's
    # authorization query uses the scalar token captured above.
    assert captured_claim.lease_handle.lease_token == 88


@pytest.mark.asyncio
async def test_executor_never_completes_while_cloud_task_is_live():
    release = asyncio.Event()

    async def push():
        await release.wait()

    cloud_task = asyncio.create_task(push())
    pa = SimpleNamespace(_pending_cloud_push_task=cloud_task)
    executor = StatelessTurnExecutor(cloud_push_wait_seconds=0.001)

    waiter = asyncio.create_task(executor._await_cloud_push(pa))
    await asyncio.sleep(0.02)
    assert not waiter.done()
    assert not cloud_task.done()

    release.set()
    await waiter
    assert cloud_task.done()


@pytest.mark.parametrize("stateless", [True, False], ids=["stateless", "pinned"])
@pytest.mark.asyncio
async def test_teardown_skips_raw_sync_only_for_stateless(stateless: bool):
    sync = SimpleNamespace(
        push_all=AsyncMock(),
        pull_all=AsyncMock(),
        aclose=AsyncMock(),
    )
    session = SimpleNamespace(
        memory_service=None,
        messages=[],
        workspace_sync=sync,
        workspace_manager=None,
        retire_shell_owner=MagicMock(),
        cleanup=AsyncMock(),
    )

    with (
        patch.object(papp, "_session", session),
        patch.object(papp, "_thread_id", THREAD_ID),
        patch.object(papp, "_loop_task", None),
        patch.object(papp, "_event_writer", None),
        patch.object(papp, "_control_owner_agent_id", None),
        patch.object(papp, "_registered_pinned_agent_id", return_value=None),
        patch.object(papp, "_stateless_mode", return_value=stateless),
        patch.object(papp, "_stop_watchdogs"),
        patch.object(papp, "_stop_thread_control_watcher", AsyncMock()),
        patch.object(papp, "_await_pending_cloud_push", AsyncMock()),
        patch.object(papp, "_clear_all_canvas_awareness"),
        patch.object(papp, "_subscribers", {}),
        patch.object(papp, "_sessions_served", 0),
        patch.object(papp, "_max_sessions_per_process", 0),
    ):
        await papp._terminate_session_inner("test", mark_thread=False)

    if stateless:
        sync.push_all.assert_not_awaited()
        sync.pull_all.assert_not_awaited()
    else:
        sync.push_all.assert_awaited_once_with()
        sync.pull_all.assert_awaited_once_with()
    sync.aclose.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("workspace_response", "expected_backend"),
    [
        (
            {"status": "ready", "pod_ip": "10.0.0.8"},
            "sandbox",
        ),
        (
            {"vm_status": "ready", "vm_ssh_host": "10.0.0.9"},
            "vm",
        ),
    ],
)
@pytest.mark.asyncio
async def test_workspace_poll_preserves_generation(
    workspace_response: dict, expected_backend: str
):
    workspace_response["workspace_generation"] = WORKSPACE_GENERATION
    workspace_response["workspace_runtime_incarnation"] = WORKSPACE_RUNTIME_INCARNATION
    client = SimpleNamespace(
        get_thread_workspace=AsyncMock(return_value=workspace_response)
    )

    result = await papp._poll_workspace_ready(client, THREAD_ID, timeout=1)

    assert result is not None
    assert result["backend"] == expected_backend
    assert result["workspace_generation"] == WORKSPACE_GENERATION
    assert result["workspace_runtime_incarnation"] == WORKSPACE_RUNTIME_INCARNATION


@pytest.mark.asyncio
async def test_internal_workspace_payload_exposes_private_binding_generation():
    import orchestrator.main as orch_main

    thread = {
        "id": THREAD_ID,
        "user_id": None,
        "project_id": None,
        "metadata": {
            "workspace_container": {
                "status": "ready",
                "pod_ip": "10.0.0.8",
                "_canvas_workspace_generation": WORKSPACE_GENERATION,
                "_runtime_incarnation": WORKSPACE_RUNTIME_INCARNATION,
            },
            "_workspace_binding": {
                "generation": WORKSPACE_GENERATION,
                "kind": "remote",
            },
        },
    }
    with (
        patch.object(
            orch_main.postgres_db,
            "get_thread",
            AsyncMock(return_value=thread),
        ),
        patch.object(
            orch_main.postgres_db,
            "list_thread_mounts",
            AsyncMock(return_value=[]),
        ),
        patch.object(orch_main, "_thread_project_ids", AsyncMock(return_value=[])),
        patch.object(
            orch_main,
            "_revalidate_thread_project_ids",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            orch_main, "_resolve_thread_datasources", AsyncMock(return_value=None)
        ),
        patch.object(
            orch_main, "_resolve_thread_repositories", AsyncMock(return_value=None)
        ),
        patch.object(
            orch_main,
            "_agent_canvas_workspace_capabilities",
            return_value=(False, False, False),
        ),
        patch.object(
            orch_main, "_build_agent_cloud_mount", AsyncMock(return_value=None)
        ),
        patch.object(orch_main, "_build_agent_cloud_sync", return_value=None),
        patch.object(orch_main, "_cloud_workspace_driver", return_value="sync"),
        patch.object(
            orch_main,
            "main_cloud_router",
            SimpleNamespace(active=SimpleNamespace(is_initialized=False)),
        ),
        patch.object(
            orch_main, "_resolve_session_config", AsyncMock(return_value=None)
        ),
        patch.object(
            orch_main,
            "_inject_thread_dispatch_credentials",
            AsyncMock(side_effect=lambda value, **_kwargs: value),
        ),
        patch.object(
            orch_main,
            "_inject_lite_workspace_config",
            side_effect=lambda value, **_kwargs: value,
        ),
    ):
        response = await orch_main._agent_get_thread_workspace_locked(THREAD_ID)

    assert response["workspace_generation"] == WORKSPACE_GENERATION
    assert response["workspace_runtime_incarnation"] == WORKSPACE_RUNTIME_INCARNATION


def test_owner_payload_redacts_private_workspace_runtime_incarnation():
    import orchestrator.main as orch_main

    record = {
        "metadata": {
            "workspace_container": {
                "status": "ready",
                "_canvas_workspace_generation": WORKSPACE_GENERATION,
                "_runtime_incarnation": WORKSPACE_RUNTIME_INCARNATION,
            }
        }
    }

    redacted = orch_main._redact_nested_workspace_state(record, field="metadata")

    workspace = redacted["metadata"]["workspace_container"]
    assert "_canvas_workspace_generation" not in workspace
    assert "_runtime_incarnation" not in workspace


async def _internal_workspace_response_for_lite_thread(
    thread: dict,
    *,
    cloud_mount=None,
    cloud_sync=None,
):
    import orchestrator.main as orch_main

    with (
        patch.object(
            orch_main.postgres_db,
            "get_thread",
            AsyncMock(return_value=thread),
        ),
        patch.object(
            orch_main.postgres_db,
            "list_thread_mounts",
            AsyncMock(return_value=[]),
        ),
        patch.object(orch_main, "_thread_project_ids", AsyncMock(return_value=[])),
        patch.object(
            orch_main,
            "_revalidate_thread_project_ids",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            orch_main, "_resolve_thread_datasources", AsyncMock(return_value=None)
        ),
        patch.object(
            orch_main, "_resolve_thread_repositories", AsyncMock(return_value=None)
        ),
        patch.object(
            orch_main,
            "_agent_canvas_workspace_capabilities",
            return_value=(False, False, False),
        ),
        patch.object(
            orch_main,
            "_build_agent_cloud_mount",
            AsyncMock(return_value=cloud_mount),
        ),
        patch.object(orch_main, "_build_agent_cloud_sync", return_value=cloud_sync),
        patch.object(orch_main, "_cloud_workspace_driver", return_value="sync"),
        patch.object(
            orch_main,
            "main_cloud_router",
            SimpleNamespace(active=SimpleNamespace(is_initialized=False)),
        ),
        patch.object(
            orch_main, "_resolve_session_config", AsyncMock(return_value=None)
        ),
        patch.object(
            orch_main,
            "_inject_thread_dispatch_credentials",
            AsyncMock(side_effect=lambda value, **_kwargs: value),
        ),
        patch.object(
            orch_main,
            "_inject_lite_workspace_config",
            side_effect=lambda value, **_kwargs: value,
        ),
    ):
        return await orch_main._agent_get_thread_workspace_locked(THREAD_ID)


@pytest.mark.asyncio
async def test_none_tier_suppresses_structured_and_legacy_cloud_surfaces():
    """ScratchBackend state is intentionally disposable and must not mirror."""

    thread = {
        "id": THREAD_ID,
        "user_id": None,
        "project_id": None,
        "execution_lane": "stateless",
        "nc_session_folder": "Sessions/should-not-be-used",
        "metadata": {"config_override": {"workspace": {"backend": "none"}}},
    }
    response = await _internal_workspace_response_for_lite_thread(
        thread,
        cloud_mount={"driver": "rclone", "mounts": [{"id": "unexpected"}]},
        cloud_sync={"backend": "nextcloud", "webdav_url": "http://unexpected"},
    )

    assert response["workspace_generation"] is None
    assert response["cloud_mount"] is None
    assert response["cloud_sync"] is None
    assert response["nc_session_folder"] is None


@pytest.mark.asyncio
async def test_pinned_none_tier_preserves_historical_cloud_surfaces():
    """The S2 stateless guard must not change the pinned attach contract."""

    cloud_sync = {"backend": "nextcloud", "webdav_url": "http://historical"}
    thread = {
        "id": THREAD_ID,
        "user_id": None,
        "project_id": None,
        "execution_lane": "pinned",
        "nc_session_folder": "Sessions/pinned-none",
        "metadata": {"config_override": {"workspace": {"backend": "none"}}},
    }
    response = await _internal_workspace_response_for_lite_thread(
        thread,
        cloud_mount=None,
        cloud_sync=cloud_sync,
    )

    assert response["cloud_mount"] is None
    assert response["cloud_sync"] == cloud_sync
    assert response["nc_session_folder"] == "Sessions/pinned-none"


@pytest.mark.asyncio
async def test_stateless_virtual_workspace_without_binding_fails_attach_payload():
    thread = {
        "id": THREAD_ID,
        "user_id": None,
        "project_id": None,
        "execution_lane": "stateless",
        "nc_session_folder": "Sessions/thread",
        "metadata": {"config_override": {"workspace": {"backend": "virtual"}}},
    }

    with pytest.raises(HTTPException) as exc_info:
        await _internal_workspace_response_for_lite_thread(
            thread,
            cloud_sync={"backend": "nextcloud", "webdav_url": "http://cloud"},
        )

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_stateless_virtual_workspace_wrong_backing_fails_closed():
    """A UUID from an old object-store namespace is not an attestation."""

    import orchestrator.main as orch_main

    current_spec = {
        "type": "s3",
        "root": "current-bucket",
        "config": {"endpoint": "https://current.example.test"},
    }
    thread = {
        "id": THREAD_ID,
        "user_id": None,
        "project_id": None,
        "execution_lane": "stateless",
        "nc_session_folder": "Sessions/thread",
        "metadata": {
            "config_override": {"workspace": {"backend": "virtual"}},
            "_workspace_binding": {
                "kind": "virtual",
                "generation": WORKSPACE_GENERATION,
                "backing_id": f"rclone:{'0' * 64}",
            },
        },
    }

    with (
        patch.object(
            orch_main, "_virtual_workspace_rclone_spec", return_value=current_spec
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _internal_workspace_response_for_lite_thread(
            thread,
            cloud_sync={"backend": "nextcloud", "webdav_url": "http://cloud"},
        )

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_stateless_virtual_workspace_exact_backing_exposes_generation():
    """The current deterministic namespace may attest its binding UUID."""

    import orchestrator.main as orch_main
    from orchestrator.services.workspace_binding import virtual_thread_backing_id

    current_spec = {
        "type": "s3",
        "root": "current-bucket",
        "config": {"endpoint": "https://current.example.test"},
    }
    thread = {
        "id": THREAD_ID,
        "user_id": None,
        "project_id": None,
        "execution_lane": "stateless",
        "nc_session_folder": "Sessions/thread",
        "metadata": {
            "config_override": {"workspace": {"backend": "virtual"}},
            "_workspace_binding": {
                "kind": "virtual",
                "generation": WORKSPACE_GENERATION,
                "backing_id": virtual_thread_backing_id(THREAD_ID, current_spec),
            },
        },
    }

    with patch.object(
        orch_main, "_virtual_workspace_rclone_spec", return_value=current_spec
    ):
        response = await _internal_workspace_response_for_lite_thread(
            thread,
            cloud_sync={"backend": "nextcloud", "webdav_url": "http://cloud"},
        )

    assert response["workspace_generation"] == WORKSPACE_GENERATION


@pytest.mark.parametrize(("stateless", "expects_sync"), [(True, False), (False, True)])
@pytest.mark.asyncio
async def test_none_agent_cloud_suppression_is_stateless_only(
    monkeypatch, stateless: bool, expects_sync: bool
):
    """Pinned ScratchBackend keeps the pre-S2 legacy sync construction path."""

    if stateless:
        monkeypatch.setenv("STATELESS_EXECUTOR", "1")
    else:
        monkeypatch.delenv("STATELESS_EXECUTOR", raising=False)

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.cloud_mount_manager = None
            self.cloud_mount_error = None
            self.overlay_mount_manager = None
            self.workspace_manager = SimpleNamespace(
                path="/workspace",
                backend=SimpleNamespace(supports_file_tools=False),
            )
            self.workspace_sync = None
            self.cloud_sync_workspace_generation = ""
            self.postgres_conn = None
            self.tool_context = None

        async def setup(self, **kwargs):
            return None

    cloud_sync = {"backend": "nextcloud", "webdav_url": "http://historical"}
    workspace_payload = {
        "cloud_mount": None,
        "cloud_sync": cloud_sync,
        "nc_session_folder": "Sessions/pinned-none",
        "protected_cloud": False,
        "project_ids": [],
        "datasources": None,
    }
    client = SimpleNamespace(
        get_thread_workspace=AsyncMock(return_value=workspace_payload)
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(workspace=SimpleNamespace(backend="none")),
        _tactical_llm=None,
        _llm=object(),
        _auxiliary_llm=None,
        postgres_conn=None,
        vector_conn=None,
    )
    workspace_sync = MagicMock()
    workspace_sync.pull_all = AsyncMock()
    workspace_sync.__len__.return_value = 1

    with (
        patch.object(papp, "_session", None),
        patch.object(papp, "_thread_id", None),
        patch.object(papp, "_event_writer", None),
        patch.object(papp, "_agent", agent),
        patch.object(papp, "_orchestrator_client", client),
        patch.object(papp, "PersistentSession", FakeSession),
        patch.object(papp, "_session_backend_is_lite", return_value=True),
        patch.object(
            papp, "_build_sync_coordinator", return_value=workspace_sync
        ) as build_sync,
        patch.object(papp, "_restore_session_messages", AsyncMock()),
        patch.object(papp, "_update_thread_status", AsyncMock()),
        patch.object(papp, "_start_watchdogs"),
        patch.object(papp, "_officer_cfg", return_value=None),
        patch.object(papp, "_apply_session_embedding_env"),
    ):
        try:
            await papp._attach_session(THREAD_ID, config_override={})
        finally:
            papp._session = None
            papp._thread_id = None

    if expects_sync:
        build_sync.assert_called_once()
        assert build_sync.call_args.kwargs["cloud_cfg"] == cloud_sync
        workspace_sync.pull_all.assert_awaited_once_with()
    else:
        build_sync.assert_not_called()
        workspace_sync.pull_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_workspace_fetch_retains_generation_without_coordinator():
    """No-cloud pending-row checks must see the final authoritative identity."""

    instances = []

    class FakeSession:
        def __init__(self, *args, **kwargs):
            instances.append(self)
            self.cloud_mount_manager = None
            self.cloud_mount_error = None
            self.overlay_mount_manager = None
            self.workspace_manager = SimpleNamespace(
                path="/workspace",
                backend=SimpleNamespace(supports_file_tools=True),
            )
            self.workspace_sync = None
            self.cloud_sync_workspace_generation = ""
            self.postgres_conn = None
            self.tool_context = None

        async def setup(self, **kwargs):
            return None

    # The readiness response has no generation. The first metadata hydration
    # fetch also lacks it; only the later cloud-config fetch carries the binding
    # generation, and deliberately carries no cloud config/coordinator.
    client = SimpleNamespace(
        get_thread_workspace=AsyncMock(
            side_effect=[
                {"project_ids": [], "datasources": None, "cloud_mount": None},
                {
                    "workspace_generation": WORKSPACE_GENERATION,
                    "cloud_sync": None,
                    "nc_session_folder": None,
                    "cloud_sync_degraded": False,
                },
            ]
        )
    )
    readiness = {
        "backend": "sandbox",
        "remote": {"host": "10.0.0.8", "port": 22},
        "cloud_sync": None,
        "nc_session_folder": None,
    }
    agent = SimpleNamespace(
        config=object(),
        _tactical_llm=None,
        _llm=object(),
        _auxiliary_llm=None,
        postgres_conn=None,
        vector_conn=None,
    )

    with (
        patch.object(papp, "_session", None),
        patch.object(papp, "_thread_id", None),
        patch.object(papp, "_event_writer", None),
        patch.object(papp, "_agent", agent),
        patch.object(papp, "_orchestrator_client", client),
        patch.object(papp, "PersistentSession", FakeSession),
        patch.object(papp, "_poll_workspace_ready", AsyncMock(return_value=readiness)),
        patch.object(papp, "_build_sync_coordinator") as build_sync,
        patch.object(papp, "_restore_session_messages", AsyncMock()),
        patch.object(papp, "_update_thread_status", AsyncMock()),
        patch.object(papp, "_start_watchdogs"),
        patch.object(papp, "_officer_cfg", return_value=None),
        patch.object(papp, "_apply_session_embedding_env"),
    ):
        await papp._attach_session(THREAD_ID, config_override={})

    assert client.get_thread_workspace.await_count == 2
    assert len(instances) == 1
    assert instances[0].workspace_sync is None
    assert instances[0].cloud_sync_workspace_generation == WORKSPACE_GENERATION
    build_sync.assert_not_called()
