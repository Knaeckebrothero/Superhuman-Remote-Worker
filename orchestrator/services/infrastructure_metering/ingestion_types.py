"""Strict wire models for dedicated inventory collector ingestion."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .collectors.contracts import WatchGapReason, normalized_payload
from .collectors.pod_normalization import ALLOWED_POD_LABELS, POD_REQUESTS_ALGORITHM
from .collectors.storage_normalization import (
    ALLOWED_PV_FINALIZERS,
    ALLOWED_STORAGE_LABELS,
    CSI_VOLUME_IDENTITY_SCHEME,
    PVC_STORAGE_REQUEST_ALGORITHM,
    PV_STORAGE_CAPACITY_ALGORITHM,
    PV_UID_IDENTITY_SCHEME,
)
from .collectors.vmi_normalization import VMI_CAPACITY_ALGORITHM


_CLUSTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NAMESPACE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")

# Shared client/server body caps. Keeping these beside the wire models prevents
# a collector from signing and sending an envelope the ingestion endpoint must
# reject solely because the two sides drifted to different limits.
TICKET_BODY_LIMIT = 16 * 1024
SNAPSHOT_METADATA_BODY_LIMIT = 2 * 1024 * 1024
SNAPSHOT_BATCH_BODY_LIMIT = 2 * 1024 * 1024
WATCH_BODY_LIMIT = 2 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InventoryScopeWire(_StrictModel):
    source_cluster: str = Field(min_length=1, max_length=128)
    api_resource: str = Field(min_length=1, max_length=128)
    namespace: str | None = Field(default=None, max_length=253)
    cluster_scoped: bool = False

    @model_validator(mode="after")
    def validate_scope(self) -> "InventoryScopeWire":
        if not _CLUSTER_ID.fullmatch(self.source_cluster):
            raise ValueError("invalid source cluster")
        if len(self.api_resource.split("/")) != 3:
            raise ValueError("invalid API resource")
        if self.cluster_scoped:
            if self.namespace is not None:
                raise ValueError("cluster-scoped inventory cannot name a namespace")
        elif self.namespace is None or not _NAMESPACE.fullmatch(self.namespace):
            raise ValueError("namespaced inventory requires a valid namespace")
        return self


class InventoryErrorWire(_StrictModel):
    error_class: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    scope: InventoryScopeWire
    message: str = Field(min_length=1, max_length=512)
    kind: str | None = Field(default=None, min_length=1, max_length=128)
    uid: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("message")
    @classmethod
    def single_line_message(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("inventory errors must be single-line")
        return value


class InventoryItemWire(_StrictModel):
    scope: InventoryScopeWire
    snapshot_id: UUID | None
    kind: str = Field(min_length=1, max_length=128)
    uid: str = Field(min_length=1, max_length=256)
    revision_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    valid_for_metering: bool
    normalized: dict[str, Any]

    @field_validator("normalized")
    @classmethod
    def normalized_is_explicitly_projected(
        cls, value: dict[str, Any]
    ) -> dict[str, Any]:
        return normalized_payload(value)

    @model_validator(mode="after")
    def validate_shape(self) -> "InventoryItemWire":
        if self.valid_for_metering and self.revision_hash is None:
            raise ValueError("valid inventory item needs a revision and payload")
        if not self.valid_for_metering and self.revision_hash is not None:
            raise ValueError("invalid inventory item cannot claim a revision")
        return self


class _PodQuantityEvidence(_StrictModel):
    original: str = Field(min_length=1, max_length=128)
    decimal_value: str = Field(min_length=1, max_length=128)
    normalized_value: int = Field(ge=0, le=2**63 - 1)
    normalized_unit: Literal["millicore", "byte"]


class _PodResourceEvidence(_StrictModel):
    cpu: _PodQuantityEvidence | None = None
    memory: _PodQuantityEvidence | None = None


class _PodContainerRequestEvidence(_StrictModel):
    name: str = Field(min_length=1, max_length=253)
    restart_policy: Literal["Always"] | None
    requests: _PodResourceEvidence


class _PodStatusResources(_StrictModel):
    requests: _PodResourceEvidence


class _PodContainerStatusEvidence(_StrictModel):
    name: str = Field(min_length=1, max_length=253)
    resources: _PodStatusResources | None = None
    allocated_resources: _PodResourceEvidence | None = None


class _PodStatusEvidence(_StrictModel):
    resources: _PodStatusResources | None = None
    allocated_resources: _PodResourceEvidence | None = None


class _PodAdmittedRequestEvidence(_StrictModel):
    containers: list[_PodContainerRequestEvidence] = Field(max_length=10_000)
    init_containers: list[_PodContainerRequestEvidence] = Field(max_length=10_000)
    pod_requests: _PodResourceEvidence
    overhead: _PodResourceEvidence


class _PodResizeRequestEvidence(_StrictModel):
    container_statuses: list[_PodContainerStatusEvidence] = Field(max_length=10_000)
    init_container_statuses: list[_PodContainerStatusEvidence] = Field(
        max_length=10_000
    )
    pod_resources: _PodStatusEvidence


class _PodRequestEvidence(_StrictModel):
    admitted_requests: _PodAdmittedRequestEvidence | None = None
    resize_request_status: _PodResizeRequestEvidence | None = None

    @model_validator(mode="after")
    def all_or_no_evidence(self) -> "_PodRequestEvidence":
        if (self.admitted_requests is None) != (self.resize_request_status is None):
            raise ValueError("Pod request evidence must be complete or empty")
        return self


class _PodOwnerReference(_StrictModel):
    kind: str = Field(min_length=1, max_length=128)
    uid: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=253)


class _PodScheduledCondition(_StrictModel):
    status: Literal["True", "False", "Unknown"]
    last_transition_time: str | None = Field(default=None, max_length=64)


class _PodLifecycle(_StrictModel):
    state: Literal["unscheduled", "active", "terminal"]
    scheduled: bool
    terminal: bool
    accrues: bool
    phase: str | None = Field(default=None, max_length=64)
    node_name: str | None = Field(default=None, max_length=253)
    deletion_requested: bool
    creation_timestamp: str | None = Field(default=None, max_length=64)
    deletion_timestamp: str | None = Field(default=None, max_length=64)
    start_time: str | None = Field(default=None, max_length=64)
    pod_scheduled_condition: _PodScheduledCondition | None


_CAPACITY_SOURCE = Literal[
    "pod-level-status-aware",
    "pod-level-spec",
    "container-derived-status-aware",
    "container-derived-spec",
    "previous-or-current-conservative",
]


class _PodCapacity(_StrictModel):
    cpu_millicores: int = Field(ge=0, le=2**63 - 1)
    memory_bytes: int = Field(ge=0, le=2**63 - 1)
    cpu_source: _CAPACITY_SOURCE
    memory_source: _CAPACITY_SOURCE
    overhead_cpu_millicores: int = Field(ge=0, le=2**63 - 1)
    overhead_memory_bytes: int = Field(ge=0, le=2**63 - 1)
    capacity_quality: Literal["exact", "resize-status-unavailable"]
    resize_status: Literal["none", "infeasible", "deferred", "pending", "in-progress"]
    status_resources_used: bool
    measurement_algorithm: Literal[POD_REQUESTS_ALGORITHM]


class _PodDiagnostic(_StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    path: str = Field(min_length=1, max_length=512)
    resource: Literal["cpu", "memory"] | None = None
    original_value: str | None = Field(default=None, max_length=128)
    covered_by_pod_level: bool | None = None


class _NormalizedPodPayload(_StrictModel):
    source_kind: Literal["pod"]
    api_version: str = Field(min_length=1, max_length=64)
    namespace: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=253)
    uid: str = Field(min_length=1, max_length=256)
    resource_version: str | None = Field(default=None, max_length=1024)
    labels: dict[str, str]
    owner_references: list[_PodOwnerReference] = Field(max_length=1_000)
    lifecycle: _PodLifecycle
    capacity: _PodCapacity | None
    request_evidence: _PodRequestEvidence
    diagnostics: list[_PodDiagnostic] = Field(max_length=2_000)
    measurement_algorithm: Literal[POD_REQUESTS_ALGORITHM]
    valid_for_metering: bool
    revision_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("labels")
    @classmethod
    def labels_are_allowlisted(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > len(ALLOWED_POD_LABELS):
            raise ValueError("too many Pod attribution labels")
        for key, label_value in value.items():
            if key not in ALLOWED_POD_LABELS:
                raise ValueError("Pod attribution label is not allowlisted")
            if len(label_value) > 63 or any(char.isspace() for char in label_value):
                raise ValueError("Pod attribution label value is invalid")
        return value

    @model_validator(mode="after")
    def capacity_matches_validity(self) -> "_NormalizedPodPayload":
        if self.valid_for_metering:
            if self.capacity is None or self.revision_hash is None:
                raise ValueError("meterable Pod payload lacks capacity or revision")
        elif self.revision_hash is not None:
            raise ValueError("invalid Pod payload cannot claim a revision")
        return self


class _NormalizedPodFallback(_StrictModel):
    source_kind: Literal["pod"]
    uid: str = Field(min_length=1, max_length=256)
    namespace: str = Field(min_length=1, max_length=253)
    valid_for_metering: Literal[False]
    revision_hash: None
    normalization_error: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")


_NORMALIZED_POD_PAYLOAD = TypeAdapter(_NormalizedPodPayload | _NormalizedPodFallback)


def validate_normalized_pod_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the only normalized JSONB shape accepted by Pod ingestion.

    The generic collector contract prevents known raw fields, but this server
    boundary is deliberately allowlist-only: newly added Kubernetes fields do
    not become durable merely because a collector accidentally forwards them.
    """

    _NORMALIZED_POD_PAYLOAD.validate_python(value)
    return value


