import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
for p in (str(project_root), str(project_root / "orchestrator")):
    if p not in sys.path:
        sys.path.insert(0, p)

from unittest.mock import AsyncMock  # noqa: E402

from services.session_provisioner import (  # noqa: E402
    ensure_session_workspace,
    reconcile_session_workspaces,
)
from services.workspace_lifecycle import EnsureOutcome, WorkspaceOwner  # noqa: E402


PINNED_THREAD_ID = "11111111-1111-4111-8111-111111111111"
PINNED_THREAD_ID_2 = "22222222-2222-4222-8222-222222222222"
PINNED_RUNTIME_GENERATION = "33333333-3333-4333-8333-333333333333"


def _pinned_thread(thread_id: str, *, workspace_status: str) -> dict:
    return {
        "id": thread_id,
        "status": "active",
        "execution_lane": "pinned",
        "runtime_generation": PINNED_RUNTIME_GENERATION,
        "runtime_retirement_token": None,
        "metadata": {"workspace_container": {"status": workspace_status}},
    }


@pytest.mark.asyncio
async def test_failed_session_workspace_is_recreated():
    """Regression for the stuck RAG session: a 'failed' workspace on an active
    thread must be recreated, not left stuck."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value=_pinned_thread(PINNED_THREAD_ID, workspace_status="failed")
    )
    prov = AsyncMock()
    prov.create_pinned_thread_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()
    res = await ensure_session_workspace(
        PINNED_THREAD_ID, db=db, provisioner=prov, suspension=susp
    )
    prov.create_pinned_thread_workspace.assert_awaited_once_with(PINNED_THREAD_ID)
    assert res.outcome in (EnsureOutcome.PENDING, EnsureOutcome.READY)


@pytest.mark.asyncio
async def test_pinned_workspace_create_reuses_outer_runtime_lock():
    """The pinned ensure owns the thread lock across its authority re-read.

    Its lifecycle delegate must not try to acquire the same non-reentrant
    advisory lock again while creating a missing workspace.
    """

    class _LockingDB:
        def __init__(self):
            self.lock_entries = 0

        async def get_thread(self, _thread_id):
            return _pinned_thread(PINNED_THREAD_ID, workspace_status="deleted")

        @asynccontextmanager
        async def thread_advisory_lock(self, _thread_id):
            self.lock_entries += 1
            if self.lock_entries > 1:
                raise AssertionError("pinned workspace ensure reacquired its own lock")
            yield True

    db = _LockingDB()
    provisioner = AsyncMock()
    provisioner.create_pinned_thread_workspace = AsyncMock(return_value=True)

    result = await ensure_session_workspace(
        PINNED_THREAD_ID,
        db=db,
        provisioner=provisioner,
        suspension=AsyncMock(),
    )

    assert result is not None and result.outcome == EnsureOutcome.PENDING
    assert db.lock_entries == 1
    provisioner.create_pinned_thread_workspace.assert_awaited_once_with(
        PINNED_THREAD_ID,
        runtime_lock_held=True,
    )


@pytest.mark.asyncio
async def test_ensure_skips_ended_thread():
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={"id": "t1", "status": "ended", "metadata": {}}
    )
    prov = AsyncMock()
    susp = AsyncMock()
    res = await ensure_session_workspace("t1", db=db, provisioner=prov, suspension=susp)
    assert res is None
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_skips_missing_thread():
    db = AsyncMock()
    db.get_thread = AsyncMock(return_value=None)
    prov = AsyncMock()
    res = await ensure_session_workspace(
        "gone", db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert res is None
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_handles_str_metadata_ready():
    """metadata stored as a JSON string is parsed; a ready workspace is a no-op."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": '{"workspace_container": {"status": "ready"}}',
        }
    )
    prov = AsyncMock()
    res = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert res.outcome == EnsureOutcome.READY
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_stateless_ready_workspace_holds_same_name_replacement_uid():
    runtime_uid = "11111111-1111-4111-8111-111111111111"
    db = AsyncMock()
    db.get_thread = AsyncMock(return_value=_stateless_sandbox_thread("ready"))
    prov = AsyncMock()
    prov.workspace_pod_authority = AsyncMock(return_value="replacement")
    prov.create_workspace = AsyncMock(return_value=True)

    res = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=AsyncMock()
    )

    assert res.outcome == EnsureOutcome.PENDING
    prov.workspace_pod_authority.assert_awaited_once()
    assert prov.workspace_pod_authority.await_args.kwargs == {
        "expected_runtime_incarnation": runtime_uid
    }
    prov.create_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_ready_workspace_keeps_phase_only_drift_probe():
    """The stateless runtime fence must not change pinned workspace admission."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": PINNED_THREAD_ID,
            "status": "active",
            "execution_lane": "pinned",
            "runtime_generation": PINNED_RUNTIME_GENERATION,
            "runtime_retirement_token": None,
            "metadata": {
                "config_override": {"workspace": {"backend": "sandbox"}},
                "workspace_container": {
                    "status": "ready",
                    "provisioner": "k8s",
                    "_runtime_incarnation": ("11111111-1111-4111-8111-111111111111"),
                },
            },
        }
    )
    prov = AsyncMock()
    prov.workspace_pod_authority = AsyncMock(return_value="exact_live")

    res = await ensure_session_workspace(
        PINNED_THREAD_ID, db=db, provisioner=prov, suspension=AsyncMock()
    )

    assert res.outcome == EnsureOutcome.READY
    prov.workspace_pod_live.assert_awaited_once_with(
        WorkspaceOwner.session(PINNED_THREAD_ID)
    )


@pytest.mark.asyncio
async def test_reconcile_iterates_and_counts():
    db = AsyncMock()
    db.list_threads_needing_workspace = AsyncMock(
        return_value=[{"id": PINNED_THREAD_ID}, {"id": PINNED_THREAD_ID_2}]
    )

    async def _get_thread(tid):
        return _pinned_thread(tid, workspace_status="failed")

    db.get_thread = AsyncMock(side_effect=_get_thread)
    prov = AsyncMock()
    prov.create_pinned_thread_workspace = AsyncMock(return_value=True)
    n = await reconcile_session_workspaces(
        db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert n == 2
    assert prov.create_pinned_thread_workspace.await_count == 2


@pytest.mark.asyncio
async def test_reconcile_empty_is_noop():
    db = AsyncMock()
    db.list_threads_needing_workspace = AsyncMock(return_value=[])
    n = await reconcile_session_workspaces(
        db=db, provisioner=AsyncMock(), suspension=AsyncMock()
    )
    assert n == 0


@pytest.mark.asyncio
async def test_reconcile_survives_one_thread_failing():
    """One bad thread must not abort the whole sweep."""
    db = AsyncMock()
    db.list_threads_needing_workspace = AsyncMock(
        return_value=[{"id": "bad"}, {"id": "good"}]
    )

    async def _get_thread(tid):
        if tid == "bad":
            raise RuntimeError("boom")
        return {
            "id": tid,
            "status": "active",
            "metadata": {"workspace_container": {"status": "failed"}},
        }

    db.get_thread = AsyncMock(side_effect=_get_thread)
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    n = await reconcile_session_workspaces(
        db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert n == 1  # only "good" succeeded; "bad" was caught


@pytest.mark.asyncio
async def test_ensure_skips_lite_backend_thread():
    """A virtual/none session runs with no workspace pod (no_workspace_agent_mode.md
    §4) — ensure must no-op rather than provision one."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": {
                "config_override": {"workspace": {"backend": "virtual"}},
            },
        }
    )
    prov = AsyncMock()
    res = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert res is None
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_skips_lite_backend_thread_str_metadata():
    """Same skip when metadata arrives as a JSON string (asyncpg JSONB)."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": '{"config_override": {"workspace": {"backend": "none"}}}',
        }
    )
    prov = AsyncMock()
    res = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert res is None
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_suspended_workspace_status_fires_restore():
    """An active thread whose workspace is 'suspended' is restored (fire-and-forget),
    not recreated."""
    import asyncio

    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": {"workspace_container": {"status": "suspended"}},
        }
    )
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()
    susp.restore = AsyncMock(return_value=True)
    res = await ensure_session_workspace("t1", db=db, provisioner=prov, suspension=susp)
    await asyncio.sleep(0)  # let the fire-and-forget restore task run
    susp.restore.assert_awaited_once()
    prov.create_workspace.assert_not_called()
    assert res.outcome == EnsureOutcome.PENDING


@pytest.mark.asyncio
async def test_ensure_skips_vm_backend_thread():
    """A vm-tier session's workspace IS the VM (metadata.vm, owned by
    vm_provisioner) — ensure must not provision a sandbox container alongside it
    (knowledge-base/knowledge/issues/session_vm_backend_never_attaches.md, Defect 1)."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": {
                "config_override": {"workspace": {"backend": "vm"}},
            },
        }
    )
    prov = AsyncMock()
    res = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert res is None
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_skips_vm_backend_thread_legacy_remote_alias():
    """'remote' is the legacy alias for 'vm' and must skip identically."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": {
                "config_override": {"workspace": {"backend": "remote"}},
            },
        }
    )
    prov = AsyncMock()
    res = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert res is None
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_skips_vm_backend_thread_str_metadata():
    """Same skip when metadata arrives as a JSON string (asyncpg JSONB)."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": '{"config_override": {"workspace": {"backend": "vm"}}}',
        }
    )
    prov = AsyncMock()
    res = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert res is None
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_skips_vm_thread_with_gitea_workspace_container():
    """The exact production shape that regressed: _setup_gitea writes
    workspace_container={git_remote_url, repo_name} for EVERY thread including
    vm-tier ones, with no 'status' key. That entry must not be read as "a
    container belongs here" — this is the state that made both /prepare and the
    reconcile sweep provision a sandbox pod for a VM session."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": {
                "config_override": {"workspace": {"backend": "vm"}},
                "workspace_container": {
                    "git_remote_url": "http://gitea/srw/thread-t1.git",
                    "repo_name": "thread-t1",
                },
            },
        }
    )
    prov = AsyncMock()
    res = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert res is None
    prov.create_workspace.assert_not_called()


# ---------------------------------------------------------------------------
# Suspended VM sessions must be restored on reconnect
# (knowledge-base/knowledge/features/vm_workspace_persistence_reconciliation.md, live-gate step 4)
# ---------------------------------------------------------------------------


def _suspended_vm_thread(backend="vm", vm_status="deleted", rootdisk="kept"):
    """A VM session after suspend. vm.status is 'deleted' rather than
    'suspended' because the controller's async delete-status overwrites the
    suspend marker; rootdisk='kept' is the durable signature of "torn down
    with the disk waiting"."""
    return {
        "id": "t-vm",
        "status": "suspended",
        "metadata": {
            "config_override": {"workspace": {"backend": backend}},
            "workspace_container": {
                "git_remote_url": "http://gitea/srw/thread-t-vm.git",
                "repo_name": "thread-t-vm",
            },
            "vm": {
                "status": vm_status,
                "rootdisk": rootdisk,
                "ssh_host": "100.64.2.9",
                "ssh_port": 22,
            },
        },
    }


