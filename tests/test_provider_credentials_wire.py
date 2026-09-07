"""Wire contracts for user- and project-scoped provider API keys.

Characterized against the pre-extraction ``main.py`` handlers and ported onto
``orchestrator.routers.provider_credentials`` unchanged. The load-bearing
property is secret omission: a key goes in, and no read or write response ever
carries it back — only the prefix the store persists.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from tests._mounted_router import mount_router


USER_ID = UUID("00000000-0000-0000-0000-0000000000a1")
PROJECT_ID = "00000000-0000-0000-0000-0000000000b1"
SECRET = "sk-super-secret-value-never-echoed"


def _stored_row(provider="openai", label=None):
    """What the store returns: prefix only, never the key column."""
    return {
        "id": uuid4(),
        "provider": provider,
        "key_prefix": SECRET[:8],
        "label": label,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def _store(**over):
    store = SimpleNamespace(
        list_user_api_keys=AsyncMock(return_value=[_stored_row()]),
        upsert_user_api_key=AsyncMock(return_value=_stored_row()),
        delete_user_api_key=AsyncMock(return_value=True),
        get_project_members=AsyncMock(
            return_value=[{"user_id": USER_ID, "role": "owner"}]
        ),
        list_project_api_keys=AsyncMock(return_value=[_stored_row()]),
        upsert_project_api_key=AsyncMock(return_value=_stored_row()),
        delete_project_api_key=AsyncMock(return_value=True),
    )
    for key, value in over.items():
        setattr(store, key, value)
    return store


def _client(store, *, user=None):
    from orchestrator.routers.provider_credentials import (
        ProviderCredentialsDependencies,
        router,
    )
    from orchestrator.services.provider_credentials import (
        ProviderCredentialDependencies,
    )

    resolved = user if user is not None else {"id": USER_ID, "is_admin": False}

    async def approved(_request, _store):
        return resolved

    deps = ProviderCredentialsDependencies(
        store=store,
        operations=ProviderCredentialDependencies(store=store),
        require_approved_user=approved,
    )
    app = mount_router(
        router,
        factories={"provider_credentials_dependencies_factory": lambda: deps},
    )
    return TestClient(app)


# =============================================================================
# Secret omission
# =============================================================================


def test_set_then_read_never_echoes_the_key():
    store = _store()
    client = _client(store)

    put = client.put(
        "/api/settings/api-keys/openai",
        json={"api_key": SECRET, "label": "personal"},
    )
    assert put.status_code == 200
    assert SECRET not in put.text
    assert put.json()["key_prefix"] == SECRET[:8]
    assert "api_key" not in put.json()

    got = client.get("/api/settings/api-keys")
    assert got.status_code == 200
    assert SECRET not in got.text
    assert got.json()[0]["key_prefix"] == SECRET[:8]


def test_only_the_prefix_is_derived_from_the_submitted_key():
    store = _store()
    _client(store).put("/api/settings/api-keys/openai", json={"api_key": SECRET})
    kwargs = store.upsert_user_api_key.await_args.kwargs
    assert kwargs["key_prefix"] == SECRET[:8]
    assert kwargs["api_key"] == SECRET  # the store, and only the store, gets it


def test_project_key_write_response_omits_the_key():
    store = _store()
    resp = _client(store).put(
        f"/api/projects/{PROJECT_ID}/api-keys/openai", json={"api_key": SECRET}
    )
    assert resp.status_code == 200
    assert SECRET not in resp.text


def test_uuid_and_datetime_columns_are_stringified():
    row = _stored_row()
    store = _store(list_user_api_keys=AsyncMock(return_value=[row]))
    body = _client(store).get("/api/settings/api-keys").json()[0]
    assert body["id"] == str(row["id"])
    assert body["created_at"] == str(row["created_at"])


# =============================================================================
# Provider validation
# =============================================================================


def test_unknown_provider_is_refused_before_any_store_write():
    store = _store()
    resp = _client(store).put(
        "/api/settings/api-keys/not-a-provider", json={"api_key": SECRET}
    )
    assert resp.status_code == 400
    assert "Invalid provider 'not-a-provider'" in resp.json()["detail"]
    assert SECRET not in resp.text
    store.upsert_user_api_key.assert_not_awaited()


def test_project_provider_validation_precedes_the_role_check():
    """Order is characterized, not chosen: main validated the provider first."""
    store = _store()
    resp = _client(store).put(
        f"/api/projects/{PROJECT_ID}/api-keys/nope", json={"api_key": SECRET}
    )
    assert resp.status_code == 400
    store.get_project_members.assert_not_awaited()


# =============================================================================
# Deletion
# =============================================================================


def test_delete_missing_user_key_is_404():
    store = _store(delete_user_api_key=AsyncMock(return_value=False))
    resp = _client(store).delete("/api/settings/api-keys/openai")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "No API key for provider 'openai'"


def test_delete_user_key_reports_deleted():
    resp = _client(_store()).delete("/api/settings/api-keys/openai")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}


def test_delete_missing_project_key_is_404():
    store = _store(delete_project_api_key=AsyncMock(return_value=False))
    resp = _client(store).delete(f"/api/projects/{PROJECT_ID}/api-keys/openai")
    assert resp.status_code == 404


# =============================================================================
# Project authorization
# =============================================================================


def test_non_member_cannot_list_project_keys():
    store = _store(get_project_members=AsyncMock(return_value=[]))
    resp = _client(store).get(f"/api/projects/{PROJECT_ID}/api-keys")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Not a member of this project"
    store.list_project_api_keys.assert_not_awaited()


def test_member_can_list_project_keys():
    store = _store(
        get_project_members=AsyncMock(
            return_value=[{"user_id": str(USER_ID), "role": "viewer"}]
        )
    )
    resp = _client(store).get(f"/api/projects/{PROJECT_ID}/api-keys")
    assert resp.status_code == 200


@pytest.mark.parametrize("role", ["viewer", "reader", "guest"])
def test_read_only_roles_cannot_write_a_project_key(role):
    store = _store(
        get_project_members=AsyncMock(
            return_value=[{"user_id": str(USER_ID), "role": role}]
        )
    )
    resp = _client(store).put(
        f"/api/projects/{PROJECT_ID}/api-keys/openai", json={"api_key": SECRET}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Requires owner or editor role"
    store.upsert_project_api_key.assert_not_awaited()


@pytest.mark.parametrize("role", ["owner", "editor"])
def test_write_roles_may_set_a_project_key(role):
    store = _store(
        get_project_members=AsyncMock(
            return_value=[{"user_id": str(USER_ID), "role": role}]
        )
    )
    resp = _client(store).put(
        f"/api/projects/{PROJECT_ID}/api-keys/openai", json={"api_key": SECRET}
    )
    assert resp.status_code == 200


def test_non_member_cannot_delete_a_project_key():
    store = _store(get_project_members=AsyncMock(return_value=[]))
    resp = _client(store).delete(f"/api/projects/{PROJECT_ID}/api-keys/openai")
    assert resp.status_code == 403
    store.delete_project_api_key.assert_not_awaited()


# =============================================================================
# Per-application dependency isolation
# =============================================================================


def test_each_application_resolves_its_own_store():
    first, second = _store(), _store()
    _client(first).get("/api/settings/api-keys")
    first.list_user_api_keys.assert_awaited_once()
    second.list_user_api_keys.assert_not_awaited()

    _client(second).get("/api/settings/api-keys")
    second.list_user_api_keys.assert_awaited_once()
    first.list_user_api_keys.assert_awaited_once()