class _VMIOwnerHint(_StrictModel):
    kind: Literal["job", "thread"]
    owner_id: str = Field(min_length=1, max_length=63)


class _VMReference(_StrictModel):
    uid: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=253)


class _VMIRootDataVolume(_StrictModel):
    name: str = Field(min_length=1, max_length=253)


class _VMIPhaseTransition(_StrictModel):
    phase: str = Field(min_length=1, max_length=64)
    timestamp: str = Field(min_length=1, max_length=64)


class _VMILifecycle(_StrictModel):
    state: Literal["unscheduled", "active", "terminal"]
    scheduled: bool
    terminal: bool
    accrues: bool
    phase: str | None = Field(default=None, max_length=64)
    node_name: str | None = Field(default=None, max_length=253)
    paused: bool
    migrating: bool
    deletion_requested: bool
    creation_timestamp: str | None = Field(default=None, max_length=64)
    deletion_timestamp: str | None = Field(default=None, max_length=64)
    scheduled_transition_timestamp: str | None = Field(default=None, max_length=64)
    terminal_transition_timestamp: str | None = Field(default=None, max_length=64)
    phase_transitions: list[_VMIPhaseTransition] = Field(max_length=256)

    @model_validator(mode="after")
    def state_is_consistent(self) -> "_VMILifecycle":
        if self.state == "terminal":
            if not self.terminal or self.accrues:
                raise ValueError("terminal VMI lifecycle is inconsistent")
        elif self.state == "active":
            if not self.scheduled or self.terminal or not self.accrues:
                raise ValueError("active VMI lifecycle is inconsistent")
        elif self.scheduled or self.terminal or self.accrues:
            raise ValueError("unscheduled VMI lifecycle is inconsistent")
        return self


