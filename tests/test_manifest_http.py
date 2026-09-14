"""Authentication, public request shape, and side-effect-free manifest operations."""

import json
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest

from orchestrator.routers.manifests import ManifestDependencies, router
from orchestrator.services.manifests import ManifestService


def manifest():
    return {
        "apiVersion": "srw/v1alpha1",
        "kind": "Expert",
        "metadata": {"name": "custom-worker"},
        "spec": {
            "runtime": {"image": "example/custom:1", "config": {"customTool": None}}
        },
    }


def make_app(*, service=None, guard=None, resources=None, execution=None, trigger=None):
    app = FastAPI()
    dependencies = ManifestDependencies(
        service=service or ManifestService(),
        require_approved_user=guard or AsyncMock(return_value={"id": "approved-user"}),
        resources=resources,
        execution=execution,
        trigger_dispatch=trigger,
    )
    app.state.manifest_dependencies_factory = lambda: dependencies
    app.include_router(router)
    return app


def test_secret_request_remains_documented_without_echoing_validation_inputs():
    body = make_app().openapi()["paths"]["/api/resource-secrets/{name}"]["put"][
        "requestBody"
    ]
    schema = body["content"]["application/json"]["schema"]
    assert body["required"] is True
    assert schema["required"] == ["values"]
    assert schema["properties"]["values"]["additionalProperties"]["writeOnly"] is True
    assert "$ref" not in json.dumps(schema)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/resources", None),
        ("GET", "/api/resources/00000000-0000-0000-0000-000000000001", None),
        (
            "DELETE",
            "/api/resources/00000000-0000-0000-0000-000000000001?expected_version=1",
            None,
        ),
        ("POST", "/api/manifests/apply", {"source": "{}"}),
        (
            "PUT",
            "/api/resource-secrets/provider",
            {"values": {"key": "private-sentinel"}},
        ),
        (
            "POST",
            "/api/resources/00000000-0000-0000-0000-000000000001/outcome",
            {"attempt": 1, "outcome": "Succeeded"},
        ),
        ("GET", "/api/workspace-instances/00000000-0000-0000-0000-000000000001", None),
        (
            "DELETE",
            "/api/workspace-instances/00000000-0000-0000-0000-000000000001?expected_generation=0",
            None,
        ),
    ],
)
async def test_native_operations_require_approval_before_store_or_runtime(
    method, path, body
):
    resources, execution = Mock(), Mock()
    app = make_app(
        resources=resources,
        execution=execution,
        guard=AsyncMock(side_effect=HTTPException(403, "Denied")),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(
            method, path, **({"json": body} if body is not None else {})
        )
    assert response.status_code == 403
    assert not resources.mock_calls and not execution.mock_calls


@pytest.mark.asyncio
async def test_credential_validation_never_echoes_invalid_secret_values():
    resources = Mock()
    resources.put_secret = AsyncMock(return_value={"resourceVersion": 1})
    app = make_app(resources=resources)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/api/resource-secrets/provider",
            json={"values": {"key": {"private-sentinel": "invalid-secret-shape"}}},
        )
        assert response.status_code == 422
        assert (
            "private-sentinel" not in response.text
            and "invalid-secret-shape" not in response.text
        )
        resources.put_secret.assert_not_awaited()
        response = await client.put(
            "/api/resource-secrets/provider",
            json={"values": {"key": "private-sentinel"}},
        )
        assert response.json() == {"resourceVersion": 1}
        assert resources.put_secret.await_args.kwargs["values"] == {
            "key": "private-sentinel"
        }


