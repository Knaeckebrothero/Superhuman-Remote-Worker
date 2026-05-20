"""OpenCloudBackend — ``MainCloudBackend`` implementation for OpenCloud.

Speaks LibreGraph (the Microsoft-Graph subset under ``/graph/v1.0/`` and
``/graph/v1beta1/``) exclusively. The legacy OCS endpoints that both
OpenCloud and Nextcloud expose are deliberately NOT used — the Graph
surface is the strategic direction upstream and unifies cleanly with the
future MS365 adapter.

Authenticates as a service account via Keycloak's ``client_credentials``
OAuth2 flow (NOT OpenCloud's internal ``OCIS_SERVICE_ACCOUNT_*`` mechanism,
which is an internal gRPC path not exposed as a stable public contract).

The adapter is fully async, built on raw ``httpx.AsyncClient`` (no
``msgraph-sdk``), and hits roughly the same error-mapping and telemetry
patterns as ``NextcloudBackend``.

See §5.2 of ``docs/features/main_cloud_abstraction.md`` for the full
design, and §13.2 for the underlying research references.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import quote, unquote

import httpx

from .base import HealthStatus, UserHome
from .config import OpenCloudSettings
from .errors import CloudBackendError, CloudBackendErrorKind
from .handles import (
    GroupId,
    ProjectFolderEntry,
    ProjectFolderHandle,
    SessionFolderHandle,
    ShareHandle,
    UserId,
)
from .telemetry import instrument_backend_op

logger = logging.getLogger(__name__)

BACKEND_ID = "opencloud"

_DEFAULT_QUOTA_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB
# OpenCloud ships several roles with identical displayName ("Can edit",
# "Can view") but different applicability — some work on whole Spaces,
# others on subfolders/files inside a Space. The `@libre.graph.weight`
# distinguishes them:
#
#   "Can view"   weight=10 (shares)  / weight=40 (Spaces)
#   "Can edit"   weight=60 (shares)  / weight=90 (Spaces) / weight=100 (files)
#   "Can manage" weight=120 (Spaces only)
#
# The Space-weighted roles (40 / 90 / 120) target the `/drives/{id}/root/invite`
# endpoint — i.e. granting access to a whole drive. The share-weighted roles
# (10 / 60) target `/drives/{id}/items/{item}/invite` — i.e. a specific
# subfolder within a drive. Using the wrong weight returns
# `400 "role not applicable to this resource"`.
#
# Session folders are subfolders inside the agent-home Space, so they invite
# with the share-weighted "Can edit" (weight=60). Project folders share the
# whole Space with a group, so they use the Space-weighted "Can edit"
# (weight=90).
#
# The well-known UUIDs are stable across OpenCloud releases (they come from
# the roleDefinitions compiled into the binary), but we still resolve by
# (displayName, weight) at runtime so forks that change the UUIDs keep
# working.
_SPACE_EDITOR_ROLE_WEIGHT = 90  # "Can edit" — whole-Space invite
_SPACE_VIEWER_ROLE_WEIGHT = 40  # "Can view" — whole-Space invite
_SPACE_EDITOR_ROLE_NAME = "Can edit"
_SPACE_VIEWER_ROLE_NAME = "Can view"
_FOLDER_EDITOR_ROLE_WEIGHT = 60  # "Can edit" — subfolder invite
_FOLDER_EDITOR_ROLE_NAME = "Can edit"
_AGENT_HOME_SPACE_NAME = "srw-agent-home"
_TOKEN_CLOCK_SKEW_SECONDS = 30.0


# PROPFIND response patterns — module-level so they compile once. Used by
# list_project_folder to walk a Space recursively. The XML matches the
# existing regex-based approach in _resolve_item_id rather than pulling
# in a full XML parser; OpenCloud's PROPFIND output is structurally
# regular enough that this is safe.
_PROPFIND_RESPONSE_RE = re.compile(
    r"<d:response[^>]*>(.*?)</d:response>", re.DOTALL | re.IGNORECASE
)
_PROPFIND_HREF_RE = re.compile(r"<d:href[^>]*>([^<]+)</d:href>", re.IGNORECASE)
_PROPFIND_COLLECTION_RE = re.compile(r"<d:collection\s*/>", re.IGNORECASE)
_PROPFIND_CONTENTLENGTH_RE = re.compile(
    r"<d:getcontentlength[^>]*>(\d+)</d:getcontentlength>", re.IGNORECASE
)
_PROPFIND_ETAG_RE = re.compile(r"<d:getetag[^>]*>([^<]+)</d:getetag>", re.IGNORECASE)
_PROPFIND_CONTENTTYPE_RE = re.compile(
    r"<d:getcontenttype[^>]*>([^<]+)</d:getcontenttype>", re.IGNORECASE
)


def _parse_propfind_entries(xml: str, *, href_prefix: str) -> list[ProjectFolderEntry]:
    """Pull file + directory entries out of a PROPFIND multistatus body.

    ``href_prefix`` is the URL path prefix to strip so returned paths are
    relative to the folder root (no leading slash). Both ``href_prefix``
    and the ``<d:href>`` values are URL-decoded before comparison —
    OpenCloud's response uses literal ``$`` in href even when the request
    URL had it as ``%24``, which would otherwise break a naive
    ``startswith`` check. Entries pointing at the root itself (empty path
    after stripping) are dropped.
    """
    decoded_prefix = unquote(href_prefix)
    entries: list[ProjectFolderEntry] = []
    for resp in _PROPFIND_RESPONSE_RE.finditer(xml):
        block = resp.group(1)
        href_match = _PROPFIND_HREF_RE.search(block)
        if not href_match:
            continue
        href = unquote(href_match.group(1).strip())
        if not href.startswith(decoded_prefix):
            continue
        rel_path = href[len(decoded_prefix) :].rstrip("/")
        if not rel_path:
            # The folder root itself shows up first; skip it.
            continue
        is_dir = bool(_PROPFIND_COLLECTION_RE.search(block))
        size_match = _PROPFIND_CONTENTLENGTH_RE.search(block)
        etag_match = _PROPFIND_ETAG_RE.search(block)
        ctype_match = _PROPFIND_CONTENTTYPE_RE.search(block)
        entries.append(
            ProjectFolderEntry(
                path=rel_path,
                is_dir=is_dir,
                size=int(size_match.group(1)) if size_match else 0,
                etag=etag_match.group(1).strip().strip('"') if etag_match else "",
                content_type=ctype_match.group(1).strip() if ctype_match else "",
            )
        )
    return entries


class OpenCloudBackend:
    """Main-cloud backend for OpenCloud.

    Uses Keycloak client_credentials to obtain a service-account access
    token, then calls LibreGraph on the OpenCloud instance. The token is
    cached and refreshed ~30s before expiry.

    The "agent home" Space hosts every session folder — analogous to
    Nextcloud's ``agent-service`` user home. It is created (if missing)
    at ``ensure_initialized`` time and its drive id is cached on the
    instance for the lifetime of the process.
    """

    backend_id = BACKEND_ID

    def __init__(self, settings: OpenCloudSettings) -> None:
        self._settings = settings
        self._base_url = str(settings.base_url).rstrip("/")
        self._public_url = str(settings.public_url).rstrip("/")
        self._keycloak_issuer = str(settings.keycloak_issuer).rstrip("/")
        self._client_id = settings.keycloak_client_id
        self._client_secret = settings.keycloak_client_secret.get_secret_value()
        self._default_quota = (
            settings.default_quota_bytes
            if settings.default_quota_bytes is not None
            else _DEFAULT_QUOTA_BYTES
        )

        self._initialized = False
        self._client: Optional[httpx.AsyncClient] = None

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        self._role_cache: dict[str, str] = {}

        # Populated by ensure_initialized once the agent-home Space exists.
        self._agent_home_drive_id: Optional[str] = None
        self._agent_home_webdav_base: Optional[str] = None
        self._agent_home_web_url: Optional[str] = None

    # --------------------------------------------------------------- Properties

    @property
    def is_configured(self) -> bool:
        return bool(
            self._base_url
            and self._keycloak_issuer
            and self._client_id
            and self._client_secret
        )

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def webdav_credentials(self) -> dict[str, str]:
        """OpenCloud WebDAV is OAuth bearer-based, not user+password.

        Agents cannot re-use a username/password pair here — they need a
        live bearer token. The ``WorkspaceSyncService`` will grow a
        bearer-aware variant when the first customer wires OpenCloud up
        as their agent data channel; until then, return ``{}`` so the
        legacy ``username``/``password`` plumbing silently falls back.
        """
        return {}

    # ---------------------------------------------------------------- Lifecycle

    async def ensure_initialized(self) -> bool:
        """Verify reachability, fetch role catalog, ensure agent-home Space.

        Soft-failure semantics: returns ``False`` when OpenCloud is not
        configured or unreachable so the rest of the orchestrator can
        boot. After this, every other method raises ``CloudBackendError``
        on hard failure.
        """
        if not self.is_configured:
            logger.info(
                "OpenCloud backend not configured "
                "(missing base_url / keycloak_issuer / client_id / client_secret)"
            )
            return False

        try:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "User-Agent": "SuperhumanRemoteWorker/0.1 main-cloud-backend",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=10.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )

            # Sanity: fetch a token. Any failure here surfaces config problems
            # (wrong Keycloak URL, bad secret, disabled service account) at
            # startup instead of during the first tenant call.
            await self._get_service_token()

            # Fetch the role catalog so sharing calls don't race on the first
            # invite. Non-fatal if the endpoint 404s on older servers — we
            # fall back to lazy resolution.
            try:
                self._role_cache = await self._load_role_catalog()
            except CloudBackendError as e:
                logger.warning(
                    "OpenCloud role catalog preload failed (%s) — "
                    "adapter will retry lazily on first invite.",
                    e,
                )

            # Ensure the agent-home Space exists — this is where session
            # folders live. Cache its drive id for the process lifetime.
            (
                self._agent_home_drive_id,
                self._agent_home_web_url,
            ) = await self._ensure_agent_home_space()
            self._agent_home_webdav_base = (
                f"{self._base_url}/dav/spaces/{self._agent_home_drive_id}"
            )

            self._initialized = True
            logger.info(
                "OpenCloud backend initialized (agent-home drive_id=%s)",
                self._agent_home_drive_id,
            )
            return True
        except Exception as e:
            logger.warning("OpenCloud backend init failed: %s", e)
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None
        self._initialized = False
        self._access_token = None
        self._token_expires_at = 0.0

    async def health_check(self) -> HealthStatus:
        if not self._initialized or self._client is None:
            return HealthStatus(ok=False, latency_ms=0.0, detail="not initialized")
        try:
            start = time.monotonic()
            await self._graph_get("/graph/v1.0/me")
            elapsed_ms = (time.monotonic() - start) * 1000
            return HealthStatus(ok=True, latency_ms=elapsed_ms, detail="ok")
        except Exception as e:
            return HealthStatus(ok=False, latency_ms=0.0, detail=str(e))

    # ------------------------------------------------------------------ Identity

    @instrument_backend_op("resolve_user_identity")
    async def resolve_user_identity(
        self, email: Optional[str], display_name: Optional[str]
    ) -> Optional[UserId]:
        """Resolve an SSO user to the backend's LibreGraph user id.

        LibreGraph does NOT support ``$filter=mail eq '...'`` on /users —
        only ``memberOf/any(...)`` and ``appRoleAssignments/any(...)``
        are honored. We use ``$search`` instead and do a client-side
        exact match on the ``mail`` / ``displayName`` fields.
        """
        self._ensure_ready()
        candidates = [c for c in (email, display_name) if c]
        if not candidates:
            return None
        try:
            for query in candidates:
                resp = await self._graph_get(
                    "/graph/v1.0/users",
                    params={"$search": f'"{query}"'},
                )
                users = resp.json().get("value", [])
                if email:
                    for user in users:
                        if (user.get("mail") or "").lower() == email.lower():
                            return UserId(str(user["id"]))
                if display_name:
                    for user in users:
                        if (
                            user.get("displayName") or ""
                        ).lower() == display_name.lower():
                            return UserId(str(user["id"]))
                if len(users) == 1:
                    return UserId(str(users[0]["id"]))
        except CloudBackendError:
            raise
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e
        return None

    @instrument_backend_op("ensure_user")
    async def ensure_user(
        self,
        *,
        sub: str,
        issuer: str,
        email: Optional[str],
        display_name: Optional[str],
        preferred_username: Optional[str] = None,
    ) -> Optional[UserId]:
        """Create the OpenCloud user record for an SSO identity if missing.

        Without this, OpenCloud only provisions a user on their first
        browser login, which races session-folder sharing for new threads.

        Looks up by (issuer, sub) first — that's the stable identity — then
        falls back to email/displayName resolution. POSTs a LibreGraph user
        with ``identities[]`` carrying the Keycloak ``sub`` so the existing
        autoprovision path still reconciles on login.
        """
        self._ensure_ready()
        existing = await self._find_user_by_sub(issuer, sub)
        if existing:
            return existing
        fallback = await self.resolve_user_identity(email, display_name)
        if fallback:
            return fallback
        if not email or not display_name:
            return None
        # userType is read-only in OpenCloud's LibreGraph impl — including it
        # yields 400 "userType is a read-only attribute". Defaults to Member.
        body: dict[str, Any] = {
            "accountEnabled": True,
            "displayName": display_name,
            "mail": email,
            "identities": [
                {"issuer": issuer, "issuerAssignedId": sub},
            ],
        }
        if preferred_username:
            body["onPremisesSamAccountName"] = preferred_username
        try:
            resp = await self._graph_post("/graph/v1.0/users", json=body)
            # OpenCloud returns 400 with an error code of "nameAlreadyExists" on
            # concurrent-create collisions (not the 409 LibreGraph docs imply).
            # Treat both as a race — look up the user that the other caller
            # already created instead of failing the whole provision.
            if resp.status_code == 409 or (
                resp.status_code == 400 and "nameAlreadyExists" in resp.text
            ):
                return await self._find_user_by_sub(
                    issuer, sub
                ) or await self.resolve_user_identity(email, display_name)
            resp.raise_for_status()
            user_id = resp.json().get("id")
            if user_id:
                logger.info(
                    "Provisioned OpenCloud user %s (sub=%s, email=%s)",
                    user_id,
                    sub,
                    email,
                )
                return UserId(str(user_id))
            return None
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    async def _find_user_by_sub(self, issuer: str, sub: str) -> Optional[UserId]:
        """Locate an existing LibreGraph user by (issuer, sub) identity tuple."""
        try:
            resp = await self._graph_get(
                "/graph/v1.0/users",
                params={"$search": f'"{sub}"'},
            )
            for user in resp.json().get("value", []):
                for identity in user.get("identities") or []:
                    if (
                        identity.get("issuer") == issuer
                        and identity.get("issuerAssignedId") == sub
                    ):
                        return UserId(str(user["id"]))
        except httpx.HTTPError:
            return None
        return None

    def get_default_home_browser_url(self) -> Optional[str]:
        """Generic OpenCloud Files app URL (user-agnostic)."""
        return f"{self._public_url}/files/"

    async def get_user_home(self, user_id: UserId) -> Optional[UserHome]:
        """Fetch the user's personal Space and return its URL triple."""
        if not self._initialized or self._client is None:
            return None
        try:
            resp = await self._graph_get(
                f"/graph/v1.0/users/{user_id}",
                params={"$expand": "drive"},
            )
            user_data = resp.json()
            drive = user_data.get("drive") or {}
            drive_id = drive.get("id")
            if not drive_id:
                return None
            browser_url = (drive.get("webUrl") or f"{self._public_url}/files/").rstrip(
                "/"
            )
            root = drive.get("root") or {}
            webdav_url = root.get("webDavUrl") or (
                f"{self._base_url}/dav/spaces/{drive_id}/"
            )
            handle = ProjectFolderHandle(
                backend=self.backend_id,
                native_id=str(drive_id),
                vendor_meta={
                    "kind": "user_home",
                    "username": str(user_id),
                    "webdav_url": webdav_url,
                },
            )
            return UserHome(
                handle=handle,
                browser_url=browser_url,
                webdav_url=webdav_url,
            )
        except CloudBackendError:
            raise
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    # -------------------------------------------------------------------- Groups

    @instrument_backend_op("ensure_group")
    async def ensure_group(self, group_id: GroupId) -> None:
        """Create a LibreGraph group if missing (idempotent).

        Persists the requested ``group_id`` as ``displayName``. The server
        assigns a UUID; callers never see it because every subsequent
        operation looks the group up by displayName via
        ``_resolve_group_id``.
        """
        self._ensure_ready()
        try:
            existing = await self._resolve_group_id(group_id)
            if existing is not None:
                return
            resp = await self._graph_post(
                "/graph/v1.0/groups",
                json={"displayName": str(group_id)},
            )
            resp.raise_for_status()
        except CloudBackendError:
            raise
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    @instrument_backend_op("add_user_to_group")
    async def add_user_to_group(self, user_id: UserId, group_id: GroupId) -> None:
        self._ensure_ready()
        try:
            resolved = await self._require_group_id(group_id)
            resp = await self._graph_post(
                f"/graph/v1.0/groups/{resolved}/members/$ref",
                json={
                    "@odata.id": (f"{self._base_url}/graph/v1.0/users/{user_id}"),
                },
            )
            resp.raise_for_status()
        except CloudBackendError:
            raise
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    @instrument_backend_op("remove_user_from_group")
    async def remove_user_from_group(self, user_id: UserId, group_id: GroupId) -> None:
        self._ensure_ready()
        try:
            resolved = await self._require_group_id(group_id)
            resp = await self._graph_delete(
                f"/graph/v1.0/groups/{resolved}/members/{user_id}/$ref",
            )
            if resp.status_code == 404:
                return  # already gone — idempotent
            resp.raise_for_status()
        except CloudBackendError:
            raise
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    # ------------------------------------------------------------- Project folders

    @instrument_backend_op("ensure_project_folder")
    async def ensure_project_folder(
        self, *, project_name: str, group_id: GroupId
    ) -> ProjectFolderHandle:
        """Create a project Space and grant the group editor access.

        LibreGraph:
          1. ``POST /graph/v1.0/drives`` creates the Space
             (driveType is auto-assigned as ``project``).
          2. ``POST /graph/v1beta1/drives/{id}/root/invite`` grants the
             project group the Space-applicable "Can edit" role.
        """
        self._ensure_ready()
        try:
            quota: dict[str, Any] = {}
            if self._default_quota:
                quota = {"quota": {"total": self._default_quota}}
            resp = await self._graph_post(
                "/graph/v1.0/drives",
                json={"name": project_name, **quota},
            )
            resp.raise_for_status()
            drive = resp.json()
            drive_id = drive.get("id")
            if not drive_id:
                raise CloudBackendError(
                    CloudBackendErrorKind.UNKNOWN,
                    "OpenCloud drive creation returned no id",
                    backend=self.backend_id,
                    raw=drive,
                )
            root = drive.get("root") or {}
            web_url = (drive.get("webUrl") or "").rstrip("/")
            webdav_url = root.get("webDavUrl") or (
                f"{self._base_url}/dav/spaces/{drive_id}/"
            )

            # Grant the project group editor access on the new Space.
            group_native_id = await self._require_group_id(group_id)
            await self._invite_group(
                drive_id,
                group_native_id,
                _SPACE_EDITOR_ROLE_NAME,
                _SPACE_EDITOR_ROLE_WEIGHT,
            )

            logger.info(
                "Created OpenCloud Space '%s' (drive_id=%s) for group '%s'",
                project_name,
                drive_id,
                group_id,
            )
            return ProjectFolderHandle(
                backend=self.backend_id,
                native_id=str(drive_id),
                vendor_meta={
                    "mountpoint": project_name,
                    "webdav_url": webdav_url,
                    "web_url": web_url,
                },
            )
        except CloudBackendError:
            raise
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    @instrument_backend_op("delete_project_folder")
    async def delete_project_folder(
        self, handle: ProjectFolderHandle, *, if_exists: bool = True
    ) -> None:
        """Disable-then-purge delete pattern.

        OpenCloud refuses to purge an enabled Space with a 400. The web UI
        works around this by first sending a DELETE with no headers (which
        disables the Space) and then a DELETE with ``Purge: T`` (which
        permanently removes it). This is not documented upstream and was
        discovered from Web UI traffic; see §5.2 of the design doc.
        """
        self._ensure_ready()
        drive_id = handle.native_id
        if not drive_id:
            raise CloudBackendError(
                CloudBackendErrorKind.INVALID_REQUEST,
                "missing drive id on ProjectFolderHandle",
                backend=self.backend_id,
            )
        try:
            # Step 1: disable the Space.
            resp = await self._graph_delete(f"/graph/v1.0/drives/{drive_id}")
            if resp.status_code == 404:
                if if_exists:
                    logger.debug("OpenCloud Space drive_id=%s already gone", drive_id)
                    return
                raise CloudBackendError(
                    CloudBackendErrorKind.NOT_FOUND,
                    f"Space {drive_id} not found",
                    backend=self.backend_id,
                    status_code=404,
                )
            # 204 No Content on disable, some versions return 200.
            if resp.status_code not in (200, 204):
                resp.raise_for_status()

            # Step 2: purge the disabled Space.
            resp = await self._graph_delete(
                f"/graph/v1.0/drives/{drive_id}",
                extra_headers={"Purge": "T"},
            )
            if resp.status_code == 404:
                # Purge after disable can return 404 when the server has
                # already reaped it; treat as success.
                return
            resp.raise_for_status()
            logger.info("Purged OpenCloud Space drive_id=%s", drive_id)
        except CloudBackendError:
            raise
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    @instrument_backend_op("refresh_project_folder_access")
    async def refresh_project_folder_access(
        self, handle: ProjectFolderHandle, group_id: GroupId
    ) -> None:
        """No-op for OpenCloud.

        Unlike Nextcloud (server#57445), OpenCloud propagates group
        membership changes to Space grants automatically. The method is
        kept in the Protocol for symmetry and is a silent no-op on this
        backend; operators never need to poke the Space to refresh
        access for a newly-added member.
        """
        self._ensure_ready()
        _ = handle, group_id  # documented no-op — see docstring
        return

    def get_project_folder_browser_url(
        self, handle: ProjectFolderHandle
    ) -> Optional[str]:
        """Browser deep-link to a Space in the OpenCloud Web UI.

        Prefers the ``webUrl`` captured from the drive-creation response
        (persisted as ``vendor_meta.web_url``) because OpenCloud Web routes
        Spaces by drive *alias* (e.g. ``project/<uuid>``), not by the raw
        composite drive id. Falling back to ``/files/spaces/<drive_id>``
        silently resolves to the user's personal space on real OpenCloud.
        """
        web_url = handle.vendor_meta.get("web_url")
        if web_url:
            return str(web_url)
        drive_id = handle.native_id
        if not drive_id:
            return None
        # Delimiters in the composite drive id must be percent-encoded.
        # See opencloud-eu/web#1795 — most HTTP clients skip `!` and `$`
        # by default.
        safe_id = quote(drive_id, safe="")
        return f"{self._public_url}/files/spaces/{safe_id}"

    @instrument_backend_op("list_project_folder")
    async def list_project_folder(
        self,
        handle: ProjectFolderHandle,
        *,
        subpath: str = "",
    ) -> list[ProjectFolderEntry]:
        """Recursive enumeration of a project folder Space, via repeated
        ``PROPFIND`` with ``Depth: 1``.

        OpenCloud (oCIS) rejects ``Depth: infinity`` with HTTP 400, so we
        walk the tree breadth-first instead — one ``Depth: 1`` PROPFIND
        per directory. Returns every descendant (files + directories);
        order is breadth-first.
        """
        self._ensure_ready()
        drive_id = handle.native_id
        if not drive_id:
            raise CloudBackendError(
                CloudBackendErrorKind.INVALID_REQUEST,
                "list_project_folder: handle has no drive id",
                backend=self.backend_id,
                retryable=False,
            )
        safe_drive = quote(drive_id, safe="")
        base_path = f"/dav/spaces/{safe_drive}"
        # Strip the leading prefix off of every href so callers get
        # paths relative to the project root.
        href_prefix = f"{base_path}/"

        all_entries: list[ProjectFolderEntry] = []
        # Breadth-first queue of subpaths to expand. Empty string = root.
        queue: list[str] = [subpath.strip("/")]
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            safe_sub = quote(current, safe="/")
            url_path = f"{base_path}/{safe_sub}" if safe_sub else base_path
            entries = await self._propfind_depth_one(url_path, href_prefix)
            for entry in entries:
                # Skip entries that fall outside ``subpath`` — Depth=1
                # only returns immediate children, but defensive guard.
                if current and not entry.path.startswith(current.rstrip("/") + "/"):
                    if entry.path != current:
                        continue
                all_entries.append(entry)
                if entry.is_dir and entry.path not in seen:
                    queue.append(entry.path)
        return all_entries

    async def _propfind_depth_one(
        self, url_path: str, href_prefix: str
    ) -> list[ProjectFolderEntry]:
        """Single ``Depth: 1`` PROPFIND that returns one folder's children.

        Sent with no body — the server returns all known props by
        default, same shape that ``_resolve_item_id`` (Depth=0) gets
        back. Including a body XML had OpenCloud returning 400 in
        testing.
        """
        token = await self._get_service_token()
        try:
            resp = await self._client.request(
                "PROPFIND",
                url_path,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Depth": "1",
                },
            )
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e
        if resp.status_code == 404:
            raise CloudBackendError(
                CloudBackendErrorKind.NOT_FOUND,
                f"path {url_path} not found",
                backend=self.backend_id,
                status_code=404,
            )
        if resp.status_code != 207:
            raise CloudBackendError(
                CloudBackendErrorKind.UNKNOWN,
                f"unexpected PROPFIND status {resp.status_code} for {url_path}",
                backend=self.backend_id,
                status_code=resp.status_code,
                raw={"body": resp.text[:500]},
            )
        return _parse_propfind_entries(resp.text, href_prefix=href_prefix)

    @instrument_backend_op("get_project_folder_file_bytes")
    async def get_project_folder_file_bytes(
        self,
        handle: ProjectFolderHandle,
        *,
        path: str,
    ) -> bytes:
        """GET one file out of a project folder Space."""
        self._ensure_ready()
        drive_id = handle.native_id
        if not drive_id:
            raise CloudBackendError(
                CloudBackendErrorKind.INVALID_REQUEST,
                "get_project_folder_file_bytes: handle has no drive id",
                backend=self.backend_id,
                retryable=False,
            )
        safe_drive = quote(drive_id, safe="")
        rel = path.lstrip("/")
        if not rel:
            raise CloudBackendError(
                CloudBackendErrorKind.INVALID_REQUEST,
                "get_project_folder_file_bytes: path must be non-empty",
                backend=self.backend_id,
                retryable=False,
            )
        safe_path = quote(rel, safe="/")
        url = f"/dav/spaces/{safe_drive}/{safe_path}"
        token = await self._get_service_token()
        try:
            resp = await self._client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e
        if resp.status_code == 404:
            raise CloudBackendError(
                CloudBackendErrorKind.NOT_FOUND,
                f"file {path!r} not found in drive {drive_id}",
                backend=self.backend_id,
                status_code=404,
            )
        if resp.status_code != 200:
            raise CloudBackendError(
                CloudBackendErrorKind.UNKNOWN,
                f"GET file {path!r}: unexpected status {resp.status_code}",
                backend=self.backend_id,
                status_code=resp.status_code,
            )
        return resp.content

    def get_project_folder_webdav_url(
        self, handle: ProjectFolderHandle
    ) -> Optional[str]:
        """Internal WebDAV URL for a Space.

        Preference order: persisted ``webdav_url`` from the create response
        → reconstructed ``{base_url}/dav/spaces/{drive_id}/``.
        """
        persisted = handle.vendor_meta.get("webdav_url")
        if persisted:
            return str(persisted)
        drive_id = handle.native_id
        if not drive_id:
            return None
        safe_id = quote(drive_id, safe="")
        return f"{self._base_url}/dav/spaces/{safe_id}/"

    # ------------------------------------------------------------- Session folders

    @instrument_backend_op("ensure_session_folder")
    async def ensure_session_folder(self, *, session_id: str) -> SessionFolderHandle:
        """Create ``sessions/{session_id}`` under the agent-home Space via WebDAV.

        OpenCloud WebDAV lives at ``{base_url}/dav/spaces/{drive-id}/``
        (note: NOT ``/remote.php/dav/...`` — that's Nextcloud only). The
        path must be MKCOL-ed one segment at a time because intermediate
        collections are NOT auto-created.
        """
        self._ensure_ready()
        if self._agent_home_webdav_base is None:
            raise CloudBackendError(
                CloudBackendErrorKind.UNAVAILABLE,
                "agent-home Space WebDAV base not set",
                backend=self.backend_id,
            )
        folder_path = f"sessions/{session_id}"
        parts = folder_path.strip("/").split("/")
        token = await self._get_service_token()
        try:
            for i in range(len(parts)):
                partial = "/".join(parts[: i + 1])
                url = f"{self._agent_home_webdav_base}/{partial}"
                resp = await self._client.request(
                    "MKCOL",
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code in (201, 405):
                    continue
                resp.raise_for_status()
            logger.info("Created OpenCloud session folder: %s", folder_path)
            vendor_meta: dict[str, Any] = {"drive_id": self._agent_home_drive_id}
            try:
                # Capture the compound fileId so the cloud-button URL can
                # deep-link via OpenCloud's /f/<fileId> private-link resolver.
                # PROPFIND failure is non-fatal — URL falls back to Shares view.
                vendor_meta["item_id"] = await self._resolve_item_id(
                    self._agent_home_drive_id, folder_path
                )
            except Exception as e:
                logger.warning(
                    "Could not resolve item_id for new session folder %s: %s",
                    folder_path,
                    e,
                )
            return SessionFolderHandle(
                backend=self.backend_id,
                native_id=folder_path,
                vendor_meta=vendor_meta,
            )
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    @instrument_backend_op("delete_session_folder")
    async def delete_session_folder(
        self, handle: SessionFolderHandle, *, if_exists: bool = True
    ) -> None:
        self._ensure_ready()
        if self._agent_home_webdav_base is None:
            raise CloudBackendError(
                CloudBackendErrorKind.UNAVAILABLE,
                "agent-home Space WebDAV base not set",
                backend=self.backend_id,
            )
        folder_path = handle.native_id
        token = await self._get_service_token()
        try:
            url = f"{self._agent_home_webdav_base}/{folder_path}"
            resp = await self._client.delete(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code in (200, 204):
                logger.info("Deleted OpenCloud session folder: %s", folder_path)
                return
            if resp.status_code == 404:
                if if_exists:
                    return
                raise CloudBackendError(
                    CloudBackendErrorKind.NOT_FOUND,
                    f"Session folder '{folder_path}' not found",
                    backend=self.backend_id,
                    status_code=404,
                )
            resp.raise_for_status()
        except CloudBackendError:
            raise
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    @instrument_backend_op("share_session_folder")
    async def share_session_folder(
        self, handle: SessionFolderHandle, user_id: UserId
    ) -> ShareHandle:
        """Invite a user to a session folder via LibreGraph sharing v1beta1."""
        self._ensure_ready()
        drive_id = handle.vendor_meta.get("drive_id") or self._agent_home_drive_id
        if not drive_id:
            raise CloudBackendError(
                CloudBackendErrorKind.UNAVAILABLE,
                "session handle is missing drive_id and no agent-home cached",
                backend=self.backend_id,
            )
        folder_path = handle.native_id
        try:
            role_id = await self._role_id(
                _FOLDER_EDITOR_ROLE_NAME,
                _FOLDER_EDITOR_ROLE_WEIGHT,
            )
            item_id = await self._resolve_item_id(str(drive_id), folder_path)
            safe_drive = quote(str(drive_id), safe="")
            safe_item = quote(item_id, safe="")
            resp = await self._graph_post(
                f"/graph/v1beta1/drives/{safe_drive}/items/{safe_item}/invite",
                json={
                    "recipients": [
                        {
                            "@libre.graph.recipient.type": "user",
                            "objectId": str(user_id),
                        }
                    ],
                    "roles": [role_id],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            perms = data.get("value") or []
            if not perms:
                raise CloudBackendError(
                    CloudBackendErrorKind.UNKNOWN,
                    "OpenCloud invite returned no permissions",
                    backend=self.backend_id,
                    raw=data,
                )
            permission_id = perms[0].get("id")
            if not permission_id:
                raise CloudBackendError(
                    CloudBackendErrorKind.UNKNOWN,
                    "OpenCloud invite permission missing id",
                    backend=self.backend_id,
                    raw=data,
                )
            return ShareHandle(
                backend=self.backend_id,
                native_id=str(permission_id),
                vendor_meta={
                    "drive_id": str(drive_id),
                    "item_id": item_id,
                },
            )
        except CloudBackendError:
            raise
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    @instrument_backend_op("revoke_session_share")
    async def revoke_session_share(
        self, share: ShareHandle, *, if_exists: bool = True
    ) -> None:
        self._ensure_ready()
        drive_id = share.vendor_meta.get("drive_id")
        item_id = share.vendor_meta.get("item_id")
        if not drive_id or not item_id:
            raise CloudBackendError(
                CloudBackendErrorKind.INVALID_REQUEST,
                "ShareHandle.vendor_meta missing drive_id/item_id",
                backend=self.backend_id,
            )
        safe_drive = quote(str(drive_id), safe="")
        safe_item = quote(str(item_id), safe="")
        safe_permission = quote(share.native_id, safe="")
        try:
            resp = await self._graph_delete(
                f"/graph/v1beta1/drives/{safe_drive}/items/{safe_item}"
                f"/permissions/{safe_permission}",
            )
            if resp.status_code in (200, 204):
                return
            if resp.status_code == 404:
                if if_exists:
                    return
                raise CloudBackendError(
                    CloudBackendErrorKind.NOT_FOUND,
                    f"Share {share.native_id} not found",
                    backend=self.backend_id,
                    status_code=404,
                )
            resp.raise_for_status()
        except CloudBackendError:
            raise
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    def get_session_folder_browser_url(
        self, handle: SessionFolderHandle
    ) -> Optional[str]:
        """Best-effort browser link to the session folder for the thread owner.

        Session folders live in the orchestrator's ``srw-agent-home`` Space
        and are shared with the owner via a folder-level invite. Deep-linking
        into that Space 404s for the recipient because they are not a Space
        member. OpenCloud's ``/f/<fileId>`` private-link resolver handles this
        by dispatching on the viewer's effective access — a share recipient
        gets routed to their Shares-mounted view of the same folder. Fall
        back to the "Shared with me" list if the item_id wasn't captured
        (older sessions created before we started storing it).
        """
        item_id = handle.vendor_meta.get("item_id")
        if item_id:
            safe_id = quote(str(item_id), safe="")
            return f"{self._public_url}/f/{safe_id}"
        return f"{self._public_url}/files/shares/with-me"

    def get_session_folder_webdav_url(
        self, handle: SessionFolderHandle
    ) -> Optional[str]:
        drive_id = handle.vendor_meta.get("drive_id") or self._agent_home_drive_id
        if not drive_id:
            return None
        safe_id = quote(str(drive_id), safe="")
        safe_path = quote(handle.native_id, safe="/")
        return f"{self._base_url}/dav/spaces/{safe_id}/{safe_path}/"

    @instrument_backend_op("put_session_file")
    async def put_session_file(
        self,
        handle: SessionFolderHandle,
        *,
        path: str,
        content: bytes,
        content_type: Optional[str] = None,
    ) -> None:
        """PUT one file into a session folder, MKCOL-ing parents on the way.

        ``path`` is slash-separated, relative to the session folder root,
        no leading slash. WebDAV PUT does not auto-create missing
        intermediate collections, so we MKCOL each parent segment one
        at a time first (idempotent — 405 means "already there").
        """
        self._ensure_ready()
        if self._agent_home_webdav_base is None:
            raise CloudBackendError(
                CloudBackendErrorKind.UNAVAILABLE,
                "agent-home Space WebDAV base not set",
                backend=self.backend_id,
            )
        rel = path.strip("/")
        if not rel:
            raise CloudBackendError(
                CloudBackendErrorKind.INVALID_REQUEST,
                "put_session_file: path must be non-empty",
                backend=self.backend_id,
                retryable=False,
            )
        token = await self._get_service_token()
        folder_path = handle.native_id.strip("/")
        full_segments = folder_path.split("/") + rel.split("/")
        # MKCOL every parent collection of the target file.
        parents = full_segments[:-1]
        try:
            for i in range(len(parents)):
                partial = "/".join(parents[: i + 1])
                url = f"{self._agent_home_webdav_base}/{quote(partial, safe='/')}"
                resp = await self._client.request(
                    "MKCOL",
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code in (201, 405):
                    continue
                resp.raise_for_status()

            put_url = (
                f"{self._agent_home_webdav_base}/"
                f"{quote('/'.join(full_segments), safe='/')}"
            )
            headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
            if content_type:
                headers["Content-Type"] = content_type
            put_resp = await self._client.put(put_url, headers=headers, content=content)
            if put_resp.status_code not in (200, 201, 204):
                put_resp.raise_for_status()
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e

    # ------------------------------------------------------------ Internal helpers

    def _ensure_ready(self) -> None:
        if not self._initialized or self._client is None:
            raise CloudBackendError(
                CloudBackendErrorKind.UNAVAILABLE,
                "OpenCloud backend not initialized",
                backend=self.backend_id,
                retryable=False,
            )

    async def _get_service_token(self) -> str:
        """Return a cached or freshly-minted Keycloak service-account token.

        Refreshes ~30s before expiry to absorb clock skew between the
        orchestrator and Keycloak. Protected by ``_token_lock`` so
        concurrent callers share a single refresh.
        """
        now = time.monotonic()
        if self._access_token and now < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            now = time.monotonic()
            if self._access_token and now < self._token_expires_at:
                return self._access_token
            assert self._client is not None
            try:
                resp = await self._client.post(
                    f"{self._keycloak_issuer}/protocol/openid-connect/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        # openid scope is required so the token unlocks the
                        # Keycloak userinfo endpoint that OpenCloud's proxy
                        # calls as part of its OIDC middleware. Without it,
                        # OpenCloud returns 401 on every API call with
                        # "failed to get userinfo: 403 Forbidden".
                        "scope": "openid",
                    },
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise self._map_http_error(e) from e
            payload = resp.json()
            token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not token or not isinstance(expires_in, (int, float)):
                raise CloudBackendError(
                    CloudBackendErrorKind.AUTHENTICATION_FAILED,
                    "Keycloak token response missing access_token/expires_in",
                    backend=self.backend_id,
                    raw=payload,
                )
            self._access_token = str(token)
            self._token_expires_at = (
                time.monotonic() + float(expires_in) - _TOKEN_CLOCK_SKEW_SECONDS
            )
            return self._access_token

    async def _graph_get(
        self, path: str, *, params: Optional[dict[str, Any]] = None
    ) -> httpx.Response:
        assert self._client is not None
        token = await self._get_service_token()
        resp = await self._client.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp

    async def _graph_post(
        self,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
    ) -> httpx.Response:
        assert self._client is not None
        token = await self._get_service_token()
        return await self._client.post(
            path,
            json=json,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    async def _graph_delete(
        self,
        path: str,
        *,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        assert self._client is not None
        token = await self._get_service_token()
        headers = {"Authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)
        return await self._client.delete(path, headers=headers)

    def _map_http_error(self, exc: Exception) -> CloudBackendError:
        """Translate ``httpx`` errors into ``CloudBackendError``."""
        if isinstance(exc, httpx.HTTPStatusError):
            resp = exc.response
            status = resp.status_code
            kind_map = {
                400: CloudBackendErrorKind.INVALID_REQUEST,
                401: CloudBackendErrorKind.AUTHENTICATION_FAILED,
                403: CloudBackendErrorKind.PERMISSION_DENIED,
                404: CloudBackendErrorKind.NOT_FOUND,
                409: CloudBackendErrorKind.ALREADY_EXISTS,
                413: CloudBackendErrorKind.QUOTA_EXCEEDED,
                429: CloudBackendErrorKind.THROTTLED,
            }
            kind = kind_map.get(status)
            if kind is None:
                kind = (
                    CloudBackendErrorKind.UNAVAILABLE
                    if 500 <= status < 600
                    else CloudBackendErrorKind.UNKNOWN
                )
            retryable = kind in (
                CloudBackendErrorKind.THROTTLED,
                CloudBackendErrorKind.UNAVAILABLE,
                CloudBackendErrorKind.TIMEOUT,
            )
            raw: Optional[dict[str, Any]]
            try:
                raw = resp.json()
            except ValueError:
                raw = {"text": resp.text[:500]}
            request_id = resp.headers.get("x-request-id")
            return CloudBackendError(
                kind,
                f"HTTP {status} from {resp.request.method} {resp.request.url}",
                backend=self.backend_id,
                status_code=status,
                request_id=request_id,
                raw=raw,
                retryable=retryable,
            )
        if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
            return CloudBackendError(
                CloudBackendErrorKind.TIMEOUT,
                f"{type(exc).__name__}: {exc}",
                backend=self.backend_id,
                retryable=True,
            )
        if isinstance(exc, httpx.ConnectError):
            return CloudBackendError(
                CloudBackendErrorKind.UNAVAILABLE,
                f"connection error: {exc}",
                backend=self.backend_id,
                retryable=True,
            )
        if isinstance(exc, httpx.RequestError):
            return CloudBackendError(
                CloudBackendErrorKind.UNAVAILABLE,
                f"request error: {type(exc).__name__}: {exc}",
                backend=self.backend_id,
                retryable=True,
            )
        return CloudBackendError(
            CloudBackendErrorKind.UNKNOWN,
            f"{type(exc).__name__}: {exc}",
            backend=self.backend_id,
        )

    # ---------------------------------------------------------------- Role catalog

    async def _load_role_catalog(self) -> dict[tuple[str, int], str]:
        """Fetch the role definitions from LibreGraph.

        Returns ``{(displayName, weight): roleId}``.  Keys are compound
        because OpenCloud ships several roles with identical displayName
        (e.g. multiple "Can edit" entries) distinguished only by their
        ``@libre.graph.weight``.

        Raises on 404/5xx so that callers can decide whether to fall back
        (``ensure_initialized`` logs and continues; ``_role_id`` refreshes
        and retries).

        OpenCloud returns this endpoint as a bare JSON array, unlike most
        other LibreGraph endpoints which wrap results in ``{"value": [...]}``.
        We accept both shapes so this adapter keeps working if the server
        wraps the payload in a future release.
        """
        resp = await self._graph_get(
            "/graph/v1beta1/roleManagement/permissions/roleDefinitions"
        )
        body = resp.json()
        if isinstance(body, list):
            roles = body
        elif isinstance(body, dict):
            roles = body.get("value", [])
        else:
            raise CloudBackendError(
                CloudBackendErrorKind.UNKNOWN,
                f"unexpected roleDefinitions response shape: {type(body).__name__}",
                backend=self.backend_id,
            )
        return {
            (role["displayName"], int(role.get("@libre.graph.weight", 0))): role["id"]
            for role in roles
            if "id" in role
        }

    async def _role_id(self, display_name: str, weight: int) -> str:
        """Return the role UUID for ``(display_name, weight)``, refreshing the cache if needed."""
        key = (display_name, weight)
        if key in self._role_cache:
            return self._role_cache[key]
        try:
            self._role_cache = await self._load_role_catalog()
        except CloudBackendError as e:
            raise CloudBackendError(
                CloudBackendErrorKind.NOT_SUPPORTED,
                f"could not load OpenCloud role catalog: {e}",
                backend=self.backend_id,
            ) from e
        if key not in self._role_cache:
            raise CloudBackendError(
                CloudBackendErrorKind.NOT_SUPPORTED,
                f"role {display_name!r} not in OpenCloud role catalog",
                backend=self.backend_id,
            )
        return self._role_cache[display_name]

    # ---------------------------------------------------------------- Group lookup

    async def _resolve_group_id(self, group_id: GroupId) -> Optional[str]:
        """Look up a group by displayName. Returns the LibreGraph UUID or None."""
        resp = await self._graph_get(
            "/graph/v1.0/groups",
            params={"$search": f'"{group_id}"'},
        )
        for group in resp.json().get("value", []):
            if group.get("displayName") == str(group_id):
                return str(group.get("id"))
        return None

    async def _require_group_id(self, group_id: GroupId) -> str:
        resolved = await self._resolve_group_id(group_id)
        if resolved is None:
            raise CloudBackendError(
                CloudBackendErrorKind.NOT_FOUND,
                f"group {group_id!r} not found in OpenCloud",
                backend=self.backend_id,
            )
        return resolved

    # ---------------------------------------------------------------- Invite helper

    async def _invite_group(
        self,
        drive_id: str,
        group_native_id: str,
        role_display_name: str,
        role_weight: int,
    ) -> None:
        role_id = await self._role_id(role_display_name, role_weight)
        safe_drive = quote(drive_id, safe="")
        resp = await self._graph_post(
            f"/graph/v1beta1/drives/{safe_drive}/root/invite",
            json={
                "recipients": [
                    {
                        "@libre.graph.recipient.type": "group",
                        "objectId": group_native_id,
                    }
                ],
                "roles": [role_id],
            },
        )
        resp.raise_for_status()

    # ---------------------------------------------------------------- Agent home

    async def _ensure_agent_home_space(self) -> tuple[str, Optional[str]]:
        """Create or fetch the agent-home Space; return (drive_id, web_url).

        Session folders live under this Space — it is analogous to the
        ``agent-service`` user home in the Nextcloud backend. Searched by
        the fixed display name ``srw-agent-home``; created if missing.

        ``web_url`` is the authoritative browser deep-link from the drive
        response; callers should prefer it over constructing
        ``/files/spaces/{drive_id}`` themselves (OpenCloud Web routes by
        drive alias, not raw id).
        """
        # Look up an existing space first. ``/graph/v1.0/drives`` lists
        # every drive the caller can see, which for the service account
        # includes project Spaces too, so we filter by name client-side.
        resp = await self._graph_get(
            "/graph/v1.0/drives",
            params={"$filter": f"name eq '{_AGENT_HOME_SPACE_NAME}'"},
        )
        for drive in resp.json().get("value", []):
            if drive.get("name") == _AGENT_HOME_SPACE_NAME:
                web_url = (drive.get("webUrl") or "").rstrip("/") or None
                return str(drive["id"]), web_url

        # Not found — create it.
        resp = await self._graph_post(
            "/graph/v1.0/drives",
            json={"name": _AGENT_HOME_SPACE_NAME},
        )
        resp.raise_for_status()
        drive = resp.json()
        drive_id = drive.get("id")
        if not drive_id:
            raise CloudBackendError(
                CloudBackendErrorKind.UNKNOWN,
                "agent-home Space creation returned no id",
                backend=self.backend_id,
                raw=drive,
            )
        web_url = (drive.get("webUrl") or "").rstrip("/") or None
        logger.info("Created OpenCloud agent-home Space drive_id=%s", drive_id)
        return str(drive_id), web_url

    # ---------------------------------------------------------------- Item lookup

    async def _resolve_item_id(self, drive_id: str, folder_path: str) -> str:
        """Look up a folder's LibreGraph item id by path, via WebDAV PROPFIND.

        Returns the compound fileid ``{drive_id}!{item_uuid}`` that the
        ``/graph/v1beta1/drives/{id}/items/{item}/invite`` endpoint
        expects.

        We use WebDAV here instead of LibreGraph's native
        ``/graph/v1.0/drives/{id}/root:/{path}`` form because the latter
        returns 404 for subfolders of project Spaces on current OpenCloud
        (tested against docker.io/opencloudeu/opencloud:latest as of
        2026-04-23). PROPFIND on the corresponding ``/dav/spaces/`` URL
        reliably returns the item's ``oc:fileid``, which is the compound
        id the /invite endpoint wants.

        Raises NOT_FOUND if the folder doesn't exist.
        """
        assert self._client is not None
        safe_drive = quote(drive_id, safe="")
        safe_path = quote(folder_path.strip("/"), safe="/")
        token = await self._get_service_token()
        try:
            resp = await self._client.request(
                "PROPFIND",
                f"/dav/spaces/{safe_drive}/{safe_path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Depth": "0",
                },
            )
        except httpx.HTTPError as e:
            raise self._map_http_error(e) from e
        if resp.status_code == 404:
            raise CloudBackendError(
                CloudBackendErrorKind.NOT_FOUND,
                f"item {folder_path!r} not found in drive {drive_id}",
                backend=self.backend_id,
                status_code=404,
            )
        if resp.status_code not in (200, 207):
            raise CloudBackendError(
                CloudBackendErrorKind.UNKNOWN,
                (
                    f"unexpected PROPFIND status {resp.status_code} "
                    f"for {folder_path!r} in drive {drive_id}"
                ),
                backend=self.backend_id,
                status_code=resp.status_code,
                raw={"body": resp.text[:500]},
            )
        match = re.search(r"<oc:fileid>([^<]+)</oc:fileid>", resp.text)
        if not match:
            raise CloudBackendError(
                CloudBackendErrorKind.NOT_FOUND,
                f"oc:fileid missing in PROPFIND response for {folder_path!r}",
                backend=self.backend_id,
                raw={"body": resp.text[:500]},
            )
        return match.group(1)