class _VMICpuTopology(_StrictModel):
    cores: int = Field(ge=1, le=2**31 - 1)
    sockets: int = Field(ge=1, le=2**31 - 1)
    threads: int = Field(ge=1, le=2**31 - 1)
    vcpus: int = Field(ge=1, le=2**31 - 1)

    @model_validator(mode="after")
    def product_matches(self) -> "_VMICpuTopology":
        if self.cores * self.sockets * self.threads != self.vcpus:
            raise ValueError("VMI admitted CPU topology is inconsistent")
        return self


class _VMIMemoryEvidence(_StrictModel):
    original: str = Field(min_length=1, max_length=128)
    decimal_value: str = Field(min_length=1, max_length=128)
    normalized_value: int = Field(ge=1, le=2**63 - 1)
    normalized_unit: Literal["byte"]


class _VMICapacity(_StrictModel):
    cpu_millicores: int = Field(ge=1, le=2**63 - 1)
    memory_bytes: int = Field(ge=1, le=2**63 - 1)
    cpu_topology: _VMICpuTopology
    memory_evidence: _VMIMemoryEvidence
    cpu_source: Literal["vmi-status-current-topology", "vmi-admitted-topology"]
    memory_source: Literal["vmi-status-guest-current", "vmi-admitted-guest-memory"]
    capacity_quality: Literal["exact"]
    measurement_algorithm: Literal[VMI_CAPACITY_ALGORITHM]

    @model_validator(mode="after")
    def quantities_match_evidence(self) -> "_VMICapacity":
        if self.cpu_millicores != self.cpu_topology.vcpus * 1000:
            raise ValueError("VMI admitted CPU capacity is inconsistent")
        if self.memory_bytes != self.memory_evidence.normalized_value:
            raise ValueError("VMI admitted memory capacity is inconsistent")
        return self