@pytest.mark.asyncio
async def test_suspended_vm_thread_is_restored_on_ensure():
    """The reconnect path: /prepare fires ensure_session_workspace; a VM thread
    whose disk is waiting must get its VM back. Without this trigger nothing
    restores a suspended VM session — the two main.py triggers key on
    workspace_container.status, which VM suspend never writes (live-gate
    finding, thread a1240add)."""
    db = AsyncMock()
    db.get_thread = AsyncMock(return_value=_suspended_vm_thread())
    prov = AsyncMock()
    susp = AsyncMock()
    susp.restore = AsyncMock(return_value=True)

    res = await ensure_session_workspace(
        "t-vm", db=db, provisioner=prov, suspension=susp
    )

    susp.restore.assert_awaited_once()
    owner = susp.restore.await_args.args[0]
    assert owner.kind == "session" and owner.id == "t-vm"
    assert res is not None and res.status == "restoring"
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_upgraded_suspended_vm_thread_is_restored_despite_lite_backend():
    """An UPGRADED thread still declares its original backend ('virtual' here)
    — the upgrade endpoints never rewrite it. The restore check runs before the
    lite arm so the declared string cannot hide the waiting disk."""
    db = AsyncMock()
    db.get_thread = AsyncMock(return_value=_suspended_vm_thread(backend="virtual"))
    prov = AsyncMock()
    susp = AsyncMock()
    susp.restore = AsyncMock(return_value=True)

    res = await ensure_session_workspace(
        "t-vm", db=db, provisioner=prov, suspension=susp
    )

    susp.restore.assert_awaited_once()
    assert res is not None and res.status == "restoring"


