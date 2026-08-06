from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


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
