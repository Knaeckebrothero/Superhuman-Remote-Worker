"""Pure, raw-free normalization of Kubernetes PVCs and PVs.

PVC demand and provisioned volume assets are deliberately different inventory
identities.  Claims retain their immutable Kubernetes UID.  CSI-backed PVs use
an HMAC-derived durable asset identity so a retained disk re-imported through a
new PV object resumes the same physical lifecycle without persisting the raw
``volumeHandle`` or CSI attributes.  Non-CSI PVs fall back to the immutable PV
UID because Kubernetes exposes no portable external-asset identity for them.

Only a small classification and lifecycle allowlist leaves this module.  In
particular, annotations, arbitrary labels, volume sources, CSI attributes, and
the HMAC key never enter the normalized payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Any, Literal

from .quantities import (
    NormalizedQuantity,
    QuantityNormalizationError,
    normalize_byte_quantity,
)


PVC_STORAGE_REQUEST_ALGORITHM = "pvc-request-storage-k8s-v1"
PV_STORAGE_CAPACITY_ALGORITHM = "pv-capacity-storage-k8s-v1"
CSI_VOLUME_IDENTITY_SCHEME = "csi-hmac-sha256-v1"
PV_UID_IDENTITY_SCHEME = "pv-uid-v1"

ALLOWED_STORAGE_LABELS = frozenset(
    {
        "app",
        "app.kubernetes.io/name",
        "app.kubernetes.io/instance",
        "app.kubernetes.io/component",
        "app.kubernetes.io/managed-by",
        "job-id",
        "thread-id",
        "srw/job-id",
        "srw/thread-id",
        "srw/component",
        "srw.io/component",
        "srw.io/job-id",
        "srw.io/thread-id",
        "srw.io/owner-kind",
        "srw.io/owner-id",
        "srw.io/rootdisk",
        "srw.io/golden-image",
        "srw.io/vm-image",
        "topology.kubernetes.io/region",
        "topology.kubernetes.io/zone",
        "failure-domain.beta.kubernetes.io/region",
        "failure-domain.beta.kubernetes.io/zone",
    }
)

# These are lifecycle evidence, not a pass-through of arbitrary finalizer text.
# The external-provisioner and in-tree controller finalizers are relevant to a
# future backend-deletion proof.  PV protection proves only Kubernetes object
# safety and must not by itself be interpreted as external deletion evidence.
ALLOWED_PV_FINALIZERS = frozenset(
    {
        "external-provisioner.volume.kubernetes.io/finalizer",
        "kubernetes.io/pv-controller",
        "kubernetes.io/pv-protection",
    }
)

_CLUSTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KEY_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CSI_HMAC_CONTEXT = b"srw-infrastructure-volume-identity-v1\x00"
_KEY_FINGERPRINT_CONTEXT = b"srw-infrastructure-volume-identity-key-fingerprint-v1\x00"


class StorageNormalizationError(ValueError):
    """Fatal storage normalization error with a stable, raw-free code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class StorageDiagnostic:
    """Sanitized evidence safe for durable JSONB storage."""

    code: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path}


@dataclass(frozen=True, slots=True)
class StorageOwnerReference:
    kind: str
    uid: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "uid": self.uid, "name": self.name}


@dataclass(frozen=True, slots=True)
class StorageLifecycle:
    """Kubernetes-visible lifetime; absence/deletion is reconciled elsewhere."""

    phase: str | None
    accrues: bool
    deletion_requested: bool
    creation_timestamp: str | None
    deletion_timestamp: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "accrues": self.accrues,
            "deletion_requested": self.deletion_requested,
            "creation_timestamp": self.creation_timestamp,
            "deletion_timestamp": self.deletion_timestamp,
        }


@dataclass(frozen=True, slots=True)
class StorageCapacity:
    storage_bytes: int
    source: Literal[
        "pvc-requested-storage",
        "pv-provisioned-capacity",
    ]
    measurement_algorithm: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "storage_bytes": self.storage_bytes,
            "source": self.source,
            "measurement_algorithm": self.measurement_algorithm,
        }


