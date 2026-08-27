from datetime import datetime, timezone
import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from orchestrator.services.infrastructure_metering.collectors.contracts import (
    WatchGapReason,
)

from orchestrator.services.infrastructure_metering.ingestion_types import (
    InventoryItemWire,
    InventorySnapshotBegin,
    InventorySnapshotFinalize,
    InventoryTicketRequest,
    InventoryWatchApply,
    InventoryWatchFinish,
    WatchObservationWire,
    validate_normalized_vmi_payload,
)


SCOPE = {
    "source_cluster": "dev-cluster",
    "api_resource": "core/v1/pods",
    "namespace": "srw",
    "cluster_scoped": False,
}
SNAPSHOT_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
NOW = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)


def _item(**changes):
    item = {
        "scope": SCOPE,
        "snapshot_id": SNAPSHOT_ID,
        "kind": "pod",
        "uid": "pod-uid",
        "revision_hash": "a" * 64,
        "valid_for_metering": True,
        "normalized": {"uid": "pod-uid", "capacity": {"cpu": 500}},
    }
    item.update(changes)
    return item


def test_ticket_intents_require_exact_snapshot_or_watch_shape():
    snapshot = InventoryTicketRequest(
        scope=SCOPE,
        intent="snapshot",
        snapshot_id=SNAPSHOT_ID,
    )
    assert snapshot.snapshot_id == SNAPSHOT_ID

    with pytest.raises(ValidationError):
        InventoryTicketRequest(
            scope=SCOPE,
            intent="watch-session",
            snapshot_id=SNAPSHOT_ID,
            starting_resource_version="rv-1",
        )


