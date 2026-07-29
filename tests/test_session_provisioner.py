import sys
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
from services.workspace_lifecycle import EnsureOutcome  # noqa: E402


@pytest.mark.asyncio
async def test_failed_session_workspace_is_recreated():
    """Regression for the stuck RAG session: a 'failed' workspace on an active
    thread must be recreated, not left stuck."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": {"workspace_container": {"status": "failed"}},
        }
    )
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()
    res = await ensure_session_workspace("t1", db=db, provisioner=prov, suspension=susp)
    prov.create_workspace.assert_awaited()  # recreated — not stuck
    assert res.outcome in (EnsureOutcome.PENDING, EnsureOutcome.READY)


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
async def test_reconcile_iterates_and_counts():
    db = AsyncMock()
    db.list_threads_needing_workspace = AsyncMock(
        return_value=[{"id": "t1"}, {"id": "t2"}]
    )

    async def _get_thread(tid):
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
    assert n == 2
    assert prov.create_workspace.await_count == 2


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
    (docs/issues/session_vm_backend_never_attaches.md, Defect 1)."""
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
# (docs/features/vm_workspace_persistence_reconciliation.md, live-gate step 4)
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
        return_value={
            "id": "t-pod",
            "status": "active",
            "metadata": {"workspace_container": {"status": "failed"}},
        }
    )
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()

    res = await ensure_session_workspace(
        "t-pod", db=db, provisioner=prov, suspension=susp
    )

    # unchanged: falls through to ensure_workspace, which recreates
    prov.create_workspace.assert_awaited()
    assert res is not None
