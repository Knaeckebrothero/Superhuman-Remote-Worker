"""Initialization preserves literal commands and refuses unproved readiness."""

import base64
from copy import deepcopy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from orchestrator.services.dispatch_guards import (
    VM_PARK_INITIALIZATION,
    VM_WAIT,
    vm_provisioning_decision,
)
from orchestrator.services.vm_readiness import VMReadinessService
from shared.workspace_initialization import (
    REQUEST_PATH,
    initialization_receipt,
    initialization_request,
    validate_initialization_request,
)
from tests.test_vm_readiness import FakeDB, FakeProvisioner, candidate
from tests import test_vm_readiness as readiness_tests
from vm_controller.guest_initialization import step_command
from vm_controller.workspace_initialization import inject_workspace_initialization


STEPS = [{"command": ["python3", "-c", "print('${JOB_ID}; $HOME `uname`\\n')"]}]
successful_ssh = readiness_tests.successful_ssh


def receipt(owner, request, phase="Succeeded"):
    return {
        "version": 1,
        "ownerId": owner,
        "revision": request["revision"],
        "phase": phase,
        "step": len(request["steps"]) if phase == "Succeeded" else 0,
        "exitCode": 0 if phase == "Succeeded" else 9 if phase == "Failed" else None,
    }


def test_cloud_init_roundtrip_keeps_commands_literal_and_out_of_root_runcmd():
    request = initialization_request(STEPS)
    original = deepcopy(request)
    rendered = inject_workspace_initialization(
        "#cloud-config\nruncmd:\n  - [systemctl, restart, ssh]\n",
        owner_id="00000000-0000-4000-8000-000000000001",
        request=request,
    )
    parsed = yaml.safe_load(rendered)
    payload = next(
        item for item in parsed["write_files"] if item["path"] == REQUEST_PATH
    )
    assert payload["owner"] == "root:root"
    assert payload["permissions"] == "0600"
    recovered = json.loads(base64.b64decode(payload["content"]))
    assert recovered["recipe"] == original == request
    assert parsed["runcmd"][0] == ["systemctl", "restart", "ssh"]
    assert STEPS[0]["command"] not in parsed["runcmd"]
    command = step_command(recovered["ownerId"], 0, STEPS[0]["command"], 90)
    assert command[command.index("--") + 1 :] == STEPS[0]["command"]
    assert "--expand-environment=no" in command
    assert "--property=User=agent-host" in command
    assert "--property=KillMode=control-group" in command


def test_changed_input_cannot_keep_the_original_revision():
    original = initialization_request(STEPS)
    original["steps"][0]["command"][0] = "different-command"
    with pytest.raises(ValueError, match="revision"):
        validate_initialization_request(original)


@pytest.mark.parametrize(
    "changes",
    [
        {"ownerId": "different-owner"},
        {"revision": "different-recipe"},
        {"exitCode": True},
        {"exitCode": None},
        {"step": True},
        {"phase": "Ready"},
        {"privateOutput": "must not reach the API"},
    ],
)
def test_stale_or_malformed_success_is_not_a_readiness_receipt(changes):
    request = initialization_request(STEPS)
    with pytest.raises(ValueError):
        initialization_receipt(
            {**receipt("owner", request), **changes},
            owner_id="owner",
            revision=request["revision"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["job", "thread"])
@pytest.mark.parametrize("phase", [None, "Running", "Failed", "Succeeded"])
async def test_open_ssh_does_not_start_work_until_initialization_succeeds(
    monkeypatch,
    successful_ssh,
    kind,
    phase,
):
    request = initialization_request(STEPS)
    row = candidate(initialization=request)
    db = FakeDB(**({"jobs": [row]} if kind == "job" else {"threads": [row]}))
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.10",
            "phase": "Running",
            "active_pod_uid": "pod-1",
        }
    )
    result = None if phase is None else receipt(row["entity_id"], request, phase)
    monkeypatch.setattr(
        "orchestrator.services.vm_initialization.read_vm_initialization",
        AsyncMock(return_value=result),
    )
    trigger = MagicMock()
    await VMReadinessService(db, provisioner, trigger_dispatch=trigger).run_cycle()
    statuses = [item[3].get("status") for item in db.promotions]
    assert ("ready" in statuses) is (phase == "Succeeded")
    if phase != "Succeeded":
        successful_ssh[1].assert_not_awaited()
    assert ("failed" in statuses) is (phase == "Failed")


