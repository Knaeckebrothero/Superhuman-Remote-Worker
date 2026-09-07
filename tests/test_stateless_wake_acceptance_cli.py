from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.operator_cli.stateless_wake_acceptance import (
    CONFIRMATION,
    GateError,
    _async_main,
    _marker,
    _safe_delivery,
    main,
    parse_args,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE = "orchestrator.operator_cli.stateless_wake_acceptance"


def test_default_mode_is_read_only_inspection():
    args = parse_args([])

    assert args.execute is False
    assert args.cleanup_only is False
    assert args.run_id is None
    assert args.owner_user_id is None


@pytest.mark.parametrize(
    "argv",
    [
        ["--execute"],
        ["--execute", "--run-id", "wake-gate-001", "--confirm", CONFIRMATION],
        ["--cleanup-only", "--run-id", "wake-gate-001"],
        ["--run-id", "wake-gate-001"],
        ["--confirm", CONFIRMATION],
    ],
)
def test_mutation_requires_all_explicit_guards(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_execute_arguments_are_canonicalized():
    owner = str(uuid4())
    args = parse_args(
        [
            "--execute",
            "--run-id",
            "wake-gate-001",
            "--owner-user-id",
            owner,
            "--confirm",
            CONFIRMATION,
        ]
    )

    assert args.execute is True
    assert args.run_id == "wake-gate-001"
    assert args.owner_user_id == owner


@pytest.mark.asyncio
async def test_direct_mutation_requires_wrapper_context_attestation(monkeypatch):
    monkeypatch.delenv("SRW_WAKE_GATE_CONTEXT", raising=False)
    args = parse_args(
        [
            "--cleanup-only",
            "--run-id",
            "wake-gate-001",
            "--confirm",
            CONFIRMATION,
        ]
    )

    with pytest.raises(GateError, match="host wrapper") as raised:
        await _async_main(args)
    assert raised.value.code == "wrong_context"


def test_safe_delivery_projection_excludes_content_and_credentials():
    source = {
        "delivery_id": str(uuid4()),
        "state": "settled",
        "execution_lane": "stateless",
        "claim_generation": 2,
        "owner_run_queue_lease_token": 7,
        "owner_executor": "srw-agent-stateless-example",
        "owner_executor_pod_uid": str(uuid4()),
        "admitted_turn_number": 3,
        "content": "postgresql://operator:secret@db.internal/srw",
        "private_key": "super-secret-key",
    }

    result = _safe_delivery(source)

    assert set(result) == {
        "delivery_id",
        "state",
        "execution_lane",
        "claim_generation",
        "lease_token",
        "executor",
        "executor_pod_uid",
        "admitted_turn_number",
    }
    assert "secret" not in json.dumps(result)


def test_fixture_marker_is_exact_and_non_descriptive():
    assert _marker("wake-gate-001") == {
        "kind": "stateless_durable_wake_k3d",
        "run_id": "wake-gate-001",
    }


def test_top_level_unexpected_failure_is_redacted(monkeypatch, capsys):
    secret_shaped_error = (
        "postgresql://operator:password@internal-db:5432/srw "
        "https://admin:password@forge.internal/private"
    )

    async def fail(_args):
        raise RuntimeError(secret_shaped_error)

    monkeypatch.setattr(
        "orchestrator.operator_cli.stateless_wake_acceptance._async_main", fail
    )

    assert main([]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "code": "unexpected_exception",
        "event": "gate_failed",
        "exception_class": "RuntimeError",
        "status": "error",
    }
    assert secret_shaped_error not in json.dumps(payload)


def test_operator_module_runs_from_installed_package_layout(tmp_path):
    source_root = tmp_path / "src"
    package_root = source_root / "orchestrator"
    package_root.mkdir(parents=True)
    shutil.copy2(ROOT / "src/orchestrator/__init__.py", package_root / "__init__.py")
    shutil.copytree(
        ROOT / "src/orchestrator/operator_cli", package_root / "operator_cli"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    environment["PYTHONSAFEPATH"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", MODULE, "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--execute" in result.stdout
    assert "--cleanup-only" in result.stdout


def test_production_image_build_checks_deployed_module_help():
    dockerfile = (ROOT / "docker" / "Dockerfile.orchestrator").read_text()
    assert f"RUN python -m {MODULE} --help >/dev/null" in dockerfile


def test_host_wrapper_has_context_artifact_and_confirmation_fences():
    wrapper = (ROOT / "scripts" / "stateless-durable-wake-k3d-gate.sh").read_text()

    assert 'EXPECTED_CONTEXT="k3d-srw"' in wrapper
    assert "kubectl config current-context" in wrapper
    assert "stateless_wake_acceptance.py" in wrapper
    assert "0191_stateless_input_deliveries.sql" in wrapper
    assert "0192_stateless_input_delivery_validate.sql" in wrapper
    assert "0197_non_pinned_workspace_process_zero.sql" in wrapper
    assert "0198_non_pinned_workspace_lifecycle_authority.sql" in wrapper
    assert "require_deployment_converged" in wrapper
    assert "WORKSPACE_CLEANUP_RECONCILIATION_ENABLED" in wrapper
    assert "WORKSPACE_REATTACH_FRESH_FALLBACK" in wrapper
    assert "OFFICER_AUTO_PULL_RELEASE_ENABLED" in wrapper
    assert "K8s Pod IPs are not recipient authority" in wrapper
    assert "SRW_WAKE_GATE_CONTEXT" in wrapper
    assert "python -m orchestrator.operator_cli.stateless_wake_acceptance" in wrapper
    assert "postgresql://" not in wrapper


def test_runbook_requires_zero_residue_and_never_enables_auto_pull():
    runbook = (ROOT / "tests" / "stateless_durable_wake_k3d_validation.md").read_text()

    assert "warm_fifo" in runbook
    assert "fresh_attach" in runbook
    assert "lease_handoff" in runbook
    assert "lost_response_historical_lane" in runbook
    assert "auto_pull_enabled` is zero" in runbook
    assert "--cleanup-only" in runbook
