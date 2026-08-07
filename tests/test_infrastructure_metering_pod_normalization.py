from __future__ import annotations

from copy import deepcopy
import json
import re

import pytest

from orchestrator.services.infrastructure_metering.collectors.contracts import (
    normalized_payload,
)
from orchestrator.services.infrastructure_metering.collectors.pod_normalization import (
    POD_REQUESTS_ALGORITHM,
    PodEffectiveRequest,
    PodLifecycleState,
    PodNormalizationError,
    classify_pod_lifecycle,
    normalize_pod,
)
from orchestrator.services.infrastructure_metering.collectors.quantities import (
    QuantityNormalizationError,
    normalize_byte_quantity,
    normalize_cpu_millicores,
)


GIB = 1024**3
MIB = 1024**2


def _pod(
    *,
    containers: list[dict] | None = None,
    init_containers: list[dict] | None = None,
    pod_requests: dict | None = None,
    overhead: dict | None = None,
    phase: str = "Running",
    node_name: str | None = "node-a",
) -> dict:
    spec: dict = {
        "containers": containers
        if containers is not None
        else [
            {
                "name": "app",
                "resources": {"requests": {"cpu": "100m", "memory": "1Gi"}},
            }
        ]
    }
    if init_containers is not None:
        spec["initContainers"] = init_containers
    if pod_requests is not None:
        spec["resources"] = {"requests": pod_requests}
    if overhead is not None:
        spec["overhead"] = overhead
    if node_name is not None:
        spec["nodeName"] = node_name
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "pod-a",
            "namespace": "workers",
            "uid": "0a83bcf5-feb8-449d-ae26-a0418f445f3d",
            "resourceVersion": "17",
            "creationTimestamp": "2026-08-05T08:00:00Z",
        },
        "spec": spec,
        "status": {
            "phase": phase,
            "startTime": "2026-08-05T08:00:03Z",
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "True" if node_name else "False",
                    "lastTransitionTime": "2026-08-05T08:00:02Z",
                }
            ],
        },
    }


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [("100m", 100), ("0.1", 100), (".1m", 1), (0, 0)],
)
def test_cpu_quantities_normalize_upward_without_float_drift(
    quantity: object, expected: int
) -> None:
    result = normalize_cpu_millicores(quantity)

    assert result.normalized_value == expected
    assert result.normalized_unit == "millicore"


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [("1Gi", GIB), ("1G", 1_000_000_000), ("512Mi", 512 * MIB), ("0.1", 1)],
)
def test_byte_quantities_normalize_upward(quantity: object, expected: int) -> None:
    assert normalize_byte_quantity(quantity).normalized_value == expected


@pytest.mark.parametrize(
    "quantity",
    [True, 0.1, "-1m", "NaN", "Infinity", "wat", " 1Gi", "1Gi "],
)
def test_invalid_quantities_are_rejected_not_coerced_to_zero(quantity: object) -> None:
    with pytest.raises(QuantityNormalizationError):
        normalize_cpu_millicores(quantity)


def test_quantities_reject_values_that_cannot_fit_storage_bigint() -> None:
    with pytest.raises(QuantityNormalizationError, match="quantity-overflow"):
        normalize_byte_quantity(str(2**63))


def test_regular_containers_sum_and_overhead_is_added_once() -> None:
    raw = _pod(
        containers=[
            {
                "name": "app",
                "image": "registry.invalid/private:token",
                "resources": {
                    "requests": {"cpu": "250m", "memory": "1Gi"},
                    "limits": {"cpu": "99", "memory": "99Gi"},
                },
            },
            {
                "name": "sidecar",
                "resources": {"requests": {"cpu": "0.5", "memory": "500M"}},
            },
        ],
        overhead={"cpu": "25m", "memory": "10Mi"},
    )
    raw["spec"]["ephemeralContainers"] = [
        {
            "name": "debug",
            "resources": {"requests": {"cpu": "100", "memory": "100Gi"}},
        }
    ]

    result = normalize_pod(raw)

    assert result.valid_for_metering
    assert result.effective_request is not None
    assert result.effective_request.cpu_millicores == 775
    assert result.effective_request.memory_bytes == GIB + 500_000_000 + 10 * MIB
    assert result.effective_request.overhead_cpu_millicores == 25
    assert result.effective_request.measurement_algorithm == POD_REQUESTS_ALGORITHM