@dataclass(frozen=True, slots=True)
class VolumeClaimReference:
    uid: str
    namespace: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"uid": self.uid, "namespace": self.namespace, "name": self.name}


@dataclass(frozen=True, slots=True)
class NormalizedPersistentVolumeClaim:
    """Allowlisted PVC observation representing logical requested demand."""

    uid: str
    namespace: str
    name: str
    resource_version: str | None
    api_version: str
    labels: dict[str, str]
    owner_references: tuple[StorageOwnerReference, ...]
    lifecycle: StorageLifecycle
    capacity: StorageCapacity | None
    capacity_evidence: NormalizedQuantity | None
    storage_class: str | None
    access_modes: tuple[str, ...]
    volume_mode: str | None
    bound_volume_name: str | None
    diagnostics: tuple[StorageDiagnostic, ...]
    valid_for_metering: bool
    kind: Literal["pvc"] = "pvc"
    revision_hash: str | None = field(init=False)

    def __post_init__(self) -> None:
        revision_hash = _revision_hash(self.revision_payload())
        if not self.valid_for_metering:
            revision_hash = None
        elif self.capacity is None or self.capacity_evidence is None:
            raise ValueError("a meterable PVC requires requested storage capacity")
        object.__setattr__(self, "revision_hash", revision_hash)

    def revision_payload(self) -> dict[str, Any]:
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
                "labels": dict(sorted(self.labels.items())),
                "owner_references": [
                    reference.to_dict() for reference in self.owner_references
                ],
            },
            "lifecycle": {"phase": self.lifecycle.phase},
            "capacity": self.capacity.to_dict() if self.capacity else None,
            "storage_profile": {
                "storage_class": self.storage_class,
                "access_modes": list(self.access_modes),
                "volume_mode": self.volume_mode,
                "bound_volume_name": self.bound_volume_name,
            },
            "diagnostics": _revision_diagnostics(self.diagnostics),
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
            "labels": dict(self.labels),
            "owner_references": [
                reference.to_dict() for reference in self.owner_references
            ],
            "lifecycle": self.lifecycle.to_dict(),
            "capacity": self.capacity.to_dict() if self.capacity else None,
            "capacity_evidence": (
                self.capacity_evidence.to_dict() if self.capacity_evidence else None
            ),
            "storage_class": self.storage_class,
            "access_modes": list(self.access_modes),
            "volume_mode": self.volume_mode,
            "bound_volume_name": self.bound_volume_name,
            "measurement_basis": "claim-requested",
            "measurement_algorithm": PVC_STORAGE_REQUEST_ALGORITHM,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "valid_for_metering": self.valid_for_metering,
            "revision_hash": self.revision_hash,
        }


