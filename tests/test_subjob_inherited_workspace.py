"""Tests for subjob workspace inheritance resolved live at dispatch.

Regression coverage for
docs/issues/subjob_inherits_stale_workspace_container_snapshot.md: a scholar/
critic subjob copies the parent's ``workspace_container`` / ``vm`` context by
value at spawn time. When the scholar is spawned ~3s after its parent (before
the parent pod is ready) that snapshot is frozen at ``status='created'`` with
no SSH host and strands the subjob at ``init_workspace``.

``_resolve_subjob_inherited_workspace`` (orchestrator/main.py) re-reads the
parent's LIVE context at dispatch and overlays it, so the subjob rides the
parent pod instead of stranding. These tests exercise the real function with a
mocked ``postgres_db.get_job``.
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
        """Critic spawned after the parent pod went ready — snapshot is usable."""
        mock = patch_get_job(None)
        job = _subjob({"workspace_container": dict(READY_CONTAINER)})
        assert await main._resolve_subjob_inherited_workspace(job) == ("proceed", None)
        mock.assert_not_awaited()


class TestContainerInheritance:
    @pytest.mark.asyncio
    async def test_stale_snapshot_overlays_parent_ready_container(self, patch_get_job):
        patch_get_job(
            {"status": "waiting", "context": {"workspace_container": READY_CONTAINER}}
        )
        job = _subjob({"workspace_container": dict(STALE_CONTAINER)})
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
        job = _subjob({"workspace_container": dict(STALE_CONTAINER)}, age_s=5.0)
        assert await main._resolve_subjob_inherited_workspace(job) == ("wait", None)
        # Context left untouched while waiting.
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
            {"workspace_container": dict(STALE_CONTAINER)},
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
        job = _subjob({"workspace_container": dict(STALE_CONTAINER)})
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
        job = _subjob({"workspace_container": dict(STALE_CONTAINER)})
        action, _ = await main._resolve_subjob_inherited_workspace(job)
        assert action == "fail"

    @pytest.mark.asyncio
    async def test_parent_missing_fails_fast(self, patch_get_job):
        patch_get_job(None)
        job = _subjob({"workspace_container": dict(STALE_CONTAINER)})
        action, msg = await main._resolve_subjob_inherited_workspace(job)
        assert action == "fail"
        assert "no longer exists" in msg

    @pytest.mark.asyncio
    async def test_context_as_json_string(self, patch_get_job):
        import json

        patch_get_job(
            {"status": "waiting", "context": {"workspace_container": READY_CONTAINER}}
        )
        job = _subjob(json.dumps({"workspace_container": dict(STALE_CONTAINER)}))
        result = await main._resolve_subjob_inherited_workspace(job)
        assert result == ("proceed", None)
        assert job["context"]["workspace_container"]["status"] == "ready"


class TestVmInheritance:
    @pytest.mark.asyncio
    async def test_stale_vm_overlays_parent_ready_vm(self, patch_get_job):
        patch_get_job({"status": "waiting", "context": {"vm": READY_VM}})
        job = _subjob({"vm": {"status": "creating", "requested": True}})
        result = await main._resolve_subjob_inherited_workspace(job)
        assert result == ("proceed", None)
        assert job["context"]["vm"]["status"] == "ready"
        assert job["context"]["vm"]["ssh_host"] == READY_VM["ssh_host"]

    @pytest.mark.asyncio
    async def test_vm_parent_not_ready_waits(self, patch_get_job):
        patch_get_job(
            {"status": "waiting", "context": {"vm": {"status": "provisioning"}}}
        )
        job = _subjob({"vm": {"status": "creating", "requested": True}}, age_s=5.0)
        assert await main._resolve_subjob_inherited_workspace(job) == ("wait", None)


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
