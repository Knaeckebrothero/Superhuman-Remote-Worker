"""Tests for NextcloudBackend RO-reader provisioning (protected cloud mode).

Stands the backend up against an httpx.MockTransport fake that speaks the OCS
user/group admin endpoints, the groupfolders group-ACL endpoints, and the
WebDAV groupfolders path (for the canary). Asserts the provisioning LOGIC —
a low-priv reader account, a per-mount READ-only (permission=1) grant, a
rotated credential, and clean revoke — not live NC status-code semantics
(those are the §11.4 live-validation concern).
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, quote, unquote

import httpx
import pytest

from orchestrator.services.cloud import NextcloudBackend, ProjectFolderHandle
from orchestrator.services.cloud.config import NextcloudSettings

NC_BASE = "https://nc.example.com"
AGENT_USER = "agent-service"
MOUNTPOINT = "Test Project"
FOLDER_ID = "7"


def _settings() -> NextcloudSettings:
    return NextcloudSettings(
        base_url=NC_BASE,
        public_url=NC_BASE,
        admin_user="admin",
        admin_password="admin",
        agent_user=AGENT_USER,
        agent_password="agent-service-dev",
    )


def _handle() -> ProjectFolderHandle:
    return ProjectFolderHandle(
        backend="nextcloud",
        native_id=FOLDER_ID,
        vendor_meta={"mountpoint": MOUNTPOINT},
    )


def _json(s: str) -> dict:
    return json.loads(s)


class FakeNcOcs:
    """In-memory OCS + groupfolders + WebDAV stand-in for reader provisioning."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.groups: set[str] = set()
        # folder_id -> {group_id: permissions}
        self.folder_group_perms: dict[str, dict[str, int]] = {}
        self.files: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []

    def _ocs(self, statuscode: int, extra: dict | None = None) -> httpx.Response:
        body = {"ocs": {"meta": {"status": "ok", "statuscode": statuscode}}}
        if extra:
            body["ocs"].update(extra)
        return httpx.Response(200, json=body)

    def _form(self, request: httpx.Request) -> dict[str, str]:
        parsed = parse_qs(request.content.decode() or "")
        return {k: v[0] for k, v in parsed.items()}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = unquote(request.url.path)
        method = request.method

        # --- OCS users ---
        if path == "/ocs/v2.php/cloud/users" and method == "POST":
            uid = self._form(request).get("userid", "")
            if uid in self.users:
                return self._ocs(102)  # already exists
            self.users[uid] = {"groups": [], "password": self._form(request).get("password")}
            return self._ocs(100)

        m = re.fullmatch(r"/ocs/v2\.php/cloud/users/([^/]+)", path)
        if m and method == "PUT":
            uid = m.group(1)
            form = self._form(request)
            if form.get("key") == "password":
                self.users.setdefault(uid, {"groups": []})["password"] = form.get("value")
            return self._ocs(100)

        m = re.fullmatch(r"/ocs/v2\.php/cloud/users/([^/]+)/groups", path)
        if m and method == "POST":
            uid, gid = m.group(1), self._form(request).get("groupid", "")
            self.users.setdefault(uid, {"groups": []})["groups"].append(gid)
            return self._ocs(100)
        if m and method == "DELETE":
            uid, gid = m.group(1), self._form(request).get("groupid", "")
            groups = self.users.get(uid, {}).get("groups", [])
            if gid in groups:
                groups.remove(gid)
            return self._ocs(100)

        # --- OCS groups ---
        if path == "/ocs/v2.php/cloud/groups" and method == "POST":
            gid = self._form(request).get("groupid", "")
            self.groups.add(gid)
            return httpx.Response(200, json={"ocs": {"meta": {"statuscode": 200}}})

        m = re.fullmatch(r"/ocs/v2\.php/cloud/groups/([^/]+)", path)
        if m and method == "DELETE":
            self.groups.discard(m.group(1))
            self.folder_group_perms and [
                perms.pop(m.group(1), None) for perms in self.folder_group_perms.values()
            ]
            return self._ocs(100)

        # --- groupfolders ACL ---
        m = re.fullmatch(r"/index\.php/apps/groupfolders/folders/([^/]+)/groups", path)
        if m and method == "POST":
            self.folder_group_perms.setdefault(m.group(1), {}).setdefault(
                self._form(request).get("group", ""), 31
            )
            return self._ocs(100)

        m = re.fullmatch(
            r"/index\.php/apps/groupfolders/folders/([^/]+)/groups/([^/]+)", path
        )
        if m and method == "POST":
            fid, gid = m.group(1), m.group(2)
            self.folder_group_perms.setdefault(fid, {})[gid] = int(
                self._form(request).get("permissions", "0")
            )
            return self._ocs(100)

        # --- WebDAV groupfolders (canary) ---
        dav_prefix = f"/remote.php/dav/groupfolders/{AGENT_USER}/{MOUNTPOINT}"
        if path.startswith(dav_prefix):
            rel = path[len(dav_prefix):].strip("/")
            if method == "PUT":
                # MKCOL parents are also PUT-preceded by MKCOL; accept both.
                self.files[rel] = bytes(request.content)
                return httpx.Response(201)
            if method == "MKCOL":
                return httpx.Response(201)
            if method == "DELETE":
                self.files.pop(rel, None)
                return httpx.Response(204)
            if method == "GET":
                if rel in self.files:
                    return httpx.Response(200, content=self.files[rel])
                return httpx.Response(404)

        return httpx.Response(500, content=f"unhandled {method} {path}".encode())