@dataclass(frozen=True, slots=True)
class NormalizedPersistentVolume:
    """Allowlisted PV attachment for one durable provisioned-volume asset."""

    # ``uid`` is the source identity consumed by inventory, not necessarily the
    # PV object's UID.  The latter remains a separate attachment/incarnation.
    uid: str
    pv_uid: str
    name: str
    resource_version: str | None
    api_version: str
    identity_scheme: Literal["csi-hmac-sha256-v1", "pv-uid-v1"]
    identity_key_version: str
    identity_key_fingerprint: str
    labels: dict[str, str]
    owner_references: tuple[StorageOwnerReference, ...]
    lifecycle: StorageLifecycle
    capacity: StorageCapacity | None
    capacity_evidence: NormalizedQuantity | None
    storage_class: str | None
    access_modes: tuple[str, ...]
    volume_mode: str | None
    reclaim_policy: str | None
    finalizers: tuple[str, ...]
    claim_reference: VolumeClaimReference | None
    csi_driver: str | None
    diagnostics: tuple[StorageDiagnostic, ...]
    valid_for_metering: bool
    namespace: None = None
    kind: Literal["volume"] = "volume"
    revision_hash: str | None = field(init=False)

    def __post_init__(self) -> None:
        revision_hash = _revision_hash(self.revision_payload())
        if not self.valid_for_metering:
            revision_hash = None
        elif self.capacity is None or self.capacity_evidence is None:
            raise ValueError("a meterable PV requires provisioned capacity")
        object.__setattr__(self, "revision_hash", revision_hash)

    def revision_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "api_version": self.api_version,
            "identity": {
                "durable_asset_id": self.uid,
                "identity_scheme": self.identity_scheme,
                "identity_key_version": self.identity_key_version,
                "identity_key_fingerprint": self.identity_key_fingerprint,
                "pv_uid": self.pv_uid,
                "name": self.name,
                "creation_timestamp": self.lifecycle.creation_timestamp,
            },
            "attribution": {
                "labels": dict(sorted(self.labels.items())),
                "owner_references": [
                    reference.to_dict() for reference in self.owner_references
                ],
                "claim_reference": (
                    self.claim_reference.to_dict() if self.claim_reference else None
                ),
            },
            "lifecycle": {
                "phase": self.lifecycle.phase,
                "reclaim_policy": self.reclaim_policy,
                "finalizers": list(self.finalizers),
            },
            "capacity": self.capacity.to_dict() if self.capacity else None,
            "storage_profile": {
                "storage_class": self.storage_class,
                "access_modes": list(self.access_modes),
                "volume_mode": self.volume_mode,
                "csi_driver": self.csi_driver,
            },
            "diagnostics": _revision_diagnostics(self.diagnostics),
        }

    def to_db_item(self) -> dict[str, Any]:
        """Return a projection that cannot reveal CSI handles or attributes."""

        backend_deletion_finalizers = {
            "external-provisioner.volume.kubernetes.io/finalizer",
            "kubernetes.io/pv-controller",
        }
        lifecycle = self.lifecycle.to_dict()
        lifecycle["has_deletion_protection_finalizer"] = bool(
            backend_deletion_finalizers.intersection(self.finalizers)
        )
        return {
            "source_kind": self.kind,
            "api_version": self.api_version,
            "namespace": None,
            "name": self.name,
            "uid": self.uid,
            "resource_version": self.resource_version,
            "volume_identity": {
                "scheme": self.identity_scheme,
                "key_version": self.identity_key_version,
                "key_fingerprint": self.identity_key_fingerprint,
                "durable_asset_id": self.uid,
                "pv_uid": self.pv_uid,
            },
            "labels": dict(self.labels),
            "owner_references": [
                reference.to_dict() for reference in self.owner_references
            ],
            "lifecycle": lifecycle,
            "capacity": self.capacity.to_dict() if self.capacity else None,
            "capacity_evidence": (
                self.capacity_evidence.to_dict() if self.capacity_evidence else None
            ),
            "storage_class": self.storage_class,
            "access_modes": list(self.access_modes),
            "volume_mode": self.volume_mode,
            "reclaim_policy": self.reclaim_policy,
            "finalizers": list(self.finalizers),
            "claim_reference": (
                self.claim_reference.to_dict() if self.claim_reference else None
            ),
            "csi_driver": self.csi_driver,
            "measurement_basis": "volume-provisioned",
            "measurement_algorithm": PV_STORAGE_CAPACITY_ALGORITHM,
            "mapping_state": "unmapped",
            "resource": "unmapped_block_volume",
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "valid_for_metering": self.valid_for_metering,
            "revision_hash": self.revision_hash,
        }


# Short names are convenient at the collector/runtime boundary while the full
# Kubernetes names keep public imports unambiguous.
NormalizedPVC = NormalizedPersistentVolumeClaim
NormalizedPV = NormalizedPersistentVolume


