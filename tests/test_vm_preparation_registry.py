import hashlib
import json

import httpx
import pytest

from vm_controller.preparation_registry import RegistryResolver, RegistryResolutionError


def manifest():
    return json.dumps({"schemaVersion": 2, "layers": [], "config": {}}).encode()


@pytest.mark.asyncio
async def test_digest_is_computed_and_verified_against_both_reference_and_header():
    payload = manifest()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    resolver = RegistryResolver(
        hosts=["registry.example"],
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=payload, headers={"Docker-Content-Digest": digest}
            )
        ),
    )
    assert (
        await resolver.resolve("registry.example/team/base:tag")
        == "registry.example/team/base@" + digest
    )
    with pytest.raises(RegistryResolutionError, match="verification"):
        await resolver.resolve("registry.example/team/base@sha256:" + "0" * 64)


@pytest.mark.asyncio
async def test_registry_digest_header_cannot_override_received_content():
    resolver = RegistryResolver(
        hosts=["registry.example"],
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=manifest(),
                headers={"Docker-Content-Digest": "sha256:" + "0" * 64},
            )
        ),
    )
    with pytest.raises(RegistryResolutionError, match="verification"):
        await resolver.resolve("registry.example/team/base:tag")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "realm",
    [
        "http://tokens.example/token",
        "https://169.254.169.254/token",
        "https://user:pass@tokens.example/token",
        "https://tokens.example/token?scope=admin",
    ],
)
async def test_registry_challenge_cannot_redirect_token_requests_to_other_services(
    realm,
):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            401, headers={"WWW-Authenticate": f'Bearer realm="{realm}"'}
        )

    resolver = RegistryResolver(
        hosts=["registry.example"],
        token_hosts=["tokens.example"],
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RegistryResolutionError):
        await resolver.resolve("registry.example/team/base:tag")
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_token_scope_is_constructed_from_requested_repository():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "tokens.example":
            assert request.url.params["scope"] == "repository:team/base:pull"
            return httpx.Response(200, json={"token": "fixture-token"})
        if request.headers.get("Authorization") == "Bearer fixture-token":
            return httpx.Response(200, content=manifest())
        return httpx.Response(
            401,
            headers={
                "WWW-Authenticate": 'Bearer realm="https://tokens.example/token",service="registry.example",scope="repository:private/admin:push"'
            },
        )

    resolver = RegistryResolver(
        hosts=["registry.example"],
        token_hosts=["tokens.example"],
        transport=httpx.MockTransport(handler),
    )
    assert "@sha256:" in await resolver.resolve("registry.example/team/base:tag")
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_multiarchitecture_image_resolves_the_verified_amd64_child():
    child = manifest()
    digest = "sha256:" + hashlib.sha256(child).hexdigest()
    index = {
        "schemaVersion": 2,
        "manifests": [
            {"digest": digest, "platform": {"os": "linux", "architecture": "amd64"}},
            {
                "digest": "sha256:" + "0" * 64,
                "platform": {"os": "linux", "architecture": "arm64"},
            },
        ],
    }

    def handler(request):
        return (
            httpx.Response(200, content=child)
            if request.url.path.endswith(digest)
            else httpx.Response(200, json=index)
        )

    resolver = RegistryResolver(
        hosts=["registry.example"], transport=httpx.MockTransport(handler)
    )
    assert (
        await resolver.resolve("registry.example/team/base:tag")
        == "registry.example/team/base@" + digest
    )


@pytest.mark.asyncio
async def test_unapproved_registry_fails_without_a_network_request():
    def handler(request):
        pytest.fail("Unapproved registry was contacted")

    resolver = RegistryResolver(
        hosts=["registry.example"], transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RegistryResolutionError, match="not enabled"):
        await resolver.resolve("internal.example/service:latest")


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [None, {}, [None], [{"platform": None}]])
async def test_malformed_index_is_a_resolution_failure(index):
    resolver = RegistryResolver(
        hosts=["registry.example"],
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"schemaVersion": 2, "manifests": index})
        ),
    )
    with pytest.raises(RegistryResolutionError, match="index"):
        await resolver.resolve("registry.example/base:v1")
