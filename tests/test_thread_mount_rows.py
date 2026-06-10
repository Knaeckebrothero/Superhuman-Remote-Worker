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


# ---------------------------------------------------------------------------
# Phase 4 — session-folder skip predicate
# ---------------------------------------------------------------------------


def test_should_skip_session_folder_with_project_default_mount():
    """A ``project_default`` row with webdav_url means the user's cloud
    home is mounted at workspace root — session folder is redundant.
    (Phase 2 behavior, preserved by Phase 4.)
    """
    from main import _should_skip_session_folder

    rows = [
        {
            "mount_kind": "project_default",
            "target_path": "",
            "webdav_url": "https://oc.test/dav/spaces/drive-xyz/",
        }
    ]
    assert _should_skip_session_folder(rows) is True


def test_rclone_driver_keeps_session_folder_fallback(monkeypatch):
    """With the lazy mount driver enabled, keep the regular session folder
    provisioned so unsupported user-home auth has a safe fallback.
    """
    from main import _should_skip_session_folder

    monkeypatch.setenv("CLOUD_WORKSPACE_DRIVER", "rclone_mount")
    rows = [
        {
            "mount_kind": "project_default",
            "target_path": "",
            "webdav_url": "https://nc.test/remote.php/dav/files/alice/",
        }
    ]
    assert _should_skip_session_folder(rows) is False


def test_should_skip_session_folder_with_non_default_project_mount():
    """Phase 4: a regular ``project`` mount (non-default, under
    ``projects/<name>/``) also counts as a user-visible cloud surface
    — session folder is redundant for it too. This is the new Phase 4
    behavior; pre-Phase-4 this returned False and the session folder
    was unnecessarily provisioned alongside.
    """
    from main import _should_skip_session_folder

    rows = [
        {
            "mount_kind": "project",
            "target_path": "projects/alpha",
            "webdav_url": "https://oc.test/dav/spaces/drive-abc/",
        }
    ]
    assert _should_skip_session_folder(rows) is True


def test_should_skip_session_folder_with_repo_mount():
    """Forward-compatibility: a ``repo`` row with a transport URL
    is also a user-visible surface (Phase 3b territory). The predicate
    is mount-kind-agnostic — any mount with a working webdav_url
    short-circuits the session folder.
    """
    from main import _should_skip_session_folder

    rows = [
        {
            "mount_kind": "repo",
            "target_path": "repos/alpha",
            "webdav_url": "https://gitea.test/some/repo",
        }
    ]
    assert _should_skip_session_folder(rows) is True


def test_should_skip_session_folder_empty_mounts():
    """No mounts → fall back to legacy session folder so the thread
    isn't left with zero cloud surfaces. This is the fallback case
    Phase 4 deliberately preserves (unattached sessions still get a
    folder).
    """
    from main import _should_skip_session_folder

    assert _should_skip_session_folder([]) is False


def test_should_skip_session_folder_mount_without_webdav_url():
    """A mount row exists but its ``webdav_url`` is None — e.g.
    backend wasn't initialized when the row was built, or
    user-home resolution failed transiently. The row is not a usable
    sync target, so don't skip the fallback. (Same observable-state
    safety net Phase 2 introduced.)
    """
    from main import _should_skip_session_folder

    rows = [
        {
            "mount_kind": "project_default",
            "target_path": "",
            "webdav_url": None,
        },
        {
            "mount_kind": "project",
            "target_path": "projects/alpha",
            "webdav_url": "",  # empty string is also falsy
        },
    ]
    assert _should_skip_session_folder(rows) is False


def test_should_skip_session_folder_returns_true_on_first_usable_mount():
    """At least one usable row is enough — predicate is short-circuit
    OR semantics. Verifies a mix of failed + working rows still
    skips the session folder.
    """
    from main import _should_skip_session_folder

    rows = [
        {"mount_kind": "project", "target_path": "projects/a", "webdav_url": None},
        {
            "mount_kind": "project",
            "target_path": "projects/b",
            "webdav_url": "https://oc.test/dav/spaces/drive-b/",
        },
    ]
    assert _should_skip_session_folder(rows) is True


