---
tags:
  - feature
  - architecture
  - cloud-infrastructure
  - storage
aliases:
  - main cloud abstraction
  - swappable cloud backend
  - cloud backend interface
related:
  - "[[features/sso_and_cloud_storage]]"
  - "[[features/project_cloud_folders]]"
  - "[[done/cloud_storage_alternatives]]"
  - "[[datasources]]"
---

# Main Cloud Abstraction — One Configurable Main Cloud Per Deployment

The system has exactly one **main cloud**: the file-storage backend used for the user's home folder in their default project, and for the session folders created by persistent agent threads. Today this main cloud is hardwired to Nextcloud. This document designs a swappable backend layer with OpenCloud as the new default and Nextcloud (plus, eventually, Microsoft 365 and Google Workspace) as alternative implementations behind a single Python interface.

**Status:** Phases 1 → 4 shipped 2026-04-11; Phase 5 deferred. Short form:

- **Phase 1** — `MainCloudBackend` Protocol + opaque handles, `NextcloudAdmin` → `NextcloudBackend` refactor, schema migration, 22 call sites rewritten in `orchestrator/main.py`. No user-visible change.
- **Phase 1.5** — `CloudBackendError` error taxonomy, HTML-redirect assertion, `/users/details?search=` switch, client-side 429 leaky bucket, `retry.py` + `config.py` + `telemetry.py` + `FakeMainCloudBackend` contract tests, Nextcloud bootstrap runbook.
- **Phase 2** — OpenCloud adapter (`opencloud.py`, ~780 LoC) via LibreGraph + Keycloak `client_credentials`, disable-then-purge delete, role-UUID cache, compose profile, Keycloak client seeding, bootstrap runbook. Opt-in via `MAIN_CLOUD_BACKEND=opencloud`.
- **Phase 3** — Greenfield default flipped to OpenCloud; Nextcloud behind `COMPOSE_PROFILES=nextcloud`. `_detect_legacy_nextcloud_mode()` auto-routes existing deployments on upgrade with zero .env changes.
- **Phase 4** — Admin Cloud Storage settings UI in the cockpit. Admins edit non-secret main-cloud config, persisted to `system_settings.main_cloud` JSONB, hot-reloaded across replicas via Postgres `pg_notify` / `LISTEN`. Secrets stay in Vault/ESO/.env, referenced by name only.
- **Phase 5 — DEFERRED.** MS365 + Google Workspace adapters, and the agent-side `src/tools/webdav/` refactor the first non-WebDAV adapter forces. The Phase 1–4 interface accommodates both; this is a separate future workstream, scoped in §8.

See §8 for the phase-by-phase breakdown with line-by-line deliverables.

**Depends on:** [[features/sso_and_cloud_storage]] (Keycloak SSO), [[features/project_cloud_folders]] (current project/session folder lifecycle).

**Background:** the decision to swap Nextcloud out as the default and the comparative analysis of candidate backends are documented in [[done/cloud_storage_alternatives]]. This document is the implementation design that follows from that decision.

## 1. Goal

Three things, in priority order:

1. **Decouple the orchestrator from any vendor SDK.** All cloud-specific logic moves behind a single `MainCloudBackend` Protocol. The orchestrator never imports `NextcloudAdmin` directly; it calls `main_cloud.ensure_project_folder(...)` and the active backend handles the call.
2. **Ship OpenCloud as the new default main cloud,** deployed alongside the system the same way Nextcloud is deployed today (a service in `docker-compose.yaml`, a manifest in `deployment/`, an init script that creates the agent service account and a Keycloak OIDC client).
3. **Allow users to point the system at an external main cloud** (their own existing Nextcloud, OpenCloud, or eventually Microsoft 365 / Google Workspace) via configuration, so the bundled default is the out-of-the-box experience but not the only option.

The main cloud is exactly one per deployment. Multi-cloud, per-project cloud selection, and treating arbitrary external clouds as automatic backends are all out of scope — see §10.

## 2. The "One Main Cloud" Constraint

The main cloud is the file backend that gets used **automatically** by lifecycle code at well-defined moments:

| Lifecycle moment | What gets created on the main cloud |
|---|---|
| User created (first SSO login) | User home directory is discovered via `resolve_user_identity()`; SSO provisioning is upstream of this layer |
| User's default project | A `webdav`/`cloud` datasource pointing to the user's home directory on the main cloud |
| New non-default project created | A shared "project folder" + a datasource pointing to it; the project's group is granted access |
| User added to a project | The user is granted access to that project's shared folder |
| Persistent agent thread started | A session folder, shared with the user, mounted into the agent's workspace via `WorkspaceSyncService` |
| Project / session deleted | Corresponding folders cleaned up |

Other clouds connected as plain datasources (a user attaching a personal Google Drive to a specific job, for example) are an entirely different mechanism — they live in the `datasources` table and have no automatic lifecycle wiring. Those are out of scope.

The "one main cloud" rule is what makes the abstraction tractable: there is one place to look in the orchestrator (`main_cloud = ...`) and one set of credentials to manage. Multiple main clouds per deployment would require per-project routing logic, conflict resolution, and a UX nightmare; we explicitly do not want that. **Non-destructive switching** (§6) lets us swap the *active* backend without invalidating data that older projects created on the *previous* backend, which is not the same as "many main clouds at once."

## 3. Current State (post-Phase 3: OpenCloud default, Nextcloud opt-in)

This section described the pre-abstraction Nextcloud-only state as a snapshot — the facts below are **historical**, retained for context on what was refactored. The Phase 1 through Phase 3 rollout replaced most of this: `NextcloudAdmin` is gone, `MainCloudBackend` is the contract, and the active backend is resolved at startup from env vars with OpenCloud as the greenfield default. Use `git blame` or §5 / §8 for the current picture.

The pre-Phase-1 Nextcloud integration was fully captured in [[features/project_cloud_folders]] and [[features/sso_and_cloud_storage]]. The relevant facts, audited 2026-04-11:

- **One service: `NextcloudAdmin`** in `orchestrator/services/nextcloud_admin.py` (528 LoC) is the single point of contact between orchestrator code and the Nextcloud APIs (Group Folders, OCS Provisioning, OCS Share, raw WebDAV `MKCOL`/`DELETE`). It speaks raw `httpx.AsyncClient`, no Nextcloud SDK.
- **Lifecycle wiring** lives in `orchestrator/main.py` across **22 call sites** spread over 9 endpoints: `POST /api/users`, `POST /api/projects`, `GET /api/projects/{id}`, `DELETE /api/projects/{id}`, `POST /api/projects/{id}/members`, `DELETE /api/projects/{id}/members/{uid}`, `POST /api/persistent/threads`, `DELETE /api/persistent/threads/{id}`, and the startup hook at `lifespan()`. The module-level singleton is created at `orchestrator/main.py:137` with `nextcloud_admin = NextcloudAdmin()` and its `ensure_initialized()` is called inside the FastAPI `lifespan` context manager.
- **Schema** has three Nextcloud-named columns originally added in `orchestrator/database/schema.sql`: `projects.nextcloud_folder_id INTEGER`, `threads.nc_session_folder TEXT`, `threads.nc_share_id INTEGER`. Post-cutover those edits would ship as a new file under `orchestrator/database/migrations/app/`; the column rename to a vendor-neutral name is a Step-1 expand-contract migration in that directory. (Audit dated 2026-04-11; cutover landed in May 2026 — see `docs/db_migration.md`.)
- **Datasource layer is already vendor-agnostic.** The `webdav` datasource type is generic, `WorkspaceSyncService` (`src/services/workspace_sync.py`) speaks WebDAV not Nextcloud (it uses `webdavclient3`), and the agent tools in `src/tools/webdav/tools.py` are protocol-level. **None of these need to change.** The agent-side is fully vendor-neutral today — confirmed by a repo-wide grep.
- **Deployment** ships Nextcloud as a `docker-compose.yaml` service on port 8800 (`nextcloud:31-apache`) with `docker/nextcloud/setup-nextcloud.sh` as a `before-starting` hook that installs `user_oidc` and `groupfolders`, creates the `srw-agents` group, and creates the `agent-service` user. A sibling `deployment/19-nextcloud.yaml` provisions the same for Kubernetes. Keycloak OIDC wiring was originally in `docker/keycloak/setup-nextcloud-oidc.sh` but has been folded into the Nextcloud setup hook (the older file is marked deprecated).
- **Configuration** is via `NEXTCLOUD_*` env vars in `.env.example` lines 507–612: `NEXTCLOUD_URL`, `NEXTCLOUD_PUBLIC_URL`, `NEXTCLOUD_ADMIN_USER`, `NEXTCLOUD_ADMIN_PASSWORD`, `NEXTCLOUD_AGENT_USER`, `NEXTCLOUD_AGENT_PASSWORD`, `NEXTCLOUD_OIDC_CLIENT_SECRET`, plus optional S3 backend keys. No central `Settings` class — each service reads `os.getenv(...)` in its constructor.

The blast radius of the swap is concentrated in **one service file** (`nextcloud_admin.py`), **one schema file** (add new columns, keep old ones for one release), and **22 orchestrator call sites** that get mechanically rewritten. Agents, datasource resolution, and the WebDAV tooling do not need to change at all.

## 4. Design Overview

### 4.1. The `MainCloudBackend` interface

The interface is a `Protocol` (not an `abc.ABC`), in `orchestrator/services/cloud/base.py`. Structural subtyping is the right tool here: each backend talks to a different API, so there is no shared implementation to reuse, and the modern consensus for "N implementations of one contract, all internal" is Protocol over ABC (see §13.1).

Two design moves distinguish this interface from the v1 draft:

1. **Opaque dataclass handles**, not strings and not URLs, cross method boundaries. Every backend stashes its native identifier(s) inside the handle, and the orchestrator round-trips handles through the DB without parsing them. This is the single biggest forward-compatibility lever for Microsoft 365 (§5.3) — the day MS Graph lands, we add a `GraphAddress` payload to the handle and the rest of the system does not change.
2. **Core protocol is narrow; optional capabilities are separate `Supports*` protocols** (the rclone `Fs` + `Copier` / `Mover` / `PublicLinker` pattern). Every required method every backend must implement lives in the core. Anything that some backends can do and others cannot (public links, quotas, delta change feeds, versioning, trash) lives in a capability protocol that the caller can check via `isinstance` or via a `Features` dataclass.

#### 4.1.1. Handles and identity

```python
# orchestrator/services/cloud/handles.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, NewType, Optional

UserId  = NewType("UserId",  str)   # backend-opaque user identifier (Nextcloud username,
                                    # OpenCloud onPremisesSamAccountName or uuid,
                                    # MS Graph user.id GUID)
GroupId = NewType("GroupId", str)   # backend-opaque group identifier (same rules)

@dataclass(frozen=True, slots=True)
class ProjectFolderHandle:
    """Opaque handle for a shared project folder.

    Round-trips through the database as a single TEXT column (see §4.5).
    Callers MUST NOT parse `native_id` or `vendor_meta`; only the owning
    backend may do so. `backend` is the discriminator used by the router
    when the active backend differs from the one that created this handle.
    """
    backend: str                              # "nextcloud" | "opencloud" | "ms365"
    native_id: str                            # whatever the backend's API returns as primary handle
    vendor_meta: dict[str, Any] = field(default_factory=dict)

    def to_db(self) -> str: ...               # serialises to a single string (§4.5)
    @classmethod
    def from_db(cls, s: str) -> "ProjectFolderHandle": ...

@dataclass(frozen=True, slots=True)
class SessionFolderHandle:
    backend: str
    native_id: str
    vendor_meta: dict[str, Any] = field(default_factory=dict)

    def to_db(self) -> str: ...
    @classmethod
    def from_db(cls, s: str) -> "SessionFolderHandle": ...

@dataclass(frozen=True, slots=True)
class ShareHandle:
    backend: str
    native_id: str                            # Nextcloud OCS share id, OpenCloud permission.id,
                                              # Graph permission.id — all opaque
    vendor_meta: dict[str, Any] = field(default_factory=dict)
```

Why not bare `str`? Static type-checkers will happily let you pass a `GroupId` where a `UserId` is expected if both are strings; they will not let you pass a `UserId` where a `ShareHandle` is expected if they are distinct dataclasses. The `NewType` pattern costs nothing at runtime and catches a real class of bugs in IDE/mypy.

