"""Tests for subjob workspace inheritance resolved live at dispatch.

Regression coverage for two coupled issues:

* docs/issues/subjob_inherits_stale_workspace_container_snapshot.md — a
  scholar/critic subjob copies the parent's ``workspace_container`` / ``vm``
  context by value at spawn time. When the scholar is spawned ~3s after its
  parent (before the parent pod is ready) that snapshot is frozen at
  ``status='created'`` with no SSH host and strands the subjob at
  ``init_workspace``. ``_resolve_subjob_inherited_workspace`` re-reads the
  parent's LIVE context at dispatch and overlays it.

* docs/issues/scholar_selfprovisioned_workspace_misclassified_as_inherited.md —
  when the parent has NO workspace at scholar-spawn (idle cluster), the scholar
  inherits nothing and self-provisions its OWN pod, writing its own
  ``workspace_container`` into context. The old resolver keyed the inherit path
  off mere *presence* of that key, so it misread the self-provisioned scholar as
  "inheriting", waited 600s on a parent workspace that never exists, then failed
  — and the dispatch-path failure never unblocked the parent, stranding it in
  ``waiting`` forever. The fix: inheriting subjobs carry an explicit
  ``inherits_parent_workspace`` flag (stamped only when they actually copy the
  parent's snapshot); the resolver gates on the flag, not on key presence; and a
  dispatch-time subjob failure routes through the parent-unblock handlers.

These tests exercise the real functions with a mocked ``postgres_db``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import tests.conftest  # noqa: F401 — applies license/crypto/env shims + sys.path
import orchestrator.main as main


READY_CONTAINER = {
    "status": "ready",
    "host": "workspace-parent.ns.svc.cluster.local",
    "pod_ip": "10.42.3.218",
    "port": 30022,
    "pod_name": "workspace-parent",
}
STALE_CONTAINER = {"status": "created", "pod_name": "workspace-parent"}
READY_VM = {"status": "ready", "ssh_host": "100.64.0.1", "pod_ip": "10.0.2.1"}


def _subjob(context, *, parent_id="parent-uuid", age_s=5.0):
    """Build a subjob row like get_dispatchable_jobs returns."""
    return {
        "id": "subjob-uuid",
        "parent_job_id": parent_id,
        "context": context,
        "created_at": datetime.now(timezone.utc) - timedelta(seconds=age_s),
    }


def _inherited(container=None, vm=None):
    """Context of a subjob that INHERITED its parent's workspace at spawn.

    Carries the explicit ``inherits_parent_workspace`` flag (stamped by
    ``_spawn_scholar_subjob`` / ``_trigger_verification_on_complete`` when they
    copy the parent's ``vm`` / ``workspace_container``) plus the copied snapshot.
    Contrast a *self-provisioned* subjob, whose context carries the same
    workspace key with NO flag.
    """
    ctx: dict = {"inherits_parent_workspace": True}
    if container is not None:
        ctx["workspace_container"] = container
    if vm is not None:
        ctx["vm"] = vm
    return ctx


@pytest.fixture
def patch_get_job(monkeypatch):
    """Patch main.postgres_db.get_job with an AsyncMock; return the mock."""

    def _apply(parent_row):
        mock = AsyncMock(return_value=parent_row)
        monkeypatch.setattr(main.postgres_db, "get_job", mock)
        return mock

    return _apply


class TestNonInheriting:
    """No-op paths: nothing to resolve."""

    @pytest.mark.asyncio
    async def test_no_parent_proceeds(self, patch_get_job):
        mock = patch_get_job(None)
        job = {"id": "j", "parent_job_id": None, "context": {}}
        assert await main._resolve_subjob_inherited_workspace(job) == ("proceed", None)
        mock.assert_not_awaited()  # never touches the DB

    @pytest.mark.asyncio
    async def test_subjob_without_inherited_keys_proceeds(self, patch_get_job):
        mock = patch_get_job(None)
        job = _subjob({"scholar_target": "parent-uuid"})
        assert await main._resolve_subjob_inherited_workspace(job) == ("proceed", None)
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_own_snapshot_already_ready_proceeds(self, patch_get_job):
        """A ready child snapshot is trusted only after parent liveness is rechecked."""
        mock = patch_get_job(
            {"status": "waiting", "context": {"workspace_container": READY_CONTAINER}}
        )
        job = _subjob(_inherited(container=dict(READY_CONTAINER)))
        assert await main._resolve_subjob_inherited_workspace(job) == ("proceed", None)
        mock.assert_awaited_once_with("parent-uuid")


class TestSelfProvisionedDiscrimination:
    """A subjob that self-provisioned its OWN workspace (no inherit flag) must
    ride the normal dispatch path — never wait on, or fail against, a parent
    workspace that will never exist.

    Regression for
    docs/issues/scholar_selfprovisioned_workspace_misclassified_as_inherited.md.
    The old resolver keyed off presence of ``workspace_container`` / ``vm`` and
    so misclassified these as inheriting.
    """

    @pytest.mark.asyncio
    async def test_self_provisioned_ready_container_proceeds_without_flag(
        self, patch_get_job
    ):
        # Scholar self-provisioned: it wrote its OWN ready container into context
        # but carries NO inherits_parent_workspace flag. Must proceed WITHOUT even
        # consulting the (workspace-less) parent.
        mock = patch_get_job(None)
        job = _subjob({"workspace_container": dict(READY_CONTAINER)})
        assert await main._resolve_subjob_inherited_workspace(job) == ("proceed", None)
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_provisioned_creating_container_proceeds_not_waits(
        self, patch_get_job
    ):
        # The exact shape that used to strand: a self-provisioned pod mid-creation
        # (status=creating) with no flag. Old code waited on the parent for 600s
        # then failed; new code proceeds immediately down the normal path.
        mock = patch_get_job({"status": "waiting", "context": {}})
        job = _subjob(
            {
                "workspace_container": {
                    "status": "creating",
                    "pod_name": "workspace-self",
                }
            }
        )
        assert await main._resolve_subjob_inherited_workspace(job) == ("proceed", None)
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_provisioned_vm_proceeds_without_flag(self, patch_get_job):
        mock = patch_get_job(None)
        job = _subjob({"vm": {"status": "creating", "requested": True}})
        assert await main._resolve_subjob_inherited_workspace(job) == ("proceed", None)
        mock.assert_not_awaited()


class TestContainerInheritance:
    @pytest.mark.asyncio
    async def test_stale_snapshot_overlays_parent_ready_container(self, patch_get_job):
        patch_get_job(
            {"status": "waiting", "context": {"workspace_container": READY_CONTAINER}}
        )
        job = _subjob(_inherited(container=dict(STALE_CONTAINER)))
        result = await main._resolve_subjob_inherited_workspace(job)
        assert result == ("proceed", None)
        # In-memory context now carries the parent's ready host/pod_ip.
        assert job["context"]["workspace_container"]["status"] == "ready"
        assert job["context"]["workspace_container"]["host"] == READY_CONTAINER["host"]

    @pytest.mark.asyncio
    async def test_parent_still_provisioning_waits(self, patch_get_job):
        patch_get_job(
            {
                "status": "waiting",
                "context": {"workspace_container": {"status": "creating"}},
            }
        )
        job = _subjob(_inherited(container=dict(STALE_CONTAINER)), age_s=5.0)
        assert await main._resolve_subjob_inherited_workspace(job) == ("wait", None)
        # Inherited snapshot left untouched while waiting.
        assert job["context"]["workspace_container"] == STALE_CONTAINER

    @pytest.mark.asyncio
    async def test_wait_times_out_to_fail(self, patch_get_job):
        patch_get_job(
            {
                "status": "waiting",
                "context": {"workspace_container": {"status": "creating"}},
            }
        )
        job = _subjob(
            _inherited(container=dict(STALE_CONTAINER)),
            age_s=main._INHERIT_WORKSPACE_MAX_WAIT_S + 60,
        )
        action, msg = await main._resolve_subjob_inherited_workspace(job)
        assert action == "fail"
        assert "Timed out" in msg

    @pytest.mark.asyncio
    async def test_parent_workspace_failed_fails_fast(self, patch_get_job):
        patch_get_job(
            {
                "status": "processing",
                "context": {"workspace_container": {"status": "failed"}},
            }
        )
        job = _subjob(_inherited(container=dict(STALE_CONTAINER)))
        action, msg = await main._resolve_subjob_inherited_workspace(job)
        assert action == "fail"
        assert "unavailable" in msg

    @pytest.mark.asyncio
    async def test_ready_snapshot_with_dead_parent_fails_fast(self, patch_get_job):
        patch_get_job(
            {
                "status": "failed",
                "context": {"workspace_container": {"status": "deleted"}},
            }
        )
        job = _subjob(_inherited(container=dict(READY_CONTAINER)))
        action, msg = await main._resolve_subjob_inherited_workspace(job)
        assert action == "fail"
        assert "unavailable" in msg

    @pytest.mark.asyncio
    async def test_parent_terminal_fails_fast(self, patch_get_job):
        patch_get_job(
            {
                "status": "failed",
                "context": {"workspace_container": {"status": "creating"}},
            }
        )
        job = _subjob(_inherited(container=dict(STALE_CONTAINER)))
        action, _ = await main._resolve_subjob_inherited_workspace(job)
        assert action == "fail"

    @pytest.mark.asyncio
    async def test_parent_missing_fails_fast(self, patch_get_job):
        patch_get_job(None)
        job = _subjob(_inherited(container=dict(STALE_CONTAINER)))
        action, msg = await main._resolve_subjob_inherited_workspace(job)
        assert action == "fail"
        assert "no longer exists" in msg

    @pytest.mark.asyncio
    async def test_context_as_json_string(self, patch_get_job):
        import json

        patch_get_job(
            {"status": "waiting", "context": {"workspace_container": READY_CONTAINER}}
        )
        job = _subjob(json.dumps(_inherited(container=dict(STALE_CONTAINER))))
        result = await main._resolve_subjob_inherited_workspace(job)
        assert result == ("proceed", None)
        assert job["context"]["workspace_container"]["status"] == "ready"


class TestVmInheritance:
    @pytest.mark.asyncio
    async def test_stale_vm_overlays_parent_ready_vm(self, patch_get_job):
        patch_get_job({"status": "waiting", "context": {"vm": READY_VM}})
        job = _subjob(_inherited(vm={"status": "creating", "requested": True}))
        result = await main._resolve_subjob_inherited_workspace(job)
        assert result == ("proceed", None)
        assert job["context"]["vm"]["status"] == "ready"
        assert job["context"]["vm"]["ssh_host"] == READY_VM["ssh_host"]

    @pytest.mark.asyncio
    async def test_vm_parent_not_ready_waits(self, patch_get_job):
        patch_get_job(
            {"status": "waiting", "context": {"vm": {"status": "provisioning"}}}
        )
        job = _subjob(
            _inherited(vm={"status": "creating", "requested": True}), age_s=5.0
        )
        assert await main._resolve_subjob_inherited_workspace(job) == ("wait", None)


class TestFailSubjobUnblocksParent:
    """A subjob failed at dispatch time (e.g. it cannot inherit its parent's
    workspace) must also unblock its parent, which _spawn_scholar_subjob left
    held in 'waiting'. Otherwise the parent strands forever — the secondary bug
    in scholar_selfprovisioned_workspace_misclassified_as_inherited.md.
    """

    @pytest.fixture
    def patch_db(self, monkeypatch):
        """Record status transitions + context merges; parent starts 'waiting'."""
        calls = {"status": [], "merge": []}

        async def fake_update_status(job_id, status=None, **kw):
            calls["status"].append((job_id, status, kw.get("error_message")))

        async def fake_merge(job_id, delta):
            calls["merge"].append((job_id, delta))

        parent_row = {"id": "parent-uuid", "status": "waiting", "context": {}}

        monkeypatch.setattr(
            main.postgres_db,
            "update_job_status",
            AsyncMock(side_effect=fake_update_status),
        )
        monkeypatch.setattr(
            main.postgres_db, "get_job", AsyncMock(return_value=parent_row)
        )
        monkeypatch.setattr(
            main.postgres_db, "merge_job_context", AsyncMock(side_effect=fake_merge)
        )
        monkeypatch.setattr(main, "_trigger_dispatch", lambda *a, **k: None)
        return calls

    @pytest.mark.asyncio
    async def test_failed_scholar_unblocks_waiting_parent(self, patch_db):
        job = _subjob({"scholar_target": "parent-uuid"})
        job["id"] = "scholar-uuid"
        job["status"] = "created"
        job["creation_order"] = None

        await main._fail_subjob_and_unblock_parent(job, "cannot inherit: boom")

        # 1. Scholar marked failed with the diagnostic message.
        assert ("scholar-uuid", "failed", "cannot inherit: boom") in patch_db["status"]
        # 2. Parent flipped waiting -> created (the unblock).
        assert ("parent-uuid", "created", None) in patch_db["status"]
        # 3. scholar_failed recorded on the parent context.
        assert ("parent-uuid", {"scholar_failed": True}) in patch_db["merge"]

    @pytest.mark.asyncio
    async def test_non_scholar_subjob_still_marked_failed(self, patch_db):
        # A subjob that isn't a scholar/delegation child (no scholar_target, no
        # creation_order) is still failed; there is simply no parent to unblock.
        job = _subjob({"verification_target": "parent-uuid"})
        job["id"] = "critic-uuid"
        job["status"] = "created"
        job["creation_order"] = None

        await main._fail_subjob_and_unblock_parent(job, "workspace gone")

        assert ("critic-uuid", "failed", "workspace gone") in patch_db["status"]
        # No unblock: parent was never transitioned to created.
        assert not any(s == "created" for _, s, _ in patch_db["status"])


# =============================================================================
# Dispatch backstop predicate — mirrors _dispatch_job_to_agent (main.py).
# A workspace-backed job with no injected SSH remote hard-fails the agent at
# init_workspace; the dispatcher refuses to POST it. Lite tiers are exempt.
# =============================================================================


def _dispatch_would_refuse(config_override: dict | None) -> bool:
    """Replica of the backstop condition in _dispatch_job_to_agent."""
    ws_final = (config_override or {}).get("workspace", {})
    backend_final = ws_final.get("backend")
    return backend_final not in main.LITE_BACKENDS and not ws_final.get("remote")


class TestDispatchBackstopPredicate:
    def test_refuses_sandbox_without_remote(self):
        assert _dispatch_would_refuse({"workspace": {"backend": "sandbox"}}) is True

    def test_refuses_vm_without_remote(self):
        assert _dispatch_would_refuse({"workspace": {"backend": "vm"}}) is True

    def test_refuses_no_config_at_all(self):
        # backend defaults to sandbox agent-side → still needs remote.
        assert _dispatch_would_refuse(None) is True
        assert _dispatch_would_refuse({}) is True

    def test_allows_sandbox_with_remote(self):
        co = {
            "workspace": {"backend": "sandbox", "remote": {"host": "h", "port": 30022}}
        }
        assert _dispatch_would_refuse(co) is False

    def test_allows_vm_with_remote(self):
        co = {"workspace": {"backend": "vm", "remote": {"host": "h"}}}
        assert _dispatch_would_refuse(co) is False

    def test_exempts_lite_backends(self):
        assert _dispatch_would_refuse({"workspace": {"backend": "virtual"}}) is False
        assert _dispatch_would_refuse({"workspace": {"backend": "none"}}) is False
