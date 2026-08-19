"""Terminal thread teardown must release the thread's VM.

``_archive_and_cleanup_workspace`` gated the thread-VM release on an
*allowlist* — ``status in ("provisioning", "created", "ready")`` — while the job
branch a few lines below used a *denylist*. Every other status therefore fell
through and ``release_thread_vm`` never ran, so the VM's rootdisk DataVolume
(20 GiB) and its Headscale node leaked permanently. ``suspended`` is the case
that matters: the rootdisk is kept on purpose while suspended, and terminal
teardown is the only thing that ever purges it. Kept-disk GC is jobs-only and
the controller's orphan backstop ships off, so nothing else reclaims them.

See knowledge-base/knowledge/issues/vm_reliability_assessment.md P1-7.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import orchestrator.main as orch_main


def _thread_with_vm(status):
    metadata = {"vm": {"status": status}} if status is not None else {}
    return {"id": "t1", "metadata": metadata}


async def _cleanup_thread(status):
    """Run the real cleanup for a thread whose VM sits at ``status``."""
    vm_provisioner = SimpleNamespace(
        is_available=True, release_thread_vm=AsyncMock(return_value=True)
    )
    with (
        patch.object(
            orch_main,
            "postgres_db",
            SimpleNamespace(get_thread=AsyncMock(return_value=_thread_with_vm(status))),
        ),
        patch.object(orch_main, "vm_provisioner", vm_provisioner),
        patch.object(
            orch_main, "container_provisioner", SimpleNamespace(is_available=False)
        ),
    ):
        await orch_main._archive_and_cleanup_workspace("t1", entity_type="threads")
    return vm_provisioner.release_thread_vm


class TestThreadVmReleasedOnTeardown:
    @pytest.mark.asyncio
    async def test_suspended_thread_vm_is_released(self):
        # The leak: a suspended VM keeps its rootdisk, and this is the only
        # path that ever purges it.
        release = await _cleanup_thread("suspended")
        release.assert_awaited_once_with("t1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status", ["ready", "created", "provisioning", "failed", "delete_failed"]
    )
    async def test_live_or_failed_thread_vm_is_released(self, status):
        release = await _cleanup_thread(status)
        release.assert_awaited_once_with("t1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["deleted", "deleting"])
    async def test_already_torn_down_thread_vm_is_not_released_again(self, status):
        release = await _cleanup_thread(status)
        release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_thread_that_never_had_a_vm_is_left_alone(self):
        # Guards the obvious mis-fix: swapping the allowlist for a bare denylist
        # makes a VM-less thread's absent status pass the check and fire a
        # spurious teardown.
        release = await _cleanup_thread(None)
        release.assert_not_awaited()


async def _cleanup_job(status):
    """Run the real cleanup for a job whose VM sits at ``status``."""
    context = {"vm": {"status": status}} if status is not None else {}
    vm_provisioner = SimpleNamespace(
        is_available=True, release_vm=AsyncMock(return_value=True)
    )
    with (
        patch.object(
            orch_main,
            "postgres_db",
            SimpleNamespace(
                get_job=AsyncMock(return_value={"id": "j1", "context": context})
            ),
        ),
        patch.object(orch_main, "vm_provisioner", vm_provisioner),
        patch.object(
            orch_main, "container_provisioner", SimpleNamespace(is_available=False)
        ),
    ):
        await orch_main._archive_and_cleanup_workspace("j1")
    return vm_provisioner.release_vm


class TestJobAndThreadTeardownAgree:
    """Both entity types must reclaim on the same statuses.

    They are separate branches of one function and drifted once already. Testing
    them behaviourally — rather than only asserting the shared predicate — is
    what catches a future edit that inlines one side again.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        ["suspended", "ready", "created", "provisioning", "failed", "delete_failed"],
    )
    async def test_both_release_on_the_same_live_statuses(self, status):
        thread_release = await _cleanup_thread(status)
        job_release = await _cleanup_job(status)
        assert thread_release.await_count == job_release.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["deleted", "deleting", None])
    async def test_both_skip_on_the_same_finished_statuses(self, status):
        thread_release = await _cleanup_thread(status)
        job_release = await _cleanup_job(status)
        assert thread_release.await_count == job_release.await_count == 0
