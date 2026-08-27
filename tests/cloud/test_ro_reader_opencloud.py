"""Tests for OpenCloudBackend RO-reader provisioning (protected cloud mode).

Asserts the provisioning LOGIC against an httpx.MockTransport fake: a reader
LibreGraph user, a per-mount Space **Viewer** (weight 40, "Can view") invite —
NOT the editor role — captured as the revoke handle, and clean permission
delete. OpenCloud is code-read-only/unverified-live (design §11.7) and reader
KC-machine-user creation is the §9.2 open question; live validation is the
§11.4 manual gate.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from orchestrator.services.cloud import OpenCloudBackend, ProjectFolderHandle
from orchestrator.services.cloud.config import OpenCloudSettings

KC_BASE = "https://kc.example.com/realms/srw"
OC_BASE = "https://cloud.example.com"
DRIVE_ID = "drive-proj-1"


def _settings() -> OpenCloudSettings:
    return OpenCloudSettings(
        backend_id="opencloud",
        base_url=OC_BASE,
        public_url=OC_BASE,
        keycloak_issuer=KC_BASE,
        keycloak_client_id="opencloud-orchestrator",
        keycloak_client_secret=SecretStr("test-secret"),
        admin_role_claim_value="opencloudAdmin",
        default_quota_bytes=0,
    )


def _handle() -> ProjectFolderHandle:
    return ProjectFolderHandle(
        backend="opencloud", native_id=DRIVE_ID, vendor_meta={"mountpoint": "Proj"}
    )


def _json(s: str) -> dict:
    return json.loads(s)


class FakeOc:
    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.invites: list[dict] = []
        self.deleted_permissions: list[str] = []
        self.files: dict[str, bytes] = {}
        self.next_id = 1
        self.role_catalog = [
            {"id": "editor-role", "displayName": "Can edit", "@libre.graph.weight": 90},
            {"id": "viewer-role", "displayName": "Can view", "@libre.graph.weight": 40},
        ]

    def _new(self, prefix: str) -> str:
        v = f"{prefix}{self.next_id}"
        self.next_id += 1
        return v

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        method = request.method
        path = url.path
        if url.host == "kc.example.com":
            return httpx.Response(
                200,
                json={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
            )
        if "/dav/spaces/" in str(url):
            if method == "MKCOL":
                return httpx.Response(201)
            if method == "PUT":
                self.files[path] = bytes(request.content)
                return httpx.Response(201)
            if method == "DELETE":
                self.files.pop(path, None)
                return httpx.Response(204)
            return httpx.Response(500)

        if (
            method == "GET"
            and path == "/graph/v1beta1/roleManagement/permissions/roleDefinitions"
        ):
            return httpx.Response(200, json=list(self.role_catalog))
        if method == "GET" and path == "/graph/v1.0/users":
            return httpx.Response(200, json={"value": list(self.users.values())})
        if method == "POST" and path == "/graph/v1.0/users":
            body = json.loads(request.content)
            # Real oCIS REQUIRES onPremisesSamAccountName — a create without it
            # 400s ("no value given for required property ...", live dev-cluster
            # validation 2026-07-10). Mirror that so the fake is faithful.
            if not body.get("onPremisesSamAccountName"):
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": "invalidRequest",
                            "message": "no value given for required property onPremisesSamAccountName",
                        }
                    },
                )
            uid = self._new("user-")
            self.users[uid] = {"id": uid, "displayName": body["displayName"]}
            return httpx.Response(201, json=self.users[uid])
        if method == "POST" and path.endswith("/root/invite"):
            body = json.loads(request.content)
            perm_id = self._new("perm-")
            self.invites.append(
                {
                    "drive": path.split("/drives/")[1].split("/")[0],
                    "recipients": body["recipients"],
                    "roles": body["roles"],
                    "permission_id": perm_id,
                }
            )
            return httpx.Response(200, json={"value": [{"id": perm_id}]})
        if method == "DELETE" and "/permissions/" in path:
            self.deleted_permissions.append(path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        return httpx.Response(500, content=f"unhandled {method} {path}".encode())


def _oc_backend_with_fake():
    backend = OpenCloudBackend(_settings())
    fake = FakeOc()
    backend._client = httpx.AsyncClient(
        base_url=OC_BASE, transport=httpx.MockTransport(fake.handler)
    )
    backend._initialized = True
    # Pre-seed the role cache with the correct tuple keys so _role_id hits the
    # early-return path (the refresh path has a latent bug, opencloud.py:1610).
    backend._role_cache = {
        ("Can edit", 90): "editor-role",
        ("Can view", 40): "viewer-role",
    }
    return backend, fake


@pytest.mark.asyncio
async def test_ensure_ro_reader_creates_libregraph_user():
    backend, fake = _oc_backend_with_fake()
    reader_id = await backend.ensure_ro_reader(user_key="abc")
    assert reader_id in fake.users
    assert fake.users[reader_id]["displayName"] == "srw-reader-abc"


@pytest.mark.asyncio
async def test_ensure_ro_reader_is_idempotent():
    backend, fake = _oc_backend_with_fake()
    first = await backend.ensure_ro_reader(user_key="abc")
    second = await backend.ensure_ro_reader(user_key="abc")  # resolves existing
    assert first == second
    assert len(fake.users) == 1


@pytest.mark.asyncio
async def test_mint_grant_uses_viewer_role_not_editor():
    backend, fake = _oc_backend_with_fake()
    await backend.ensure_ro_reader(user_key="abc")
    grant = await backend.mint_ro_grant(_handle(), user_key="abc", grant_key="thread-1")
    invite = fake.invites[-1]
    assert invite["roles"] == ["viewer-role"]  # Viewer, NOT editor
    assert invite["recipients"][0]["@libre.graph.recipient.type"] == "user"
    assert grant.credentials is None  # OC uses a short-TTL bearer, no stored cred
    assert grant.auth_kind == "keycloak_user_impersonation"
    assert f"/dav/spaces/{DRIVE_ID}/" in grant.webdav_url
    assert _json(grant.grant_handle)["permission_id"] == invite["permission_id"]


@pytest.mark.asyncio
async def test_revoke_grant_deletes_the_permission():
    backend, fake = _oc_backend_with_fake()
    await backend.ensure_ro_reader(user_key="abc")
    grant = await backend.mint_ro_grant(_handle(), user_key="abc", grant_key="thread-1")
    await backend.revoke_ro_grant(grant.grant_handle, user_key="abc")
    assert _json(grant.grant_handle)["permission_id"] in fake.deleted_permissions


@pytest.mark.asyncio
async def test_canary_seed_and_remove_roundtrip():
    backend, fake = _oc_backend_with_fake()
    fixture = await backend.seed_canary_fixture(_handle())
    assert any(fixture.path.split("/")[-1] in p for p in fake.files)
    await backend.remove_canary_fixture(_handle(), fixture)
