"""Focused safety tests for the temporary S2 lite-only admission gate."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from orchestrator.services.stateless_workspace_gate import (
    stateless_lite_workspace_check,
)


THREAD_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
VIRTUAL_GENERATION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _thread(metadata):
    return {
        "id": THREAD_ID,
        "execution_lane": "stateless",
        "metadata": metadata,
    }


def _metadata(backend: str = "virtual", **extra):
    return {
        "config_override": {"workspace": {"backend": backend}},
        **extra,
    }


@pytest.mark.parametrize(
    "metadata",
    [
        _metadata("virtual"),
        _metadata(
            "none",
            workspace_container={
                "git_remote_url": "ssh://gitea/thread.git",
                "repo_name": "thread-aaaaaaaa",
            },
        ),
        json.dumps(
            _metadata(
                "virtual",
                vm={},
                workspace_container={},
                _workspace_binding={
                    "generation": VIRTUAL_GENERATION,
                    "kind": "virtual",
                    "backing_id": "rclone:0123456789abcdef",
                    "ssh_host_key_fingerprint": None,
                },
            )
        ),
    ],
    ids=["virtual", "none-with-gitea", "valid-virtual-binding-json"],
)
def test_classifier_admits_only_unmaterialized_lite_workspaces(metadata):
    backend, refusal = stateless_lite_workspace_check(_thread(metadata))

    assert backend in {"virtual", "none"}
    assert refusal is None


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (_metadata("sandbox"), "declared_backend_not_lite"),
        ({}, "declared_backend_not_lite"),
        (_metadata("virtual", vm={"status": "ready"}), "vm_context_present"),
        (_metadata("virtual", vm=[]), "vm_context_malformed"),
        (
            _metadata("virtual", workspace_container={"status": "pending"}),
            "workspace_context_present",
        ),
        (
            _metadata("virtual", workspace_container=[]),
            "workspace_context_malformed",
        ),
        (
            _metadata(
                "virtual",
                _workspace_binding={
                    "generation": VIRTUAL_GENERATION,
                    "kind": "remote",
                    "backing_id": "k8s-pod:srw:uid",
                    "ssh_host_key_fingerprint": "SHA256:remote",
                },
            ),
            "non_virtual_workspace_binding",
        ),
        (
            _metadata("virtual", _workspace_binding=[]),
            "non_virtual_workspace_binding",
        ),
        (
            _metadata(
                "virtual",
                _workspace_binding={
                    "generation": "not-a-uuid",
                    "kind": "virtual",
                    "backing_id": "rclone:0123",
                    "ssh_host_key_fingerprint": None,
                },
            ),
            "virtual_workspace_binding_malformed",
        ),
        (
            _metadata(
                "virtual",
                _workspace_binding={
                    "generation": VIRTUAL_GENERATION,
                    "kind": "virtual",
                    "backing_id": "rclone:0123",
                    "ssh_host_key_fingerprint": None,
                    "future_identity": "must-not-degrade-open",
                },
            ),
            "virtual_workspace_binding_malformed",
        ),
        ("not-json", "declared_backend_not_lite"),
    ],
    ids=[
        "sandbox",
        "missing-backend",
        "vm-materialized",
        "vm-malformed",
        "workspace-materialized",
        "workspace-malformed",
        "remote-binding",
        "binding-malformed",
        "virtual-binding-malformed",
        "virtual-binding-extra-key",
        "metadata-malformed",
    ],
)
def test_classifier_refuses_non_lite_physical_and_malformed_state(metadata, reason):
    _backend, refusal = stateless_lite_workspace_check(_thread(metadata))

    assert refusal == reason


@pytest.mark.asyncio
async def test_stateless_vm_upgrade_refuses_before_grants_or_provisioning():
    from orchestrator import main as orch_main

    db = MagicMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": THREAD_ID,
            "execution_lane": "stateless",
            "metadata": _metadata(),
        }
    )
    provisioner = MagicMock()
    provisioner.is_available = True
    provisioner.create_thread_vm = AsyncMock()
    grants = AsyncMock()

    with (
        patch.object(orch_main, "require_internal", AsyncMock()),
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "vm_provisioner", provisioner),
        patch.object(orch_main, "_enforce_workspace_upgrade_grants", grants),
    ):
        with pytest.raises(HTTPException) as exc:
            await orch_main.agent_upgrade_thread_to_vm(MagicMock(), THREAD_ID)

    assert exc.value.status_code == 409
    grants.assert_not_awaited()
    provisioner.create_thread_vm.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_sandbox_upgrade_refuses_before_any_workspace_write():
    from orchestrator import main as orch_main

    db = MagicMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": THREAD_ID,
            "execution_lane": "stateless",
            "metadata": _metadata(),
        }
    )
    db.merge_thread_workspace_context = AsyncMock()
    provisioner = MagicMock()
    provisioner.is_available = True
    provisioner.in_cluster = True
    provisioner.create_workspace = AsyncMock()
    grants = AsyncMock()

    with (
        patch.object(orch_main, "require_internal", AsyncMock()),
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "container_provisioner", provisioner),
        patch.object(orch_main, "_enforce_workspace_upgrade_grants", grants),
    ):
        with pytest.raises(HTTPException) as exc:
            await orch_main.agent_upgrade_thread_to_workspace(
                MagicMock(),
                THREAD_ID,
                orch_main.ThreadWorkspaceUpgradeRequest(target_tier="sandbox"),
            )

    assert exc.value.status_code == 409
    grants.assert_not_awaited()
    db.merge_thread_workspace_context.assert_not_awaited()
    provisioner.create_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_config_cannot_mutate_workspace_tier():
    from orchestrator import main as orch_main

    db = MagicMock()
    db.merge_thread_config_override = AsyncMock()
    audit = AsyncMock()
    thread = {
        "id": THREAD_ID,
        "user_id": None,
        "execution_lane": "stateless",
        "metadata": _metadata(),
    }

    with (
        patch.object(orch_main, "postgres_db", db),
        patch.object(orch_main, "log_security_event", audit),
    ):
        with pytest.raises(HTTPException) as exc:
            await orch_main._apply_thread_config_update(
                THREAD_ID,
                thread,
                {"workspace": {"backend": "sandbox"}},
                None,
                request=MagicMock(),
                actor=None,
            )

    assert exc.value.status_code == 409
    db.merge_thread_config_override.assert_not_awaited()
    audit.assert_not_awaited()
