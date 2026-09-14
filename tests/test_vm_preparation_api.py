"""Cache scope access and signed controller responses are independent gates."""

from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException
import httpx
import pytest

from orchestrator.services.manifest_workspaces import ManifestWorkspaceService
from orchestrator.services.vm_provisioner import VMProvisioner
from shared.vm_lifecycle_auth import AUTH_FIELD, sign_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("write", [False, True])
async def test_cache_account_and_token_scope_are_checked_before_controller_io(write):
    actor = {"id": str(uuid4()), "is_admin": False}
    db, provisioner = AsyncMock(), AsyncMock()
    service = ManifestWorkspaceService(
        db, None, namespace="test", default_image="test", vm_provisioner=provisioner
    )
    with pytest.raises(HTTPException) as denied:
        await service.preparation_cache(
            actor,
            {"kind": "Account", "name": str(uuid4())},
            uid=str(uuid4()) if write else None,
        )
    assert denied.value.status_code == 403
    provisioner.preparation_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_viewer_can_inspect_but_cannot_evict_cache():
    actor = {"id": str(uuid4()), "is_admin": False}
    db, provisioner = AsyncMock(), AsyncMock()
    db.get_project.return_value = {"status": "active"}
    db.get_user_role_in_project.return_value = "viewer"
    provisioner.preparation_operation.return_value = {"artifacts": []}
    service = ManifestWorkspaceService(
        db, None, namespace="test", default_image="test", vm_provisioner=provisioner
    )
    scope = {"kind": "Project", "name": str(uuid4())}
    assert await service.preparation_cache(actor, scope) == {"artifacts": []}
    with pytest.raises(HTTPException) as denied:
        await service.preparation_cache(actor, scope, uid=str(uuid4()))
    assert denied.value.status_code == 403
    assert provisioner.preparation_operation.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["bad-id", "me"])
async def test_invalid_project_id_fails_before_database_query(name):
    db = AsyncMock()
    service = ManifestWorkspaceService(db, None, namespace="test", default_image="test")
    with pytest.raises(HTTPException) as error:
        await service.preparation_cache(
            {"id": str(uuid4())}, {"kind": "Project", "name": name}
        )
    assert error.value.status_code == 422
    db.get_project.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["none", "correlation", "operation", "secret"])
async def test_controller_cache_response_requires_signature_operation_and_correlation(
    monkeypatch, tamper
):
    import json

    monkeypatch.setenv("VM_MODE", "same-cluster")
    monkeypatch.setenv("VM_CONTROLLER_URL", "http://controller.invalid")
    monkeypatch.setenv(
        "VM_LIFECYCLE_HMAC_SECRET", "fixture-only-preparation-secret-32-bytes"
    )
    provisioner = VMProvisioner()

    async def respond(request):
        payload = json.loads(request.content)
        result = sign_payload(
            {"artifacts": []},
            direction="response",
            operation="preparation-delete"
            if tamper == "operation"
            else "preparation-list",
            secret=b"wrong-secret"
            if tamper == "secret"
            else provisioner._lifecycle_hmac_secret,
            correlation_id=str(uuid4())
            if tamper == "correlation"
            else payload[AUTH_FIELD]["request_id"],
        )
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(
        base_url="http://controller.invalid", transport=httpx.MockTransport(respond)
    ) as client:
        provisioner._http_client = client
        if tamper == "none":
            assert await provisioner.preparation_operation(
                "list", {"scope": {"kind": "Account", "uid": str(uuid4())}}
            ) == {"artifacts": []}
        else:
            with pytest.raises(ValueError, match="authentication"):
                await provisioner.preparation_operation(
                    "list", {"scope": {"kind": "Account", "uid": str(uuid4())}}
                )
