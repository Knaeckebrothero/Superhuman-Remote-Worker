"""Native manifest requests from the shared client, CLI and MCP use one API."""

import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys

import httpx
import pytest

from orchestrator.operator_cli import manifest_resources as cli
from shared.orch_surface.client import AsyncCockpitClient, MutationOutcomeUnknown
from shared.runtime_actor import RUNTIME_ACTOR_HEADER, RUNTIME_ACTOR_REFRESH_HEADER


UID = "11111111-1111-4111-8111-111111111111"
REVISION = "sha256:" + "a" * 64
DOCUMENT = {
    "apiVersion": "srw/v1alpha1",
    "kind": "Expert",
    "metadata": {"name": "custom", "scope": {"kind": "Account", "name": "me"}},
    "spec": {
        "runtime": {
            "image": "example.invalid/harness:v1",
            "config": {"tools": ["unknown"], "keep": None},
        }
    },
}
RECORD = {"resource": DOCUMENT, "uid": UID, "resourceVersion": 3, "revision": REVISION}


@pytest.mark.asyncio
async def test_native_methods_forward_source_scopes_versions_and_plan_without_private_normalization():
    captured = []

    async def handler(request):
        captured.append(
            (
                request.method,
                request.url.path,
                dict(request.url.params),
                json.loads(request.content) if request.content else None,
            )
        )
        return httpx.Response(
            200,
            json=RECORD
            if request.method == "GET" and request.url.path.endswith(UID)
            else {"resources": [], "source": "exported"},
        )

    source = json.dumps(DOCUMENT)
    scope = {"kind": "Project", "name": UID}
    versions = {f"Expert/Project/{UID}/custom": 3}
    async with AsyncCockpitClient(
        "http://orchestrator.test", transport=httpx.MockTransport(handler)
    ) as client:
        await client.manifest_validate(source, format="json")
        await client.manifest_preview(source, format="json", default_scope=scope)
        await client.manifest_apply(
            source,
            format="json",
            default_scope=scope,
            expected_versions=versions,
            plan_revision=REVISION,
            idempotency_key="stable-operation",
        )
        await client.list_manifest_resources(
            scope_kind="Project", scope_name=UID, kind="Expert"
        )
        await client.get_manifest_resource(UID)
        await client.export_manifest_resource(UID, output_format="json")
        await client.delete_manifest_resource(UID, expected_version=3)
    assert captured[0] == (
        "POST",
        "/api/manifests/validate",
        {},
        {"source": source, "format": "json"},
    )
    assert captured[1][3] == {
        "source": source,
        "format": "json",
        "default_scope": scope,
        "resolution": "stored",
    }
    assert captured[2] == (
        "POST",
        "/api/manifests/apply",
        {},
        {
            "source": source,
            "format": "json",
            "default_scope": scope,
            "expected_versions": versions,
            "plan_revision": REVISION,
            "idempotency_key": "stable-operation",
        },
    )
    assert captured[3][2] == {
        "scope_kind": "Project",
        "scope_name": UID,
        "kind": "Expert",
    }
    assert captured[6][:2] == ("POST", "/api/manifests/export")
    assert json.loads(captured[6][3]["source"]) == DOCUMENT
    assert captured[7] == (
        "DELETE",
        f"/api/resources/{UID}",
        {"expected_version": "3"},
        None,
    )


@pytest.mark.asyncio
async def test_bearer_mode_discards_ambient_and_invocation_internal_authority(
    monkeypatch,
):
    monkeypatch.setenv("MCP_INTERNAL_KEY", "ambient-internal-fixture")
    recorded = []

    async def handler(request):
        recorded.append(dict(request.headers))
        return httpx.Response(200, json={"resources": []})

    names = (
        "X-Internal-Key",
        "X-MCP-User-Id",
        "X-MCP-Scope",
        RUNTIME_ACTOR_HEADER,
        RUNTIME_ACTOR_REFRESH_HEADER,
    )
    async with AsyncCockpitClient(
        "http://orchestrator.test",
        bearer_token="explicit-user-fixture",
        transport=httpx.MockTransport(handler),
    ) as client:
        client._client.headers.update({name: "pollution-fixture" for name in names})
        with client.invocation_scope(
            user_id="other-user",
            scope="project:other",
            runtime_actor_refresh="other-authority",
        ):
            await client.list_manifest_resources()
        await client.list_manifest_resources()
    for headers in recorded:
        assert headers["authorization"] == "Bearer explicit-user-fixture"
        assert not any(name.lower() in headers for name in names)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["apply", "delete"])
