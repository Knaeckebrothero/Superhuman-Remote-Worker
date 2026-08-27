"""Safety contract for the local managed-repository residue gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "k3d-managed-repository-residue-gate.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "k3d_managed_repository_residue_gate", _SCRIPT
)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)


def _args(**overrides):
    values = {
        "context": "k3d-srw",
        "namespace": "srw",
        "orchestrator_deployment": "srw-orchestrator",
        "workspace_image": None,
        "gate_id": "srw-mr-residue-012345abcdef",
        "timeout_seconds": 240,
        "run": False,
        "confirm": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_plan_defaults_are_local_and_non_mutating(capsys) -> None:
    assert gate.main(["--gate-id", "srw-mr-residue-012345abcdef"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "plan"
    assert payload["cleanup"] == "planned"
    assert all(item["result"] == "planned" for item in payload["phases"])
    assert payload["vm"] == "preflight_only"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context", "main-dev"),
        ("context", "k3d-other"),
        ("namespace", "default"),
        ("namespace", "production"),
    ],
)
def test_context_and_namespace_cannot_escape_local_cluster(field, value) -> None:
    with pytest.raises(gate.SafetyError, match="restricted"):
        gate.validate_config(_args(**{field: value}))


def test_mutation_requires_exact_confirmation() -> None:
    with pytest.raises(gate.SafetyError, match="requires"):
        gate.validate_config(_args(run=True))
    with pytest.raises(gate.SafetyError, match="requires"):
        gate.validate_config(_args(run=True, confirm="yes"))
    config = gate.validate_config(_args(run=True, confirm="LOCAL-K3D-DISPOSABLE"))
    assert config.run is True


@pytest.mark.parametrize(
    "gate_id",
    [
        "srw-mr-residue-short",
        "srw-mr-residue-012345ABCDEf",
        "other-012345abcdef",
        "srw-mr-residue-012345abcdef-extra",
    ],
)
def test_gate_id_must_be_exact_unique_slug(gate_id) -> None:
    with pytest.raises(gate.SafetyError, match="gate id"):
        gate.validate_config(_args(gate_id=gate_id))


@pytest.mark.parametrize(
    "image",
    [
        "https://registry.example/workspace:latest",
        "registry.example/workspace:tag with-space",
        "registry.example/workspace:\nsecret",
        "registry.example/user:password@workspace",
    ],
)
def test_workspace_image_rejects_url_or_credential_shaped_values(image) -> None:
    with pytest.raises(gate.SafetyError, match="image"):
        gate.validate_config(_args(workspace_image=image))


def test_workspace_manifest_is_exact_secret_free_and_finalizer_backed() -> None:
    config = gate.validate_config(_args())
    manifest = gate.build_workspace_pod_manifest(
        config, "registry.local:5000/workspace:gate"
    )
    metadata = manifest["metadata"]
    assert metadata["name"] == config.gate_id
    assert metadata["namespace"] == "srw"
    assert metadata["labels"]["srw/job-id"] == config.owner_id
    assert metadata["labels"][gate.GATE_LABEL] == config.gate_id
    assert metadata["finalizers"] == [gate.PROCESS_ZERO_FINALIZER]
    encoded = json.dumps(manifest)
    assert "secret" not in encoded.lower()
    assert "auto_pull" not in encoded
    assert len(manifest["spec"]["containers"]) == 1


def test_kubectl_runner_always_pins_context_and_namespace(monkeypatch) -> None:
    config = gate.validate_config(_args())
    observed = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    runner = gate.KubectlRunner(config)
    runner.run(["get", "pods"], operation="test")
    assert observed["argv"][:5] == [
        "kubectl",
        "--context",
        "k3d-srw",
        "--namespace",
        "srw",
    ]


def test_subprocess_failure_never_echoes_output_or_arguments(monkeypatch) -> None:
    config = gate.validate_config(_args())
    private_marker = "-----BEGIN PRIVATE KEY-----"

    def fake_run(_argv, **_kwargs):
        return SimpleNamespace(
            returncode=41,
            stdout=private_marker.encode(),
            stderr=b"credential-bearing internal endpoint",
        )

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    runner = gate.KubectlRunner(config)
    with pytest.raises(gate.GateFailure) as caught:
        runner.run(
            ["exec", "pod", "--", "tool", "argument-must-not-echo"],
            operation="redacted operation",
            input_data=private_marker.encode(),
        )
    message = str(caught.value)
    assert message == "redacted operation failed (rc=41)"
    assert private_marker not in message
    assert "endpoint" not in message
    assert "argument-must-not-echo" not in message


def test_exact_terminal_classifier_is_narrow() -> None:
    base = {
        "metadata": {"deletionTimestamp": "now"},
        "spec": {"containers": [{"name": "workspace"}], "nodeName": "node-a"},
        "status": {
            "containerStatuses": [
                {"name": "workspace", "state": {"terminated": {"exitCode": 0}}}
            ]
        },
    }
    assert gate.ManagedRepositoryResidueGate._pod_is_exact_terminal(base)
    assigned_missing = {
        "metadata": {"deletionTimestamp": "now"},
        "spec": {"containers": [{"name": "workspace"}], "nodeName": "node-a"},
        "status": {},
    }
    assert not gate.ManagedRepositoryResidueGate._pod_is_exact_terminal(
        assigned_missing
    )
    unscheduled = {
        "metadata": {"deletionTimestamp": "now"},
        "spec": {"containers": [{"name": "workspace"}]},
        "status": {"phase": "Pending"},
    }
    assert gate.ManagedRepositoryResidueGate._pod_is_exact_terminal(unscheduled)
    not_deleting = {
        "metadata": {},
        "spec": {"containers": [{"name": "workspace"}]},
        "status": {"phase": "Pending"},
    }
    assert not gate.ManagedRepositoryResidueGate._pod_is_exact_terminal(not_deleting)


def test_controller_fixture_has_no_post_or_auto_pull_mutation() -> None:
    program = gate.controller_program()
    assert "managed_repository_process_zero_receipts" in program
    assert "_release_process_zero_finalizer" in program
    assert "INSERT INTO jobs" in program
    assert "Posts" not in program
    assert "auto_pull" not in program
    assert "private" not in program.lower()


def test_safe_report_has_no_runtime_coordinates_or_credentials() -> None:
    config = gate.validate_config(_args())
    report = gate.plan_report(config).as_dict()
    encoded = json.dumps(report)
    assert "image" not in encoded
    assert "endpoint" not in encoded
    assert "password" not in encoded
    assert "PRIVATE KEY" not in encoded
