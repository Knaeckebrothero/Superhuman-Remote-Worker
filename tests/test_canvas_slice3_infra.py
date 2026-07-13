"""Static infrastructure gates for Dynamic Canvas live preview foundation."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


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
