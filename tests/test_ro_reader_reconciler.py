"""Exact attempt and effect-horizon tests for the protected-reader sweep."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.cloud.protected_effect_contract import (
    NextcloudEffectCapability,
    NextcloudEffectFenceIntent,
    NextcloudEffectRequestAuthority,
    adopt_protected_effect_capability,
    sign_protected_effect_capability,
    sign_protected_effect_request,
)
from orchestrator.services.cloud.protected_reader_authority import (
    ProtectedNextcloudReaderGrantPlan,
)
from orchestrator.services.cloud_staging.source_identity import (
    ProtectedMountSourceIdentity,
)
from orchestrator.services.ro_reader_reconciler import reconcile_orphaned_ro_mounts


_THREAD = "11111111-1111-4111-8111-111111111111"
_THREAD_2 = "22222222-2222-4222-8222-222222222222"
_USER = "33333333-3333-4333-8333-333333333333"
_GENERATION = "44444444-4444-4444-8444-444444444444"
_INSTANCE = "55555555-5555-4555-8555-555555555555"
_MOUNT = "66666666-6666-4666-8666-666666666666"
_SOURCE_REF = "77777777-7777-4777-8777-777777777777"
_ATTEMPT = "88888888-8888-4888-8888-888888888888"
_CONFIG_SHA = "a" * 64
_KEY = b"k" * 32

_SOURCE = ProtectedMountSourceIdentity(
    backend_instance_id=_INSTANCE,
    source_ref=_SOURCE_REF,
    target_path="projects/example",
    native_id="17",
    mountpoint="Example",
)


def _plan(attempt: str = _ATTEMPT) -> ProtectedNextcloudReaderGrantPlan:
    return ProtectedNextcloudReaderGrantPlan(
        engage_attempt=attempt,
        backend_instance_id=_INSTANCE,
        source=_SOURCE,
    )


def _row(
    *,
    row_id: str = "99999999-9999-4999-8999-999999999999",
    thread_id: str = _THREAD,
    status: str = "active",
    attempt: str = _ATTEMPT,
) -> dict:
    plan = _plan(attempt)
    return {
        "id": row_id,
        "thread_id": thread_id,
        "user_id": _USER,
        "backend": "nextcloud",
        "backend_instance_id": _INSTANCE,
        "reader_id": plan.reader_id,
        "grant_group_id": plan.group_id,
        "grant_handle": plan.grant_handle,
        "grant_handle_sha256": plan.grant_handle_sha256,
        "status": status,
        "runtime_generation": _GENERATION,
        "engage_attempt": attempt,
        "source_binding": _SOURCE.binding,
        "source_binding_sha256": _SOURCE.sha256,
        "selected_mount_id": _MOUNT,
        "etag_baseline": {},
        "staged_epoch": 0,
        "staged_summary": None,
    }


def _thread(*, thread_id: str = _THREAD, status: str = "active") -> dict:
    return {
        "id": thread_id,
        "user_id": _USER,
        "status": status,
        "execution_lane": "pinned",
        "runtime_generation": _GENERATION,
        "runtime_retirement_token": None,
        "metadata": {"protected_cloud": True},
    }


def _mount() -> dict:
    return {
        "id": _MOUNT,
        "thread_id": _THREAD,
        "mount_kind": "project",
        "source_kind": "project_folder",
        "source_ref": _SOURCE_REF,
        "target_path": "projects/example",
        "backend_id": "nextcloud",
        "backend_instance_id": _INSTANCE,
        "cloud_handle": (
            '{"backend":"nextcloud","native_id":"17",'
            '"vendor_meta":{"mountpoint":"Example"}}'
        ),
    }


@asynccontextmanager
async def _lock(*_args, **_kwargs):
    yield True


def _db(*rows: dict) -> MagicMock:
    by_thread = {str(row["thread_id"]): row for row in rows}
    db = MagicMock()
    db.list_unclosed_cloud_ro_effect_intents = AsyncMock(return_value=[])
    db.list_unsettled_ro_mount_authorities = AsyncMock(return_value=list(rows))
    db.try_thread_advisory_lock = MagicMock(side_effect=_lock)
    db.get_ro_mount_by_thread = AsyncMock(
        side_effect=lambda thread_id: by_thread.get(str(thread_id))
    )
    db.get_thread = AsyncMock(return_value=None)
    db.list_thread_mounts = AsyncMock(return_value=[])
    db.begin_ro_mount_revocation_if_matches = AsyncMock(return_value=True)
    db.finish_ro_mount_revocation_if_matches = AsyncMock(return_value=True)
    return db


def _router_backend():
    backend = MagicMock()
    backend.protected_effect_hmac_key = _KEY
    backend.protected_effect_config_sha256 = _CONFIG_SHA
    backend.revoke_protected_reader_attempt = AsyncMock()
    router = MagicMock()
    router.for_backend_instance = MagicMock(return_value=backend)
    return router, backend


@pytest.mark.asyncio
async def test_reconciler_leaves_exact_live_active_attempt_alone():
    row = _row()
    db = _db(row)
    db.get_thread = AsyncMock(return_value=_thread())
    db.list_thread_mounts = AsyncMock(return_value=[_mount()])
    router, backend = _router_backend()

    assert await reconcile_orphaned_ro_mounts(postgres_db=db, router=router) == 0

    backend.revoke_protected_reader_attempt.assert_not_awaited()
    db.begin_ro_mount_revocation_if_matches.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_revokes_active_attempt_for_dead_thread():
    row = _row()
    db = _db(row)
    router, backend = _router_backend()

    assert await reconcile_orphaned_ro_mounts(postgres_db=db, router=router) == 1

    backend.revoke_protected_reader_attempt.assert_awaited_once_with(_plan())
    db.finish_ro_mount_revocation_if_matches.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciler_cleans_abandoned_engaging_attempt_even_when_thread_live():
    row = _row(status="engaging")
    db = _db(row)
    db.get_thread = AsyncMock(return_value=_thread())
    db.list_thread_mounts = AsyncMock(return_value=[_mount()])
    router, backend = _router_backend()

    assert await reconcile_orphaned_ro_mounts(postgres_db=db, router=router) == 1
    backend.revoke_protected_reader_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciler_keeps_revoking_until_effect_horizon_elapsed():
    row = _row(status="revoking")
    db = _db(row)
    db.finish_ro_mount_revocation_if_matches = AsyncMock(return_value=False)
    router, backend = _router_backend()

    assert await reconcile_orphaned_ro_mounts(postgres_db=db, router=router) == 0
    backend.revoke_protected_reader_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciler_continues_after_one_exact_revoke_failure():
    row1 = _row(
        row_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        thread_id=_THREAD,
        attempt="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    row2 = _row(
        row_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        thread_id=_THREAD_2,
        attempt="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    )
    db = _db(row1, row2)
    router, backend = _router_backend()
    backend.revoke_protected_reader_attempt = AsyncMock(
        side_effect=[RuntimeError("lost response"), None]
    )

    assert await reconcile_orphaned_ro_mounts(postgres_db=db, router=router) == 1
    assert backend.revoke_protected_reader_attempt.await_count == 2
    assert db.finish_ro_mount_revocation_if_matches.await_count == 1


def _effect_intent() -> NextcloudEffectFenceIntent:
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    capability = NextcloudEffectCapability(
        backend_instance_id=_INSTANCE,
        config_sha256=_CONFIG_SHA,
        queue_bound_seconds=5,
        handler_bound_seconds=7,
        clock_skew_bound_seconds=2,
        safety_margin_seconds=3,
        capability_max_age_seconds=10,
        server_time=now,
    )
    validated = adopt_protected_effect_capability(
        capability.binding,
        signature=sign_protected_effect_capability(capability, key=_KEY),
        key=_KEY,
        db_before=now - timedelta(seconds=1),
        db_after=now + timedelta(seconds=1),
        expected_backend_instance_id=_INSTANCE,
        expected_config_sha256=_CONFIG_SHA,
    )
    assert validated is not None
    request = NextcloudEffectRequestAuthority(
        backend_instance_id=_INSTANCE,
        config_sha256=_CONFIG_SHA,
        engage_attempt=_ATTEMPT,
        method="POST",
        path="/ocs/v1.php/cloud/groups",
        body_sha256="b" * 64,
        effect_not_after=now + timedelta(seconds=5),
    )
    return NextcloudEffectFenceIntent.capture(
        capability=validated,
        request=request,
        request_signature=sign_protected_effect_request(request, key=_KEY),
        key=_KEY,
        db_dispatched_at=now + timedelta(seconds=1),
    )


@pytest.mark.asyncio
async def test_reconciler_closes_restart_orphaned_signed_effect_intent():
    intent = _effect_intent()
    db = _db()
    db.list_unclosed_cloud_ro_effect_intents = AsyncMock(
        return_value=[
            {
                "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                "thread_id": _THREAD,
                "runtime_generation": _GENERATION,
                "engage_attempt": _ATTEMPT,
                "backend_instance_id": _INSTANCE,
                "backend_id": "nextcloud",
                "config_sha256": _CONFIG_SHA,
                "request_authority_sha256": intent.request.authority_sha256,
                "fence_intent": intent.binding,
            }
        ]
    )
    db.get_protected_effect_database_time = AsyncMock(
        return_value=intent.db_dispatched_at + timedelta(seconds=20)
    )
    db.close_cloud_ro_effect_intent = AsyncMock(return_value=True)
    router, _backend = _router_backend()

    assert await reconcile_orphaned_ro_mounts(postgres_db=db, router=router) == 0
    db.close_cloud_ro_effect_intent.assert_awaited_once()
    horizon = db.close_cloud_ro_effect_intent.await_args.kwargs["horizon"]
    assert horizon.intent.binding == intent.binding
    assert horizon.dispatch_closed_at > intent.db_dispatched_at
