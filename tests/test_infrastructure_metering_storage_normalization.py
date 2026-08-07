from __future__ import annotations

import json

import pytest

from orchestrator.services.infrastructure_metering.collectors.contracts import (
    normalized_payload,
)
from orchestrator.services.infrastructure_metering.collectors.storage_normalization import (
    CSI_VOLUME_IDENTITY_SCHEME,
    PV_UID_IDENTITY_SCHEME,
    StorageNormalizationError,
    durable_volume_source_uid,
    normalize_persistent_volume,
    normalize_persistent_volume_claim,
    volume_identity_key_fingerprint,
)


GIB = 1024**3
IDENTITY_KEY = b"k" * 32
KEY_VERSION = "metering-key-2026-01"


def _pvc(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "uid": "claim-uid",
            "namespace": "srw",
            "name": "pvc-workspace-job-1",
            "resourceVersion": "10",
            "creationTimestamp": "2026-08-01T12:00:00Z",
            "labels": {
                "app": "srw-workspace",
                "srw/job-id": "job-1",
                "srw.io/owner-kind": "job",
                "srw.io/owner-id": "job-1",
                "private.example/token": "must-never-leave-the-normalizer",
            },
            "ownerReferences": [
                {"kind": "DataVolume", "uid": "dv-uid", "name": "root-disk"}
            ],
            "annotations": {"private.example/credential": "secret-value"},
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "20Gi"}},
            "storageClassName": "premium-rwo",
            "volumeMode": "Filesystem",
            "volumeName": "pvc-claim-uid",
            "dataSource": {"kind": "Secret", "name": "do-not-copy"},
        },
        "status": {"phase": "Pending"},
    }
    raw.update(overrides)
    return raw


def _pv(
    *,
    pv_uid: str = "pv-uid-1",
    name: str = "pv-one",
    handle: str = "provider-volume-secret-123",
    phase: str = "Bound",
) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {
            "uid": pv_uid,
            "name": name,
            "resourceVersion": "21",
            "creationTimestamp": "2026-08-01T12:30:00Z",
            "labels": {
                "topology.kubernetes.io/zone": "eu01-1",
                "private.example/customer": "do-not-copy",
            },
            "annotations": {"pv.kubernetes.io/provisioned-by": "private-value"},
            "finalizers": [
                "kubernetes.io/pv-protection",
                "external-provisioner.volume.kubernetes.io/finalizer",
                "private.example/finalizer-that-must-not-be-copied",
            ],
        },
        "spec": {
            "capacity": {"storage": "25Gi"},
            "accessModes": ["ReadWriteOnce"],
            "volumeMode": "Filesystem",
            "storageClassName": "premium-rwo",
            "persistentVolumeReclaimPolicy": "Retain",
            "claimRef": {
                "uid": "claim-uid",
                "namespace": "srw",
                "name": "pvc-workspace-job-1",
            },
            "csi": {
                "driver": "cinder.csi.openstack.org",
                "volumeHandle": handle,
                "volumeAttributes": {
                    "private-token": "csi-attribute-secret",
                },
                "controllerPublishSecretRef": {
                    "name": "csi-controller-secret",
                    "namespace": "kube-system",
                },
            },
        },
        "status": {"phase": phase, "message": "raw-provider-message"},
    }


def test_pvc_projects_requested_capacity_and_only_safe_classification_fields() -> None:
    result = normalize_persistent_volume_claim(_pvc())
    projected = normalized_payload(result)

    assert result.uid == "claim-uid"
    assert result.namespace == "srw"
    assert result.valid_for_metering
    assert result.capacity is not None
    assert result.capacity.storage_bytes == 20 * GIB
    assert result.lifecycle.phase == "Pending"
    assert result.lifecycle.accrues
    assert projected["measurement_basis"] == "claim-requested"
    assert projected["storage_class"] == "premium-rwo"
    assert projected["access_modes"] == ["ReadWriteOnce"]
    assert projected["bound_volume_name"] == "pvc-claim-uid"
    assert projected["labels"] == {
        "app": "srw-workspace",
        "srw/job-id": "job-1",
        "srw.io/owner-id": "job-1",
        "srw.io/owner-kind": "job",
    }
    assert projected["owner_references"] == [
        {"kind": "DataVolume", "uid": "dv-uid", "name": "root-disk"}
    ]

    encoded = json.dumps(projected, sort_keys=True)
    for forbidden in (
        "secret-value",
        "must-never-leave-the-normalizer",
        "do-not-copy",
        "annotations",
        "dataSource",
    ):
        assert forbidden not in encoded


def test_pvc_revision_ignores_transport_churn_but_splits_on_expansion() -> None:
    original = normalize_persistent_volume_claim(_pvc())
    churned_raw = _pvc()
    churned_raw["metadata"] = {
        **churned_raw["metadata"],  # type: ignore[arg-type]
        "resourceVersion": "999",
    }
    churned_raw["status"] = {
        "phase": "Pending",
        "conditions": [{"type": "FileSystemResizePending", "message": "ignored"}],
    }
    churned = normalize_persistent_volume_claim(churned_raw)

    expanded_raw = _pvc()
    expanded_raw["spec"] = {
        **expanded_raw["spec"],  # type: ignore[arg-type]
        "resources": {"requests": {"storage": "30Gi"}},
    }
    expanded = normalize_persistent_volume_claim(expanded_raw)

    assert churned.revision_hash == original.revision_hash
    assert expanded.revision_hash != original.revision_hash


def test_invalid_pvc_capacity_remains_identifiable_but_not_meterable() -> None:
    raw = _pvc()
    raw["spec"] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "-1Gi"}},
    }

    result = normalize_persistent_volume_claim(raw)

    assert result.uid == "claim-uid"
    assert not result.valid_for_metering
    assert result.revision_hash is None
    assert result.capacity is None
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "negative-quantity"
    ]


