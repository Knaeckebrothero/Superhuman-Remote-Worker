from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_orchestrator_images_bake_slice3_metering_package_atomically():
    import_check = (
        'RUN python -c "import services.infrastructure_metering.compute_activation"'
    )
    for relative_path in (
        "docker/Dockerfile.orchestrator",
        "docker/Dockerfile.orchestrator.dev",
    ):
        dockerfile = (ROOT / relative_path).read_text(encoding="utf-8")
        assert import_check in dockerfile

    tiltfile = (ROOT / "Tiltfile").read_text(encoding="utf-8")
    orchestrator_build = tiltfile.split("docker_build(\n    'srw-orchestrator'", 1)[
        1
    ].split(
        "# -----------------------------------------------------------------------------",
        1,
    )[0]
    fallback_paths = orchestrator_build.split("fall_back_on([", 1)[1].split("]),", 1)[0]
    assert "'orchestrator/services/infrastructure_metering/'" in fallback_paths


def _render_remote_vmi_ingress(*extra: str) -> list[dict]:
    chart = ROOT / "helm"
    values = chart / "ci/test-values.yaml"
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "metering-remote",
            str(chart),
            "-f",
            str(values),
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
            "--set",
            "infrastructureMetering.networkPolicy.enabled=true",
            "--set-string",
            "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
            "--set",
            "infrastructureMetering.vmInventoryEnabled=true",
            "--set-string",
            "infrastructureMetering.vmStableClusterId=vm-cluster",
            "--set-string",
            "infrastructureMetering.vmNamespace=agent-vms",
            "--set-string",
            "infrastructureMetering.vmIngestionSecretName=vmi-metering-key",
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [item for item in yaml.safe_load_all(rendered) if item]


def _render_remote_storage_server(*extra: str) -> list[dict]:
    chart = ROOT / "helm"
    values = chart / "ci/test-values.yaml"
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "metering-remote-storage",
            str(chart),
            "-f",
            str(values),
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
            "--set",
            "infrastructureMetering.networkPolicy.enabled=true",
            "--set-string",
            "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
            "--set",
            "infrastructureMetering.vmPvcInventoryEnabled=true",
            "--set-string",
            "infrastructureMetering.vmStableClusterId=vm-cluster",
            "--set-string",
            "infrastructureMetering.vmNamespace=agent-vms",
            "--set-string",
            "infrastructureMetering.vmStorageIngestionSecretName=vm-storage-metering-key",
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [item for item in yaml.safe_load_all(rendered) if item]


def _render_metering_external_secrets(*extra: str) -> list[dict]:
    chart = ROOT / "helm"
    values = chart / "ci/test-values.yaml"
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "metering-vault",
            str(chart),
            "-f",
            str(values),
            "--set",
            "infrastructureMetering.externalSecrets.enabled=true",
            "--set-string",
            (
                "infrastructureMetering.externalSecrets.vaultPath="
                "homelab/superhuman-remote-worker/srw-secrets"
            ),
            "--set-string",
            (
                "infrastructureMetering.vmIngestionSecretName="
                "srw-infra-metering-vmi-ingestion"
            ),
            "--set-string",
            (
                "infrastructureMetering.vmStorageIngestionSecretName="
                "srw-infra-metering-vm-storage-ingestion"
            ),
            "--set-string",
            (
                "infrastructureMetering.volumeIdentitySecretName="
                "srw-infra-metering-volume-identity"
            ),
            "--set-string",
            ("infrastructureMetering.vmLifecycleAuthSecretName=srw-vm-lifecycle-auth"),
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [item for item in yaml.safe_load_all(rendered) if item]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_metering_credentials_select_short_properties_from_srw_secret_bundle():
    objects = _render_metering_external_secrets()
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