def test_serial_init_containers_use_per_resource_peak() -> None:
    result = normalize_pod(
        _pod(
            containers=[
                {
                    "name": "app",
                    "resources": {"requests": {"cpu": "200m", "memory": "200Mi"}},
                }
            ],
            init_containers=[
                {
                    "name": "cpu-init",
                    "resources": {"requests": {"cpu": "1", "memory": "100Mi"}},
                },
                {
                    "name": "memory-init",
                    "resources": {"requests": {"cpu": "500m", "memory": "2Gi"}},
                },
            ],
        )
    )

    assert result.effective_request is not None
    assert result.effective_request.cpu_millicores == 1000
    assert result.effective_request.memory_bytes == 2 * GIB


def test_restartable_init_sidecars_accumulate_in_declaration_order() -> None:
    result = normalize_pod(
        _pod(
            containers=[
                {
                    "name": "app",
                    "resources": {"requests": {"cpu": "100m", "memory": "200Mi"}},
                }
            ],
            init_containers=[
                {
                    "name": "sidecar-one",
                    "restartPolicy": "Always",
                    "resources": {"requests": {"cpu": "200m", "memory": "100Mi"}},
                },
                {
                    "name": "serial-init",
                    "resources": {"requests": {"cpu": "1", "memory": "50Mi"}},
                },
                {
                    "name": "sidecar-two",
                    "restartPolicy": "Always",
                    "resources": {"requests": {"cpu": "300m", "memory": "500Mi"}},
                },
                {
                    "name": "late-init",
                    "resources": {"requests": {"cpu": "100m", "memory": "1Gi"}},
                },
            ],
        )
    )

    assert result.effective_request is not None
    assert result.effective_request.cpu_millicores == 1200
    assert result.effective_request.memory_bytes == GIB + 600 * MIB


def test_pod_level_request_overrides_only_present_resource_then_adds_overhead() -> None:
    result = normalize_pod(
        _pod(
            pod_requests={"cpu": "2"},
            overhead={"cpu": "10m", "memory": "1Mi"},
        )
    )

    assert result.effective_request is not None
    assert result.effective_request.cpu_millicores == 2010
    assert result.effective_request.memory_bytes == GIB + MIB
    assert result.effective_request.cpu_source == "pod-level-spec"
    assert result.effective_request.memory_source == "container-derived-spec"


def test_missing_requests_are_scheduler_zero_with_coverage_diagnostics() -> None:
    result = normalize_pod(
        _pod(
            containers=[
                {
                    "name": "app",
                    "resources": {"limits": {"cpu": "8", "memory": "32Gi"}},
                }
            ],
            pod_requests={"memory": "4Gi"},
        )
    )

    assert result.valid_for_metering
    assert result.effective_request is not None
    assert result.effective_request.cpu_millicores == 0
    assert result.effective_request.memory_bytes == 4 * GIB
    missing = {
        item.resource: item.covered_by_pod_level
        for item in result.diagnostics
        if item.code == "missing-request"
    }
    assert missing == {"cpu": False, "memory": True}


def test_identifiable_invalid_capacity_remains_present_with_null_revision() -> None:
    raw = _pod()
    raw["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "-1"

    result = normalize_pod(raw)

    assert result.uid == raw["metadata"]["uid"]
    assert not result.valid_for_metering
    assert result.revision_hash is None
    assert result.effective_request is None
    assert [item.code for item in result.diagnostics] == ["negative-quantity"]
    assert result.diagnostics[0].original_value == "-1"


def test_missing_exact_scope_identity_is_fatal() -> None:
    raw = _pod()
    del raw["metadata"]["uid"]

    with pytest.raises(PodNormalizationError, match="metadata-uid"):
        normalize_pod(raw)


def test_projection_keeps_only_allowlisted_metadata_and_request_evidence() -> None:
    raw = _pod()
    raw["metadata"].update(
        {
            "annotations": {"private": "annotation-secret"},
            "labels": {
                "srw/job-id": "job-42",
                "srw.io/thread-id": "11111111-2222-3333-4444-555555555555",
                "srw/purpose": "session",
                "srw/managed-by": "agent-provisioner",
                "app.kubernetes.io/name": "worker",
                "private-token": "label-secret",
            },
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "worker-abc",
                    "uid": "owner-uid",
                    "controller": True,
                }
            ],
        }
    )
    raw["spec"]["containers"][0].update(
        {
            "image": "registry.invalid/image-secret",
            "command": ["do-not-copy-command"],
            "env": [
                {
                    "name": "TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {"name": "secret-name", "key": "token"}
                    },
                }
            ],
        }
    )

    result = normalize_pod(raw)
    projected = normalized_payload(result)
    encoded = json.dumps(projected, sort_keys=True)

    assert result.labels == {
        "app.kubernetes.io/name": "worker",
        "srw.io/thread-id": "11111111-2222-3333-4444-555555555555",
        "srw/job-id": "job-42",
        "srw/managed-by": "agent-provisioner",
        "srw/purpose": "session",
    }
    assert result.owner_references[0].to_dict() == {
        "kind": "ReplicaSet",
        "uid": "owner-uid",
        "name": "worker-abc",
    }
    for forbidden in (
        "annotation-secret",
        "label-secret",
        "image-secret",
        "do-not-copy-command",
        "secret-name",
        "secretKeyRef",
        '"metadata"',
        '"spec"',
    ):
        assert forbidden not in encoded


