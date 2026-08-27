from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from orchestrator.services.cloud.backend_instance_authority import (
    MAIN_CLOUD_INSTALLATION_PROOF_DOMAIN,
    MainCloudBackendInstanceAuthority,
    main_cloud_installation_proof_sha256,
)
from orchestrator.services.cloud.config import (
    NextcloudSettings,
    load_main_cloud_config_from_instance,
    main_cloud_routing_snapshot,
    main_cloud_secret_references,
)


INSTANCE_ID = "99999999-9999-4999-8999-aaaaaaaaaaaa"
PROOF_SHA = main_cloud_installation_proof_sha256(
    backend_id="nextcloud",
    remote_identity="ocabcdef1234",
)


def _nextcloud_routing(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "version": 1,
        "backend_id": "nextcloud",
        "base_url": "https://cloud.internal.example/nextcloud/",
        "public_url": "https://cloud.example/nextcloud/",
        "admin_user": "admin",
        "agent_user": "agent-service",
        "protected_effect_url": None,
        "protected_effect_config_sha256": None,
    }
    value.update(overrides)
    return value


def _instance(**overrides: object) -> MainCloudBackendInstanceAuthority:
    values: dict[str, object] = {
        "backend_instance_id": INSTANCE_ID,
        "backend_id": "nextcloud",
        "routing": _nextcloud_routing(),
        "installation_proof_sha256": PROOF_SHA,
        "secret_refs": {
            "admin_password": "env:NEXTCLOUD_ADMIN_PASSWORD",
            "agent_password": "env:NEXTCLOUD_AGENT_PASSWORD",
        },
        "secret_revision": 1,
    }
    values.update(overrides)
    return MainCloudBackendInstanceAuthority.capture(**values)  # type: ignore[arg-type]


def test_backend_instance_has_exact_nonsecret_canonical_shape() -> None:
    authority = _instance()

    assert authority.binding == {
        "version": 1,
        "backend_instance_id": INSTANCE_ID,
        "backend_id": "nextcloud",
        "routing": {
            "version": 1,
            "backend_id": "nextcloud",
            "base_url": "https://cloud.internal.example/nextcloud",
            "public_url": "https://cloud.example/nextcloud",
            "admin_user": "admin",
            "agent_user": "agent-service",
            "protected_effect_url": None,
            "protected_effect_config_sha256": None,
        },
        "routing_sha256": authority.routing_sha256,
        "installation_proof_sha256": PROOF_SHA,
        "secret_refs": {
            "admin_password": "env:NEXTCLOUD_ADMIN_PASSWORD",
            "agent_password": "env:NEXTCLOUD_AGENT_PASSWORD",
        },
        "secret_revision": 1,
    }
    assert "password" not in authority.canonical_json.lower().replace(
        "admin_password", ""
    ).replace("agent_password", "")
    assert "ocabcdef1234" not in authority.canonical_json
    assert MainCloudBackendInstanceAuthority.from_binding(authority.binding) is not None


def test_installation_proof_is_provider_domain_separated() -> None:
    assert MAIN_CLOUD_INSTALLATION_PROOF_DOMAIN.endswith(b"\0")
    assert PROOF_SHA != main_cloud_installation_proof_sha256(
        backend_id="opencloud",
        remote_identity="ocabcdef1234",
    )


