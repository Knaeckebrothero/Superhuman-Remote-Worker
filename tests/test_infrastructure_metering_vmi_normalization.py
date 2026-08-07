from __future__ import annotations

from copy import deepcopy
import json

import pytest

from orchestrator.services.infrastructure_metering.collectors.contracts import (
    normalized_payload,
)
from orchestrator.services.infrastructure_metering.collectors.vmi_normalization import (
    VMI_CAPACITY_ALGORITHM,
    VMILifecycleState,
    VMINormalizationError,
    normalize_virtual_machine_instance,
)


GIB = 1024**3


def _vmi() -> dict[str, object]:
    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachineInstance",
        "metadata": {
            "uid": "vmi-uid-one",
            "namespace": "agent-vms",
            "name": "agent-vm-job-1",
            "resourceVersion": "12345",
            "creationTimestamp": "2026-08-06T10:00:00Z",
            "labels": {
                "srw.io/owner-kind": "job",
                "srw.io/owner-id": "job-1",
                "private.example/customer": "must-not-leave",
            },
            "annotations": {"private.example/token": "secret-token"},
            "ownerReferences": [
                {
                    "apiVersion": "kubevirt.io/v1",
                    "kind": "VirtualMachine",
                    "name": "agent-vm-job-1",
                    "uid": "vm-uid-one",
                }
            ],
        },
        "spec": {
            "domain": {
                "cpu": {"cores": 2, "sockets": 2, "threads": 2},
                "memory": {"guest": "16Gi"},
                "devices": {"interfaces": [{"name": "default", "model": "virtio"}]},
            },
            "volumes": [
                {
                    "name": "rootdisk",
                    "dataVolume": {"name": "agent-vm-job-1-rootdisk"},
                },
                {
                    "name": "cloud-init",
                    "cloudInitNoCloud": {"userData": "private cloud init"},
                },
            ],
        },
        "status": {
            "phase": "Running",
            "nodeName": "worker-a",
            "phaseTransitionTimestamps": [
                {
                    "phase": "Scheduling",
                    "phaseTransitionTimestamp": "2026-08-06T10:00:01Z",
                },
                {
                    "phase": "Scheduled",
                    "phaseTransitionTimestamp": "2026-08-06T10:00:02Z",
                },
                {
                    "phase": "Running",
                    "phaseTransitionTimestamp": "2026-08-06T10:00:03Z",
                },
            ],
            "interfaces": [{"ipAddress": "10.0.0.12"}],
            "launcherContainerImageVersion": "private.registry/virt-launcher:tag",
        },
    }


def test_projects_admitted_capacity_lifecycle_and_only_safe_links() -> None:
    result = normalize_virtual_machine_instance(_vmi())
    projected = normalized_payload(result)

    assert result.uid == "vmi-uid-one"
    assert result.resource_version == "12345"
    assert result.valid_for_metering
    assert result.revision_hash is not None
    assert result.admitted_capacity is not None
    assert result.admitted_capacity.cpu_topology.vcpus == 8
    assert result.admitted_capacity.cpu_millicores == 8000
    assert result.admitted_capacity.memory_bytes == 16 * GIB
    assert result.admitted_capacity.cpu_source == "vmi-admitted-topology"
    assert result.admitted_capacity.memory_source == "vmi-admitted-guest-memory"
    assert result.lifecycle.state is VMILifecycleState.ACTIVE
    assert result.lifecycle.scheduled
    assert result.lifecycle.accrues
    assert not result.lifecycle.terminal
    assert result.lifecycle.scheduled_transition_timestamp == (
        "2026-08-06T10:00:02.000000Z"
    )
    assert result.owner_hint is not None
    assert result.owner_hint.to_dict() == {"kind": "job", "owner_id": "job-1"}
    assert result.vm_reference is not None
    assert result.vm_reference.to_dict() == {
        "uid": "vm-uid-one",
        "name": "agent-vm-job-1",
    }
    assert result.root_data_volume is not None
    assert result.root_data_volume.name == "agent-vm-job-1-rootdisk"
    assert projected["measurement_basis"] == "guest-provisioned"
    assert projected["measurement_algorithm"] == VMI_CAPACITY_ALGORITHM
    assert projected["resource"] == "workspace_vm"

    encoded = json.dumps(projected, sort_keys=True)
    for forbidden in (
        "must-not-leave",
        "secret-token",
        "private cloud init",
        "10.0.0.12",
        "private.registry",
        "annotations",
        "interfaces",
        "virt-launcher",
    ):
        assert forbidden not in encoded


