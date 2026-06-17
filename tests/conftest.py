"""Pytest configuration for tests."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

# Add project root AND orchestrator/ to path. orchestrator/ is on sys.path
# at runtime (uvicorn --app-dir orchestrator + PYTHONPATH=orchestrator:.)
# so modules under it use sibling imports (``from security.crypto import``
# rather than ``from orchestrator.security.crypto``). Tests that import
# ``orchestrator.database.postgres`` would otherwise fail to resolve the
# transitive ``from security.crypto import`` inside it.
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "orchestrator"))

# orchestrator/main.py SystemExits at import time unless the license gate is
# accepted. Tests only exercise its utility functions — auto-accept here so
# CI and local runs don't need extra setup.
os.environ.setdefault("LICENSE_TERMS_ACCEPTED", "true")

# Set WORKSPACE_PATH to a temp directory for tests so that workspace files
# (logs, checkpoints, uploads) don't get created inside the repository.
if "WORKSPACE_PATH" not in os.environ:
    _test_workspace = tempfile.mkdtemp(prefix="srw_test_workspace_")
    os.environ["WORKSPACE_PATH"] = _test_workspace

# Provide a deterministic encryption key so any code path that touches
# orchestrator.security.crypto during tests has a working cipher. Real
# credentials never run through this key.
os.environ.setdefault(
    "APP_ENCRYPTION_KEY", "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="
)

# orchestrator/main.py also guards on vector-DB credentials at import time.
# Tests only exercise its utility functions / pure models, never the vector
# store, so provide a dummy URL. setdefault never overrides a real CI/prod value.
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test:test@localhost:5432/test")


# =============================================================================
# F1 multi-tenancy fixture — three users, two projects, jobs/threads/sessions
# =============================================================================
#
# This is the canonical fixture for any test that needs to exercise the
# visibility model in orchestrator/security/access.py. It is mock-only — no
# real Postgres — so tests stay fast. The shapes mirror what the real DB
# layer returns; if you find yourself reaching for a field that isn't here,
# extend the fixture rather than re-inventing it per-file.
#
# Layout:
#   user_a  ──owner──▶ project_a ──contains──▶ job_a
#                                    │
#                                    └─thread_a   builder_session_a
#   user_b  ──owner──▶ project_b ──contains──▶ job_b
#                                    │
#                                    └─thread_b   builder_session_b
#   user_admin (is_admin=True, no project membership; admin role bypasses)
#
# user_a has no membership in project_b (and vice versa). user_admin has no
# membership rows but should pass every gate via the is_admin bypass.


_UID_A = UUID("11111111-1111-1111-1111-111111111111")
_UID_B = UUID("22222222-2222-2222-2222-222222222222")
_UID_ADMIN = UUID("33333333-3333-3333-3333-333333333333")
_PID_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PID_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_JID_A = UUID("a1111111-1111-1111-1111-111111111111")
_JID_B = UUID("b2222222-2222-2222-2222-222222222222")
_TID_A = UUID("a3333333-3333-3333-3333-333333333333")
_TID_B = UUID("b4444444-4444-4444-4444-444444444444")
_SID_A = UUID("a5555555-5555-5555-5555-555555555555")
_SID_B = UUID("b6666666-6666-6666-6666-666666666666")
_DSID_A = UUID("a7777777-7777-7777-7777-777777777777")
_DSID_B = UUID("b8888888-8888-8888-8888-888888888888")
_DSID_GLOBAL = UUID("c9999999-9999-9999-9999-999999999999")


def _make_user(uid: UUID, name: str, is_admin: bool = False) -> dict:
    # ``real_is_admin`` mirrors the contract of
    # ``security.auth.require_approved_user`` — it always sets the flag so
    # downstream gates (``_require_admin``) can read it. Tests use the
    # already-resolved user dict, so the flag matches ``is_admin`` here.
    # The "view as user" shadow (X-Admin-View-As: user) is tested in
    # tests/test_view_as_user.py against the auth resolver itself.
    return {
        "id": uid,
        "display_name": name,
        "email": f"{name}@example.test",
        "is_admin": is_admin,
        "real_is_admin": is_admin,
        "is_approved": True,
        "auth_method": "cookie",
        "scopes": [],
    }


@pytest.fixture
def user_a() -> dict:
    return _make_user(_UID_A, "user_a")


@pytest.fixture
def user_b() -> dict:
    return _make_user(_UID_B, "user_b")


@pytest.fixture
def user_admin() -> dict:
    return _make_user(_UID_ADMIN, "admin", is_admin=True)


@pytest.fixture
def project_a() -> dict:
    return {"id": _PID_A, "name": "Project A", "user_id": _UID_A}


@pytest.fixture
def project_b() -> dict:
    return {"id": _PID_B, "name": "Project B", "user_id": _UID_B}


@pytest.fixture
def job_a() -> dict:
    return {"id": _JID_A, "user_id": _UID_A, "project_id": _PID_A, "status": "created"}


@pytest.fixture
def job_b() -> dict:
    return {"id": _JID_B, "user_id": _UID_B, "project_id": _PID_B, "status": "created"}


@pytest.fixture
def thread_a() -> dict:
    return {"id": _TID_A, "user_id": _UID_A, "title": "thread A"}


@pytest.fixture
def thread_b() -> dict:
    return {"id": _TID_B, "user_id": _UID_B, "title": "thread B"}


@pytest.fixture
def builder_session_a() -> dict:
    return {"id": _SID_A, "user_id": _UID_A, "expert_id": None}


@pytest.fixture
def builder_session_b() -> dict:
    return {"id": _SID_B, "user_id": _UID_B, "expert_id": None}


@pytest.fixture
def datasource_a() -> dict:
    """Created by user_a, linked to project_a."""
    return {
        "id": _DSID_A,
        "name": "ds_a",
        "type": "postgresql",
        "created_by": _UID_A,
        "credentials": {"username": "pg_a", "password": "secret_a"},
        "connection_url": "postgresql://host/db_a",
        "is_global": False,
        "job_id": None,
    }


@pytest.fixture
def datasource_b() -> dict:
    """Created by user_b, linked to project_b."""
    return {
        "id": _DSID_B,
        "name": "ds_b",
        "type": "postgresql",
        "created_by": _UID_B,
        "credentials": {"username": "pg_b", "password": "secret_b"},
        "connection_url": "postgresql://host/db_b",
        "is_global": False,
        "job_id": None,
    }


@pytest.fixture
def datasource_global() -> dict:
    """Created by admin, no project link — admin-only by visibility."""
    return {
        "id": _DSID_GLOBAL,
        "name": "ds_global",
        "type": "postgresql",
        "created_by": _UID_ADMIN,
        "credentials": {"username": "pg_root", "password": "rootsecret"},
        "connection_url": "postgresql://host/db_global",
        "is_global": True,
        "job_id": None,
    }


@pytest.fixture
def fake_db(
    project_a,
    project_b,
    job_a,
    job_b,
    thread_a,
    thread_b,
    builder_session_a,
    builder_session_b,
    datasource_a,
    datasource_b,
    datasource_global,
):
    """AsyncMock postgres_db prewired with the 3-user / 2-project graph.

    Lookups are dispatched on UUID — both ``UUID`` objects and the string
    form are accepted, matching how the real layer normalises. Methods
    that return None for missing rows do so here as well.
    """
    projects = {_PID_A: project_a, _PID_B: project_b}
    jobs = {_JID_A: job_a, _JID_B: job_b}
    threads = {_TID_A: thread_a, _TID_B: thread_b}
    sessions = {_SID_A: builder_session_a, _SID_B: builder_session_b}
    datasources = {
        _DSID_A: datasource_a,
        _DSID_B: datasource_b,
        _DSID_GLOBAL: datasource_global,
    }
    # (project_id, user_id) → role. Each project owner is its sole member.
    memberships: dict[tuple[UUID, UUID], str] = {
        (_PID_A, _UID_A): "owner",
        (_PID_B, _UID_B): "owner",
    }
    # datasource_id → [project_id, ...] linkages via project_datasources.
    datasource_projects: dict[UUID, list[UUID]] = {
        _DSID_A: [_PID_A],
        _DSID_B: [_PID_B],
        _DSID_GLOBAL: [],
    }

    def _to_uuid(value):
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (ValueError, TypeError):
            return None

    async def get_project(pid):
        return projects.get(_to_uuid(pid))

    async def get_job(jid):
        return jobs.get(_to_uuid(jid))

    async def get_thread(tid):
        return threads.get(_to_uuid(tid))

    async def get_builder_session(sid):
        return sessions.get(_to_uuid(sid))

    async def get_user_role_in_project(pid, uid):
        return memberships.get((_to_uuid(pid), _to_uuid(uid)))

    async def get_projects_for_user(uid, limit=100):
        u = _to_uuid(uid)
        return [
            projects[pid]
            for (pid, member_uid), _role in memberships.items()
            if member_uid == u
        ]

    async def get_datasource(dsid):
        return datasources.get(_to_uuid(dsid))

    async def list_datasource_projects(dsid):
        return [str(pid) for pid in datasource_projects.get(_to_uuid(dsid), [])]

    async def list_datasources(job_id=None, ds_type=None, limit=100):
        return list(datasources.values())

    db = AsyncMock()
    db.get_project = AsyncMock(side_effect=get_project)
    db.get_job = AsyncMock(side_effect=get_job)
    db.get_thread = AsyncMock(side_effect=get_thread)
    db.get_builder_session = AsyncMock(side_effect=get_builder_session)
    db.get_user_role_in_project = AsyncMock(side_effect=get_user_role_in_project)
    db.get_projects_for_user = AsyncMock(side_effect=get_projects_for_user)
    db.get_datasource = AsyncMock(side_effect=get_datasource)
    db.list_datasource_projects = AsyncMock(side_effect=list_datasource_projects)
    db.list_datasources = AsyncMock(side_effect=list_datasources)
    return db


@pytest.fixture
def fake_request():
    """Minimal stand-in for fastapi.Request.

    The access.py helpers only call ``require_approved_user(request, db)``
    on it; that path doesn't touch the request when callers patch the
    auth resolver in tests. A bare MagicMock with no spec is enough.
    """
    return MagicMock()