class _VMIDiagnostic(_StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    path: str = Field(min_length=1, max_length=512)


class _NormalizedVMIPayload(_StrictModel):
    source_kind: Literal["vmi"]
    api_version: Literal["kubevirt.io/v1"]
    namespace: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=253)
    uid: str = Field(min_length=1, max_length=256)
    resource_version: str = Field(min_length=1, max_length=1024)
    owner_hint: _VMIOwnerHint | None
    vm_reference: _VMReference | None
    root_data_volume: _VMIRootDataVolume | None
    lifecycle: _VMILifecycle
    capacity: _VMICapacity | None
    measurement_basis: Literal["guest-provisioned"]
    measurement_algorithm: Literal[VMI_CAPACITY_ALGORITHM]
    resource: Literal["workspace_vm"]
    diagnostics: list[_VMIDiagnostic] = Field(max_length=2_000)
    valid_for_metering: bool
    revision_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def capacity_matches_validity(self) -> "_NormalizedVMIPayload":
        if self.valid_for_metering:
            if self.capacity is None or self.revision_hash is None:
                raise ValueError("meterable VMI payload lacks capacity or revision")
        elif self.capacity is not None or self.revision_hash is not None:
            raise ValueError("invalid VMI payload cannot claim capacity or revision")
        return self


_NORMALIZED_VMI_PAYLOAD = TypeAdapter(_NormalizedVMIPayload)