def test_paused_and_live_migrating_vmi_remains_active() -> None:
    raw = _vmi()
    raw["status"] = {
        **raw["status"],  # type: ignore[arg-type]
        "conditions": [{"type": "Paused", "status": "True"}],
        "migrationState": {
            "migrationUid": "migration-private-id",
            "sourceNode": "worker-a",
            "targetNode": "worker-b",
            "completed": False,
            "failed": False,
        },
    }

    result = normalize_virtual_machine_instance(raw)

    assert result.lifecycle.paused
    assert result.lifecycle.migrating
    assert result.lifecycle.state is VMILifecycleState.ACTIVE
    assert result.lifecycle.accrues
    assert "migration-private-id" not in json.dumps(normalized_payload(result))


def test_hotplug_migration_prefers_current_status_over_desired_spec() -> None:
    raw = _vmi()
    raw["spec"] = deepcopy(raw["spec"])
    # Desired values have already moved to the hotplug target.
    raw["spec"]["domain"]["cpu"] = {  # type: ignore[index]
        "cores": 2,
        "sockets": 4,
        "threads": 2,
    }
    raw["spec"]["domain"]["memory"] = {"guest": "32Gi"}  # type: ignore[index]
    # KubeVirt documents these status fields as the capacity actually running
    # while the migration/hotplug operation is still in progress.
    raw["status"] = {
        **raw["status"],  # type: ignore[arg-type]
        "currentCPUTopology": {"cores": 2, "sockets": 2, "threads": 2},
        "memory": {
            "guestAtBoot": "16Gi",
            "guestCurrent": "16Gi",
            "guestRequested": "32Gi",
        },
        "migrationState": {"completed": False, "failed": False},
    }

    result = normalize_virtual_machine_instance(raw)

    assert result.valid_for_metering
    assert result.lifecycle.migrating
    assert result.admitted_capacity is not None
    assert result.admitted_capacity.cpu_millicores == 8000
    assert result.admitted_capacity.memory_bytes == 16 * GIB
    assert result.admitted_capacity.cpu_source == "vmi-status-current-topology"
    assert result.admitted_capacity.memory_source == "vmi-status-guest-current"
    assert result.admitted_capacity.measurement_algorithm == VMI_CAPACITY_ALGORITHM


def test_live_shape_defaults_omitted_kubevirt_topology_axes() -> None:
    raw = _vmi()
    raw["metadata"] = deepcopy(raw["metadata"])
    raw["metadata"]["labels"] = {}  # type: ignore[index]
    raw["spec"] = deepcopy(raw["spec"])
    # KubeVirt 1.8 serializes its uint32 topology fields with omitempty. A live
    # admitted 8-vCPU VMI can therefore carry only `cores: 8` in both sources.
    raw["spec"]["domain"]["cpu"] = {  # type: ignore[index]
        "cores": 8,
        "model": "host-model",
    }
    raw["spec"]["domain"]["memory"] = {  # type: ignore[index]
        "guest": "16Gi",
        "maxGuest": "64Gi",
    }
    raw["status"] = {
        **raw["status"],  # type: ignore[arg-type]
        "currentCPUTopology": {"cores": 8},
        "memory": {
            "guestAtBoot": "16Gi",
            "guestCurrent": "16Gi",
            "guestRequested": "16Gi",
        },
    }

    result = normalize_virtual_machine_instance(raw)

    assert result.valid_for_metering
    assert result.admitted_capacity is not None
    assert result.admitted_capacity.cpu_topology.to_dict() == {
        "cores": 8,
        "sockets": 1,
        "threads": 1,
        "vcpus": 8,
    }
    assert result.admitted_capacity.cpu_millicores == 8000
    assert result.admitted_capacity.memory_bytes == 16 * GIB
    assert result.admitted_capacity.cpu_source == "vmi-status-current-topology"
    assert result.admitted_capacity.memory_source == "vmi-status-guest-current"

    admitted_only = deepcopy(raw)
    admitted_only["status"].pop("currentCPUTopology")  # type: ignore[union-attr]
    admitted_only["status"].pop("memory")  # type: ignore[union-attr]
    admitted_result = normalize_virtual_machine_instance(admitted_only)

    assert admitted_result.valid_for_metering
    assert admitted_result.admitted_capacity is not None
    assert admitted_result.admitted_capacity.cpu_topology.to_dict() == {
        "cores": 8,
        "sockets": 1,
        "threads": 1,
        "vcpus": 8,
    }
    assert admitted_result.admitted_capacity.cpu_source == "vmi-admitted-topology"
    assert admitted_result.admitted_capacity.memory_source == (
        "vmi-admitted-guest-memory"
    )


