"""Main cloud abstraction package.

Single entry point for main-cloud backends. The orchestrator imports
``MainCloudRouter`` + ``build_backend`` and uses the router's ``active``,
``for_project(row)``, and ``for_thread(row)`` accessors to reach the right
backend instance. Individual backends live in siblings of this module
(``nextcloud.py`` today; ``opencloud.py`` / ``ms365.py`` in later phases).

See ``docs/features/main_cloud_abstraction.md`` for the full design.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .base import HealthStatus, MainCloudBackend, UserHome
from .config import (
    MainCloudConfig,
    MS365Settings,
    NextcloudSettings,
    OpenCloudSettings,
    load_main_cloud_config,
)
from .errors import CloudBackendError, CloudBackendErrorKind, FeatureNotAvailable
from .handles import (
    GroupId,
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

    ``NextcloudBackend`` still reads env vars directly — the Pydantic
    settings class exists for symmetry and validation but the legacy
    adapter predates it and the behaviour is identical. When a
    ``db_overlay`` is present, the overlay is still honored for the
    backend_id decision but Nextcloud settings stay env-driven.
    """
    if backend_id is None:
        settings = load_main_cloud_config(db_overlay=db_overlay)
        backend_id = settings.backend_id
        if backend_id == "opencloud":
            assert isinstance(settings, OpenCloudSettings)
            return OpenCloudBackend(settings)
        # Nextcloud falls through to the env-var constructor below so
        # legacy deployments that never set MAIN_CLOUD_BACKEND keep
        # working with zero config churn.

    if backend_id == "nextcloud":
        return NextcloudBackend()
    if backend_id == "opencloud":
        settings = load_main_cloud_config(
            backend_override="opencloud", db_overlay=db_overlay
        )
        assert isinstance(settings, OpenCloudSettings)
        return OpenCloudBackend(settings)
    raise ValueError(
        f"unknown main cloud backend: {backend_id!r} (known: {sorted(REGISTRY)})"
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
        self._reload_lock = asyncio.Lock()

    @property
    def active(self) -> MainCloudBackend:
        return self._active

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
        return self.for_backend(project_row.get("main_cloud_backend"))

    def for_thread(self, thread_row: dict[str, Any]) -> MainCloudBackend:
        """Dispatch to the backend that originally created this thread."""
        return self.for_backend(thread_row.get("main_cloud_backend"))

    async def ensure_initialized(self) -> bool:
        """Initialize the active backend. Called from the FastAPI lifespan."""
        return await self._active.ensure_initialized()

    async def replace_active(self, new_backend: MainCloudBackend) -> None:
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
        async with self._reload_lock:
            old = self._active
            self._active = new_backend
            if old.backend_id == new_backend.backend_id:
                try:
                    await old.close()
                except Exception as e:
                    logger.warning(
                        "Main cloud router: error closing old %s backend "
                        "after replace_active: %s",
                        old.backend_id,
                        e,
                    )
            else:
                # Preserve the old backend under its id so that
                # for_project/for_thread dispatch to it for pre-flip rows.
                self._legacy[old.backend_id] = old

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
        for backend in self._legacy.values():
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
    "FeatureNotAvailable",
    "GroupId",
    "HealthStatus",
    "LeakyBucket",
    "MainCloudBackend",
    "MainCloudConfig",
    "MainCloudRouter",
    "MS365Settings",
    "NextcloudBackend",
    "NextcloudSettings",
    "OpenCloudBackend",
    "OpenCloudSettings",
    "ProjectFolderHandle",
    "SessionFolderHandle",
    "ShareHandle",
    "UserHome",
    "UserId",
    "build_backend",
    "load_main_cloud_config",
    "retryable_policy",
]
