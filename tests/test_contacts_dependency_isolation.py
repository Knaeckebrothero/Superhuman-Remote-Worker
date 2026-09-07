"""Exercise contacts through ASGI with app-owned services and real request parsing."""

import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from orchestrator.routers.contacts import (
    ContactsDependencies,
    get_contacts_dependencies,
    project_router,
    router,
)


def make_app(dependencies):
    app = FastAPI()
    app.state.contacts_dependencies = dependencies
    app.include_router(router)
    app.include_router(project_router)
    return app


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://contacts.test"
    )


@pytest.mark.asyncio
async def test_two_apps_keep_database_and_caller_isolated_during_concurrent_requests():
    ready = [asyncio.Event(), asyncio.Event()]
    dependencies = []
    for index in range(2):
        db = MagicMock()
        db.list_contacts_for_user = AsyncMock(return_value=[{"id": f"c-{index}"}])

        async def authorize(request, actual_db, index=index, expected_db=db):
            assert actual_db is expected_db
            ready[index].set()
            await ready[1 - index].wait()
            return {"id": f"user-{index}"}

        dependencies.append(
            ContactsDependencies(db=db, require_approved_user=authorize)
        )

    async with (
        client_for(make_app(dependencies[0])) as first,
        client_for(make_app(dependencies[1])) as second,
    ):
        responses = await asyncio.wait_for(
            asyncio.gather(
                first.get("/api/contacts?q=first"), second.get("/api/contacts?q=second")
            ),
            timeout=2,
        )
    for index, response in enumerate(responses):
        assert response.status_code == 200
        assert response.json() == {"contacts": [{"id": f"c-{index}"}]}
        dependencies[index].db.list_contacts_for_user.assert_awaited_once_with(
            f"user-{index}", project_id=None, channel=None, q=("first", "second")[index]
        )


@pytest.mark.asyncio
async def test_dependency_override_is_scoped_to_one_app():
    db = MagicMock()
    db.list_contacts_for_user = AsyncMock(return_value=[])
    original = ContactsDependencies(db=db)
    first, second = make_app(original), make_app(original)
    first.dependency_overrides[get_contacts_dependencies] = (
        lambda: ContactsDependencies(
            db=db,
            require_approved_user=AsyncMock(return_value={"id": "overridden-user"}),
        )
    )
    async with client_for(first) as first_client, client_for(second) as second_client:
        allowed = await first_client.get("/api/contacts")
        denied = await second_client.get("/api/contacts")
    assert allowed.status_code == 200
    assert denied.status_code == 401
    db.list_contacts_for_user.assert_awaited_once_with(
        "overridden-user", project_id=None, channel=None, q=None
    )


@pytest.mark.asyncio
async def test_project_gate_denies_before_database_access():
    db = MagicMock()
    gate = AsyncMock(side_effect=HTTPException(status_code=403, detail="denied"))
    app = make_app(ContactsDependencies(db=db, require_project_member=gate))
    async with client_for(app) as client:
        response = await client.get("/api/projects/project-1/contacts")
    assert response.status_code == 403
    assert response.json() == {"detail": "denied"}
    assert db.mock_calls == []
    assert gate.await_args.args[1:] == (db, "project-1")


@pytest.mark.asyncio
async def test_create_parses_body_normalizes_address_and_serializes_response():
    db = MagicMock()
    db.create_contact = AsyncMock(return_value={"id": "contact-1"})
    db.add_contact_address = AsyncMock(return_value={"id": "address-1"})
    expected = {"id": "contact-1", "display_name": "Anna", "addresses": []}
    db.get_contact = AsyncMock(return_value=expected)
    app = make_app(
        ContactsDependencies(
            db=db, require_approved_user=AsyncMock(return_value={"id": "user-1"})
        )
    )
    async with client_for(app) as client:
        invalid = await client.post("/api/contacts", json={"addresses": []})
        assert invalid.status_code == 422
        db.create_contact.assert_not_awaited()
        response = await client.post(
            "/api/contacts",
            json={
                "display_name": "  Anna  ",
                "addresses": [{"channel": "email", "address": " Anna@Example.test "}],
            },
        )
    assert response.status_code == 200
    assert response.json() == {"contact": expected}
    db.create_contact.assert_awaited_once_with("user-1", "Anna", None)
    db.add_contact_address.assert_awaited_once_with(
        "contact-1", "user-1", "email", "anna@example.test", False
    )


@pytest.mark.asyncio
async def test_internal_contacts_derive_project_from_job_and_validate_exclusive_ids():
    db = MagicMock()
    db.resolve_project_for_agent = AsyncMock(return_value="server-project")
    db.get_project_contacts = AsyncMock(return_value=[{"id": "contact-1"}])
    gate = AsyncMock()
    app = make_app(ContactsDependencies(db=db, require_internal=gate))
    async with client_for(app) as client:
        invalid = await client.get("/api/contacts/internal/list?job_id=j&thread_id=t")
        assert invalid.status_code == 400
        db.resolve_project_for_agent.assert_not_awaited()
        response = await client.get(
            "/api/contacts/internal/list?job_id=j&project_id=client-project"
        )
    assert response.status_code == 200
    assert response.json() == {"contacts": [{"id": "contact-1"}]}
    assert gate.await_count == 2
    db.resolve_project_for_agent.assert_awaited_once_with(job_id="j", thread_id=None)
    db.get_project_contacts.assert_awaited_once_with("server-project")


def test_importing_contacts_does_not_execute_application_startup():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; import orchestrator.routers.contacts; "
                "assert 'orchestrator.main' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