def test_metering_external_secrets_defaults_stay_dark_with_stable_properties():
    """Chart contract only. The dev instance's posture (vault path, secret
    names, all inventory/shadow flags pinned dark) moved to the HomeLab
    values ConfigMap with the HelmOp cutover and is guarded there, not here.
    The property names below are the ESO bundle convention shared with the
    hand-minted ingestion Secrets — renaming them breaks live clusters."""
    expected_properties = {
        "vmiIngestionProperty": "METERING_VMI_INGESTION_KEY",
        "vmStorageIngestionProperty": "METERING_VM_STORAGE_INGESTION_KEY",
        "volumeIdentityProperty": "METERING_VOLUME_IDENTITY_KEY",
        "vmLifecycleAuthProperty": "METERING_VM_LIFECYCLE_HMAC_SECRET",
    }
    chart_defaults = yaml.safe_load(
        (ROOT / "helm/values.yaml").read_text(encoding="utf-8")
    )
    sync_defaults = chart_defaults["infrastructureMetering"]["externalSecrets"]
    assert sync_defaults["enabled"] is False
    assert {
        key: sync_defaults[key] for key in expected_properties
    } == expected_properties


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_dedicated_metering_collector_renders_only_with_scoped_pod_reads():
    chart = ROOT / "helm"
    values = chart / "ci/test-values.yaml"
    base = ["helm", "template", "metering-test", str(chart), "-f", str(values)]

    disabled = subprocess.run(
        base,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "app.kubernetes.io/component: infra-collector" not in disabled
    disabled_objects = [item for item in yaml.safe_load_all(disabled) if item]
    disabled_orchestrator = next(
        item
        for item in disabled_objects
        if item["kind"] == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "orchestrator"
    )
    disabled_env = {
        entry["name"]: entry
        for entry in disabled_orchestrator["spec"]["template"]["spec"]["containers"][0][
            "env"
        ]
    }
    disabled_configmap = next(
        item
        for item in disabled_objects
        if item["kind"] == "ConfigMap"
        and "INFRASTRUCTURE_METERING_MAX_SNAPSHOT_BYTES" in item.get("data", {})
    )
    assert (
        disabled_configmap["data"]["INFRASTRUCTURE_METERING_MAX_SNAPSHOT_BYTES"]
        == "67108864"
    )
    assert (
        disabled_configmap["data"][
            "INFRASTRUCTURE_METERING_MAX_COLLECTOR_CLOCK_SKEW_SECONDS"
        ]
        == "300"
    )
    assert (
        disabled_configmap["data"]["INFRASTRUCTURE_METERING_SOURCE_AWARE_READS_ENABLED"]
        == "false"
    )
    assert (
        disabled_configmap["data"]["INFRASTRUCTURE_METERING_CUTOVER_ENABLED"] == "false"
    )
    for key in (
        "INFRASTRUCTURE_METERING_PVC_INVENTORY_ENABLED",
        "INFRASTRUCTURE_METERING_PV_INVENTORY_ENABLED",
        "INFRASTRUCTURE_METERING_PVC_SHADOW_ENABLED",
        "INFRASTRUCTURE_METERING_PV_SHADOW_ENABLED",
        "INFRASTRUCTURE_METERING_PVC_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_PV_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_IDE_POD_SHADOW_ENABLED",
        "INFRASTRUCTURE_METERING_AGENT_POD_SHADOW_ENABLED",
        "INFRASTRUCTURE_METERING_IDE_POD_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_AGENT_POD_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_VM_INVENTORY_ENABLED",
        "INFRASTRUCTURE_METERING_VM_SHADOW_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PVC_INVENTORY_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PV_INVENTORY_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PVC_SHADOW_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PV_SHADOW_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PVC_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PV_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PV_CLUSTER_WIDE_RBAC_ACKNOWLEDGED",
    ):
        assert disabled_configmap["data"][key] == "false"
    assert (
        disabled_configmap["data"][
            "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY_VERSION"
        ]
        == ""
    )
    assert (
        disabled_configmap["data"][
            "INFRASTRUCTURE_METERING_VOLUME_RESOURCE_MAPPINGS_JSON"
        ]
        == "[]"
    )
    assert (
        disabled_configmap["data"]["INFRASTRUCTURE_METERING_VM_STABLE_CLUSTER_ID"] == ""
    )
    assert disabled_configmap["data"]["INFRASTRUCTURE_METERING_VM_NAMESPACE"] == ""
    assert (
        disabled_env["INFRASTRUCTURE_METERING_INGESTION_KEY"]["valueFrom"][
            "secretKeyRef"
        ]["optional"]
        is True
    )

    enabled = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set",
            "infrastructureMetering.shadowEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
            "--set-string",
            "infrastructureMetering.namespaceAllowlist[0]=agent-space",
            "--set",
            "infrastructureMetering.networkPolicy.enabled=true",
            "--set-string",
            "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(enabled) if item]
    collector_objects = [
        item
        for item in objects
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "infra-collector"
    ]
    deployment = next(
        item for item in collector_objects if item["kind"] == "Deployment"
    )
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["template"]["spec"]["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 999,
        "runAsGroup": 999,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert deployment["metadata"]["annotations"]["reloader.stakater.com/auto"] == (
        "true"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == [
        "python",
        "-m",
        "services.infrastructure_metering.collector_runtime",
    ]
    env_names = {entry["name"] for entry in container["env"]}
    assert "INFRASTRUCTURE_METERING_INGESTION_KEY" in env_names
    assert (
        next(
            entry
            for entry in container["env"]
            if entry["name"] == "INFRASTRUCTURE_METERING_INGESTION_KEY"
        )["valueFrom"]["secretKeyRef"]["name"]
        == "test-infra-metering-ingestion"
    )
    assert "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY" not in env_names
    assert not any("POSTGRES" in name or "DATABASE" in name for name in env_names)
    orchestrator = next(
        item
        for item in objects
        if item["kind"] == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "orchestrator"
    )
    orchestrator_env = {
        entry["name"]: entry
        for entry in orchestrator["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert (
        orchestrator_env["INFRASTRUCTURE_METERING_INGESTION_KEY"]["valueFrom"][
            "secretKeyRef"
        ]["optional"]
        is False
    )
    assert (
        orchestrator_env["INFRASTRUCTURE_METERING_INGESTION_KEY"]["valueFrom"][
            "secretKeyRef"
        ]["name"]
        == "test-infra-metering-ingestion"
    )
    assert "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY" not in orchestrator_env

    roles = [item for item in collector_objects if item["kind"] == "Role"]
    assert {item["metadata"]["namespace"] for item in roles} == {
        "default",
        "agent-space",
    }
    for role in roles:
        assert role["rules"] == [
            {
                "apiGroups": [""],
                "resources": ["pods"],
                "verbs": ["get", "list", "watch"],
            }
        ]
        rendered = yaml.safe_dump(role)
        assert "secrets" not in rendered
        assert "persistentvolumeclaims" not in rendered
    assert not any(item["kind"] == "ClusterRole" for item in collector_objects)

    configmap = next(
        item
        for item in objects
        if item["kind"] == "ConfigMap"
        and "INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST" in item.get("data", {})
    )
    assert configmap["data"]["INFRASTRUCTURE_METERING_NAMESPACE_ALLOWLIST"] == (
        "default,agent-space"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_vmi_ingress_is_opt_in_tls_cidr_and_hmac_key_is_distinct():
    disabled = _render_remote_vmi_ingress()
    assert not any(
        item.get("metadata", {}).get("name", "").endswith("remote-infra-metering")
        for item in disabled
    )

    enabled = _render_remote_vmi_ingress(
        "--set",
        "infrastructureMetering.remoteIngress.enabled=true",
        "--set-string",
        "infrastructureMetering.remoteIngress.sourceCidrs[0]=203.0.113.10/32",
    )
    middleware = next(
        item
        for item in enabled
        if item["kind"] == "Middleware"
        and item["metadata"]["name"].endswith("allow-remote-infra-metering")
    )
    assert middleware["spec"]["ipWhiteList"]["sourceRange"] == ["203.0.113.10/32"]
    ingress = next(
        item
        for item in enabled
        if item["kind"] == "Ingress"
        and item["metadata"]["name"].endswith("api-ingress-remote-infra-metering")
    )
    assert ingress["spec"]["tls"]
    assert ingress["spec"]["rules"][0]["http"]["paths"][0]["path"] == (
        "/api/internal/infrastructure-metering"
    )
    orchestrator = next(
        item
        for item in enabled
        if item["kind"] == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "orchestrator"
    )
    env = {
        entry["name"]: entry
        for entry in orchestrator["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["INFRASTRUCTURE_METERING_VMI_INGESTION_KEY"]["valueFrom"][
        "secretKeyRef"
    ] == {
        "name": "vmi-metering-key",
        "key": "INFRASTRUCTURE_METERING_VMI_INGESTION_KEY",
        "optional": False,
    }


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_storage_server_gates_key_and_ingress_are_independent() -> None:
    disabled_ingress = _render_remote_storage_server()
    assert not any(
        item.get("metadata", {}).get("name", "").endswith("remote-infra-metering")
        for item in disabled_ingress
    )

    configmap = next(
        item
        for item in disabled_ingress
        if item["kind"] == "ConfigMap"
        and "INFRASTRUCTURE_METERING_VM_PVC_INVENTORY_ENABLED" in item.get("data", {})
    )
    assert configmap["data"]["INFRASTRUCTURE_METERING_VM_INVENTORY_ENABLED"] == (
        "false"
    )
    assert configmap["data"]["INFRASTRUCTURE_METERING_VM_PVC_INVENTORY_ENABLED"] == (
        "true"
    )
    assert configmap["data"]["INFRASTRUCTURE_METERING_VM_PV_INVENTORY_ENABLED"] == (
        "false"
    )
    for gate in (
        "INFRASTRUCTURE_METERING_VM_PVC_SHADOW_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PV_SHADOW_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PVC_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PV_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_VM_PV_CLUSTER_WIDE_RBAC_ACKNOWLEDGED",
        "INFRASTRUCTURE_METERING_PUBLICATION_ENABLED",
        "INFRASTRUCTURE_METERING_SOURCE_AWARE_READS_ENABLED",
        "INFRASTRUCTURE_METERING_V2_READS_ENABLED",
        "INFRASTRUCTURE_METERING_CUTOVER_ENABLED",
    ):
        assert configmap["data"][gate] == "false"

    orchestrator = next(
        item
        for item in disabled_ingress
        if item["kind"] == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "orchestrator"
    )
    env = {
        entry["name"]: entry
        for entry in orchestrator["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["INFRASTRUCTURE_METERING_VM_STORAGE_INGESTION_KEY"]["valueFrom"][
        "secretKeyRef"
    ] == {
        "name": "vm-storage-metering-key",
        "key": "INFRASTRUCTURE_METERING_VM_STORAGE_INGESTION_KEY",
        "optional": False,
    }
    assert (
        env["INFRASTRUCTURE_METERING_VMI_INGESTION_KEY"]["valueFrom"]["secretKeyRef"][
            "optional"
        ]
        is True
    )

    enabled_ingress = _render_remote_storage_server(
        "--set",
        "infrastructureMetering.remoteIngress.enabled=true",
        "--set-string",
        "infrastructureMetering.remoteIngress.sourceCidrs[0]=203.0.113.11/32",
    )
    middleware = next(
        item
        for item in enabled_ingress
        if item["kind"] == "Middleware"
        and item["metadata"]["name"].endswith("allow-remote-infra-metering")
    )
    assert middleware["spec"]["ipWhiteList"]["sourceRange"] == ["203.0.113.11/32"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_pv_server_gate_requires_and_exports_cluster_rbac_ack() -> None:
    objects = _render_remote_storage_server(
        "--set",
        "infrastructureMetering.vmPvInventoryEnabled=true",
        "--set",
        "infrastructureMetering.vmPvClusterWideRbacAcknowledged=true",
        "--set-string",
        "infrastructureMetering.volumeIdentityKeyVersion=storage-v1",
    )
    configmap = next(
        item
        for item in objects
        if item["kind"] == "ConfigMap"
        and "INFRASTRUCTURE_METERING_VM_PV_INVENTORY_ENABLED" in item.get("data", {})
    )

    assert configmap["data"]["INFRASTRUCTURE_METERING_VM_PV_INVENTORY_ENABLED"] == (
        "true"
    )
    assert (
        configmap["data"][
            "INFRASTRUCTURE_METERING_VM_PV_CLUSTER_WIDE_RBAC_ACKNOWLEDGED"
        ]
        == "true"
    )
    assert configmap["data"]["INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY_VERSION"] == (
        "storage-v1"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_missing_remote_ingress_parent_from_reused_values_defaults_off():
    """An upgrade from a pre-Slice-3 values set must remain renderable."""

    chart = ROOT / "helm"
    values = chart / "ci/test-values.yaml"
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "metering-old-values",
            str(chart),
            "-f",
            str(values),
            "--skip-schema-validation",
            "--set-json",
            "infrastructureMetering.remoteIngress=null",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(rendered) if item]
    assert not any(
        item.get("metadata", {}).get("name", "").endswith("remote-infra-metering")
        for item in objects
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["--set", "infrastructureMetering.agentPodShadowEnabled=true"],
            "agent Pod shadow gates require collectorEnabled",
        ),
        (
            ["--set", "infrastructureMetering.vmInventoryEnabled=true"],
            "vmInventoryEnabled requires collectorEnabled",
        ),
        (
            ["--set", "infrastructureMetering.vmPvcInventoryEnabled=true"],
            "remote storage inventory requires collectorEnabled",
        ),
    ],
)
def test_slice3_metering_gates_render_fail_closed(arguments, message):
    result = subprocess.run(
        [
            "helm",
            "template",
            "metering-test",
            str(ROOT / "helm"),
            "-f",
            str(ROOT / "helm/ci/test-values.yaml"),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_remote_storage_server_config_fails_closed() -> None:
    base = [
        "helm",
        "template",
        "metering-remote-storage",
        str(ROOT / "helm"),
        "-f",
        str(ROOT / "helm/ci/test-values.yaml"),
        "--set",
        "infrastructureMetering.collectorEnabled=true",
        "--set-string",
        "infrastructureMetering.stableClusterId=dev-cluster",
        "--set",
        "infrastructureMetering.networkPolicy.enabled=true",
        "--set-string",
        "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
        "--set",
        "infrastructureMetering.vmPvcInventoryEnabled=true",
        "--set-string",
        "infrastructureMetering.vmStableClusterId=vm-cluster",
        "--set-string",
        "infrastructureMetering.vmNamespace=agent-vms",
        "--set-string",
        "infrastructureMetering.vmStorageIngestionSecretName=vm-storage-hmac",
    ]
    cases = [
        (
            [
                "--set-string",
                "infrastructureMetering.vmStorageIngestionSecretName=",
            ],
            "requires vmStorageIngestionSecretName",
        ),
        (
            ["--set-string", "infrastructureMetering.vmStableClusterId="],
            "requires vmStableClusterId",
        ),
        (
            ["--set-string", "infrastructureMetering.vmNamespace="],
            "requires vmNamespace",
        ),
        (
            ["--set", "infrastructureMetering.vmPvcShadowEnabled=true"],
            "vmPvcShadowEnabled requires shadowEnabled",
        ),
        (
            ["--set", "infrastructureMetering.vmPvcPublicationEnabled=true"],
            "vmPvcPublicationEnabled requires vmPvcShadowEnabled",
        ),
        (
            ["--set", "infrastructureMetering.vmPvInventoryEnabled=true"],
            "requires vmPvClusterWideRbacAcknowledged=true",
        ),
        (
            [
                "--set",
                "infrastructureMetering.vmPvInventoryEnabled=true",
                "--set",
                "infrastructureMetering.vmPvClusterWideRbacAcknowledged=true",
            ],
            "vmPvInventoryEnabled requires volumeIdentityKeyVersion",
        ),
        (
            [
                "--set-string",
                "infrastructureMetering.vmStorageIngestionSecretName=test-infra-metering-ingestion",
            ],
            "must be separate from ingestionSecretName",
        ),
        (
            [
                "--set-string",
                "infrastructureMetering.vmIngestionSecretName=vm-storage-hmac",
            ],
            "must be separate from vmIngestionSecretName",
        ),
        (
            [
                "--set-string",
                "infrastructureMetering.volumeIdentitySecretName=vm-storage-hmac",
            ],
            "must be separate from volumeIdentitySecretName",
        ),
    ]

    for extra, message in cases:
        result = subprocess.run(
            base + extra,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, extra
        assert message in result.stderr, result.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_storage_inventory_rbac_and_identity_secret_are_independently_scoped():
    chart = ROOT / "helm"
    values = chart / "ci/test-values.yaml"
    base = [
        "helm",
        "template",
        "metering-test",
        str(chart),
        "-f",
        str(values),
        "--set",
        "infrastructureMetering.collectorEnabled=true",
        "--set-string",
        "infrastructureMetering.stableClusterId=dev-cluster",
        "--set",
        "infrastructureMetering.networkPolicy.enabled=true",
        "--set-string",
        "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
    ]

    pvc_rendered = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.pvcInventoryEnabled=true",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    pvc_objects = [item for item in yaml.safe_load_all(pvc_rendered) if item]
    pvc_roles = [
        item
        for item in pvc_objects
        if item["kind"] == "Role"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "infra-collector"
    ]
    assert pvc_roles
    for role in pvc_roles:
        assert role["rules"] == [
            {
                "apiGroups": [""],
                "resources": ["pods"],
                "verbs": ["get", "list", "watch"],
            },
            {
                "apiGroups": [""],
                "resources": ["persistentvolumeclaims"],
                "verbs": ["get", "list", "watch"],
            },
        ]
        assert "secrets" not in yaml.safe_dump(role)
    assert not any(
        item["kind"] == "ClusterRole"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "infra-collector"
        for item in pvc_objects
    )
    pvc_collector = next(
        item
        for item in pvc_objects
        if item["kind"] == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "infra-collector"
    )
    pvc_env = {
        entry["name"]
        for entry in pvc_collector["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY" not in pvc_env

    pv_rendered = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.pvInventoryEnabled=true",
            "--set-string",
            "infrastructureMetering.volumeIdentityKeyVersion=storage-v1",
            "--set-string",
            "infrastructureMetering.volumeIdentitySecretName=volume-identity",
            "--set-string",
            "infrastructureMetering.volumeResourceMappings[0].mappingVersion=local-v1",
            "--set-string",
            "infrastructureMetering.volumeResourceMappings[0].storageClass=local-path",
            "--set-string",
            "infrastructureMetering.volumeResourceMappings[0].csiDriver=",
            "--set-string",
            "infrastructureMetering.volumeResourceMappings[0].volumeMode=filesystem",
            "--set-string",
            "infrastructureMetering.volumeResourceMappings[0].resource=block_volume_local_path",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    pv_objects = [item for item in yaml.safe_load_all(pv_rendered) if item]
    cluster_role = next(
        item
        for item in pv_objects
        if item["kind"] == "ClusterRole"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "infra-collector"
    )
    assert cluster_role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["persistentvolumes"],
            "verbs": ["get", "list", "watch"],
        }
    ]
    assert "secrets" not in yaml.safe_dump(cluster_role)
    cluster_binding = next(
        item
        for item in pv_objects
        if item["kind"] == "ClusterRoleBinding"
        and item["metadata"]["name"] == cluster_role["metadata"]["name"]
    )
    assert cluster_binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": cluster_role["metadata"]["name"],
    }
    pv_collector = next(
        item
        for item in pv_objects
        if item["kind"] == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "infra-collector"
    )
    pv_env = {
        entry["name"]: entry
        for entry in pv_collector["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert pv_env["INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY"]["valueFrom"][
        "secretKeyRef"
    ] == {
        "name": "volume-identity",
        "key": "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY",
    }
    orchestrator = next(
        item
        for item in pv_objects
        if item["kind"] == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "orchestrator"
    )
    orchestrator_env = {
        entry["name"]
        for entry in orchestrator["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY" not in orchestrator_env
    assert "INFRASTRUCTURE_METERING_VOLUME_RESOURCE_MAPPINGS_JSON" in orchestrator_env
    assert "INFRASTRUCTURE_METERING_VOLUME_RESOURCE_MAPPINGS_JSON" not in pv_env
    configmap = next(
        item
        for item in pv_objects
        if item["kind"] == "ConfigMap"
        and "INFRASTRUCTURE_METERING_VOLUME_RESOURCE_MAPPINGS_JSON"
        in item.get("data", {})
    )
    assert yaml.safe_load(
        configmap["data"]["INFRASTRUCTURE_METERING_VOLUME_RESOURCE_MAPPINGS_JSON"]
    ) == [
        {
            "mappingVersion": "local-v1",
            "storageClass": "local-path",
            "csiDriver": "",
            "volumeMode": "filesystem",
            "resource": "block_volume_local_path",
        }
    ]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_chart_managed_metering_keys_are_dedicated_distinct_and_strong():
    chart = ROOT / "helm"
    values = chart / "ci/test-values.yaml"
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "metering-test",
            str(chart),
            "-f",
            str(values),
            "--set",
            "externalSecrets.enabled=false",
            "--set",
            "secrets.create=true",
            "--set-string",
            "infrastructureMetering.ingestionSecretName=",
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
            "--set",
            "infrastructureMetering.networkPolicy.allowUnrestrictedEgress=true",
            "--set",
            "infrastructureMetering.pvInventoryEnabled=true",
            "--set-string",
            "infrastructureMetering.volumeIdentityKeyVersion=storage-v1",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(rendered) if item]
    secret = next(
        item
        for item in objects
        if item["kind"] == "Secret"
        and "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY" in item.get("stringData", {})
    )
    identity_key = secret["stringData"]["INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY"]
    assert (
        secret["stringData"]["INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY_VERSION"]
        == "storage-v1"
    )
    ingestion_secret = next(
        item
        for item in objects
        if item["kind"] == "Secret"
        and "INFRASTRUCTURE_METERING_INGESTION_KEY" in item.get("stringData", {})
    )
    ingestion_key = ingestion_secret["stringData"][
        "INFRASTRUCTURE_METERING_INGESTION_KEY"
    ]
    application_secret = next(
        item
        for item in objects
        if item["kind"] == "Secret"
        and "APP_ENCRYPTION_KEY" in item.get("stringData", {})
    )
    assert len(identity_key) >= 32
    assert len(ingestion_key) >= 32
    assert (
        "INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY"
        not in (application_secret["stringData"])
    )
    assert (
        "INFRASTRUCTURE_METERING_INGESTION_KEY"
        not in (application_secret["stringData"])
    )
    assert (
        ingestion_secret["metadata"]["name"] != application_secret["metadata"]["name"]
    )
    assert identity_key != ingestion_key


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_metering_collector_helm_guards_fail_closed():
    chart = ROOT / "helm"
    values = chart / "ci/test-values.yaml"
    base = ["helm", "template", "metering-test", str(chart), "-f", str(values)]

    missing_identity = subprocess.run(
        base + ["--set", "infrastructureMetering.collectorEnabled=true"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_identity.returncode != 0
    assert "requires stableClusterId" in missing_identity.stderr

    unrestricted_egress = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unrestricted_egress.returncode != 0
    assert "requires networkPolicy.enabled or explicit" in unrestricted_egress.stderr

    unsafe_policy = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
            "--set",
            "infrastructureMetering.networkPolicy.enabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unsafe_policy.returncode != 0
    assert "requires apiServerCidrs" in unsafe_policy.stderr

    unsupported_mode = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
            "--set-string",
            "infrastructureMetering.deploymentMode=in-process",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unsupported_mode.returncode != 0
    assert "currently requires deploymentMode=dedicated" in unsupported_mode.stderr

    invalid_retention = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.snapshotItemRetentionDays=30",
            "--set",
            "infrastructureMetering.diagnosticRetentionDays=14",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert invalid_retention.returncode != 0
    assert "must be greater than or equal" in invalid_retention.stderr

    source_aware_without_v2 = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.sourceAwareReadsEnabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert source_aware_without_v2.returncode != 0
    assert "requires v2ReadsEnabled" in source_aware_without_v2.stderr

    cutover_without_shadow = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.cutoverEnabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cutover_without_shadow.returncode != 0
    assert "require collectorEnabled" in cutover_without_shadow.stderr

    pvc_without_collector = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.pvcInventoryEnabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert pvc_without_collector.returncode != 0
    assert "pvcInventoryEnabled requires collectorEnabled" in (
        pvc_without_collector.stderr
    )

    pv_without_key_version = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
            "--set",
            "infrastructureMetering.networkPolicy.enabled=true",
            "--set-string",
            "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
            "--set",
            "infrastructureMetering.pvInventoryEnabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert pv_without_key_version.returncode != 0
    assert "pvInventoryEnabled requires volumeIdentityKeyVersion" in (
        pv_without_key_version.stderr
    )

    pv_without_identity_secret = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
            "--set",
            "infrastructureMetering.networkPolicy.enabled=true",
            "--set-string",
            "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
            "--set",
            "infrastructureMetering.pvInventoryEnabled=true",
            "--set-string",
            "infrastructureMetering.volumeIdentityKeyVersion=storage-v1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert pv_without_identity_secret.returncode != 0
    assert "requires volumeIdentitySecretName outside chart-managed" in (
        pv_without_identity_secret.stderr
    )

    collector_without_ingestion_secret = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.ingestionSecretName=",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert collector_without_ingestion_secret.returncode != 0
    assert "requires ingestionSecretName outside chart-managed" in (
        collector_without_ingestion_secret.stderr
    )

    collector_using_application_secret = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "secrets.existingSecret=app-secret",
            "--set-string",
            "infrastructureMetering.ingestionSecretName=app-secret",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert collector_using_application_secret.returncode != 0
    assert "ingestionSecretName must be separate from the application Secret" in (
        collector_using_application_secret.stderr
    )

    pvc_shadow_without_inventory = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.pvcShadowEnabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert pvc_shadow_without_inventory.returncode != 0
    assert "pvcShadowEnabled requires pvcInventoryEnabled" in (
        pvc_shadow_without_inventory.stderr
    )

    pvc_publication_without_master = subprocess.run(
        base
        + [
            "--set",
            "infrastructureMetering.pvcPublicationEnabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert pvc_publication_without_master.returncode != 0
    assert "pvcPublicationEnabled requires publicationEnabled" in (
        pvc_publication_without_master.stderr
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_metering_collector_network_policy_has_no_unrestricted_dns_egress():
    chart = ROOT / "helm"
    values = chart / "ci/test-values.yaml"
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "metering-test",
            str(chart),
            "-f",
            str(values),
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
            "--set",
            "infrastructureMetering.networkPolicy.enabled=true",
            "--set-string",
            "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(rendered) if item]
    policy = next(
        item
        for item in objects
        if item["kind"] == "NetworkPolicy"
        and item["metadata"]["name"].endswith("infra-collector")
    )
    dns_rule = next(
        rule
        for rule in policy["spec"]["egress"]
        if any(port["port"] == 53 for port in rule.get("ports", []))
    )
    assert dns_rule["to"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
            },
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }
    ]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_public_ingress_blocks_internal_metering_path_when_collector_is_enabled():
    chart = ROOT / "helm"
    values = chart / "ci/test-values.yaml"
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "metering-test",
            str(chart),
            "-f",
            str(values),
            "--set",
            "infrastructureMetering.collectorEnabled=true",
            "--set-string",
            "infrastructureMetering.stableClusterId=dev-cluster",
            "--set",
            "infrastructureMetering.networkPolicy.enabled=true",
            "--set-string",
            "infrastructureMetering.networkPolicy.apiServerCidrs[0]=10.0.0.1/32",
            "--set",
            "auth.bff.sameOriginApi=true",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(rendered) if item]
    middleware = next(
        item
        for item in objects
        if item["kind"] == "Middleware"
        and item["metadata"]["name"].endswith("block-public-infra-metering")
    )
    assert middleware["spec"]["ipWhiteList"]["sourceRange"] == ["127.0.0.99/32"]
    blocked = next(
        item
        for item in objects
        if item["kind"] == "Ingress"
        and item["metadata"]["name"].endswith("api-ingress-blocked-infra-metering")
    )
    assert (
        blocked["metadata"]["annotations"][
            "traefik.ingress.kubernetes.io/router.priority"
        ]
        == "120"
    )
    paths = blocked["spec"]["rules"][0]["http"]["paths"]
    assert [path["path"] for path in paths] == ["/api/internal/infrastructure-metering"]
    cockpit_block = next(
        item
        for item in objects
        if item["kind"] == "Ingress"
        and item["metadata"]["name"].endswith("cockpit-ingress-blocked-infra-metering")
    )
    assert cockpit_block["spec"]["rules"][0]["http"]["paths"][0]["path"] == (
        "/api/internal/infrastructure-metering"
    )


# helm/ci/test-values.yaml pins vm.mode=external, so a colocated render has to
# switch topology explicitly: vmController.enabled is a deprecated alias the
# chart now rejects against an explicit mode. Same-cluster also forbids the
# Tailscale sidecar those values turn on.
_SAME_CLUSTER_MODE = (
    "--set",
    "vm.mode=same-cluster",
    "--set",
    "agent.tailscale.enabled=false",
)


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
@pytest.mark.parametrize(
    "secret_name_key",
    [
        "vm.lifecycleAuthSecretName",
        "infrastructureMetering.vmLifecycleAuthSecretName",
    ],
)
def test_colocated_vm_lifecycle_auth_is_paired_and_dedicated(
    secret_name_key: str,
) -> None:
    """One Secret name has to reach both the orchestrator and the controller.

    The metering-side spelling is the deprecated alias that
    ``srw.vmLifecycleAuthSecretName`` falls back to; either spelling must land
    the same secretKeyRef on both Deployments.
    """
    output = subprocess.run(
        [
            "helm",
            "template",
            "vm-lifecycle-test",
            str(ROOT / "helm"),
            "-f",
            str(ROOT / "helm/ci/test-values.yaml"),
            *_SAME_CLUSTER_MODE,
            "--set-string",
            f"{secret_name_key}=vm-lifecycle",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(output) if item]
    deployments = {
        item["metadata"]["labels"].get("app.kubernetes.io/component"): item
        for item in objects
        if item["kind"] == "Deployment"
    }
    for component in ("orchestrator", "vm-controller"):
        env = {
            entry["name"]: entry
            for entry in deployments[component]["spec"]["template"]["spec"][
                "containers"
            ][0]["env"]
        }
        secret_ref = env["VM_LIFECYCLE_HMAC_SECRET"]["valueFrom"]["secretKeyRef"]
        assert secret_ref == {
            "name": "vm-lifecycle",
            "key": "VM_LIFECYCLE_HMAC_SECRET",
        }
    assert deployments["vm-controller"]["spec"]["strategy"] == {"type": "Recreate"}
    vm_role = next(
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
    } in vm_role["rules"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
@pytest.mark.parametrize(
    "secret_name_key",
    [
        "vm.lifecycleAuthSecretName",
        "infrastructureMetering.vmLifecycleAuthSecretName",
    ],
)
def test_colocated_vm_lifecycle_auth_miswiring_fails_render(
    secret_name_key: str,
) -> None:
    result = subprocess.run(
        [
            "helm",
            "template",
            "vm-lifecycle-test",
            str(ROOT / "helm"),
            "-f",
            str(ROOT / "helm/ci/test-values.yaml"),
            *_SAME_CLUSTER_MODE,
            "--set-string",
            "secrets.existingSecret=app-secret",
            "--set-string",
            f"{secret_name_key}=app-secret",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "must be separate from the application Secret" in result.stderr
