"""B06's four assigned caller files serve the application handling the request.

R1.B06 closed ``routers/sessions.py``, ``routers/vm_guest.py``,
``services/provision_or_assign.py`` and ``services/session_lifecycle.py``
against ``orchestrator.main``. The point of that move is not tidiness: while
each of them resolved a module singleton, two FastAPI applications in one
process shared one store and one provisioner no matter which was answering,
and neither background task could be driven without importing the whole
startup chain.

These cases hold the closure itself — a regression to any process-wide lookup
fails here rather than in production.
"""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
import pytest

from orchestrator.routers import sessions, vm_guest
from orchestrator.services import (
    provision_or_assign as provision_or_assign_service,
    session_lifecycle,
    session_attach_recovery,
    thread_admission,
)

from ._mounted_router import mount_router


_ENTITY_ID = "c3333333-3333-4333-8333-333333333333"
_THREAD_ID = "d4444444-4444-4444-8444-444444444444"


# --------------------------------------------------------------------------- #
# No module in the closed set reaches the application module at all
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "module",
    [sessions, vm_guest, provision_or_assign_service, session_lifecycle],
    ids=lambda m: m.__name__.rsplit(".", 1)[-1],
)
def test_closed_caller_never_imports_the_application_module(module) -> None:
    """Neither at import time nor from inside any function body."""

    tree = ast.parse(inspect.getsource(module))
    offenders = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "orchestrator.main"
    ] + [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        and any(alias.name == "orchestrator.main" for alias in node.names)
    ]
    assert offenders == [], offenders


# --------------------------------------------------------------------------- #
# vm_guest: the store comes off the request
# --------------------------------------------------------------------------- #


def test_vm_guest_store_lookup_never_reaches_the_application_module() -> None:
    """The helper resolves per call, off the request, and imports nothing."""

    helper = ast.parse(inspect.getsource(vm_guest._get_db)).body[0]
    assert isinstance(helper, ast.FunctionDef)
    assert [argument.arg for argument in helper.args.args] == ["request"]
    assert not [
        node
        for node in ast.walk(helper)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert ast.unparse(helper.body[-1]) == "return request.app.state.store"


def test_vm_guest_register_uses_the_store_of_its_own_application(monkeypatch) -> None:
    seen: list[object] = []

    async def _auth(request, db, entity_id):
        del request, entity_id
        seen.append(db)
        raise _Unauthorized()

    class _Unauthorized(Exception):
        pass

    monkeypatch.setattr(vm_guest, "require_vm_guest", _auth)

    app_a = mount_router(vm_guest.router)
    app_b = mount_router(vm_guest.router)
    store_a, store_b = object(), object()
    app_a.state.store = store_a
    app_b.state.store = store_b

    body = {"hostname": "h", "ip": "10.0.0.1", "pid": 1}
    url = f"/api/internal/vm/{_ENTITY_ID}/register"
    for app in (app_a, app_b, app_a):
        client = TestClient(app, raise_server_exceptions=False)
        client.post(url, json=body)

    assert seen == [store_a, store_b, store_a]


# --------------------------------------------------------------------------- #
# sessions: every collaborator comes off the request
# --------------------------------------------------------------------------- #


def test_sessions_dependencies_resolve_from_the_requesting_application() -> None:
    helper = ast.parse(inspect.getsource(sessions.get_sessions_dependencies)).body[0]
    assert isinstance(helper, ast.FunctionDef)
    assert [argument.arg for argument in helper.args.args] == ["request"]
    assert (
        ast.unparse(helper.body[-1])
        == "return request.app.state.sessions_dependencies_factory()"
    )


def _sessions_app(store: Any) -> Any:
    app = mount_router(sessions.router)
    app.state.sessions_dependencies_factory = lambda: SimpleNamespace(store=store)
    return app


def test_connection_route_uses_the_store_of_its_own_application(monkeypatch) -> None:
    seen: list[object] = []

    async def _approved(request, db):
        del request
        seen.append(db)
        return {"id": "u1", "is_approved": True}

    monkeypatch.setattr(sessions, "require_approved_user", _approved)

    store_a, store_b = _MissingThreadStore(), _MissingThreadStore()
    app_a, app_b = _sessions_app(store_a), _sessions_app(store_b)

    url = f"/api/sessions/{_THREAD_ID}/connection"
    for app in (app_a, app_b, app_a):
        assert TestClient(app).get(url).status_code == 404

    assert seen == [store_a, store_b, store_a]


class _MissingThreadStore:
    async def get_thread(self, thread_id: str) -> None:
        del thread_id
        return None


# --------------------------------------------------------------------------- #
# The two background paths take their collaborators as arguments
# --------------------------------------------------------------------------- #


def test_wait_for_binding_takes_the_callers_store() -> None:
    signature = inspect.signature(session_lifecycle.wait_for_binding)
    store = signature.parameters["store"]
    assert store.kind is inspect.Parameter.KEYWORD_ONLY
    assert store.default is inspect.Parameter.empty


def test_provision_or_assign_takes_a_constructed_port() -> None:
    signature = inspect.signature(provision_or_assign_service.provision_or_assign)
    dependencies = signature.parameters["dependencies"]
    assert dependencies.kind is inspect.Parameter.KEYWORD_ONLY
    assert dependencies.default is inspect.Parameter.empty
    # ``from __future__ import annotations`` leaves the annotation a string.
    assert dependencies.annotation == "ProvisionOrAssignDependencies"


@pytest.mark.parametrize(
    "dependencies_class",
    [
        thread_admission.ThreadAdmissionDependencies,
        session_attach_recovery.SessionAttachRecoveryDependencies,
    ],
    ids=["thread_admission", "session_attach_recovery"],
)
def test_schedulers_receive_the_binder_as_an_explicit_port(dependencies_class) -> None:
    """Neither scheduler imports the binder; it arrives already constructed."""

    assert "provision_or_assign" in dependencies_class.__dataclass_fields__


@pytest.mark.parametrize(
    "module",
    [thread_admission, session_attach_recovery],
    ids=lambda m: m.__name__.rsplit(".", 1)[-1],
)
def test_schedulers_no_longer_import_the_binder_at_the_call_site(module) -> None:
    source = inspect.getsource(module)
    assert "from orchestrator.services.provision_or_assign import" not in source