@pytest.mark.asyncio
async def test_build_agent_cloud_mount_falls_back_to_session_folder(monkeypatch):
    """If a default user-home row lacks safe rclone credentials, the rclone
    payload uses the regular session folder instead of the eager home clone.
    """
    from main import _build_agent_cloud_mount
    from services.cloud import (
        CloudBackendError,
        CloudBackendErrorKind,
        RcloneMountSpec,
    )

    class Backend:
        backend_id = "nextcloud"
        is_initialized = True

        async def build_rclone_mount_spec(self, *, mount_kind, **kwargs):
            if mount_kind != "session_folder":
                raise CloudBackendError(
                    CloudBackendErrorKind.NOT_SUPPORTED,
                    "missing explicit user-home credentials",
                    backend=self.backend_id,
                )
            return RcloneMountSpec(
                source_type="webdav",
                source_config={
                    "url": "https://nc.test/remote.php/dav/files/agent/session/",
                    "vendor": "nextcloud",
                    "user": "agent-service",
                },
                auth={"type": "basic", "password": "agent-pass"},
            )

    monkeypatch.setenv("CLOUD_WORKSPACE_DRIVER", "rclone_mount")
    router = MagicMock()
    router.for_backend.return_value = Backend()
    thread = {
        "id": "thread-1",
        "main_cloud_backend": "nextcloud",
        "main_cloud_session_handle": "sessions/thread-1",
    }
    rows = [
        {
            "id": "mount-home",
            "mount_kind": "project_default",
            "target_path": "",
            "source_ref": "p-default",
            "backend_id": "nextcloud",
            "cloud_handle": (
                '{"backend":"nextcloud","native_id":"home:alice",'
                '"vendor_meta":{"kind":"user_home","username":"alice"}}'
            ),
        }
    ]

    with patch("main.main_cloud_router", router):
        payload = await _build_agent_cloud_mount(
            thread,
            mount_rows=rows,
            metadata={"vm": {"status": "ready", "ssh_host": "10.0.0.5"}},
        )

    assert payload is not None
    assert payload["driver"] == "rclone"
    assert payload["fallback"] is True
    assert len(payload["mounts"]) == 1
    mount = payload["mounts"][0]
    assert mount["mount_kind"] == "session_folder"
    assert mount["target_path"] == "/cloud/home"
    assert mount["workspace_name"] == "home"


@pytest.mark.asyncio
async def test_build_agent_cloud_mount_uses_supported_thread_mount(monkeypatch):
    from main import _build_agent_cloud_mount
    from services.cloud import RcloneMountSpec

    class Backend:
        backend_id = "nextcloud"
        is_initialized = True

        async def build_rclone_mount_spec(self, *, mount_kind, target_path, **kwargs):
            return RcloneMountSpec(
                source_type="webdav",
                source_config={
                    "url": "https://nc.test/remote.php/dav/files/alice/",
                    "vendor": "nextcloud",
                    "user": "alice",
                },
                auth={"type": "basic", "password": "app-pass"},
            )

    monkeypatch.setenv("CLOUD_WORKSPACE_DRIVER", "rclone_mount")
    router = MagicMock()
    router.for_backend.return_value = Backend()
    rows = [
        {
            "id": "mount-home",
            "mount_kind": "project_default",
            "target_path": "",
            "source_ref": "p-default",
            "backend_id": "nextcloud",
            "cloud_handle": (
                '{"backend":"nextcloud","native_id":"home:alice",'
                '"vendor_meta":{"kind":"user_home","username":"alice"}}'
            ),
        }
    ]

    with patch("main.main_cloud_router", router):
        payload = await _build_agent_cloud_mount(
            {"id": "thread-1"},
            mount_rows=rows,
            metadata={"vm": {"status": "ready", "ssh_host": "10.0.0.5"}},
        )

    assert payload is not None
    assert payload["fallback"] is False
    assert payload["mounts"][0]["mount_kind"] == "project_default"
    assert payload["mounts"][0]["target_path"] == "/cloud/home"