def _backend_with_ocs_fake():
    backend = NextcloudBackend(_settings())
    fake = FakeNcOcs()
    backend._client = httpx.AsyncClient(
        base_url=NC_BASE, transport=httpx.MockTransport(fake.handler)
    )
    backend._initialized = True
    backend._agent_user = AGENT_USER
    backend._agent_password = "pw"
    return backend, fake


@pytest.mark.asyncio
async def test_ensure_ro_reader_creates_low_priv_account():
    backend, fake = _backend_with_ocs_fake()
    reader_id = await backend.ensure_ro_reader(user_key="abc")
    assert reader_id == "srw-reader-abc"
    assert fake.users["srw-reader-abc"]["groups"] == []  # no folder access yet


@pytest.mark.asyncio
async def test_ensure_ro_reader_is_idempotent():
    backend, fake = _backend_with_ocs_fake()
    await backend.ensure_ro_reader(user_key="abc")
    reader_id = await backend.ensure_ro_reader(user_key="abc")  # tolerates OCS 102
    assert reader_id == "srw-reader-abc"


@pytest.mark.asyncio
async def test_mint_grant_gives_reader_read_only_on_folder():
    backend, fake = _backend_with_ocs_fake()
    await backend.ensure_ro_reader(user_key="abc")
    grant = await backend.mint_ro_grant(_handle(), user_key="abc", grant_key="thread-1")
    group = _json(grant.grant_handle)["group_id"]
    assert fake.folder_group_perms[FOLDER_ID][group] == 1  # read-only, not 31
    assert group in fake.users["srw-reader-abc"]["groups"]
    assert grant.credentials  # a rotated app-password was issued
    assert grant.auth_kind == "basic"
    assert grant.reader_id == "srw-reader-abc"
    assert quote(MOUNTPOINT, safe="") in grant.webdav_url


@pytest.mark.asyncio
async def test_revoke_grant_removes_folder_access_but_keeps_account():
    backend, fake = _backend_with_ocs_fake()
    await backend.ensure_ro_reader(user_key="abc")
    grant = await backend.mint_ro_grant(_handle(), user_key="abc", grant_key="thread-1")
    await backend.revoke_ro_grant(grant.grant_handle, user_key="abc")
    group = _json(grant.grant_handle)["group_id"]
    assert group not in fake.folder_group_perms.get(FOLDER_ID, {})
    assert "srw-reader-abc" in fake.users  # account survives


@pytest.mark.asyncio
async def test_canary_seed_and_remove_roundtrip():
    backend, fake = _backend_with_ocs_fake()
    fixture = await backend.seed_canary_fixture(_handle())
    assert fixture.path in fake.files  # written with the write identity
    await backend.remove_canary_fixture(_handle(), fixture)
    assert fixture.path not in fake.files