def test_topology_without_any_serialized_axis_fails_closed() -> None:
    raw = _vmi()
    raw["spec"] = deepcopy(raw["spec"])
    raw["spec"]["domain"]["cpu"] = {"model": "host-model"}  # type: ignore[index]

    result = normalize_virtual_machine_instance(raw)

    assert not result.valid_for_metering
    assert result.admitted_capacity is None
    assert ("invalid-vcpu-topology", "spec.domain.cpu") in {
        (diagnostic.code, diagnostic.path) for diagnostic in result.diagnostics
    }


def test_completed_hotplug_changes_revision_to_new_current_capacity() -> None:
    migrating = _vmi()
    migrating["status"] = {
        **migrating["status"],  # type: ignore[arg-type]
        "currentCPUTopology": {"cores": 2, "sockets": 2, "threads": 2},
        "memory": {"guestCurrent": "16Gi"},
    }
    completed = deepcopy(migrating)
    completed["status"]["currentCPUTopology"]["sockets"] = 4  # type: ignore[index]
    completed["status"]["memory"]["guestCurrent"] = "24Gi"  # type: ignore[index]

    before = normalize_virtual_machine_instance(migrating)
    after = normalize_virtual_machine_instance(completed)

    assert after.admitted_capacity is not None
    assert after.admitted_capacity.cpu_millicores == 16000
    assert after.admitted_capacity.memory_bytes == 24 * GIB
    assert before.revision_hash != after.revision_hash


@pytest.mark.parametrize(
    ("status_update", "expected_path"),
    [
        (
            {"currentCPUTopology": {"cores": 2, "sockets": 4, "threads": 0}},
            "status.currentCPUTopology.threads",
        ),
        ({"memory": {"guestCurrent": "invalid"}}, "status.memory.guestCurrent"),
        ({"memory": None}, "status.memory"),
    ],
)
def test_present_invalid_current_capacity_rejects_instead_of_falling_back(
    status_update: dict[str, object], expected_path: str
) -> None:
    raw = _vmi()
    raw["status"] = {**raw["status"], **status_update}  # type: ignore[arg-type]

    result = normalize_virtual_machine_instance(raw)

    assert not result.valid_for_metering
    assert result.admitted_capacity is None
    assert expected_path in {item.path for item in result.diagnostics}


def test_partial_old_status_without_current_fields_uses_admitted_fallback() -> None:
    raw = _vmi()
    raw["status"] = {
        **raw["status"],  # type: ignore[arg-type]
        "memory": {"guestAtBoot": "16Gi", "guestRequested": "32Gi"},
    }

    result = normalize_virtual_machine_instance(raw)

    assert result.admitted_capacity is not None
    assert result.admitted_capacity.cpu_source == "vmi-admitted-topology"
    assert result.admitted_capacity.memory_source == "vmi-admitted-guest-memory"
    assert result.admitted_capacity.memory_bytes == 16 * GIB


@pytest.mark.parametrize("phase", ["Succeeded", "Failed"])
def test_terminal_vmi_stops_accrual_even_when_node_name_remains(phase: str) -> None:
    raw = _vmi()
    raw["status"] = {
        **raw["status"],  # type: ignore[arg-type]
        "phase": phase,
        "phaseTransitionTimestamps": [
            {
                "phase": "Scheduled",
                "phaseTransitionTimestamp": "2026-08-06T10:00:02Z",
            },
            {
                "phase": phase,
                "phaseTransitionTimestamp": "2026-08-06T11:00:00Z",
            },
        ],
    }

    result = normalize_virtual_machine_instance(raw)

    assert result.lifecycle.state is VMILifecycleState.TERMINAL
    assert result.lifecycle.terminal
    assert not result.lifecycle.accrues
    assert result.lifecycle.terminal_transition_timestamp == (
        "2026-08-06T11:00:00.000000Z"
    )


def test_admitted_but_unscheduled_vmi_is_meterable_without_accruing_yet() -> None:
    raw = _vmi()
    raw["status"] = {"phase": "Pending", "phaseTransitionTimestamps": []}

    result = normalize_virtual_machine_instance(raw)

    assert result.valid_for_metering
    assert result.lifecycle.state is VMILifecycleState.UNSCHEDULED
    assert not result.lifecycle.scheduled
    assert not result.lifecycle.accrues


