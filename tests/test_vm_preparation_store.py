"""Storage retirement proves all identities before its first deletion."""

from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from vm_controller.preparation_store import (
    DISK_LABEL,
    PreparationConflict,
    PreparationStore,
)


@pytest.mark.asyncio
async def test_replaced_pvc_prevents_deletion_of_even_the_captured_datavolume():
    store = PreparationStore(Mock(), Mock(), "test", "local-path")
    store.unused = AsyncMock(return_value=True)
    store.dv = AsyncMock(
        return_value={
            "metadata": {"uid": "dv-original", "labels": {DISK_LABEL: "owner"}}
        }
    )
    store.pvc = AsyncMock(
        return_value=Obj(
            metadata=Obj(uid="pvc-replacement", labels={DISK_LABEL: "owner"})
        )
    )
    store.call = AsyncMock()
    with pytest.raises(PreparationConflict, match="PVC identity"):
        await store.delete_disk(
            "disk",
            owner_uid="owner",
            pvc_uid="pvc-original",
            dv_uid="dv-original",
            retire_import=True,
        )
    store.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_disk_consumer_blocks_failed_import_retirement():
    store = PreparationStore(Mock(), Mock(), "test", "local-path")
    store.unused = AsyncMock(return_value=False)
    store.call = AsyncMock()
    assert not await store.delete_disk(
        "disk", owner_uid="owner", pvc_uid="pvc", dv_uid="dv", retire_import=True
    )
    store.unused.assert_awaited_once_with("disk", ignore_cdi_owner="pvc")
    store.call.assert_not_awaited()
