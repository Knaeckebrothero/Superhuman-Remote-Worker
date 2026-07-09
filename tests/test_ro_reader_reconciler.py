"""Tests for the orphaned-RO-reader reconciler (design §8.1.4).

Never trust revoke-on-teardown alone: a leader-gated sweep revokes any active
cloud_ro_mounts grant whose thread is gone or ended, and marks the row revoked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.ro_reader_reconciler import reconcile_orphaned_ro_mounts


@pytest.mark.asyncio
async def test_reconciler_revokes_grants_for_dead_threads():
    db = AsyncMock()
    db.list_active_ro_mounts = AsyncMock(return_value=[
        {"id": "r1", "thread_id": "alive", "backend": "nextcloud",
         "grant_handle": '{"group_id":"g1","reader_id":"srw-reader-a"}',
         "user_id": "u1"},
        {"id": "r2", "thread_id": "dead", "backend": "nextcloud",
         "grant_handle": '{"group_id":"g2","reader_id":"srw-reader-b"}',
         "user_id": "u2"},
    ])
    db.get_thread = AsyncMock(
        side_effect=lambda tid: {"id": tid, "status": "active"} if tid == "alive" else None
    )
    db.mark_ro_mount_revoked = AsyncMock(return_value=True)
    backend = AsyncMock()
    router = MagicMock()
    router.for_backend = MagicMock(return_value=backend)

    n = await reconcile_orphaned_ro_mounts(postgres_db=db, router=router)

    assert n == 1
    backend.revoke_ro_grant.assert_awaited_once()  # only the dead one
    db.mark_ro_mount_revoked.assert_awaited_once_with("r2")


@pytest.mark.asyncio
async def test_reconciler_revokes_grants_for_ended_threads():
    db = AsyncMock()
    db.list_active_ro_mounts = AsyncMock(return_value=[
        {"id": "r3", "thread_id": "ended", "backend": "nextcloud",
         "grant_handle": '{"group_id":"g","reader_id":"srw-reader-x"}', "user_id": "u"},
    ])
    db.get_thread = AsyncMock(return_value={"id": "ended", "status": "ended"})
    db.mark_ro_mount_revoked = AsyncMock(return_value=True)
    backend = AsyncMock()
    router = MagicMock(for_backend=MagicMock(return_value=backend))

    n = await reconcile_orphaned_ro_mounts(postgres_db=db, router=router)
    assert n == 1


@pytest.mark.asyncio
async def test_reconciler_leaves_live_grants_alone():
    db = AsyncMock()
    db.list_active_ro_mounts = AsyncMock(return_value=[
        {"id": "r1", "thread_id": "alive", "backend": "nextcloud",
         "grant_handle": '{"group_id":"g","reader_id":"srw-reader-a"}', "user_id": "u"},
    ])
    db.get_thread = AsyncMock(return_value={"id": "alive", "status": "active"})
    db.mark_ro_mount_revoked = AsyncMock()
    backend = AsyncMock()
    router = MagicMock(for_backend=MagicMock(return_value=backend))

    n = await reconcile_orphaned_ro_mounts(postgres_db=db, router=router)
    assert n == 0
    backend.revoke_ro_grant.assert_not_awaited()
    db.mark_ro_mount_revoked.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_continues_after_a_revoke_failure():
    db = AsyncMock()
    db.list_active_ro_mounts = AsyncMock(return_value=[
        {"id": "r1", "thread_id": "dead1", "backend": "nextcloud",
         "grant_handle": '{"group_id":"g1","reader_id":"srw-reader-a"}', "user_id": "u"},
        {"id": "r2", "thread_id": "dead2", "backend": "nextcloud",
         "grant_handle": '{"group_id":"g2","reader_id":"srw-reader-b"}', "user_id": "u"},
    ])
    db.get_thread = AsyncMock(return_value=None)  # both orphaned
    db.mark_ro_mount_revoked = AsyncMock(return_value=True)
    backend = AsyncMock()
    backend.revoke_ro_grant = AsyncMock(side_effect=[RuntimeError("boom"), None])
    router = MagicMock(for_backend=MagicMock(return_value=backend))

    n = await reconcile_orphaned_ro_mounts(postgres_db=db, router=router)
    # First revoke raised → not counted / not marked; second succeeded.
    assert n == 1
    db.mark_ro_mount_revoked.assert_awaited_once_with("r2")