@pytest.mark.asyncio
async def test_apply_triggers_dispatch_only_after_successful_resource_commit():
    events = []
    resources = Mock()

    async def apply(*args, **kwargs):
        events.append("committed")
        return {"operationId": "receipt"}

    resources.apply = AsyncMock(side_effect=apply)
    app = make_app(resources=resources, trigger=lambda: events.append("dispatch"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/manifests/apply",
            json={"source": "{}", "idempotency_key": "submission"},
        )
        assert response.status_code == 200 and events == ["committed", "dispatch"]
        resources.apply.side_effect = HTTPException(409, "Conflict")
        response = await client.post("/api/manifests/apply", json={"source": "{}"})
        assert response.status_code == 409 and events == ["committed", "dispatch"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,method",
    [("schema", "GET"), ("validate", "POST"), ("preview", "POST"), ("export", "POST")],
)
@pytest.mark.parametrize("status", [401, 403])
async def test_every_operation_requires_an_approved_user_before_service_access(
    operation, method, status
):
    service = Mock(spec=ManifestService)
    guard = AsyncMock(side_effect=HTTPException(status_code=status, detail="Denied"))
    app = make_app(service=service, guard=guard)
    kwargs = (
        {}
        if method == "GET"
        else {"json": {"source": json.dumps(manifest()), "format": "json"}}
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(method, f"/api/manifests/{operation}", **kwargs)
    assert response.status_code == status
    guard.assert_awaited_once()
    assert service.mock_calls == []


@pytest.mark.asyncio
async def test_authenticated_validate_preview_and_export_roundtrip():
    guard = AsyncMock(return_value={"id": "approved-user"})
    app = make_app(guard=guard)
    body = {"source": json.dumps(manifest()), "format": "json"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        schema = await client.get("/api/manifests/schema")
        assert schema.status_code == 200
        assert schema.json()["properties"]["apiVersion"]["const"] == "srw/v1alpha1"
        validation = await client.post("/api/manifests/validate", json=body)
        assert validation.status_code == 200
        assert validation.json()["valid"] is True
        preview = await client.post("/api/manifests/preview", json=body)
        assert preview.status_code == 422
        assert preview.json()["detail"]["code"] == "MissingScope"
        scoped = {**body, "default_scope": {"kind": "Account", "name": "personal"}}
        preview = await client.post("/api/manifests/preview", json=scoped)
        assert preview.status_code == 200
        result = preview.json()
        assert result["admissionReady"] is False
        assert result["effects"] == []
        assert result["resolved"][0]["spec"]["runtime"]["config"] == {
            "customTool": None
        }
        exported = await client.post(
            "/api/manifests/export", json={**scoped, "output_format": "yaml"}
        )
        assert exported.status_code == 200
        again = await client.post(
            "/api/manifests/preview",
            json={"source": exported.json()["source"], "format": "yaml"},
        )
        assert again.status_code == 200
        assert again.json() == result
    assert guard.await_count == 6


@pytest.mark.asyncio
async def test_malformed_source_has_structured_diagnostics_without_echoed_values():
    async with AsyncClient(
        transport=ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/manifests/validate",
            json={"source": "kind: Expert\nkind: PRIVATE-SENTINEL"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "DuplicateKey",
        "message": "Duplicate object key.",
        "document": 1,
        "path": "/",
    }
    assert "PRIVATE-SENTINEL" not in response.text


@pytest.mark.asyncio
async def test_request_model_rejects_undeclared_controls_and_invalid_formats():
    async with AsyncClient(
        transport=ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        for extra in ({"apply": True}, {"actor_id": "someone-else"}, {"format": "xml"}):
            response = await client.post(
                "/api/manifests/preview",
                json={"source": json.dumps(manifest()), **extra},
            )
            assert response.status_code == 422


@pytest.mark.asyncio
async def test_bundle_preview_does_not_claim_live_scope_or_secret_authorization():
    doc = manifest()
    doc["metadata"]["scope"] = {"kind": "Account", "name": "declared-scope"}
    doc["spec"]["runtime"]["env"] = {
        "TOKEN": {"secretRef": {"name": "auth", "key": "token"}}
    }
    async with AsyncClient(
        transport=ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/manifests/preview", json={"source": json.dumps(doc), "format": "json"}
        )
    assert response.status_code == 200
    result = response.json()
    assert result["admissionReady"] is False
    assert {"resourceAuthorization", "credentialDelivery"} <= set(
        result["pendingChecks"]
    )
    assert result["resolved"][0]["spec"]["runtime"]["env"]["TOKEN"] == {
        "secretRef": {"name": "auth", "key": "token", "scope": doc["metadata"]["scope"]}
    }


@pytest.mark.asyncio
async def test_main_mount_uses_the_real_approved_user_dependency(monkeypatch):
    from orchestrator import main

    guard = AsyncMock(return_value={"id": "approved-user"})
    monkeypatch.setattr(main, "require_approved_user", guard)
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/manifests/validate",
            json={"source": json.dumps(manifest()), "format": "json"},
        )
    assert response.status_code == 200
    assert response.json()["valid"] is True
    guard.assert_awaited_once()
    assert guard.await_args.args[1] is main.postgres_db
