"""Tests for the Phase 4 DB-overlay merging in ``load_main_cloud_config``.

The overlay is what the cockpit admin UI writes to
``system_settings.main_cloud`` — non-secret fields in ``value`` win over
env vars, while secret fields come from env vars named in
``credentials_ref`` or fall back to the legacy per-backend env var.
"""

from __future__ import annotations

import pytest

from orchestrator.services.cloud.config import (
    NextcloudSettings,
    OpenCloudSettings,
    load_main_cloud_config,
)


def _clear_cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for key in list(os.environ):
        if key.startswith(
            ("MAIN_CLOUD_", "NEXTCLOUD_", "OPENCLOUD_", "MS365_", "ROTATED_")
        ):
            monkeypatch.delenv(key, raising=False)


class TestOpenCloudOverlay:
    def test_overlay_value_wins_over_env(self, monkeypatch: pytest.MonkeyPatch):
        _clear_cloud_env(monkeypatch)
        # Env vars say one thing, overlay says another — overlay wins.
        monkeypatch.setenv("OPENCLOUD_URL", "http://env:9200")
        monkeypatch.setenv("OPENCLOUD_PUBLIC_URL", "http://env-public:9200")
        monkeypatch.setenv("OPENCLOUD_KEYCLOAK_ISSUER", "http://env-kc:8080/realms/srw")
        monkeypatch.setenv("OPENCLOUD_KEYCLOAK_CLIENT_SECRET", "env-secret")

        overlay = {
            "value": {
                "backend_id": "opencloud",
                "base_url": "http://overlay:9200",
                "public_url": "http://overlay-public:9200",
                "keycloak_issuer": "http://overlay-kc:8080/realms/srw",
                "keycloak_client_id": "overlay-client",
                "admin_role_claim_value": "overlay-admin",
                "default_quota_bytes": 5368709120,
            },
            "credentials_ref": None,
        }
        settings = load_main_cloud_config(db_overlay=overlay)
        assert isinstance(settings, OpenCloudSettings)
        assert str(settings.base_url) == "http://overlay:9200/"
        assert str(settings.public_url) == "http://overlay-public:9200/"
        assert str(settings.keycloak_issuer) == "http://overlay-kc:8080/realms/srw"
        assert settings.keycloak_client_id == "overlay-client"
        assert settings.admin_role_claim_value == "overlay-admin"
        assert settings.default_quota_bytes == 5368709120
        # With credentials_ref=None, the secret falls through to the
        # legacy env var.
        assert settings.keycloak_client_secret.get_secret_value() == "env-secret"

    def test_credentials_ref_redirects_secret_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _clear_cloud_env(monkeypatch)
        monkeypatch.setenv("OPENCLOUD_URL", "http://overlay:9200")
        monkeypatch.setenv("OPENCLOUD_PUBLIC_URL", "http://overlay:9200")
        monkeypatch.setenv("OPENCLOUD_KEYCLOAK_ISSUER", "http://kc:8080/realms/srw")
        # Legacy env still has the old secret, but credentials_ref points
        # at a new env var — the new one wins for fields listed in
        # __secret_fields__.
        monkeypatch.setenv("OPENCLOUD_KEYCLOAK_CLIENT_SECRET", "old-secret")
        monkeypatch.setenv("ROTATED_SECRET", "new-rotated-secret")

        overlay = {
            "value": {
                "backend_id": "opencloud",
                "__secret_fields__": ["keycloak_client_secret"],
            },
            "credentials_ref": "env:ROTATED_SECRET",
        }
        settings = load_main_cloud_config(db_overlay=overlay)
        assert isinstance(settings, OpenCloudSettings)
        assert (
            settings.keycloak_client_secret.get_secret_value() == "new-rotated-secret"
        )

    def test_partial_overlay_fills_from_env(self, monkeypatch: pytest.MonkeyPatch):
        _clear_cloud_env(monkeypatch)
        monkeypatch.setenv("OPENCLOUD_URL", "http://env:9200")
        monkeypatch.setenv("OPENCLOUD_PUBLIC_URL", "http://env:9200")
        monkeypatch.setenv("OPENCLOUD_KEYCLOAK_ISSUER", "http://kc:8080/realms/srw")
        monkeypatch.setenv("OPENCLOUD_KEYCLOAK_CLIENT_SECRET", "secret")

        # Overlay only sets the client id; everything else still comes
        # from env vars.
        overlay = {
            "value": {
                "backend_id": "opencloud",
                "keycloak_client_id": "custom-from-overlay",
            },
            "credentials_ref": None,
        }
        settings = load_main_cloud_config(db_overlay=overlay)
        assert isinstance(settings, OpenCloudSettings)
        assert settings.keycloak_client_id == "custom-from-overlay"
        assert str(settings.base_url) == "http://env:9200/"

    def test_overlay_switches_backend(self, monkeypatch: pytest.MonkeyPatch):
        _clear_cloud_env(monkeypatch)
        # Env is OpenCloud-flavoured, overlay flips to Nextcloud.
        monkeypatch.setenv("OPENCLOUD_URL", "http://env:9200")
        overlay = {
            "value": {"backend_id": "nextcloud"},
            "credentials_ref": None,
        }
        settings = load_main_cloud_config(db_overlay=overlay)
        assert isinstance(settings, NextcloudSettings)


