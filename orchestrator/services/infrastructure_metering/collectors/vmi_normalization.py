"""Pure, raw-free normalization of admitted KubeVirt VMIs.

The VirtualMachineInstance UID is the compute lifecycle identity.  VM names and
create-time defaults are deliberately not used as capacity authority.  For a
running VMI, KubeVirt's current CPU topology and guest-current memory win over
the admitted spec while a hotplug migration is in flight.  Older KubeVirt
versions that omit those status fields fall back to the admitted VMI spec; a
present-but-invalid current value fails closed instead of undercounting.  Only
a small accounting allowlist leaves this module; raw annotations, devices,
cloud-init, images, interfaces, and status messages do not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Literal

from .quantities import (
    SIGNED_BIGINT_MAX,
    NormalizedQuantity,
    QuantityNormalizationError,
    normalize_byte_quantity,
)


VMI_CAPACITY_ALGORITHM = "kubevirt-vmi-current-guest-v2"
VMI_API_VERSION = "kubevirt.io/v1"
VMI_KIND = "VirtualMachineInstance"

_TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})
_OWNER_KINDS = frozenset({"job", "thread"})


class VMINormalizationError(ValueError):
    """Fatal VMI identity/shape error with a stable, raw-free code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VMILifecycleState(StrEnum):
    UNSCHEDULED = "unscheduled"
    ACTIVE = "active"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class VMIDiagnostic:
    """Sanitized evidence safe for durable inventory storage."""

    code: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path}


@dataclass(frozen=True, slots=True)
class VMIOwnerHint:
    """Untrusted Kubernetes hint; the application DB remains authoritative."""

    kind: Literal["job", "thread"]
    owner_id: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "owner_id": self.owner_id}


@dataclass(frozen=True, slots=True)
class VMReference:
    """The controlling VirtualMachine identity, when KubeVirt supplies it."""

    uid: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"uid": self.uid, "name": self.name}


@dataclass(frozen=True, slots=True)
class RootDataVolumeReference:
    """Provisioning link only; a DataVolume is never a second disk asset."""

    name: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name}


@dataclass(frozen=True, slots=True)
class VMIPhaseTransition:
    phase: str
    timestamp: str

    def to_dict(self) -> dict[str, str]:
        return {"phase": self.phase, "timestamp": self.timestamp}


@dataclass(frozen=True, slots=True)
class VMILifecycle:
    """KubeVirt-visible lifecycle evidence; inventory owns disappearance."""

    state: VMILifecycleState
    scheduled: bool
    terminal: bool
    accrues: bool
    phase: str | None
    node_name: str | None
    paused: bool
    migrating: bool
    deletion_requested: bool
    creation_timestamp: str | None
    deletion_timestamp: str | None
    scheduled_transition_timestamp: str | None
    terminal_transition_timestamp: str | None
    phase_transitions: tuple[VMIPhaseTransition, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "scheduled": self.scheduled,
            "terminal": self.terminal,
            "accrues": self.accrues,
            "phase": self.phase,
            "node_name": self.node_name,
            "paused": self.paused,
            "migrating": self.migrating,
            "deletion_requested": self.deletion_requested,
            "creation_timestamp": self.creation_timestamp,
            "deletion_timestamp": self.deletion_timestamp,
            "scheduled_transition_timestamp": self.scheduled_transition_timestamp,
            "terminal_transition_timestamp": self.terminal_transition_timestamp,
            "phase_transitions": [item.to_dict() for item in self.phase_transitions],
        }


@dataclass(frozen=True, slots=True)
class VMICpuTopology:
    cores: int
    sockets: int
    threads: int
    vcpus: int

    def to_dict(self) -> dict[str, int]:
        return {
            "cores": self.cores,
            "sockets": self.sockets,
            "threads": self.threads,
            "vcpus": self.vcpus,
        }