async def test_manifest_mutations_never_retry_an_ambiguous_response(operation):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("lost response", request=request)

    async with AsyncCockpitClient(
        "http://orchestrator.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(MutationOutcomeUnknown):
            if operation == "apply":
                await client.manifest_apply(json.dumps(DOCUMENT), format="json")
            else:
                await client.delete_manifest_resource(UID, expected_version=3)
    assert calls == 1


@pytest.mark.asyncio
async def test_preview_timeout_is_a_read_failure_without_mutation_claim():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("preview timeout", request=request)

    async with AsyncCockpitClient(
        "http://orchestrator.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.manifest_preview(json.dumps(DOCUMENT), format="json")
    assert calls == 1


@pytest.mark.asyncio
async def test_invalid_resource_identity_or_version_cannot_change_url_scope():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    async with AsyncCockpitClient(
        "http://orchestrator.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError):
            await client.get_manifest_resource("../secrets")
        with pytest.raises(ValueError):
            await client.delete_manifest_resource(UID, expected_version=True)
        with pytest.raises(ValueError):
            await client.delete_manifest_resource(UID, expected_version=0)
    assert requests == []


@pytest.fixture
def cli_transport(monkeypatch):
    monkeypatch.setenv("SRW_API_URL", "https://orchestrator.test")
    monkeypatch.setenv("SRW_TOKEN", "private-auth-fixture")
    monkeypatch.setenv("MCP_INTERNAL_KEY", "unrelated-internal-fixture")
    calls = []
    state = {"status": 200, "result": {"resources": [RECORD]}, "error": None}

    async def handler(request):
        calls.append(request)
        if state["error"]:
            raise state["error"]("transport failure", request=request)
        return httpx.Response(state["status"], json=state["result"])

    def factory(url, **kwargs):
        assert isinstance(kwargs["verify"], ssl.SSLContext)
        assert kwargs["trust_env"] is False
        return AsyncCockpitClient(url, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli, "AsyncCockpitClient", factory)
    return calls, state


def test_cli_applies_reviewed_plan_and_observed_versions_from_real_files(
    cli_transport, tmp_path, capsys
):
    calls, _ = cli_transport
    source = tmp_path / "expert.json"
    source.write_text(json.dumps(DOCUMENT))
    reviewed = tmp_path / "plan.json"
    reviewed.write_text(
        json.dumps(
            {"operation": "preview", "resolution": "stored", "planRevision": REVISION}
        )
    )
    versions = tmp_path / "versions.json"
    versions.write_text(json.dumps({"Expert/Account/me/custom": 3}))
    assert (
        cli.main(
            [
                "apply",
                "-f",
                str(source),
                "--plan",
                str(reviewed),
                "--expected-versions",
                str(versions),
                "--idempotency-key",
                "apply-fixture",
            ]
        )
        == 0
    )
    assert len(calls) == 1
    request = calls[0]
    body = json.loads(request.content)
    assert body["plan_revision"] == REVISION
    assert body["expected_versions"] == {"Expert/Account/me/custom": 3}
    assert body["idempotency_key"] == "apply-fixture"
    assert json.loads(body["source"]) == [DOCUMENT]
    output = capsys.readouterr()
    assert json.loads(output.out)["resources"][0]["uid"] == UID
    assert "private-auth-fixture" not in output.out + output.err
    assert request.headers["authorization"] == "Bearer private-auth-fixture"
    assert "x-internal-key" not in request.headers


def test_cli_combines_json_and_yaml_before_stored_preview(
    cli_transport, tmp_path, capsys
):
    calls, _ = cli_transport
    first = tmp_path / "first.json"
    first.write_text(json.dumps(DOCUMENT))
    second = tmp_path / "second.yaml"
    second.write_text(
        "apiVersion: srw/v1alpha1\nkind: Connector\nmetadata: {name: source}\nspec: {driver: environment, config: {literal: null}}\n"
    )
    assert (
        cli.main(
            [
                "preview",
                "-f",
                str(first),
                "-f",
                str(second),
                "--scope-kind",
                "Project",
                "--scope-name",
                UID,
            ]
        )
        == 0
    )
    body = json.loads(calls[0].content)
    assert body["format"] == "json"
    assert body["resolution"] == "stored"
    assert body["default_scope"] == {"kind": "Project", "name": UID}
    assert len(json.loads(body["source"])) == 2
    assert json.loads(body["source"])[1]["spec"]["config"]["literal"] is None
    capsys.readouterr()


def test_cli_get_filters_and_delete_precondition(cli_transport, capsys):
    calls, _ = cli_transport
    assert (
        cli.main(
            ["get", "--kind", "Job", "--scope-kind", "Project", "--scope-name", UID]
        )
        == 0
    )
    assert dict(calls[-1].url.params) == {
        "kind": "Job",
        "scope_kind": "Project",
        "scope_name": UID,
    }
    assert cli.main(["delete", UID, "--expected-version", "3"]) == 0
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.params["expected_version"] == "3"
    capsys.readouterr()


def test_cli_exports_authored_private_json_without_observed_fields(
    cli_transport, monkeypatch, capsys
):
    calls, state = cli_transport
    state["result"] = RECORD
    original = cli.AsyncCockpitClient

    def factory(url, **kwargs):
        client = original(url, **kwargs)

        async def export(source, **options):
            assert json.loads(source) == DOCUMENT
            assert options == {"format": "json", "output_format": "json"}
            return {"format": "json", "source": json.dumps(DOCUMENT)}

        client.manifest_export = export
        return client

    monkeypatch.setattr(cli, "AsyncCockpitClient", factory)
    assert cli.main(["export", UID, "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == DOCUMENT
    assert len(calls) == 1


def test_cli_token_stdin_is_exclusive_and_not_printed(
    cli_transport, monkeypatch, capsys
):
    calls, _ = cli_transport
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-auth-fixture\n"))
    assert cli.main(["--token-stdin", "get"]) == 0
    assert calls[0].headers["authorization"] == "Bearer stdin-auth-fixture"
    output = capsys.readouterr()
    assert "stdin-auth-fixture" not in output.out + output.err
    assert cli.main(["--token-stdin", "apply", "-f", "-"]) == 1
    assert len(calls) == 1
    assert "cannot be used together" in capsys.readouterr().err


def test_cli_requires_explicit_user_auth_and_never_uses_internal_key_fallback(
    cli_transport, monkeypatch, capsys
):
    calls, _ = cli_transport
    monkeypatch.delenv("SRW_TOKEN")
    assert cli.main(["get"]) == 1
    assert calls == []
    assert "SRW_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["get", "--scope-kind", "Project"],
        ["get", UID, "--kind", "Expert"],
        ["delete", UID, "--expected-version", "0"],
    ],
)
def test_cli_rejects_invalid_filters_and_versions_before_any_request(
    cli_transport, capsys, arguments
):
    calls, _ = cli_transport
    assert cli.main(arguments) == 1
    assert calls == []
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "InvalidInput"


def test_cli_unknown_mutation_is_one_attempt_and_reflected_auth_is_redacted(
    cli_transport, tmp_path, capsys
):
    calls, state = cli_transport
    source = tmp_path / "resource.json"
    source.write_text(json.dumps(DOCUMENT))
    state["error"] = httpx.ReadTimeout
    assert cli.main(["apply", "-f", str(source)]) == 1
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "OutcomeUnknown"
    state.update(
        error=None,
        status=409,
        result={
            "detail": {
                "code": "VersionConflict",
                "message": "reflected private-auth-fixture",
            }
        },
    )
    assert cli.main(["apply", "-f", str(source)]) == 1
    diagnostic = capsys.readouterr().err
    assert "private-auth-fixture" not in diagnostic
    assert json.loads(diagnostic)["error"]["code"] == "VersionConflict"


def test_cli_does_not_follow_an_authenticated_redirect(cli_transport, capsys):
    calls, state = cli_transport
    state.update(status=302, result={})
    assert cli.main(["get"]) == 1
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().err)["error"]["status"] == 302


def test_cli_rejects_bundle_only_or_missing_plan_revision(
    cli_transport, tmp_path, capsys
):
    calls, _ = cli_transport
    source = tmp_path / "expert.json"
    source.write_text(json.dumps(DOCUMENT))
    reviewed = tmp_path / "plan.json"
    for result in (
        {"operation": "preview", "resolution": "bundle"},
        {"operation": "preview", "resolution": "stored"},
    ):
        reviewed.write_text(json.dumps(result))
        assert cli.main(["apply", "-f", str(source), "--plan", str(reviewed)]) == 1
        assert calls == []
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "InvalidInput"


def test_registered_mcp_manifest_mutation_uses_scoped_identity_and_fails_closed():
    script = r"""
import asyncio, json
import httpx
from mcp_server import server
from shared.orch_surface.client import AsyncCockpitClient
from shared.orch_surface.jobs import CallerCtx, AUTH_CONTEXT_FAILURE_NOTICE

async def main():
    captured = []
    async def handler(request):
        captured.append((request.headers.get('X-MCP-User-Id'), request.headers.get('X-MCP-Scope'), request.headers.get('X-Internal-Key'), json.loads(request.content)))
        return httpx.Response(200, json={'resources': []}) if request.headers.get('X-MCP-User-Id') else httpx.Response(401, json={'detail': 'Unauthenticated'})
    async with AsyncCockpitClient('http://orchestrator.test', transport=httpx.MockTransport(handler)) as client:
        server._get_client = lambda: client
        server._get_mcp_caller_ctx = lambda: CallerCtx(kind='mcp', user_id='caller-fixture', project_ids=('11111111-1111-4111-8111-111111111111',))
        response = await server.manifest_apply(source='{"opaque":null}', format='json', scope_kind='Project', scope_name='11111111-1111-4111-8111-111111111111', expected_versions={'fixture': 3}, idempotency_key='request-fixture')
        assert json.loads(response) == {'resources': []}
        assert captured[0][:3] == ('caller-fixture', 'project:11111111-1111-4111-8111-111111111111', 'internal-fixture')
        assert captured[0][3]['source'] == '{"opaque":null}'
        assert captured[0][3]['expected_versions'] == {'fixture': 3}
        server._get_mcp_caller_ctx = lambda: CallerCtx(kind='mcp', auth_failed=True)
        denied = await server.manifest_apply(source='{}', format='json')
        assert denied.startswith(AUTH_CONTEXT_FAILURE_NOTICE)
        assert captured[1][:3] == (None, None, None)
    print('manifest MCP scope checks passed')
asyncio.run(main())
"""
    environment = {
        **os.environ,
        "MCP_TRANSPORT": "stdio",
        "MCP_INTERNAL_KEY": "internal-fixture",
    }
    checked = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == "manifest MCP scope checks passed"