@pytest.mark.parametrize(
    "instance_id",
    [
        INSTANCE_ID.upper(),
        INSTANCE_ID.replace("-", ""),
        "00000000-0000-0000-0000-000000000000",
        "not-a-uuid",
        "",
        None,
        True,
    ],
)
def test_backend_instance_rejects_noncanonical_or_missing_uuid(
    instance_id: object,
) -> None:
    with pytest.raises(ValueError, match="canonical UUID"):
        _instance(backend_instance_id=instance_id)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(version=True),
        lambda value: value.update(backend_id="opencloud"),
        lambda value: value.update(base_url="https://user:secret@cloud.invalid"),
        lambda value: value.update(base_url="https://cloud.invalid/a/../b"),
        lambda value: value.update(base_url="https://cloud.invalid/?switch=1"),
        lambda value: value.update(public_url="javascript:alert(1)"),
        lambda value: value.update(admin_user=" admin"),
        lambda value: value.update(agent_user="agent\nservice"),
        lambda value: value.update(protected_effect_url="http://effect.invalid"),
        lambda value: value.update(protected_effect_config_sha256="a" * 64),
        lambda value: value.update(extra="value"),
    ],
)
def test_nextcloud_routing_rejects_malformed_or_ambiguous_coordinates(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    routing = _nextcloud_routing()
    mutate(routing)
    with pytest.raises(ValueError):
        _instance(routing=routing)


def test_opencloud_routing_and_secret_shape_are_exact() -> None:
    authority = MainCloudBackendInstanceAuthority.capture(
        backend_instance_id=INSTANCE_ID,
        backend_id="opencloud",
        routing={
            "version": 1,
            "backend_id": "opencloud",
            "base_url": "https://oc.internal.example/",
            "public_url": "https://oc.example/",
            "keycloak_issuer": "https://id.example/realms/srw/",
            "keycloak_client_id": "orchestrator",
            "admin_role_claim_value": "opencloudAdmin",
            "default_quota_bytes": 1024,
            "mount_insecure_tls": False,
        },
        installation_proof_sha256=main_cloud_installation_proof_sha256(
            backend_id="opencloud",
            remote_identity="drive-123",
        ),
        secret_refs={"keycloak_client_secret": "env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET"},
    )

    assert authority.routing["base_url"] == "https://oc.internal.example"
    assert authority.routing["keycloak_issuer"] == "https://id.example/realms/srw"
    assert MainCloudBackendInstanceAuthority.from_binding(authority.binding) is not None


@pytest.mark.parametrize(
    "secret_refs",
    [
        {},
        {"admin_password": "env:ADMIN_ONLY"},
        {
            "admin_password": "NEXTCLOUD_ADMIN_PASSWORD",
            "agent_password": "env:NEXTCLOUD_AGENT_PASSWORD",
        },
        {
            "admin_password": "env:NEXTCLOUD_ADMIN_PASSWORD",
            "agent_password": "env:NEXTCLOUD_AGENT_PASSWORD",
            "plaintext": "secret",
        },
    ],
)
def test_secret_reference_shape_never_accepts_values_or_partial_authority(
    secret_refs: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="secret reference"):
        _instance(secret_refs=secret_refs)


def test_binding_parser_rejects_digest_revision_and_normalization_mutations() -> None:
    authority = _instance()
    mutations: list[Callable[[dict[str, Any]], object]] = [
        lambda value: value.update(version=True),
        lambda value: value.update(routing_sha256="0" * 64),
        lambda value: value.update(installation_proof_sha256="A" * 64),
        lambda value: value.update(secret_revision=1.0),
        lambda value: value["routing"].update(
            base_url="https://cloud.internal.example/nextcloud/"
        ),
        lambda value: value.update(extra=True),
    ]

    for mutate in mutations:
        binding = authority.binding
        mutate(binding)
        assert MainCloudBackendInstanceAuthority.from_binding(binding) is None


def test_authority_is_deeply_immutable_from_caller_owned_mappings() -> None:
    routing = _nextcloud_routing()
    refs = {
        "admin_password": "env:NEXTCLOUD_ADMIN_PASSWORD",
        "agent_password": "env:NEXTCLOUD_AGENT_PASSWORD",
    }
    authority = _instance(routing=routing, secret_refs=refs)
    original = authority.canonical_json

    routing["base_url"] = "https://attacker.invalid"
    refs["admin_password"] = "env:ATTACKER"
    returned_routing = authority.routing
    returned_routing["base_url"] = "https://attacker.invalid"
    returned_refs = authority.secret_refs
    returned_refs["admin_password"] = "env:ATTACKER"

    assert authority.canonical_json == original
    with pytest.raises(AttributeError, match="immutable"):
        authority._backend_id = "opencloud"  # type: ignore[misc]


def test_config_snapshot_and_historical_rebuild_use_only_exact_secret_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = NextcloudSettings(
        base_url="https://cloud.internal.example/nextcloud",
        public_url="https://cloud.example/nextcloud",
        admin_user="admin",
        admin_password="not-persisted-admin-secret",
        agent_user="agent-service",
        agent_password="not-persisted-agent-secret",
    )
    monkeypatch.setenv("NEXTCLOUD_ADMIN_PASSWORD", "historical-admin-secret")
    monkeypatch.setenv("NEXTCLOUD_AGENT_PASSWORD", "historical-agent-secret")
    refs = main_cloud_secret_references("nextcloud")
    authority = MainCloudBackendInstanceAuthority.capture(
        backend_instance_id=INSTANCE_ID,
        backend_id="nextcloud",
        routing=main_cloud_routing_snapshot(settings),
        installation_proof_sha256=PROOF_SHA,
        secret_refs=refs,
    )

    rebuilt = load_main_cloud_config_from_instance(authority)

    assert isinstance(rebuilt, NextcloudSettings)
    assert rebuilt.admin_password.get_secret_value() == "historical-admin-secret"
    assert rebuilt.agent_password.get_secret_value() == "historical-agent-secret"
    assert "not-persisted" not in authority.canonical_json


@pytest.mark.parametrize(
    "overrides",
    [
        {"protected_effect_url": "http://effect.internal"},
        {"protected_effect_config_sha256": "a" * 64},
        {"protected_effect_hmac_key": "k" * 32},
        {
            "protected_effect_url": "http://effect.internal",
            "protected_effect_config_sha256": "a" * 64,
        },
    ],
)
def test_nextcloud_settings_refuse_partial_protected_effect_authority(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="protected-effect.*configured together"):
        NextcloudSettings(
            base_url="https://cloud.internal.example",
            public_url="https://cloud.example",
            admin_user="admin",
            admin_password="admin-secret",
            agent_user="agent-service",
            agent_password="agent-secret",
            **overrides,
        )


def test_nextcloud_settings_require_a_strong_effect_hmac_key() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        NextcloudSettings(
            base_url="https://cloud.internal.example",
            public_url="https://cloud.example",
            admin_user="admin",
            admin_password="admin-secret",
            agent_user="agent-service",
            agent_password="agent-secret",
            protected_effect_url="http://effect.internal",
            protected_effect_config_sha256="a" * 64,
            protected_effect_hmac_key="too-short",
        )


def test_historical_rebuild_never_falls_back_when_a_recorded_ref_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _instance()
    monkeypatch.delenv("NEXTCLOUD_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("MAIN_CLOUD_ADMIN_PASSWORD", "wrong-active-secret")
    monkeypatch.setenv("NEXTCLOUD_AGENT_PASSWORD", "historical-agent-secret")

    with pytest.raises(ValueError, match="admin_password.*unresolved"):
        load_main_cloud_config_from_instance(authority)