def test_vmi_payload_validator_accepts_only_consistent_admitted_capacity():
    payload = {
        "source_kind": "vmi",
        "api_version": "kubevirt.io/v1",
        "namespace": "srw-vms",
        "name": "vm-one",
        "uid": "vmi-uid-one",
        "resource_version": "17",
        "owner_hint": {"kind": "job", "owner_id": "job-one"},
        "vm_reference": {"uid": "vm-uid-one", "name": "vm-one"},
        "root_data_volume": {"name": "vm-one-rootdisk"},
        "lifecycle": {
            "state": "active",
            "scheduled": True,
            "terminal": False,
            "accrues": True,
            "phase": "Running",
            "node_name": "node-one",
            "paused": False,
            "migrating": False,
            "deletion_requested": False,
            "creation_timestamp": "2026-08-05T12:00:00.000000Z",
            "deletion_timestamp": None,
            "scheduled_transition_timestamp": "2026-08-05T12:00:01.000000Z",
            "terminal_transition_timestamp": None,
            "phase_transitions": [
                {"phase": "Scheduled", "timestamp": "2026-08-05T12:00:01.000000Z"}
            ],
        },
        "capacity": {
            "cpu_millicores": 8000,
            "memory_bytes": 16 * 1024**3,
            "cpu_topology": {
                "cores": 2,
                "sockets": 2,
                "threads": 2,
                "vcpus": 8,
            },
            "memory_evidence": {
                "original": "16Gi",
                "decimal_value": str(16 * 1024**3),
                "normalized_value": 16 * 1024**3,
                "normalized_unit": "byte",
            },
            "cpu_source": "vmi-admitted-topology",
            "memory_source": "vmi-admitted-guest-memory",
            "capacity_quality": "exact",
            "measurement_algorithm": "kubevirt-vmi-current-guest-v2",
        },
        "measurement_basis": "guest-provisioned",
        "measurement_algorithm": "kubevirt-vmi-current-guest-v2",
        "resource": "workspace_vm",
        "diagnostics": [],
        "valid_for_metering": True,
        "revision_hash": "a" * 64,
    }

    assert validate_normalized_vmi_payload(payload) is payload
    with pytest.raises(ValidationError, match="CPU capacity"):
        validate_normalized_vmi_payload(
            {**payload, "capacity": {**payload["capacity"], "cpu_millicores": 7000}}
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        validate_normalized_vmi_payload({**payload, "annotations": {}})
    with pytest.raises(ValidationError):
        InventoryTicketRequest(
            scope=SCOPE,
            intent="watch-session",
            starting_resource_version="0",
        )


def test_inventory_item_rejects_raw_or_secret_fields_recursively():
    with pytest.raises(ValidationError, match="forbidden"):
        InventoryItemWire.model_validate(
            _item(normalized={"capacity": {}, "nested": {"env": []}})
        )
    with pytest.raises(ValidationError, match="valid dictionary"):
        InventoryItemWire.model_validate(_item(normalized=None))
    with pytest.raises(ValidationError):
        InventoryItemWire.model_validate(
            _item(
                revision_hash=None,
                valid_for_metering=False,
                normalized=None,
            )
        )


def test_snapshot_has_no_received_clock_and_enforces_complete_shape():
    payload = {
        "ticket_id": UUID("bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
        "ticket_token": "t" * 32,
        "collector_id": "kubernetes-pods",
        **SCOPE,
        "collection_started_at": NOW,
        "collection_completed_at": NOW,
        "source_snapshot_at": None,
        "complete": True,
        "snapshot_id": SNAPSHOT_ID,
        "leader_generation": 2,
        "controller_epoch": None,
        "sequence": None,
        "resource_version": "rv-1",
        "item_count": 1,
        "item_digest": "a" * 64,
        "pages_read": 1,
        "bytes_read": 100,
        "items_streamed": True,
        "fatal_errors": [],
        "item_errors": [],
    }
    assert InventorySnapshotBegin.model_validate(payload).complete
    with pytest.raises(ValidationError, match="extra_forbidden"):
        InventorySnapshotBegin.model_validate({**payload, "received_at": NOW})
    with pytest.raises(ValidationError, match="authoritative metadata"):
        InventorySnapshotBegin.model_validate({**payload, "item_digest": None})
    with pytest.raises(ValidationError, match="metadata-only"):
        InventorySnapshotBegin.model_validate(
            {
                **payload,
                "complete": False,
                "resource_version": None,
                "item_digest": None,
                "item_count": 1,
                "fatal_errors": [
                    {
                        "error_class": "kubernetes-api",
                        "scope": SCOPE,
                        "message": "LIST failed",
                        "kind": None,
                        "uid": None,
                    }
                ],
            }
        )


def test_watch_bookmark_cannot_claim_presence_or_carry_an_item():
    with pytest.raises(ValidationError):
        WatchObservationWire(
            scope=SCOPE,
            event_type="BOOKMARK",
            resource_version="rv-2",
            source_event_bytes=128,
            collector_observed_at=NOW,
            confirms_presence=True,
            item=_item(snapshot_id=None),
        )


def test_snapshot_finalize_repeats_only_sealing_metadata_and_has_no_receipt_clock():
    payload = {
        "ticket_id": uuid4(),
        "ticket_token": "t" * 32,
        "snapshot_id": uuid4(),
        "scope": SCOPE,
        "shadow_enabled": True,
        "collection_completed_at": NOW,
        "source_snapshot_at": None,
        "complete": True,
        "resource_version": "rv-2",
        "item_count": 1,
        "item_digest": "a" * 64,
        "controller_epoch": None,
        "sequence": None,
        "fatal_errors": [],
    }
    assert InventorySnapshotFinalize.model_validate(payload).complete
    with pytest.raises(ValidationError, match="extra_forbidden"):
        InventorySnapshotFinalize.model_validate({**payload, "received_at": NOW})
    with pytest.raises(ValidationError, match="authoritative metadata"):
        InventorySnapshotFinalize.model_validate({**payload, "item_digest": None})
    with pytest.raises(ValidationError, match="metadata-only"):
        InventorySnapshotFinalize.model_validate(
            {
                **payload,
                "complete": False,
                "resource_version": None,
                "item_digest": None,
                "item_count": 1,
                "fatal_errors": [
                    {
                        "error_class": "kubernetes-api",
                        "scope": SCOPE,
                        "message": "LIST failed",
                        "kind": None,
                        "uid": None,
                    }
                ],
            }
        )


def test_watch_apply_requires_event_id_and_nonzero_prior_cursor():
    observation = {
        "scope": SCOPE,
        "event_type": "BOOKMARK",
        "resource_version": "rv-2",
        "source_event_bytes": 64,
        "collector_observed_at": NOW,
        "confirms_presence": False,
        "item": None,
    }
    payload = {
        "ticket_id": uuid4(),
        "ticket_token": "w" * 32,
        "leader_generation": 2,
        "event_id": uuid4(),
        "expected_resource_version": "rv-1",
        "observation": observation,
    }
    assert InventoryWatchApply.model_validate(payload).event_id
    with pytest.raises(ValidationError, match="resource version 0"):
        InventoryWatchApply.model_validate(
            {**payload, "expected_resource_version": "0"}
        )


def test_watch_history_loss_requires_stable_event_id():
    payload = {
        "ticket_id": uuid4(),
        "ticket_token": "w" * 32,
        "leader_generation": 2,
        "scope": SCOPE,
        "started_at": NOW,
        "completed_at": NOW,
        "starting_resource_version": "rv-1",
        "committed_resource_version": "rv-1",
        "processed_events": 0,
        "object_events": 0,
        "bookmarks": 0,
        "bytes_read": 64,
        "reconnect_required": True,
        "relist_required": True,
        "history_lost": True,
        "limit_reached": False,
        "gap_reason": WatchGapReason.RESOURCE_VERSION_EXPIRED,
        "ambiguous_resource_version": None,
        "history_event_id": uuid4(),
        "fatal_errors": [],
        "item_errors": [],
    }
    assert InventoryWatchFinish.model_validate(payload).history_lost
    with pytest.raises(ValidationError, match="idempotent event ID"):
        InventoryWatchFinish.model_validate({**payload, "history_event_id": None})

    with pytest.raises(ValidationError, match="typed gap reason"):
        InventoryWatchFinish.model_validate({**payload, "gap_reason": None})

    ambiguous = InventoryWatchFinish.model_validate(
        {
            **payload,
            "gap_reason": WatchGapReason.AMBIGUOUS_APPLY,
            "ambiguous_resource_version": "rv-attempted",
        }
    )
    assert ambiguous.ambiguous_resource_version == "rv-attempted"

    with pytest.raises(ValidationError, match="attempted resource version"):
        InventoryWatchFinish.model_validate(
            {
                **payload,
                "gap_reason": WatchGapReason.AMBIGUOUS_APPLY,
                "ambiguous_resource_version": None,
            }
        )
    with pytest.raises(ValidationError, match="attempted resource version"):
        InventoryWatchFinish.model_validate(
            {**payload, "ambiguous_resource_version": "rv-unexpected"}
        )
    with pytest.raises(ValidationError, match="resource version 0"):
        InventoryWatchFinish.model_validate(
            {
                **payload,
                "gap_reason": WatchGapReason.AMBIGUOUS_APPLY,
                "ambiguous_resource_version": "0",
            }
        )

    no_gap = {
        **payload,
        "history_lost": False,
        "relist_required": False,
        "gap_reason": None,
        "history_event_id": None,
    }
    assert not InventoryWatchFinish.model_validate(no_gap).history_lost
    with pytest.raises(ValidationError, match="typed gap reason"):
        InventoryWatchFinish.model_validate(
            {
                **no_gap,
                "gap_reason": WatchGapReason.QUEUE_OVERFLOW,
            }
        )

    json_payload = {
        **payload,
        "gap_reason": "resource-version-expired",
    }
    parsed = InventoryWatchFinish.model_validate_json(
        json.dumps(json_payload, default=str)
    )
    assert parsed.gap_reason == WatchGapReason.RESOURCE_VERSION_EXPIRED
    with pytest.raises(ValidationError):
        InventoryWatchFinish.model_validate_json(
            json.dumps({**json_payload, "gap_reason": "unknown-gap"}, default=str)
        )
