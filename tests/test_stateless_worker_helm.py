from __future__ import annotations

import os
from pathlib import Path
import signal
import shutil
import subprocess
import time

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


def _stateless_agent_container(deployment: dict) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    matches = [container for container in containers if container["name"] == "agent"]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_stateless_worker_gate_is_independent_and_default_off() -> None:
    config_map = _only_kind(_render(show_only="templates/configmap.yaml"), "ConfigMap")

    assert config_map["data"]["STATELESS_SESSION_ENABLED"] == "false"
    assert config_map["data"]["STATELESS_WORKER_ENABLED"] == "false"
    assert config_map["data"]["STATELESS_WORKER_DEFAULT_ENABLED"] == "false"
    assert config_map["data"]["COMPLETION_COMMANDS_ENABLED"] == "false"
    assert config_map["data"]["COMPLETION_STATUS_REORDER_ENABLED"] == "false"
    assert config_map["data"]["COMPLETION_FINALIZER_INLINE_DELAY_SECONDS"] == "0"
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
def test_completion_status_reorder_requires_completion_commands() -> None:
    command = [
        "helm",
        "template",
        "stateless-worker-config-test",
        str(CHART),
        "-f",
        str(CHART / "ci/test-values.yaml"),
        "--set",
        "orchestrator.completionCommandsEnabled=false",
        "--set",
        "orchestrator.completionStatusReorderEnabled=true",
    ]

    rendered = subprocess.run(command, capture_output=True, text=True)

    assert rendered.returncode != 0
    assert (
        "orchestrator.completionStatusReorderEnabled requires "
        "orchestrator.completionCommandsEnabled" in rendered.stderr
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_generic_pool_opens_sessions_without_opening_worker_admission() -> None:
    documents = _render(
        "agent.stateless.enabled=true",
        "agent.stateless.worker.enabled=false",
    )
    config_map = _only_kind(
        [
            document
            for document in documents
            if document.get("kind") == "ConfigMap"
            and str(document.get("metadata", {}).get("name", "")).endswith(
                "-remote-worker-config"
            )
        ],
        "ConfigMap",
    )
    stateless_deployments = [
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and str(document.get("metadata", {}).get("name", "")).endswith(
            "-agent-stateless"
        )
    ]

    assert config_map["data"]["STATELESS_SESSION_ENABLED"] == "true"
    assert config_map["data"]["STATELESS_WORKER_ENABLED"] == "false"
    assert len(stateless_deployments) == 1
    assert _stateless_agent_container(stateless_deployments[0])["command"][
        -1
    ].startswith("exec python agent.py --mode stateless")


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_stateless_executor_grace_exceeds_shutdown_and_abort_budget() -> None:
    deployment = _only_kind(
        _render(
            "agent.stateless.enabled=true",
            show_only="templates/agent/stateless-deployment.yaml",
        ),
        "Deployment",
    )

    # Runtime defaults are 120s graceful shutdown + 15s abort. The remaining
    # margin lets local resource drains publish the exact claimant ACK before
    # kubelet may send SIGKILL.
    assert (
        deployment["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 180
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_stateless_executor_execs_python_as_pid1() -> None:
    deployment = _only_kind(
        _render(
            "agent.stateless.enabled=true",
            show_only="templates/agent/stateless-deployment.yaml",
        ),
        "Deployment",
    )

    assert _stateless_agent_container(deployment)["command"] == [
        "sh",
        "-c",
        (
            "exec python agent.py --mode stateless --config worker_base "
            "--port 8001 --host 0.0.0.0 --loop"
        ),
    ]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_rendered_stateless_shell_delivers_sigterm_to_agent(
    tmp_path: Path,
) -> None:
    deployment = _only_kind(
        _render(
            "agent.stateless.enabled=true",
            show_only="templates/agent/stateless-deployment.yaml",
        ),
        "Deployment",
    )
    command = _stateless_agent_container(deployment)["command"]
    ready_path = tmp_path / "ready"
    term_path = tmp_path / "term"
    (tmp_path / "agent.py").write_text(
        """\
import os
from pathlib import Path
import signal


def stop(signum, _frame):
    Path(os.environ["TERM_PATH"]).write_text(str(signum))
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
Path(os.environ["READY_PATH"]).write_text(str(os.getpid()))
signal.pause()
"""
    )
    environment = {
        **os.environ,
        "READY_PATH": str(ready_path),
        "TERM_PATH": str(term_path),
    }
    process = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)

        assert ready_path.exists(), (
            f"probe did not become ready (returncode={process.poll()})"
        )
        # `exec` replaces the shell rather than leaving Python as a child.
        assert int(ready_path.read_text()) == process.pid

        process.terminate()
        assert process.wait(timeout=5) == 0
        assert term_path.read_text() == str(signal.SIGTERM.value)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_stateless_worker_local_budget_override_reaches_both_planes() -> None:
    settings = (
        "agent.stateless.enabled=true",
        "agent.stateless.worker.enabled=true",
        "agent.stateless.worker.defaultEnabled=true",
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
    assert config_map["data"]["STATELESS_WORKER_DEFAULT_ENABLED"] == "true"
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
        "STATELESS_WORKER_DEFAULT_ENABLED",
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


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_completion_command_gate_true_reaches_control_and_execution_planes() -> None:
    documents = _render(
        "agent.stateless.enabled=true",
        "orchestrator.completionCommandsEnabled=true",
        "orchestrator.completionStatusReorderEnabled=true",
        "orchestrator.completionFinalizerInlineDelaySeconds=15",
    )
    config_map = next(
        document
        for document in documents
        if document.get("kind") == "ConfigMap"
        and "COMPLETION_COMMANDS_ENABLED" in document.get("data", {})
    )
    config_map_name = config_map["metadata"]["name"]
    assert config_map["data"]["COMPLETION_COMMANDS_ENABLED"] == "true"
    assert config_map["data"]["COMPLETION_STATUS_REORDER_ENABLED"] == "true"
    assert config_map["data"]["COMPLETION_FINALIZER_INLINE_DELAY_SECONDS"] == "15"

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
    orchestrator_env = {entry["name"]: entry for entry in orchestrator["env"]}
    assert orchestrator_env["COMPLETION_COMMANDS_ENABLED"]["valueFrom"][
        "configMapKeyRef"
    ] == {
        "name": config_map_name,
        "key": "COMPLETION_COMMANDS_ENABLED",
    }
    assert orchestrator_env["COMPLETION_STATUS_REORDER_ENABLED"]["valueFrom"][
        "configMapKeyRef"
    ] == {
        "name": config_map_name,
        "key": "COMPLETION_STATUS_REORDER_ENABLED",
    }
    assert orchestrator_env["COMPLETION_FINALIZER_INLINE_DELAY_SECONDS"]["valueFrom"][
        "configMapKeyRef"
    ] == {
        "name": config_map_name,
        "key": "COMPLETION_FINALIZER_INLINE_DELAY_SECONDS",
    }

    executor = _stateless_agent_container(deployments["agent-stateless"])
    assert {
        entry["configMapRef"]["name"]
        for entry in executor["envFrom"]
        if "configMapRef" in entry
    } == {config_map_name}
    # The execution plane receives the exact shared key through envFrom; an
    # explicit container value would override it and split the flag state.
    assert "COMPLETION_COMMANDS_ENABLED" not in {
        entry["name"] for entry in executor.get("env", [])
    }


def test_tilt_overlay_explicitly_enables_short_worker_batches() -> None:
    values = yaml.safe_load((ROOT / "deployment/values-tilt.yaml").read_text())

    assert values["agent"]["stateless"]["enabled"] is True
    worker = values["agent"]["stateless"]["worker"]
    assert worker == {
        "enabled": True,
        "defaultEnabled": False,
        "batchMinWallSeconds": 60,
    }