@pytest.mark.asyncio
async def test_purged_disk_does_not_trigger_restore():
    """rootdisk='purged' (terminal release) means there is nothing to reattach
    — the VM arm returns None exactly as before."""
    db = AsyncMock()
    db.get_thread = AsyncMock(return_value=_suspended_vm_thread(rootdisk="purged"))
    prov = AsyncMock()
    susp = AsyncMock()

    res = await ensure_session_workspace(
        "t-vm", db=db, provisioner=prov, suspension=susp
    )

    susp.restore.assert_not_called()
    assert res is None


@pytest.mark.asyncio
async def test_live_vm_thread_does_not_trigger_restore():
    """A running VM (status=ready) has nothing to restore; the vm arm skips as
    before. Also guards double-fire: 'restoring' itself must not re-trigger."""
    for status in ("ready", "restoring", "provisioning"):
        db = AsyncMock()
        db.get_thread = AsyncMock(return_value=_suspended_vm_thread(vm_status=status))
        susp = AsyncMock()

        res = await ensure_session_workspace(
            "t-vm", db=db, provisioner=AsyncMock(), suspension=susp
        )

        susp.restore.assert_not_called()
        assert res is None


@pytest.mark.asyncio
async def test_container_thread_with_no_vm_context_is_untouched():
    """Sandbox threads never carry vm.rootdisk — their suspended restore keeps
    flowing through ensure_workspace's own branch."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value=_pinned_thread(PINNED_THREAD_ID, workspace_status="failed")
    )
    prov = AsyncMock()
    prov.create_pinned_thread_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()

    res = await ensure_session_workspace(
        PINNED_THREAD_ID, db=db, provisioner=prov, suspension=susp
    )

    # unchanged: falls through to ensure_workspace, which recreates
    prov.create_pinned_thread_workspace.assert_awaited_once_with(PINNED_THREAD_ID)
    assert res is not None


def _stateless_sandbox_thread(status: str, *, restore_required: bool = False):
    runtime_uid = "11111111-1111-4111-8111-111111111111"
    workspace_generation = "22222222-2222-4222-8222-222222222222"
    workspace = {
        "status": status,
        "provisioner": "k8s",
        "_runtime_incarnation": runtime_uid,
    }
    binding = None
    if status == "ready":
        workspace.update(
            {
                "pod_ip": "10.42.0.7",
                "port": 30022,
                "pod_name": "ws-thread-t1",
                "namespace": "agent-workspaces",
                "_canvas_workspace_generation": workspace_generation,
            }
        )
        binding = {
            "generation": workspace_generation,
            "kind": "remote",
            "backing_id": f"k8s-pod:agent-workspaces:{runtime_uid}",
            "ssh_host_key_fingerprint": "SHA256:trusted",
        }
    if restore_required:
        workspace["_snapshot_restore_required"] = True
    return {
        "id": "t1",
        "status": "awaiting_user",
        "execution_lane": "stateless",
        "metadata": {
            "config_override": {"workspace": {"backend": "sandbox"}},
            "workspace_container": workspace,
            **({"_workspace_binding": binding} if binding is not None else {}),
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda thread: thread["metadata"]["config_override"].update({"officer": []}),
        lambda thread: thread["metadata"]["config_override"].update(
            {"officer": {"enabled": True}}
        ),
        lambda thread: thread["metadata"].update({"protected_cloud": True}),
        lambda thread: thread["metadata"].update({"vm": {"status": "ready"}}),
        lambda thread: thread["metadata"]["workspace_container"].update(
            {"provisioner": "docker"}
        ),
        lambda thread: thread["metadata"]["workspace_container"].update(
            {"_runtime_creation": None}
        ),
    ],
    ids=[
        "malformed-officer",
        "officer-enabled",
        "protected-cloud",
        "vm-evidence",
        "docker-workspace",
        "malformed-create-marker",
    ],
)
async def test_stateless_ensure_combined_classifier_refuses_before_effects(mutate):
    thread = _stateless_sandbox_thread("pending")
    mutate(thread)
    db = AsyncMock()
    db.get_thread = AsyncMock(return_value=thread)
    provisioner = AsyncMock()
    suspension = AsyncMock()

    result = await ensure_session_workspace(
        "t1", db=db, provisioner=provisioner, suspension=suspension
    )

    assert result is not None and result.outcome == EnsureOutcome.PENDING
    provisioner.create_workspace.assert_not_awaited()
    provisioner.workspace_pod_authority.assert_not_awaited()
    suspension.restore.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["deleted", "failed", "created", "ready"])
async def test_failed_stateless_snapshot_restore_retries_extract_not_create(status):
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value=_stateless_sandbox_thread(status, restore_required=True)
    )
    prov = AsyncMock()
    prov.workspace_pod_authority = AsyncMock(return_value="exact_live")
    susp = AsyncMock()
    susp.restore = AsyncMock(return_value=False)

    res = await ensure_session_workspace("t1", db=db, provisioner=prov, suspension=susp)

    assert res is not None and res.status == "failed"
    susp.restore.assert_awaited_once_with(
        WorkspaceOwner.session("t1"),
        expected_runtime_incarnation="11111111-1111-4111-8111-111111111111",
    )
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [None, 0, "", [], {}, "true", 1])
async def test_malformed_restore_marker_blocks_background_ensure_effects(malformed):
    thread = _stateless_sandbox_thread("deleted")
    workspace = thread["metadata"]["workspace_container"]
    workspace.pop("_runtime_incarnation")
    workspace["_snapshot_restore_required"] = malformed
    db = AsyncMock()
    db.get_thread = AsyncMock(return_value=thread)
    provisioner = AsyncMock()
    suspension = AsyncMock()

    result = await ensure_session_workspace(
        "t1", db=db, provisioner=provisioner, suspension=suspension
    )

    assert result is not None and result.outcome == EnsureOutcome.PENDING
    provisioner.create_workspace.assert_not_awaited()
    provisioner.workspace_pod_authority.assert_not_awaited()
    suspension.restore.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_workspace_restore_is_single_owner_across_replicas():
    """A second HA caller cannot concurrently extract the same snapshot."""

    class _LockingDB:
        def __init__(self):
            self.thread = _stateless_sandbox_thread("suspended", restore_required=True)
            self.lock = asyncio.Lock()

        async def get_thread(self, _thread_id):
            return self.thread

        @asynccontextmanager
        async def stateless_session_workspace_ensure_lock(self, _thread_id):
            if self.lock.locked():
                yield False
                return
            await self.lock.acquire()
            try:
                yield True
            finally:
                self.lock.release()

    db = _LockingDB()
    entered_restore = asyncio.Event()
    release_restore = asyncio.Event()

    async def _restore(_owner, **_kwargs):
        entered_restore.set()
        await release_restore.wait()
        db.thread = _stateless_sandbox_thread("ready")
        return True

    susp = AsyncMock()
    susp.restore = AsyncMock(side_effect=_restore)
    prov = AsyncMock()
    prov.workspace_pod_authority = AsyncMock(return_value="exact_live")

    winner = asyncio.create_task(
        ensure_session_workspace("t1", db=db, provisioner=prov, suspension=susp)
    )
    await entered_restore.wait()
    loser = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=susp
    )

    assert loser is not None and loser.outcome == EnsureOutcome.PENDING
    susp.restore.assert_awaited_once()
    release_restore.set()
    await winner

    settled = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=susp
    )
    assert settled is not None and settled.outcome == EnsureOutcome.READY
    susp.restore.assert_awaited_once()


@pytest.mark.asyncio
async def test_stateless_create_gives_up_when_thread_ends_during_actuation():
    before = _stateless_sandbox_thread("deleted")
    before_workspace = before["metadata"]["workspace_container"]
    before_workspace.pop("_runtime_incarnation")
    generation = "33333333-3333-4333-8333-333333333333"
    before_workspace["_runtime_creation"] = {
        "generation": generation,
        "mode": "create",
        "attempted": False,
        "replaces_uid": None,
    }
    ended = _stateless_sandbox_thread("ready")
    ended["status"] = "ended"

    class _CreationDB:
        def __init__(self):
            self.rows = iter([before, before, ended])

        async def get_thread(self, _thread_id):
            return next(self.rows)

        @asynccontextmanager
        async def stateless_session_workspace_ensure_lock(self, _thread_id):
            yield True

        async def prepare_stateless_thread_workspace_creation(self, *_args, **_kwargs):
            return {
                "state": "pending",
                "creation": {
                    "generation": generation,
                    "mode": "create",
                    "attempted": False,
                    "replaces_uid": None,
                },
            }

    db = _CreationDB()
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    prov.release_workspace = AsyncMock(return_value=True)

    res = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=AsyncMock()
    )

    assert res is None
    prov.create_workspace.assert_awaited_once_with(
        WorkspaceOwner.session("t1"),
        stateless_creation_generation=generation,
        allow_stateless_create=True,
    )
    prov.release_workspace.assert_awaited_once_with(
        WorkspaceOwner.session("t1"), reclaim_volume=False
    )


@pytest.mark.asyncio
async def test_markerless_stateless_physical_row_never_mints_or_creates():
    thread = _stateless_sandbox_thread("pending")
    thread["metadata"]["workspace_container"].pop("_runtime_incarnation")

    class _DB:
        def __init__(self):
            self.prepare_calls = 0

        async def get_thread(self, _thread_id):
            return thread

        @asynccontextmanager
        async def stateless_session_workspace_ensure_lock(self, _thread_id):
            yield True

        async def prepare_stateless_thread_workspace_creation(self, *_args, **_kwargs):
            self.prepare_calls += 1
            raise AssertionError("markerless recovery must not mint authority")

    db = _DB()
    provisioner = AsyncMock()

    result = await ensure_session_workspace(
        "t1", db=db, provisioner=provisioner, suspension=AsyncMock()
    )

    assert result is not None and result.outcome == EnsureOutcome.PENDING
    assert db.prepare_calls == 0
    provisioner.create_workspace.assert_not_awaited()
    provisioner.workspace_pod_authority.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_suspended_wake_keeps_its_successfully_restored_workspace():
    before = _stateless_sandbox_thread("suspended", restore_required=True)
    restored = _stateless_sandbox_thread("ready")
    restored["status"] = "suspended"
    db = AsyncMock()
    db.get_thread = AsyncMock(side_effect=[before, restored])
    prov = AsyncMock()
    prov.workspace_pod_authority = AsyncMock(return_value="exact_live")
    prov.release_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()
    susp.restore = AsyncMock(return_value=True)

    res = await ensure_session_workspace("t1", db=db, provisioner=prov, suspension=susp)

    assert res is not None and res.status == "restoring"
    susp.restore.assert_awaited_once_with(
        WorkspaceOwner.session("t1"),
        expected_runtime_incarnation="11111111-1111-4111-8111-111111111111",
    )
    prov.release_workspace.assert_not_awaited()