@pytest.mark.parametrize(
    ("cpu", "memory", "expected_code"),
    [
        (
            {"cores": 2, "sockets": 2, "threads": 0},
            "16Gi",
            "invalid-vcpu-topology",
        ),
        (
            {"cores": 2, "sockets": 2, "threads": 2},
            "-1Gi",
            "negative-quantity",
        ),
        ({"cores": 2, "sockets": 2, "threads": 2}, "0", "zero-capacity"),
    ],
)
def test_invalid_capacity_remains_identifiable_but_not_meterable(
    cpu: dict[str, int], memory: str, expected_code: str
) -> None:
    raw = _vmi()
    raw["spec"] = deepcopy(raw["spec"])
    raw["spec"]["domain"]["cpu"] = cpu  # type: ignore[index]
    raw["spec"]["domain"]["memory"] = {"guest": memory}  # type: ignore[index]

    result = normalize_virtual_machine_instance(raw)

    assert result.uid == "vmi-uid-one"
    assert result.namespace == "agent-vms"
    assert not result.valid_for_metering
    assert result.admitted_capacity is None
    assert result.revision_hash is None
    assert expected_code in {item.code for item in result.diagnostics}


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda raw: raw.update(kind="Pod"), "not-a-virtualmachineinstance"),
        (
            lambda raw: raw.update(apiVersion="kubevirt.io/v1alpha3"),
            "unsupported-vmi-api-version",
        ),
        (
            lambda raw: raw["metadata"].pop("uid"),  # type: ignore[union-attr]
            "invalid-metadata-uid",
        ),
        (
            lambda raw: raw["metadata"].pop("resourceVersion"),  # type: ignore[union-attr]
            "invalid-metadata-resourceVersion",
        ),
    ],
)
def test_strict_identity_rejects_untrustworthy_objects(mutation, code: str) -> None:
    raw = _vmi()
    mutation(raw)

    with pytest.raises(VMINormalizationError) as exc:
        normalize_virtual_machine_instance(raw)

    assert exc.value.code == code


def test_invalid_owner_hint_becomes_unattributed_without_losing_capacity() -> None:
    raw = _vmi()
    raw["metadata"] = deepcopy(raw["metadata"])
    raw["metadata"]["labels"] = {  # type: ignore[index]
        "srw.io/owner-kind": "customer",
        "srw.io/owner-id": "somebody-else",
    }

    result = normalize_virtual_machine_instance(raw)

    assert result.valid_for_metering
    assert result.owner_hint is None
    assert [(item.code, item.path) for item in result.diagnostics] == [
        ("invalid-owner-hint", "metadata.labels.srw.io/owner-kind")
    ]


def test_ambiguous_data_volumes_do_not_guess_a_root_disk() -> None:
    raw = _vmi()
    raw["spec"] = deepcopy(raw["spec"])
    raw["spec"]["volumes"] = [  # type: ignore[index]
        {"name": "disk-a", "dataVolume": {"name": "dv-a"}},
        {"name": "disk-b", "dataVolume": {"name": "dv-b"}},
    ]

    result = normalize_virtual_machine_instance(raw)

    assert result.root_data_volume is None
    assert "ambiguous-root-data-volume" in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_single_non_root_data_volume_is_not_guessed_as_root() -> None:
    raw = _vmi()
    raw["spec"] = deepcopy(raw["spec"])
    raw["spec"]["volumes"] = [  # type: ignore[index]
        {"name": "data", "dataVolume": {"name": "customer-data"}}
    ]

    result = normalize_virtual_machine_instance(raw)

    assert result.root_data_volume is None
    assert "ambiguous-root-data-volume" in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_revision_ignores_transport_and_private_status_churn_but_splits_capacity() -> (
    None
):
    original = normalize_virtual_machine_instance(_vmi())

    churned_raw = _vmi()
    churned_raw["metadata"] = {
        **churned_raw["metadata"],  # type: ignore[arg-type]
        "resourceVersion": "99999",
    }
    churned_raw["status"] = {
        **churned_raw["status"],  # type: ignore[arg-type]
        "guestOSInfo": {"prettyName": "private guest image"},
    }
    churned = normalize_virtual_machine_instance(churned_raw)

    resized_raw = _vmi()
    resized_raw["spec"] = deepcopy(resized_raw["spec"])
    resized_raw["spec"]["domain"]["cpu"]["sockets"] = 4  # type: ignore[index]
    resized = normalize_virtual_machine_instance(resized_raw)

    assert churned.revision_hash == original.revision_hash
    assert resized.revision_hash != original.revision_hash


def test_phase_timestamp_before_creation_is_sanitized_out() -> None:
    raw = _vmi()
    raw["status"] = {
        **raw["status"],  # type: ignore[arg-type]
        "phaseTransitionTimestamps": [
            {
                "phase": "Scheduled",
                "phaseTransitionTimestamp": "2026-08-06T09:59:59Z",
            }
        ],
    }

    result = normalize_virtual_machine_instance(raw)

    assert result.lifecycle.phase_transitions == ()
    assert result.lifecycle.scheduled_transition_timestamp is None
    assert "unsafe-timestamp-order" in {
        diagnostic.code for diagnostic in result.diagnostics
    }
