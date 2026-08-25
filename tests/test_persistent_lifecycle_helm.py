"""Chart contract for the first-rollout persistent lifecycle fence."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm"


def test_automatic_persistent_reconciliation_defaults_false() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["orchestrator"]["persistentAgentReconciliationEnabled"] is False
    assert values["orchestrator"]["officerRuntimeVerificationEnabled"] is False


def test_flag_renders_through_configmap_and_orchestrator_environment() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "lifecycle-proof",
            str(CHART),
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            "--set",
            "orchestrator.persistentAgentReconciliationEnabled=true",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
    configmap = next(
        doc
        for doc in documents
        if doc.get("kind") == "ConfigMap"
        and "PERSISTENT_AGENT_RECONCILIATION_ENABLED" in (doc.get("data") or {})
    )
    assert configmap["data"]["PERSISTENT_AGENT_RECONCILIATION_ENABLED"] == "true"

    deployment = next(
        doc
        for doc in documents
        if doc.get("kind") == "Deployment"
        and any(
            container.get("name") == "orchestrator"
            for container in doc["spec"]["template"]["spec"]["containers"]
        )
    )
    orchestrator = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container.get("name") == "orchestrator"
    )
    env = {entry["name"]: entry for entry in orchestrator.get("env") or []}
    assert (
        env["PERSISTENT_AGENT_RECONCILIATION_ENABLED"]["valueFrom"]["configMapKeyRef"][
            "key"
        ]
        == "PERSISTENT_AGENT_RECONCILIATION_ENABLED"
    )


def test_agent_pull_policy_reaches_dynamic_persistent_pods() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "pull-policy-proof",
            str(CHART),
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            "--set",
            "image.agent.pullPolicy=IfNotPresent",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
    configmap = next(
        doc
        for doc in documents
        if doc.get("kind") == "ConfigMap"
        and "PERSISTENT_AGENT_IMAGE_PULL_POLICY" in (doc.get("data") or {})
    )
    assert configmap["data"]["PERSISTENT_AGENT_IMAGE_PULL_POLICY"] == "IfNotPresent"

    deployment = next(
        doc
        for doc in documents
        if doc.get("kind") == "Deployment"
        and any(
            container.get("name") == "orchestrator"
            for container in doc["spec"]["template"]["spec"]["containers"]
        )
    )
    orchestrator = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container.get("name") == "orchestrator"
    )
    env = {entry["name"]: entry for entry in orchestrator.get("env") or []}
    assert (
        env["PERSISTENT_AGENT_IMAGE_PULL_POLICY"]["valueFrom"]["configMapKeyRef"]["key"]
        == "PERSISTENT_AGENT_IMAGE_PULL_POLICY"
    )


def test_runtime_verification_flag_renders_dark_and_can_be_enabled_explicitly() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    base_command = [
        "helm",
        "template",
        "runtime-verification-proof",
        str(CHART),
        "-f",
        str(CHART / "ci" / "test-values.yaml"),
    ]

    def _render(*extra: str):
        output = subprocess.run(
            [*base_command, *extra],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        documents = [doc for doc in yaml.safe_load_all(output) if doc]
        configmap = next(
            doc
            for doc in documents
            if doc.get("kind") == "ConfigMap"
            and "OFFICER_RUNTIME_VERIFICATION_ENABLED" in (doc.get("data") or {})
        )
        deployment = next(
            doc
            for doc in documents
            if doc.get("kind") == "Deployment"
            and any(
                container.get("name") == "orchestrator"
                for container in doc["spec"]["template"]["spec"]["containers"]
            )
        )
        orchestrator = next(
            container
            for container in deployment["spec"]["template"]["spec"]["containers"]
            if container.get("name") == "orchestrator"
        )
        return configmap, {
            entry["name"]: entry for entry in orchestrator.get("env") or []
        }

    dark, dark_env = _render()
    assert dark["data"]["OFFICER_RUNTIME_VERIFICATION_ENABLED"] == "false"
    assert (
        dark_env["OFFICER_RUNTIME_VERIFICATION_ENABLED"]["valueFrom"][
            "configMapKeyRef"
        ]["key"]
        == "OFFICER_RUNTIME_VERIFICATION_ENABLED"
    )
    enabled, _ = _render("--set", "orchestrator.officerRuntimeVerificationEnabled=true")
    assert enabled["data"]["OFFICER_RUNTIME_VERIFICATION_ENABLED"] == "true"