@dataclass(frozen=True, slots=True)
class VMIAdmittedCapacity:
    """Exact current/admitted guest capacity ready for time integration."""

    cpu_millicores: int
    memory_bytes: int
    cpu_topology: VMICpuTopology
    memory_evidence: NormalizedQuantity
    cpu_source: Literal["vmi-status-current-topology", "vmi-admitted-topology"] = (
        "vmi-admitted-topology"
    )
    memory_source: Literal["vmi-status-guest-current", "vmi-admitted-guest-memory"] = (
        "vmi-admitted-guest-memory"
    )
    capacity_quality: Literal["exact"] = "exact"
    measurement_algorithm: str = VMI_CAPACITY_ALGORITHM

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_millicores": self.cpu_millicores,
            "memory_bytes": self.memory_bytes,
            "cpu_topology": self.cpu_topology.to_dict(),
            "memory_evidence": self.memory_evidence.to_dict(),
            "cpu_source": self.cpu_source,
            "memory_source": self.memory_source,
            "capacity_quality": self.capacity_quality,
            "measurement_algorithm": self.measurement_algorithm,
        }


@dataclass(frozen=True, slots=True)
class NormalizedVirtualMachineInstance:
    """Allowlisted VMI observation suitable for bounded inventory staging."""

    uid: str
    namespace: str
    name: str
    resource_version: str
    api_version: str
    owner_hint: VMIOwnerHint | None
    vm_reference: VMReference | None
    root_data_volume: RootDataVolumeReference | None
    lifecycle: VMILifecycle
    admitted_capacity: VMIAdmittedCapacity | None
    diagnostics: tuple[VMIDiagnostic, ...]
    valid_for_metering: bool
    kind: Literal["vmi"] = "vmi"
    revision_hash: str | None = field(init=False)

    def __post_init__(self) -> None:
        revision_hash = None
        if self.valid_for_metering:
            if self.admitted_capacity is None:
                raise ValueError("a meterable VMI requires admitted capacity")
            encoded = json.dumps(
                self.revision_payload(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            revision_hash = hashlib.sha256(encoded).hexdigest()
        object.__setattr__(self, "revision_hash", revision_hash)

    def revision_payload(self) -> dict[str, Any]:
        """Return metering-significant state without transport-only churn."""

        return {
            "kind": self.kind,
            "api_version": self.api_version,
            "identity": {
                "uid": self.uid,
                "namespace": self.namespace,
                "name": self.name,
                "creation_timestamp": self.lifecycle.creation_timestamp,
            },
            "attribution": {
                "owner_hint": self.owner_hint.to_dict() if self.owner_hint else None,
                "vm_reference": (
                    self.vm_reference.to_dict() if self.vm_reference else None
                ),
                "root_data_volume": (
                    self.root_data_volume.to_dict() if self.root_data_volume else None
                ),
            },
            "lifecycle": {
                "scheduled": self.lifecycle.scheduled,
                "terminal": self.lifecycle.terminal,
                "phase": self.lifecycle.phase,
                "node_name": self.lifecycle.node_name,
                "paused": self.lifecycle.paused,
                "migrating": self.lifecycle.migrating,
                "scheduled_transition_timestamp": (
                    self.lifecycle.scheduled_transition_timestamp
                ),
                "terminal_transition_timestamp": (
                    self.lifecycle.terminal_transition_timestamp
                ),
            },
            "capacity": (
                self.admitted_capacity.to_dict()
                if self.admitted_capacity is not None
                else None
            ),
            "diagnostics": sorted(
                (item.to_dict() for item in self.diagnostics),
                key=lambda item: (item["code"], item["path"]),
            ),
        }

    def to_db_item(self) -> dict[str, Any]:
        """Return an explicit raw-object-free inventory projection."""

        return {
            "source_kind": self.kind,
            "api_version": self.api_version,
            "namespace": self.namespace,
            "name": self.name,
            "uid": self.uid,
            "resource_version": self.resource_version,
            "owner_hint": self.owner_hint.to_dict() if self.owner_hint else None,
            "vm_reference": (
                self.vm_reference.to_dict() if self.vm_reference else None
            ),
            "root_data_volume": (
                self.root_data_volume.to_dict() if self.root_data_volume else None
            ),
            "lifecycle": self.lifecycle.to_dict(),
            "capacity": (
                self.admitted_capacity.to_dict()
                if self.admitted_capacity is not None
                else None
            ),
            "measurement_basis": "guest-provisioned",
            "measurement_algorithm": VMI_CAPACITY_ALGORITHM,
            "resource": "workspace_vm",
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "valid_for_metering": self.valid_for_metering,
            "revision_hash": self.revision_hash,
        }


# Short name for callers that mirror NormalizedPod/NormalizedPVC.
NormalizedVMI = NormalizedVirtualMachineInstance


def _required_identity_text(value: Any, path: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(character.isspace() for character in value)
    ):
        code = path.replace(".", "-").replace("_", "-")
        raise VMINormalizationError(f"invalid-{code}")
    return value


def _safe_optional_identifier(
    value: Any,
    *,
    path: str,
    diagnostics: list[VMIDiagnostic],
    maximum: int = 253,
) -> str | None:
    if value in (None, ""):
        return None
    if (
        isinstance(value, str)
        and value == value.strip()
        and len(value) <= maximum
        and not any(character.isspace() for character in value)
    ):
        return value
    diagnostics.append(VMIDiagnostic("invalid-lifecycle-field", path))
    return None


def _canonical_timestamp(
    value: Any,
    *,
    path: str,
    diagnostics: list[VMIDiagnostic],
) -> str | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise TypeError
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return (
            parsed.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, TypeError, ValueError):
        diagnostics.append(VMIDiagnostic("invalid-timestamp", path))
        return None


def _owner_hint(
    metadata: Mapping[str, Any], diagnostics: list[VMIDiagnostic]
) -> VMIOwnerHint | None:
    labels_value = metadata.get("labels", {})
    if labels_value is None:
        labels_value = {}
    if not isinstance(labels_value, Mapping):
        diagnostics.append(VMIDiagnostic("invalid-metadata-field", "metadata.labels"))
        return None
    owner_kind = labels_value.get("srw.io/owner-kind")
    owner_id = labels_value.get("srw.io/owner-id")
    if owner_kind in (None, "") and owner_id in (None, ""):
        return None
    if owner_kind not in _OWNER_KINDS:
        diagnostics.append(
            VMIDiagnostic("invalid-owner-hint", "metadata.labels.srw.io/owner-kind")
        )
        return None
    if not (
        isinstance(owner_id, str)
        and owner_id
        and owner_id == owner_id.strip()
        and len(owner_id) <= 63
        and not any(character.isspace() for character in owner_id)
    ):
        diagnostics.append(
            VMIDiagnostic("invalid-owner-hint", "metadata.labels.srw.io/owner-id")
        )
        return None
    return VMIOwnerHint(kind=owner_kind, owner_id=owner_id)


def _vm_reference(
    metadata: Mapping[str, Any], diagnostics: list[VMIDiagnostic]
) -> VMReference | None:
    values = metadata.get("ownerReferences", [])
    if values is None:
        values = []
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        diagnostics.append(
            VMIDiagnostic("invalid-metadata-field", "metadata.ownerReferences")
        )
        return None
    references: list[VMReference] = []
    for index, value in enumerate(values[:64]):
        if not isinstance(value, Mapping) or value.get("kind") != "VirtualMachine":
            continue
        path = f"metadata.ownerReferences[{index}]"
        try:
            references.append(
                VMReference(
                    uid=_required_identity_text(
                        value.get("uid"), f"{path}.uid", maximum=256
                    ),
                    name=_required_identity_text(
                        value.get("name"), f"{path}.name", maximum=253
                    ),
                )
            )
        except VMINormalizationError:
            diagnostics.append(VMIDiagnostic("invalid-vm-reference", path))
    if len(values) > 64:
        diagnostics.append(
            VMIDiagnostic("metadata-field-limit", "metadata.ownerReferences")
        )
    unique = {(item.uid, item.name): item for item in references}
    if len(unique) > 1:
        diagnostics.append(
            VMIDiagnostic("ambiguous-vm-reference", "metadata.ownerReferences")
        )
        return None
    return next(iter(unique.values()), None)


def _phase_transitions(
    status: Mapping[str, Any],
    *,
    creation_timestamp: str | None,
    diagnostics: list[VMIDiagnostic],
) -> tuple[VMIPhaseTransition, ...]:
    values = status.get("phaseTransitionTimestamps", [])
    if values is None:
        values = []
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        diagnostics.append(
            VMIDiagnostic("invalid-lifecycle-field", "status.phaseTransitionTimestamps")
        )
        return ()
    transitions: dict[tuple[str, str], VMIPhaseTransition] = {}
    for index, value in enumerate(values[:128]):
        path = f"status.phaseTransitionTimestamps[{index}]"
        if not isinstance(value, Mapping):
            diagnostics.append(VMIDiagnostic("invalid-lifecycle-field", path))
            continue
        phase = _safe_optional_identifier(
            value.get("phase"),
            path=f"{path}.phase",
            diagnostics=diagnostics,
            maximum=64,
        )
        timestamp = _canonical_timestamp(
            value.get("phaseTransitionTimestamp"),
            path=f"{path}.phaseTransitionTimestamp",
            diagnostics=diagnostics,
        )
        if phase is None or timestamp is None:
            continue
        if creation_timestamp is not None and timestamp < creation_timestamp:
            diagnostics.append(
                VMIDiagnostic(
                    "unsafe-timestamp-order", f"{path}.phaseTransitionTimestamp"
                )
            )
            continue
        transitions[(timestamp, phase)] = VMIPhaseTransition(phase, timestamp)
    if len(values) > 128:
        diagnostics.append(
            VMIDiagnostic("field-limit", "status.phaseTransitionTimestamps")
        )
    return tuple(transitions[key] for key in sorted(transitions))


def _paused(status: Mapping[str, Any], diagnostics: list[VMIDiagnostic]) -> bool:
    values = status.get("conditions", [])
    if values is None:
        values = []
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        diagnostics.append(
            VMIDiagnostic("invalid-lifecycle-field", "status.conditions")
        )
        return False
    paused = False
    for index, value in enumerate(values[:256]):
        if not isinstance(value, Mapping):
            continue
        if value.get("type") != "Paused":
            continue
        condition_status = value.get("status")
        if condition_status not in {"True", "False", "Unknown"}:
            diagnostics.append(
                VMIDiagnostic(
                    "invalid-lifecycle-field", f"status.conditions[{index}].status"
                )
            )
            continue
        paused = paused or condition_status == "True"
    return paused


def _migrating(status: Mapping[str, Any], diagnostics: list[VMIDiagnostic]) -> bool:
    value = status.get("migrationState")
    if value in (None, {}):
        return False
    if not isinstance(value, Mapping):
        diagnostics.append(
            VMIDiagnostic("invalid-lifecycle-field", "status.migrationState")
        )
        return False
    completed = value.get("completed", False)
    failed = value.get("failed", False)
    if not isinstance(completed, bool) or not isinstance(failed, bool):
        diagnostics.append(
            VMIDiagnostic("invalid-lifecycle-field", "status.migrationState")
        )
        return False
    return not completed and not failed


def _lifecycle(
    raw: Mapping[str, Any], diagnostics: list[VMIDiagnostic]
) -> VMILifecycle:
    metadata_value = raw.get("metadata")
    assert isinstance(metadata_value, Mapping)
    status_value = raw.get("status", {})
    if not isinstance(status_value, Mapping):
        diagnostics.append(VMIDiagnostic("invalid-object-shape", "status"))
        status: Mapping[str, Any] = {}
    else:
        status = status_value

    phase = _safe_optional_identifier(
        status.get("phase"), path="status.phase", diagnostics=diagnostics, maximum=64
    )
    node_name = _safe_optional_identifier(
        status.get("nodeName"),
        path="status.nodeName",
        diagnostics=diagnostics,
        maximum=253,
    )
    creation_timestamp = _canonical_timestamp(
        metadata_value.get("creationTimestamp"),
        path="metadata.creationTimestamp",
        diagnostics=diagnostics,
    )
    deletion_timestamp = _canonical_timestamp(
        metadata_value.get("deletionTimestamp"),
        path="metadata.deletionTimestamp",
        diagnostics=diagnostics,
    )
    if (
        creation_timestamp is not None
        and deletion_timestamp is not None
        and deletion_timestamp < creation_timestamp
    ):
        diagnostics.append(
            VMIDiagnostic("unsafe-timestamp-order", "metadata.deletionTimestamp")
        )
        deletion_timestamp = None

    transitions = _phase_transitions(
        status,
        creation_timestamp=creation_timestamp,
        diagnostics=diagnostics,
    )
    scheduled_times = [
        item.timestamp for item in transitions if item.phase == "Scheduled"
    ]
    terminal_times = [
        item.timestamp for item in transitions if item.phase in _TERMINAL_PHASES
    ]
    scheduled = node_name is not None or phase in {"Scheduled", "Running"}
    terminal = phase in _TERMINAL_PHASES
    if terminal:
        state = VMILifecycleState.TERMINAL
    elif scheduled:
        state = VMILifecycleState.ACTIVE
    else:
        state = VMILifecycleState.UNSCHEDULED
    return VMILifecycle(
        state=state,
        scheduled=scheduled,
        terminal=terminal,
        accrues=scheduled and not terminal,
        phase=phase,
        node_name=node_name,
        paused=_paused(status, diagnostics),
        migrating=_migrating(status, diagnostics),
        deletion_requested=metadata_value.get("deletionTimestamp") not in (None, ""),
        creation_timestamp=creation_timestamp,
        deletion_timestamp=deletion_timestamp,
        scheduled_transition_timestamp=min(scheduled_times)
        if scheduled_times
        else None,
        terminal_transition_timestamp=max(terminal_times) if terminal_times else None,
        phase_transitions=transitions,
    )


def _positive_topology_value(
    value: Any, *, path: str, diagnostics: list[VMIDiagnostic]
) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        diagnostics.append(VMIDiagnostic("invalid-vcpu-topology", path))
        return None
    return value


def _topology(
    value: Any,
    *,
    path: str,
    diagnostics: list[VMIDiagnostic],
) -> VMICpuTopology | None:
    if not isinstance(value, Mapping):
        diagnostics.append(VMIDiagnostic("invalid-object-shape", path))
        return None

    # KubeVirt's CPUTopology axes are uint32 fields serialized with omitempty.
    # Its vCPU helper multiplies only the non-zero axes, so an admitted object
    # such as {"cores": 8} means 8 * 1 * 1 rather than incomplete evidence.
    # Require at least one serialized axis, default only omitted axes to the
    # multiplicative identity, and keep an explicitly present invalid value
    # fail-closed. CurrentCPUTopology uses the same upstream type/semantics.
    axes = ("cores", "sockets", "threads")
    if not any(axis in value for axis in axes):
        diagnostics.append(VMIDiagnostic("invalid-vcpu-topology", path))
        return None

    def topology_axis(axis: str) -> int | None:
        if axis not in value:
            return 1
        return _positive_topology_value(
            value[axis], path=f"{path}.{axis}", diagnostics=diagnostics
        )

    cores = topology_axis("cores")
    sockets = topology_axis("sockets")
    threads = topology_axis("threads")
    if cores is None or sockets is None or threads is None:
        return None
    vcpus = cores * sockets * threads
    if vcpus > SIGNED_BIGINT_MAX // 1000:
        diagnostics.append(VMIDiagnostic("quantity-overflow", path))
        return None
    return VMICpuTopology(
        cores=cores,
        sockets=sockets,
        threads=threads,
        vcpus=vcpus,
    )


def _guest_memory(
    value: Any,
    *,
    path: str,
    diagnostics: list[VMIDiagnostic],
) -> NormalizedQuantity | None:
    try:
        parsed_memory = normalize_byte_quantity(value)
        if parsed_memory.normalized_value <= 0:
            diagnostics.append(VMIDiagnostic("zero-capacity", path))
            return None
        return parsed_memory
    except QuantityNormalizationError as exc:
        diagnostics.append(VMIDiagnostic(exc.code, path))
        return None


def _capacity(
    raw: Mapping[str, Any], diagnostics: list[VMIDiagnostic]
) -> VMIAdmittedCapacity | None:
    spec = raw.get("spec")
    if not isinstance(spec, Mapping):
        diagnostics.append(VMIDiagnostic("invalid-object-shape", "spec"))
        return None
    domain = spec.get("domain")
    if not isinstance(domain, Mapping):
        diagnostics.append(VMIDiagnostic("invalid-object-shape", "spec.domain"))
        return None
    status_value = raw.get("status", {})
    status = status_value if isinstance(status_value, Mapping) else {}

    # KubeVirt documents currentCPUTopology as the topology actually used by
    # the workload while spec.domain.cpu can already hold the hotplug target.
    # Presence is meaningful: malformed current evidence must not fall back to
    # a possibly larger/smaller desired spec and silently mis-meter the VM.
    if "currentCPUTopology" in status:
        cpu_topology = _topology(
            status.get("currentCPUTopology"),
            path="status.currentCPUTopology",
            diagnostics=diagnostics,
        )
        cpu_source: Literal["vmi-status-current-topology", "vmi-admitted-topology"] = (
            "vmi-status-current-topology"
        )
    else:
        cpu_topology = _topology(
            domain.get("cpu"), path="spec.domain.cpu", diagnostics=diagnostics
        )
        cpu_source = "vmi-admitted-topology"

    # guestCurrent is likewise the memory currently available to the guest;
    # guestRequested/spec.guest may be the still-migrating target.  A status
    # memory object without guestCurrent is compatible with older/partial
    # KubeVirt status and therefore uses the admitted spec fallback.
    status_memory_value = status.get("memory")
    if "memory" in status and not isinstance(status_memory_value, Mapping):
        diagnostics.append(VMIDiagnostic("invalid-object-shape", "status.memory"))
        memory_evidence = None
        memory_source: Literal[
            "vmi-status-guest-current", "vmi-admitted-guest-memory"
        ] = "vmi-status-guest-current"
    elif (
        isinstance(status_memory_value, Mapping)
        and "guestCurrent" in status_memory_value
    ):
        memory_evidence = _guest_memory(
            status_memory_value.get("guestCurrent"),
            path="status.memory.guestCurrent",
            diagnostics=diagnostics,
        )
        memory_source = "vmi-status-guest-current"
    else:
        memory_value = domain.get("memory")
        if not isinstance(memory_value, Mapping):
            diagnostics.append(
                VMIDiagnostic("invalid-object-shape", "spec.domain.memory")
            )
            memory_evidence = None
        else:
            memory_evidence = _guest_memory(
                memory_value.get("guest"),
                path="spec.domain.memory.guest",
                diagnostics=diagnostics,
            )
        memory_source = "vmi-admitted-guest-memory"

    if cpu_topology is None or memory_evidence is None:
        return None
    return VMIAdmittedCapacity(
        cpu_millicores=cpu_topology.vcpus * 1000,
        memory_bytes=memory_evidence.normalized_value,
        cpu_topology=cpu_topology,
        memory_evidence=memory_evidence,
        cpu_source=cpu_source,
        memory_source=memory_source,
    )


def _root_data_volume(
    raw: Mapping[str, Any], diagnostics: list[VMIDiagnostic]
) -> RootDataVolumeReference | None:
    spec = raw.get("spec")
    if not isinstance(spec, Mapping):
        return None
    values = spec.get("volumes", [])
    if values is None:
        values = []
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        diagnostics.append(VMIDiagnostic("invalid-object-shape", "spec.volumes"))
        return None
    candidates: list[tuple[str, str]] = []
    for index, value in enumerate(values[:256]):
        if not isinstance(value, Mapping):
            continue
        data_volume = value.get("dataVolume")
        if not isinstance(data_volume, Mapping):
            continue
        path = f"spec.volumes[{index}]"
        volume_name = _safe_optional_identifier(
            value.get("name"),
            path=f"{path}.name",
            diagnostics=diagnostics,
            maximum=253,
        )
        data_volume_name = _safe_optional_identifier(
            data_volume.get("name"),
            path=f"{path}.dataVolume.name",
            diagnostics=diagnostics,
            maximum=253,
        )
        if volume_name is not None and data_volume_name is not None:
            candidates.append((volume_name, data_volume_name))
    roots = sorted({name for volume, name in candidates if volume == "rootdisk"})
    if len(roots) == 1:
        return RootDataVolumeReference(roots[0])
    if candidates:
        diagnostics.append(VMIDiagnostic("ambiguous-root-data-volume", "spec.volumes"))
    return None


def normalize_virtual_machine_instance(
    raw: Mapping[str, Any],
) -> NormalizedVirtualMachineInstance:
    """Normalize one admitted KubeVirt VMI without retaining the raw object."""

    if not isinstance(raw, Mapping):
        raise VMINormalizationError("invalid-vmi-object")
    if raw.get("kind") != VMI_KIND:
        raise VMINormalizationError("not-a-virtualmachineinstance")
    if raw.get("apiVersion") != VMI_API_VERSION:
        raise VMINormalizationError("unsupported-vmi-api-version")
    metadata = raw.get("metadata")
    if not isinstance(metadata, Mapping):
        raise VMINormalizationError("invalid-metadata")
    uid = _required_identity_text(metadata.get("uid"), "metadata.uid", maximum=256)
    namespace = _required_identity_text(
        metadata.get("namespace"), "metadata.namespace", maximum=253
    )
    name = _required_identity_text(metadata.get("name"), "metadata.name", maximum=253)
    resource_version = _required_identity_text(
        metadata.get("resourceVersion"), "metadata.resourceVersion", maximum=1024
    )

    diagnostics: list[VMIDiagnostic] = []
    lifecycle = _lifecycle(raw, diagnostics)
    capacity = _capacity(raw, diagnostics)
    return NormalizedVirtualMachineInstance(
        uid=uid,
        namespace=namespace,
        name=name,
        resource_version=resource_version,
        api_version=VMI_API_VERSION,
        owner_hint=_owner_hint(metadata, diagnostics),
        vm_reference=_vm_reference(metadata, diagnostics),
        root_data_volume=_root_data_volume(raw, diagnostics),
        lifecycle=lifecycle,
        admitted_capacity=capacity,
        diagnostics=tuple(diagnostics),
        valid_for_metering=capacity is not None,
    )


normalize_vmi = normalize_virtual_machine_instance


__all__ = [
    "NormalizedVMI",
    "NormalizedVirtualMachineInstance",
    "RootDataVolumeReference",
    "VMIAdmittedCapacity",
    "VMI_API_VERSION",
    "VMI_CAPACITY_ALGORITHM",
    "VMICpuTopology",
    "VMIDiagnostic",
    "VMILifecycle",
    "VMILifecycleState",
    "VMINormalizationError",
    "VMIOwnerHint",
    "VMIPhaseTransition",
    "VMReference",
    "normalize_virtual_machine_instance",
    "normalize_vmi",
]
