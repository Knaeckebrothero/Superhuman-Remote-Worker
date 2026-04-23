"""Tests for the cloud_sync factory."""

from __future__ import annotations

import logging
from pathlib import Path

from src.services.cloud_sync import (
    NextcloudWorkspaceSync,
    OpenCloudWorkspaceSync,
    build_workspace_sync,
)


def test_factory_builds_nextcloud(tmp_path: Path):
    cfg = {
        "backend": "nextcloud",
        "webdav_url": "http://nc/remote.php/dav/files/agent/sess/",
        "auth": {"type": "basic", "username": "agent", "password": "p"},
    }
    sync = build_workspace_sync(workspace_path=tmp_path, cloud_cfg=cfg)
    assert isinstance(sync, NextcloudWorkspaceSync)


def test_factory_builds_opencloud(tmp_path: Path):
    cfg = {
        "backend": "opencloud",
        "webdav_url": "http://oc/dav/spaces/abc/sessions/xyz/",
        "auth": {
            "type": "keycloak_client_credentials",
            "issuer": "http://kc/realms/srw",
            "client_id": "srw-orch",
            "client_secret": "shh",
        },
    }
    sync = build_workspace_sync(workspace_path=tmp_path, cloud_cfg=cfg)
    assert isinstance(sync, OpenCloudWorkspaceSync)


def test_factory_returns_none_on_empty(tmp_path: Path):
    assert build_workspace_sync(workspace_path=tmp_path, cloud_cfg=None) is None
    assert build_workspace_sync(workspace_path=tmp_path, cloud_cfg={}) is None


def test_factory_returns_none_on_mismatch(tmp_path: Path, caplog):
    caplog.set_level(logging.WARNING, logger="src.services.cloud_sync")
    cfg = {
        "backend": "opencloud",
        "webdav_url": "http://oc/x/",
        "auth": {"type": "basic", "username": "a", "password": "b"},
    }
    out = build_workspace_sync(workspace_path=tmp_path, cloud_cfg=cfg)
    assert out is None
    assert "unsupported backend/auth combo" in caplog.text
    # Should not log the secret
    assert "shh" not in caplog.text


def test_factory_returns_none_missing_fields(tmp_path: Path, caplog):
    caplog.set_level(logging.WARNING, logger="src.services.cloud_sync")
    assert (
        build_workspace_sync(
            workspace_path=tmp_path,
            cloud_cfg={"backend": "nextcloud"},
        )
        is None
    )