@pytest.mark.asyncio
async def test_build_agent_cloud_mount_uses_container_runtime_by_default(monkeypatch):
    from main import _build_agent_cloud_mount
    from services.cloud import RcloneMountSpec

    class Backend:
        backend_id = "nextcloud"
        is_initialized = True

        async def build_rclone_mount_spec(self, *, mount_kind, target_path, **kwargs):
            assert mount_kind == "session_folder"
            assert target_path == "/cloud/home"
            return RcloneMountSpec(
                source_type="webdav",
                source_config={
                    "url": "https://nc.test/remote.php/dav/files/agent/session/",
                    "vendor": "nextcloud",
                    "user": "agent-service",
                },
                auth={"type": "basic", "password": "agent-pass"},
            )

    monkeypatch.setenv("CLOUD_WORKSPACE_DRIVER", "rclone_mount")
    monkeypatch.delenv("CLOUD_RCLONE_ALLOW_CONTAINER", raising=False)
    router = MagicMock()
    router.for_backend.return_value = Backend()
    thread = {
        "id": "thread-1",
        "main_cloud_backend": "nextcloud",
        "main_cloud_session_handle": "sessions/thread-1",
    }

    with patch("main.main_cloud_router", router):
        payload = await _build_agent_cloud_mount(
            thread,
            mount_rows=[],
            metadata={
                "workspace_container": {
                    "status": "ready",
                    "pod_ip": "10.42.0.10",
                }
            },
        )

    assert payload is not None
    assert payload["driver"] == "rclone"
    assert payload["fallback"] is False
    assert payload["mounts"][0]["mount_kind"] == "session_folder"


@pytest.mark.asyncio
async def test_build_agent_cloud_mount_container_runtime_can_be_disabled(monkeypatch):
    from main import _build_agent_cloud_mount

    monkeypatch.setenv("CLOUD_WORKSPACE_DRIVER", "rclone_mount")
    monkeypatch.setenv("CLOUD_RCLONE_ALLOW_CONTAINER", "false")
    payload = await _build_agent_cloud_mount(
        {
            "id": "thread-1",
            "main_cloud_backend": "nextcloud",
            "main_cloud_session_handle": "sessions/thread-1",
        },
        mount_rows=[],
        metadata={
            "workspace_container": {
                "status": "ready",
                "pod_ip": "10.42.0.10",
            }
        },
    )

    assert payload is None


# ---------------------------------------------------------------------------
# Phase 3a — multi-project mount path collision handling
# ---------------------------------------------------------------------------


def _multi_project_db(projects: list[dict]) -> MagicMock:
    """Build a fake postgres_db that resolves each project_id to a row."""
    fake_db = MagicMock()
    table = {p["id"]: p for p in projects}

    async def get_project(pid: str):
        return table.get(pid)

    fake_db.get_project = AsyncMock(side_effect=get_project)
    fake_db.get_project_members = AsyncMock(return_value=[_owner_member()])
    fake_db.get_user = AsyncMock(return_value=_owner_user_record())
    return fake_db


def _router_for_backend(backend: MagicMock) -> MagicMock:
    router = MagicMock()
    router.for_project.return_value = backend
    router.for_backend.return_value = backend
    return router


@pytest.mark.asyncio
async def test_collision_two_same_named_projects_get_distinct_paths():
    """Two non-default projects with the same name → first wins ``projects/alpha``,
    second gets ``projects/alpha-2``. UNIQUE (thread_id, target_path) at
    persistence time always holds.
    """
    from main import _build_thread_mount_rows

    fake_db = _multi_project_db(
        [
            _project(project_id="p-1", name="Alpha"),
            _project(project_id="p-2", name="Alpha"),
        ]
    )
    backend = _backend()

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", _router_for_backend(backend)),
    ):
        rows = await _build_thread_mount_rows(["p-1", "p-2"])

    assert len(rows) == 2
    assert [r["target_path"] for r in rows] == [
        "projects/alpha",
        "projects/alpha-2",
    ]
    assert [r["source_ref"] for r in rows] == ["p-1", "p-2"]