def test_unbounded_allowlisted_metadata_is_rejected_from_projection() -> None:
    raw = _pod()
    raw["metadata"]["labels"] = {"srw/job-id": "x" * 64}
    raw["metadata"]["ownerReferences"] = [
        {
            "kind": "ReplicaSet",
            "name": "worker",
            "uid": "x" * 257,
        }
    ]

    result = normalize_pod(raw)

    assert not result.valid_for_metering
    assert result.labels == {}
    assert result.owner_references == ()
    assert {item.path for item in result.diagnostics} == {
        "metadata.labels.srw/job-id",
        "metadata.ownerReferences[0]",
    }


def test_invalid_start_evidence_is_named_and_cannot_be_backdated() -> None:
    raw = _pod()
    raw["metadata"]["creationTimestamp"] = "not-a-timestamp"
    raw["status"]["conditions"][0]["lastTransitionTime"] = "also-invalid"

    result = normalize_pod(raw)

    assert result.valid_for_metering
    assert result.lifecycle.creation_timestamp is None
    assert result.lifecycle.pod_scheduled_transition_time is None
    assert {(item.code, item.path) for item in result.diagnostics} >= {
        ("invalid-timestamp", "metadata.creationTimestamp"),
        ("invalid-timestamp", "status.conditions[0].lastTransitionTime"),
    }


def test_parseable_start_evidence_before_creation_cannot_be_backdated() -> None:
    raw = _pod()
    raw["status"]["startTime"] = "2026-08-05T07:59:59Z"
    raw["status"]["conditions"][0]["lastTransitionTime"] = "2026-08-05T07:59:58Z"

    result = normalize_pod(raw)

    assert result.lifecycle.start_time is None
    assert result.lifecycle.pod_scheduled_transition_time is None
    assert {(item.code, item.path) for item in result.diagnostics} >= {
        ("unsafe-timestamp-order", "status.startTime"),
        (
            "unsafe-timestamp-order",
            "status.conditions.PodScheduled.lastTransitionTime",
        ),
    }


