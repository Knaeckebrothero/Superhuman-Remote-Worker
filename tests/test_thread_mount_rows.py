"""Unit tests for the thread-mount-row builders in ``orchestrator/main.py``.

Phase 2 of ``docs/features/cloud_collaboration_model.md`` §9 introduces the
``project_default`` row shape — default projects mount the owner's cloud
home at the workspace root rather than under ``projects/<slug>/``. These
tests cover the builder helpers (``_build_thread_mount_rows`` and
``_build_default_project_mount_row``) directly so the wiring is exercised
without standing up the full thread-create path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _project(
    *, project_id: str, is_default: bool = False, name: str = "Project"
) -> dict:
    """Shape a project row the way ``get_project`` returns it."""
    return {
        "id": project_id,
        "name": name,
        "is_default": is_default,
        "main_cloud_backend": "opencloud",
        "main_cloud_folder_handle": (
            "opencloud:drive-abc:" if not is_default else None
        ),
    }


def _user_home(webdav_url: str = "https://oc.test/dav/spaces/drive-xyz/"):
    home = MagicMock()
    home.webdav_url = webdav_url
    handle = MagicMock()
    handle.to_db.return_value = "opencloud:drive-xyz:user_home"
    home.handle = handle
    return home


def _backend(*, initialized: bool = True, backend_id: str = "opencloud"):
    backend = MagicMock()
    backend.is_initialized = initialized
    backend.backend_id = backend_id
    backend.resolve_user_identity = AsyncMock(return_value="user-xyz")
    backend.get_user_home = AsyncMock(return_value=_user_home())
    backend.get_project_folder_webdav_url = MagicMock(
        return_value="https://oc.test/dav/spaces/drive-abc/"
    )
    return backend


def _owner_member(
    *,
    user_id: str = "owner-uuid",
    email: str = "alice@example.com",
    display_name: str = "Alice",
) -> dict:
    return {
        "role": "owner",
        "user_id": user_id,
        "email": email,
        "display_name": display_name,
    }


def _owner_user_record(*, keycloak_sub: str = "alice-keycloak-sub") -> dict:
    return {"id": "owner-uuid", "keycloak_sub": keycloak_sub}


@pytest.mark.asyncio
async def test_default_project_emits_user_home_row():
    """Default project → ``project_default`` row with target_path='' and
    the owner's home Space's webdav URL. ``target_user_sub`` carries the
    Keycloak ``sub`` of the owner so the agent can do RFC 8693 exchange.
    """
    from main import _build_default_project_mount_row

    project = _project(project_id="p-default", is_default=True, name="Default")
    backend = _backend()

    fake_db = MagicMock()
    fake_db.get_project_members = AsyncMock(return_value=[_owner_member()])
    fake_db.get_user = AsyncMock(return_value=_owner_user_record())
    router = MagicMock()
    router.for_project.return_value = backend

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", router),
    ):
        row = await _build_default_project_mount_row("p-default", project)

    assert row is not None
    assert row["mount_kind"] == "project_default"
    assert row["target_path"] == ""
    assert row["source_kind"] == "user_home"
    assert row["source_ref"] == "p-default"
    assert row["backend_id"] == "opencloud"
    assert row["webdav_url"] == "https://oc.test/dav/spaces/drive-xyz/"
    assert row["cloud_handle"] == "opencloud:drive-xyz:user_home"
    assert row["target_user_sub"] == "alice-keycloak-sub"
    backend.resolve_user_identity.assert_awaited_once_with("alice@example.com", "alice")
    backend.get_user_home.assert_awaited_once_with("user-xyz")


@pytest.mark.asyncio
async def test_default_project_no_owner_returns_none():
    """Owner missing from the project → fall back to legacy session folder."""
    from main import _build_default_project_mount_row

    project = _project(project_id="p", is_default=True)
    fake_db = MagicMock()
    fake_db.get_project_members = AsyncMock(return_value=[])
    fake_db.get_user = AsyncMock(return_value=None)
    router = MagicMock()
    router.for_project.return_value = _backend()

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", router),
    ):
        row = await _build_default_project_mount_row("p", project)
    assert row is None


@pytest.mark.asyncio
async def test_default_project_owner_missing_keycloak_sub_returns_none():
    """Owner exists but has never SSO'd, so we don't have their Keycloak
    sub yet → can't do token-exchange → no row. Caller falls back to
    legacy session folder so the thread still has SOMETHING.
    """
    from main import _build_default_project_mount_row

    project = _project(project_id="p", is_default=True)
    fake_db = MagicMock()
    fake_db.get_project_members = AsyncMock(return_value=[_owner_member()])
    fake_db.get_user = AsyncMock(
        return_value={"id": "owner-uuid", "keycloak_sub": None}
    )
    router = MagicMock()
    router.for_project.return_value = _backend()

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", router),
    ):
        row = await _build_default_project_mount_row("p", project)
    assert row is None


@pytest.mark.asyncio
async def test_default_project_user_home_unresolvable_returns_none():
    """Owner exists on the backend but ``get_user_home`` returns None
    (e.g. drive not yet provisioned) → fall back, no row.
    """
    from main import _build_default_project_mount_row

    project = _project(project_id="p", is_default=True)
    backend = _backend()
    backend.get_user_home = AsyncMock(return_value=None)

    fake_db = MagicMock()
    fake_db.get_project_members = AsyncMock(
        return_value=[_owner_member(email="bob@example.com", display_name="Bob")]
    )
    fake_db.get_user = AsyncMock(return_value=_owner_user_record())
    router = MagicMock()
    router.for_project.return_value = backend

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", router),
    ):
        row = await _build_default_project_mount_row("p", project)
    assert row is None


@pytest.mark.asyncio
async def test_default_project_backend_uninitialized_returns_none():
    from main import _build_default_project_mount_row

    project = _project(project_id="p", is_default=True)
    backend = _backend(initialized=False)
    fake_db = MagicMock()
    fake_db.get_project_members = AsyncMock(return_value=[])
    fake_db.get_user = AsyncMock(return_value=None)
    router = MagicMock()
    router.for_project.return_value = backend

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", router),
    ):
        row = await _build_default_project_mount_row("p", project)
    assert row is None


@pytest.mark.asyncio
async def test_build_thread_mount_rows_mixes_default_and_non_default():
    """One default project + one non-default project → two rows of the
    right shapes. The default-project row points at workspace root; the
    non-default row lives under ``projects/<slug>/``.
    """
    from main import _build_thread_mount_rows

    default_project = _project(project_id="p-default", is_default=True, name="My Home")
    other_project = _project(project_id="p-other", is_default=False, name="Alpha")
    fake_db = MagicMock()

    async def get_project(pid: str):
        return {"p-default": default_project, "p-other": other_project}.get(pid)

    fake_db.get_project = AsyncMock(side_effect=get_project)
    fake_db.get_project_members = AsyncMock(
        return_value=[_owner_member(email="carol@example.com", display_name="Carol")]
    )
    fake_db.get_user = AsyncMock(
        return_value=_owner_user_record(keycloak_sub="carol-sub")
    )
    backend = _backend()
    router = MagicMock()
    router.for_project.return_value = backend
    router.for_backend.return_value = backend

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", router),
    ):
        rows = await _build_thread_mount_rows(["p-default", "p-other"])

    assert len(rows) == 2
    by_kind = {r["mount_kind"]: r for r in rows}
    assert by_kind["project_default"]["target_path"] == ""
    assert by_kind["project_default"]["source_ref"] == "p-default"
    assert by_kind["project"]["target_path"] == "projects/alpha"
    assert by_kind["project"]["source_ref"] == "p-other"


@pytest.mark.asyncio
async def test_project_ids_from_mounts_includes_project_default():
    """Phase 2: a ``project_default`` row counts as a project attachment
    for downstream datasource/visibility resolution.
    """
    from main import _project_ids_from_mounts

    rows = [
        {"mount_kind": "project_default", "source_ref": "p-default"},
        {"mount_kind": "project", "source_ref": "p-alpha"},
        {"mount_kind": "repo", "source_ref": "r-1"},
    ]
    assert _project_ids_from_mounts(rows) == ["p-default", "p-alpha"]