@pytest.mark.asyncio
async def test_collision_case_insensitive():
    """``_slugify_mount_name`` lowercases, so "Alpha" and "alpha" produce
    the same slug. Collision logic must still dedup the second one.
    """
    from main import _build_thread_mount_rows

    fake_db = _multi_project_db(
        [
            _project(project_id="p-1", name="Alpha"),
            _project(project_id="p-2", name="alpha"),
        ]
    )
    backend = _backend()

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", _router_for_backend(backend)),
    ):
        rows = await _build_thread_mount_rows(["p-1", "p-2"])

    assert [r["target_path"] for r in rows] == [
        "projects/alpha",
        "projects/alpha-2",
    ]


@pytest.mark.asyncio
async def test_collision_three_same_named_projects():
    """Three same-named → ``alpha``, ``alpha-2``, ``alpha-3``. The suffix
    counter walks forward and doesn't reuse freed-up indices (none get
    freed in this scenario anyway).
    """
    from main import _build_thread_mount_rows

    fake_db = _multi_project_db(
        [
            _project(project_id="p-1", name="Alpha"),
            _project(project_id="p-2", name="Alpha"),
            _project(project_id="p-3", name="Alpha"),
        ]
    )
    backend = _backend()

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", _router_for_backend(backend)),
    ):
        rows = await _build_thread_mount_rows(["p-1", "p-2", "p-3"])

    assert [r["target_path"] for r in rows] == [
        "projects/alpha",
        "projects/alpha-2",
        "projects/alpha-3",
    ]


@pytest.mark.asyncio
async def test_no_collision_unique_names_unaffected():
    """Sanity check: unique names don't acquire suffixes (regression guard
    in case the suffix loop is ever rewritten with an off-by-one).
    """
    from main import _build_thread_mount_rows

    fake_db = _multi_project_db(
        [
            _project(project_id="p-1", name="Alpha"),
            _project(project_id="p-2", name="Beta"),
            _project(project_id="p-3", name="Gamma"),
        ]
    )
    backend = _backend()

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", _router_for_backend(backend)),
    ):
        rows = await _build_thread_mount_rows(["p-1", "p-2", "p-3"])

    assert [r["target_path"] for r in rows] == [
        "projects/alpha",
        "projects/beta",
        "projects/gamma",
    ]


@pytest.mark.asyncio
async def test_dedupe_repeated_project_id():
    """Same UUID twice in project_ids → one row, not two — and definitely
    not a row at ``projects/alpha`` plus a phantom ``projects/alpha-2``
    pointing at the same source_ref.
    """
    from main import _build_thread_mount_rows

    fake_db = _multi_project_db(
        [
            _project(project_id="p-1", name="Alpha"),
        ]
    )
    backend = _backend()

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", _router_for_backend(backend)),
    ):
        rows = await _build_thread_mount_rows(["p-1", "p-1", "p-1"])

    assert len(rows) == 1
    assert rows[0]["target_path"] == "projects/alpha"
    assert rows[0]["source_ref"] == "p-1"


@pytest.mark.asyncio
async def test_collision_with_default_project_present():
    """Default project at workspace root + two collision-named non-default
    projects. The default's empty ``target_path`` doesn't share the
    namespace with non-defaults (which live under ``projects/``), so the
    suffix logic only fires between the non-defaults.
    """
    from main import _build_thread_mount_rows

    fake_db = _multi_project_db(
        [
            _project(project_id="p-default", is_default=True, name="My Home"),
            _project(project_id="p-1", name="Alpha"),
            _project(project_id="p-2", name="Alpha"),
        ]
    )
    backend = _backend()

    with (
        patch("main.postgres_db", fake_db),
        patch("main.main_cloud_router", _router_for_backend(backend)),
    ):
        rows = await _build_thread_mount_rows(["p-default", "p-1", "p-2"])

    assert len(rows) == 3
    paths = [r["target_path"] for r in rows]
    assert "" in paths  # default at root
    assert "projects/alpha" in paths
    assert "projects/alpha-2" in paths
    # source_refs preserved in input order
    assert [r["source_ref"] for r in rows] == ["p-default", "p-1", "p-2"]
