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
        "stateless_deployment": "srw-agent-stateless",
        "workspace_image": None,
        "gate_id": "srw-mr-residue-012345abcdef",
        "timeout_seconds": 240,
        "run": False,
        "cleanup_only": False,
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
    with pytest.raises(gate.SafetyError, match="requires"):
        gate.validate_config(_args(cleanup_only=True))
    with pytest.raises(gate.SafetyError, match="explicit --gate-id"):
        gate.validate_config(
            _args(
                gate_id=None,
                cleanup_only=True,
                confirm="LOCAL-K3D-DISPOSABLE",
            )
        )
    cleanup = gate.validate_config(
        _args(cleanup_only=True, confirm="LOCAL-K3D-DISPOSABLE")
    )
    assert cleanup.cleanup_only is True
    with pytest.raises(gate.SafetyError, match="mutually exclusive"):
        gate.validate_config(
            _args(
                run=True,
                cleanup_only=True,
                confirm="LOCAL-K3D-DISPOSABLE",
            )
        )


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
    reservation_id = "11111111-2222-4333-8444-555555555555"
    manifest = gate.build_workspace_pod_manifest(
        config,
        "registry.local:5000/workspace:gate",
        creation_reservation_id=reservation_id,
    )
    metadata = manifest["metadata"]
    assert metadata["name"] == config.gate_id
    assert metadata["namespace"] == "srw"
    assert metadata["labels"]["srw/job-id"] == config.owner_id
    assert metadata["labels"][gate.GATE_LABEL] == config.gate_id
    assert (
        metadata["annotations"][gate.CREATION_RESERVATION_ANNOTATION] == reservation_id
    )
    assert metadata["finalizers"] == [gate.PROCESS_ZERO_FINALIZER]
    encoded = json.dumps(manifest)
    assert "secret" not in encoded.lower()
    assert "auto_pull" not in encoded
    assert len(manifest["spec"]["containers"]) == 1
    assert manifest["spec"]["containers"][0]["volumeMounts"] == [
        {"name": "workspace-data", "mountPath": "/home/agent-host"}
    ]
    assert manifest["spec"]["volumes"] == [
        {
            "name": "workspace-data",
            "persistentVolumeClaim": {"claimName": config.pvc_name},
        }
    ]


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
    assert "0193_managed_repository_process_zero_authority.sql" in program
    assert "0197_non_pinned_workspace_process_zero.sql" in program
    assert "0198_non_pinned_workspace_lifecycle_authority.sql" in program
    assert "stale_managed_repository_workspace_process_zero_is_current" in program
    assert "_managed_repository_process_zero_replay_is_current" in program
    assert 'mode == "finalizer-release-lost"' in program
    assert 'mode == "workspace-delete-replay"' in program
    assert "WORKSPACE_DELETE_REPLAYED" in program
    assert 'mode == "owner-shared-resources"' in program
    assert "record_managed_repository_workspace_creation_resource" in program
    assert "_create_pvc" in program
    assert "_create_service" in program
    assert 'mode == "workspace-prepare"' in program
    assert "prepare_workspace_cleanup_intent" in program
    assert 'mode == "workspace-reconcile"' in program
    assert "reconcile_workspace_cleanup_intent" in program
    assert 'mode == "owner-reserve"' in program
    assert "reserve_managed_repository_workspace_creation" in program
    assert "mark_managed_repository_workspace_creation_started" in program
    assert "managed_repository_workspace_creation_claim_is_current" in program
    assert "authorize_managed_repository_workspace_creation_runtime" in program
    assert "settle_managed_repository_workspace_creation_reservation" in program
    assert "request_workspace_creation_cancellation" in program
    assert "_require_workspace_creation_reservation_annotation" in program
    assert '"_creation_reservation_id": reservation_id' in program
    assert '"_creation_claim_token": str(claim_token)' in program
    assert "owner ledger cleanup refused" in program
    assert "AND settled_at IS NOT NULL" in program
    assert "provisioner.is_available" in program
    assert "provisioner.available" not in program
    assert "INSERT INTO jobs" in program
    assert "'paused', '{}'::jsonb" in program
    assert "'processing', '{}'::jsonb" not in program
    assert "Posts" not in program
    assert "post_auto_pull" in program
    assert "thread_auto_pull" in program
    assert "UPDATE project_officers" not in program
    assert "UPDATE threads SET metadata" not in program
    assert "private" not in program.lower()