def test_revision_hash_ignores_transport_and_non_metering_churn() -> None:
    first_raw = _pod()
    first_raw["metadata"]["labels"] = {
        "srw/job-id": "job-42",
        "ignored": "one",
    }
    first = normalize_pod(first_raw)

    equivalent_raw = deepcopy(first_raw)
    equivalent_raw["metadata"]["resourceVersion"] = "999"
    equivalent_raw["metadata"]["deletionTimestamp"] = "2026-08-05T09:00:00Z"
    equivalent_raw["metadata"]["labels"]["ignored"] = "two"
    equivalent_raw["status"]["phase"] = "Unknown"
    equivalent_raw["status"]["startTime"] = "2026-08-05T08:00:04Z"
    equivalent_raw["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "0.1"
    equivalent = normalize_pod(equivalent_raw)

    assert first.revision_hash == equivalent.revision_hash
    assert re.fullmatch(r"[0-9a-f]{64}", first.revision_hash or "")

    changed_attribution = deepcopy(first_raw)
    changed_attribution["metadata"]["labels"]["srw/job-id"] = "job-43"
    assert normalize_pod(changed_attribution).revision_hash != first.revision_hash

    changed_capacity = deepcopy(first_raw)
    changed_capacity["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "101m"
    assert normalize_pod(changed_capacity).revision_hash != first.revision_hash


@pytest.mark.parametrize(
    ("phase", "node_name", "deleting", "state", "accrues"),
    [
        ("Pending", None, False, PodLifecycleState.UNSCHEDULED, False),
        ("Pending", "node-a", False, PodLifecycleState.ACTIVE, True),
        ("Running", "node-a", False, PodLifecycleState.ACTIVE, True),
        ("Unknown", "node-a", False, PodLifecycleState.ACTIVE, True),
        ("Running", "node-a", True, PodLifecycleState.ACTIVE, True),
        ("Succeeded", "node-a", False, PodLifecycleState.TERMINAL, False),
        ("Failed", "node-a", True, PodLifecycleState.TERMINAL, False),
    ],
)
def test_lifecycle_classification_matrix(
    phase: str,
    node_name: str | None,
    deleting: bool,
    state: PodLifecycleState,
    accrues: bool,
) -> None:
    raw = _pod(phase=phase, node_name=node_name)
    if deleting:
        raw["metadata"]["deletionTimestamp"] = "2026-08-05T09:00:00Z"

    lifecycle = classify_pod_lifecycle(raw)

    assert lifecycle.state is state
    assert lifecycle.accrues is accrues
    assert lifecycle.deletion_requested is deleting


def test_pod_scheduled_true_condition_is_sufficient_without_node_name() -> None:
    raw = _pod(phase="Pending", node_name=None)
    raw["status"]["conditions"][0]["status"] = "True"

    lifecycle = classify_pod_lifecycle(raw)

    assert lifecycle.scheduled
    assert lifecycle.accrues
    assert lifecycle.pod_scheduled_transition_time == ("2026-08-05T08:00:02.000000Z")


def _with_resize_status(
    raw: dict,
    *,
    actual: dict,
    allocated: dict,
    reason: str,
) -> dict:
    raw["status"]["containerStatuses"] = [
        {
            "name": "app",
            "resources": {"requests": actual},
            "allocatedResources": allocated,
        }
    ]
    raw["status"]["conditions"].append(
        {
            "type": "PodResizePending",
            "status": "True",
            "reason": reason,
        }
    )
    return raw


@pytest.mark.parametrize(
    ("desired", "actual", "allocated", "reason", "cpu", "memory"),
    [
        (
            {"cpu": "2", "memory": "2Gi"},
            {"cpu": "1", "memory": "1Gi"},
            {"cpu": "1500m", "memory": "1536Mi"},
            "Deferred",
            2000,
            2 * GIB,
        ),
        (
            {"cpu": "500m", "memory": "512Mi"},
            {"cpu": "1", "memory": "1Gi"},
            {"cpu": "750m", "memory": "768Mi"},
            "Deferred",
            1000,
            GIB,
        ),
        (
            {"cpu": "2", "memory": "2Gi"},
            {"cpu": "1", "memory": "1Gi"},
            {"cpu": "1500m", "memory": "1536Mi"},
            "Infeasible",
            1500,
            1536 * MIB,
        ),
    ],
)
def test_status_aware_resize_matches_upstream_max_semantics(
    desired: dict,
    actual: dict,
    allocated: dict,
    reason: str,
    cpu: int,
    memory: int,
) -> None:
    raw = _pod(containers=[{"name": "app", "resources": {"requests": desired}}])
    _with_resize_status(raw, actual=actual, allocated=allocated, reason=reason)

    result = normalize_pod(raw)

    assert result.valid_for_metering
    assert result.effective_request is not None
    assert result.effective_request.cpu_millicores == cpu
    assert result.effective_request.memory_bytes == memory
    assert result.effective_request.status_resources_used
    assert result.effective_request.resize_status == reason.lower()


def test_status_aware_pod_level_resize_preserves_per_resource_fallback() -> None:
    raw = _pod(pod_requests={"cpu": "2"})
    raw["status"]["containerStatuses"] = [
        {
            "name": "app",
            "resources": {"requests": {"cpu": "100m", "memory": "1Gi"}},
            "allocatedResources": {"cpu": "100m", "memory": "1Gi"},
        }
    ]
    raw["status"]["resources"] = {"requests": {"cpu": "1"}}
    raw["status"]["allocatedResources"] = {"cpu": "1500m"}
    raw["status"]["conditions"].append(
        {
            "type": "PodResizePending",
            "status": "True",
            "reason": "Deferred",
        }
    )

    result = normalize_pod(raw)

    assert result.effective_request is not None
    assert result.effective_request.cpu_millicores == 2000
    assert result.effective_request.memory_bytes == GIB
    assert result.effective_request.cpu_source == "pod-level-status-aware"
    assert result.effective_request.memory_source == ("container-derived-status-aware")


def test_status_aware_resize_applies_to_restartable_init_not_serial_init() -> None:
    raw = _pod(
        containers=[
            {
                "name": "app",
                "resources": {"requests": {"cpu": "100m", "memory": "100Mi"}},
            }
        ],
        init_containers=[
            {
                "name": "native-sidecar",
                "restartPolicy": "Always",
                "resources": {"requests": {"cpu": "500m", "memory": "500Mi"}},
            },
            {
                "name": "serial-init",
                "resources": {"requests": {"cpu": "200m", "memory": "200Mi"}},
            },
        ],
    )
    raw["status"]["containerStatuses"] = [
        {
            "name": "app",
            "resources": {"requests": {"cpu": "100m", "memory": "100Mi"}},
            "allocatedResources": {"cpu": "100m", "memory": "100Mi"},
        }
    ]
    raw["status"]["initContainerStatuses"] = [
        {
            "name": "native-sidecar",
            "resources": {"requests": {"cpu": "1", "memory": "1Gi"}},
            "allocatedResources": {"cpu": "750m", "memory": "750Mi"},
        },
        {
            "name": "serial-init",
            "resources": {"requests": {"cpu": "9", "memory": "9Gi"}},
            "allocatedResources": {"cpu": "9", "memory": "9Gi"},
        },
    ]
    raw["status"]["conditions"].append(
        {
            "type": "PodResizePending",
            "status": "True",
            "reason": "Deferred",
        }
    )

    result = normalize_pod(raw)

    assert result.effective_request is not None
    assert result.effective_request.cpu_millicores == 1200
    assert result.effective_request.memory_bytes == GIB + 200 * MIB


def _previous_request(cpu: int = 1000, memory: int = GIB) -> PodEffectiveRequest:
    return PodEffectiveRequest(
        cpu_millicores=cpu,
        memory_bytes=memory,
        cpu_source="container-derived-spec",
        memory_source="container-derived-spec",
        overhead_cpu_millicores=0,
        overhead_memory_bytes=0,
        capacity_quality="exact",
        resize_status="none",
        status_resources_used=False,
    )


def test_1000m_to_500m_resize_without_actuated_status_holds_previous_max() -> None:
    raw = _pod(
        containers=[
            {
                "name": "app",
                "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}},
            }
        ]
    )
    raw["status"]["conditions"].append(
        {
            "type": "PodResizePending",
            "status": "True",
            "reason": "Deferred",
        }
    )

    result = normalize_pod(raw, previous_request=_previous_request())

    assert result.valid_for_metering
    assert result.effective_request is not None
    assert result.effective_request.cpu_millicores == 1000
    assert result.effective_request.memory_bytes == GIB
    assert result.effective_request.capacity_quality == "resize-status-unavailable"
    assert "resize-status-unavailable" in {item.code for item in result.diagnostics}


def test_resize_without_actuated_status_or_previous_capacity_is_invalid() -> None:
    raw = _pod()
    raw["status"]["conditions"].append(
        {
            "type": "PodResizeInProgress",
            "status": "True",
        }
    )

    result = normalize_pod(raw)

    assert not result.valid_for_metering
    assert result.revision_hash is None
    assert result.effective_request is None
    assert "resize-status-unavailable" in {item.code for item in result.diagnostics}


def test_node_allocatable_dra_is_explicitly_unsupported_not_silently_zero() -> None:
    raw = _pod()
    raw["status"]["resourceClaimStatuses"] = [
        {"name": "claim", "resourceClaimName": "claim-a"}
    ]

    result = normalize_pod(raw)

    assert not result.valid_for_metering
    assert result.effective_request is None
    assert result.revision_hash is None
    assert "dra-capacity-unsupported" in {item.code for item in result.diagnostics}


def test_db_item_is_plain_json_with_deterministic_revision() -> None:
    result = normalize_pod(_pod())

    payload = normalized_payload(result)
    assert json.loads(json.dumps(payload)) == payload
    assert payload["revision_hash"] == result.revision_hash
    assert payload["valid_for_metering"] is True
    assert payload["capacity"]["cpu_millicores"] == 100
