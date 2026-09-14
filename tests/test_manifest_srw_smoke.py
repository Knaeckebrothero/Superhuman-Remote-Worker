"""Checks for the opt-in deterministic SRW adapter rollout smoke."""

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest

from shared.manifests import validate_documents
from tests import test_manifest_native_full_schema as full_schema

database = full_schema.database
postgres_url = full_schema.postgres_url


@pytest.fixture
def smoke_module():
    path = Path(__file__).resolve().parents[1] / "scripts/manifests-srw-k3d-smoke.py"
    spec = importlib.util.spec_from_file_location("manifest_srw_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_workloads_declare_the_real_adapter_with_bounded_private_config(
    smoke_module,
):
    prefix = "cutover-123456abcdef-"
    worker = smoke_module.authored_expert(
        prefix, "test.invalid/srw:installed", "zz-srw-smoke"
    )
    job = smoke_module.authored_job(prefix, worker)
    session = smoke_module.authored_expert(
        prefix, "test.invalid/srw:installed", "zz-srw-smoke", session=True
    )
    validate_documents([job, session])
    assert job["spec"]["execution"]["workspace"]["template"]["inline"] == {
        "backend": "sandbox",
        "retention": "Delete",
    }
    assert job["spec"]["retry"] == {"maxAttempts": 1}
    assert job["spec"]["completion"] == {"mode": "Reported"}
    for document in (worker, session):
        runtime = document["spec"]["runtime"]
        assert runtime["adapter"] == "srw/v1"
        assert not set(runtime) & {"command", "args", "env", "probes", "resources"}
        config = runtime["config"]["config"]
        assert config["memory"]["enabled"] is False
        assert config["llm"]["model"] == config["auxiliary"]["model"] == "zz-srw-smoke"
        assert not any(
            config["tools"].get(group)
            for group in ("research", "delegation", "communication", "shell")
        )


