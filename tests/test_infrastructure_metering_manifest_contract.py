"""Cross-layer vectors for the collector/app inventory manifest contract."""

from orchestrator.services.infrastructure_metering.collectors.kubernetes import (
    inventory_item_digest,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryItem,
    SanitizedInventoryError,
    inventory_manifest_digest,
)


def test_collector_and_app_store_share_the_v1_manifest_digest_vector():
    revision = "0" * 64
    rows = [("pod", "pod-a", revision), ("pod", "pod-z", "invalid")]
    items = [
        InventoryItem(
            source_kind="pod",
            source_uid="pod-a",
            revision_hash=revision,
            normalized_item={"uid": "pod-a"},
            valid_for_metering=True,
        ),
        InventoryItem(
            source_kind="pod",
            source_uid="pod-z",
            revision_hash=None,
            normalized_item={"uid": "pod-z", "valid_for_metering": False},
            valid_for_metering=False,
            item_error=SanitizedInventoryError("capacity-invalid"),
        ),
    ]
    expected = "e8fd7345c145ec4873f01e52a5955c2dde923bbe17ed5cc07c5d6ba6338d29ab"

    assert inventory_item_digest(rows) == expected
    assert inventory_manifest_digest(reversed(items)) == expected
