"""Wire contracts for the two simple admin system-settings toggles.

New coverage: before R1.B02 these handlers lived in ``main.py`` and only their
*route registration* was asserted (``tests/test_admin_vm_controls.py``). The
property worth pinning is the deliberate asymmetry of their defaults —
``vm_workspaces`` fails open, ``tts_library`` fails closed — because a
copy-paste that unified them would silently disable VM workspaces on upgrade or
silently open a shared, plan-limited voice-slot budget.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests._mounted_router import mount_router


ADMIN = {"id": "00000000-0000-0000-0000-0000000000ad", "email": "admin@test"}
UPDATED_AT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)

VM_PATH = "/api/admin/system-settings/vm_workspaces"
TTS_PATH = "/api/admin/system-settings/tts_library"


def _client(*, row=None, get_error=None, upsert_error=None, admin=ADMIN, gate=None):
    from orchestrator.routers.system_settings import (
        SystemSettingsDependencies as RouteDeps,
    )
    from orchestrator.routers.system_settings import router
    from orchestrator.services.system_settings import (
        SystemSettingsDependencies as OpDeps,
    )

    async def get_setting(_key):
        if get_error is not None:
            raise get_error
        return row

    async def upsert(_key, value, *, updated_by):
        if upsert_error is not None:
            raise upsert_error
        return {"value": value, "updated_at": UPDATED_AT, "updated_by": updated_by}

    store = SimpleNamespace(
        get_system_setting=AsyncMock(side_effect=get_setting),
        upsert_system_setting=AsyncMock(side_effect=upsert),
    )

    async def require_admin(_request):
        if gate is not None:
            return await gate(_request)
        return admin

    deps = RouteDeps(operations=OpDeps(store=store), require_admin=require_admin)
    app = mount_router(
        router, factories={"system_settings_dependencies_factory": lambda: deps}
    )
    return TestClient(app), store


# =============================================================================
# vm_workspaces — fail OPEN
# =============================================================================


def test_absent_vm_row_reads_as_enabled():
    client, _ = _client(row=None)
    body = client.get(VM_PATH).json()
    assert body == {"enabled": True, "updated_at": None, "updated_by": None}


def test_vm_row_is_disabled_only_on_an_explicit_false():
    client, _ = _client(row={"value": {"enabled": False}, "updated_at": UPDATED_AT})
    assert client.get(VM_PATH).json()["enabled"] is False


@pytest.mark.parametrize("value", [{}, {"enabled": None}, {"other": 1}, "nonsense"])
def test_a_malformed_vm_value_still_reads_as_enabled(value):
    client, _ = _client(row={"value": value})
    assert client.get(VM_PATH).json()["enabled"] is True


def test_vm_toggle_stamps_the_acting_admin_and_echoes_the_row():
    client, store = _client()
    body = client.put(VM_PATH, json={"enabled": False}).json()
    assert body["enabled"] is False
    assert body["updated_by"] == "admin@test"
    assert body["updated_at"] == UPDATED_AT.isoformat()
    assert store.upsert_system_setting.await_args.args[0] == "vm_workspaces"


def test_vm_toggle_falls_back_to_the_admin_id_without_an_email():
    client, store = _client(admin={"id": "admin-uuid"})
    client.put(VM_PATH, json={"enabled": True})
    assert store.upsert_system_setting.await_args.kwargs["updated_by"] == "admin-uuid"


# =============================================================================
# tts_library — fail CLOSED
# =============================================================================


def test_absent_tts_row_reads_as_disabled():
    client, _ = _client(row=None)
    body = client.get(TTS_PATH).json()
    assert body == {"enabled": False, "updated_at": None, "updated_by": None}


@pytest.mark.parametrize("value", [{}, {"enabled": False}, {"enabled": "yes"}, None])
def test_tts_row_is_enabled_only_on_an_explicit_true(value):
    client, _ = _client(row={"value": value})
    assert client.get(TTS_PATH).json()["enabled"] is False


def test_tts_row_enabled_on_true():
    client, _ = _client(row={"value": {"enabled": True}, "updated_at": UPDATED_AT})
    assert client.get(TTS_PATH).json()["enabled"] is True


def test_tts_toggle_writes_the_namespaced_key():
    client, store = _client()
    client.put(TTS_PATH, json={"enabled": True})
    assert (
        store.upsert_system_setting.await_args.args[0]
        == "tts.elevenlabs_library_enabled"
    )


# =============================================================================
# Shared refusals
# =============================================================================


@pytest.mark.parametrize("path", [VM_PATH, TTS_PATH])
@pytest.mark.parametrize(
    "body", [{"enabled": "true"}, {"enabled": 1}, {}, {"enabled": None}]
)
def test_a_non_boolean_is_refused_before_any_write(path, body):
    client, store = _client()
    resp = client.put(path, json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "`enabled` must be a boolean"
    store.upsert_system_setting.assert_not_awaited()


@pytest.mark.parametrize("path", [VM_PATH, TTS_PATH])
def test_a_store_read_failure_is_a_500(path):
    client, _ = _client(get_error=RuntimeError("db down"))
    resp = client.get(path)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "db down"


@pytest.mark.parametrize("path", [VM_PATH, TTS_PATH])
def test_a_store_write_failure_is_a_500(path):
    client, _ = _client(upsert_error=RuntimeError("db down"))
    assert client.put(path, json={"enabled": True}).status_code == 500


@pytest.mark.parametrize("path", [VM_PATH, TTS_PATH])
@pytest.mark.parametrize("method", ["get", "put"])
def test_every_route_runs_the_admin_gate(path, method):
    async def deny(_request):
        raise HTTPException(status_code=403, detail="Admin access required")

    client, store = _client(gate=deny)
    resp = getattr(client, method)(
        path, **({"json": {"enabled": True}} if method == "put" else {})
    )
    assert resp.status_code == 403
    store.get_system_setting.assert_not_awaited()
    store.upsert_system_setting.assert_not_awaited()


def test_each_application_resolves_its_own_store():
    first, first_store = _client()
    _second, second_store = _client()
    first.get(VM_PATH)
    first_store.get_system_setting.assert_awaited_once()
    second_store.get_system_setting.assert_not_awaited()
