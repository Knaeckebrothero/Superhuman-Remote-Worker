from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _render_stateless_deployment(*, enabled: bool) -> dict | None:
    chart = ROOT / "helm"
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "stateless-workspace-ssh-test",
            str(chart),
            "-f",
            str(chart / "ci/test-values.yaml"),
            "--set",
            f"agent.stateless.enabled={str(enabled).lower()}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    matches = [
        item
        for item in yaml.safe_load_all(rendered)
        if item
        and item.get("kind") == "Deployment"
        and item.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "agent-stateless"
    ]
    assert len(matches) <= 1
    return matches[0] if matches else None


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_stateless_workspace_ssh_key_mount_is_scoped_and_default_off() -> None:
    assert _render_stateless_deployment(enabled=False) is None

    deployment = _render_stateless_deployment(enabled=True)
    assert deployment is not None
    pod_spec = deployment["spec"]["template"]["spec"]
    agent = next(
        container
        for container in pod_spec["containers"]
        if container["name"] == "agent"
    )

    key_mounts = [
        mount for mount in agent["volumeMounts"] if mount["name"] == "vm-ssh-key"
    ]
    assert key_mounts == [
        {
            "name": "vm-ssh-key",
            "mountPath": "/run/secrets/vm-ssh-key",
            "subPath": "ssh-privatekey",
            "readOnly": True,
        }
    ]

    key_volumes = [
        volume for volume in pod_spec["volumes"] if volume["name"] == "vm-ssh-key"
    ]
    assert key_volumes == [
        {
            "name": "vm-ssh-key",
            "secret": {
                "secretName": (
                    "stateless-workspace-ssh-test-superhuman-remote-worker-vm-ssh-key"
                ),
                "defaultMode": 0o444,
            },
        }
    ]
    assert "data" not in key_volumes[0]["secret"]
    assert "stringData" not in key_volumes[0]["secret"]
