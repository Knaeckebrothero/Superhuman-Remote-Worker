"""Wire contracts for the extracted admin main-cloud settings routes.

R1.B04 lane M. Two things live here that a careless move would break, so both
are pinned end to end:

* **What leaves the process.** GET reports a secret only as
  ``{"env_var": ..., "set": ...}``; PUT and POST /test drop every secret-field
  key from the body before it can reach JSONB or a probe overlay; and no
  response, on any path, echoes a secret value.
* **Installation authority.** The backfill refuses — 409/503, never a guess —
  unless the live installation has just been re-attested and every named
  provider resolves to exactly one proof, and the reload path answers 409
  rather than reporting success when the active instance moved.

The admin gate is awaited before any body inspection on every route, so a
non-admin cannot learn which backends the deployment accepts or whether a
secret env var is wired.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests._mounted_router import mount_router

from orchestrator.routers import main_cloud_settings as route_module
from orchestrator.services import main_cloud_settings as ops
from orchestrator.services.cloud import config as cloud_config

BASE = "/api/admin/system-settings/main_cloud"
ADMIN = {"id": "admin-1", "email": "admin@example.test"}
SECRET = "super-secret-client-value"
PROOF = "0" * 64
OTHER_PROOF = "f" * 64
ACTIVE_INSTANCE = "4e72e665-1f70-4b69-9804-d981b51416e6"


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class _Authority:
    """Stand-in for ``MainCloudBackendInstanceAuthority``.

    ``routing`` is a **property returning a fresh copy**, matching the sealed
    real class. That matters: the GET path stamps ``__secret_fields__`` onto
    the overlay's copy, and only a copying property keeps that synthetic key
    out of ``effective``.
    """

    def __init__(
        self,
        backend_id: str = "opencloud",
        instance_id: str = ACTIVE_INSTANCE,
        proof: str = PROOF,
        routing: dict[str, Any] | None = None,
        secret_refs: dict[str, str] | None = None,
    ) -> None:
        self.backend_id = backend_id
        self.backend_instance_id = instance_id
        self.installation_proof_sha256 = proof
        self.routing_sha256 = "a" * 64
        self.secret_revision = 3
        self._routing = dict(routing or {"base_url": "https://cloud.example"})
        self._secret_refs = dict(
            secret_refs or {"keycloak_client_secret": "env:OC_CLIENT_SECRET"}
        )

    @property
    def routing(self) -> dict[str, Any]:
        return dict(self._routing)

    @property
    def secret_refs(self) -> dict[str, str]:
        return dict(self._secret_refs)


def _authority(*args: Any, **kwargs: Any) -> _Authority:
    return _Authority(*args, **kwargs)


def _active_backend() -> SimpleNamespace:
    return SimpleNamespace(
        backend_id="opencloud",
        backend_instance_id=ACTIVE_INSTANCE,
        is_initialized=True,
        is_configured=True,
        _settings=SimpleNamespace(
            base_url="https://cloud.example",
            public_url="https://cloud.example",
            keycloak_issuer="https://auth.example/realms/srw",
            keycloak_client_id="srw",
            admin_role_claim_value="admin",
            default_quota_bytes=10,
            # A settings object always carries the live secret; nothing in the
            # read path may reach for it.
            keycloak_client_secret=SimpleNamespace(get_secret_value=lambda: SECRET),
        ),
    )


def _router(**over: Any) -> SimpleNamespace:
    router = SimpleNamespace(
        active=_active_backend(),
        active_instance_id=ACTIVE_INSTANCE,
    )
    for key, value in over.items():
        setattr(router, key, value)
    return router


def _store(**over: Any) -> SimpleNamespace:
    store = SimpleNamespace(
        get_active_main_cloud_backend_instance=AsyncMock(return_value=None),
        list_main_cloud_backend_instances=AsyncMock(return_value=[]),
        survey_unstamped_main_cloud_rows=AsyncMock(
            return_value={"projects": [], "threads": []}
        ),
        stamp_main_cloud_instance_authority=AsyncMock(
            return_value={"projects": 0, "threads": 0}
        ),
        delete_system_setting=AsyncMock(return_value=None),
    )
    for key, value in over.items():
        setattr(store, key, value)
    return store


def _wire(*, store=None, cloud_router=None, admin_gate=None, rebind=None):
    calls = SimpleNamespace(admin=0, rebound=[])
    backing = store if store is not None else _store()

    async def require_admin(_request):
        calls.admin += 1
        if admin_gate is not None:
            return await admin_gate()
        return ADMIN

    operations = ops.MainCloudSettingsDependencies(
        store=backing,
        cloud_router=cloud_router if cloud_router is not None else _router(),
        rebind_cloud_router=rebind or calls.rebound.append,
        thread_mount_dependencies=lambda: None,
    )
    dependencies = route_module.MainCloudSettingsRouteDependencies(
        operations=operations,
        require_admin=require_admin,
    )
    app = mount_router(
        route_module.router,
        factories={"main_cloud_settings_dependencies_factory": lambda: dependencies},
    )
    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        calls=calls,
        store=backing,
        operations=operations,
    )


def _contains_secret(payload: Any) -> bool:
    return SECRET in json.dumps(payload, default=str)


# --------------------------------------------------------------------------- #
# The admin gate is the first thing every route does
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", BASE, None),
        ("put", BASE, {"value": "not-an-object"}),
        ("post", f"{BASE}/test", {"value": {"backend_id": "bogus"}}),
        ("post", f"{BASE}/reload", None),
        ("post", f"{BASE}/backfill-instance-authority", None),
        ("delete", BASE, None),
    ],
)
def test_a_non_admin_is_refused_before_the_body_is_inspected(method, path, body):
    async def deny():
        raise HTTPException(status_code=403, detail="Admin access required")

    wired = _wire(admin_gate=deny)
    kwargs = {"json": body} if body is not None else {}

    response = getattr(wired.client, method)(path, **kwargs)

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"
    assert wired.calls.admin == 1
    wired.store.get_active_main_cloud_backend_instance.assert_not_awaited()


# --------------------------------------------------------------------------- #
# GET — secrets are provenance only
# --------------------------------------------------------------------------- #


def test_get_reports_secret_provenance_and_never_a_secret_value(monkeypatch):
    monkeypatch.setenv("OC_CLIENT_SECRET", SECRET)
    authority = _authority()
    store = _store(
        get_active_main_cloud_backend_instance=AsyncMock(
            return_value={
                "authority": authority,
                "activated_at": None,
                "activation_revision": 7,
            }
        )
    )
    wired = _wire(store=store)

    body = wired.client.get(BASE).json()

    assert body["secrets"] == {
        "keycloak_client_secret": {"env_var": "OC_CLIENT_SECRET", "set": True}
    }
    assert body["activation_revision"] == 7
    assert body["overlay"]["present"] is True
    assert body["overlay"]["credentials_ref"] == "env:OC_CLIENT_SECRET"
    assert body["overlay"]["value"]["__secret_fields__"] == ["keycloak_client_secret"]
    # The synthetic marker belongs to the overlay copy alone; the recorded
    # routing authority mirrored into ``effective`` must stay untouched.
    assert "__secret_fields__" not in body["effective"]
    assert body["allowed_backends"] == ["nextcloud", "opencloud"]
    assert body["backend_instance"]["installation_proof_sha256"] == PROOF
    assert not _contains_secret(body)


def test_get_reports_an_unset_secret_without_inventing_one(monkeypatch):
    monkeypatch.delenv("OC_CLIENT_SECRET", raising=False)
    store = _store(
        get_active_main_cloud_backend_instance=AsyncMock(
            return_value={
                "authority": _authority(),
                "activated_at": None,
                "activation_revision": 1,
            }
        )
    )
    wired = _wire(store=store)

    body = wired.client.get(BASE).json()

    assert body["secrets"]["keycloak_client_secret"]["set"] is False
    assert "value" not in body["secrets"]["keycloak_client_secret"]


def test_get_degrades_to_an_absent_overlay_when_the_registry_read_fails():
    store = _store(
        get_active_main_cloud_backend_instance=AsyncMock(side_effect=RuntimeError("db"))
    )
    wired = _wire(store=store)

    body = wired.client.get(BASE).json()

    assert body["overlay"]["present"] is False
    assert body["backend_instance"] is None
    assert body["activation_revision"] == 0
    assert body["secrets"] == {}
    # The live effective config still describes the active adapter.
    assert body["effective"]["backend_id"] == "opencloud"
    assert not _contains_secret(body)


def test_get_effective_config_never_reads_the_settings_secret():
    wired = _wire()

    body = wired.client.get(BASE).json()

    assert body["effective"]["keycloak_client_id"] == "srw"
    assert "keycloak_client_secret" not in body["effective"]
    assert not _contains_secret(body)


# --------------------------------------------------------------------------- #
# The sanitizer
# --------------------------------------------------------------------------- #


def test_sanitizer_keeps_only_known_non_secret_fields():
    clean = ops._sanitize_main_cloud_value(
        "opencloud",
        {
            "backend_id": "opencloud",
            "base_url": "https://cloud.example",
            "keycloak_client_secret": SECRET,
            "unknown_knob": "x",
            "public_url": "",
            "default_quota_bytes": 5,
        },
    )

    assert clean == {
        "backend_id": "opencloud",
        "base_url": "https://cloud.example",
        "default_quota_bytes": 5,
        "__secret_fields__": ["keycloak_client_secret"],
    }


def test_sanitizer_records_every_nextcloud_secret_field():
    clean = ops._sanitize_main_cloud_value(
        "nextcloud",
        {"admin_password": SECRET, "agent_password": SECRET, "admin_user": "admin"},
    )

    assert clean["__secret_fields__"] == [
        "admin_password",
        "agent_password",
        "oidc_client_secret",
    ]
    assert not _contains_secret(clean)


def test_env_var_provenance_reports_presence_only(monkeypatch):
    monkeypatch.setenv("SOME_SECRET", SECRET)
    assert ops._env_var_provenance("SOME_SECRET") == {
        "env_var": "SOME_SECRET",
        "set": True,
    }


# --------------------------------------------------------------------------- #
# PUT — refusal ordering, then the write
# --------------------------------------------------------------------------- #


def test_put_rejects_a_non_object_value_first():
    wired = _wire()

    response = wired.client.put(BASE, json={"value": "nope"})

    assert response.status_code == 400
    assert response.json()["detail"] == "`value` must be an object"


def test_put_rejects_an_unknown_backend_naming_the_allowed_set():
    wired = _wire()

    response = wired.client.put(BASE, json={"value": {"backend_id": "dropbox"}})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "unknown backend_id 'dropbox'" in detail
    assert "['nextcloud', 'opencloud']" in detail


def test_put_rejects_a_non_string_credentials_ref_before_the_revision():
    wired = _wire()

    response = wired.client.put(
        BASE,
        json={"value": {"backend_id": "opencloud"}, "credentials_ref": 7},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "`credentials_ref` must be a string or null"


@pytest.mark.parametrize("revision", [None, -1, "3", True])
def test_put_requires_a_non_negative_integer_revision(revision):
    """``True`` is deliberately refused: the check is ``type(...) is not int``."""
    wired = _wire()

    response = wired.client.put(
        BASE,
        json={
            "value": {"backend_id": "opencloud"},
            "expected_activation_revision": revision,
        },
    )

    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "`expected_activation_revision` must be a non-negative integer"
    )


def _valid_put_body(**over: Any) -> dict[str, Any]:
    body = {
        "value": {
            "backend_id": "opencloud",
            "base_url": "https://cloud.example",
            "keycloak_client_secret": SECRET,
        },
        "credentials_ref": "env:OC_CLIENT_SECRET",
        "expected_activation_revision": 0,
    }
    body.update(over)
    return body


def _accept_config(monkeypatch, *, missing=None):
    monkeypatch.setattr(cloud_config, "load_main_cloud_config", lambda **_k: object())
    monkeypatch.setattr(
        cloud_config, "missing_secret_envs", lambda *_a, **_k: missing or []
    )


def test_put_never_lets_a_secret_field_reach_the_probe_overlay(monkeypatch):
    seen: dict[str, Any] = {}

    def load(*, db_overlay):
        seen["probe"] = db_overlay
        return object()

    monkeypatch.setattr(cloud_config, "load_main_cloud_config", load)
    monkeypatch.setattr(cloud_config, "missing_secret_envs", lambda *_a, **_k: [])
    activated = {"authority": _authority(), "activation_revision": 1}
    monkeypatch.setattr(
        ops, "activate_main_cloud_config", AsyncMock(return_value=activated)
    )
    monkeypatch.setattr(ops, "fire_reload", AsyncMock())
    wired = _wire()

    response = wired.client.put(BASE, json=_valid_put_body())

    assert response.status_code == 200
    assert "keycloak_client_secret" not in seen["probe"]["value"]
    assert not _contains_secret(seen["probe"])
    assert not _contains_secret(response.json())


def test_put_refuses_when_the_backend_secret_env_is_not_wired(monkeypatch):
    _accept_config(monkeypatch, missing=[{"env_var": "OC_CLIENT_SECRET"}])
    activate = AsyncMock()
    monkeypatch.setattr(ops, "activate_main_cloud_config", activate)
    wired = _wire()

    response = wired.client.put(BASE, json=_valid_put_body())

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "secret env not set for backend 'opencloud': OC_CLIENT_SECRET" in detail
    assert "built-in dev" in detail
    activate.assert_not_awaited()


def test_put_maps_an_invalid_config_to_400(monkeypatch):
    def load(**_kwargs):
        raise ValueError("base_url is required")

    monkeypatch.setattr(cloud_config, "load_main_cloud_config", load)
    monkeypatch.setattr(cloud_config, "missing_secret_envs", lambda *_a, **_k: [])
    wired = _wire()

    response = wired.client.put(BASE, json=_valid_put_body())

    assert response.status_code == 400
    assert "invalid main cloud config" in response.json()["detail"]


def test_put_maps_a_failed_attestation_to_500_without_activating(monkeypatch):
    _accept_config(monkeypatch)
    monkeypatch.setattr(
        ops,
        "activate_main_cloud_config",
        AsyncMock(side_effect=RuntimeError("remote proof mismatch")),
    )
    wired = _wire()

    response = wired.client.put(BASE, json=_valid_put_body())

    assert response.status_code == 500
    assert "no unverified adapter" in response.json()["detail"]


def test_put_maps_a_lost_cas_to_409(monkeypatch):
    _accept_config(monkeypatch)
    monkeypatch.setattr(ops, "activate_main_cloud_config", AsyncMock(return_value=None))
    wired = _wire()

    response = wired.client.put(BASE, json=_valid_put_body())

    assert response.status_code == 409
    assert "activation authority changed" in response.json()["detail"]


def test_put_fans_out_and_drops_the_legacy_setting_on_success(monkeypatch):
    _accept_config(monkeypatch)
    authority = _authority()
    monkeypatch.setattr(
        ops,
        "activate_main_cloud_config",
        AsyncMock(return_value={"authority": authority, "activation_revision": 4}),
    )
    fire = AsyncMock()
    monkeypatch.setattr(ops, "fire_reload", fire)
    wired = _wire()

    body = wired.client.put(BASE, json=_valid_put_body()).json()

    assert body == {
        "status": "ok",
        "backend_id": "opencloud",
        "backend_instance_id": ACTIVE_INSTANCE,
        "activation_revision": 4,
        "reloaded": True,
    }
    wired.store.delete_system_setting.assert_awaited_once_with("main_cloud")
    fire.assert_awaited_once_with(wired.store, ACTIVE_INSTANCE)


def test_put_survives_a_failed_legacy_cleanup(monkeypatch):
    _accept_config(monkeypatch)
    monkeypatch.setattr(
        ops,
        "activate_main_cloud_config",
        AsyncMock(return_value={"authority": _authority(), "activation_revision": 4}),
    )
    monkeypatch.setattr(ops, "fire_reload", AsyncMock())
    store = _store(delete_system_setting=AsyncMock(side_effect=RuntimeError("gone")))
    wired = _wire(store=store)

    assert wired.client.put(BASE, json=_valid_put_body()).status_code == 200


# --------------------------------------------------------------------------- #
# POST /test — a dry run that persists nothing
# --------------------------------------------------------------------------- #


def test_dry_run_rejects_an_unknown_backend():
    wired = _wire()

    response = wired.client.post(f"{BASE}/test", json={"value": {"backend_id": "x"}})

    assert response.status_code == 400
    assert response.json()["detail"] == "unknown backend_id 'x'"


def test_dry_run_names_the_unwired_env_var_but_no_value(monkeypatch):
    monkeypatch.setattr(
        cloud_config,
        "missing_secret_envs",
        lambda *_a, **_k: [{"env_var": "OC_CLIENT_SECRET"}],
    )
    wired = _wire()

    body = wired.client.post(f"{BASE}/test", json=_valid_put_body()).json()

    assert body["ok"] is False
    assert "OC_CLIENT_SECRET" in body["detail"]
    assert not _contains_secret(body)


def test_dry_run_reports_a_build_failure_without_persisting(monkeypatch):
    monkeypatch.setattr(cloud_config, "missing_secret_envs", lambda *_a, **_k: [])

    def build(**_kwargs):
        raise RuntimeError("bad url")

    monkeypatch.setattr(ops, "build_backend", build)
    wired = _wire()

    body = wired.client.post(f"{BASE}/test", json=_valid_put_body()).json()

    # The builder's exception text can name internal hosts and credentials, so
    # the client gets the route's own wording plus the correlation id that
    # finds the logged exception.
    assert body["ok"] is False
    assert body["detail"] == "build_backend failed"
    assert "bad url" not in json.dumps(body)
    assert re.fullmatch(r"[0-9a-f]{12}", body["error_ref"])
    wired.store.delete_system_setting.assert_not_awaited()


def test_dry_run_always_closes_the_probe_backend(monkeypatch):
    monkeypatch.setattr(cloud_config, "missing_secret_envs", lambda *_a, **_k: [])
    closed: list[bool] = []
    probe = SimpleNamespace(
        ensure_initialized=AsyncMock(return_value=True),
        health_check=AsyncMock(
            return_value=SimpleNamespace(ok=True, detail="fine", latency_ms=12)
        ),
        close=AsyncMock(side_effect=lambda: closed.append(True)),
    )
    monkeypatch.setattr(ops, "build_backend", lambda **_k: probe)
    wired = _wire()

    body = wired.client.post(f"{BASE}/test", json=_valid_put_body()).json()

    assert body == {"ok": True, "detail": "fine", "latency_ms": 12}
    assert closed == [True]


def test_dry_run_reports_an_uninitialized_backend(monkeypatch):
    monkeypatch.setattr(cloud_config, "missing_secret_envs", lambda *_a, **_k: [])
    probe = SimpleNamespace(
        ensure_initialized=AsyncMock(return_value=False),
        health_check=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(ops, "build_backend", lambda **_k: probe)
    wired = _wire()

    body = wired.client.post(f"{BASE}/test", json=_valid_put_body()).json()

    assert body["ok"] is False
    assert "not initialized" in body["detail"]
    probe.health_check.assert_not_awaited()


def test_dry_run_sanitizes_the_probe_overlay(monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cloud_config, "missing_secret_envs", lambda *_a, **_k: [])

    def build(*, db_overlay):
        seen["probe"] = db_overlay
        raise RuntimeError("stop here")

    monkeypatch.setattr(ops, "build_backend", build)
    wired = _wire()

    wired.client.post(f"{BASE}/test", json=_valid_put_body())

    assert "keycloak_client_secret" not in seen["probe"]["value"]
    assert not _contains_secret(seen["probe"])


# --------------------------------------------------------------------------- #
# POST /reload
# --------------------------------------------------------------------------- #


def test_reload_reports_the_active_instance_on_success(monkeypatch):
    monkeypatch.setattr(
        ops, "reload_active_main_cloud_instance", AsyncMock(return_value=True)
    )
    wired = _wire()

    body = wired.client.post(f"{BASE}/reload").json()

    assert body == {
        "status": "ok",
        "backend_id": "opencloud",
        "backend_instance_id": ACTIVE_INSTANCE,
    }
    # The router object is mutated in place by ``replace_active``; nothing in
    # this path rebinds the application's handle.
    assert wired.calls.rebound == []


@pytest.mark.parametrize("outcome", [False, None])
def test_reload_409s_when_the_active_instance_moved(monkeypatch, outcome):
    monkeypatch.setattr(
        ops, "reload_active_main_cloud_instance", AsyncMock(return_value=outcome)
    )
    wired = _wire()

    response = wired.client.post(f"{BASE}/reload")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "active instance changed during re-attestation; retry"
    )


def test_reload_maps_a_failed_attestation_to_500(monkeypatch):
    monkeypatch.setattr(
        ops,
        "reload_active_main_cloud_instance",
        AsyncMock(side_effect=RuntimeError("keycloak down")),
    )
    wired = _wire()

    response = wired.client.post(f"{BASE}/reload")

    assert response.status_code == 500
    assert "re-attestation failed" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# POST /backfill-instance-authority
# --------------------------------------------------------------------------- #


def _legacy_project(provider="opencloud"):
    return {
        "id": uuid4(),
        "name": "Legacy",
        "status": "active",
        "main_cloud_backend": provider,
    }


def _backfill_store(*, projects=None, threads=None, registry=None, active=None):
    return _store(
        survey_unstamped_main_cloud_rows=AsyncMock(
            return_value={
                "projects": projects if projects is not None else [],
                "threads": threads if threads is not None else [],
            }
        ),
        list_main_cloud_backend_instances=AsyncMock(
            return_value=registry if registry is not None else [_authority()]
        ),
        get_active_main_cloud_backend_instance=AsyncMock(
            return_value=(
                active
                if active is not None
                else {"authority": _authority(), "activation_revision": 2}
            )
        ),
    )


def test_backfill_is_a_noop_without_reattesting_when_nothing_is_unstamped(monkeypatch):
    reattest = AsyncMock(return_value=True)
    monkeypatch.setattr(ops, "reload_active_main_cloud_instance", reattest)
    wired = _wire(store=_backfill_store())

    body = wired.client.post(f"{BASE}/backfill-instance-authority").json()

    assert body["status"] == "noop"
    assert body["applied"] is False
    reattest.assert_not_awaited()


def test_backfill_dry_runs_by_default(monkeypatch):
    monkeypatch.setattr(
        ops, "reload_active_main_cloud_instance", AsyncMock(return_value=True)
    )
    store = _backfill_store(projects=[_legacy_project()])
    wired = _wire(store=store)

    body = wired.client.post(f"{BASE}/backfill-instance-authority").json()

    assert body["status"] == "dry_run"
    assert body["applied"] is False
    assert body["projects"] == 1
    assert body["plan"][0]["backend_instance_id"] == ACTIVE_INSTANCE
    store.stamp_main_cloud_instance_authority.assert_not_awaited()


def test_backfill_applies_only_when_asked(monkeypatch):
    monkeypatch.setattr(
        ops, "reload_active_main_cloud_instance", AsyncMock(return_value=True)
    )
    store = _backfill_store(projects=[_legacy_project()])
    store.stamp_main_cloud_instance_authority = AsyncMock(
        return_value={"projects": 1, "threads": 0}
    )
    wired = _wire(store=store)

    body = wired.client.post(f"{BASE}/backfill-instance-authority?apply=true").json()

    assert body["status"] == "ok"
    assert body["applied"] is True
    assert body["projects"] == 1
    store.stamp_main_cloud_instance_authority.assert_awaited_once_with(
        backend_id="opencloud", backend_instance_id=ACTIVE_INSTANCE
    )


def test_backfill_refuses_two_distinct_installations(monkeypatch):
    monkeypatch.setattr(
        ops, "reload_active_main_cloud_instance", AsyncMock(return_value=True)
    )
    store = _backfill_store(
        projects=[_legacy_project()],
        registry=[
            _authority(),
            _authority(instance_id=str(uuid4()), proof=OTHER_PROOF),
        ],
    )
    wired = _wire(store=store)

    response = wired.client.post(f"{BASE}/backfill-instance-authority?apply=true")

    assert response.status_code == 409
    assert "distinct" in response.json()["detail"]
    store.stamp_main_cloud_instance_authority.assert_not_awaited()


def test_backfill_refuses_a_provider_that_is_not_the_active_backend(monkeypatch):
    monkeypatch.setattr(
        ops, "reload_active_main_cloud_instance", AsyncMock(return_value=True)
    )
    store = _backfill_store(
        projects=[_legacy_project(provider="nextcloud")],
        registry=[_authority(backend_id="nextcloud")],
    )
    wired = _wire(store=store)

    response = wired.client.post(f"{BASE}/backfill-instance-authority?apply=true")

    assert response.status_code == 409
    assert "is not the active backend" in response.json()["detail"]


def test_backfill_refuses_when_the_registry_proof_does_not_match(monkeypatch):
    monkeypatch.setattr(
        ops, "reload_active_main_cloud_instance", AsyncMock(return_value=True)
    )
    store = _backfill_store(
        projects=[_legacy_project()],
        registry=[_authority(proof=OTHER_PROOF)],
    )
    wired = _wire(store=store)

    response = wired.client.post(f"{BASE}/backfill-instance-authority?apply=true")

    assert response.status_code == 409
    assert "does not match the installation just attested" in response.json()["detail"]


def test_backfill_refuses_when_reattestation_lost_the_race(monkeypatch):
    monkeypatch.setattr(
        ops, "reload_active_main_cloud_instance", AsyncMock(return_value=False)
    )
    store = _backfill_store(projects=[_legacy_project()])
    wired = _wire(store=store)

    response = wired.client.post(f"{BASE}/backfill-instance-authority?apply=true")

    assert response.status_code == 409
    store.stamp_main_cloud_instance_authority.assert_not_awaited()


def test_backfill_503s_when_the_live_proof_cannot_be_verified(monkeypatch):
    monkeypatch.setattr(
        ops,
        "reload_active_main_cloud_instance",
        AsyncMock(side_effect=RuntimeError("upstream down")),
    )
    store = _backfill_store(projects=[_legacy_project()])
    wired = _wire(store=store)

    response = wired.client.post(f"{BASE}/backfill-instance-authority?apply=true")

    assert response.status_code == 503
    assert "cannot verify the live installation proof" in response.json()["detail"]


def test_backfill_refuses_without_an_active_instance(monkeypatch):
    monkeypatch.setattr(
        ops, "reload_active_main_cloud_instance", AsyncMock(return_value=True)
    )
    store = _backfill_store(projects=[_legacy_project()])
    store.get_active_main_cloud_backend_instance = AsyncMock(return_value=None)
    wired = _wire(store=store)

    response = wired.client.post(f"{BASE}/backfill-instance-authority?apply=true")

    assert response.status_code == 409
    assert "no active main-cloud instance" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# DELETE
# --------------------------------------------------------------------------- #


def test_delete_activates_the_env_described_installation(monkeypatch):
    authority = _authority()
    activate = AsyncMock(
        return_value={"authority": authority, "activation_revision": 9}
    )
    monkeypatch.setattr(ops, "activate_main_cloud_config", activate)
    fire = AsyncMock()
    monkeypatch.setattr(ops, "fire_reload", fire)
    store = _store(
        get_active_main_cloud_backend_instance=AsyncMock(
            return_value={"authority": authority, "activation_revision": 8}
        )
    )
    wired = _wire(store=store)

    body = wired.client.delete(BASE).json()

    assert body == {
        "status": "ok",
        "existed": True,
        "backend_id": "opencloud",
        "backend_instance_id": ACTIVE_INSTANCE,
        "activation_revision": 9,
    }
    assert activate.await_args.kwargs["db_overlay"] is None
    assert activate.await_args.kwargs["expected_activation_revision"] == 8
    fire.assert_awaited_once_with(store, ACTIVE_INSTANCE)


def test_delete_maps_a_lost_cas_to_409(monkeypatch):
    monkeypatch.setattr(ops, "activate_main_cloud_config", AsyncMock(return_value=None))
    wired = _wire()

    response = wired.client.delete(BASE)

    assert response.status_code == 409
    assert "reload and retry" in response.json()["detail"]


def test_delete_maps_a_failed_attestation_to_500(monkeypatch):
    monkeypatch.setattr(
        ops,
        "activate_main_cloud_config",
        AsyncMock(side_effect=RuntimeError("no proof")),
    )
    wired = _wire()

    response = wired.client.delete(BASE)

    assert response.status_code == 500
    assert "env-described main cloud attestation failed" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Per-invocation dependency resolution
# --------------------------------------------------------------------------- #


def test_a_rebound_router_singleton_is_observed_by_the_next_request():
    state = {"router": _router()}

    def _dependencies():
        async def require_admin(_request):
            return ADMIN

        return route_module.MainCloudSettingsRouteDependencies(
            operations=ops.MainCloudSettingsDependencies(
                store=_store(),
                cloud_router=state["router"],
                rebind_cloud_router=lambda _new: None,
                thread_mount_dependencies=lambda: None,
            ),
            require_admin=require_admin,
        )

    app = mount_router(
        route_module.router,
        factories={"main_cloud_settings_dependencies_factory": _dependencies},
    )
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get(BASE).json()["effective"]["backend_id"] == "opencloud"

    swapped = _active_backend()
    swapped.backend_id = "nextcloud"
    swapped._base_url = "https://nc.example"
    state["router"] = _router(active=swapped, active_instance_id="other")

    body = client.get(BASE).json()
    assert body["effective"]["backend_id"] == "nextcloud"
    assert body["effective"]["base_url"] == "https://nc.example"
