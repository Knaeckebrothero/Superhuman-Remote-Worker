"""Credential delivery and default-off behavior for background cloud work."""

from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from orchestrator.services import cloud_push_recovery as recovery


class Database:
    def __init__(self):
        self.thread_id = uuid4()
        self.thread = {
            "id": self.thread_id,
            "execution_lane": "stateless",
            "status": "active",
            "metadata": {
                "_workspace_binding": {"generation": "workspace-1", "kind": "virtual"}
            },
        }
        self.session_state = "done"

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield

    @asynccontextmanager
    async def thread_datasource_lock(self, _thread):
        yield

    async def get_thread(self, _thread):
        return deepcopy(self.thread)

    async def fetchval(self, _sql, *_args):
        return self.session_state

    async def fetchrow(self, sql, *_args):
        return (
            deepcopy(self.thread)
            if "FROM threads" in sql
            else {"state": self.session_state}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "race", [None, "lease", "ended", "workspace", "retirement", "input", "acknowledged"]
)
async def test_credentials_are_revalidated_after_slow_assembly(monkeypatch, race):
    monkeypatch.setenv("STATELESS_CLOUD_PUSH_RECOVERY_ENABLED", "true")
    db = Database()
    task = {
        "thread_id": db.thread_id,
        "task_kind": "cloud_push",
        "detail": {"workspace_generation": "workspace-1"},
    }
    loader = AsyncMock(side_effect=[task, None if race == "lease" else task])
    monkeypatch.setattr(recovery, "load_bg_task", loader)
    monkeypatch.setattr(
        recovery,
        "task_is_pending",
        AsyncMock(side_effect=[True, race != "acknowledged"]),
    )

    async def resolve(_thread):
        if race == "ended":
            db.thread["status"] = "ended"
        elif race == "retirement":
            db.thread["metadata"]["_stateless_claim_retirement"] = {}
        elif race == "workspace":
            db.thread["metadata"]["_workspace_binding"]["generation"] = "workspace-2"
        elif race == "input":
            db.session_state = "queued"
        return {
            "workspace": {"backend": "virtual", "workspace_generation": "workspace-1"},
            "cloud_sync": {"auth": "test-only-cloud-credential"},
            "model_api_key": "must-not-leave-assembly",
            "datasources": ["must-not-leave-assembly"],
        }

    call = recovery.build_cloud_push_bundle(
        db,
        unit_id=str(uuid4()),
        lease_token=1,
        pod_name="pod",
        pod_uid="uid",
        resolve_workspace=resolve,
    )
    if race in {"lease", "ended", "workspace", "retirement"}:
        with pytest.raises(recovery.CloudPushBundleRefused):
            await call
    else:
        body = await call
        assert "model_api_key" not in body and "datasources" not in body
        if race:
            assert "workspace" not in body and "cloud_sync" not in body
            assert body["deferred" if race == "input" else "obsolete"] is True
        else:
            assert body["cloud_sync"]["auth"] == "test-only-cloud-credential"
            assert body["thread_id"] == str(db.thread_id)


@pytest.mark.asyncio
async def test_flag_off_sweep_and_bundle_do_not_touch_database(monkeypatch):
    monkeypatch.delenv("STATELESS_CLOUD_PUSH_RECOVERY_ENABLED", raising=False)
    assert await recovery.sweep_stale_cloud_pushes(object()) == 0
    unit_id = str(uuid4())
    resolver = AsyncMock()
    body = await recovery.build_cloud_push_bundle(
        object(),
        unit_id=unit_id,
        lease_token=1,
        pod_name="pod",
        pod_uid="uid",
        resolve_workspace=resolver,
    )
    assert body == {"unit_id": unit_id, "unit_kind": "bg_task", "deferred": True}
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_input_defers_before_any_credential_resolution(monkeypatch):
    monkeypatch.setenv("STATELESS_CLOUD_PUSH_RECOVERY_ENABLED", "true")
    db = Database()
    db.session_state = "queued"
    monkeypatch.setattr(
        recovery,
        "load_bg_task",
        AsyncMock(return_value={"thread_id": db.thread_id, "task_kind": "cloud_push"}),
    )
    monkeypatch.setattr(recovery, "task_is_pending", AsyncMock(return_value=True))
    resolver = AsyncMock()
    body = await recovery.build_cloud_push_bundle(
        db,
        unit_id=str(uuid4()),
        lease_token=1,
        pod_name="pod",
        pod_uid="uid",
        resolve_workspace=resolver,
    )
    assert body["deferred"]
    resolver.assert_not_awaited()