def validate_normalized_vmi_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the raw-free admitted-VMI projection accepted by ingestion."""

    _NORMALIZED_VMI_PAYLOAD.validate_python(value)
    return value


class _StorageQuantityEvidence(_StrictModel):
    original: str = Field(min_length=1, max_length=128)
    decimal_value: str = Field(min_length=1, max_length=128)
    normalized_value: int = Field(ge=0, le=2**63 - 1)
    normalized_unit: Literal["byte"]


class _StorageCapacity(_StrictModel):
    storage_bytes: int = Field(ge=0, le=2**63 - 1)
    source: Literal["pvc-requested-storage", "pv-provisioned-capacity"]
    measurement_algorithm: Literal[
        PVC_STORAGE_REQUEST_ALGORITHM,
        PV_STORAGE_CAPACITY_ALGORITHM,
    ]


class _StorageLifecycle(_StrictModel):
    phase: str | None = Field(default=None, max_length=64)
    accrues: Literal[True]
    deletion_requested: bool
    creation_timestamp: str | None = Field(default=None, max_length=64)
    deletion_timestamp: str | None = Field(default=None, max_length=64)


class _PVStorageLifecycle(_StorageLifecycle):
    has_deletion_protection_finalizer: bool


class _StorageDiagnostic(_StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    path: str = Field(min_length=1, max_length=512)


class _StorageOwnerReference(_StrictModel):
    kind: str = Field(min_length=1, max_length=128)
    uid: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=253)


class _StorageBasePayload(_StrictModel):
    api_version: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=253)
    uid: str = Field(min_length=1, max_length=256)
    resource_version: str | None = Field(default=None, max_length=1024)
    labels: dict[str, str]
    owner_references: list[_StorageOwnerReference] = Field(max_length=1_000)
    lifecycle: _StorageLifecycle
    capacity: _StorageCapacity | None
    capacity_evidence: _StorageQuantityEvidence | None
    storage_class: str | None = Field(default=None, max_length=253)
    access_modes: list[str] = Field(max_length=32)
    volume_mode: str | None = Field(default=None, max_length=64)
    measurement_algorithm: Literal[
        PVC_STORAGE_REQUEST_ALGORITHM,
        PV_STORAGE_CAPACITY_ALGORITHM,
    ]
    diagnostics: list[_StorageDiagnostic] = Field(max_length=2_000)
    valid_for_metering: bool
    revision_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("labels")
    @classmethod
    def labels_are_allowlisted(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > len(ALLOWED_STORAGE_LABELS):
            raise ValueError("too many storage attribution labels")
        for key, label_value in value.items():
            if key not in ALLOWED_STORAGE_LABELS:
                raise ValueError("storage attribution label is not allowlisted")
            if len(label_value) > 63 or any(char.isspace() for char in label_value):
                raise ValueError("storage attribution label value is invalid")
        return value

    @field_validator("access_modes")
    @classmethod
    def access_modes_are_bounded(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("storage access modes must be unique and sorted")
        if any(not item or len(item) > 64 or item != item.strip() for item in value):
            raise ValueError("storage access mode is invalid")
        return value

    @model_validator(mode="after")
    def capacity_matches_validity(self) -> "_StorageBasePayload":
        if self.valid_for_metering:
            if (
                self.capacity is None
                or self.capacity_evidence is None
                or self.revision_hash is None
            ):
                raise ValueError("meterable storage payload lacks capacity or revision")
            if self.capacity.storage_bytes != self.capacity_evidence.normalized_value:
                raise ValueError("storage capacity differs from quantity evidence")
            if self.capacity.measurement_algorithm != self.measurement_algorithm:
                raise ValueError("storage capacity algorithm differs from payload")
        elif self.revision_hash is not None:
            raise ValueError("invalid storage payload cannot claim a revision")
        return self


class _NormalizedPVCPayload(_StorageBasePayload):
    source_kind: Literal["pvc"]
    namespace: str = Field(min_length=1, max_length=253)
    bound_volume_name: str | None = Field(default=None, max_length=253)
    measurement_basis: Literal["claim-requested"]
    measurement_algorithm: Literal[PVC_STORAGE_REQUEST_ALGORITHM]

    @model_validator(mode="after")
    def pvc_capacity_contract(self) -> "_NormalizedPVCPayload":
        if self.capacity is not None:
            if (
                self.capacity.source != "pvc-requested-storage"
                or self.capacity.measurement_algorithm != PVC_STORAGE_REQUEST_ALGORITHM
            ):
                raise ValueError("PVC capacity has the wrong provenance")
        return self


class _VolumeIdentity(_StrictModel):
    scheme: Literal[CSI_VOLUME_IDENTITY_SCHEME, PV_UID_IDENTITY_SCHEME]
    key_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    key_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    durable_asset_id: str = Field(min_length=1, max_length=256)
    pv_uid: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def identity_matches_scheme(self) -> "_VolumeIdentity":
        if self.scheme == CSI_VOLUME_IDENTITY_SCHEME:
            if not re.fullmatch(r"[0-9a-f]{64}", self.durable_asset_id):
                raise ValueError("CSI durable asset identity is invalid")
        elif self.durable_asset_id != self.pv_uid:
            raise ValueError("fallback durable asset identity must be the PV UID")
        return self


class _VolumeClaimReference(_StrictModel):
    uid: str = Field(min_length=1, max_length=256)
    namespace: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=253)


class _NormalizedPVPayload(_StorageBasePayload):
    source_kind: Literal["volume"]
    namespace: None
    lifecycle: _PVStorageLifecycle
    volume_identity: _VolumeIdentity
    reclaim_policy: str | None = Field(default=None, max_length=64)
    finalizers: list[
        Literal[
            "external-provisioner.volume.kubernetes.io/finalizer",
            "kubernetes.io/pv-controller",
            "kubernetes.io/pv-protection",
        ]
    ] = Field(max_length=len(ALLOWED_PV_FINALIZERS))
    claim_reference: _VolumeClaimReference | None
    csi_driver: str | None = Field(default=None, max_length=253)
    measurement_basis: Literal["volume-provisioned"]
    measurement_algorithm: Literal[PV_STORAGE_CAPACITY_ALGORITHM]
    mapping_state: Literal["unmapped"]
    resource: Literal["unmapped_block_volume"]

    @field_validator("finalizers")
    @classmethod
    def finalizers_are_unique_and_sorted(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("PV finalizers must be unique and sorted")
        return value

    @model_validator(mode="after")
    def pv_identity_and_capacity_contract(self) -> "_NormalizedPVPayload":
        if self.uid != self.volume_identity.durable_asset_id:
            raise ValueError("PV inventory UID differs from durable asset identity")
        if self.volume_identity.scheme == CSI_VOLUME_IDENTITY_SCHEME:
            if not self.csi_driver:
                raise ValueError("CSI volume identity requires a driver")
        elif self.csi_driver is not None:
            raise ValueError("PV UID fallback cannot claim a CSI driver")
        if self.capacity is not None:
            if (
                self.capacity.source != "pv-provisioned-capacity"
                or self.capacity.measurement_algorithm != PV_STORAGE_CAPACITY_ALGORITHM
            ):
                raise ValueError("PV capacity has the wrong provenance")
        return self


class _NormalizedStorageFallback(_StrictModel):
    source_kind: Literal["pvc", "volume"]
    uid: str = Field(min_length=1, max_length=256)
    namespace: str | None = Field(default=None, max_length=253)
    valid_for_metering: Literal[False]
    revision_hash: None
    normalization_error: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")


_NORMALIZED_PVC_PAYLOAD = TypeAdapter(
    _NormalizedPVCPayload | _NormalizedStorageFallback
)
_NORMALIZED_PV_PAYLOAD = TypeAdapter(_NormalizedPVPayload | _NormalizedStorageFallback)


def validate_normalized_pvc_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Recursively validate the only PVC JSONB shapes accepted by ingestion."""

    parsed = _NORMALIZED_PVC_PAYLOAD.validate_python(value)
    if isinstance(parsed, _NormalizedStorageFallback) and parsed.source_kind != "pvc":
        raise ValueError("PVC fallback carries the wrong source kind")
    return value


