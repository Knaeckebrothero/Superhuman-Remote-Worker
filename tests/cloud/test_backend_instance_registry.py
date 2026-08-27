from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestrator.services.cloud import MainCloudRouter
from orchestrator.services.cloud.backend_instance_authority import (
    MainCloudBackendInstanceAuthority,
    main_cloud_installation_proof_sha256,
)
from orchestrator.services.cloud.instance_registry import (
    activate_main_cloud_config,
    initialize_main_cloud_instance_authority,
    reload_active_main_cloud_instance,
)
from tests.cloud.fake import FakeMainCloudBackend


_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_PROOF_A = main_cloud_installation_proof_sha256(
    backend_id="nextcloud",
    remote_identity="installation-a",
)
_PROOF_B = main_cloud_installation_proof_sha256(
    backend_id="nextcloud",
    remote_identity="installation-b",
)


def _authority(
    instance_id: str = _A,
    *,
    proof: str = _PROOF_A,
    base_url: str = "https://a.internal.example",
    refs: dict[str, str] | None = None,
    secret_revision: int = 1,
) -> MainCloudBackendInstanceAuthority:
    return MainCloudBackendInstanceAuthority.capture(
        backend_instance_id=instance_id,
        backend_id="nextcloud",
        routing={
            "version": 1,
            "backend_id": "nextcloud",
            "base_url": base_url,
            "public_url": base_url.replace("internal.", ""),
            "admin_user": "admin",
            "agent_user": "agent-service",
            "protected_effect_url": None,
            "protected_effect_config_sha256": None,
        },
        installation_proof_sha256=proof,
        secret_refs=refs
        or {
            "admin_password": "env:NEXTCLOUD_ADMIN_PASSWORD",
            "agent_password": "env:NEXTCLOUD_AGENT_PASSWORD",
        },
        secret_revision=secret_revision,
    )


def _backend(authority: MainCloudBackendInstanceAuthority) -> FakeMainCloudBackend:
    backend = FakeMainCloudBackend(start_initialized=True)
    backend.backend_id = authority.backend_id
    backend._installation_proof_sha256 = authority.installation_proof_sha256
    return backend


def _active(
    authority: MainCloudBackendInstanceAuthority,
    revision: int,
) -> dict[str, object]:
    return {"authority": authority, "activation_revision": revision}


@pytest.mark.asyncio
async def test_reload_rereads_pointer_before_process_local_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_a = _authority()
    authority_b = _authority(
        _B,
        proof=_PROOF_B,
        base_url="https://b.internal.example",
    )
    candidate = _backend(authority_a)
    router = MainCloudRouter(_backend(authority_b))
    db = type("DB", (), {})()
    db.get_active_main_cloud_backend_instance = AsyncMock(
        side_effect=[_active(authority_a, 1), _active(authority_b, 2)]
    )
    import orchestrator.services.cloud as cloud_pkg

    monkeypatch.setattr(
        cloud_pkg,
        "build_backend_from_instance",
        lambda authority: candidate,
    )

    assert await reload_active_main_cloud_instance(db, router) is False
    assert router.active is not candidate


@pytest.mark.asyncio
async def test_reload_installs_exact_attested_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority()
    candidate = _backend(authority)
    previous = _backend(_authority(_B))
    router = MainCloudRouter(previous)
    db = type("DB", (), {})()
    db.get_active_main_cloud_backend_instance = AsyncMock(
        return_value=_active(authority, 3)
    )
    import orchestrator.services.cloud as cloud_pkg

    monkeypatch.setattr(
        cloud_pkg,
        "build_backend_from_instance",
        lambda value: candidate,
    )

    assert await reload_active_main_cloud_instance(db, router) is True
    assert router.active is candidate
    assert candidate.backend_instance_id == _A
    assert previous.is_initialized is False


@pytest.mark.asyncio
async def test_first_boot_adopts_candidate_before_installing_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority()
    candidate = _backend(authority)
    previous = _backend(_authority(_B))
    router = MainCloudRouter(previous)
    db = type("DB", (), {})()
    db.get_active_main_cloud_backend_instance = AsyncMock(return_value=None)
    db.install_initial_main_cloud_backend_instance = AsyncMock(
        return_value=_active(authority, 1)
    )
    import orchestrator.services.cloud.instance_registry as registry

    monkeypatch.setattr(
        registry,
        "build_attested_main_cloud_candidate",
        AsyncMock(return_value=(candidate, authority)),
    )

    result = await initialize_main_cloud_instance_authority(
        db,
        router,
        legacy_overlay=None,
    )

    assert result == _active(authority, 1)
    assert router.active is candidate
    assert candidate.backend_instance_id == _A


