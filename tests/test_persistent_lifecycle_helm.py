"""Chart contract for the first-rollout persistent lifecycle fence."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from orchestrator.database.migrate import _maintenance_gate


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm"
RUNTIME_AUTHORITY_MIGRATION = (
    ROOT
    / "orchestrator/database/migrations/app/0185_thread_runtime_generation_retirement.sql"
)


def test_automatic_persistent_reconciliation_defaults_false() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["orchestrator"]["persistentAgentReconciliationEnabled"] is False
    assert values["orchestrator"]["officerRuntimeVerificationEnabled"] is False
    assert values["orchestrator"]["runtimeAuthorityMigrationMaintenanceAck"] is False
    assert values["agent"]["persistentInputCancellationEnabled"] is False
    assert values["agent"]["requirePinnedStatusIdentity"] == "true"


def _render_orchestrator(*extra_args: str) -> dict:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "runtime-authority-cutover",
            str(CHART),
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            *extra_args,
            "--show-only",
            "templates/orchestrator/deployment.yaml",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return next(doc for doc in yaml.safe_load_all(rendered) if doc)


def test_runtime_authority_migration_defaults_to_rolling_refusal() -> None:
    deployment = _render_orchestrator()
    assert deployment["spec"]["strategy"] == {"type": "RollingUpdate"}
    orchestrator = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "orchestrator"
    )
    env = {entry["name"]: entry.get("value") for entry in orchestrator["env"]}
    assert env["MIGRATION_MAINTENANCE_GATES"] == ""


def test_runtime_authority_ack_uses_same_value_for_gate_and_recreate() -> None:
    deployment = _render_orchestrator(
        "--set", "orchestrator.runtimeAuthorityMigrationMaintenanceAck=true"
    )
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    orchestrator = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "orchestrator"
    )
    env = {entry["name"]: entry.get("value") for entry in orchestrator["env"]}
    migration_gate = _maintenance_gate(
        RUNTIME_AUTHORITY_MIGRATION.read_text(encoding="utf-8")
    )
    assert migration_gate == "pinned-runtime-authority-v1"
    assert env["MIGRATION_MAINTENANCE_GATES"] == migration_gate


def test_runtime_authority_cutover_notes_are_acknowledgement_gated() -> None:
    notes = (CHART / "templates" / "NOTES.txt").read_text(encoding="utf-8")
    guard = "{{- if .Values.orchestrator.runtimeAuthorityMigrationMaintenanceAck }}"
    start = notes.index(guard) + len(guard)
    end = notes.index("{{- end }}", start)
    cutover_notes = notes[start:end]

    assert "PINNED RUNTIME AUTHORITY CUTOVER" in cutover_notes
    assert "`pinned-runtime-authority-v1` migration gate" in cutover_notes
    assert "Do not use Helm\n`--atomic`" in cutover_notes
    assert "never roll back to a pre-0185 image" in cutover_notes
    assert "PINNED RUNTIME AUTHORITY CUTOVER" not in notes[:start]
    assert "PINNED RUNTIME AUTHORITY CUTOVER" not in notes[end:]


def test_runtime_authority_ack_refuses_legacy_identity_mode() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "runtime-authority-cutover",
            str(CHART),
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            "--set",
            "orchestrator.runtimeAuthorityMigrationMaintenanceAck=true",
            "--set-string",
            "agent.requirePinnedStatusIdentity=false",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rendered.returncode != 0
    assert (
        "runtimeAuthorityMigrationMaintenanceAck requires "
        "agent.requirePinnedStatusIdentity=true" in rendered.stderr
    )


def test_pinned_status_identity_strict_gate_reaches_both_planes() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "pinned-status-proof",
            str(CHART),
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            "--set",
            "agent.requirePinnedStatusIdentity=true",
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
        and "REQUIRE_PINNED_STATUS_IDENTITY" in (doc.get("data") or {})
    )
    assert configmap["data"]["REQUIRE_PINNED_STATUS_IDENTITY"] == "true"
    deployment = next(
        doc
        for doc in documents
        if doc.get("kind") == "Deployment"
        and any(
            c.get("name") == "orchestrator"
            for c in doc["spec"]["template"]["spec"]["containers"]
        )
    )
    orchestrator = next(
        c
        for c in deployment["spec"]["template"]["spec"]["containers"]
        if c.get("name") == "orchestrator"
    )
    env = {entry["name"]: entry for entry in orchestrator["env"]}
    assert (
        env["REQUIRE_PINNED_STATUS_IDENTITY"]["valueFrom"]["configMapKeyRef"]["key"]
        == "REQUIRE_PINNED_STATUS_IDENTITY"
    )


def test_input_cancellation_writer_gate_renders_for_agent_envfrom() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "input-cancellation-proof",
            str(CHART),
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            "--set",
            "agent.persistentInputCancellationEnabled=true",
            "--show-only",
            "templates/configmap.yaml",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    configmap = next(doc for doc in yaml.safe_load_all(rendered) if doc)
    assert configmap["data"]["PERSISTENT_INPUT_CANCELLATION_ENABLED"] == "true"


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
