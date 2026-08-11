from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm"


def _render(*settings: str, show_only: str | None = None) -> list[dict]:
    command = [
        "helm",
        "template",
        "stateless-worker-config-test",
        str(CHART),
        "-f",
        str(CHART / "ci/test-values.yaml"),
    ]
    if show_only:
        command.extend(["--show-only", show_only])
    for setting in settings:
        command.extend(["--set", setting])
    rendered = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [document for document in yaml.safe_load_all(rendered) if document]


def _only_kind(documents: list[dict], kind: str) -> dict:
    matches = [document for document in documents if document.get("kind") == kind]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_stateless_worker_gate_is_independent_and_default_off() -> None:
    config_map = _only_kind(_render(show_only="templates/configmap.yaml"), "ConfigMap")

    assert config_map["data"]["STATELESS_SESSION_ENABLED"] == "false"
    assert config_map["data"]["STATELESS_WORKER_ENABLED"] == "false"
    assert config_map["data"]["WORKER_BATCH_MIN_WALL_SECONDS"] == "300"
    assert config_map["data"]["LANGGRAPH_STRICT_MSGPACK"] == "true"


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_worker_gate_requires_a_stateless_executor_pool() -> None:
    command = [
        "helm",
        "template",
        "stateless-worker-config-test",
        str(CHART),
        "-f",
        str(CHART / "ci/test-values.yaml"),
        "--set",
        "agent.stateless.enabled=false",
        "--set",
        "agent.stateless.worker.enabled=true",
    ]

    rendered = subprocess.run(command, capture_output=True, text=True)

    assert rendered.returncode != 0
    assert (
        "agent.stateless.worker.enabled requires agent.stateless.enabled"
        in rendered.stderr
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_generic_pool_opens_sessions_without_opening_worker_admission() -> None:
    config_map = _only_kind(
        _render(
            "agent.stateless.enabled=true",
            "agent.stateless.worker.enabled=false",
            show_only="templates/configmap.yaml",
        ),
        "ConfigMap",
    )

    assert config_map["data"]["STATELESS_SESSION_ENABLED"] == "true"
    assert config_map["data"]["STATELESS_WORKER_ENABLED"] == "false"


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_stateless_worker_local_budget_override_reaches_both_planes() -> None:
    settings = (
        "agent.stateless.enabled=true",
        "agent.stateless.worker.enabled=true",
        "agent.stateless.worker.batchMinWallSeconds=60",
    )
    documents = _render(*settings)
    config_map = next(
        document
        for document in documents
        if document.get("kind") == "ConfigMap"
        and "STATELESS_WORKER_ENABLED" in document.get("data", {})
    )
    config_map_name = config_map["metadata"]["name"]

    assert config_map["data"]["STATELESS_WORKER_ENABLED"] == "true"
    assert config_map["data"]["STATELESS_SESSION_ENABLED"] == "true"
    assert config_map["data"]["WORKER_BATCH_MIN_WALL_SECONDS"] == "60"

    deployments = {
        document["metadata"]["labels"]["app.kubernetes.io/component"]: document
        for document in documents
        if document.get("kind") == "Deployment"
        and "app.kubernetes.io/component"
        in document.get("metadata", {}).get("labels", {})
    }
    orchestrator = next(
        container
        for container in deployments["orchestrator"]["spec"]["template"]["spec"][
            "containers"
        ]
        if container["name"] == "orchestrator"
    )
    env_by_name = {entry["name"]: entry for entry in orchestrator["env"]}
    for key in (
        "STATELESS_SESSION_ENABLED",
        "STATELESS_WORKER_ENABLED",
        "WORKER_BATCH_MIN_WALL_SECONDS",
    ):
        assert env_by_name[key]["valueFrom"]["configMapKeyRef"] == {
            "name": config_map_name,
            "key": key,
        }

    executor = next(
        container
        for container in deployments["agent-stateless"]["spec"]["template"]["spec"][
            "containers"
        ]
        if container["name"] == "agent"
    )
    assert {
        entry["configMapRef"]["name"]
        for entry in executor["envFrom"]
        if "configMapRef" in entry
    } == {config_map_name}


def test_tilt_overlay_explicitly_enables_short_worker_batches() -> None:
    values = yaml.safe_load((ROOT / "deployment/values-tilt.yaml").read_text())

    assert values["agent"]["stateless"]["enabled"] is True
    worker = values["agent"]["stateless"]["worker"]
    assert worker == {"enabled": True, "batchMinWallSeconds": 60}
