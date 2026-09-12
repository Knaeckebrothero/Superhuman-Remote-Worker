"""OCI manifest resolution for operator-approved VM preparation registries."""

import asyncio
import hashlib
import json
import re
from urllib.parse import urlsplit

import httpx

from shared.workspace_preparation import ARCHITECTURE, image_reference

ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class RegistryResolutionError(ValueError):
    pass


class RegistryResolver:
    def __init__(self, *, hosts, insecure_hosts=(), token_hosts=(), transport=None):
        self.hosts = frozenset(hosts)
        self.insecure_hosts = frozenset(insecure_hosts)
        self.token_hosts = frozenset(token_hosts)
        self.transport = transport
        if not self.insecure_hosts <= self.hosts:
            raise ValueError("Insecure registry hosts must be explicitly allowed.")

    def permitted(self, image):
        host, _, _ = image_reference(image)
        if host not in self.hosts:
            raise RegistryResolutionError(
                "Image registry is not enabled for preparation."
            )

    async def _response(self, client, url, *, headers=None, params=None):
        try:
            async with asyncio.timeout(20):
                async with client.stream(
                    "GET", url, headers=headers, params=params
                ) as response:
                    content = bytearray()
                    async for block in response.aiter_bytes():
                        content.extend(block)
                        if len(content) > 1024 * 1024:
                            raise RegistryResolutionError(
                                "Registry response exceeds its size limit."
                            )
                    return response.status_code, response.headers, bytes(content)
        except TimeoutError:
            raise RegistryResolutionError(
                "Registry response deadline exceeded."
            ) from None

    async def resolve(self, image):
        self.permitted(image)
        host, repository, reference = image_reference(image)
        endpoint = "registry-1.docker.io" if host == "docker.io" else host
        scheme = "http" if host in self.insecure_hosts else "https"
        url = f"{scheme}://{endpoint}/v2/{repository}/manifests/"
        headers = {"Accept": ACCEPT}
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        ) as client:
            code, response_headers, content = await self._response(
                client, url + reference, headers=headers
            )
            if code == 401:
                challenge = response_headers.get("WWW-Authenticate", "")
                if not challenge.lower().startswith("bearer "):
                    raise RegistryResolutionError(
                        "Registry requires unsupported preparation authentication."
                    )
                fields = dict(re.findall(r'(\w+)="([^"\r\n]*)"', challenge[7:]))
                realm = fields.get("realm", "")
                parsed = urlsplit(realm)
                if (
                    parsed.scheme != "https"
                    or parsed.netloc not in self.token_hosts
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise RegistryResolutionError(
                        "Registry token endpoint is not enabled."
                    )
                code, _, token_body = await self._response(
                    client,
                    realm,
                    params={
                        "service": fields.get("service", endpoint),
                        "scope": f"repository:{repository}:pull",
                    },
                )
                if code != 200:
                    raise RegistryResolutionError("Registry token request failed.")
                token_data = json.loads(token_body)
                if not isinstance(token_data, dict):
                    raise RegistryResolutionError("Registry returned an invalid token.")
                token = token_data.get("token") or token_data.get("access_token")
                if (
                    not isinstance(token, str)
                    or not 1 <= len(token) <= 16384
                    or "\n" in token
                    or "\r" in token
                ):
                    raise RegistryResolutionError("Registry returned an invalid token.")
                headers["Authorization"] = "Bearer " + token
                code, response_headers, content = await self._response(
                    client, url + reference, headers=headers
                )
            digest, manifest = self._manifest(
                code, response_headers, content, reference
            )
            if "manifests" in manifest:
                if not isinstance(manifest["manifests"], list) or any(
                    not isinstance(m, dict)
                    or not isinstance(m.get("platform", {}), dict)
                    for m in manifest["manifests"]
                ):
                    raise RegistryResolutionError(
                        "Registry returned an invalid image index."
                    )
                candidates = [
                    m
                    for m in manifest["manifests"]
                    if m.get("platform", {}).get("os") == "linux"
                    and m.get("platform", {}).get("architecture") == ARCHITECTURE
                    and not m.get("platform", {}).get("variant")
                ]
                if len(candidates) != 1:
                    raise RegistryResolutionError(
                        "Base image has no unambiguous linux/amd64 manifest."
                    )
                reference = candidates[0].get("digest", "")
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", reference):
                    raise RegistryResolutionError("Invalid platform image digest.")
                code, response_headers, content = await self._response(
                    client, url + reference, headers=headers
                )
                digest, manifest = self._manifest(
                    code, response_headers, content, reference
                )
            if manifest.get("schemaVersion") != 2 or not isinstance(
                manifest.get("layers"), list
            ):
                raise RegistryResolutionError(
                    "Registry did not return an OCI image manifest."
                )
            return f"{host}/{repository}@{digest}"

    @staticmethod
    def _manifest(code, headers, content, reference):
        if code != 200:
            raise RegistryResolutionError(
                f"Image manifest resolution failed (HTTP {code})."
            )
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if headers.get("Docker-Content-Digest", digest) != digest or (
            reference.startswith("sha256:") and reference != digest
        ):
            raise RegistryResolutionError(
                "Registry manifest digest verification failed."
            )
        try:
            manifest = json.loads(content)
        except (ValueError, TypeError) as exc:
            raise RegistryResolutionError(
                "Registry returned an invalid manifest."
            ) from exc
        if not isinstance(manifest, dict):
            raise RegistryResolutionError("Registry returned an invalid manifest.")
        return digest, manifest
