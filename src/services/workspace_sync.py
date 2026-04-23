"""Deprecation shim — use ``src.services.cloud_sync`` instead.

This module historically held the monolithic ``WorkspaceSyncService`` that
only spoke Nextcloud WebDAV with basic auth. It now re-exports the
``NextcloudWorkspaceSync`` subclass from the new cloud_sync package for one
release; new code should call ``build_workspace_sync(...)`` from
``src.services.cloud_sync``.
"""

from __future__ import annotations

import warnings

from .cloud_sync import (
    SYNC_IGNORE_PATTERNS,
    NextcloudWorkspaceSync as WorkspaceSyncService,
)
from .cloud_sync.base import _should_ignore

warnings.warn(
    "src.services.workspace_sync.WorkspaceSyncService is deprecated; "
    "use src.services.cloud_sync.build_workspace_sync(...) instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["WorkspaceSyncService", "SYNC_IGNORE_PATTERNS", "_should_ignore"]