@pytest.mark.asyncio
async def test_activation_creates_new_instance_under_pointer_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_a = _authority()
    authority_b = _authority(
        _B,
        proof=_PROOF_B,
        base_url="https://b.internal.example",
    )
    candidate = _backend(authority_b)
    previous = _backend(authority_a)
    previous.bind_backend_instance(_A)
    router = MainCloudRouter(previous)
    router._instances[_A] = previous
    router._instance_secret_revisions[_A] = 1
    db = type("DB", (), {})()
    db.get_active_main_cloud_backend_instance = AsyncMock(
        side_effect=[_active(authority_a, 4), _active(authority_b, 5)]
    )
    db.register_main_cloud_backend_instance = AsyncMock(return_value=authority_b)
    db.activate_main_cloud_backend_instance = AsyncMock(
        return_value=_active(authority_b, 5)
    )
    db.rotate_main_cloud_backend_secret_refs = AsyncMock()
    import orchestrator.services.cloud.instance_registry as registry

    monkeypatch.setattr(
        registry,
        "build_attested_main_cloud_candidate",
        AsyncMock(return_value=(candidate, authority_b)),
    )

    result = await activate_main_cloud_config(
        db,
        router,
        db_overlay={"value": {"backend_id": "nextcloud"}},
        expected_activation_revision=4,
        activated_by="admin",
    )

    assert result == _active(authority_b, 5)
    db.activate_main_cloud_backend_instance.assert_awaited_once_with(
        _B,
        expected_activation_revision=4,
        activated_by="admin",
    )
    assert router.active is candidate
    assert previous.is_initialized is True


@pytest.mark.asyncio
async def test_same_installation_rotates_only_secret_reference_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_a = _authority()
    proposed = _authority(
        _B,
        refs={
            "admin_password": "env:ROTATED_ADMIN_PASSWORD",
            "agent_password": "env:ROTATED_AGENT_PASSWORD",
        },
    )
    rotated = _authority(
        _A,
        refs=proposed.secret_refs,
        secret_revision=2,
    )
    candidate = _backend(proposed)
    previous = _backend(authority_a)
    previous.bind_backend_instance(_A)
    router = MainCloudRouter(previous)
    router._instances[_A] = previous
    router._instance_secret_revisions[_A] = 1
    db = type("DB", (), {})()
    db.get_active_main_cloud_backend_instance = AsyncMock(
        side_effect=[_active(authority_a, 7), _active(rotated, 7)]
    )
    db.rotate_main_cloud_backend_secret_refs = AsyncMock(return_value=rotated)
    db.register_main_cloud_backend_instance = AsyncMock()
    db.activate_main_cloud_backend_instance = AsyncMock()
    import orchestrator.services.cloud.instance_registry as registry

    monkeypatch.setattr(
        registry,
        "build_attested_main_cloud_candidate",
        AsyncMock(return_value=(candidate, proposed)),
    )

    result = await activate_main_cloud_config(
        db,
        router,
        db_overlay={"value": {"backend_id": "nextcloud"}},
        expected_activation_revision=7,
        activated_by="admin",
    )

    assert result == _active(rotated, 7)
    db.rotate_main_cloud_backend_secret_refs.assert_awaited_once()
    db.register_main_cloud_backend_instance.assert_not_awaited()
    db.activate_main_cloud_backend_instance.assert_not_awaited()
    assert router.active is candidate
    assert candidate.backend_instance_id == _A


@pytest.mark.asyncio
async def test_stale_activation_revision_performs_no_probe_or_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority()
    db = type("DB", (), {})()
    db.get_active_main_cloud_backend_instance = AsyncMock(
        return_value=_active(authority, 9)
    )
    builder = AsyncMock()
    import orchestrator.services.cloud.instance_registry as registry

    monkeypatch.setattr(registry, "build_attested_main_cloud_candidate", builder)

    result = await activate_main_cloud_config(
        db,
        MainCloudRouter(_backend(authority)),
        db_overlay=None,
        expected_activation_revision=8,
        activated_by="admin",
    )

    assert result is None
    builder.assert_not_awaited()
