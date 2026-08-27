"""Main cloud abstraction package.

Single entry point for main-cloud backends. The orchestrator imports
``MainCloudRouter`` + ``build_backend`` and uses the router's ``active``,
``for_project(row)``, and ``for_thread(row)`` accessors to reach the right
backend instance. Individual backends live in siblings of this module
(``nextcloud.py`` today; ``opencloud.py`` / ``ms365.py`` in later phases).

See ``knowledge-base/knowledge/features/main_cloud_abstraction.md`` for the full design.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .base import (
    CloudMountSubject,
    HealthStatus,
    MainCloudBackend,
    RcloneMountSpec,
    SupportsRcloneMount,
    UserHome,
)
from .backend_instance_authority import MainCloudBackendInstanceAuthority
from .config import (
    MainCloudConfig,
    MS365Settings,
    NextcloudSettings,
    OpenCloudSettings,
    load_main_cloud_config,
    load_main_cloud_config_from_instance,
)
from .errors import CloudBackendError, CloudBackendErrorKind, FeatureNotAvailable
from .handles import (
    GroupId,
    ProjectFolderEntry,
    ProjectFolderHandle,
    SessionFolderHandle,
    ShareHandle,
    UserId,
)
from .nextcloud import NextcloudBackend
from .opencloud import OpenCloudBackend
from .retry import LeakyBucket, retryable_policy

logger = logging.getLogger(__name__)

RELOAD_CHANNEL = "main_cloud_config_changed"


REGISTRY: dict[str, type] = {
    "nextcloud": NextcloudBackend,
    "opencloud": OpenCloudBackend,
    # "ms365": MS365Backend,          # Phase 5
}


def build_backend_from_config(settings: MainCloudConfig) -> MainCloudBackend:
    """Construct one adapter from an already validated settings snapshot."""

    if isinstance(settings, NextcloudSettings):
        return NextcloudBackend(settings)
    if isinstance(settings, OpenCloudSettings):
        return OpenCloudBackend(settings)
    raise ValueError(
        f"unknown main cloud backend: {settings.backend_id!r} "
        f"(known: {sorted(REGISTRY)})"
    )


def build_backend(
    backend_id: str | None = None,
    *,
    db_overlay: Optional[dict] = None,
) -> MainCloudBackend:
    """Construct a main-cloud backend instance.

    Phase 3: when called with no argument, this routes through
    ``load_main_cloud_config`` which picks the backend via
    ``MAIN_CLOUD_BACKEND`` → ``_detect_legacy_nextcloud_mode`` →
    ``opencloud``. Callers can still pass an explicit ``backend_id``
    to force a specific backend (used by ``MainCloudRouter._legacy``
    when reviving a cached adapter for a pre-flip row).

    Phase 4: ``db_overlay`` layers persisted ``system_settings.main_cloud``
    values over the env-var defaults. This is what lets the cockpit
    admin panel change the active backend without a pod restart.

    Both backends now consume a validated Pydantic settings object
    (``NextcloudSettings`` / ``OpenCloudSettings``) built by
    ``load_main_cloud_config`` — so the ``MAIN_CLOUD_*`` aliases, the
    ``db_overlay``, and ``credentials_ref`` apply uniformly to both
    (Issue 12). Earlier, ``NextcloudBackend`` read ``NEXTCLOUD_*`` env vars
    directly in its constructor and ignored the overlay.
    """
    if backend_id is None:
        settings = load_main_cloud_config(db_overlay=db_overlay)
        return build_backend_from_config(settings)

    if backend_id == "nextcloud":
        settings = load_main_cloud_config(
            backend_override="nextcloud", db_overlay=db_overlay
        )
        return build_backend_from_config(settings)
    if backend_id == "opencloud":
        settings = load_main_cloud_config(
            backend_override="opencloud", db_overlay=db_overlay
        )
        return build_backend_from_config(settings)
    raise ValueError(
        f"unknown main cloud backend: {backend_id!r} (known: {sorted(REGISTRY)})"
    )


def build_backend_from_instance(
    authority: MainCloudBackendInstanceAuthority,
) -> MainCloudBackend:
    """Rebuild a historical adapter without consulting active config."""

    settings = load_main_cloud_config_from_instance(authority)
    if isinstance(settings, NextcloudSettings):
        return NextcloudBackend(settings)
    if isinstance(settings, OpenCloudSettings):
        return OpenCloudBackend(settings)
    raise ValueError(
        f"unsupported retained main-cloud backend: {authority.backend_id!r}"
    )


class MainCloudRouter:
    """Holds the active main-cloud backend plus a cache of legacy backends.

    Non-destructive switching: when the operator changes the active backend,
    existing projects and threads keep pointing at their original backend via
    the ``main_cloud_backend`` column on their row. ``for_project`` /
    ``for_thread`` dispatch each call to the right instance; ``active`` is
    used for fresh creates.

    For deployments that only ever use one backend (the common case), the
    ``_legacy`` cache stays empty and ``for_project``/``for_thread`` return
    ``self._active`` for every row.
    """

    def __init__(self, active: MainCloudBackend) -> None:
        self._active = active
        self._legacy: dict[str, MainCloudBackend] = {}
        self._instances: dict[str, MainCloudBackend] = {}
        self._instance_secret_revisions: dict[str, int] = {}
        self._reload_lock = asyncio.Lock()

    @property
    def active(self) -> MainCloudBackend:
        """The current global backend.

        Call sites should NOT read this directly to act on a resource —
        resolve through ``for_project`` / ``for_thread`` / ``for_owner`` so a
        live backend swap can't land an operation on the wrong cloud
        (Issue 16, knowledge-base/knowledge/issues/main_cloud.md). ``active`` is for the admin
        config surface (reporting / editing *which* backend is active) and is
        the value those seams return today.
        """
        return self._active

    @property
    def active_instance_id(self) -> str | None:
        return self._active.backend_instance_id

    def bind_active_instance(
        self,
        authority: MainCloudBackendInstanceAuthority,
    ) -> None:
        """Attach DB authority only after the active adapter was attested."""

        if (
            not isinstance(authority, MainCloudBackendInstanceAuthority)
            or authority.backend_id != self._active.backend_id
            or self._active.installation_proof_sha256
            != authority.installation_proof_sha256
        ):
            raise ValueError("active main-cloud installation proof does not match")
        self._active.bind_backend_instance(authority.backend_instance_id)
        self._instances[authority.backend_instance_id] = self._active
        self._instance_secret_revisions[authority.backend_instance_id] = (
            authority.secret_revision
        )

    def for_backend_instance(
        self,
        backend_instance_id: str,
        *,
        expected_backend_id: str,
        expected_secret_revision: int | None = None,
    ) -> MainCloudBackend:
        """Resolve only a cached exact installation; never fall back active."""

        backend = self._instances.get(str(backend_instance_id))
        if (
            backend is None
            or backend.backend_instance_id != str(backend_instance_id)
            or backend.backend_id != expected_backend_id
            or (
                expected_secret_revision is not None
                and self._instance_secret_revisions.get(str(backend_instance_id))
                != expected_secret_revision
            )
        ):
            raise FeatureNotAvailable(
                f"main-cloud backend instance {backend_instance_id!r}",
                backend=expected_backend_id,
            )
        return backend

    async def resolve_backend_instance(
        self,
        authority: MainCloudBackendInstanceAuthority,
        *,
        force_rebuild: bool = False,
    ) -> MainCloudBackend:
        """Build, attest, and cache one retained installation by UUID."""

        if not isinstance(authority, MainCloudBackendInstanceAuthority):
            raise ValueError("main-cloud backend instance authority is missing")
        cached = self._instances.get(authority.backend_instance_id)
        cached_revision = self._instance_secret_revisions.get(
            authority.backend_instance_id
        )
        if cached is not None and not force_rebuild:
            if cached.backend_id != authority.backend_id:
                raise ValueError("cached main-cloud backend provider does not match")
            if cached_revision == authority.secret_revision:
                return cached
        async with self._reload_lock:
            cached = self._instances.get(authority.backend_instance_id)
            cached_revision = self._instance_secret_revisions.get(
                authority.backend_instance_id
            )
            if cached is not None and not force_rebuild:
                if cached.backend_id != authority.backend_id:
                    raise ValueError(
                        "cached main-cloud backend provider does not match"
                    )
                if cached_revision == authority.secret_revision:
                    return cached
            backend = build_backend_from_instance(authority)
            try:
                initialized = await backend.ensure_initialized()
                if (
                    not initialized
                    or backend.installation_proof_sha256
                    != authority.installation_proof_sha256
                ):
                    raise FeatureNotAvailable(
                        "retained main-cloud installation proof",
                        backend=authority.backend_id,
                    )
                backend.bind_backend_instance(authority.backend_instance_id)
            except BaseException:
                try:
                    await backend.close()
                except Exception:
                    pass
                raise
            self._instances[authority.backend_instance_id] = backend
            self._instance_secret_revisions[authority.backend_instance_id] = (
                authority.secret_revision
            )
            if cached is not None and cached is not self._active:
                try:
                    await cached.close()
                except Exception as e:
                    logger.warning(
                        "Main cloud router: error closing superseded retained "
                        "instance %s: %s",
                        authority.backend_instance_id,
                        e,
                    )
            return backend

    def for_backend(self, backend_id: str | None) -> MainCloudBackend:
        """Return the backend instance for a given backend id.

        ``None`` or an empty string falls back to the active backend — this
        covers rows whose ``main_cloud_backend`` column has not been populated
        yet (e.g. very old projects created before the migration ran).
        """
        if not backend_id or backend_id == self._active.backend_id:
            return self._active
        if backend_id not in self._legacy:
            try:
                self._legacy[backend_id] = build_backend(backend_id)
            except ValueError:
                logger.warning(
                    "Main cloud router: unknown legacy backend %r — falling "
                    "back to active (%s). Operations on this row will use the "
                    "wrong backend.",
                    backend_id,
                    self._active.backend_id,
                )
                return self._active
        return self._legacy[backend_id]

    def for_project(self, project_row: dict[str, Any]) -> MainCloudBackend:
        """Dispatch to the backend that originally created this project."""
        instance_id = project_row.get("main_cloud_backend_instance_id")
        backend_id = project_row.get("main_cloud_backend")
        if instance_id:
            if not backend_id:
                raise FeatureNotAvailable(
                    "project main-cloud provider authority",
                    backend="unknown",
                )
            return self.for_backend_instance(
                str(instance_id),
                expected_backend_id=str(backend_id),
            )
        if backend_id:
            raise FeatureNotAvailable(
                "legacy project backend-instance authority",
                backend=str(backend_id),
            )
        return self.for_backend(project_row.get("main_cloud_backend"))

    def for_thread(self, thread_row: dict[str, Any]) -> MainCloudBackend:
        """Dispatch to the backend that originally created this thread."""
        instance_id = thread_row.get("main_cloud_backend_instance_id")
        backend_id = thread_row.get("main_cloud_backend")
        if instance_id:
            if not backend_id:
                raise FeatureNotAvailable(
                    "thread main-cloud provider authority",
                    backend="unknown",
                )
            return self.for_backend_instance(
                str(instance_id),
                expected_backend_id=str(backend_id),
            )
        if backend_id:
            raise FeatureNotAvailable(
                "legacy thread backend-instance authority",
                backend=str(backend_id),
            )
        return self.for_backend(thread_row.get("main_cloud_backend"))

    def for_owner(self, owner: dict[str, Any] | None = None) -> MainCloudBackend:
        """Resolve the backend for a *fresh* create on behalf of an owner.

        Call sites that mint a brand-new cloud resource (a session folder, a
        loose-job export folder, a user's personal storage) have no
        ``main_cloud_backend`` column to dispatch on yet — the resource is
        created on, and then stamped with, the active backend. Routing those
        creates through this seam (instead of reading ``active`` directly)
        keeps *all* backend acquisition going through the router.

        ``owner`` is the user/owner context. It is **reserved** for per-org
        resolution (owner → org → backend) under multi-tenancy; today every
        owner resolves to the single global active backend. Adding the
        parameter now means multi-tenancy later is "fill in the key" here,
        not a call-site refactor. See Issue 16 in knowledge-base/knowledge/issues/main_cloud.md.
        """
        if not self._active.backend_instance_id:
            raise FeatureNotAvailable(
                "durable active backend-instance authority",
                backend=self._active.backend_id,
            )
        return self._active

    async def ensure_initialized(self) -> bool:
        """Initialize the active backend. Called from the FastAPI lifespan."""
        return await self._active.ensure_initialized()

    async def replace_active(
        self,
        new_backend: MainCloudBackend,
        *,
        authority: MainCloudBackendInstanceAuthority | None = None,
    ) -> None:
        """Atomically swap the active backend and close the previous one.

        The old active backend is moved into the ``_legacy`` cache under
        its own id when the backend id changes (so in-flight reads for
        projects that were created on the old backend still route
        correctly). When the backend id is unchanged — e.g. the operator
        just edited the base URL or rotated the client secret — the old
        backend is closed outright.

        Callers MUST ensure ``new_backend.ensure_initialized()`` has
        already returned ``True`` before handing it to this method.
        This function is async-safe via ``_reload_lock``.
        """
        if authority is not None and (
            not isinstance(authority, MainCloudBackendInstanceAuthority)
            or authority.backend_id != new_backend.backend_id
            or new_backend.installation_proof_sha256
            != authority.installation_proof_sha256
        ):
            raise ValueError("replacement main-cloud installation proof does not match")
        async with self._reload_lock:
            if authority is not None:
                new_backend.bind_backend_instance(authority.backend_instance_id)
            old = self._active
            if old is new_backend:
                if authority is not None:
                    self._instances[authority.backend_instance_id] = new_backend
                    self._instance_secret_revisions[authority.backend_instance_id] = (
                        authority.secret_revision
                    )
                return
            self._active = new_backend
            old_instance = old.backend_instance_id
            new_instance = new_backend.backend_instance_id
            if old_instance and old_instance != new_instance:
                self._instances[old_instance] = old
            elif old_instance and old_instance == new_instance:
                try:
                    await old.close()
                except Exception as e:
                    logger.warning(
                        "Main cloud router: error closing old %s backend "
                        "after replace_active: %s",
                        old.backend_id,
                        e,
                    )
            elif old.backend_id != new_backend.backend_id:
                # Preserve the old backend under its id so that
                # for_project/for_thread dispatch to it for pre-flip rows.
                self._legacy[old.backend_id] = old
            else:
                try:
                    await old.close()
                except Exception as e:
                    logger.warning(
                        "Main cloud router: error closing unbound old %s "
                        "backend after replace_active: %s",
                        old.backend_id,
                        e,
                    )
            if new_instance:
                self._instances[new_instance] = new_backend
                if authority is not None:
                    self._instance_secret_revisions[new_instance] = (
                        authority.secret_revision
                    )

    async def reload_from_db(self, db_overlay: Optional[dict]) -> bool:
        """Rebuild the active backend from the current DB overlay.

        Returns ``True`` if the new backend initialized successfully and
        was installed as the active backend. Returns ``False`` if
        initialization failed — in that case the current active backend
        is left in place and callers should surface a 5xx to the caller
        so the operator knows the config change did not take effect.
        """
        try:
            new_backend = build_backend(db_overlay=db_overlay)
        except Exception as e:
            logger.error("Main cloud router: build_backend failed during reload: %s", e)
            return False

        try:
            ok = await new_backend.ensure_initialized()
        except Exception as e:
            logger.error(
                "Main cloud router: ensure_initialized raised during reload: %s",
                e,
            )
            try:
                await new_backend.close()
            except Exception:  # best-effort cleanup
                pass
            return False

        if not ok:
            logger.error(
                "Main cloud router: new backend reported init failure; "
                "keeping previous active backend"
            )
            try:
                await new_backend.close()
            except Exception:  # best-effort cleanup
                pass
            return False

        await self.replace_active(new_backend)
        logger.info(
            "Main cloud router: reloaded active backend to %s",
            new_backend.backend_id,
        )
        return True

    async def close(self) -> None:
        """Close the active backend and any cached legacy backends."""
        await self._active.close()
        closed: set[int] = {id(self._active)}
        for backend in self._instances.values():
            if id(backend) in closed:
                continue
            try:
                await backend.close()
            except Exception as e:
                logger.warning(
                    "Main cloud router: error closing retained instance %s: %s",
                    backend.backend_instance_id,
                    e,
                )
            closed.add(id(backend))
        self._instances.clear()
        self._instance_secret_revisions.clear()
        for backend in self._legacy.values():
            if id(backend) in closed:
                continue
            try:
                await backend.close()
            except Exception as e:  # best-effort shutdown
                logger.warning(
                    "Main cloud router: error closing legacy backend %s: %s",
                    backend.backend_id,
                    e,
                )
        self._legacy.clear()


__all__ = [
    "REGISTRY",
    "RELOAD_CHANNEL",
    "CloudBackendError",
    "CloudBackendErrorKind",
    "CloudMountSubject",
    "FeatureNotAvailable",
    "GroupId",
    "HealthStatus",
    "LeakyBucket",
    "MainCloudBackend",
    "MainCloudBackendInstanceAuthority",
    "MainCloudConfig",
    "MainCloudRouter",
    "MS365Settings",
    "NextcloudBackend",
    "NextcloudSettings",
    "OpenCloudBackend",
    "OpenCloudSettings",
    "ProjectFolderEntry",
    "ProjectFolderHandle",
    "RcloneMountSpec",
    "SessionFolderHandle",
    "ShareHandle",
    "SupportsRcloneMount",
    "UserHome",
    "UserId",
    "build_backend",
    "build_backend_from_config",
    "build_backend_from_instance",
    "load_main_cloud_config",
    "retryable_policy",
]
