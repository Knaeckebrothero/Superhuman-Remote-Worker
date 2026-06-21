"""Pins for the thread VM-upgrade abort endpoint (workspace_tier_upgrade.md Q7).

When the live upgrade handler's ``_poll_vm_ready`` gives up (a cold ~2.8GB CDI
import outruns the poll budget), the agent calls
``POST /api/agents/threads/{id}/abort-vm-upgrade`` so the half-provisioned VM is
torn down instead of leaking — the orphan that previously needed a manual
``kubectl delete``. The endpoint must:

- 404 on an unknown thread,
- delete the VM when the provisioner is available,
- ALWAYS mark ``metadata.vm.status='aborted'`` so the provisioning-in-progress
  guard doesn't wedge a retry, even when the provisioner is down or the delete
  raises.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import orchestrator.main as orch_main


def _db(thread):
    return SimpleNamespace(
        get_thread=AsyncMock(return_value=thread),
        merge_thread_vm_context=AsyncMock(return_value=True),
    )


def _provisioner(*, available=True, delete_result=True, delete_exc=None):
    return SimpleNamespace(
        is_available=available,
        delete_thread_vm=AsyncMock(return_value=delete_result, side_effect=delete_exc),
    )


class TestAbortThreadVmUpgrade:
    @pytest.mark.asyncio
    async def test_404_when_thread_missing(self):
        db = _db(None)
        with (
            patch.object(orch_main, "require_internal", AsyncMock()),
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main, "vm_provisioner", _provisioner()),
        ):
            with pytest.raises(orch_main.HTTPException) as exc:
                await orch_main.agent_abort_thread_vm_upgrade(MagicMock(), "tid")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_deletes_vm_and_marks_aborted(self):
        db = _db({"id": "tid"})
        prov = _provisioner(available=True, delete_result=True)
        with (
            patch.object(orch_main, "require_internal", AsyncMock()),
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main, "vm_provisioner", prov),
        ):
            out = await orch_main.agent_abort_thread_vm_upgrade(MagicMock(), "tid")

        prov.delete_thread_vm.assert_awaited_once_with("tid")
        db.merge_thread_vm_context.assert_awaited_once_with(
            "tid", {"status": "aborted"}
        )
        assert out == {"status": "aborted", "thread_id": "tid", "vm_deleted": True}

    @pytest.mark.asyncio
    async def test_provisioner_unavailable_still_marks_aborted(self):
        """No VM to delete, but the in-progress marker must still be cleared."""
        db = _db({"id": "tid"})
        prov = _provisioner(available=False)
        with (
            patch.object(orch_main, "require_internal", AsyncMock()),
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main, "vm_provisioner", prov),
        ):
            out = await orch_main.agent_abort_thread_vm_upgrade(MagicMock(), "tid")

        prov.delete_thread_vm.assert_not_called()
        db.merge_thread_vm_context.assert_awaited_once_with(
            "tid", {"status": "aborted"}
        )
        assert out["vm_deleted"] is False

    @pytest.mark.asyncio
    async def test_delete_exception_swallowed_still_marks_aborted(self):
        """A failed delete must not block clearing the retry guard."""
        db = _db({"id": "tid"})
        prov = _provisioner(available=True, delete_exc=RuntimeError("nats down"))
        with (
            patch.object(orch_main, "require_internal", AsyncMock()),
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main, "vm_provisioner", prov),
        ):
            out = await orch_main.agent_abort_thread_vm_upgrade(MagicMock(), "tid")

        db.merge_thread_vm_context.assert_awaited_once_with(
            "tid", {"status": "aborted"}
        )
        assert out["vm_deleted"] is False
