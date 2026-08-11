"""Focused safety tests for stateless session workspace admission."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from orchestrator.services.stateless_workspace_gate import (
    stateless_workspace_check,
)


THREAD_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
VIRTUAL_GENERATION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SANDBOX_GENERATION = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SANDBOX_RUNTIME = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


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


def _k8s_sandbox_metadata(*, status: str = "ready", **workspace_extra):
    workspace = {
        "status": status,
        "provisioner": "k8s",
    }
    metadata = _metadata("sandbox", workspace_container=workspace)
    if status == "ready":
        workspace.update(
            {
                "pod_ip": "10.42.0.25",
                "port": 30022,
                "pod_name": "ws-thread-aaaaaaaaaaaa",
                "namespace": "agent-workspaces",
                "_canvas_workspace_generation": SANDBOX_GENERATION,
                "_runtime_incarnation": SANDBOX_RUNTIME,
            }
        )
        metadata["_workspace_binding"] = {
            "generation": SANDBOX_GENERATION,
            "kind": "remote",
            "backing_id": "k8s-pvc:agent-workspaces:pvc-uid",
            "ssh_host_key_fingerprint": "SHA256:trusted",
        }
    workspace.update(workspace_extra)
    return metadata


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
    backend, refusal = stateless_workspace_check(_thread(metadata))

    assert backend in {"virtual", "none"}
    assert refusal is None


@pytest.mark.parametrize(
    "metadata",
    [
        _k8s_sandbox_metadata(),
        _k8s_sandbox_metadata(status="pending"),
        _k8s_sandbox_metadata(status="creating", pod_name="ws-thread-aaaaaaaaaaaa"),
        _k8s_sandbox_metadata(status="restoring"),
        _k8s_sandbox_metadata(status="suspended"),
        _k8s_sandbox_metadata(status="failed"),
        _k8s_sandbox_metadata(status="deleted"),
    ],
    ids=["ready", "pending", "creating", "restoring", "suspended", "failed", "deleted"],
)
def test_classifier_admits_attested_k8s_sandbox_lifecycle(metadata):
    backend, refusal = stateless_workspace_check(_thread(metadata))

    assert backend == "sandbox"
    assert refusal is None


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (_metadata("sandbox"), "workspace_context_missing"),
        ({}, "declared_backend_unsupported"),
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
        ("not-json", "declared_backend_unsupported"),
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
    _backend, refusal = stateless_workspace_check(_thread(metadata))

    assert refusal == reason


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (
            _metadata(
                "sandbox",
                workspace_container={"status": "ready", "provisioner": "docker"},
            ),
            "workspace_not_k8s",
        ),
        (
            _metadata(
                "sandbox",
                workspace_container={"status": "future", "provisioner": "k8s"},
            ),
            "workspace_status_unavailable",
        ),
        (
            _k8s_sandbox_metadata(
                _runtime_incarnation="not-a-uuid",
            ),
            "workspace_runtime_incarnation_malformed",
        ),
        (
            {
                **_k8s_sandbox_metadata(),
                "_workspace_binding": {
                    "generation": SANDBOX_GENERATION,
                    "kind": "remote",
                    "backing_id": "docker:workspace-1",
                    "ssh_host_key_fingerprint": "SHA256:trusted",
                },
            },
            "remote_workspace_binding_malformed",
        ),
        (
            _k8s_sandbox_metadata(
                _canvas_workspace_generation=VIRTUAL_GENERATION,
            ),
            "workspace_generation_mismatch",
        ),
        (
            {
                **_k8s_sandbox_metadata(),
                "workspace_container": {
                    **_k8s_sandbox_metadata()["workspace_container"],
                    "_runtime_incarnation": None,
                },
            },
            "workspace_runtime_incarnation_missing",
        ),
        (
            {
                **_k8s_sandbox_metadata(),
                "workspace_container": {
                    **_k8s_sandbox_metadata()["workspace_container"],
                    "pod_ip": None,
                    "host": None,
                },
            },
            "workspace_endpoint_missing",
        ),
    ],
    ids=[
        "docker",
        "future-status",
        "runtime-malformed",
        "non-k8s-binding",
        "generation-mismatch",
        "runtime-missing",
        "endpoint-missing",
    ],
)
def test_classifier_refuses_unattested_or_non_k8s_sandbox(metadata, reason):
    backend, refusal = stateless_workspace_check(_thread(metadata))

    assert backend == "sandbox"
    assert refusal == reason


def test_session_admission_defaults_to_pinned_while_pool_is_off(monkeypatch):
    from orchestrator import main as orch_main

    monkeypatch.setattr(orch_main, "STATELESS_SESSION_ENABLED", False)

    for backend in ("sandbox", "virtual", "none", "vm"):
        assert (
            orch_main._resolve_thread_execution_lane(
                workspace_backend=backend,
                effective_config={"workspace": {"backend": backend}},
            )
            == "pinned"
        )


@pytest.mark.parametrize("backend", ["sandbox", "virtual", "none"])
def test_enabled_pool_auto_admits_supported_ordinary_tiers(monkeypatch, backend):
    from orchestrator import main as orch_main

    monkeypatch.setattr(orch_main, "STATELESS_SESSION_ENABLED", True)
    monkeypatch.setattr(
        orch_main,
        "container_provisioner",
        SimpleNamespace(is_available=True, in_cluster=True),
    )
    monkeypatch.setattr(
        orch_main,
        "_virtual_workspace_rclone_spec",
        lambda: {"type": "s3", "root": "workspaces"},
    )

    assert (
        orch_main._resolve_thread_execution_lane(
            workspace_backend=backend,
            effective_config={"workspace": {"backend": backend}},
        )
        == "stateless"
    )


def test_enabled_pool_falls_back_to_pinned_when_supported_tier_is_unready(monkeypatch):
    from orchestrator import main as orch_main

    monkeypatch.setattr(orch_main, "STATELESS_SESSION_ENABLED", True)
    monkeypatch.setattr(
        orch_main,
        "container_provisioner",
        SimpleNamespace(is_available=True, in_cluster=False),
    )
    monkeypatch.setattr(
        orch_main, "_virtual_workspace_rclone_spec", lambda: {"type": "memory"}
    )

    assert (
        orch_main._resolve_thread_execution_lane(
            workspace_backend="sandbox",
            effective_config={"workspace": {"backend": "sandbox"}},
        )
        == "pinned"
    )
    assert (
        orch_main._resolve_thread_execution_lane(
            workspace_backend="virtual",
            effective_config={"workspace": {"backend": "virtual"}},
        )
        == "pinned"
    )


def test_public_create_contract_has_no_execution_lane_selector():
    from orchestrator import main as orch_main

    assert "execution_lane" not in orch_main.ThreadCreateRequest.model_fields
    with pytest.raises(ValueError, match="orchestrator-managed"):
        orch_main.ThreadCreateRequest(execution_lane="pinned")


@pytest.mark.parametrize("backend", ["vm", "remote", "future"])
def test_enabled_pool_keeps_unsupported_tiers_pinned_on_omission(monkeypatch, backend):
    from orchestrator import main as orch_main

    monkeypatch.setattr(orch_main, "STATELESS_SESSION_ENABLED", True)

    assert (
        orch_main._resolve_thread_execution_lane(
            workspace_backend=backend,
            effective_config={"workspace": {"backend": backend}},
        )
        == "pinned"
    )


@pytest.mark.parametrize("officer", [{"enabled": True}, {"conference": True}])
def test_pinned_only_session_classes_do_not_auto_admit(monkeypatch, officer):
    from orchestrator import main as orch_main

    monkeypatch.setattr(orch_main, "STATELESS_SESSION_ENABLED", True)
    config = {"workspace": {"backend": "none"}, "officer": officer}

    assert (
        orch_main._resolve_thread_execution_lane(
            workspace_backend="none", effective_config=config
        )
        == "pinned"
    )


def test_materialized_ordinary_class_wins_over_later_expert_and_account_changes():
    from orchestrator import main as orch_main

    materialized = {
        "officer": orch_main._materialized_session_class_override(
            {"officer": {"enabled": False, "conference": False}}
        )
    }
    for mutable_lower_layer in (
        {"officer": {"enabled": True}},  # account default edited later
        {"officer": {"conference": True}},  # selected expert edited later
    ):
        effective = orch_main._deep_merge_dicts(mutable_lower_layer, materialized)
        assert orch_main._stateless_session_class_refusal(effective) is None


@pytest.mark.asyncio
async def test_stateless_config_patch_cannot_enable_pinned_only_session_class(
    monkeypatch,
):
    from orchestrator import main as orch_main

    metadata = _k8s_sandbox_metadata()
    metadata["config_override"]["officer"] = {
        "enabled": False,
        "conference": False,
    }
    thread = {
        **_thread(metadata),
        "user_id": None,
        "project_id": None,
    }
    db = MagicMock()
    db.merge_thread_config_override = AsyncMock(return_value=True)
    monkeypatch.setattr(orch_main, "postgres_db", db)

    with pytest.raises(HTTPException, match="pinned-only") as exc:
        await orch_main._apply_thread_config_update(
            THREAD_ID,
            thread,
            {"officer": {"enabled": True}},
            None,
            request=MagicMock(),
            actor=None,
        )

    assert exc.value.status_code == 409
    db.merge_thread_config_override.assert_not_awaited()


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