Why the `vendor_meta` escape hatch? Because the research on libcloud's `extra` dict and jclouds's "Views vs ProviderMetadata" split is clear: *acknowledge the leak*. Every adapter will want to stash some context to avoid round-tripping an API call on every operation (OpenCloud's `drive.root.webDavUrl`, Nextcloud's mountpoint name, MS Graph's `drive_id`). Hiding that behind "opaque" would force the adapter to re-resolve state on every call. Accepting a named `dict[str, Any]` field with a documented "do not read from outside the owning backend" rule is cleaner than `**kwargs`-style magic.

#### 4.1.2. The core protocol

The interface below is **as shipped in Phase 1** — i.e. the contract new adapters are expected to match. Two conventions matter:

1. **Phase 1 returns `Optional[...]` on soft failures.** `ensure_*` methods return `Optional[Handle]` rather than always-non-None `Handle`. This preserves the legacy NextcloudAdmin "log-and-return-None" behavior so the call sites in `orchestrator/main.py` did not need new error-handling logic. Phase 1.5 (§8) tightens this to raise `CloudBackendError` on hard failures and narrows the return types.
2. **URL methods are sync, not async.** The Nextcloud adapter constructs URLs purely from the handle's `vendor_meta` — no HTTP round-trip — so making them `async` is wasted overhead. OpenCloud will likely need to cache `drive.root.webDavUrl` in `vendor_meta` for the same sync-ness; if a future adapter genuinely needs to hit the wire on every call, we promote the method back to async and accept the churn.

```python
# orchestrator/services/cloud/base.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from .handles import (
    GroupId, ProjectFolderHandle, SessionFolderHandle, ShareHandle, UserId,
)

@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    latency_ms: float
    detail: str = ""

@dataclass(frozen=True, slots=True)
class UserHome:
    handle: ProjectFolderHandle        # a user's home is modelled as a folder handle
    browser_url: str
    webdav_url: Optional[str]

@runtime_checkable
class MainCloudBackend(Protocol):
    """Contract every main-cloud implementation must satisfy.

    - Methods that touch the wire are async. URL constructors are sync.
    - `ensure_*` methods are idempotent and return the handle for the
      resource, whether newly created or already present. Phase 1 soft
      failures return ``None``; Phase 1.5 raises ``CloudBackendError``.
    - `delete_*` methods default to ``if_exists=True`` — deleting a
      non-existent resource is a no-op, not an error (the "Shrine rule",
      §13.8).
    - Errors caught inside an adapter MUST be translated to
      ``CloudBackendError`` before re-raising (Phase 1.5). In Phase 1
      they are logged and swallowed.
    - See §4.8 for eventual-consistency rules on ensure/create ops.
    """

    backend_id: str                          # "nextcloud" | "opencloud" | "ms365" | ...

    # ---- Lifecycle -------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        """True when the backend has the env vars it needs. Cheap; no I/O."""
    @property
    def is_initialized(self) -> bool:
        """True after a successful ``ensure_initialized`` call."""

    async def health_check(self) -> HealthStatus:
        """Cheap, authenticated call that verifies reachability and
        credentials. Used by /health/cloud and the circuit breaker."""

    async def ensure_initialized(self) -> bool:
        """Called once during FastAPI lifespan. Performs reachability
        checks, resolves the service-account identity, warms caches.
        Returns ``False`` on soft failure — the orchestrator degrades
        gracefully rather than refusing to start (same convention as the
        legacy NextcloudAdmin)."""

    async def close(self) -> None:
        """Close any held HTTP clients. Called during lifespan shutdown."""

    # ---- Identity --------------------------------------------------------
    async def resolve_user_identity(
        self, email: Optional[str], display_name: Optional[str],
    ) -> Optional[UserId]:
        """Resolve an SSO user to the backend's internal user identifier.
        Returns ``None`` if the user has not yet been auto-provisioned.
        Both arguments are ``Optional`` — callers may pass ``None`` when a
        field is missing from the user row; the adapter MUST accept that
        and return ``None`` rather than raise."""

    async def get_user_home(self, user_id: UserId) -> Optional[UserHome]:
        """Return a ``UserHome`` with ``browser_url`` (always),
        ``webdav_url`` (if the backend speaks WebDAV natively), and a
        handle. Returns ``None`` if the backend is not initialized."""

    def get_default_home_browser_url(self) -> Optional[str]:
        """Generic browser URL for the cloud's home/files view, user-agnostic.
        Used by cockpit deep-links when no specific user is in scope (e.g.
        the default project's cloud_storage_url). Equivalent to the legacy
        ``NextcloudAdmin.get_user_home_browser_url()`` accessor."""

    # ---- Groups ----------------------------------------------------------
    async def ensure_group(self, group_id: GroupId) -> None: ...
    async def add_user_to_group(self, user_id: UserId, group_id: GroupId) -> None: ...
    async def remove_user_from_group(self, user_id: UserId, group_id: GroupId) -> None: ...

    # ---- Project folders -------------------------------------------------
    async def ensure_project_folder(
        self, *, project_name: str, group_id: GroupId,
    ) -> Optional[ProjectFolderHandle]:
        """Create a shared folder for the project and grant the project
        group access. Idempotent: returns the existing handle if a folder
        with this name/group already exists. See §4.8 on eventual-
        consistency waits. Phase 1.5 adds an ``idempotency_key`` kwarg
        once Nextcloud's ``OC-Request-ID`` header is honored end-to-end."""

    async def delete_project_folder(
        self, handle: ProjectFolderHandle, *, if_exists: bool = True,
    ) -> None: ...

    async def refresh_project_folder_access(
        self, handle: ProjectFolderHandle, group_id: GroupId,
    ) -> None:
        """Re-assert that the project group has access to the folder.
        Workaround hook for backends where group-membership changes do
        not propagate automatically (Nextcloud bug #57445). No-op on
        backends that do not need it (OpenCloud, MS Graph)."""

    def get_project_folder_browser_url(
        self, handle: ProjectFolderHandle,
    ) -> Optional[str]: ...
    def get_project_folder_webdav_url(
        self, handle: ProjectFolderHandle,
    ) -> Optional[str]:
        """Return a WebDAV URL, or ``None`` for backends that do not speak
        WebDAV (Microsoft Graph). Callers must handle ``None``. See §5.3."""

    # ---- Session folders -------------------------------------------------
    async def ensure_session_folder(
        self, *, session_id: str,
    ) -> Optional[SessionFolderHandle]:
        """Create (or re-fetch) a folder used by one persistent agent
        thread. The adapter chooses its location inside the main cloud."""

    async def delete_session_folder(
        self, handle: SessionFolderHandle, *, if_exists: bool = True,
    ) -> None: ...

    async def share_session_folder(
        self, handle: SessionFolderHandle, user_id: UserId,
    ) -> Optional[ShareHandle]: ...

    async def revoke_session_share(
        self, share: ShareHandle, *, if_exists: bool = True,
    ) -> None: ...

    def get_session_folder_browser_url(
        self, handle: SessionFolderHandle,
    ) -> Optional[str]: ...
    def get_session_folder_webdav_url(
        self, handle: SessionFolderHandle,
    ) -> Optional[str]: ...

    # ---- WebDAV credentials ----------------------------------------------
    @property
    def webdav_credentials(self) -> dict[str, str]:
        """Agent credentials for WebDAV access. Nextcloud returns
        ``{"username": agent_user, "password": agent_password}``; backends
        that do not speak WebDAV (Microsoft Graph) return ``{}`` so the
        call site in ``POST /api/projects`` can skip WebDAV datasource
        creation cleanly."""
```

The handle serializer signature also drifted slightly from the v1 sketch — `from_db` takes a `*, backend` kwarg so bare-string rows (the Phase 1 backfill format) can be deserialized without self-describing JSON:

```python
# orchestrator/services/cloud/handles.py
@classmethod
def from_db(cls, s: str, *, backend: str) -> "ProjectFolderHandle":
    """Deserialize. `backend` is the row's ``main_cloud_backend`` column;
    it acts as a default for bare-string handles persisted before the JSON
    format existed (including the Phase 1 backfill from ``nextcloud_folder_id``).
    """
```

That is the required surface — twenty async methods, four sync URL constructors, plus three properties — organised by responsibility. Note what is **not** there: quota management, versioning, trash operations, delta change feeds, public link creation, advanced ACL editing. Every one of those is a real requirement for *some* backend, but the rclone post-mortem is unambiguous (§13.1): "the bigger the interface, the weaker the abstraction." These go in optional capability protocols:

```python
# orchestrator/services/cloud/capabilities.py
@runtime_checkable
class SupportsQuota(Protocol):
    async def get_quota(self, h: ProjectFolderHandle) -> "QuotaInfo": ...
    async def set_quota(self, h: ProjectFolderHandle, total_bytes: int) -> None: ...

@runtime_checkable
class SupportsPublicLinks(Protocol):
    async def create_public_link(
        self, h: ProjectFolderHandle, *,
        role: str = "view", expires_at: Optional[datetime] = None,
        password: Optional[str] = None,
    ) -> "PublicLinkHandle": ...
    async def revoke_public_link(self, link: "PublicLinkHandle") -> None: ...

@runtime_checkable
class SupportsTrash(Protocol):
    async def list_trash(self, h: ProjectFolderHandle) -> list["TrashEntry"]: ...
    async def restore_from_trash(self, h: ProjectFolderHandle, entry_id: str) -> None: ...
    async def purge_trash_entry(self, h: ProjectFolderHandle, entry_id: str) -> None: ...

@runtime_checkable
class SupportsDeltaChanges(Protocol):
    async def start_watch(self, h: ProjectFolderHandle) -> "DeltaCursor": ...
    async def poll(self, cursor: "DeltaCursor") -> tuple["DeltaCursor", list["Change"]]: ...
```

Callers that want a capability check it:

```python
if isinstance(main_cloud, SupportsPublicLinks):
    link = await main_cloud.create_public_link(handle, ...)
else:
    raise FeatureNotAvailable("public_links", backend=main_cloud.backend_id)
```

This is the rclone "Features-struct-plus-function-pointers" pattern translated to Python Protocols. In the v1 shipped code, we implement the core protocol on both backends and skip every `Supports*` protocol until a concrete caller needs one. YAGNI discipline on the interface; flexibility on the shape.

#### 4.1.3. Backend factory and router

```python
# orchestrator/services/cloud/__init__.py
from __future__ import annotations
from .base import MainCloudBackend
from .config import MainCloudConfig, load_main_cloud_config
from .nextcloud import NextcloudBackend
from .opencloud import OpenCloudBackend

REGISTRY: dict[str, type[MainCloudBackend]] = {
    "nextcloud": NextcloudBackend,
    "opencloud": OpenCloudBackend,
    # "ms365": MS365Backend,  # added when the adapter lands (§5.3)
}

def build_backend(config: MainCloudConfig) -> MainCloudBackend:
    cls = REGISTRY.get(config.backend_id)
    if not cls:
        raise ValueError(
            f"unknown main cloud backend: {config.backend_id!r} "
            f"(known: {sorted(REGISTRY)})"
        )
    return cls(config)

class MainCloudRouter:
    """Holds one instance of the active backend plus a cache of "legacy"
    backend instances for any rows whose backend differs from the active
    one (non-destructive switching, §6).
    """
    def __init__(self, active: MainCloudBackend) -> None:
        self._active = active
        self._legacy: dict[str, MainCloudBackend] = {}

    @property
    def active(self) -> MainCloudBackend:
        return self._active

    def for_backend(self, backend_id: str) -> MainCloudBackend:
        if backend_id == self._active.backend_id:
            return self._active
        if backend_id not in self._legacy:
            cfg = load_main_cloud_config(backend_override=backend_id)
            self._legacy[backend_id] = build_backend(cfg)
        return self._legacy[backend_id]

    def for_project(self, project_row: dict) -> MainCloudBackend:
        stored = project_row.get("main_cloud_backend")
        return self.for_backend(stored) if stored else self._active

    async def close(self) -> None:
        await self._active.close()
        for b in self._legacy.values():
            await b.close()
```

The orchestrator stores **one** `MainCloudRouter` at module scope (matching the existing `nextcloud_admin` pattern) and calls `router.active.ensure_project_folder(...)` for creates, `router.for_project(row).delete_project_folder(...)` for deletes/updates. For the common case (single backend in use), `_legacy` stays empty.

#### 4.1.4. Why handles and not WebDAV URLs

The v1 draft of this design returned WebDAV URLs directly from the interface (`get_project_folder_webdav_url`, `get_session_folder_webdav_url`). The research on Microsoft Graph is blunt: **MS Graph is not a WebDAV backend**. The `driveItem.webDavUrl` property exists as a compatibility artifact but uses cookie/NTLM auth, not OAuth bearer tokens; the Windows WebClient service is on the deprecation list; and re-fetching `webDavUrl` per item is slower than issuing `/drives/{id}/items/{id}/...` Graph calls directly. Returning WebDAV URLs from the interface either forces every MS365 call through a local `rclone serve webdav` sidecar (extra process, extra auth bridge) or forces the agent-side tool to pretend WebDAV works when the first PUT fails under OAuth.

Opaque handles sidestep this cleanly: the handle returned by `ensure_project_folder` contains *whatever the backend needs to round-trip*, and access URLs are resolved through separate methods (`get_project_folder_browser_url` always returns something; `get_project_folder_webdav_url` returns `Optional[str]`). On the day MS365 lands, the adapter's handle carries `GraphAddress(drive_id, item_id)` in `vendor_meta` and `get_project_folder_webdav_url` returns `None`; the agent-side tool tier grows a `graph_tool` that dispatches on `handle.backend == "ms365"`. No caller of the interface above the tool layer has to change.

This is the Shrine pattern ("`#url` *may return `nil`*") applied in Python. It is also the jclouds pattern (URLs are *one possible* result of an optional `BlobRequestSigner`, not a property of every object). Both libraries made this choice after being burned by the "everything has a URL" assumption; we get to learn from them for free.

### 4.2. Configuration model

Two layers, with the second added in v1 but populated by the settings UI in v2.

**Layer 1 — Deploy-time env vars**, loaded into a Pydantic discriminated union. Per-backend settings classes keep the "Nextcloud field accidentally applied to OpenCloud" class of bug statically impossible:

```python
# orchestrator/services/cloud/config.py
from __future__ import annotations
from typing import Annotated, Literal, Optional, Union
from pydantic import BaseModel, Field, HttpUrl, SecretStr

class NextcloudSettings(BaseModel):
    model_config = {"extra": "forbid"}
    backend_id:        Literal["nextcloud"] = "nextcloud"
    base_url:          HttpUrl
    public_url:        HttpUrl
    admin_user:        str
    admin_password:    SecretStr
    agent_user:        str
    agent_password:    SecretStr
    oidc_client_secret: Optional[SecretStr] = None

class OpenCloudSettings(BaseModel):
    model_config = {"extra": "forbid"}
    backend_id:             Literal["opencloud"] = "opencloud"
    base_url:               HttpUrl                  # internal URL (container-to-container)
    public_url:             HttpUrl                  # browser-facing URL
    keycloak_issuer:        HttpUrl                  # OIDC issuer for service-account tokens
    keycloak_client_id:     str                      # service-account client id
    keycloak_client_secret: SecretStr                # service-account client secret
    admin_role_claim_value: str = "opencloud-admin"  # claim value that maps to admin role
    default_quota_bytes:    Optional[int] = None     # None = server default

class MS365Settings(BaseModel):
    model_config = {"extra": "forbid"}
    backend_id:    Literal["ms365"] = "ms365"
    tenant_id:     str
    client_id:     str
    client_secret: SecretStr
    site_id:       Optional[str] = None              # Sites.Selected path; None = tenant-wide

MainCloudConfig = Annotated[
    Union[NextcloudSettings, OpenCloudSettings, MS365Settings],
    Field(discriminator="backend_id"),
]

def load_main_cloud_config(
    *, backend_override: Optional[str] = None,
) -> MainCloudConfig:
    """Read env vars (with `MAIN_CLOUD_BACKEND` picking the variant),
    validate via Pydantic, return the discriminated union instance.

    Existing `NEXTCLOUD_*` env vars are read as aliases when
    `MAIN_CLOUD_BACKEND=nextcloud` — see §4.4.4 for the alias table.
    """
    ...
```

`extra="forbid"` turns config typos into startup errors. Pydantic's discriminated-union validation means every backend's settings are validated against its own schema; there is no flat dict of strings that silently accepts a key the adapter cannot consume.

**Layer 2 — Runtime override via a `system_settings` table**. This table does not exist in the current schema (confirmed by grep on `orchestrator/database/schema.sql`); this design adds it.

```sql
-- Added to schema.sql following the existing idempotent convention
CREATE TABLE IF NOT EXISTS system_settings (
    key            TEXT PRIMARY KEY,
    value          JSONB NOT NULL,
    credentials_ref TEXT,                  -- env var name OR vault path; never a raw secret
    updated_at     TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_by     TEXT                    -- user id who last wrote this; audit only
);
```

The `main_cloud` row holds:

```json
{
  "backend_id": "opencloud",
  "base_url":   "https://cloud.example.com",
  "public_url": "https://cloud.example.com",
  "keycloak_issuer":    "https://sso.example.com/realms/srw",
  "keycloak_client_id": "opencloud-orchestrator",
  "default_quota_bytes": 10737418240
}
```

`load_main_cloud_config()` reads the row first, falls back to env vars if absent, and then resolves credentials via `credentials_ref`. **Credentials are never stored in the row directly** — only a pointer to where they live (env var name in dev, Vault path in prod). This matches the codebase's existing pattern: there is no credential-indirection layer in Python today; secrets are plain env vars in dev and K8s Secrets backed by Vault/ESO in prod, and Python never sees the vault logic. The `credentials_ref` field is a forward-compatible marker — v1 treats it as an env var name; v3 (if/when a native Vault-aware loader is built) resolves it to a path.

Read/write helpers follow the existing `merge_job_context` pattern in `orchestrator/database/postgres.py` (idempotent JSONB merge with `jsonb_strip_nulls` for patch semantics):

```python
# orchestrator/database/postgres.py — new helpers, mirroring get_user_settings/update_user_settings
async def get_system_setting(self, key: str) -> Optional[dict]: ...
async def set_system_setting(
    self, key: str, value: dict, credentials_ref: Optional[str] = None,
    updated_by: Optional[str] = None,
) -> None: ...
async def merge_system_setting(self, key: str, patch: dict) -> None: ...
```

### 4.3. Default OpenCloud deployment

OpenCloud ships as a `docker-compose.yaml` service, a sibling K8s manifest, and a `before-starting` init script that provisions the service-account identity in Keycloak and validates the reverse-proxy OIDC config. Env-var names below are drawn from the OpenCloud / oCIS documentation and compose overlay; all research-verified against the `opencloud-eu/opencloud-compose` reference as of April 2026.

```yaml
# docker-compose.yaml — new service
opencloud:
  image: docker.io/opencloudeu/opencloud-rolling:latest  # pin a tag in production
  container_name: srw-opencloud
  entrypoint: ["/bin/sh"]
  command: ["-c", "opencloud init || true; opencloud server"]
  environment:
    OC_URL: https://${OPENCLOUD_DOMAIN:-localhost:9200}
    OC_INSECURE: "${OC_INSECURE:-true}"          # dev only; production terminates TLS upstream
    OC_LOG_LEVEL: ${LOG_LEVEL:-info}

    # Bootstrap admin password (used only on first `init`)
    IDM_ADMIN_PASSWORD: ${MAIN_CLOUD_ADMIN_PASSWORD:?must-be-set}

    # External OIDC: Keycloak is the IdP; bundled IDP is disabled
    OC_OIDC_ISSUER: ${KEYCLOAK_INTERNAL_URL}/realms/srw
    PROXY_OIDC_REWRITE_WELLKNOWN: "true"
    PROXY_OIDC_ACCESS_TOKEN_VERIFY_METHOD: jwt

    # USER IDENTITY CLAIM — MUST be `sub` on day 0; changing later corrupts
    # existing spaces and shares (see §5.2.6). `sub` is the only OIDC-stable
    # identifier per OIDC §5.7.
    PROXY_USER_OIDC_CLAIM: sub

    # Auto-provision users on first login from the IdP's claims
    PROXY_AUTOPROVISION_ACCOUNTS: "true"
    PROXY_AUTOPROVISION_CLAIM_USERNAME:    preferred_username
    PROXY_AUTOPROVISION_CLAIM_EMAIL:       email
    PROXY_AUTOPROVISION_CLAIM_DISPLAYNAME: name
    PROXY_AUTOPROVISION_CLAIM_GROUPS:      groups
    GRAPH_USERNAME_MATCH: none              # relaxed username validation for IdP-sourced names

    # Role mapping from claim (the orchestrator's service account gets admin via its group claim)
    PROXY_ROLE_ASSIGNMENT_DRIVER:    oidc
    PROXY_ROLE_ASSIGNMENT_OIDC_CLAIM: roles

    # Disable bundled IDP; keep IDM for writable LDAP (autoprovisioning needs a writable backend)
    OC_EXCLUDE_RUN_SERVICES: idp

  volumes:
    - opencloud_config:/etc/opencloud
    - opencloud_data:/var/lib/opencloud
    - ./docker/opencloud/setup-opencloud.sh:/docker-entrypoint-hooks.d/setup-opencloud.sh:ro
  ports:
    - "${OPENCLOUD_PORT:-9200}:9200"
  restart: unless-stopped

volumes:
  opencloud_config:
  opencloud_data:
```

`docker/opencloud/setup-opencloud.sh` does the init work analogous to `setup-nextcloud.sh`:

1. `opencloud init || true` — creates `/etc/opencloud/opencloud.yaml` on first run with random internal secrets; no-op on re-run unless `--force-overwrite`.
2. Verify the OIDC discovery document is reachable (`curl -sf $OC_OIDC_ISSUER/.well-known/openid-configuration`) before continuing. Fail loudly if Keycloak is not yet up.
3. Wait for the LibreGraph endpoint to answer (`curl -sf $OC_URL/graph/v1.0/me` as admin via IdM).
4. Create the `srw-agents` group via `POST /graph/v1.0/groups` with `{"displayName": "srw-agents"}`.
5. Validate the Keycloak client exists for the orchestrator service account (out-of-band — the Keycloak realm config is the source of truth; the script only checks, it does not create the client). The `client_id` is `opencloud-orchestrator` with `client_credentials` grant enabled; it is assigned to a Keycloak group whose name is exactly `admin_role_claim_value` from config (default: `opencloud-admin`).

A sibling `deployment/19-opencloud.yaml` mirrors `deployment/19-nextcloud.yaml` for K8s: ConfigMap for the setup hook, PVCs for `opencloud_config` and `opencloud_data`, Deployment with the env vars above, Service on port 9200.

Nextcloud is **kept** in the compose file but moved into a profile so it does not start by default:

```yaml
nextcloud:
  profiles: ["nextcloud"]
  # ...existing service definition unchanged
```

Deployments that explicitly opt into Nextcloud start it via `podman-compose --profile nextcloud up -d`.

#### 4.3.1. NEXTCLOUD_* as aliases

Existing `NEXTCLOUD_*` env vars stay valid and map onto `MAIN_CLOUD_*` whenever `MAIN_CLOUD_BACKEND=nextcloud`. The loader (§4.2) reads:

| Generic name | Nextcloud alias | Used when |
|---|---|---|
| `MAIN_CLOUD_BACKEND` | — | Always; defaults to `opencloud` for new installs, `nextcloud` for existing |
| `MAIN_CLOUD_URL` | `NEXTCLOUD_URL` | `backend_id == "nextcloud"` |
| `MAIN_CLOUD_PUBLIC_URL` | `NEXTCLOUD_PUBLIC_URL` | same |
| `MAIN_CLOUD_ADMIN_USER` | `NEXTCLOUD_ADMIN_USER` | same |
| `MAIN_CLOUD_ADMIN_PASSWORD` | `NEXTCLOUD_ADMIN_PASSWORD` | same |
| `MAIN_CLOUD_AGENT_USER` | `NEXTCLOUD_AGENT_USER` | same |
| `MAIN_CLOUD_AGENT_PASSWORD` | `NEXTCLOUD_AGENT_PASSWORD` | same |

Existing Nextcloud deployments upgrade to the new code without touching their `.env`. They are implicitly opted into `MAIN_CLOUD_BACKEND=nextcloud` by the loader's "legacy Nextcloud mode" heuristic (no `MAIN_CLOUD_BACKEND` set + at least one `NEXTCLOUD_*` var set → default to `nextcloud`).

### 4.4. Schema changes

Three tables touched plus the new `system_settings` table from §4.2. All migrations follow the existing idempotent pattern in the repo:

```sql
-- projects: new generic column, keep legacy for one release
DO $$ BEGIN
    ALTER TABLE projects ADD COLUMN main_cloud_backend TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE projects ADD COLUMN main_cloud_folder_handle TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- Backfill from the existing column; a UUID-typed cast is fine because
-- Nextcloud returns numeric folder IDs which stringify cleanly.
UPDATE projects
   SET main_cloud_backend = 'nextcloud',
       main_cloud_folder_handle = nextcloud_folder_id::text
 WHERE nextcloud_folder_id IS NOT NULL
   AND main_cloud_folder_handle IS NULL;

-- threads (persistent threads) — three new columns
DO $$ BEGIN
    ALTER TABLE threads ADD COLUMN main_cloud_backend TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE threads ADD COLUMN main_cloud_session_handle TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE threads ADD COLUMN main_cloud_share_handle TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

UPDATE threads
   SET main_cloud_backend = 'nextcloud',
       main_cloud_session_handle = nc_session_folder,
       main_cloud_share_handle = nc_share_id::text
 WHERE nc_session_folder IS NOT NULL
   AND main_cloud_session_handle IS NULL;

-- system_settings — new table
CREATE TABLE IF NOT EXISTS system_settings (
    key             TEXT PRIMARY KEY,
    value           JSONB NOT NULL,
    credentials_ref TEXT,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_by      TEXT
);
```

The handle columns are `TEXT` because every backend serializes its handle differently (Nextcloud `str(int)`, OpenCloud `<provider>!<space>` composite with the `!` delimiter preserved, MS Graph `b!<base64url>` concatenated with the item id). The orchestrator never parses them — it uses `ProjectFolderHandle.from_db(text)` / `.to_db()` which are adapter-aware.

The `main_cloud_backend` column on each row is the central rule for non-destructive switching (§6): it remembers which backend created the row so the router can dispatch subsequent operations on that row to the correct adapter even after the deployment-wide active backend has changed.

The legacy `nextcloud_folder_id`, `nc_session_folder`, `nc_share_id` columns stay for one release as read-only fallbacks. They are dropped in the release after Phase 1 ships.

### 4.5. Lifecycle integration points

Every endpoint that currently calls `nextcloud_admin` is rewritten to call `main_cloud_router`. The signatures align so the changes are mostly mechanical, with one important routing rule.

| Endpoint | Today | After |
|---|---|---|
| `POST /api/users` (line 11085) | `nextcloud_admin.get_user_home_webdav_url()` + `agent_user/password` | `router.active.resolve_user_identity()` + `router.active.get_user_home()` → create datasource against the handle |
| `POST /api/projects` (line 11204) | `nextcloud_admin.ensure_group()` + `create_project_folder()` | `router.active.ensure_group()` + `router.active.ensure_project_folder()`. Record `main_cloud_backend = router.active.backend_id` and `main_cloud_folder_handle = handle.to_db()` on the project row |
| `GET /api/projects/{id}` (line 11275) | `nextcloud_admin.get_folder_browser_url()` / `get_user_home_browser_url()` | `router.for_project(row).get_project_folder_browser_url(handle)` / `get_user_home().browser_url` |
| `DELETE /api/projects/{id}` (line 11363) | `nextcloud_admin.delete_project_folder(int)` | `router.for_project(row).delete_project_folder(handle)` |
| `POST /api/projects/{id}/members` (line 11421) | `nextcloud_admin.resolve_nc_username()` + `add_user_to_group()` + `refresh_group_folder_access()` | Same sequence against `router.for_project(row)` — **not** `router.active`. The refresh call is still required for Nextcloud; it is a no-op for OpenCloud and MS365 |
| `DELETE /api/projects/{id}/members/{uid}` (line 11505) | `nextcloud_admin.resolve_nc_username()` + `remove_user_from_group()` | Same, against the project's backend |
| `POST /api/persistent/threads` (line 8993, `_setup_nextcloud`) | `nextcloud_admin.create_session_folder()` + `share_folder_with_user()` | `_setup_main_cloud()` calls `router.active.ensure_session_folder()` + `share_session_folder()`. Records backend id on the thread row |
| `DELETE /api/persistent/threads/{id}` (line 9108) | `nextcloud_admin.delete_folder(nc_folder)` | `router.for_thread(row).revoke_session_share()` + `delete_session_folder()` |
| `lifespan()` (line 2316) | `await nextcloud_admin.ensure_initialized()` | `await router.active.ensure_initialized()` + health check + store router on `app.state.main_cloud_router` |

**The routing rule:** for **create** operations, always use `router.active`. For **read / update / delete** operations on existing rows, look up the row's `main_cloud_backend` and use `router.for_project(row)`. This is what makes switching non-destructive — see §6.

**OpenCloud group-membership rule (post-2026-04-20).** OpenCloud's proxy reconciles a user's LibreGraph group memberships to exactly match the `groups` claim in their Keycloak token on every login. A direct `backend.add_user_to_group(...)` for project access therefore gets **deleted** on the user's next auth — which is every time a new OpenCloud tab is opened. Project membership must flow through the Keycloak project group (`KeycloakGroupSync.ensure_project_group` / `add_user_to_project_group` / `remove_user_from_project_group`), which shares the same flat name (`project-{uuid}`) as the LibreGraph group. The orchestrator still calls `backend.ensure_group(group_name)` + `backend.ensure_project_folder(group_id=group_name)` at project creation so the LibreGraph group exists and is invited to the Space; the OpenCloud reconciler then populates membership from the token claim on each login. The cockpit triggers a Keycloak token refresh (`KeycloakService.forceRefreshToken`) after a successful `POST /api/projects` so the creator sees the new Space without having to log out. `backend.add_user_to_group` remains available for realm-level groups such as `opencloudAdmin`, where the group *is* in the Keycloak token.

Pseudo-code for a delete site:

```python
backend = main_cloud_router.for_project(project_row)
handle = ProjectFolderHandle.from_db(project_row["main_cloud_folder_handle"])
await backend.delete_project_folder(handle)  # if_exists=True by default
```

For deployments that only ever use one backend (the common case), `router.for_project` always returns `router.active` and the `_legacy` cache in the router stays empty. The cache only matters during the post-switch window when old rows still reference the previous backend.

### 4.6. Connection lifecycle and pooling

Each backend implementation owns one long-lived `httpx.AsyncClient`, created inside `ensure_initialized()` during FastAPI `lifespan` startup, closed inside `close()` during shutdown. One client per backend instance — **not** per request, not per call.

This matches the existing `NextcloudAdmin` pattern (single `httpx.AsyncClient` stored as `self._client`) and the FastAPI lifespan convention already in `orchestrator/main.py:2287–2460`. The research is unanimous: per-request client creation defeats connection pooling and is a well-known performance anti-pattern.

Explicit timeout configuration for every client:

```python
httpx.AsyncClient(
    base_url=self.config.base_url,
    timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=10.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    headers={
        "User-Agent": "SuperhumanRemoteWorker/0.1 main-cloud-backend",
        # backend-specific headers applied on top: OCS-APIRequest for Nextcloud, etc.
    },
)
```

The four timeouts matter independently. `pool` is the sneaky one — if tasks get cancelled and connections are not released, pool timeout lets subsequent requests fail fast instead of hanging. Defaults have been tuned up from httpx's defaults based on typical Nextcloud/OpenCloud response times.

### 4.7. Error taxonomy

Every public method on `MainCloudBackend` raises a single exception type, `CloudBackendError`, carrying a discriminator enum and preserving vendor context for debugging. This is the Stripe error-handling pattern: *map HTTP status first (infrastructure concern), then error types (domain concern); preserve the raw response alongside typed properties*.

```python
# orchestrator/services/cloud/errors.py
from __future__ import annotations
from enum import StrEnum
from typing import Any, Optional

class CloudBackendErrorKind(StrEnum):
    NOT_FOUND             = "not_found"
    ALREADY_EXISTS        = "already_exists"
    PERMISSION_DENIED     = "permission_denied"
    AUTHENTICATION_FAILED = "authentication_failed"
    QUOTA_EXCEEDED        = "quota_exceeded"
    THROTTLED             = "throttled"         # 429 or vendor equivalent
    TIMEOUT               = "timeout"
    UNAVAILABLE           = "unavailable"       # 5xx, connection refused, circuit open
    INVALID_REQUEST       = "invalid_request"   # 400 — our fault
    NOT_SUPPORTED         = "not_supported"     # backend cannot perform this op
    UNKNOWN               = "unknown"

class CloudBackendError(Exception):
    def __init__(
        self,
        kind: CloudBackendErrorKind,
        message: str,
        *,
        backend: str,
        vendor_code: Optional[str] = None,
        vendor_message: Optional[str] = None,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        raw: Optional[dict[str, Any]] = None,
        retryable: bool = False,
    ) -> None:
        self.kind = kind
        self.backend = backend
        self.vendor_code = vendor_code
        self.vendor_message = vendor_message
        self.status_code = status_code
        self.request_id = request_id
        self.raw = raw
        self.retryable = retryable
        super().__init__(f"[{backend}:{kind.value}] {message}")

class FeatureNotAvailable(CloudBackendError):
    """Raised when a caller invokes a capability the backend does not support."""
    def __init__(self, feature: str, *, backend: str) -> None:
        super().__init__(
            CloudBackendErrorKind.NOT_SUPPORTED,
            f"backend {backend!r} does not support {feature!r}",
            backend=backend,
        )
```

Why one exception type and not one subclass per kind? Subclassing works in Python but multiplies pain once there are 10+ error shapes (Stripe hit this and eventually moved to the enum pattern). Callers pattern-match on `e.kind`; the discriminator is a cheap enum value, not a class-hierarchy walk. The `raw` field preserves the original vendor response so debug tooling can still dig when a mapped kind is too coarse.

Each backend has a private `_map_error(exc_or_response) -> CloudBackendError` translator. Parity tests (§4.9) assert a matrix of vendor errors → correct kinds, one row per (backend, vendor error, expected kind).

**Phase 1 status.** `CloudBackendError` and `CloudBackendErrorKind` live in `orchestrator/services/cloud/errors.py` and are importable, but the Phase 1 Nextcloud adapter does **not raise them yet** — soft failures still follow the legacy log-and-return-None path so the call-site rewrite in `orchestrator/main.py` stays behavior-identical. Phase 1.5 (§8) wires `_map_error` into every `httpx` boundary, switches `ensure_*` return types to non-Optional `Handle`, and updates the `try/except Exception` blocks in `main.py` to catch `CloudBackendError` specifically. OpenCloud (Phase 2) is built against the tightened contract from day one, so the parity tests that assert `ensure_*` never returns `None` will fire against any Phase 1.5 regression.

### 4.8. Idempotency and eventual consistency

**Rule 1 — Every method named `ensure_*` is idempotent.** Calling `ensure_project_folder(name="Foo", group_id="g1")` twice returns the same handle both times. The adapter is allowed to consult its own cache (keyed by `(project_name, group_id)`) or re-query the backend; either is fine as long as the method is observably idempotent.

**Rule 2 — Every method named `delete_*` is idempotent against missing resources.** `delete_project_folder(handle)` on a handle whose backend resource has already been removed is a no-op, not an error. The `if_exists=True` default on every delete makes this explicit. Callers who *want* the error can pass `if_exists=False`.

**Rule 3 — Optional idempotency keys.** `ensure_*` methods accept an optional `idempotency_key: str | None` that the backend passes through to the vendor API if the vendor supports it (Nextcloud's `OC-Request-ID`, OpenCloud's soon-to-be-added equivalent, MS Graph's `client-request-id`). The orchestrator generates the key before the first attempt by hashing `(operation_name, primary_argument)` so retries use the same key. This is the Stripe pattern applied one layer up.

**Rule 4 — Wait-and-verify after create.** Several backends are eventually consistent:
- Nextcloud needs a filecache warm before a newly created Group Folder shows up in PROPFIND from the service account.
- OpenCloud's space creation is synchronous but the OIDC group-to-space propagation has a small window.
- **Microsoft Graph takes 5–60 seconds** to provision a SharePoint site and drive after creating an M365 group, and `GET /groups/{id}/drive` returns 404 during that window.

The adapter handles this by polling after create with exponential backoff and jitter, capped at a reasonable number of attempts, using `tenacity`:

```python
from tenacity import (
    AsyncRetrying, retry_if_exception_type, wait_exponential, wait_random, stop_after_attempt,
)

async def _wait_until_visible(self, handle: ProjectFolderHandle) -> None:
    async for attempt in AsyncRetrying(
        retry=retry_if_exception_type(CloudBackendError),
        wait=wait_exponential(multiplier=1, min=0.5, max=8) + wait_random(0, 0.5),
        stop=stop_after_attempt(6),
        reraise=True,
    ):
        with attempt:
            await self._get_folder(handle)  # raises NOT_FOUND until visible
```

Each `ensure_*` method documents its visibility guarantee. The default contract is "returns after the resource is visible to subsequent `get_*` calls on the same backend"; callers who need weaker guarantees use a separate `*_async` variant (not in v1 — add if a caller needs it).

### 4.9. Rate limits, retries, and circuit breaking

Each backend has different rate-limit behavior. The adapter pattern is: map the vendor's rate-limit response to `CloudBackendErrorKind.THROTTLED`, honor any `Retry-After` header, and expose retry decisions through `CloudBackendError.retryable`.

**Nextcloud.** NC 30.0.10+ enforces **20 new shares per 10 minutes per user** on `POST /ocs/v2.php/apps/files_sharing/api/v1/shares`. This is a client-side leaky-bucket problem: the adapter maintains a token bucket at ~15 req / 10 min (conservative, under the server limit) and queues calls locally before issuing them. On 429 from the server, honor `Retry-After` if present (rarely set, check anyway), otherwise exponential backoff. Also: configure the service account on the brute-force allow list via `config.php` if running at self-hosted scale; see [config options](https://docs.nextcloud.com/server/latest/admin_manual/configuration_server/bruteforce_configuration.html).

**OpenCloud.** Self-hosted OpenCloud has no documented rate limits on LibreGraph. In practice, the reverse proxy in front of the instance may enforce per-IP connection limits, and the auth-service token cache can amplify LDAP lookups under high concurrency. The adapter retries transient 5xx on LibreGraph with exponential backoff and does not impose a client-side bucket.

**Microsoft Graph / SharePoint Online.** Throttling for file operations flows through to SharePoint Online's per-app-per-tenant resource-unit limits (baseline 1,250 RU/min for a small tenant; ~600 req/min avg because most operations cost 2 RU and permission ops cost 5). Always honor `Retry-After`; use `RateLimit-Limit` / `RateLimit-Remaining` / `RateLimit-Reset` as preemptive warnings when present. Decorate the `User-Agent` with `NONISV|SuperhumanRemoteWorker|Orchestrator/0.1` — Microsoft deprioritizes undecorated traffic.

Retry policy shared across adapters, using `tenacity`:

```python
# orchestrator/services/cloud/retry.py
from tenacity import (
    AsyncRetrying, retry_if_exception_type, wait_exponential, wait_random,
    stop_after_attempt,
)
import httpx

RETRYABLE_HTTPX = (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)

def retryable_policy(max_attempts: int = 4):
    return AsyncRetrying(
        retry=(
            retry_if_exception_type(RETRYABLE_HTTPX)
            | retry_if_exception_type(CloudBackendError)  # filtered by `.retryable`
        ),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8) + wait_random(0, 0.5),
        stop=stop_after_attempt(max_attempts),
        reraise=True,
    )
```

**Circuit breaker.** Each backend instance holds a `circuitbreaker`-style state machine keyed by operation class (read vs write). When the breaker opens, subsequent calls fail fast with `CloudBackendError(kind=UNAVAILABLE, retryable=True)`. The job dispatcher in `orchestrator/main.py` already knows how to pause and re-dispatch jobs on pausable failures (the `paused` job status) — the breaker-open error triggers the same path. At the user-facing API layer, the error maps to HTTP 503. The `/health/cloud` endpoint surfaces the breaker state.

### 4.10. Testing strategy

Four tiers, following the layered approach from the cloud-connector literature:

1. **Contract tests** (`tests/cloud/test_backend_contract.py`). Parametrized over every backend including a `FakeMainCloudBackend` in-memory implementation. Asserts the common invariants: `ensure_*` is idempotent, `delete_*` is a no-op on missing, handles round-trip through `to_db` / `from_db`, eventual-consistency waits complete, error kinds are mapped correctly for a fixed vendor-error matrix. This is the Shrine `Shrine::Storage::Linter` pattern.
2. **Unit tests with the fake.** Every caller in `orchestrator/main.py` that uses `main_cloud_router` is tested against `FakeMainCloudBackend` — fast, deterministic, and fails loudly if the caller assumes a behavior the real backends cannot provide (the contract test is what keeps the fake honest).
3. **VCR regression tests** (`tests/cloud/cassettes/`). Hand-recorded `httpx` cassettes via `pytest-recording` / `vcrpy` against a running OpenCloud and Nextcloud instance, committed to the repo. CI runs with `--vcr-record=none` — any recording drift breaks the build. Recordings are re-generated by a developer with a local instance up when adding or changing behavior.
4. **Local integration suite** (marked `pytest -m integration`, not in CI by default). `testcontainers-python` boots ephemeral OpenCloud and Nextcloud containers. Runs the full contract test suite against them. Used as a pre-release gate and for local debugging when cassettes go stale.

What we do not use: `Mock`/`MagicMock` for `MainCloudBackend` in tests (loses type safety, lets impossible behaviors slip through), Pact-style consumer-driven contracts (we do not control the cloud vendor), LocalStack (AWS-specific).

### 4.11. Telemetry

Two layers:

1. **HTTP transport.** OpenTelemetry auto-instrumentation via `opentelemetry-instrumentation-httpx` produces a span per HTTP call with URL, status, latency, and retry count. Zero code changes.
2. **Domain operations.** A thin `@instrument_backend_op` decorator applied to every method on every backend emits a `cloud_backend.{kind}.{op}` span plus a counter/histogram labeled `(backend, op, status)`. Applied manually per method (about twenty methods — not worth a metaclass). Emits one structured log line per operation including `backend`, `op`, `status`, `latency_ms`, `request_id`, and `project_id` when applicable. Structured logs ship to MongoDB audit if the orchestrator convention prefers that over stdout.

Error kinds and latency histograms give us "is the backend sick?" at a glance without instrumenting every call site in `main.py`.

## 5. Backend Implementations

### 5.1. NextcloudBackend (refactor)

`orchestrator/services/cloud/nextcloud.py` is created by moving and renaming `NextcloudAdmin` into a class that implements `MainCloudBackend`. The body of each method is largely unchanged; signatures are massaged into the handle-based shape (e.g., int folder IDs become `ProjectFolderHandle(backend="nextcloud", native_id=str(id), vendor_meta={"mountpoint": name})` on the way out, parsed back on the way in).

Method renames — **shipped in Phase 1:**

| `NextcloudAdmin` method | `NextcloudBackend` method |
|---|---|
| `resolve_nc_username` | `resolve_user_identity` |
| `create_session_folder` → returns `bool` | `ensure_session_folder` → returns `Optional[SessionFolderHandle]` |
| `share_folder_with_user` → returns `int` | `share_session_folder` → returns `Optional[ShareHandle]` |
| `delete_folder` | `delete_session_folder` / `delete_project_folder` (split by handle type) |
| `create_project_folder` → returns `Optional[int]` | `ensure_project_folder` → returns `Optional[ProjectFolderHandle]` |
| `refresh_group_folder_access` | `refresh_project_folder_access` |
| `get_folder_webdav_url` | `get_project_folder_webdav_url` |
| `get_user_home_webdav_url` | `get_user_home().webdav_url` |
| `get_user_home_browser_url` | `get_default_home_browser_url` |
| `agent_user` / `agent_password` | `webdav_credentials` (dict property) |

The old file `orchestrator/services/nextcloud_admin.py` is deleted in the same commit. `NextcloudBackend.__init__` reads the same env vars the legacy class did — no Pydantic loading in Phase 1 (see §4.2 for the Phase 2 plan). **Update (2026-06-08, `main_cloud.md` Issue 12):** now historical — `NextcloudBackend` consumes a validated `NextcloudSettings` (built by `load_main_cloud_config` in `build_backend`), matching `OpenCloudBackend`, so the `MAIN_CLOUD_*` aliases / DB overlay / `credentials_ref` apply to it too.

Research-driven adjustments (see §13.3 for the full Nextcloud gotcha catalog). **Status column** marks Phase 1 shipped (✓) vs Phase 1.5 deferred (↷):

- ↷ **`OCS-APIRequest: true` header is the #1 silent failure.** Missing it yields a 302 redirect to the HTML login page, which `httpx` follows by default, producing a 200 with an HTML body that looks like success. The existing `NextcloudAdmin` sets this header correctly and the refactored class inherits that behavior unchanged, but the planned `response_is_html(r)` assertion on the response wrapper is **deferred to Phase 1.5**. Phase 1 is strictly behavior-preserving; adding a new assertion would be a new failure mode.
- ✓ **OCS v1 vs v2 status codes.** v1.php always returns HTTP 200 with the real status in `ocs.meta.statuscode` (success = `100`). v2.php uses real HTTP status with envelope `statuscode` duplicated (success = `200`). Documentation is inconsistent; the parser (inherited unchanged from NextcloudAdmin) already accepts both.
- ↷ **User lookup via `/users/details?search=<email>` + client-side exact-match filter** is more reliable than the current two-pass strategy. **Deferred to Phase 1.5** — the Phase 1 NextcloudBackend preserves the legacy two-pass (exact-ID lookup, then search) because switching lookup strategies is a behavioral change that needs its own cassette regression test.
- ✓ **Group Folders #57445 is still open on NC 31.** The `refresh_project_folder_access` workaround (delete group share → re-add) is shipped in Phase 1 and exposed at the `MainCloudBackend` interface level (no-op on non-Nextcloud backends). This was implemented because the existing call site in `POST /api/projects/{id}/members` depended on it; removing it would have been a behavior regression.
- ↷ **20-shares-per-10-min rate limit (NC 30.0.10+).** A client-side leaky bucket at ~15/10min is **deferred to Phase 1.5** for the same reason as the user-lookup switch — it is a new behavior and needs its own test coverage.
- ↷ **Service account auth is an app password, not the admin password.** **Deferred — runbook only.** No code change is required (the adapter consumes whatever `NEXTCLOUD_AGENT_PASSWORD` holds); the ops team sets the password to an app password out-of-band. Phase 1.5 adds the recommendation to the bootstrap runbook in `docs/operations/` (not yet written).
- ↷ **Nextcloud bug #4127** can crash the entire instance if a Group Folder's `root_id` points at a missing filecache entry. **Deferred to Phase 1.5** — the `ensure_initialized` health check does not yet detect this class of corruption.

Phase 1 parity tests run against the existing Nextcloud fixtures unchanged. No new tests are added in Phase 1 — the existing pytest suite continues to pass because the handle reshape is internal and the call-site rewrites are mechanical. Phase 1.5 introduces `tests/cloud/test_backend_contract.py` with a `FakeMainCloudBackend` implementation parametrized against every adapter.

### 5.2. OpenCloudBackend (new)

`orchestrator/services/cloud/opencloud.py` is the first net-new adapter. It speaks LibreGraph (a Microsoft-Graph subset under `/graph/v1.0/` and `/graph/v1beta1/`) exclusively — the legacy OCS endpoints that both OpenCloud and Nextcloud expose are deliberately not used.

The authoritative research for this adapter is in §13.2 and a snapshot of the LibreGraph OpenAPI spec (`opencloud-eu/libre-graph-api` v1.0.8); below is the condensed operational summary.

#### 5.2.1. Authentication

The orchestrator authenticates as a service account via **Keycloak's `client_credentials` OAuth2 flow**, not via OpenCloud's internal `OCIS_SERVICE_ACCOUNT_*` mechanism (that is an internal gRPC path not exposed as a public HTTP contract, and would require the orchestrator to sit inside the same cluster).

```python
async def _get_service_token(self) -> str:
    r = await self._client.post(
        f"{self.config.keycloak_issuer}/protocol/openid-connect/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     self.config.keycloak_client_id,
            "client_secret": self.config.keycloak_client_secret.get_secret_value(),
            "scope":         "openid profile",
        },
    )
    r.raise_for_status()
    token_data = r.json()
    # Cache with ~30s clock-skew margin
    self._token_expires_at = time.monotonic() + token_data["expires_in"] - 30
    return token_data["access_token"]
```

The Keycloak client is named `opencloud-orchestrator`, has `serviceAccountsEnabled: true`, and is assigned to a Keycloak group whose name matches `config.admin_role_claim_value` (default: `opencloud-admin`). OpenCloud's `proxy-config.yaml` includes a role mapping `{role_name: admin, claim_value: opencloud-admin}` so tokens issued to this client carry the admin role.

#### 5.2.2. API surface mapping

| Interface method | LibreGraph call |
|---|---|
| `resolve_user_identity(email, display_name)` | `GET /graph/v1.0/users/{email}` (direct lookup) → fall back to `GET /graph/v1.0/users?$search="{email}"` → client-side exact match on `mail` field. **`$filter=mail eq '...'` is NOT supported** — the only filter expressions LibreGraph accepts on `/users` are `memberOf/any(...)` and `appRoleAssignments/any(...)`. |
| `get_user_home(user_id)` | `GET /graph/v1.0/users/{id}?$expand=drive` → return `drive.root.webDavUrl` as `webdav_url`, `drive.webUrl` as `browser_url` |
| `ensure_group(group_id)` | `GET /graph/v1.0/groups?$search="{group_id}"` + client-side exact filter → if missing, `POST /graph/v1.0/groups {"displayName": group_id}`. Persist `group.id` (a UUID) as the stable handle |
| `add_user_to_group(user_id, group_id)` | `POST /graph/v1.0/groups/{group-id}/members/$ref` body `{"@odata.id": "{base_url}/graph/v1.0/users/{user-id}"}` |
| `remove_user_from_group(user_id, group_id)` | `DELETE /graph/v1.0/groups/{group-id}/members/{user-id}/$ref` |
| `ensure_project_folder(project_name, group_id)` | `POST /graph/v1.0/drives {"name": project_name, "quota": {"total": default_quota}}` (creates a `driveType: project` Space; `driveType` is auto-set, not user-controlled). Response contains `id` (the composite `<storageProviderId>!<spaceId>` — preserve the `!` exactly as-is). Then `POST /graph/v1beta1/drives/{drive-id}/root/invite` with `{"recipients": [{"@libre.graph.recipient.type": "group", "objectId": group-id}], "roles": [<editor-role-uuid>]}` to grant group access. **The `@libre.graph.recipient.type: "group"` annotation is required** — without it the server assumes `user` and fails |
| `delete_project_folder(handle)` | Two-step: `DELETE /graph/v1.0/drives/{drive-id}` without `Purge` header (disables the space), then `DELETE /graph/v1.0/drives/{drive-id}` with `Purge: T` (permanently deletes). Deleting an enabled space in one step returns 400. **This is not documented in the spec** — discovered from Web UI traffic. |
| `refresh_project_folder_access(handle, group_id)` | No-op. OpenCloud propagates group membership changes to space grants automatically |
| `get_project_folder_webdav_url(handle)` | Read `vendor_meta["webdav_url"]` (persisted from the create response's `root.webDavUrl`) and URL-encode the path segment. **Never hand-construct** the URL: the composite drive ID has `!` / `$` delimiters that must be percent-encoded (`!` → `%21`, `$` → `%24`), which most HTTP clients skip by default |
| `ensure_session_folder(session_id)` | Resolve the "agent home" project space (a dedicated Space created at `ensure_initialized` time, analogous to Nextcloud's agent-service user home) → `MKCOL` via WebDAV under it at `sessions/{session_id}/`. The WebDAV base URL is `{base_url}/dav/spaces/{drive-id}/` (note: **`/dav/spaces/`**, not `/remote.php/dav/...`) |
| `share_session_folder(handle, user_id)` | `POST /graph/v1beta1/drives/{drive-id}/items/{item-id}/invite` with `{"recipients": [{"@libre.graph.recipient.type": "user", "objectId": user-id}], "roles": [<editor-role-uuid>]}`. Response `value[0].id` is the stable permission id — persist as `ShareHandle.native_id` |
| `revoke_session_share(share)` | `DELETE /graph/v1beta1/drives/{drive-id}/items/{item-id}/permissions/{permission-id}` |

#### 5.2.3. Role UUID resolution

LibreGraph's sharing API identifies roles by **UUID, not by name**. The spec explicitly says: *"clients MUST treat the value as an opaque identifier"* — and the UUIDs for Viewer / Editor / Manager / File Drop *currently* match the server source hardcoded defaults but are not spec-guaranteed to be stable across deployments or versions.

The adapter resolves role names to UUIDs at startup and caches them:

```python
async def _load_role_catalog(self) -> dict[str, str]:
    """Fetch the role catalog. Returns {displayName: roleId}."""
    r = await self._graph_get("/v1beta1/roleManagement/permissions/roleDefinitions")
    roles = {role["displayName"]: role["id"] for role in r.json()["value"]}
    # Fallback: if the endpoint is missing on older releases, read from any
    # existing permissions response's @libre.graph.permissions.roles.allowedValues
    return roles

async def _role_id(self, name: str) -> str:
    if name not in self._role_cache:
        self._role_cache = await self._load_role_catalog()
    if name not in self._role_cache:
        raise CloudBackendError(
            CloudBackendErrorKind.NOT_SUPPORTED,
            f"role {name!r} not in OpenCloud role catalog",
            backend="opencloud",
        )
    return self._role_cache[name]
```

Cache invalidated on 404 from an invite call (treat a missing role as "refresh and retry once").

#### 5.2.4. Sharing v1beta1 stability

All sharing endpoints (`invite`, `createLink`, `permissions`, `roleDefinitions`) live under `/graph/v1beta1/`. Despite the `v1beta1` prefix, they are **production-stable**: they are what the OpenCloud Web UI calls, the maintainers' stated intent is to promote them to `v1.0` with the shape intact, and there is no parallel `v1.0` sharing API to fall back to. The adapter treats `v1beta1` as required and documents the upstream stability assessment in code comments.

#### 5.2.5. Pagination

LibreGraph follows the standard OData `@odata.nextLink` pattern. The adapter has a reusable helper:

```python
async def _list_all(self, start_url: str, params: dict | None = None) -> list[dict]:
    items, url, current_params = [], start_url, params
    while url:
        r = await self._graph_get(url, params=current_params)
        body = r.json()
        items.extend(body.get("value", []))
        url = body.get("@odata.nextLink")
        current_params = None  # nextLink URLs carry all params
    return items
```

`$top` and `$skip` are declared in the spec but inconsistently honored across server versions; always prefer `@odata.nextLink`.

#### 5.2.6. OIDC `sub` claim must be set on day 0

The most important operational gotcha. OpenCloud's `PROXY_USER_OIDC_CLAIM` historically defaulted to `preferred_username` but this was flagged as a bug ([`owncloud/ocis#6664`](https://github.com/owncloud/ocis/issues/6664)): per OIDC §5.7, *"the only guaranteed unique identifier for a given End-User is the combination of the `iss` Claim and the `sub` Claim."*

If a deployment starts with `preferred_username` and later switches to `sub`, **all existing spaces, shares, and group memberships become orphaned** because the internal CS3 user ID changes. Set `PROXY_USER_OIDC_CLAIM=sub` on day 0 and never change it. The `setup-opencloud.sh` init script verifies this is set before reporting readiness; the `ensure_initialized` health check re-verifies at orchestrator startup.

#### 5.2.7. Default NextcloudBackend-style helper methods reused

Raw `httpx.AsyncClient` with a small typed response wrapper, not `msgraph-sdk`-style generated clients (see §13.4 for the discussion). The adapter ships roughly 400–600 LoC.

### 5.3. MS365Backend (sketched, not in v1)

Out of scope for v1 — included here to validate that the interface accommodates Microsoft 365 without a subsequent rewrite. The research (§13.4) is clear that MS365 differs from OpenCloud / Nextcloud in two architectural ways:

1. **It is not a WebDAV backend.** `driveItem.webDavUrl` exists but is a compatibility artifact with cookie/NTLM auth that does not work with OAuth app-only tokens. The adapter speaks Microsoft Graph (`/v1.0/drives/...`, `/v1.0/groups/...`, `/v1.0/users/...`) and returns `None` from `get_*_webdav_url`. The agent-side tool layer grows a Graph-speaking variant (the `src/tools/webdav/` refactor is flagged in §10).
2. **The "project folder" primitive is a per-project Microsoft 365 (Unified) group**, not a folder inside a drive. Creating a group auto-provisions a SharePoint site and a document-library drive within 5–60 seconds. Adding/removing members on the group is how access is controlled; the drive's permissions follow. Session folders are regular folders *inside* the project group's drive.

| Interface method | Microsoft Graph mapping |
|---|---|
| `resolve_user_identity(email)` | `GET /v1.0/users?$filter=mail eq '{email}'` → persist `user.id` (a GUID). `mail` and `userPrincipalName` are not stable; resolve once and store the id |
| `get_user_home(user_id)` | `GET /v1.0/users/{user-id}/drive` → OneDrive for Business drive. `webdav_url` is `None` |
| `ensure_group(group_id)` | `POST /v1.0/groups` with `{"groupTypes": ["Unified"], "mailEnabled": true, "securityEnabled": false, "displayName": ..., "mailNickname": ..., "visibility": "Private", "owners@odata.bind": [...]}`. **`owners@odata.bind` is required on app-only creates** — without it, the M365 group is created anonymously and SharePoint never provisions the backing site, so `/groups/{id}/drive` returns 404 forever |
| `ensure_project_folder(project_name, group_id)` | Ensures the group exists, then polls `GET /v1.0/groups/{group-id}/drive` until 200 (5–60s with exponential backoff). The handle carries `vendor_meta = {"drive_id": ..., "root_item_id": ..., "group_id": ...}` |
| `ensure_session_folder(session_id)` | `POST /v1.0/drives/{drive-id}/items/{root-item-id}/children` with `{"name": session_id, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}`. On 409, `GET` the existing folder by path to achieve "create if not exists" |
| `share_session_folder(handle, user_id)` | `POST /v1.0/drives/{drive-id}/items/{item-id}/invite` with `{"recipients": [{"email": "{email}"}], "requireSignIn": true, "sendInvitation": false, "roles": ["write"]}`. Response `value[0].id` is the permission id → `ShareHandle` |
| `revoke_session_share(share)` | `DELETE /v1.0/drives/{drive-id}/items/{item-id}/permissions/{permission-id}` |

**Scopes.** App-only auth via client credentials with `Files.ReadWrite.All` + `Group.ReadWrite.All` + `User.Read.All`. All three require admin consent. The `Sites.Selected` pattern (scope-limited per-site consent) is strictly more complex to implement and deferred to a stretch goal for enterprise customers who refuse tenant-wide consent.

**Throttling.** ~600 req/min per app for a small tenant (2 RU avg × 1,250 RU/min limit). Permission operations cost 5 RU each — important for bulk add/remove flows. Always honor `Retry-After`; decorate `User-Agent: NONISV|SuperhumanRemoteWorker|Orchestrator/0.1`.

**Auth library.** `azure-identity.ClientSecretCredential` (or certificate credential) for token acquisition, raw `httpx.AsyncClient` for the actual Graph REST calls. The official `msgraph-sdk` (Kiota-generated) adds verbose fluent-builder syntax and async-only operation without improving readability enough to justify the dependency.

**Personal Microsoft accounts are not supported** — M365 groups, SharePoint, and the tenant Graph APIs all require work-or-school accounts. `resolve_user_identity` returns a clear `CloudBackendError(kind=NOT_SUPPORTED)` on consumer MSAs instead of silently breaking.

The MS365 adapter is estimated at 600–900 LoC. The interface validates cleanly — every method maps to a Graph call or a no-op — with the single exception of `get_*_webdav_url` returning `Optional[str]`. Callers that already handle `None` (because of the opaque-handle design) need no changes.

## 6. Switching Backends (Non-Destructive)

The defining property of the design: **switching the active backend never deletes or migrates existing data.** The mechanism:

1. Every project row records `main_cloud_backend` at creation time. Same for every persistent thread row. This is the central invariant.
2. The active backend (from env / settings) is used **only** for *new* operations: `POST /api/projects`, `POST /api/persistent/threads`, default datasource creation in `POST /api/users`.
3. All operations on *existing* rows go through `MainCloudRouter.for_project(row)` / `.for_thread(row)` — which returns the row's original backend, not the active one.
4. Both backends must be reachable for the orchestrator to manage data on them. If a deployment switches from Nextcloud to OpenCloud and removes the Nextcloud service entirely, calls into existing Nextcloud-backed rows will fail with `CloudBackendError(kind=UNAVAILABLE)`. The circuit breaker opens, the job dispatcher pauses affected jobs, the `/health/cloud` endpoint reports degraded. No silent data corruption.

The settings UI (v2, §7) warns explicitly when switching:

> Switching the main cloud will leave existing project and session folders on the previous cloud. They remain accessible as long as the previous cloud is reachable. New projects and sessions will be created on the new cloud. There is no automatic migration.

A manual migration tool is a separate workstream — see §6.1.

### 6.1. Per-project migration CLI (future)

When an operator wants to actually move a project from one backend to another, the pattern ships as an explicit CLI command rather than a background process:

```
python -m orchestrator.cli migrate-project <project_id> --to=opencloud-primary
```

Four phases, borrowed from the Terraform / rclone playbook:

1. **VALIDATE** — both source and dest backends healthy, dest has quota, dest doesn't already have a project folder with this name, print plan, operator confirms.
2. **COPY** — create the project folder on dest, copy all contents (using the server-side native copy path if available; for cross-vendor migration, stream through the orchestrator via WebDAV or Graph chunked uploads). Resumable on failure.
3. **REPOINT** — inside a single Postgres transaction: update `projects.main_cloud_backend`, update `projects.main_cloud_folder_handle`, insert a row in a new `retired_handles` table for eventual cleanup. Pause any jobs for this project for the duration of the transaction.
4. **VERIFY + CLEANUP** — operator runs a sanity check (read a known file), confirms, then the old source folder is deleted. Deletion only happens on explicit confirmation; the operator is always in the loop.

This is a separate design document — not shipped in v1. It is listed here to demonstrate that the per-row-backend design makes migration a clean, bounded operation rather than a systemic refactor.

## 7. Settings UI (v2) — SHIPPED in Phase 4

Phase 4 shipped the settings UI as an admin-only section on the existing cockpit Settings page (not a new `/pages/admin/cloud-storage/` directory — the Codex Proxy precedent is a cleaner pattern for an initially-small admin surface). The operational runbook lives at [`docs/operations/phase-4-settings-ui.md`](../operations/phase-4-settings-ui.md); this section keeps the original design intent as a reference snapshot.

**What shipped:**

- **Active backend selector** (dropdown: OpenCloud / Nextcloud; MS365 and external remain Phase 5).
- **Connection details form** conditional on the selected backend. OpenCloud shows base URL, public URL, Keycloak issuer, Keycloak client id, admin role claim value, default Space quota. Nextcloud shows base URL, public URL, admin user, agent user.
- **Credentials ref pointer** (free-form text, e.g. `env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET`) — never accepts or displays secret values. A read-only "secret provenance" table lists each secret field's source env var and whether it's currently set.
- **Test** button → `POST /api/admin/system-settings/main_cloud/test` (dry-run `build_backend + ensure_initialized + health_check + close` against the proposed config, returns `{ok, detail, latency_ms}`).
- **Save + Reload** button → `PUT /api/admin/system-settings/main_cloud`. The handler validates via `load_main_cloud_config(db_overlay=...)`, persists to `system_settings.main_cloud`, calls `main_cloud_router.reload_from_db` synchronously on the local replica so the HTTP response reflects the real new state, then fires `pg_notify('main_cloud_config_changed', ...)` to fan out to other replicas.
- **Reset to env defaults** → `DELETE /api/admin/system-settings/main_cloud`. Clears the overlay and rebuilds from env vars only.
- **Force reload** → `POST /api/admin/system-settings/main_cloud/reload`. Useful after out-of-band secret rotation; rebuilds the active backend on the current replica without changing the persisted overlay.

The REST endpoints live in `orchestrator/main.py` under `/api/admin/system-settings/` and are gated by `_require_admin`. The LISTEN task in `orchestrator/services/cloud/reload.py` handles multi-replica fan-out with exponential-backoff reconnect. Boot-time `lifespan()` re-applies any persisted overlay so restarts are self-healing.

**Secrets are never stored in the DB.** The sanitizer at the PUT handler drops every secret-field key from the incoming body before persisting. Secret fields are resolved at read time via `credentials_ref: "env:NAME"` against the orchestrator's own environment, or fall back to the legacy per-backend env var. Rotating a secret still means rotating the env var in the secret store — the UI only manages non-secret knobs.

## 8. Implementation Phases

**Phase 1 — interface and refactor (no behavior change). SHIPPED.** Ships with Nextcloud still the only functional backend; user-visible behavior is identical. What actually landed in Phase 1:

1. ✓ Added `orchestrator/services/cloud/{__init__.py, base.py, handles.py, errors.py, nextcloud.py}`. **Not** added: `config.py` (Pydantic discriminated union — deferred to Phase 2, see §4.2), `retry.py` (tenacity wrapper — deferred to Phase 1.5).
2. ✓ Moved `NextcloudAdmin` into `NextcloudBackend`, conforming to the Protocol. Behavior-preserving refactor only. The HTML-redirect assertion, `/users/details?search=` switch, and 429 leaky bucket on share creation are **deferred to Phase 1.5** — see §5.1.
3. ✓ Added the schema migration for `main_cloud_backend` / `main_cloud_folder_handle` on `projects`, and `main_cloud_backend` / `main_cloud_session_handle` / `main_cloud_share_handle` on `threads`. Created the `system_settings` table. Backfilled from the existing `nextcloud_folder_id`, `nc_session_folder`, `nc_share_id` columns. Idempotent `DO $$...$$` blocks appended to `schema.sql`.
4. ✓ Replaced every `nextcloud_admin.*` call site in `orchestrator/main.py` with `main_cloud_router.active.*` for creates and `main_cloud_router.for_project(row).*` / `.for_thread(row).*` for reads/updates/deletes. Ten distinct endpoints touched, roughly thirty grep hits including property accesses — see §4.5 for the line-number table.
5. ✓ Replaced `nextcloud_admin = NextcloudAdmin()` at `main.py:137` with `main_cloud_router = MainCloudRouter(build_backend("nextcloud"))`. Updated the `lifespan()` init to call `main_cloud_router.ensure_initialized()`. The Pydantic-loaded discriminated union from §4.2 is deferred — Phase 1 passes the backend id as a bare string.
6. ✓ Also migrated `orchestrator/init.py::_backfill_cloud_folders` to the new package. Writes both the new and legacy columns so in-flight deployments stay readable.
7. ✓ Deleted `orchestrator/services/nextcloud_admin.py` (528 LoC).
8. ✓ Existing pytest suite (3,931 tests) passes unchanged; `ruff check` and `ruff format` are clean.
9. ↷ `FakeMainCloudBackend` + parametrized contract test suite — **deferred to Phase 1.5.**
10. ↷ Nextcloud VCR cassette regression tests — **deferred to Phase 1.5** (needs a local Nextcloud to record against).
11. ↷ `@instrument_backend_op` decorator — **deferred to Phase 1.5.**

**Phase 1 outcome:** the abstraction layer is in place and wired end-to-end. New adapters can be added against the Protocol in §4.1 without touching `orchestrator/main.py`. The deferred items (1.5) are all additive — none of them change the Protocol shape, so Phase 2 can start in parallel with Phase 1.5 if desired.

**Phase 1.5 — hardening and observability (no interface change, no user-visible change).** Cleans up every item Phase 1 deferred and tightens the Phase 1 contract:

1. Switch `NextcloudBackend` methods from "log-and-return-None" to raising `CloudBackendError` with mapped `CloudBackendErrorKind` values. Update the Protocol return types in `base.py` to non-`Optional` `Handle`. Update the `try/except Exception` blocks in `main.py` to catch `CloudBackendError` specifically.
2. Add the `OCS-APIRequest` HTML-redirect assertion on every Nextcloud response (§5.1). Add the `ensure_initialized` check for Nextcloud bug #4127.
3. Switch Nextcloud user lookup to `/users/details?search=` + client-side exact-match (§5.1).
4. Add the 429 leaky bucket on `share_session_folder` (~15 req / 10 min client-side ceiling). Reuse the `retry.py` tenacity policy from §4.9.
5. Add `orchestrator/services/cloud/retry.py` with the shared `retryable_policy()` factory (§4.9).
6. Add `orchestrator/services/cloud/config.py` with the Pydantic discriminated-union loader (§4.2). Phase 2 uses it to load OpenCloud settings.
7. Implement `FakeMainCloudBackend` in `tests/cloud/fake.py`. Write the parametrized contract test suite (`tests/cloud/test_backend_contract.py`).
8. Record Nextcloud VCR cassettes (`tests/cloud/cassettes/`) against a local dev Nextcloud. Wire `pytest-recording` into the test runner with `--vcr-record=none` in CI.
9. Add the `@instrument_backend_op` decorator (§4.11) and apply it to every method on `NextcloudBackend`.
10. Write the Nextcloud app-password bootstrap runbook at `docs/operations/nextcloud-bootstrap.md`.
11. **Ship.** Error handling is proper; behavioral Nextcloud fixes are in; observability is in place; contract tests guard the interface for Phase 2.

**Phase 2 — OpenCloud adapter. SHIPPED.** User-visible: OpenCloud is selectable via `MAIN_CLOUD_BACKEND=opencloud` (opt-in until Phase 3 flips the default).

1. ✓ Added `orchestrator/services/cloud/opencloud.py` implementing the Protocol (~780 LoC). Keycloak `client_credentials` token flow with ~30s clock-skew margin. Role-catalog cache with lazy refresh on cache miss. Disable-then-purge delete pattern on `delete_project_folder`. Agent-home Space auto-provisioning in `ensure_initialized`. All mutating methods decorated with `@instrument_backend_op` and error-mapped via `_map_http_error`.
2. ✓ Added `opencloud` service to `docker-compose.yaml` under `profiles: ["opencloud"]`. Created `docker/opencloud/setup-opencloud.sh` as a pre-start hook that verifies `PROXY_USER_OIDC_CLAIM=sub` and logs the Keycloak bootstrap reminder. Added `opencloud_data` / `opencloud_config` named volumes.
3. ✓ Added `opencloud-web` (public) and `opencloud-orchestrator` (service-account) clients to `docker/keycloak/realm-export.json.example`, plus the `opencloud-admin` realm group and the service-account user binding. Wrote `docker/keycloak/setup-opencloud-client.sh` as an imperative idempotent fallback that uses `kcadm.sh` to create/verify clients + group membership.
4. ✓ Added `orchestrator/services/cloud/__init__.py::build_backend("opencloud")` that loads the Pydantic-validated `OpenCloudSettings` from env vars via `load_main_cloud_config(backend_override="opencloud")`.
5. ✓ Wrote `tests/cloud/test_opencloud.py` (27 tests) using `httpx.MockTransport` to fake a combined Keycloak + LibreGraph surface. Covers: token flow + caching, URL constructors (delimiter encoding), `ensure_group` idempotency, `ensure_project_folder` + group invite, two-step `delete_project_folder`, session folder WebDAV MKCOL, share + revoke round-trip, uninitialized-backend guard, full error-mapping matrix (400/401/403/404/429/500/418).
6. ✓ Wrote `docs/operations/opencloud-bootstrap.md` covering required server state (sub claim, OIDC wiring, admin group), Keycloak setup via the automated script or the UI, env var wiring, verification, OIDC caveats, and known LibreGraph quirks (disable-then-purge, delimiter encoding, $filter restrictions, role UUIDs, v1beta1 sharing).
7. ✓ Added `OPENCLOUD_*` env vars to `.env.example` with inline notes on the bootstrap flow.
8. ↷ `deployment/19-opencloud.yaml` K8s manifest — **deferred to Phase 3** when the default flips. For Phase 2, Compose is the only bundled deployment path; operators running K8s today can still configure an external OpenCloud as their main cloud via env vars.
9. ↷ `testcontainers-python` contract tests + VCR cassettes — **deferred.** The MockTransport-based unit tests cover the control flow; recording cassettes against a real OpenCloud needs an instance running, which belongs to the Phase 3 flip workstream.
10. ↷ End-to-end smoke test with a live OpenCloud + orchestrator + create project + add member + persistent thread — **deferred.** Same rationale as the K8s manifest: belongs to Phase 3 when the default flip needs real-world validation.
11. **Ship.** OpenCloud is opt-in via env var; Nextcloud still default. Phase 2.5 / Phase 3 picks up the deferred cassettes and the K8s manifest.

**Phase 3 — flip the default. SHIPPED.** Greenfield installs now default to OpenCloud; existing Nextcloud installs keep working without changes.

1. ✓ Moved the Nextcloud service in `docker-compose.yaml`, `docker-compose.local.yaml`, and `docker-compose.dev.yaml` behind `profiles: ["nextcloud"]`. Promoted OpenCloud to the always-on default in `docker-compose.yaml` and `docker-compose.local.yaml`.
2. ✓ Changed `load_main_cloud_config()` resolution order: `MAIN_CLOUD_BACKEND` env → `_detect_legacy_nextcloud_mode()` → `opencloud`. The legacy heuristic fires when any `NEXTCLOUD_*` var (other than `NEXTCLOUD_PORT`) is set and `MAIN_CLOUD_BACKEND` is unset — existing deployments keep running without any .env changes. Tightened `_detect_legacy_nextcloud_mode()` to ignore `NEXTCLOUD_PORT` alone so the port override doesn't accidentally force a Nextcloud deployment.
3. ✓ Reworked `build_backend()` to accept `backend_id=None` and route through `load_main_cloud_config` in that case. Kept the explicit `backend_id=...` path so `MainCloudRouter._legacy` can still force a specific backend when dispatching to a cached adapter.
4. ✓ Updated the two hardcoded `build_backend("nextcloud")` sites (`orchestrator/main.py:143`, `orchestrator/init.py:810`) to pass no argument, letting the env detection decide.
5. ✓ Removed the Nextcloud-hardcoded `DEFAULT_DS_WEBDAV_URL` / `DEFAULT_DS_WEBDAV_USERNAME` / `DEFAULT_DS_WEBDAV_PASSWORD` compose defaults in both `docker-compose.yaml` and `docker-compose.local.yaml`. They now forward whatever the operator sets in `.env`, with an empty default — the main-cloud adapter injects per-project datasources at creation time, so the shared "admin-visible" datasource shim is no longer needed.
6. ✓ Rewrote the `.env.example` cloud storage section: OpenCloud block comes first as the default, Nextcloud block comes second as the legacy/alternate. Added the §3-level "resolution order" explainer.
7. ✓ Wrote the upgrade notes runbook at `docs/operations/phase-3-default-flip.md` with the three paths: greenfield OpenCloud, in-place upgrade from Phase 1/2 Nextcloud, explicit migration between backends.
8. ↷ `deployment/19-opencloud.yaml` K8s manifest — still deferred, same rationale as Phase 2. Operators running Fleet-synced K8s keep Nextcloud until they explicitly import an OpenCloud manifest; no silent upgrade path on K8s.
9. ↷ End-to-end live-cluster smoke test — still deferred; belongs to the test infrastructure workstream.
10. **Ship.** New compose installs default to OpenCloud. In-place upgrades from Phase 2 Nextcloud keep their existing backend via `_detect_legacy_nextcloud_mode`. Explicit override via `MAIN_CLOUD_BACKEND=...` always wins.

**Phase 4 — settings UI. SHIPPED.** Admins edit non-secret main-cloud config from the cockpit, the active backend hot-reloads without a pod restart, and secrets stay in the secret store.

1. ✓ Added admin REST endpoints under `/api/admin/system-settings/main_cloud` in `orchestrator/main.py`:
   - `GET` — return the effective config, the persisted overlay, and per-field secret provenance (env var name + set/unset flag + length, never the value).
   - `PUT` — validate a proposed overlay, persist to `system_settings.main_cloud`, synchronously reload the local router, then fire `pg_notify` to fan out to other replicas.
   - `POST /test` — dry-run a proposed overlay (build backend + `ensure_initialized` + `health_check` + close) without persisting. Powers the "Test" button.
   - `POST /reload` — force a local reload from the current persisted overlay. Useful after out-of-band secret rotation.
   - `DELETE` — clear the overlay and reload from env vars only ("reset to defaults").
   All five endpoints are admin-only via `_require_admin`. The PUT sanitizer drops any secret-field key from the incoming body so a misconfigured UI PUT can never persist a client secret into `system_settings.value`.
2. ✓ Added DB helpers on `PostgresDB`: `get_system_setting(key)`, `upsert_system_setting(key, value, credentials_ref, updated_by)`, `delete_system_setting(key)`, and `notify_channel(channel, payload)`. The NOTIFY path uses `SELECT pg_notify($1, $2)` so both the channel name and the payload can be parameterized safely.
3. ✓ Extended `load_main_cloud_config` with a `db_overlay` parameter that layers persisted values over env-var defaults. Non-secret fields in `overlay.value` win over env vars; secret fields come from the env var named in `overlay.credentials_ref` (e.g. `env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET`) when the field is listed in `__secret_fields__`, otherwise fall back to the legacy per-backend env var. `build_backend(db_overlay=...)` threads the overlay through to the Pydantic validator.
4. ✓ Added `MainCloudRouter.replace_active(new)` (atomic swap, closes old when the id is unchanged, demotes to `_legacy` when the id changes) and `MainCloudRouter.reload_from_db(overlay)` (rebuild + init + swap, falls back to keeping the old backend on any failure). Guarded by `asyncio.Lock` so concurrent reloads serialize cleanly.
5. ✓ Added `orchestrator/services/cloud/reload.py` with a long-running `run_listen_loop` task that subscribes to the `main_cloud_config_changed` channel via `asyncpg`'s `add_listener` API. Exponential-backoff reconnect on connection loss; cooperative shutdown via the orchestrator's shared `_shutdown_event`. The task is registered in the FastAPI `lifespan` alongside the existing background sweepers.
6. ✓ Added a boot-time overlay apply in `lifespan()`: after the initial env-var `ensure_initialized`, the orchestrator reads `system_settings.main_cloud` and calls `reload_from_db` if an overlay is present, so restarts pick up the admin-saved config automatically. Non-fatal — a broken overlay logs a warning and leaves the env-var config active.
7. ✓ Extended the existing cockpit Settings page (`cockpit/src/app/pages/settings/settings.component.ts`) with an admin-only "Cloud Storage" section, mirroring the Codex Proxy pattern. Form fields render conditionally based on selected backend (OpenCloud shows Keycloak issuer + client id + quota; Nextcloud shows admin/agent users). Secret provenance table shows which env var each secret reads from and whether it's set.
8. ✓ Added `SettingsService.getMainCloudSettings / putMainCloudSettings / testMainCloudSettings / reloadMainCloudSettings / deleteMainCloudSettings` on the frontend side with typed request/response interfaces.
9. ✓ Tests: `tests/cloud/test_db_overlay.py` (7 tests — overlay merge order, credentials_ref redirect, partial overlay fill-from-env, empty overlay no-op) and `tests/cloud/test_router_reload.py` (7 tests — `replace_active` same-id vs different-id behaviour, `reload_from_db` happy path, init-failure paths, legacy dispatch after swap). Cockpit tests still pass unchanged (239).
10. ↷ Persisting the LISTEN task across orchestrator replica restarts is solved by each replica re-reading `system_settings.main_cloud` in its own `lifespan()` — no manual intervention required. Multi-pod fan-out is best-effort via `pg_notify` and not retried on delivery failure; if a replica misses a NOTIFY it stays on the old config until its next restart or an explicit `/api/admin/system-settings/main_cloud/reload` call.
11. **Ship.** The cockpit's Cloud Storage admin panel is live; operators can edit non-secret config without a pod restart. Secrets still rotate via Vault/ESO/.env, referenced by name only.

**Phase 5 — additional adapters. DEFERRED (separate future workstream).** MS365 and Google Workspace are not in scope for the current milestone. The Phase 1–4 interface already accommodates them (opaque handles, capability protocols, overlay-based config, hot-reload), so adding either one is a self-contained adapter workstream rather than a systemic refactor. The reason Phase 5 is deferred rather than shipped:

1. **Non-WebDAV forces an agent-side refactor.** Every current agent cloud tool in `src/tools/webdav/` speaks WebDAV. MS365's `driveItem.webDavUrl` is a cookie/NTLM compatibility artifact that does not work with OAuth app-only tokens, and Google Drive has no WebDAV at all. Shipping an MS365 backend without also shipping a Graph-speaking agent tool driver means the orchestrator can provision Microsoft 365 group / SharePoint drives but the agent cannot actually read or write files in them. The agent-side change is its own design conversation (tool schema, per-backend tool variants, how the registry selects which driver to load) and belongs in a separate feature doc under `docs/features/agent_cloud_tools.md`.
2. **Auth plane is separate.** Graph uses Entra ID, not Keycloak. The orchestrator's Phase 2 Keycloak `client_credentials` flow does not translate — MS365's adapter would need its own `azure-identity` bootstrap, its own token cache, and its own consent flow for the app-only permissions (`Sites.Selected`, `Group.ReadWrite.All`, `User.Read.All`). Google Workspace has a similar story with domain-wide delegation + service-account JSON key files. Neither maps onto the existing `OpenCloudSettings` / `NextcloudSettings` model without a new top-level branch in the config loader.
3. **No user demand yet.** The current deployment target (homelab + the Frankfurt UAS / FINIUS engagement) uses Keycloak for SSO and either OpenCloud or Nextcloud for file storage. Shipping MS365 support without an operator who actually wants it would mean the adapter gets no real-world shakedown before the first paying customer relies on it — and Microsoft Graph is subtle enough that untested code is code that will break on day one.
4. **Interface stability is already proven.** The contract tests in `tests/cloud/test_backend_contract.py` are parametrized over a `FakeMainCloudBackend` fixture today. Phase 5 can add an `ms365` row (cassette-backed or testcontainers-backed) without modifying the existing tests, which is the strongest evidence that the interface is forward-compatible.

**What Phase 5 would look like when it's picked up**, sketched for future readers:

1. Add `orchestrator/services/cloud/ms365.py` implementing `MainCloudBackend` against Microsoft Graph (`/v1.0/drives/...`, `/v1.0/groups/...`, `/v1.0/users/...`). Reuse the error-mapping + telemetry patterns from `nextcloud.py` / `opencloud.py`. Get-user-home returns `webdav_url=None`; `get_project_folder_webdav_url` likewise. See §5.3 for the API surface mapping.
2. Add `azure-identity` client-credentials token loader. Handle the `owners@odata.bind` requirement on Unified group creation (without it, SharePoint never provisions the backing site and `/groups/{id}/drive` returns 404 forever).
3. Add `MS365Settings` branch to the Pydantic config loader. It already exists as a scaffold — Phase 5 fills in the `tenant_id` / `client_id` / `client_secret` / `site_id` validation + discriminator wiring.
4. **Agent-side tool refactor.** Add a Graph-speaking variant under `src/tools/webdav/graph.py`. Decide how the agent tool registry selects which cloud tool to load for a given datasource — static config, backend id propagated through the datasource row, or per-job override. This is the big shape question and it blocks Phase 5 on an agent-side feature doc.
5. Add `/api/admin/system-settings/main_cloud` support for `backend_id: "ms365"` in the REST handler's allowed set and the cockpit dropdown. Add MS365 secret provenance fields.
6. Write `tests/cloud/test_ms365.py` using `httpx.MockTransport` for the control-flow tests (mirror `test_opencloud.py`). Defer live-cluster VCR cassettes until a real tenant is available.
7. Add `docs/operations/ms365-bootstrap.md` runbook covering Entra app registration, consent grant, `Sites.Selected` permission scoping, the `owners@odata.bind` gotcha.
8. **Google Workspace** as a second Phase 5 deliverable follows the same template with Google Drive REST + service-account + domain-wide delegation, plus its own `src/tools/webdav/drive.py`.

Phase 5 is **not blocked by anything in Phases 1–4**. It's paused on demand and on the agent-side tool layer design.

## 9. Legacy column cleanup

One release after Phase 1 ships, drop the legacy columns:

```sql
DO $$ BEGIN
    ALTER TABLE projects DROP COLUMN nextcloud_folder_id;
EXCEPTION WHEN undefined_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE threads DROP COLUMN nc_session_folder;
EXCEPTION WHEN undefined_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE threads DROP COLUMN nc_share_id;
EXCEPTION WHEN undefined_column THEN null;
END $$;
```

Cockpit models in `cockpit/src/app/core/models/api.model.ts` lose `nextcloud_folder_id`, `nc_session_folder`, `nc_share_id` (lines 343, 482–483). The existing `openSessionFiles()` method in `sessions-page.component.ts` (lines 652–656) switches to reading a generic `main_cloud_browser_url` field computed server-side.

## 10. Out of Scope

- **Multiple main clouds per deployment.** "One main cloud" is a design decision, not a technical limitation. Non-destructive switching (§6) is the escape hatch for "I want to migrate from A to B."
- **Per-project backend selection at creation time.** New projects always go on the active backend. Existing projects keep their original backend (recorded in `main_cloud_backend`).
- **Automatic data migration between backends.** Switching backends leaves existing data on the old backend. An explicit `migrate-project` CLI (§6.1) is a separate workstream.
- **Treating non-main clouds (Dropbox, Drive, Box, ...) as automatic backends.** They can be attached as plain `webdav` or protocol-specific datasources, but they do not host home directories or session folders.
- **Generalizing the agent-side tool surface beyond WebDAV.** Today every agent cloud tool speaks WebDAV. MS365 and Google Workspace have no usable WebDAV, so eventually `src/tools/webdav/` needs a second driver (Microsoft Graph, Google Drive REST). That is a meaningful design conversation about the agent tool layer, tracked as its own feature, and it's the load-bearing blocker for the Phase 5 workstream in §8. The opaque-handle design in §4.1 is the preparation for that refactor.
- **Cockpit-side file browser.** The cockpit currently deep-links out to the cloud's web UI. Embedding a file browser is its own feature design.
- **Quota / billing UX.** Neither backend's quota model is currently surfaced in the cockpit. Both could be exposed later via the `SupportsQuota` capability protocol (§4.1.2) if a customer asks.
- **Versioning / trash UX.** `SupportsVersioning` / `SupportsTrash` are sketched as optional capabilities; no caller in v1 uses them.

## 11. Open Questions

- **Where does `main_cloud_router` live on the FastAPI app? RESOLVED.** Module-level singleton in `orchestrator/main.py` at line 143 (`main_cloud_router = MainCloudRouter(build_backend())`), matching the existing `postgres_db` / `mongodb` / `gitea_client` pattern. No `app.state.*` refactor in Phases 1–4; revisit if the rest of the file moves to `app.state.*`.
- **OpenCloud quota default. RESOLVED.** 10 GB default in `opencloud.py::_DEFAULT_QUOTA_BYTES`, overridable via `OPENCLOUD_DEFAULT_QUOTA_BYTES` env var or the `default_quota_bytes` field on the Phase 4 DB overlay. `.env.example` documents both paths.
- **OpenCloud role names — hardcoded or configurable? RESOLVED (v1).** `opencloud.py::_DEFAULT_EDITOR_ROLE = "Space Editor"` and `_DEFAULT_VIEWER_ROLE = "Space Viewer"` are hardcoded and resolved to UUIDs via the role catalog at startup. Custom deployments that rename the roles can file an issue and we'll add an `OpenCloudSettings.roles` override — no customer has asked yet.
- **Nextcloud app password bootstrap. RESOLVED.** Runbook at `docs/operations/nextcloud-bootstrap.md` documents the one-time "log in as agent-service → create app password → paste into secret store" dance. No `srw-bootstrap` CLI in v1 — the manual runbook is the contract.
- **Backend reload after settings change (Phase 4). RESOLVED.** Shipped as `pg_notify` / `LISTEN`. The PUT handler persists + calls `main_cloud_router.reload_from_db` synchronously on the local replica (so the HTTP response reflects the real outcome), then fires `pg_notify('main_cloud_config_changed', ...)`. Other replicas hold a long-running LISTEN task (`orchestrator/services/cloud/reload.py::run_listen_loop`) that re-reads the overlay and swaps their local backend when the notification arrives. Fallbacks: boot-time `lifespan()` re-applies the persisted overlay so restarts are self-healing; admins can force a local reload via `POST /api/admin/system-settings/main_cloud/reload` when they've rotated a secret env var out-of-band.
- **`MainCloudRouter._legacy` instantiation cost. DEFERRED (no change).** No cap shipped in v1 — at most two or three backends in practice, and the legacy entries only come alive when the operator migrates. Revisit when someone lands on four-plus backends.
- **OpenCloud sharing API migration from `v1beta1`. DEFERRED (no change).** The adapter treats `v1beta1` as required. Will add a version-detection shim if upstream promotes without a deprecation window; no signal yet that this is imminent.
- **MS365 adapter — where does Keycloak fit? DEFERRED to Phase 5.** Keycloak stays as the orchestrator's user-facing IdP; the MS365 adapter uses its own `azure-identity` client-credentials flow for service-account auth. The two identity planes never federate. Full scope lives in §8 Phase 5 and §5.3.
- **Agent-side tool surface for non-WebDAV backends. DEFERRED to Phase 5.** Blocks on a separate feature doc (`docs/features/agent_cloud_tools.md` — not yet written). The Phase 1 opaque-handle design already prepares for it; no Phase 1–4 code change is needed ahead of Phase 5.

## 12. Related Documents

- [[done/cloud_storage_alternatives]] — the decision doc for choosing OpenCloud as the new default
- [[features/sso_and_cloud_storage]] — the original Keycloak + Nextcloud design (still accurate as the description of the current Nextcloud adapter)
- [[features/project_cloud_folders]] — the project/session folder lifecycle this document re-implements behind an interface
- [[datasources]] — datasource system overview
- `docs/issues/nextcloud_oidc_username.md` — OIDC username quirk; relevant precedent for OpenCloud's `sub` claim requirement

## 13. Appendices — Research References

The design above draws heavily on four research briefs commissioned for this refinement pass. The briefs live in the tool-results cache for this session; the authoritative facts are inlined above. This appendix lists the load-bearing sources so future readers can verify.

### 13.1. Connector abstraction patterns

- [Nick Craig-Wood — rclone and Go (2018)](https://www.craig-wood.com/nick/articles/rclone-and-go-2018/) — the retrospective on rclone's `Fs` interface + optional-interfaces pattern. Source of "the bigger the interface, the weaker the abstraction" quote.
- [rclone `fs` package](https://pkg.go.dev/github.com/rclone/rclone/fs) — concrete Go interfaces for capability detection.
- [Apache libcloud Storage Base API](https://libcloud.readthedocs.io/en/stable/storage/api.html) — the `extra` dict pattern and `ex_` prefix convention.
- [fog design document](https://github.com/fog/fog/wiki/fog-design-document) — the cautionary tale of monolithic-gem growth and the eventual split by provider.
- [Apache jclouds BlobStore Guide](https://jclouds.apache.org/start/blobstore/) — "Views vs ProviderMetadata" escape-hatch pattern.
- [Shrine — Creating Storages](https://shrinerb.com/docs/creating-storages) — the five-method minimal storage interface; the `#url → nil` pattern.
- [PEP 544](https://peps.python.org/pep-0544/) — Python Protocol spec; `@runtime_checkable` semantics.
- [Stripe Error Handling — DeepWiki analysis](https://deepwiki.com/stripe/stripe-node/2.4-error-handling) — the "enum kind + single error class + preserved raw response" pattern.
- [Stripe — Designing idempotent APIs](https://stripe.com/blog/idempotency) — idempotency key generation and retry semantics.
- [Tenacity docs](https://tenacity.readthedocs.io/) — async retry primitives.
- [HTTPX Advanced Clients](https://www.python-httpx.org/advanced/clients/) — connection pooling, timeout configuration.
- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/) — the canonical lifespan pattern.
- [pytest-recording (vcrpy for pytest)](https://github.com/kiwicom/pytest-recording) — VCR cassette-based regression tests.
- [testcontainers-python](https://testcontainers-python.readthedocs.io/) — integration test harness.
- [Django `STORAGES` setting](https://docs.djangoproject.com/en/5.2/ref/files/storage/) — dict-of-dicts pattern for multiple named backends.

### 13.2. OpenCloud / LibreGraph

- [LibreGraph OpenAPI spec (`opencloud-eu/libre-graph-api`)](https://github.com/opencloud-eu/libre-graph-api) — the authoritative API definition, version v1.0.8 reviewed.
- [OpenCloud Users API reference](https://docs.opencloud.eu/docs/next/dev/server/apis/http/graph/users/)
- [oCIS Spaces model (owncloud.dev)](https://owncloud.dev/ocis/storage/spaces/)
- [ADR-0007: Open Graph API for oCIS File Spaces](https://owncloud.dev/ocis/adr/0007-api-for-spaces/)
- [OpenCloud External OIDC IdP configuration](https://docs.opencloud.eu/docs/admin/configuration/authentication-and-user-management/external-idp/)
- [OpenCloud Keycloak Integration guide](https://docs.opencloud.eu/docs/admin/configuration/authentication-and-user-management/keycloak/)
- [opencloud-eu/opencloud-compose reference](https://github.com/opencloud-eu/opencloud-compose)
- [ocis init command reference](https://doc.owncloud.com/ocis/next/deployment/general/ocis-init.html)
- [`owncloud/ocis#6664` — oCIS should use the `sub` Claim](https://github.com/owncloud/ocis/issues/6664) — the "do not use preferred_username" lesson.
- [`opencloud-eu/opencloud#1578` — malformed access token](https://github.com/opencloud-eu/opencloud/issues/1578) — Keycloak client must issue JWTs, not opaque tokens.
- [`opencloud-eu/opencloud#909` — OIDC token refresh failures](https://github.com/opencloud-eu/opencloud/issues/909) — request `offline_access` scope.
- [`opencloud-eu/opencloud#2373` — LDAP query spam](https://github.com/opencloud-eu/opencloud/issues/2373) — nested LDAP groups not supported.
- [`opencloud-eu/web#1795` — WebDAV `$` parsing](https://github.com/opencloud-eu/web/issues/1795) — drive-id delimiter encoding.

### 13.3. Nextcloud

- [Nextcloud OCS APIs overview](https://docs.nextcloud.com/server/stable/developer_manual/client_apis/OCS/ocs-api-overview.html)
- [Nextcloud user provisioning API](https://docs.nextcloud.com/server/latest/admin_manual/configuration_user/user_provisioning_api.html)
- [Nextcloud OCS Share API](https://docs.nextcloud.com/server/stable/developer_manual/client_apis/OCS/ocs-share-api.html)
- [Nextcloud WebDAV basic APIs](https://docs.nextcloud.com/server/stable/developer_manual/client_apis/WebDAV/basic.html)
- [`nextcloud/server#57445` — Group share propagation bug](https://github.com/nextcloud/server/issues/57445) — **still open** on NC 31; justifies `refresh_project_folder_access`.
- [`nextcloud/server#44782` — Sharing permissions silently ignored](https://github.com/nextcloud/server/issues/44782) — create with baseline, PUT to update, GET to verify.
- [Community thread — 429 on Share API (NC 30.0.10+)](https://help.nextcloud.com/t/429-too-many-request-using-share-api-nc31-oc-ratelimit-entries-involved/224039) — 20-shares-per-10-min limit, client-side leaky bucket.
- [`nextcloud/user_oidc#1111` — human-readable names](https://github.com/nextcloud/user_oidc/issues/1111) — `unique_uid` and `mapping-uid` configuration.
- [`nextcloud/groupfolders#4127` — root_id crash](https://github.com/nextcloud/groupfolders/issues/4127) — detect in `ensure_initialized`.
- [`nextcloud/documentation#10899` — OCS status codes](https://github.com/nextcloud/documentation/issues/10899) — v1 returns 100, v2 returns 200; accept both.
- [Nextcloud Webhook Listeners (NC 30+)](https://docs.nextcloud.com/server/latest/admin_manual/webhook_listeners/index.html) — potential future event transport.
- [`cloud-py-api/nc_py_api`](https://github.com/cloud-py-api/nc_py_api) — evaluated for replacing raw `httpx`; rejected because Group Folders are not covered.

### 13.4. Microsoft Graph

- [driveItem resource type](https://learn.microsoft.com/en-us/graph/api/resources/driveitem?view=graph-rest-1.0)
- [Permission resource type](https://learn.microsoft.com/en-us/graph/api/resources/permission?view=graph-rest-1.0)
- [Create group](https://learn.microsoft.com/en-us/graph/api/group-post-groups?view=graph-rest-1.0) — `owners@odata.bind` requirement.
- [Microsoft 365 groups concept overview](https://learn.microsoft.com/en-us/graph/microsoft365-groups-concept-overview)
- [driveItem: invite](https://learn.microsoft.com/en-us/graph/api/driveitem-invite?view=graph-rest-1.0) — `permission.id` is the stable share handle.
- [Microsoft Graph throttling guidance](https://learn.microsoft.com/en-us/graph/throttling)
- [Avoid getting throttled in SharePoint Online](https://learn.microsoft.com/en-us/sharepoint/dev/general-development/how-to-avoid-getting-throttled-or-blocked-in-sharepoint-online) — resource unit model and per-app-per-tenant limits.
- [Sites.Selected overview](https://learn.microsoft.com/en-us/graph/permissions-selected-overview) — scope-limited alternative to `Files.ReadWrite.All`.
- [Tech and me — Encoding drive ids](https://www.techmikael.com/2021/01/microsoft-graph-encoding-and-decoding.html) — `b!base64url` drive id shape.
- [michev.info — SharePoint item versions via Graph](https://michev.info/blog/post/6253/my-experience-working-with-sharepoint-onedrive-for-business-item-versions-via-the-graph-api) — war stories at scale.
- [`microsoftgraph/msgraph-sdk-python`](https://github.com/microsoftgraph/msgraph-sdk-python) — evaluated, rejected in favor of raw httpx + `azure-identity`.
- [`microsoftgraph/msgraph-sdk-python#366` — asyncio event loop closed](https://github.com/microsoftgraph/msgraph-sdk-python/issues/366) — known SDK bug.
- [Microsoft Q&A — WebClient/WebDAV deprecation](https://learn.microsoft.com/en-us/answers/questions/1609470/) — the final answer on "is SharePoint WebDAV still a thing."
