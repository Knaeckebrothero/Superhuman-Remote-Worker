from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm-vm-cluster"
DEVELOP_WORKFLOW = ROOT / ".github" / "workflows" / "develop.yml"
VM_FLEET = ROOT / "deployment-vms" / "srw-vm-controller" / "fleet.yaml"
MAIN_DEV_VALUES = ROOT / "deployment" / "values-experimental.yaml"


def _base_command() -> list[str]:
    return [
        "helm",
        "template",
        "vmi-metering-test",
        str(CHART),
        "--namespace",
        "vm-control",
        "--set",
        "license.acceptTerms=true",
        "--set-string",
        "orchestratorId=srw-test",
        "--set-string",
        "nats.hubUrl=nats://nats.example:4222",
        "--set-string",
        "headscale.apiKeySecret=headscale-api",
        "--set-string",
        "ssh.publicKey=ssh-ed25519 AAAATEST vm-test",
        "--set-string",
        "image.orchestrator.tag=sha-component-test",
    ]


def _render(*extra: str) -> list[dict]:
    output = subprocess.run(
        _base_command() + list(extra),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [item for item in yaml.safe_load_all(output) if item]


def _collector_objects(objects: list[dict]) -> list[dict]:
    return [
        item
        for item in objects
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "vmi-metering"
    ]


def _storage_collector_objects(objects: list[dict]) -> list[dict]:
    return [
        item
        for item in objects
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "storage-metering"
    ]


def _storage_inventory_args() -> list[str]:
    return [
        "--set",
        "infrastructureMetering.pvcInventoryEnabled=true",
        "--set-string",
        "infrastructureMetering.vmStableClusterId=vm-cluster-test",
        "--set-string",
        "infrastructureMetering.vmNamespace=vm-workloads",
        "--set-string",
        "infrastructureMetering.orchestratorUrl=https://metering.example.internal",
        "--set-string",
        "infrastructureMetering.storageIngestionSecretName=storage-metering-hmac",
        "--set",
        "infrastructureMetering.networkPolicy.allowUnrestrictedEgress=true",
    ]


def _private_host_alias_args() -> list[str]:
    return [
        "--set-string",
        "infrastructureMetering.orchestratorHostAliases[0].ip=10.0.51.11",
        "--set-string",
        (
            "infrastructureMetering.orchestratorHostAliases[0]."
            "hostnames[0]=api.srw.works"
        ),
    ]


def _env(deployment: dict) -> dict[str, dict]:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry for entry in container["env"]}


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_vm_metering_credentials_select_short_properties_from_srw_secret_bundle():
    objects = _render(
        "--set",
        "externalSecrets.enabled=true",
        "--set",
        "infrastructureMetering.externalSecrets.enabled=true",
        "--set-string",
        (
            "infrastructureMetering.externalSecrets.vaultPath="
            "homelab/superhuman-remote-worker/srw-secrets"
        ),
        "--set-string",
        ("infrastructureMetering.ingestionSecretName=srw-infra-metering-vmi-ingestion"),
        "--set-string",
        (
            "infrastructureMetering.storageIngestionSecretName="
            "srw-infra-metering-vm-storage-ingestion"
        ),
        "--set-string",
        (
            "infrastructureMetering.volumeIdentitySecretName="
            "srw-infra-metering-volume-identity"
        ),
        "--set-string",
        "vmController.lifecycleAuthSecretName=srw-vm-lifecycle-auth",
    )
    names = {
        "srw-infra-metering-vmi-ingestion": (
            "INFRASTRUCTURE_METERING_VMI_INGESTION_KEY",
            "METERING_VMI_INGESTION_KEY",
        ),
        "srw-infra-metering-vm-storage-ingestion": (
            "INFRASTRUCTURE_METERING_VM_STORAGE_INGESTION_KEY",
            "METERING_VM_STORAGE_INGESTION_KEY",
        ),
        "srw-infra-metering-volume-identity": (
            "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY",
            "METERING_VOLUME_IDENTITY_KEY",
        ),
        "srw-vm-lifecycle-auth": (
            "VM_LIFECYCLE_HMAC_SECRET",
            "METERING_VM_LIFECYCLE_HMAC_SECRET",
        ),
    }

    external_secrets = {
        item["metadata"]["name"]: item
        for item in objects
        if item["kind"] == "ExternalSecret" and item["metadata"]["name"] in names
    }
    assert set(external_secrets) == set(names)
    for name, (runtime_key, vault_property) in names.items():
        external_secret = external_secrets[name]
        assert external_secret["spec"]["target"]["name"] == name
        assert external_secret["spec"]["data"] == [
            {
                "secretKey": runtime_key,
                "remoteRef": {
                    "key": "homelab/superhuman-remote-worker/srw-secrets",
                    "property": vault_property,
                },
            }
        ]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_vmi_collector_defaults_dark() -> None:
    objects = _render()

    assert _collector_objects(objects) == []
    assert _storage_collector_objects(objects) == []
    assert not any(
        item["kind"] == "Secret"
        and item.get("metadata", {}).get("name") == "vmi-metering-hmac"
        for item in objects
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_dark_chart_does_not_require_remote_collector_image_identity() -> None:
    objects = _render("--set-string", "image.orchestrator.tag=")

    assert _collector_objects(objects) == []
    assert _storage_collector_objects(objects) == []


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_pre_slice3_reused_values_without_metering_parent_render_dark() -> None:
    output = subprocess.run(
        _base_command()
        + [
            "--skip-schema-validation",
            "--set-json",
            "infrastructureMetering=null",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(output) if item]
    assert _collector_objects(objects) == []
    assert _storage_collector_objects(objects) == []


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_orchestrator_host_alias_is_inert_while_collectors_are_dark() -> None:
    objects = _render(*_private_host_alias_args())

    assert _collector_objects(objects) == []
    assert _storage_collector_objects(objects) == []
    assert all(
        "hostAliases" not in item.get("spec", {}).get("template", {}).get("spec", {})
        for item in objects
        if item.get("kind") == "Deployment"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_private_orchestrator_host_alias_renders_into_both_collectors() -> None:
    objects = _render(
        *_storage_inventory_args(),
        "--set",
        "infrastructureMetering.vmInventoryEnabled=true",
        "--set-string",
        "infrastructureMetering.ingestionSecretName=vmi-metering-hmac",
        "--set-string",
        "infrastructureMetering.orchestratorUrl=https://api.srw.works",
        *_private_host_alias_args(),
    )

    deployments = [
        item
        for item in _collector_objects(objects) + _storage_collector_objects(objects)
        if item["kind"] == "Deployment"
    ]
    assert len(deployments) == 2
    for deployment in deployments:
        assert deployment["spec"]["template"]["spec"]["hostAliases"] == [
            {"ip": "10.0.51.11", "hostnames": ["api.srw.works"]}
        ]
        env = _env(deployment)
        assert env["INFRASTRUCTURE_METERING_ORCHESTRATOR_URL"]["value"] == (
            "https://api.srw.works"
        )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
@pytest.mark.parametrize(
    ("aliases", "message"),
    [
        (
            '{"ip":"10.0.51.11","hostnames":["api.srw.works"]}',
            "orchestratorHostAliases must be a list",
        ),
        (
            '[{"hostnames":["api.srw.works"]}]',
            "must contain only ip and hostnames",
        ),
        (
            '[{"ip":"999.0.51.11","hostnames":["api.srw.works"]}]',
            "must be a canonical IPv4 address",
        ),
        (
            '[{"ip":"10.0.51.11","hostnames":[]}]',
            "hostnames must contain 1 to 16 entries",
        ),
        (
            '[{"ip":"10.0.51.11","hostnames":["HTTPS://api.srw.works"]}]',
            "must be a lowercase DNS hostname",
        ),
        (
            ('[{"ip":"10.0.51.11","hostnames":["api.srw.works","api.srw.works"]}]'),
            "hostnames must not contain duplicates",
        ),
    ],
)
def test_orchestrator_host_alias_invalid_shapes_fail_while_dark(
    aliases: str, message: str
) -> None:
    result = subprocess.run(
        _base_command()
        + [
            "--set-json",
            f"infrastructureMetering.orchestratorHostAliases={aliases}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_vmi_collector_is_database_free_vmi_only_and_fail_closed() -> None:
    objects = _render(
        "--set",
        "infrastructureMetering.vmInventoryEnabled=true",
        "--set-string",
        "infrastructureMetering.vmStableClusterId=vm-cluster-test",
        "--set-string",
        "infrastructureMetering.vmNamespace=vm-workloads",
        "--set-string",
        "infrastructureMetering.orchestratorUrl=https://metering.example.internal",
        "--set-string",
        "infrastructureMetering.ingestionSecretName=vmi-metering-hmac",
        "--set",
        "infrastructureMetering.networkPolicy.allowUnrestrictedEgress=true",
    )
    collector_objects = _collector_objects(objects)

    assert sorted(item["kind"] for item in collector_objects) == [
        "Deployment",
        "Role",
        "RoleBinding",
        "ServiceAccount",
    ]
    assert not any(
        item["kind"] in {"ClusterRole", "ClusterRoleBinding"}
        for item in collector_objects
    )
    assert not any(
        item["kind"] == "Secret"
        and item.get("metadata", {}).get("name") == "vmi-metering-hmac"
        for item in objects
    )

    service_account = next(
        item for item in collector_objects if item["kind"] == "ServiceAccount"
    )
    role = next(item for item in collector_objects if item["kind"] == "Role")
    binding = next(item for item in collector_objects if item["kind"] == "RoleBinding")
    deployment = next(
        item for item in collector_objects if item["kind"] == "Deployment"
    )

    assert service_account["metadata"]["namespace"] == "vm-control"
    assert service_account["automountServiceAccountToken"] is True
    assert role["metadata"]["namespace"] == "vm-workloads"
    assert role["rules"] == [
        {
            "apiGroups": ["kubevirt.io"],
            "resources": ["virtualmachineinstances"],
            "verbs": ["list", "watch"],
        }
    ]
    assert binding["metadata"]["namespace"] == "vm-workloads"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": service_account["metadata"]["name"],
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": service_account["metadata"]["name"],
            "namespace": "vm-control",
        }
    ]

    assert deployment["metadata"]["namespace"] == "vm-control"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == service_account["metadata"]["name"]
    assert pod_spec["automountServiceAccountToken"] is True
    assert pod_spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 999,
        "runAsGroup": 999,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    container = pod_spec["containers"][0]
    assert container["image"] == (
        "ghcr.io/knaeckebrothero/superhuman-remote-worker-orchestrator:"
        "sha-component-test"
    )
    assert container["command"] == [
        "python",
        "-m",
        "services.infrastructure_metering.collector_runtime",
    ]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }

    env = _env(deployment)
    assert env["INFRASTRUCTURE_METERING_COLLECTOR_ID"]["value"] == "kubevirt-vmis"
    assert env["INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID"]["value"] == (
        "vm-cluster-test"
    )
    assert env["INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST"]["value"] == (
        "vm-workloads"
    )
    assert env["INFRASTRUCTURE_METERING_VM_STABLE_CLUSTER_ID"]["value"] == (
        "vm-cluster-test"
    )
    assert env["INFRASTRUCTURE_METERING_VM_NAMESPACE"]["value"] == "vm-workloads"
    assert env["INFRASTRUCTURE_METERING_VM_INVENTORY_ENABLED"]["value"] == "true"
    assert env["INFRASTRUCTURE_METERING_VM_SHADOW_ENABLED"]["value"] == "false"
    assert env["INFRASTRUCTURE_METERING_SHADOW_ENABLED"]["value"] == "false"
    assert env["INFRASTRUCTURE_METERING_VM_PUBLICATION_ENABLED"]["value"] == ("false")
    assert env["INFRASTRUCTURE_METERING_PUBLICATION_ENABLED"]["value"] == "false"
    assert env["INFRASTRUCTURE_METERING_PVC_INVENTORY_ENABLED"]["value"] == ("false")
    assert env["INFRASTRUCTURE_METERING_PV_INVENTORY_ENABLED"]["value"] == "false"
    assert env["INFRASTRUCTURE_METERING_INGESTION_KEY"]["valueFrom"][
        "secretKeyRef"
    ] == {
        "name": "vmi-metering-hmac",
        "key": "INFRASTRUCTURE_METERING_VMI_INGESTION_KEY",
    }
    assert env["POD_UID"]["valueFrom"]["fieldRef"] == {
        "apiVersion": "v1",
        "fieldPath": "metadata.uid",
    }
    assert not any(
        "POSTGRES" in name or "DATABASE" in name or "DATABASE_URL" in name
        for name in env
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_vmi_collector_accepts_an_explicit_orchestrator_digest() -> None:
    digest = "sha256:" + ("a" * 64)
    objects = _render(
        "--set",
        "infrastructureMetering.vmInventoryEnabled=true",
        "--set-string",
        "infrastructureMetering.vmStableClusterId=vm-cluster-test",
        "--set-string",
        "infrastructureMetering.orchestratorUrl=https://metering.example.internal",
        "--set-string",
        "infrastructureMetering.ingestionSecretName=vmi-metering-hmac",
        "--set",
        "infrastructureMetering.networkPolicy.allowUnrestrictedEgress=true",
        "--set-string",
        f"image.orchestrator.digest={digest}",
    )
    deployment = next(
        item for item in _collector_objects(objects) if item["kind"] == "Deployment"
    )

    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "ghcr.io/knaeckebrothero/superhuman-remote-worker-orchestrator@" + digest
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_vmi_shadow_never_enables_publication() -> None:
    objects = _render(
        "--set",
        "infrastructureMetering.vmInventoryEnabled=true",
        "--set",
        "infrastructureMetering.vmShadowEnabled=true",
        "--set-string",
        "infrastructureMetering.vmStableClusterId=vm-cluster-test",
        "--set-string",
        "infrastructureMetering.orchestratorUrl=https://metering.example.internal",
        "--set-string",
        "infrastructureMetering.ingestionSecretName=vmi-metering-hmac",
        "--set",
        "infrastructureMetering.networkPolicy.allowUnrestrictedEgress=true",
    )
    deployment = next(
        item for item in _collector_objects(objects) if item["kind"] == "Deployment"
    )
    env = _env(deployment)

    assert env["INFRASTRUCTURE_METERING_VM_NAMESPACE"]["value"] == "vm-control"
    assert env["INFRASTRUCTURE_METERING_VM_SHADOW_ENABLED"]["value"] == "true"
    assert env["INFRASTRUCTURE_METERING_SHADOW_ENABLED"]["value"] == "true"
    assert env["INFRASTRUCTURE_METERING_VM_PUBLICATION_ENABLED"]["value"] == ("false")
    assert env["INFRASTRUCTURE_METERING_PUBLICATION_ENABLED"]["value"] == "false"


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_vmi_collector_network_policy_allows_only_declared_dependencies() -> None:
    objects = _render(
        "--set",
        "infrastructureMetering.vmInventoryEnabled=true",
        "--set-string",
        "infrastructureMetering.vmStableClusterId=vm-cluster-test",
        "--set-string",
        "infrastructureMetering.orchestratorUrl=https://metering.example.internal",
        "--set-string",
        "infrastructureMetering.ingestionSecretName=vmi-metering-hmac",
        "--set",
        "infrastructureMetering.networkPolicy.enabled=true",
        "--set-string",
        "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
        "--set-string",
        "infrastructureMetering.networkPolicy.orchestratorCidrs[0]=10.0.1.8/32",
    )

    policy = next(
        item for item in _collector_objects(objects) if item["kind"] == "NetworkPolicy"
    )
    assert policy["spec"]["ingress"] == []
    encoded = str(policy["spec"]["egress"])
    assert "10.0.0.1/32" in encoded
    assert "10.0.1.8/32" in encoded
    assert "kube-system" in encoded
    assert "443" in encoded


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_pvc_collector_is_namespace_scoped_and_database_free() -> None:
    objects = _render(*_storage_inventory_args())
    collector_objects = _storage_collector_objects(objects)

    assert sorted(item["kind"] for item in collector_objects) == [
        "Deployment",
        "Role",
        "RoleBinding",
        "ServiceAccount",
    ]
    assert not any(
        item["kind"] in {"ClusterRole", "ClusterRoleBinding"}
        for item in collector_objects
    )
    assert not any(item["kind"] == "Secret" for item in collector_objects)
    assert _collector_objects(objects) == []

    service_account = next(
        item for item in collector_objects if item["kind"] == "ServiceAccount"
    )
    role = next(item for item in collector_objects if item["kind"] == "Role")
    binding = next(item for item in collector_objects if item["kind"] == "RoleBinding")
    deployment = next(
        item for item in collector_objects if item["kind"] == "Deployment"
    )

    assert service_account["metadata"]["namespace"] == "vm-control"
    assert role["metadata"]["namespace"] == "vm-workloads"
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["persistentvolumeclaims"],
            "verbs": ["list", "watch"],
        }
    ]
    assert binding["metadata"]["namespace"] == "vm-workloads"
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": service_account["metadata"]["name"],
            "namespace": "vm-control",
        }
    ]

    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    env = _env(deployment)
    assert env["INFRASTRUCTURE_METERING_COLLECTOR_ID"]["value"] == ("kubevirt-storage")
    assert env["INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID"]["value"] == (
        "vm-cluster-test"
    )
    assert env["INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST"]["value"] == (
        "vm-workloads"
    )
    assert env["INFRASTRUCTURE_METERING_VM_PVC_INVENTORY_ENABLED"]["value"] == ("true")
    assert env["INFRASTRUCTURE_METERING_VM_PV_INVENTORY_ENABLED"]["value"] == ("false")
    assert env["INFRASTRUCTURE_METERING_INGESTION_KEY"]["valueFrom"][
        "secretKeyRef"
    ] == {
        "name": "storage-metering-hmac",
        "key": "INFRASTRUCTURE_METERING_VM_STORAGE_INGESTION_KEY",
    }
    assert "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY" not in env
    for gate in (
        "INFRASTRUCTURE_METERING_SHADOW_ENABLED",
        "INFRASTRUCTURE_METERING_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_V2_READS_ENABLED",
        "INFRASTRUCTURE_METERING_SOURCE_AWARE_READS_ENABLED",
        "INFRASTRUCTURE_METERING_CUTOVER_ENABLED",
        "INFRASTRUCTURE_METERING_VM_INVENTORY_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_PVC_INVENTORY_ENABLED",
        "INFRASTRUCTURE_METERING_PV_INVENTORY_ENABLED",
        "INFRASTRUCTURE_METERING_PVC_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_PV_PUBLICATION_ENABLED",
    ):
        assert env[gate]["value"] == "false"
    assert not any(
        "POSTGRES" in name or "DATABASE" in name or "DATABASE_URL" in name
        for name in env
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_pv_collector_requires_ack_and_uses_opaque_identity_secret() -> None:
    arguments = _storage_inventory_args()
    arguments[1] = "infrastructureMetering.pvInventoryEnabled=true"
    objects = _render(
        *arguments,
        "--set",
        "infrastructureMetering.pvClusterWideRbacAcknowledged=true",
        "--set-string",
        "infrastructureMetering.volumeIdentitySecretName=volume-identity",
        "--set-string",
        "infrastructureMetering.volumeIdentityKeyVersion=storage-v1",
    )
    collector_objects = _storage_collector_objects(objects)

    assert sorted(item["kind"] for item in collector_objects) == [
        "ClusterRole",
        "ClusterRoleBinding",
        "Deployment",
        "ServiceAccount",
    ]
    assert not any(
        item["kind"] in {"Role", "RoleBinding"} for item in collector_objects
    )
    cluster_role = next(
        item for item in collector_objects if item["kind"] == "ClusterRole"
    )
    assert cluster_role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["persistentvolumes"],
            "verbs": ["list", "watch"],
        }
    ]
    cluster_binding = next(
        item for item in collector_objects if item["kind"] == "ClusterRoleBinding"
    )
    assert cluster_binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": cluster_role["metadata"]["name"],
    }

    deployment = next(
        item for item in collector_objects if item["kind"] == "Deployment"
    )
    env = _env(deployment)
    assert env["INFRASTRUCTURE_METERING_VM_PVC_INVENTORY_ENABLED"]["value"] == ("false")
    assert env["INFRASTRUCTURE_METERING_VM_PV_INVENTORY_ENABLED"]["value"] == ("true")
    assert (
        env["INFRASTRUCTURE_METERING_VM_PV_CLUSTER_WIDE_RBAC_ACKNOWLEDGED"]["value"]
        == "true"
    )
    assert env["INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY"]["valueFrom"][
        "secretKeyRef"
    ] == {
        "name": "volume-identity",
        "key": "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY",
    }
    assert env["INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY_VERSION"]["value"] == (
        "storage-v1"
    )
    # Secret references are expected, but the chart must never render/copy the
    # key itself or grant generic Secret reads.
    assert not any(item["kind"] == "Secret" for item in collector_objects)
    assert "secrets" not in yaml.safe_dump(cluster_role["rules"])


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_storage_shadow_cannot_enable_publication_or_serving() -> None:
    objects = _render(
        *_storage_inventory_args(),
        "--set",
        "infrastructureMetering.pvInventoryEnabled=true",
        "--set",
        "infrastructureMetering.pvcShadowEnabled=true",
        "--set",
        "infrastructureMetering.pvShadowEnabled=true",
        "--set",
        "infrastructureMetering.pvClusterWideRbacAcknowledged=true",
        "--set-string",
        "infrastructureMetering.volumeIdentitySecretName=volume-identity",
        "--set-string",
        "infrastructureMetering.volumeIdentityKeyVersion=storage-v1",
    )
    deployment = next(
        item
        for item in _storage_collector_objects(objects)
        if item["kind"] == "Deployment"
    )
    env = _env(deployment)

    assert env["INFRASTRUCTURE_METERING_VM_PVC_SHADOW_ENABLED"]["value"] == "true"
    assert env["INFRASTRUCTURE_METERING_VM_PV_SHADOW_ENABLED"]["value"] == "true"
    assert env["INFRASTRUCTURE_METERING_SHADOW_ENABLED"]["value"] == "true"
    for gate in (
        "INFRASTRUCTURE_METERING_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_V2_READS_ENABLED",
        "INFRASTRUCTURE_METERING_SOURCE_AWARE_READS_ENABLED",
        "INFRASTRUCTURE_METERING_CUTOVER_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_PVC_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_PV_PUBLICATION_ENABLED",
    ):
        assert env[gate]["value"] == "false"


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_storage_network_policy_allows_only_declared_dependencies() -> None:
    arguments = _storage_inventory_args()
    arguments[-1] = "infrastructureMetering.networkPolicy.enabled=true"
    objects = _render(
        *arguments,
        "--set-string",
        "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
        "--set-string",
        "infrastructureMetering.networkPolicy.orchestratorCidrs[0]=10.0.1.8/32",
    )
    policy = next(
        item
        for item in _storage_collector_objects(objects)
        if item["kind"] == "NetworkPolicy"
    )

    assert policy["spec"]["ingress"] == []
    encoded = str(policy["spec"]["egress"])
    assert "10.0.0.1/32" in encoded
    assert "10.0.1.8/32" in encoded
    assert "kube-system" in encoded
    assert "443" in encoded


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            [
                "--set",
                "infrastructureMetering.vmInventoryEnabled=true",
                "--set-string",
                "infrastructureMetering.orchestratorUrl=https://metering.internal",
                "--set-string",
                "infrastructureMetering.ingestionSecretName=vmi-hmac",
            ],
            "requires vmStableClusterId",
        ),
        (
            [
                "--set",
                "infrastructureMetering.vmInventoryEnabled=true",
                "--set-string",
                "infrastructureMetering.vmStableClusterId=vm-cluster-test",
                "--set-string",
                "infrastructureMetering.ingestionSecretName=vmi-hmac",
            ],
            "requires orchestratorUrl",
        ),
        (
            [
                "--set",
                "infrastructureMetering.vmInventoryEnabled=true",
                "--set-string",
                "infrastructureMetering.vmStableClusterId=vm-cluster-test",
                "--set-string",
                "infrastructureMetering.orchestratorUrl=https://metering.internal",
                "--set",
                "infrastructureMetering.networkPolicy.allowUnrestrictedEgress=true",
            ],
            "requires a distinct pre-existing ingestionSecretName",
        ),
        (
            ["--set", "infrastructureMetering.vmShadowEnabled=true"],
            "vmShadowEnabled requires vmInventoryEnabled",
        ),
        (
            [
                "--set",
                "infrastructureMetering.vmInventoryEnabled=true",
                "--set-string",
                "infrastructureMetering.vmStableClusterId=vm-cluster-test",
                "--set-string",
                "infrastructureMetering.orchestratorUrl=https://metering.internal",
                "--set-string",
                "infrastructureMetering.ingestionSecretName=vmi-hmac",
            ],
            "requires networkPolicy.enabled or explicit",
        ),
        (
            [
                "--set",
                "infrastructureMetering.vmInventoryEnabled=true",
                "--set-string",
                "infrastructureMetering.vmStableClusterId=vm-cluster-test",
                "--set-string",
                "infrastructureMetering.orchestratorUrl=https://metering.internal",
                "--set-string",
                "infrastructureMetering.ingestionSecretName=vmi-hmac",
                "--set",
                "infrastructureMetering.networkPolicy.enabled=true",
            ],
            "networkPolicy.enabled requires apiServerCidrs",
        ),
        (
            [
                "--set",
                "infrastructureMetering.vmInventoryEnabled=true",
                "--set-string",
                "infrastructureMetering.vmStableClusterId=vm-cluster-test",
                "--set-string",
                "infrastructureMetering.orchestratorUrl=https://metering.internal",
                "--set-string",
                "infrastructureMetering.ingestionSecretName=vmi-hmac",
                "--set",
                "infrastructureMetering.networkPolicy.enabled=true",
                "--set-string",
                "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
            ],
            "networkPolicy.enabled requires orchestratorCidrs",
        ),
        (
            [
                "--set",
                "infrastructureMetering.vmInventoryEnabled=true",
                "--set-string",
                "infrastructureMetering.vmStableClusterId=vm-cluster-test",
                "--set-string",
                "infrastructureMetering.orchestratorUrl=https://metering.internal",
                "--set-string",
                "infrastructureMetering.ingestionSecretName=vmi-hmac",
                "--set",
                "infrastructureMetering.networkPolicy.allowUnrestrictedEgress=true",
                "--set-string",
                "image.orchestrator.tag=",
            ],
            "requires an explicit image.orchestrator.tag or image.orchestrator.digest",
        ),
        (
            [
                "--set",
                "infrastructureMetering.vmInventoryEnabled=true",
                "--set-string",
                "infrastructureMetering.vmStableClusterId=vm-cluster-test",
                "--set-string",
                "infrastructureMetering.orchestratorUrl=https://metering.internal",
                "--set-string",
                "infrastructureMetering.ingestionSecretName=vmi-hmac",
                "--set",
                "infrastructureMetering.networkPolicy.allowUnrestrictedEgress=true",
                "--set-string",
                "image.orchestrator.tag=",
                "--set-string",
                "image.orchestrator.digest=sha256:not-a-digest",
            ],
            "digest must be a sha256 OCI digest",
        ),
    ],
)
def test_vmi_collector_invalid_config_fails_render(
    extra: list[str], message: str
) -> None:
    result = subprocess.run(
        _base_command() + extra,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_storage_invalid_config_fails_render() -> None:
    base = _storage_inventory_args()
    cases = [
        (
            [
                "--set",
                "infrastructureMetering.pvcInventoryEnabled=false",
                "--set",
                "infrastructureMetering.pvcShadowEnabled=true",
            ],
            "pvcShadowEnabled requires pvcInventoryEnabled",
        ),
        (
            [
                "--set-string",
                "infrastructureMetering.storageIngestionSecretName=",
            ],
            "requires a distinct pre-existing storageIngestionSecretName",
        ),
        (
            [
                "--set",
                "infrastructureMetering.pvInventoryEnabled=true",
            ],
            "requires pvClusterWideRbacAcknowledged=true",
        ),
        (
            [
                "--set",
                "infrastructureMetering.pvInventoryEnabled=true",
                "--set",
                "infrastructureMetering.pvClusterWideRbacAcknowledged=true",
            ],
            "requires a pre-existing volumeIdentitySecretName",
        ),
        (
            [
                "--set",
                "infrastructureMetering.pvInventoryEnabled=true",
                "--set",
                "infrastructureMetering.pvClusterWideRbacAcknowledged=true",
                "--set-string",
                "infrastructureMetering.volumeIdentitySecretName=volume-identity",
            ],
            "requires volumeIdentityKeyVersion",
        ),
        (
            [
                "--set",
                "infrastructureMetering.pvInventoryEnabled=true",
                "--set",
                "infrastructureMetering.pvClusterWideRbacAcknowledged=true",
                "--set-string",
                "infrastructureMetering.volumeIdentitySecretName=storage-metering-hmac",
                "--set-string",
                "infrastructureMetering.volumeIdentityKeyVersion=storage-v1",
            ],
            "volumeIdentitySecretName must be separate from storageIngestionSecretName",
        ),
        (
            [
                "--set",
                "infrastructureMetering.vmInventoryEnabled=true",
                "--set-string",
                "infrastructureMetering.ingestionSecretName=storage-metering-hmac",
            ],
            "storageIngestionSecretName must be separate from ingestionSecretName",
        ),
        (
            [
                "--set-string",
                "infrastructureMetering.orchestratorUrl=http://metering.internal",
            ],
            "orchestratorUrl must use https",
        ),
        (
            [
                "--set-string",
                "image.orchestrator.tag=",
            ],
            "requires an explicit image.orchestrator.tag or image.orchestrator.digest",
        ),
    ]

    for extra, message in cases:
        result = subprocess.run(
            _base_command() + base + extra,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, extra
        assert message in result.stderr, result.stderr


def _workflow_step(name: str) -> str:
    workflow = DEVELOP_WORKFLOW.read_text(encoding="utf-8")
    marker = f"      - name: {name}\n"
    assert marker in workflow
    remainder = workflow.split(marker, 1)[1]
    return marker + remainder.split("\n      - name: ", 1)[0]


def test_develop_release_couples_orchestrator_image_to_vm_cluster_rollout() -> None:
    publish = _workflow_step("Package and push dev vm-cluster chart")
    bump = _workflow_step("Update vm-cluster fleet.yaml chart version")
    orchestrator_pin = _workflow_step("Update orchestrator tag")

    assert "needs.build-orchestrator.result == 'success'" in publish
    assert "needs.build-orchestrator.result == 'success'" in bump
    assert "needs.changes.outputs.orchestrator-sha" in orchestrator_pin
    assert ".helm.values.image.orchestrator.tag = strenv(COMPONENT_TAG)" in (
        orchestrator_pin
    )
    assert '.helm.values.image.orchestrator.digest = ""' in orchestrator_pin
    assert "deployment-vms/srw-vm-controller/fleet.yaml" in orchestrator_pin


def test_vm_cluster_fleet_pins_the_current_orchestrator_component_tag() -> None:
    fleet = yaml.safe_load(VM_FLEET.read_text(encoding="utf-8"))
    main_dev = yaml.safe_load(MAIN_DEV_VALUES.read_text(encoding="utf-8"))
    remote_image = fleet["helm"]["values"]["image"]["orchestrator"]

    assert remote_image["tag"] == main_dev["image"]["orchestrator"]["tag"]
    assert remote_image["tag"].startswith("sha-")
    assert remote_image["digest"] == ""


def test_vm_cluster_fleet_preconfigures_dark_private_metering_route() -> None:
    fleet = yaml.safe_load(VM_FLEET.read_text(encoding="utf-8"))
    metering = fleet["helm"]["values"]["infrastructureMetering"]

    assert metering["vmInventoryEnabled"] is False
    assert metering["pvcInventoryEnabled"] is False
    assert metering["pvInventoryEnabled"] is False
    assert metering["orchestratorHostAliases"] == [
        {"ip": "10.0.51.11", "hostnames": ["api.srw.works"]}
    ]
    assert metering["networkPolicy"]["orchestratorCidrs"] == ["10.0.51.11/32"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_controller_lifecycle_secret_and_nonce_rbac_are_scoped() -> None:
    objects = _render(
        "--set-string",
        "vmController.lifecycleAuthSecretName=vm-lifecycle",
    )
    deployment = next(
        item
        for item in objects
        if item["kind"] == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "vm-controller"
    )
    env = _env(deployment)
    assert env["VM_LIFECYCLE_HMAC_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "vm-lifecycle",
        "key": "VM_LIFECYCLE_HMAC_SECRET",
    }
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    role = next(
        item
        for item in objects
        if item["kind"] == "Role"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "vm-controller"
    )
    assert {
        "apiGroups": ["coordination.k8s.io"],
        "resources": ["leases"],
        "verbs": ["get", "list", "create", "delete"],
    } in role["rules"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_controller_without_lifecycle_auth_omits_nonce_rbac() -> None:
    objects = _render()
    role = next(
        item
        for item in objects
        if item["kind"] == "Role"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "vm-controller"
    )

    assert not any(
        "coordination.k8s.io" in rule.get("apiGroups", [])
        or "leases" in rule.get("resources", [])
        for rule in role["rules"]
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            [
                "--set-string",
                "vmController.lifecycleAuthSecretName=vm-lifecycle",
                "--set",
                "vmController.replicas=2",
            ],
            "requires replicas=1",
        ),
        (
            [
                "--set-string",
                "vmController.lifecycleAuthSecretName=vmi-hmac",
                "--set-string",
                "infrastructureMetering.ingestionSecretName=vmi-hmac",
            ],
            "must be separate from infrastructureMetering.ingestionSecretName",
        ),
    ],
)
def test_remote_controller_lifecycle_auth_miswiring_fails_render(
    extra: list[str], message: str
) -> None:
    result = subprocess.run(
        _base_command() + extra,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert message in result.stderr
