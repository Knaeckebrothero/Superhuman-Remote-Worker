"""Static infrastructure gates for Dynamic Canvas live preview foundation."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CANVAS_GATEWAY_COMMAND = [
    "uvicorn",
    "canvas_gateway:app",
    "--host",
    "0.0.0.0",
    "--port",
    "8086",
    "--http",
    "h11",
    "--no-access-log",
]


def test_workspace_sshd_limits_canvas_direct_channels_to_loopback() -> None:
    expected = (
        "AllowTcpForwarding local",
        "PermitOpen 127.0.0.1:*",
        "GatewayPorts no",
        "AllowAgentForwarding no",
        "PermitTunnel no",
    )
    for relative_path in (
        "docker/Dockerfile.workspace",
        "docker/agent-vm-base/scripts/provision-stage2.sh",
    ):
        source = (ROOT / relative_path).read_text()
        for directive in expected:
            assert source.count(directive) == 1, (relative_path, directive)


def test_canvas_live_preview_helm_gate_is_boolean_and_default_off() -> None:
    values = yaml.safe_load((ROOT / "helm/values.yaml").read_text())
    assert values["canvas"]["livePreview"]["enabled"] is False
    assert values["canvas"]["livePreview"]["deniedPorts"] == []

    schema = json.loads((ROOT / "helm/values.schema.json").read_text())
    enabled_schema = schema["properties"]["canvas"]["properties"]["livePreview"][
        "properties"
    ]["enabled"]
    assert enabled_schema == {"type": "boolean"}
    denied_schema = schema["properties"]["canvas"]["properties"]["livePreview"][
        "properties"
    ]["deniedPorts"]
    assert denied_schema == {
        "type": "array",
        "items": {"type": "integer", "minimum": 1, "maximum": 65535},
        "uniqueItems": True,
    }


def test_canvas_live_preview_gate_reaches_orchestrator_and_agent_config() -> None:
    configmap = (ROOT / "helm/templates/configmap.yaml").read_text()
    deployment = (ROOT / "helm/templates/orchestrator/deployment.yaml").read_text()
    assert configmap.count("CANVAS_LIVE_PREVIEW_ENABLED:") == 1
    assert ".Values.canvas.livePreview.enabled" in configmap
    assert deployment.count("- name: CANVAS_LIVE_PREVIEW_ENABLED") == 1
    assert "key: CANVAS_LIVE_PREVIEW_ENABLED" in deployment
    assert configmap.count("CANVAS_LIVE_PREVIEW_DENIED_PORTS:") == 1
    assert ".Values.canvas.livePreview.deniedPorts" in configmap
    assert deployment.count("- name: CANVAS_LIVE_PREVIEW_DENIED_PORTS") == 1
    assert "key: CANVAS_LIVE_PREVIEW_DENIED_PORTS" in deployment

    # Dynamically provisioned agent pods consume every shared ConfigMap key via
    # envFrom without duplicating provisioner-specific env lists. The agent's
    # actual tool capability still comes only from the orchestrator's positive
    # attach bit and is covered by the callable/runtime tests.
    for relative_path in (
        "orchestrator/services/agent_provisioner.py",
        "orchestrator/services/persistent_provisioner.py",
    ):
        source = (ROOT / relative_path).read_text()
        assert '"envFrom"' in source
        assert '"configMapRef"' in source

    compose_value = "${CANVAS_LIVE_PREVIEW_ENABLED:-false}"
    compose_denylist = "${CANVAS_LIVE_PREVIEW_DENIED_PORTS:-}"
    for relative_path in ("docker-compose.yaml", "docker-compose.local.yaml"):
        compose = yaml.safe_load((ROOT / relative_path).read_text())
        services = compose["services"]
        assert (
            services["orchestrator"]["environment"]["CANVAS_LIVE_PREVIEW_ENABLED"]
            == compose_value
        )
        assert (
            services["agent"]["environment"]["CANVAS_LIVE_PREVIEW_ENABLED"]
            == compose_value
        )
        assert (
            services["orchestrator"]["environment"]["CANVAS_LIVE_PREVIEW_DENIED_PORTS"]
            == compose_denylist
        )


def test_public_examples_keep_live_preview_disabled() -> None:
    env_example = (ROOT / ".env.example").read_text()
    local_values = yaml.safe_load(
        (ROOT / "deployment/values-local.yaml.example").read_text()
    )
    production_values = yaml.safe_load((ROOT / "helm/values.example.yaml").read_text())
    assert "CANVAS_LIVE_PREVIEW_ENABLED=false" in env_example
    assert "CANVAS_LIVE_PREVIEW_DENIED_PORTS=8501,9000" in env_example
    assert local_values["canvas"]["livePreview"]["enabled"] is False
    assert local_values["canvas"]["livePreview"]["deniedPorts"] == []
    assert production_values["canvas"]["livePreview"]["enabled"] is False
    assert production_values["canvas"]["livePreview"]["deniedPorts"] == []


def test_canvas_viewer_chart_values_are_default_off_and_fail_closed() -> None:
    values = yaml.safe_load((ROOT / "helm/values.yaml").read_text())
    viewer = values["canvas"]["livePreview"]["viewer"]
    assert viewer["enabled"] is False
    assert viewer["cookieMode"] == ""
    assert viewer["domain"] == ""
    assert viewer["hostSuffix"] == ""
    assert viewer["cockpitOrigins"] == []
    assert viewer["rawPathVerified"] is False
    assert viewer["pslBoundaryVerified"] is False
    assert viewer["networkPolicy"]["edgeNamespaceSelector"] == {}
    assert viewer["networkPolicy"]["edgePodSelector"] == {}

    schema = json.loads((ROOT / "helm/values.schema.json").read_text())
    viewer_schema = schema["properties"]["canvas"]["properties"]["livePreview"][
        "properties"
    ]["viewer"]
    assert (
        viewer_schema["properties"]["limits"]["properties"]["maxHeaderFields"][
            "minimum"
        ]
        == 10
    )
    assert viewer_schema["properties"]["networkPolicy"]["additionalProperties"] is False
    non_empty = schema["definitions"]["nonEmptyLabelSelector"]
    assert len(non_empty["allOf"][1]["anyOf"]) == 2


def test_canvas_gateway_compose_contract_is_profiled_internal_and_healthy() -> None:
    for relative_path in ("docker-compose.yaml", "docker-compose.local.yaml"):
        compose = yaml.safe_load((ROOT / relative_path).read_text())
        gateway = compose["services"]["canvas-gateway"]
        assert gateway["profiles"] == ["canvas-viewer"]
        assert "ports" not in gateway
        assert gateway["expose"] == ["8086"]
        assert gateway["command"] == CANVAS_GATEWAY_COMMAND
        assert gateway["environment"]["CANVAS_LIVE_PREVIEW_ENABLED"].endswith(
            ":-false}"
        )
        assert gateway["environment"]["CANVAS_VIEWER_ENABLED"].endswith(":-false}")

        healthcheck = gateway["healthcheck"]
        assert healthcheck["test"][:3] == ["CMD", "python", "-c"]
        assert "127.0.0.1" in healthcheck["test"][3]
        assert "8086" in healthcheck["test"][3]
        assert "/api/health" not in healthcheck["test"][3]


def test_canvas_gateway_templates_preserve_no_ingress_boundary() -> None:
    gateway_dir = ROOT / "helm/templates/canvas-gateway"
    templates = {path.name: path.read_text() for path in gateway_dir.glob("*.yaml")}
    assert set(templates) == {
        "deployment.yaml",
        "network-policy.yaml",
        "service.yaml",
    }
    assert all("kind: Ingress" not in source for source in templates.values())
    assert "type: ClusterIP" in templates["service.yaml"]
    assert "port: 8086" in templates["service.yaml"]

    deployment = templates["deployment.yaml"]
    assert "canvas_gateway:app" in deployment
    assert "--http" in deployment
    assert "- h11" in deployment
    assert "--no-access-log" in deployment
    assert "automountServiceAccountToken: false" in deployment

    policy = templates["network-policy.yaml"]
    assert "non-empty networkPolicy.edgeNamespaceSelector" in policy
    assert "port: 8086" in policy
    assert "port: 5432" in policy
    assert "port: 30022" in policy
    assert "port: 22" in policy

    shared_ingress = (ROOT / "helm/templates/ingress.yaml").read_text()
    assert "canvas-gateway" not in shared_ingress
    assert "8086" not in shared_ingress


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_canvas_gateway_helm_render_contract_and_selector_gate() -> None:
    chart = ROOT / "helm"
    test_values = chart / "ci/test-values.yaml"
    base = ["helm", "template", "canvas-test", str(chart), "-f", str(test_values)]

    default_render = subprocess.run(
        base,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    default_objects = [item for item in yaml.safe_load_all(default_render) if item]
    assert not any(
        item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "canvas-gateway"
        for item in default_objects
    )

    enabled_args = [
        "--set",
        "canvas.livePreview.enabled=true",
        "--set",
        "canvas.livePreview.viewer.enabled=true",
        "--set",
        "canvas.livePreview.viewer.cookieMode=psl-isolated",
        "--set-string",
        "canvas.livePreview.viewer.domain=example-userland.test",
        "--set-string",
        "canvas.livePreview.viewer.hostSuffix=.canvas.example-userland.test",
        "--set-string",
        "canvas.livePreview.viewer.cockpitOrigins[0]=https://cockpit.example.test",
        "--set",
        "canvas.livePreview.viewer.rawPathVerified=true",
        "--set",
        "canvas.livePreview.viewer.pslBoundaryVerified=true",
    ]

    rejected = subprocess.run(
        [*base, *enabled_args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "edgeNamespaceSelector" in rejected.stderr
    assert "edgePodSelector" in rejected.stderr

    # `{matchLabels: {}}` is still an unconstrained selector and must not turn
    # into namespace-wide/pod-wide ingress.
    empty_selector = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set-json",
            'canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector={"matchLabels":{}}',
            "--set-json",
            'canvas.livePreview.viewer.networkPolicy.edgePodSelector={"matchLabels":{}}',
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert empty_selector.returncode != 0

    rendered = subprocess.run(
        [
            *base,
            *enabled_args,
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgeNamespaceSelector.matchLabels.edge=trusted",
            "--set-string",
            "canvas.livePreview.viewer.networkPolicy.edgePodSelector.matchLabels.app=viewer-edge",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(rendered) if item]
    gateway_objects = [
        item
        for item in objects
        if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "canvas-gateway"
    ]
    assert {item["kind"] for item in gateway_objects} == {
        "Deployment",
        "NetworkPolicy",
        "Service",
    }
    for ingress_object in (item for item in objects if item["kind"] == "Ingress"):
        ingress_yaml = yaml.safe_dump(ingress_object)
        assert "canvas-gateway" not in ingress_yaml
        assert "number: 8086" not in ingress_yaml

    by_kind = {item["kind"]: item for item in gateway_objects}
    assert by_kind["Service"]["spec"]["type"] == "ClusterIP"
    assert by_kind["Service"]["spec"]["ports"][0]["port"] == 8086
    ingress = by_kind["NetworkPolicy"]["spec"]["ingress"]
    assert ingress[0]["ports"] == [{"protocol": "TCP", "port": 8086}]
    assert ingress[0]["from"] == [
        {
            "namespaceSelector": {"matchLabels": {"edge": "trusted"}},
            "podSelector": {"matchLabels": {"app": "viewer-edge"}},
        }
    ]
    gateway_egress_ports = {
        port["port"]
        for rule in by_kind["NetworkPolicy"]["spec"]["egress"]
        for port in rule.get("ports", [])
    }
    assert {22, 53, 5432, 30022} <= gateway_egress_ports

    workspace_policies = [
        item
        for item in objects
        if item["kind"] == "NetworkPolicy"
        and "-workspace-policy-" in item["metadata"]["name"]
    ]
    assert workspace_policies
    for policy in workspace_policies:
        gateway_rules = [
            rule
            for rule in policy["spec"]["ingress"]
            if any(
                peer.get("podSelector", {})
                .get("matchLabels", {})
                .get("app.kubernetes.io/component")
                == "canvas-gateway"
                for peer in rule.get("from", [])
            )
        ]
        assert len(gateway_rules) == 1
        assert gateway_rules[0]["ports"] == [
            {"protocol": "TCP", "port": 30022},
            {"protocol": "TCP", "port": 22},
        ]
        assert len(gateway_rules[0]["from"]) == 1

    container = by_kind["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == CANVAS_GATEWAY_COMMAND