def _revision_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _revision_diagnostics(
    diagnostics: tuple[StorageDiagnostic, ...],
) -> list[dict[str, str]]:
    return sorted(
        (diagnostic.to_dict() for diagnostic in diagnostics),
        key=lambda item: (item["code"], item["path"]),
    )


def _required_identity_text(value: Any, path: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(character.isspace() for character in value)
    ):
        code_path = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", path)
        code_path = re.sub(r"[^A-Za-z0-9]+", "-", code_path).strip("-").lower()
        raise StorageNormalizationError(f"invalid-{code_path}")
    return value


def _safe_optional_identifier(
    value: Any,
    *,
    path: str,
    diagnostics: list[StorageDiagnostic],
    maximum: int = 253,
) -> str | None:
    if value in (None, ""):
        return None
    if (
        isinstance(value, str)
        and len(value) <= maximum
        and value == value.strip()
        and not any(character.isspace() for character in value)
    ):
        return value
    diagnostics.append(StorageDiagnostic("invalid-classification-field", path))
    return None


def _canonical_timestamp(
    value: Any,
    *,
    path: str,
    diagnostics: list[StorageDiagnostic],
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
        diagnostics.append(StorageDiagnostic("invalid-timestamp", path))
        return None


def _safe_labels(
    metadata: Mapping[str, Any], diagnostics: list[StorageDiagnostic]
) -> dict[str, str]:
    value = metadata.get("labels", {})
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        diagnostics.append(
            StorageDiagnostic("invalid-metadata-field", "metadata.labels")
        )
        return {}
    labels: dict[str, str] = {}
    for key in sorted(ALLOWED_STORAGE_LABELS):
        if key not in value:
            continue
        label_value = value[key]
        if (
            isinstance(label_value, str)
            and len(label_value) <= 63
            and not any(character.isspace() for character in label_value)
        ):
            labels[key] = label_value
        else:
            diagnostics.append(
                StorageDiagnostic("invalid-metadata-field", f"metadata.labels.{key}")
            )
    return labels


def _owner_references(
    metadata: Mapping[str, Any], diagnostics: list[StorageDiagnostic]
) -> tuple[StorageOwnerReference, ...]:
    value = metadata.get("ownerReferences", [])
    if value is None:
        value = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        diagnostics.append(
            StorageDiagnostic("invalid-metadata-field", "metadata.ownerReferences")
        )
        return ()
    references: list[StorageOwnerReference] = []
    for index, item in enumerate(value[:1000]):
        path = f"metadata.ownerReferences[{index}]"
        if not isinstance(item, Mapping):
            diagnostics.append(StorageDiagnostic("invalid-metadata-field", path))
            continue
        try:
            references.append(
                StorageOwnerReference(
                    kind=_required_identity_text(
                        item.get("kind"), f"{path}.kind", maximum=128
                    ),
                    uid=_required_identity_text(
                        item.get("uid"), f"{path}.uid", maximum=256
                    ),
                    name=_required_identity_text(
                        item.get("name"), f"{path}.name", maximum=253
                    ),
                )
            )
        except StorageNormalizationError:
            diagnostics.append(StorageDiagnostic("invalid-metadata-field", path))
    if len(value) > 1000:
        diagnostics.append(
            StorageDiagnostic("metadata-field-limit", "metadata.ownerReferences")
        )
    references.sort(key=lambda item: (item.kind, item.uid, item.name))
    return tuple(references)


def _metadata(
    raw: Mapping[str, Any],
    *,
    expected_kind: str,
    namespaced: bool,
    diagnostics: list[StorageDiagnostic],
) -> tuple[
    str,
    str | None,
    str,
    str | None,
    str,
    dict[str, str],
    tuple[StorageOwnerReference, ...],
    StorageLifecycle,
]:
    kind = raw.get("kind")
    if kind not in (None, expected_kind):
        raise StorageNormalizationError(f"not-a-{expected_kind.lower()}")
    api_version = _required_identity_text(
        raw.get("apiVersion", "v1"), "apiVersion", maximum=64
    )
    metadata = raw.get("metadata")
    if not isinstance(metadata, Mapping):
        raise StorageNormalizationError("invalid-metadata")
    uid = _required_identity_text(metadata.get("uid"), "metadata.uid", maximum=256)
    name = _required_identity_text(metadata.get("name"), "metadata.name", maximum=253)
    if namespaced:
        namespace = _required_identity_text(
            metadata.get("namespace"), "metadata.namespace", maximum=253
        )
    else:
        namespace_value = metadata.get("namespace")
        if namespace_value not in (None, ""):
            raise StorageNormalizationError("cluster-resource-has-namespace")
        namespace = None

    resource_version = _safe_optional_identifier(
        metadata.get("resourceVersion"),
        path="metadata.resourceVersion",
        diagnostics=diagnostics,
        maximum=1024,
    )
    labels = _safe_labels(metadata, diagnostics)
    owners = _owner_references(metadata, diagnostics)

    status_value = raw.get("status", {})
    status = status_value if isinstance(status_value, Mapping) else {}
    if status_value is not None and not isinstance(status_value, Mapping):
        diagnostics.append(StorageDiagnostic("invalid-object-shape", "status"))
    phase = _safe_optional_identifier(
        status.get("phase"),
        path="status.phase",
        diagnostics=diagnostics,
        maximum=64,
    )
    creation_timestamp = _canonical_timestamp(
        metadata.get("creationTimestamp"),
        path="metadata.creationTimestamp",
        diagnostics=diagnostics,
    )
    deletion_timestamp = _canonical_timestamp(
        metadata.get("deletionTimestamp"),
        path="metadata.deletionTimestamp",
        diagnostics=diagnostics,
    )
    if (
        creation_timestamp is not None
        and deletion_timestamp is not None
        and deletion_timestamp < creation_timestamp
    ):
        diagnostics.append(
            StorageDiagnostic("unsafe-timestamp-order", "metadata.deletionTimestamp")
        )
        deletion_timestamp = None
    lifecycle = StorageLifecycle(
        phase=phase,
        # A deletion request does not prove deletion. The reconciler closes on
        # an authoritative DELETED event or complete-snapshot absence.
        accrues=True,
        deletion_requested=metadata.get("deletionTimestamp") not in (None, ""),
        creation_timestamp=creation_timestamp,
        deletion_timestamp=deletion_timestamp,
    )
    return (
        uid,
        namespace,
        name,
        resource_version,
        api_version,
        labels,
        owners,
        lifecycle,
    )


def _spec(
    raw: Mapping[str, Any], diagnostics: list[StorageDiagnostic]
) -> Mapping[str, Any]:
    value = raw.get("spec")
    if not isinstance(value, Mapping):
        diagnostics.append(StorageDiagnostic("invalid-object-shape", "spec"))
        return {}
    return value


def _capacity(
    value: Any,
    *,
    path: str,
    source: Literal["pvc-requested-storage", "pv-provisioned-capacity"],
    algorithm: str,
    diagnostics: list[StorageDiagnostic],
) -> tuple[StorageCapacity | None, NormalizedQuantity | None]:
    try:
        normalized = normalize_byte_quantity(value, resource="storage")
    except QuantityNormalizationError as exc:
        diagnostics.append(StorageDiagnostic(exc.code, path))
        return None, None
    return (
        StorageCapacity(
            storage_bytes=normalized.normalized_value,
            source=source,
            measurement_algorithm=algorithm,
        ),
        normalized,
    )


def _storage_profile(
    spec: Mapping[str, Any], diagnostics: list[StorageDiagnostic]
) -> tuple[str | None, tuple[str, ...], str | None]:
    storage_class = _safe_optional_identifier(
        spec.get("storageClassName"),
        path="spec.storageClassName",
        diagnostics=diagnostics,
    )
    volume_mode = _safe_optional_identifier(
        spec.get("volumeMode"),
        path="spec.volumeMode",
        diagnostics=diagnostics,
        maximum=64,
    )
    modes_value = spec.get("accessModes", [])
    if modes_value is None:
        modes_value = []
    modes: set[str] = set()
    if not isinstance(modes_value, Sequence) or isinstance(
        modes_value, (str, bytes, bytearray)
    ):
        diagnostics.append(
            StorageDiagnostic("invalid-object-shape", "spec.accessModes")
        )
    else:
        for index, mode in enumerate(modes_value[:32]):
            parsed = _safe_optional_identifier(
                mode,
                path=f"spec.accessModes[{index}]",
                diagnostics=diagnostics,
                maximum=64,
            )
            if parsed is not None:
                modes.add(parsed)
        if len(modes_value) > 32:
            diagnostics.append(StorageDiagnostic("field-limit", "spec.accessModes"))
    return storage_class, tuple(sorted(modes)), volume_mode


def _mapping_child(
    parent: Mapping[str, Any],
    name: str,
    *,
    path: str,
    diagnostics: list[StorageDiagnostic],
) -> Mapping[str, Any]:
    value = parent.get(name)
    if not isinstance(value, Mapping):
        diagnostics.append(StorageDiagnostic("invalid-object-shape", path))
        return {}
    return value


def durable_volume_source_uid(
    *,
    source_cluster: str,
    csi_driver: str,
    volume_handle: str,
    identity_key: bytes | str,
    identity_key_version: str,
) -> str:
    """Derive a stable CSI asset ID without exposing handle or key material."""

    if not isinstance(source_cluster, str) or not _CLUSTER_ID.fullmatch(source_cluster):
        raise StorageNormalizationError("invalid-source-cluster")
    if not isinstance(identity_key_version, str) or not _KEY_VERSION.fullmatch(
        identity_key_version
    ):
        raise StorageNormalizationError("invalid-volume-identity-key-version")
    _required_identity_text(csi_driver, "spec.csi.driver", maximum=253)
    if (
        not isinstance(volume_handle, str)
        or not volume_handle
        or len(volume_handle.encode("utf-8")) > 8192
    ):
        raise StorageNormalizationError("invalid-spec-csi-volume-handle")
    key_bytes = _identity_key_bytes(identity_key)

    # Canonical JSON length/framing prevents concatenation ambiguity while the
    # keyed digest keeps even a sensitive provider identifier out of storage.
    message = json.dumps(
        [identity_key_version, source_cluster, csi_driver, volume_handle],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hmac.new(
        key_bytes,
        _CSI_HMAC_CONTEXT + message,
        hashlib.sha256,
    ).hexdigest()
    return digest


def _identity_key_bytes(identity_key: bytes | str) -> bytes:
    if isinstance(identity_key, str):
        key_bytes = identity_key.encode("utf-8")
    elif isinstance(identity_key, bytes):
        key_bytes = identity_key
    else:
        raise StorageNormalizationError("invalid-volume-identity-key")
    if len(key_bytes) < 32:
        raise StorageNormalizationError("invalid-volume-identity-key")
    return key_bytes


def volume_identity_key_fingerprint(identity_key: bytes | str) -> str:
    """Return non-secret provenance for detecting same-version key drift."""

    return hmac.new(
        _identity_key_bytes(identity_key),
        _KEY_FINGERPRINT_CONTEXT,
        hashlib.sha256,
    ).hexdigest()


def normalize_persistent_volume_claim(
    raw: Mapping[str, Any],
) -> NormalizedPersistentVolumeClaim:
    """Normalize one PVC into claim-requested inventory."""

    if not isinstance(raw, Mapping):
        raise StorageNormalizationError("invalid-pvc-object")
    diagnostics: list[StorageDiagnostic] = []
    (
        uid,
        namespace,
        name,
        resource_version,
        api_version,
        labels,
        owner_references,
        lifecycle,
    ) = _metadata(
        raw,
        expected_kind="PersistentVolumeClaim",
        namespaced=True,
        diagnostics=diagnostics,
    )
    assert namespace is not None
    spec = _spec(raw, diagnostics)
    resources = _mapping_child(
        spec,
        "resources",
        path="spec.resources",
        diagnostics=diagnostics,
    )
    requests = _mapping_child(
        resources,
        "requests",
        path="spec.resources.requests",
        diagnostics=diagnostics,
    )
    capacity, evidence = _capacity(
        requests.get("storage"),
        path="spec.resources.requests.storage",
        source="pvc-requested-storage",
        algorithm=PVC_STORAGE_REQUEST_ALGORITHM,
        diagnostics=diagnostics,
    )
    storage_class, access_modes, volume_mode = _storage_profile(spec, diagnostics)
    bound_volume_name = _safe_optional_identifier(
        spec.get("volumeName"),
        path="spec.volumeName",
        diagnostics=diagnostics,
    )
    return NormalizedPersistentVolumeClaim(
        uid=uid,
        namespace=namespace,
        name=name,
        resource_version=resource_version,
        api_version=api_version,
        labels=labels,
        owner_references=owner_references,
        lifecycle=lifecycle,
        capacity=capacity,
        capacity_evidence=evidence,
        storage_class=storage_class,
        access_modes=access_modes,
        volume_mode=volume_mode,
        bound_volume_name=bound_volume_name,
        diagnostics=tuple(diagnostics),
        valid_for_metering=capacity is not None,
    )


def _pv_finalizers(
    raw: Mapping[str, Any], diagnostics: list[StorageDiagnostic]
) -> tuple[str, ...]:
    metadata = raw.get("metadata")
    assert isinstance(metadata, Mapping)  # already guaranteed by _metadata
    value = metadata.get("finalizers", [])
    if value is None:
        value = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        diagnostics.append(
            StorageDiagnostic("invalid-metadata-field", "metadata.finalizers")
        )
        return ()
    return tuple(
        sorted(
            {
                item
                for item in value
                if isinstance(item, str) and item in ALLOWED_PV_FINALIZERS
            }
        )
    )


def _claim_reference(
    spec: Mapping[str, Any], diagnostics: list[StorageDiagnostic]
) -> VolumeClaimReference | None:
    value = spec.get("claimRef")
    if value in (None, {}):
        return None
    if not isinstance(value, Mapping):
        diagnostics.append(StorageDiagnostic("invalid-object-shape", "spec.claimRef"))
        return None
    try:
        return VolumeClaimReference(
            uid=_required_identity_text(
                value.get("uid"), "spec.claimRef.uid", maximum=256
            ),
            namespace=_required_identity_text(
                value.get("namespace"), "spec.claimRef.namespace", maximum=253
            ),
            name=_required_identity_text(
                value.get("name"), "spec.claimRef.name", maximum=253
            ),
        )
    except StorageNormalizationError:
        diagnostics.append(
            StorageDiagnostic("invalid-claim-reference", "spec.claimRef")
        )
        return None


def normalize_persistent_volume(
    raw: Mapping[str, Any],
    *,
    source_cluster: str,
    identity_key: bytes | str,
    identity_key_version: str,
) -> NormalizedPersistentVolume:
    """Normalize one PV into independent physical-volume inventory."""

    if not isinstance(raw, Mapping):
        raise StorageNormalizationError("invalid-pv-object")
    if not isinstance(source_cluster, str) or not _CLUSTER_ID.fullmatch(source_cluster):
        raise StorageNormalizationError("invalid-source-cluster")
    if not isinstance(identity_key_version, str) or not _KEY_VERSION.fullmatch(
        identity_key_version
    ):
        raise StorageNormalizationError("invalid-volume-identity-key-version")
    identity_key_fingerprint = volume_identity_key_fingerprint(identity_key)
    diagnostics: list[StorageDiagnostic] = []
    (
        pv_uid,
        namespace,
        name,
        resource_version,
        api_version,
        labels,
        owner_references,
        lifecycle,
    ) = _metadata(
        raw,
        expected_kind="PersistentVolume",
        namespaced=False,
        diagnostics=diagnostics,
    )
    assert namespace is None
    spec = _spec(raw, diagnostics)
    capacity_map = _mapping_child(
        spec,
        "capacity",
        path="spec.capacity",
        diagnostics=diagnostics,
    )
    capacity, evidence = _capacity(
        capacity_map.get("storage"),
        path="spec.capacity.storage",
        source="pv-provisioned-capacity",
        algorithm=PV_STORAGE_CAPACITY_ALGORITHM,
        diagnostics=diagnostics,
    )
    storage_class, access_modes, volume_mode = _storage_profile(spec, diagnostics)
    reclaim_policy = _safe_optional_identifier(
        spec.get("persistentVolumeReclaimPolicy"),
        path="spec.persistentVolumeReclaimPolicy",
        diagnostics=diagnostics,
        maximum=64,
    )
    claim_reference = _claim_reference(spec, diagnostics)
    finalizers = _pv_finalizers(raw, diagnostics)

    csi_value = spec.get("csi")
    if csi_value is None:
        identity_scheme: Literal["csi-hmac-sha256-v1", "pv-uid-v1"] = (
            PV_UID_IDENTITY_SCHEME
        )
        source_uid = pv_uid
        csi_driver = None
    else:
        if not isinstance(csi_value, Mapping):
            raise StorageNormalizationError("invalid-spec-csi")
        csi_driver = _required_identity_text(
            csi_value.get("driver"), "spec.csi.driver", maximum=253
        )
        volume_handle = csi_value.get("volumeHandle")
        if not isinstance(volume_handle, str):
            raise StorageNormalizationError("invalid-spec-csi-volume-handle")
        source_uid = durable_volume_source_uid(
            source_cluster=source_cluster,
            csi_driver=csi_driver,
            volume_handle=volume_handle,
            identity_key=identity_key,
            identity_key_version=identity_key_version,
        )
        identity_scheme = CSI_VOLUME_IDENTITY_SCHEME

    return NormalizedPersistentVolume(
        uid=source_uid,
        pv_uid=pv_uid,
        name=name,
        resource_version=resource_version,
        api_version=api_version,
        identity_scheme=identity_scheme,
        identity_key_version=identity_key_version,
        identity_key_fingerprint=identity_key_fingerprint,
        labels=labels,
        owner_references=owner_references,
        lifecycle=lifecycle,
        capacity=capacity,
        capacity_evidence=evidence,
        storage_class=storage_class,
        access_modes=access_modes,
        volume_mode=volume_mode,
        reclaim_policy=reclaim_policy,
        finalizers=finalizers,
        claim_reference=claim_reference,
        csi_driver=csi_driver,
        diagnostics=tuple(diagnostics),
        valid_for_metering=capacity is not None,
    )


# Concise aliases used by scope dispatchers.
normalize_pvc = normalize_persistent_volume_claim
normalize_pv = normalize_persistent_volume


__all__ = [
    "ALLOWED_PV_FINALIZERS",
    "ALLOWED_STORAGE_LABELS",
    "CSI_VOLUME_IDENTITY_SCHEME",
    "PVC_STORAGE_REQUEST_ALGORITHM",
    "PV_STORAGE_CAPACITY_ALGORITHM",
    "PV_UID_IDENTITY_SCHEME",
    "NormalizedPV",
    "NormalizedPVC",
    "NormalizedPersistentVolume",
    "NormalizedPersistentVolumeClaim",
    "StorageCapacity",
    "StorageDiagnostic",
    "StorageLifecycle",
    "StorageNormalizationError",
    "StorageOwnerReference",
    "VolumeClaimReference",
    "durable_volume_source_uid",
    "normalize_persistent_volume",
    "normalize_persistent_volume_claim",
    "normalize_pv",
    "normalize_pvc",
    "volume_identity_key_fingerprint",
]