def validate_normalized_pv_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Recursively validate the raw-handle-free PV JSONB ingestion shape."""

    parsed = _NORMALIZED_PV_PAYLOAD.validate_python(value)
    if isinstance(parsed, _NormalizedStorageFallback):
        if parsed.source_kind != "volume" or parsed.namespace is not None:
            raise ValueError("PV fallback carries an invalid exact-scope identity")
    return value


class InventoryTicketRequest(_StrictModel):
    scope: InventoryScopeWire
    intent: Literal["snapshot", "watch-session"]
    snapshot_id: UUID | None = None
    starting_resource_version: str | None = Field(default=None, max_length=1024)
    controller_epoch: str | None = Field(default=None, min_length=1, max_length=256)
    sequence: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_intent(self) -> "InventoryTicketRequest":
        if (self.controller_epoch is None) != (self.sequence is None):
            raise ValueError("controller_epoch and sequence must be supplied together")
        if self.intent == "snapshot":
            if self.snapshot_id is None or self.starting_resource_version is not None:
                raise ValueError("snapshot ticket requires only snapshot_id")
        elif (
            self.snapshot_id is not None
            or not self.starting_resource_version
            or self.controller_epoch is not None
        ):
            raise ValueError("watch ticket requires only a starting cursor")
        if self.starting_resource_version == "0":
            raise ValueError("resource version 0 is not a metering cursor")
        return self


class InventoryTicketResponse(_StrictModel):
    ticket_id: UUID
    ticket_token: str = Field(min_length=32, max_length=512)
    leader_generation: int = Field(gt=0)
    expires_at: datetime
    last_resource_version: str | None = Field(default=None, max_length=1024)


class InventorySnapshotBegin(_StrictModel):
    ticket_id: UUID
    ticket_token: str = Field(min_length=32, max_length=512)
    collector_id: str = Field(min_length=1, max_length=128)
    source_cluster: str = Field(min_length=1, max_length=128)
    api_resource: str = Field(min_length=1, max_length=128)
    namespace: str | None = Field(default=None, max_length=253)
    cluster_scoped: bool = False
    collection_started_at: datetime
    collection_completed_at: datetime
    source_snapshot_at: datetime | None = None
    complete: bool
    snapshot_id: UUID
    leader_generation: int = Field(gt=0)
    controller_epoch: str | None = Field(default=None, max_length=256)
    sequence: int | None = Field(default=None, ge=0)
    resource_version: str | None = Field(default=None, max_length=1024)
    item_count: int = Field(ge=0)
    item_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pages_read: int = Field(ge=0)
    bytes_read: int = Field(ge=0)
    items_streamed: Literal[True]
    fatal_errors: list[InventoryErrorWire] = Field(max_length=1000)
    item_errors: list[InventoryErrorWire] = Field(max_length=2_000)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "InventorySnapshotBegin":
        scope = InventoryScopeWire(
            source_cluster=self.source_cluster,
            api_resource=self.api_resource,
            namespace=self.namespace,
            cluster_scoped=self.cluster_scoped,
        )
        if self.collection_completed_at < self.collection_started_at:
            raise ValueError("collection timestamps must be monotonic")
        if self.complete:
            if self.fatal_errors or not self.resource_version or not self.item_digest:
                raise ValueError("complete snapshot lacks authoritative metadata")
        elif (
            self.resource_version is not None
            or self.item_digest is not None
            or self.item_count != 0
        ):
            raise ValueError(
                "incomplete snapshot must be metadata-only and cannot advance cursor"
            )
        for error in (*self.fatal_errors, *self.item_errors):
            if error.scope != scope:
                raise ValueError("snapshot error scope mismatch")
        return self

    @property
    def scope(self) -> InventoryScopeWire:
        return InventoryScopeWire(
            source_cluster=self.source_cluster,
            api_resource=self.api_resource,
            namespace=self.namespace,
            cluster_scoped=self.cluster_scoped,
        )


class InventorySnapshotItemBatch(_StrictModel):
    ticket_id: UUID
    ticket_token: str = Field(min_length=32, max_length=512)
    snapshot_id: UUID
    scope: InventoryScopeWire
    batch_ordinal: int = Field(ge=0)
    items: list[InventoryItemWire] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_batch(self) -> "InventorySnapshotItemBatch":
        identities: set[tuple[str, str]] = set()
        for item in self.items:
            if item.snapshot_id != self.snapshot_id:
                raise ValueError("snapshot item ID mismatch")
            if item.scope != self.scope:
                raise ValueError("snapshot item scope mismatch")
            identity = (item.kind, item.uid)
            if identity in identities:
                raise ValueError("duplicate inventory identity in batch")
            identities.add(identity)
        return self


class InventorySnapshotFinalize(_StrictModel):
    ticket_id: UUID
    ticket_token: str = Field(min_length=32, max_length=512)
    snapshot_id: UUID
    scope: InventoryScopeWire
    shadow_enabled: bool
    collection_completed_at: datetime
    source_snapshot_at: datetime | None = None
    complete: bool
    resource_version: str | None = Field(default=None, max_length=1024)
    item_count: int = Field(ge=0)
    item_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    controller_epoch: str | None = Field(default=None, max_length=256)
    sequence: int | None = Field(default=None, ge=0)
    fatal_errors: list[InventoryErrorWire] = Field(max_length=1000)

    @model_validator(mode="after")
    def validate_finalization(self) -> "InventorySnapshotFinalize":
        if self.complete:
            if self.fatal_errors or not self.resource_version or not self.item_digest:
                raise ValueError("complete finalization lacks authoritative metadata")
        elif (
            self.resource_version is not None
            or self.item_digest is not None
            or self.item_count != 0
        ):
            raise ValueError(
                "incomplete finalization must be metadata-only and cannot advance cursor"
            )
        if (self.controller_epoch is None) != (self.sequence is None):
            raise ValueError("controller epoch and sequence must be supplied together")
        return self


class WatchObservationWire(_StrictModel):
    scope: InventoryScopeWire
    event_type: Literal["ADDED", "MODIFIED", "DELETED", "BOOKMARK"]
    resource_version: str = Field(min_length=1, max_length=1024)
    source_event_bytes: int = Field(gt=0, le=2 * 1024 * 1024)
    collector_observed_at: datetime
    confirms_presence: bool
    item: InventoryItemWire | None

    @model_validator(mode="after")
    def validate_observation(self) -> "WatchObservationWire":
        if self.resource_version == "0":
            raise ValueError("resource version 0 is not a metering cursor")
        expected_presence = self.event_type in {"ADDED", "MODIFIED"}
        if self.confirms_presence is not expected_presence:
            raise ValueError("watch presence claim does not match event type")
        if self.event_type == "BOOKMARK":
            if self.item is not None:
                raise ValueError("bookmark cannot carry an item")
        elif self.item is None or self.item.scope != self.scope:
            raise ValueError("object event requires an exact-scope item")
        return self


class InventoryWatchApply(_StrictModel):
    ticket_id: UUID
    ticket_token: str = Field(min_length=32, max_length=512)
    leader_generation: int = Field(gt=0)
    event_id: UUID
    expected_resource_version: str = Field(min_length=1, max_length=1024)
    observation: WatchObservationWire

    @model_validator(mode="after")
    def validate_cursor(self) -> "InventoryWatchApply":
        if self.expected_resource_version == "0":
            raise ValueError("resource version 0 is not a metering cursor")
        return self


class InventoryWatchFinish(_StrictModel):
    ticket_id: UUID
    ticket_token: str = Field(min_length=32, max_length=512)
    leader_generation: int = Field(gt=0)
    scope: InventoryScopeWire
    started_at: datetime
    completed_at: datetime
    starting_resource_version: str = Field(min_length=1, max_length=1024)
    committed_resource_version: str = Field(min_length=1, max_length=1024)
    processed_events: int = Field(ge=0)
    object_events: int = Field(ge=0)
    bookmarks: int = Field(ge=0)
    bytes_read: int = Field(ge=0)
    reconnect_required: bool
    relist_required: bool
    history_lost: bool
    limit_reached: bool
    gap_reason: WatchGapReason | None = None
    ambiguous_resource_version: str | None = Field(default=None, max_length=1024)
    history_event_id: UUID | None = None
    fatal_errors: list[InventoryErrorWire] = Field(max_length=1000)
    item_errors: list[InventoryErrorWire] = Field(max_length=2_000)

    @model_validator(mode="after")
    def validate_outcome(self) -> "InventoryWatchFinish":
        if self.completed_at < self.started_at:
            raise ValueError("watch timestamps must be monotonic")
        if self.object_events + self.bookmarks != self.processed_events:
            raise ValueError("watch event counters are inconsistent")
        if (
            self.starting_resource_version == "0"
            or self.committed_resource_version == "0"
        ):
            raise ValueError("resource version 0 is not a metering cursor")
        if self.history_lost and not self.relist_required:
            raise ValueError("lost history must require a relist")
        if (self.history_event_id is not None) != self.history_lost:
            raise ValueError("history loss requires exactly one idempotent event ID")
        if (self.gap_reason is not None) != self.history_lost:
            raise ValueError("history loss requires exactly one typed gap reason")
        ambiguous_apply = self.gap_reason == WatchGapReason.AMBIGUOUS_APPLY
        if (self.ambiguous_resource_version is not None) != ambiguous_apply:
            raise ValueError(
                "ambiguous apply requires exactly one attempted resource version"
            )
        if self.ambiguous_resource_version == "0":
            raise ValueError("resource version 0 is not a metering cursor")
        return self


__all__ = [
    "InventoryErrorWire",
    "InventoryItemWire",
    "InventoryScopeWire",
    "InventorySnapshotBegin",
    "InventorySnapshotFinalize",
    "InventorySnapshotItemBatch",
    "InventoryTicketRequest",
    "InventoryTicketResponse",
    "InventoryWatchApply",
    "InventoryWatchFinish",
    "SNAPSHOT_BATCH_BODY_LIMIT",
    "SNAPSHOT_METADATA_BODY_LIMIT",
    "TICKET_BODY_LIMIT",
    "WATCH_BODY_LIMIT",
    "WatchObservationWire",
    "validate_normalized_pod_payload",
    "validate_normalized_pvc_payload",
    "validate_normalized_pv_payload",
    "validate_normalized_vmi_payload",
]