def test_run_sequence_retires_a_then_publishes_b_and_replays_lost_response() -> None:
    source = _SCRIPT.read_text()
    assert "self._replace_retired_predecessor_with_current_successor()" in source
    assert "if self.pod_uid == bound_uid" in source
    assert '"finalizer-release-lost"' in source
    assert '"workspace-delete-replay"' in source
    assert "self._replay_retired_predecessor_against_current_successor(" in source
    report = gate.plan_report(gate.validate_config(_args())).as_dict()
    phases = [item["name"] for item in report["phases"]]
    assert "retired_predecessor_and_current_successor" in phases
    assert "current_successor_lost_response_and_404_replay" in phases


def test_creation_envelope_is_strict() -> None:
    envelope = gate.CreationEnvelope.from_payload(
        json.dumps(
            {
                "reservation_id": "11111111-2222-4333-8444-555555555555",
                "generation": 7,
                "claim_token": 11,
                "settled": False,
            }
        )
    )
    assert envelope.generation == 7
    assert envelope.claim_token == 11
    assert envelope.settled is False
    with pytest.raises(gate.GateFailure, match="malformed"):
        gate.CreationEnvelope.from_payload(
            json.dumps(
                {
                    "reservation_id": "not-a-uuid",
                    "generation": 7,
                    "claim_token": 11,
                    "settled": False,
                }
            )
        )
    with pytest.raises(gate.GateFailure, match="malformed"):
        gate.CreationEnvelope.from_payload(
            json.dumps(
                {
                    "reservation_id": "11111111-2222-4333-8444-555555555555",
                    "generation": True,
                    "claim_token": 11,
                    "settled": False,
                }
            )
        )