def test_csi_pv_uses_stable_hmac_across_pv_reimports_without_handle_leakage() -> None:
    first = normalize_persistent_volume(
        _pv(),
        source_cluster="cluster-a",
        identity_key=IDENTITY_KEY,
        identity_key_version=KEY_VERSION,
    )
    imported = normalize_persistent_volume(
        _pv(pv_uid="pv-uid-2", name="pv-imported"),
        source_cluster="cluster-a",
        identity_key=IDENTITY_KEY,
        identity_key_version=KEY_VERSION,
    )
    projected = normalized_payload(first)

    assert first.identity_scheme == CSI_VOLUME_IDENTITY_SCHEME
    assert len(first.uid) == 64
    assert first.uid == imported.uid
    assert first.pv_uid != imported.pv_uid
    assert first.revision_hash != imported.revision_hash
    assert first.capacity is not None
    assert first.capacity.storage_bytes == 25 * GIB
    assert projected["volume_identity"] == {
        "scheme": CSI_VOLUME_IDENTITY_SCHEME,
        "key_version": KEY_VERSION,
        "key_fingerprint": volume_identity_key_fingerprint(IDENTITY_KEY),
        "durable_asset_id": first.uid,
        "pv_uid": "pv-uid-1",
    }
    assert projected["mapping_state"] == "unmapped"
    assert projected["resource"] == "unmapped_block_volume"
    assert projected["lifecycle"]["has_deletion_protection_finalizer"] is True
    assert projected["claim_reference"] == {
        "uid": "claim-uid",
        "namespace": "srw",
        "name": "pvc-workspace-job-1",
    }

    encoded = json.dumps(projected, sort_keys=True)
    for forbidden in (
        "provider-volume-secret-123",
        "csi-attribute-secret",
        "csi-controller-secret",
        "private-value",
        "raw-provider-message",
        "private.example/finalizer",
    ):
        assert forbidden not in encoded


def test_csi_asset_identity_is_bound_to_cluster_driver_key_and_key_version() -> None:
    arguments = {
        "source_cluster": "cluster-a",
        "csi_driver": "driver.example",
        "volume_handle": "volume-one",
        "identity_key": IDENTITY_KEY,
        "identity_key_version": KEY_VERSION,
    }
    baseline = durable_volume_source_uid(**arguments)

    assert baseline != durable_volume_source_uid(
        **{**arguments, "source_cluster": "cluster-b"}
    )
    assert baseline != durable_volume_source_uid(
        **{**arguments, "csi_driver": "other.example"}
    )
    assert baseline != durable_volume_source_uid(
        **{**arguments, "volume_handle": "volume-two"}
    )
    assert baseline != durable_volume_source_uid(
        **{**arguments, "identity_key": b"z" * 32}
    )
    assert baseline != durable_volume_source_uid(
        **{**arguments, "identity_key_version": "metering-key-2027-01"}
    )


def test_identity_key_fingerprint_detects_same_version_key_drift() -> None:
    first = volume_identity_key_fingerprint(IDENTITY_KEY)

    assert len(first) == 64
    assert first == volume_identity_key_fingerprint(IDENTITY_KEY)
    assert first != volume_identity_key_fingerprint(b"z" * 32)


def test_non_csi_pv_falls_back_to_pv_uid_without_copying_volume_source() -> None:
    raw = _pv()
    spec = dict(raw["spec"])  # type: ignore[arg-type]
    spec.pop("csi")
    spec["local"] = {"path": "/private/provider/path"}
    raw["spec"] = spec

    result = normalize_persistent_volume(
        raw,
        source_cluster="cluster-a",
        identity_key=IDENTITY_KEY,
        identity_key_version=KEY_VERSION,
    )
    projected = normalized_payload(result)

    assert result.identity_scheme == PV_UID_IDENTITY_SCHEME
    assert result.uid == "pv-uid-1"
    assert projected["volume_identity"]["key_version"] == KEY_VERSION
    assert projected["volume_identity"]["key_fingerprint"] == (
        volume_identity_key_fingerprint(IDENTITY_KEY)
    )
    assert "/private/provider/path" not in json.dumps(projected)


@pytest.mark.parametrize(
    ("identity_key", "key_version", "error"),
    [
        (b"short", KEY_VERSION, "invalid-volume-identity-key"),
        (IDENTITY_KEY, "bad version", "invalid-volume-identity-key-version"),
    ],
)
def test_csi_pv_rejects_invalid_identity_configuration(
    identity_key: bytes, key_version: str, error: str
) -> None:
    with pytest.raises(StorageNormalizationError, match=error):
        normalize_persistent_volume(
            _pv(),
            source_cluster="cluster-a",
            identity_key=identity_key,
            identity_key_version=key_version,
        )


def test_pv_release_splits_attribution_revision() -> None:
    bound = normalize_persistent_volume(
        _pv(),
        source_cluster="cluster-a",
        identity_key=IDENTITY_KEY,
        identity_key_version=KEY_VERSION,
    )
    released_raw = _pv(phase="Released")
    released_spec = dict(released_raw["spec"])  # type: ignore[arg-type]
    released_spec.pop("claimRef")
    released_raw["spec"] = released_spec
    released = normalize_persistent_volume(
        released_raw,
        source_cluster="cluster-a",
        identity_key=IDENTITY_KEY,
        identity_key_version=KEY_VERSION,
    )

    assert released.uid == bound.uid
    assert released.claim_reference is None
    assert released.revision_hash != bound.revision_hash