@pytest.mark.parametrize(
    "now,expected", [(1000, VM_WAIT), (1100, VM_PARK_INITIALIZATION)]
)
def test_initialization_has_a_bounded_wait_without_recycling_partial_setup(
    now, expected
):
    assert (
        vm_provisioning_decision(
            {
                "status": "ssh_pending",
                "provisioned_at": 1,
                "initialization_started_at": 100,
            },
            provision_attempts=1,
            max_provision_attempts=3,
            now=now,
            timeout_s=600,
        )
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,accepted",
    [
        ("valid", True),
        ("stale", False),
        ("partial", False),
        ("invalid_failed_step", False),
        ("oversize", False),
        ("invalid_json", False),
    ],
)
async def test_receipt_reader_bounds_guest_data_and_checks_recipe_identity(
    monkeypatch, payload, accepted
):
    from contextlib import asynccontextmanager
    import sys
    from types import SimpleNamespace
    from orchestrator.services import vm_initialization as reader

    request = initialization_request(STEPS)
    content = receipt("owner", request)
    if payload == "stale":
        content["revision"] = "other"
    elif payload == "partial":
        content["step"] = 0
    elif payload == "invalid_failed_step":
        content.update(phase="Failed", step=len(STEPS), exitCode=9)
    output = (
        "x" * 4097
        if payload == "oversize"
        else "{"
        if payload == "invalid_json"
        else json.dumps(content)
    )

    @asynccontextmanager
    async def pinned(host, port, command, **kwargs):
        assert (host, port) == ("attested-guest", 22)
        assert kwargs["expected_host_key_fingerprint"] == "attested-pin"
        assert command.startswith("head -c 4097 -- /var/lib/")
        yield [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(sys.argv[1])",
            output,
        ]

    monkeypatch.setattr(reader, "pinned_agent_ssh_command", pinned)
    monkeypatch.setattr(reader, "resolve_ssh_key_path", lambda: "/test-key")
    result = await reader.read_vm_initialization(
        SimpleNamespace(
            host="attested-guest", port=22, ssh_host_key_fingerprint="attested-pin"
        ),
        owner_id="owner",
        request=request,
    )
    assert (result is not None) is accepted


@pytest.mark.asyncio
async def test_stale_receipt_cas_does_not_release_agent(monkeypatch, successful_ssh):
    request = initialization_request(STEPS)
    row = candidate(initialization=request)
    db = FakeDB(jobs=[row], promote_result=False)
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.10",
            "phase": "Running",
            "active_pod_uid": "pod-1",
        }
    )
    monkeypatch.setattr(
        "orchestrator.services.vm_initialization.read_vm_initialization",
        AsyncMock(return_value=receipt(row["entity_id"], request)),
    )
    await VMReadinessService(db, provisioner, trigger_dispatch=lambda: None).run_cycle()
    successful_ssh[1].assert_not_awaited()
    assert not any(item[3].get("status") == "ready" for item in db.promotions)


@pytest.mark.asyncio
@pytest.mark.parametrize("started", ["invalid", -1, True, float("nan"), float("inf")])
async def test_invalid_initialization_clock_cannot_create_an_unbounded_wait(
    monkeypatch, successful_ssh, started
):
    request = initialization_request(STEPS)
    row = candidate(initialization=request, initialization_started_at=started)
    db = FakeDB(jobs=[row])
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.10",
            "phase": "Running",
            "active_pod_uid": "pod-1",
        }
    )
    monkeypatch.setattr(
        "orchestrator.services.vm_initialization.read_vm_initialization",
        AsyncMock(return_value=None),
    )
    await VMReadinessService(db, provisioner, trigger_dispatch=lambda: None).run_cycle()
    assert any(item[3].get("status") == "failed" for item in db.promotions)
    successful_ssh[1].assert_not_awaited()