class TestNextcloudOverlay:
    def test_overlay_value_wins(self, monkeypatch: pytest.MonkeyPatch):
        _clear_cloud_env(monkeypatch)
        monkeypatch.setenv("NEXTCLOUD_URL", "http://env-nc:8800")
        monkeypatch.setenv("NEXTCLOUD_ADMIN_USER", "env-admin")
        monkeypatch.setenv("NEXTCLOUD_ADMIN_PASSWORD", "env-pw")

        overlay = {
            "value": {
                "backend_id": "nextcloud",
                "base_url": "http://overlay-nc:8800",
                "admin_user": "overlay-admin",
            },
            "credentials_ref": None,
        }
        settings = load_main_cloud_config(db_overlay=overlay)
        assert isinstance(settings, NextcloudSettings)
        assert str(settings.base_url) == "http://overlay-nc:8800/"
        assert settings.admin_user == "overlay-admin"
        # admin_password came from env var fallback.
        assert settings.admin_password.get_secret_value() == "env-pw"

    def test_credentials_ref_for_nextcloud_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _clear_cloud_env(monkeypatch)
        monkeypatch.setenv("NEXTCLOUD_URL", "http://env-nc:8800")
        monkeypatch.setenv("NEXTCLOUD_ADMIN_PASSWORD", "legacy-pw")
        monkeypatch.setenv("ROTATED_SECRET", "rotated-pw")

        overlay = {
            "value": {
                "backend_id": "nextcloud",
                "__secret_fields__": ["admin_password"],
            },
            "credentials_ref": "env:ROTATED_SECRET",
        }
        settings = load_main_cloud_config(db_overlay=overlay)
        assert isinstance(settings, NextcloudSettings)
        assert settings.admin_password.get_secret_value() == "rotated-pw"


class TestOverlayNoneBehaviour:
    def test_empty_overlay_is_identical_to_no_overlay(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _clear_cloud_env(monkeypatch)
        monkeypatch.setenv("OPENCLOUD_URL", "http://env:9200")
        monkeypatch.setenv("OPENCLOUD_PUBLIC_URL", "http://env:9200")
        monkeypatch.setenv("OPENCLOUD_KEYCLOAK_ISSUER", "http://kc:8080/realms/srw")
        monkeypatch.setenv("OPENCLOUD_KEYCLOAK_CLIENT_SECRET", "secret")

        a = load_main_cloud_config()
        b = load_main_cloud_config(db_overlay=None)
        c = load_main_cloud_config(db_overlay={"value": {}, "credentials_ref": None})
        assert (
            str(a.base_url) == str(b.base_url) == str(c.base_url) == "http://env:9200/"
        )