def test_shared_resource_envelope_is_strict() -> None:
    envelope = gate.SharedResourceEnvelope.from_payload(
        json.dumps(
            {
                "pvc_name": "pvc-workspace-01234567-89a",
                "pvc_uid": "11111111-2222-4333-8444-555555555555",
                "service_name": "srw-mr-residue-012345abcdef",
                "service_uid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            }
        )
    )
    assert envelope.service_uid == "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    with pytest.raises(gate.GateFailure, match="malformed"):
        gate.SharedResourceEnvelope.from_payload(
            json.dumps(
                {
                    "pvc_name": "../foreign",
                    "pvc_uid": "11111111-2222-4333-8444-555555555555",
                    "service_name": "srw-mr-residue-012345abcdef",
                    "service_uid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                }
            )
        )


def test_create_orders_reservation_before_external_create_and_exact_bind(
    monkeypatch,
) -> None:
    config = gate.validate_config(
        _args(workspace_image="registry.local:5000/workspace:gate")
    )
    events: list[tuple[str, object]] = []
    reservation_id = "11111111-2222-4333-8444-555555555555"
    pod_uid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    class Runner:
        def run(self, arguments, *, input_data=None, **_kwargs):
            manifest = json.loads(input_data) if input_data else None
            events.append(("external_create", manifest))
            return gate.CommandResult(0, b"")

    subject = gate.ManagedRepositoryResidueGate(config, Runner())
    monkeypatch.setattr(subject, "_pod_json", lambda: None)
    monkeypatch.setattr(subject, "_wait_running", lambda: pod_uid)

    def controller(mode, *arguments, marker):
        events.append((mode, arguments))
        if mode == "owner-reserve":
            assert marker == "OWNER_RESERVED"
            return json.dumps(
                {
                    "reservation_id": reservation_id,
                    "generation": 3,
                    "claim_token": 5,
                    "settled": False,
                }
            )
        if mode == "owner-creation-current":
            assert marker == "CREATION_CURRENT"
            return ""
        if mode == "owner-shared-resources":
            assert marker == "SHARED_RESOURCES"
            return json.dumps(
                {
                    "pvc_name": config.pvc_name,
                    "pvc_uid": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
                    "service_name": config.pod_name,
                    "service_uid": "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa",
                }
            )
        assert mode == "owner-bind"
        assert marker == "OWNER_BOUND"
        return ""

    monkeypatch.setattr(subject, "_controller", controller)
    monkeypatch.setattr(subject, "_pod_exec", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        subject, "_assert_shared_resources_current", lambda **_kwargs: None
    )
    subject._create_pod("predecessor")

    assert [event[0] for event in events] == [
        "owner-reserve",
        "owner-creation-current",
        "owner-shared-resources",
        "external_create",
        "owner-bind",
    ]
    manifest = events[3][1]
    assert isinstance(manifest, dict)
    assert (
        manifest["metadata"]["annotations"][gate.CREATION_RESERVATION_ANNOTATION]
        == reservation_id
    )
    bind_arguments = events[4][1]
    assert bind_arguments[1] == "predecessor"
    assert bind_arguments[2] == pod_uid
    assert bind_arguments[5:] == (reservation_id, "3", "5")


def test_successor_refuses_a_different_shared_resource_generation(monkeypatch) -> None:
    config = gate.validate_config(
        _args(workspace_image="registry.local:5000/workspace:gate")
    )
    subject = gate.ManagedRepositoryResidueGate(config, SimpleNamespace())
    subject.shared_resources = gate.SharedResourceEnvelope(
        config.pvc_name,
        "11111111-2222-4333-8444-555555555555",
        config.pod_name,
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    )
    monkeypatch.setattr(subject, "_pod_json", lambda: None)

    def controller(mode, *_arguments, marker):
        if mode == "owner-reserve":
            return json.dumps(
                {
                    "reservation_id": "99999999-2222-4333-8444-555555555555",
                    "generation": 8,
                    "claim_token": 9,
                    "settled": False,
                }
            )
        if mode == "owner-creation-current":
            return ""
        assert mode == "owner-shared-resources"
        assert marker == "SHARED_RESOURCES"
        return json.dumps(
            {
                "pvc_name": config.pvc_name,
                "pvc_uid": "11111111-2222-4333-8444-555555555555",
                "service_name": config.pod_name,
                "service_uid": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
            }
        )

    monkeypatch.setattr(subject, "_controller", controller)
    with pytest.raises(gate.GateFailure, match="reuse exact shared"):
        subject._create_pod("successor")


def test_cleanup_cancels_crashed_creation_before_owner_delete(monkeypatch) -> None:
    config = gate.validate_config(_args())
    subject = gate.ManagedRepositoryResidueGate(config, SimpleNamespace())
    subject.owner_created = True
    calls: list[str] = []

    def controller(mode, *_arguments, marker):
        calls.append(mode)
        assert marker in {"OWNER_TERMINAL", "CREATION_CANCELLED", "OWNER_DELETED"}
        return ""

    monkeypatch.setattr(subject, "_controller", controller)
    monkeypatch.setattr(subject, "_pod_json", lambda: None)
    monkeypatch.setattr(subject, "_resource_json", lambda *_args: None)
    assert subject.cleanup_exact()
    assert calls == ["owner-terminal", "owner-cancel-creation", "owner-delete"]


def test_settled_response_replay_adopts_exact_same_generation_without_create(
    monkeypatch,
) -> None:
    config = gate.validate_config(
        _args(workspace_image="registry.local:5000/workspace:gate")
    )
    reservation_id = "11111111-2222-4333-8444-555555555555"
    pod_uid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    class Runner:
        def run(self, *_args, **_kwargs):
            raise AssertionError("settled replay must not create a Pod")

    subject = gate.ManagedRepositoryResidueGate(config, Runner())
    monkeypatch.setattr(
        subject,
        "_pod_json",
        lambda: {
            "metadata": {
                "name": config.pod_name,
                "uid": pod_uid,
                "labels": {
                    "srw/job-id": config.owner_id,
                    gate.GATE_LABEL: config.gate_id,
                },
                "annotations": {gate.CREATION_RESERVATION_ANNOTATION: reservation_id},
            }
        },
    )
    monkeypatch.setattr(subject, "_wait_running", lambda: pod_uid)
    monkeypatch.setattr(
        subject,
        "_exact_shared_resource_uids",
        lambda: (
            "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
            "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa",
        ),
    )
    monkeypatch.setattr(subject, "_pod_exec", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        subject, "_assert_shared_resources_current", lambda **_kwargs: None
    )
    calls: list[str] = []

    def controller(mode, *_arguments, marker):
        calls.append(mode)
        if mode == "owner-reserve":
            assert marker == "OWNER_RESERVED"
            return json.dumps(
                {
                    "reservation_id": reservation_id,
                    "generation": 3,
                    "claim_token": 5,
                    "settled": True,
                }
            )
        assert mode == "owner-bind"
        assert marker == "OWNER_BOUND"
        return ""

    monkeypatch.setattr(subject, "_controller", controller)
    subject._create_pod("predecessor")
    assert calls == ["owner-reserve", "owner-bind"]


def test_artifact_preflight_checks_agent_code_and_workspace_binaries() -> None:
    config = gate.validate_config(_args())
    calls: list[tuple[list[str], bytes | None]] = []

    class Runner:
        def run(self, arguments, *, operation, input_data=None, **_kwargs):
            calls.append((list(arguments), input_data))
            return gate.CommandResult(0, b"")

    subject = gate.ManagedRepositoryResidueGate(config, Runner())
    subject.orchestrator_pods = ["srw-orchestrator-a", "srw-orchestrator-b"]
    subject._deployment_pods = lambda *_args, **_kwargs: [
        "srw-agent-stateless-a",
        "srw-agent-stateless-b",
    ]
    subject._runtime_artifact_preflight()
    subject._workspace_artifact_preflight()

    assert [call[0][1] for call in calls[:4]] == [
        "srw-orchestrator-a",
        "srw-orchestrator-b",
        "srw-agent-stateless-a",
        "srw-agent-stateless-b",
    ]
    for agent in calls[2:4]:
        assert agent[0][2:4] == ["-c", "agent"]
        assert b"9>&-" in (agent[1] or b"")
    assert b"/app/database/migrations/app/0197_" in (calls[0][1] or b"")
    assert b"record_managed_repository_workspace_creation_resource" in (
        calls[0][1] or b""
    )
    assert b"reconcile_workspace_cleanup_intent" in (calls[0][1] or b"")
    assert b"/app/orchestrator/database" not in (calls[0][1] or b"")
    workspace = calls[4]
    assert workspace[0][2] == config.pod_name
    assert "command -v ssh-agent" in workspace[0][-1]
    assert "from src" not in workspace[0][-1]


def _deployment_payload(*, replicas=2, updated=2, ready=2, available=2):
    return {
        "metadata": {"generation": 7},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": "srw-orchestrator"}},
            "template": {
                "spec": {
                    "containers": [
                        {"name": "orchestrator", "image": "registry/orch:gate"}
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 7,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
        },
    }


def _deployment_pod(name, *, ready=True, deleting=False, template_hash="abc"):
    metadata = {
        "name": name,
        "labels": {"pod-template-hash": template_hash},
    }
    if deleting:
        metadata["deletionTimestamp"] = "now"
    return {
        "metadata": metadata,
        "spec": {
            "containers": [{"name": "orchestrator", "image": "registry/orch:gate"}]
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "orchestrator",
                    "ready": ready,
                    "imageID": "registry/orch@sha256:" + "a" * 64,
                }
            ],
        },
    }


def test_deployment_inventory_requires_every_replica_and_one_running_digest() -> None:
    config = gate.validate_config(_args())

    class Runner:
        def __init__(self, deployment, pods):
            self.deployment = deployment
            self.pods = pods

        def run(self, arguments, **_kwargs):
            payload = (
                self.deployment
                if arguments[1] == "deployment"
                else {"items": self.pods}
            )
            return gate.CommandResult(0, json.dumps(payload).encode())

    subject = gate.ManagedRepositoryResidueGate(
        config,
        Runner(
            _deployment_payload(),
            [_deployment_pod("orch-a"), _deployment_pod("orch-b")],
        ),
    )
    assert subject._deployment_pods(
        "srw-orchestrator", container_name="orchestrator"
    ) == ["orch-a", "orch-b"]

    subject.runner = Runner(
        _deployment_payload(),
        [
            _deployment_pod("orch-a"),
            _deployment_pod("orch-b"),
            _deployment_pod("orch-old", deleting=True, template_hash="old"),
        ],
    )
    with pytest.raises(gate.GateFailure, match="not fully converged"):
        subject._deployment_pods("srw-orchestrator", container_name="orchestrator")

    subject.runner = Runner(
        _deployment_payload(updated=1, ready=1, available=1),
        [_deployment_pod("orch-a")],
    )
    with pytest.raises(gate.GateFailure, match="not converged"):
        subject._deployment_pods("srw-orchestrator", container_name="orchestrator")


def test_dark_config_preflight_requires_all_three_release_fences() -> None:
    config = gate.validate_config(_args())

    class Runner:
        def __init__(self, data):
            self.data = data

        def run(self, *_args, **_kwargs):
            return gate.CommandResult(0, json.dumps({"data": self.data}).encode())

    required = {
        "WORKSPACE_CLEANUP_RECONCILIATION_ENABLED": "false",
        "WORKSPACE_REATTACH_FRESH_FALLBACK": "false",
        "OFFICER_AUTO_PULL_RELEASE_ENABLED": "false",
    }
    gate.ManagedRepositoryResidueGate(config, Runner(required))._dark_config_preflight()
    unsafe = dict(required)
    unsafe["OFFICER_AUTO_PULL_RELEASE_ENABLED"] = "true"
    with pytest.raises(gate.GateFailure, match="not dark"):
        gate.ManagedRepositoryResidueGate(
            config, Runner(unsafe)
        )._dark_config_preflight()


def test_cleanup_only_adopts_exact_owner_and_labeled_pod(monkeypatch) -> None:
    config = gate.validate_config(_args())
    subject = gate.ManagedRepositoryResidueGate(config, SimpleNamespace())
    runtime_uid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    monkeypatch.setattr(
        subject,
        "_owner_inspect",
        lambda: {
            "owner_exists": True,
            "runtime_incarnation": runtime_uid,
            "active_creations": 0,
            "pending_cleanups": 0,
            "receipts": 1,
            "owner_status": "paused",
            "pod_uids": [runtime_uid],
            "pvc_uids": [],
            "service_uids": [],
            "active_reservation_ids": [],
        },
    )
    monkeypatch.setattr(subject, "_resource_json", lambda *_args: None)
    monkeypatch.setattr(
        subject,
        "_pod_json",
        lambda: {
            "metadata": {
                "name": config.pod_name,
                "uid": runtime_uid,
                "labels": {
                    gate.GATE_LABEL: config.gate_id,
                    "srw/job-id": config.owner_id,
                },
            }
        },
    )
    subject._adopt_cleanup_state()
    assert subject.owner_created is True
    assert subject.owner_bound is True
    assert subject.owner_runtime_uid == runtime_uid
    assert subject.pod_uid == runtime_uid


def _shared_pvc(config, uid, *, reservation_id=None):
    metadata = {
        "name": config.pvc_name,
        "namespace": config.namespace,
        "uid": uid,
        "labels": {
            "app": "srw-workspace",
            "srw/component": "workspace-pvc",
            "srw.io/component": "agent-workspace",
            "srw/job-id": config.owner_id,
            gate.GATE_LABEL: config.gate_id,
        },
    }
    if reservation_id:
        metadata["annotations"] = {gate.CREATION_RESERVATION_ANNOTATION: reservation_id}
    return {
        "metadata": metadata,
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "volumeMode": "Filesystem",
        },
    }


def _shared_service(config, uid, *, reservation_id=None):
    metadata = {
        "name": config.pod_name,
        "namespace": config.namespace,
        "uid": uid,
        "labels": {
            "app": "srw-workspace",
            "srw/component": "workspace-svc",
            "srw.io/component": "agent-workspace",
            "srw/job-id": config.owner_id,
            gate.GATE_LABEL: config.gate_id,
        },
    }
    if reservation_id:
        metadata["annotations"] = {gate.CREATION_RESERVATION_ANNOTATION: reservation_id}
    return {
        "metadata": metadata,
        "spec": {
            "clusterIP": "None",
            "selector": {"app": "srw-workspace", "srw/job-id": config.owner_id},
            "ports": [
                {
                    "name": "ssh",
                    "port": 30022,
                    "targetPort": 30022,
                    "protocol": "TCP",
                },
                {
                    "name": "code-server",
                    "port": 38080,
                    "targetPort": 38080,
                    "protocol": "TCP",
                },
                {
                    "name": "cdp",
                    "port": 9222,
                    "targetPort": 9222,
                    "protocol": "TCP",
                },
            ],
        },
    }


def test_cleanup_adoption_requires_uid_ledger_or_exact_active_reservation(
    monkeypatch,
) -> None:
    config = gate.validate_config(_args())
    pvc_uid = "11111111-2222-4333-8444-555555555555"
    service_uid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    reservation_id = "99999999-2222-4333-8444-555555555555"
    subject = gate.ManagedRepositoryResidueGate(config, SimpleNamespace())
    state = {
        "owner_exists": True,
        "runtime_incarnation": None,
        "active_creations": 1,
        "pending_cleanups": 0,
        "receipts": 0,
        "owner_status": "paused",
        "pod_uids": [],
        "pvc_uids": [],
        "service_uids": [],
        "active_reservation_ids": [reservation_id],
    }
    monkeypatch.setattr(subject, "_owner_inspect", lambda: state)
    resources = {
        "persistentvolumeclaim": _shared_pvc(config, pvc_uid),
        "service": _shared_service(config, service_uid),
    }
    monkeypatch.setattr(subject, "_resource_json", lambda kind, _name: resources[kind])
    monkeypatch.setattr(subject, "_pod_json", lambda: None)
    with pytest.raises(gate.GateFailure, match="not backed"):
        subject._adopt_cleanup_state()

    resources["persistentvolumeclaim"] = _shared_pvc(
        config, pvc_uid, reservation_id=reservation_id
    )
    resources["service"] = _shared_service(
        config, service_uid, reservation_id=reservation_id
    )
    # The production Service create and the gate-label patch are two external
    # calls. Cleanup-only may adopt that narrow crossed edge only through the
    # exact active reservation annotation.
    del resources["service"]["metadata"]["labels"][gate.GATE_LABEL]
    subject._adopt_cleanup_state()
    assert subject.shared_resources == gate.SharedResourceEnvelope(
        config.pvc_name,
        pvc_uid,
        config.pod_name,
        service_uid,
    )


def test_cleanup_adoption_refuses_same_name_foreign_service_shape(monkeypatch) -> None:
    config = gate.validate_config(_args())
    service_uid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    service = _shared_service(config, service_uid)
    service["spec"]["selector"]["srw/job-id"] = "foreign"
    subject = gate.ManagedRepositoryResidueGate(config, SimpleNamespace())
    monkeypatch.setattr(
        subject,
        "_owner_inspect",
        lambda: {
            "owner_exists": True,
            "runtime_incarnation": None,
            "active_creations": 0,
            "pending_cleanups": 0,
            "receipts": 0,
            "owner_status": "paused",
            "pod_uids": [],
            "pvc_uids": [],
            "service_uids": [service_uid],
            "active_reservation_ids": [],
        },
    )
    monkeypatch.setattr(
        subject,
        "_resource_json",
        lambda kind, _name: service if kind == "service" else None,
    )
    with pytest.raises(gate.GateFailure, match="Service identity changed"):
        subject._adopt_cleanup_state()


def test_stale_cleanup_replay_preserves_pod_resources_and_managed_process(
    monkeypatch,
) -> None:
    config = gate.validate_config(_args())
    subject = gate.ManagedRepositoryResidueGate(config, SimpleNamespace())
    predecessor_uid = "11111111-2222-4333-8444-555555555555"
    successor_uid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    subject.predecessor_uid = predecessor_uid
    subject.predecessor_cleanup_generation = 17
    subject.pod_uid = successor_uid
    state = {
        "pid": "73",
        "starttime": "801",
        "generation": "2",
        "workspace_generation": config.owner_id,
        "runtime_incarnation": successor_uid,
    }
    monkeypatch.setattr(subject, "_agent_state", lambda _authority: dict(state))
    monkeypatch.setattr(
        subject,
        "_pod_json",
        lambda: {"metadata": {"uid": successor_uid}},
    )
    reconciled = []
    monkeypatch.setattr(
        subject,
        "_reconcile_cleanup",
        lambda uid, generation: reconciled.append((uid, generation)),
    )
    checked = []
    monkeypatch.setattr(
        subject,
        "_assert_shared_resources_current",
        lambda **kwargs: checked.append(kwargs),
    )
    subject._replay_retired_predecessor_against_current_successor(
        authority_id="authority",
        expected_agent_pid=73,
    )
    assert reconciled == [(predecessor_uid, 17)]
    assert checked == [
        {"expected_pod_uid": successor_uid, "require_successor_marker": True}
    ]


def test_shared_service_must_select_only_the_exact_current_pod(monkeypatch) -> None:
    config = gate.validate_config(_args())
    pod_uid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    subject = gate.ManagedRepositoryResidueGate(config, SimpleNamespace())
    subject.shared_resources = gate.SharedResourceEnvelope(
        config.pvc_name,
        "11111111-2222-4333-8444-555555555555",
        config.pod_name,
        "99999999-2222-4333-8444-555555555555",
    )
    monkeypatch.setattr(
        subject,
        "_exact_shared_resource_uids",
        lambda: (
            subject.shared_resources.pvc_uid,
            subject.shared_resources.service_uid,
        ),
    )
    monkeypatch.setattr(subject, "_pod_exec", lambda *_args, **_kwargs: None)
    foreign_uid = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    monkeypatch.setattr(
        subject,
        "_resource_json",
        lambda *_args: {
            "subsets": [{"addresses": [{"targetRef": {"uid": foreign_uid}}]}]
        },
    )
    with pytest.raises(gate.GateFailure, match="foreign Pod"):
        subject._assert_shared_resources_current(
            expected_pod_uid=pod_uid,
            require_successor_marker=True,
        )

    monkeypatch.setattr(
        subject,
        "_resource_json",
        lambda *_args: {"subsets": [{"addresses": [{"targetRef": {"uid": pod_uid}}]}]},
    )
    subject._assert_shared_resources_current(
        expected_pod_uid=pod_uid,
        require_successor_marker=True,
    )


def test_zero_residue_checks_all_gate_labeled_resource_kinds(monkeypatch) -> None:
    config = gate.validate_config(_args())
    calls = []

    class Runner:
        def run(self, arguments, **_kwargs):
            calls.append(arguments)
            return gate.CommandResult(
                0,
                (
                    b'{"items":[]}'
                    if arguments[1] == "pods,configmaps,persistentvolumeclaims,services"
                    else b""
                ),
            )

    subject = gate.ManagedRepositoryResidueGate(config, Runner())
    monkeypatch.setattr(
        subject,
        "_owner_inspect",
        lambda: {
            "owner_exists": False,
            "runtime_incarnation": None,
            "active_creations": 0,
            "pending_cleanups": 0,
            "receipts": 0,
            "owner_status": None,
            "pod_uids": [],
            "pvc_uids": [],
            "service_uids": [],
            "active_reservation_ids": [],
        },
    )
    subject._assert_zero_residue()
    assert calls[-1][:2] == [
        "get",
        "pods,configmaps,persistentvolumeclaims,services",
    ]
    assert f"{gate.GATE_LABEL}={config.gate_id}" in calls[-1]


def test_safe_report_has_no_runtime_coordinates_or_credentials() -> None:
    config = gate.validate_config(_args())
    report = gate.plan_report(config).as_dict()
    encoded = json.dumps(report)
    assert "image" not in encoded
    assert "endpoint" not in encoded
    assert "password" not in encoded
    assert "PRIVATE KEY" not in encoded