def test_unknown_mutation_is_not_replayed_or_printed(smoke_module):
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("Bearer private-value", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(
            smoke_module.GateFailure, match="mutations are not replayed"
        ) as error:
            smoke_module.request(
                client,
                "POST",
                "https://api.localhost/api/admin/providers/endpoints",
                payload={"api_key": "private-value"},
            )
    assert len(calls) == 1
    assert "private-value" not in str(error.value)


def test_remote_workspace_evidence_program_is_executable(smoke_module, monkeypatch):
    def inspect_program(source):
        compile(source, "workspace-evidence.py", "exec")
        return {"inspected": True}

    monkeypatch.setattr(smoke_module.cutover, "remote_json", inspect_program)
    assert smoke_module.readonly_evidence(kind="Job", work_id=str(uuid4())) == {
        "inspected": True
    }


@pytest.mark.parametrize("kind", ("Session", "Job"))
def test_owned_cleanup_retries_explicit_503_after_exact_readback(
    smoke_module, monkeypatch, kind
):
    owner, work = str(uuid4()), str(uuid4())
    field = "title" if kind == "Session" else "description"
    row = {"id": work, "user_id": owner, field: "E2E-owned-" + kind.lower()}
    responses = iter(
        [
            (200, row),
            (503, {"detail": "private-value"}),
            (200, row),
            (200, {}),
            (404, {}),
        ]
    )
    calls, evidence = [], []

    def respond(request):
        calls.append(request.method)
        code, body = next(responses)
        return httpx.Response(code, json=body)

    monkeypatch.setattr(smoke_module.time, "sleep", lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        smoke_module.cleanup_owned_workload(
            client,
            kind=kind,
            work_id=work,
            owner_id=owner,
            prefix="owned-",
            headers={},
            evidence=evidence,
        )
    assert calls == ["GET", "DELETE", "GET", "DELETE", "GET"]
    assert evidence[0]["absent"] is True
    assert "private-value" not in json.dumps(evidence)


@pytest.mark.parametrize("field", ("id", "user_id", "title"))
def test_owned_cleanup_refuses_changed_identity_before_retry(
    smoke_module, monkeypatch, field
):
    owner, work = str(uuid4()), str(uuid4())
    row = {"id": work, "user_id": owner, "title": "E2E-owned-session"}
    responses = iter([(200, row), (503, {}), (200, {**row, field: "changed"})])
    calls = []

    def respond(request):
        calls.append(request.method)
        code, body = next(responses)
        return httpx.Response(code, json=body)

    monkeypatch.setattr(smoke_module.time, "sleep", lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(smoke_module.GateFailure, match="deletion refused"):
            smoke_module.cleanup_owned_workload(
                client,
                kind="Session",
                work_id=work,
                owner_id=owner,
                prefix="owned-",
                headers={},
                evidence=[],
            )
    assert calls == ["GET", "DELETE", "GET"]


@pytest.mark.parametrize("result", ("transport", 500, 503))
def test_owned_cleanup_does_not_replay_unknown_failure_or_unbounded_503(
    smoke_module, result
):
    owner, work = str(uuid4()), str(uuid4())
    calls, evidence = [], []

    def respond(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"id": work, "user_id": owner, "title": "E2E-owned-session"},
            )
        if result == "transport":
            raise httpx.ReadTimeout("Bearer private-value", request=request)
        return httpx.Response(result, json={"detail": "private-value"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(smoke_module.GateFailure) as error:
            smoke_module.cleanup_owned_workload(
                client,
                kind="Session",
                work_id=work,
                owner_id=owner,
                prefix="owned-",
                headers={},
                evidence=evidence,
                timeout=0,
            )
    assert calls == ["GET", "DELETE"]
    assert "private-value" not in str(error.value) + json.dumps(evidence)
    if result == 503:
        assert "deadline" in str(error.value)


def test_owned_cleanup_accepts_already_absent_identity_without_deletion(smoke_module):
    calls, evidence = [], []

    def respond(request):
        calls.append(request.method)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        smoke_module.cleanup_owned_workload(
            client,
            kind="Session",
            work_id=str(uuid4()),
            owner_id=str(uuid4()),
            prefix="owned-",
            headers={},
            evidence=evidence,
        )
    assert calls == ["GET"]
    assert evidence[0]["absent"] is True


def test_role_authenticated_fixture_login_uses_access_token(smoke_module):
    def respond(request):
        return httpx.Response(
            200,
            json={
                "access_token": "test-access-token",
                "id_token": "identity-without-roles",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        headers = smoke_module.login(client, "test-user", "test-password")
    assert headers == {"Authorization": "Bearer test-access-token"}


def test_identity_cleanup_recovers_unknown_create_then_disables_logs_out_and_deletes(
    smoke_module, monkeypatch
):
    uid = str(uuid4())
    name = "srw-manifest-admin-123456abcdef"
    calls = []

    def respond(request):
        calls.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": uid,
                        "username": name,
                        "attributes": {smoke_module.OWNER_LABEL: [name]},
                    }
                ],
            )
        return httpx.Response(204)

    monkeypatch.setattr(smoke_module, "expires_soon", lambda headers: False)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        admin = smoke_module.TemporaryAdmin(client, name)
        admin.bootstrap = {"Authorization": "Bearer private-bootstrap"}
        admin.created_intent = True
        admin.revoke()
    assert [item[0] for item in calls] == ["GET", "PUT", "POST", "DELETE"]
    assert all(uid in item[1] for item in calls[1:])
    assert calls[1][2] == {"enabled": False}
    assert calls[2][1].endswith("/logout")
    assert admin.cleanup_evidence["identityRevoked"] is True
    assert admin.cleanup_evidence["identityDeleted"] is True
    assert "private-bootstrap" not in json.dumps(admin.cleanup_evidence)


def test_identity_cleanup_refuses_a_replaced_ownership_marker(
    smoke_module, monkeypatch
):
    calls = []

    def respond(request):
        calls.append(request.method)
        return httpx.Response(
            200,
            json=[
                {
                    "id": str(uuid4()),
                    "username": "owned-name",
                    "attributes": {smoke_module.OWNER_LABEL: ["different-run"]},
                }
            ],
        )

    monkeypatch.setattr(smoke_module, "expires_soon", lambda headers: False)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        admin = smoke_module.TemporaryAdmin(client, "owned-name")
        admin.created_intent = True
        with pytest.raises(smoke_module.GateFailure, match="ownership marker"):
            admin.revoke()
    assert calls == ["GET"]


def test_failed_logout_still_attempts_exact_identity_delete(smoke_module, monkeypatch):
    uid = str(uuid4())
    calls = []

    def respond(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": uid,
                        "username": "owned-name",
                        "attributes": {smoke_module.OWNER_LABEL: ["owned-name"]},
                    }
                ],
            )
        if request.method == "POST":
            return httpx.Response(500, json={"secret": "must-not-appear"})
        return httpx.Response(204)

    monkeypatch.setattr(smoke_module, "expires_soon", lambda headers: False)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        admin = smoke_module.TemporaryAdmin(client, "owned-name")
        admin.created_intent = True
        with pytest.raises(smoke_module.GateFailure) as error:
            admin.revoke()
    assert calls == ["GET", "PUT", "POST", "DELETE"]
    assert admin.cleanup_evidence["identityDeleted"] is True
    assert "must-not-appear" not in str(error.value)


def test_keycloak_creation_receipt_supports_realms_without_unmanaged_attributes(
    smoke_module, monkeypatch
):
    uid, app_uid = str(uuid4()), str(uuid4())
    created = []
    name = "srw-manifest-admin-receipt"

    def respond(request):
        if request.url.path == "/api/auth/me":
            return httpx.Response(
                200,
                json={"user": {"id": app_uid, "is_admin": True, "is_approved": True}},
            )
        if request.method == "GET" and request.url.path.endswith("/users"):
            return httpx.Response(200, json=created)
        if request.method == "POST" and request.url.path.endswith("/users"):
            body = json.loads(request.content)
            created.append(
                {"id": uid, "username": body["username"], "email": body["email"]}
            )
            return httpx.Response(
                201,
                headers={
                    "Location": "https://auth.localhost/admin/realms/srw/users/" + uid
                },
            )
        if request.method == "GET" and request.url.path.endswith("/clients"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/clients"):
            body = json.loads(request.content)
            assert body["defaultClientScopes"] == ["profile", "email", "roles"]
            assert body["protocolMappers"][0]["protocolMapper"] == "oidc-sub-mapper"
            return httpx.Response(
                201,
                headers={
                    "Location": "https://auth.localhost/admin/realms/srw/clients/"
                    + str(uuid4())
                },
            )
        if request.method == "GET" and request.url.path.endswith("/roles/admin"):
            return httpx.Response(200, json={"id": str(uuid4()), "name": "admin"})
        return httpx.Response(204)

    monkeypatch.setattr(smoke_module, "expires_soon", lambda headers: False)
    monkeypatch.setattr(
        smoke_module,
        "login",
        lambda *args, **kwargs: {"Authorization": "Bearer test-only"},
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        admin = smoke_module.TemporaryAdmin(client, name)
        admin.create()
        assert admin.uid == uid
        assert admin.app_uid == app_uid
        admin.revoke()
    assert admin.cleanup_evidence["identityDeleted"] is True


def test_unknown_create_without_attributes_requires_the_new_random_password(
    smoke_module, monkeypatch
):
    import base64

    uid = str(uuid4())
    name = "srw-manifest-admin-unknown"
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": uid}).encode()).decode().rstrip("=")
    )
    logins = []

    def login(client, username, password, **kwargs):
        logins.append((username, password))
        return {"Authorization": "Bearer test." + payload + ".test"}

    def respond(request):
        return httpx.Response(
            200,
            json=[{"id": uid, "username": name, "email": name + "@example.invalid"}],
        )

    monkeypatch.setattr(smoke_module, "expires_soon", lambda headers: False)
    monkeypatch.setattr(smoke_module, "login", login)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        admin = smoke_module.TemporaryAdmin(client, name)
        admin.created_intent = True
        admin.password = "test-only-new-password"
        admin.recover_identity()
    assert admin.uid == uid
    assert logins == [(name, "test-only-new-password")]


@pytest.mark.asyncio
async def test_exact_default_project_cleanup_uses_real_retirement_after_user_delete(
    smoke_module, database
):
    user, project = await database.create_user_with_default_project(
        "Unique smoke fixture"
    )
    uid = str(user["id"])
    receipt = await smoke_module.owned_default_project(database, uid)
    assert receipt["projectID"] == str(project["id"])
    with pytest.raises(RuntimeError, match="removed fixture account"):
        await smoke_module.owned_default_project(database, uid, receipt)
    assert await database.delete_user(uid)
    assert await smoke_module.owned_default_project(database, uid, receipt) == {
        "removed": True
    }
    assert not await database.fetchval(
        "SELECT EXISTS(SELECT 1 FROM projects WHERE id=$1)", project["id"]
    )
    assert await smoke_module.owned_default_project(database, uid, receipt) == {
        "removed": True
    }


@pytest.mark.asyncio
async def test_default_project_cleanup_refuses_other_members_and_forged_receipt(
    smoke_module, database
):
    user, project = await database.create_user_with_default_project(
        "Unique smoke fixture"
    )
    uid = str(user["id"])
    receipt = await smoke_module.owned_default_project(database, uid)
    outsider, _ = await database.create_user_with_default_project("Unrelated account")
    await database.add_project_member(str(project["id"]), str(outsider["id"]), "viewer")
    with pytest.raises(RuntimeError, match="exclusively own"):
        await smoke_module.owned_default_project(database, uid)
    await database.delete_user(uid)
    with pytest.raises(RuntimeError, match="acquired a member"):
        await smoke_module.owned_default_project(database, uid, receipt)
    with pytest.raises(RuntimeError, match="identity changed"):
        await smoke_module.owned_default_project(
            database, uid, {**receipt, "createdAt": "forged"}
        )
    assert await database.fetchval(
        "SELECT EXISTS(SELECT 1 FROM projects WHERE id=$1)", project["id"]
    )


def test_fixture_custom_chat_model_is_exclusive_and_unknown_ids_are_redacted():
    # Separate process proves actual startup-time configuration without changing
    # the module constants consumed by the established fixture test suite.
    code = """
import asyncio,json
import httpx
from tests.e2e.app.deterministic_provider.provider import ScenarioStore,ArmScenarioRequest,create_inference_app
async def check():
    store=ScenarioStore()
    await store.arm('custom-model-gate',ArmScenarioRequest(scenario='reply'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_inference_app(store,inference_api_key='test-fixture-key')),base_url='http://fixture',headers={'Authorization':'Bearer test-fixture-key'}) as client:
        models=(await client.get('/v1/models')).json()
        assert models['data'][0]['id']=='zz-srw-manifest-test'
        bad=await client.post('/v1/chat/completions',json={'model':'private-unknown-model','messages':[{'role':'user','content':'E2E-custom-model-gate'}]})
        assert bad.status_code==400
        good=await client.post('/v1/chat/completions',json={'model':'zz-srw-manifest-test','messages':[{'role':'user','content':'E2E-custom-model-gate'}]})
        assert good.status_code==200
        state=await store.state('custom-model-gate')
        assert 'private-unknown-model' not in json.dumps(state)
        assert state['unexpected_count']==1
        print(json.dumps({'customModelAccepted':True,'unknownModelRejected':True}))
asyncio.run(check())
"""
    repo_root = Path(__file__).resolve().parents[1]
    # `python -c` only reaches the fixture package through the current
    # directory, which the CI runner does not put on the child's path. Name the
    # repo root explicitly so the import does not depend on that default.
    environment = {**os.environ, "E2E_CHAT_MODEL_ID": "zz-srw-manifest-test"}
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(repo_root),
            *([environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else []),
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, "Configured deterministic provider contract failed"
    assert json.loads(result.stdout) == {
        "customModelAccepted": True,
        "unknownModelRejected": True,
    }
