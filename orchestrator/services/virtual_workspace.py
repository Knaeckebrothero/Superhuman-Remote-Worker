"""Deployment-owned virtual-workspace transport configuration.

The persisted thread row contains only the selected backend. Credentials are
reconstructed from orchestrator environment/Secret wiring at request time and
are never copied into thread or Canvas JSONB state.
"""

from __future__ import annotations

import os
from typing import Any


def virtual_workspace_rclone_spec() -> dict[str, Any] | None:
    """Return the configured ``{type, config, root}`` rclone descriptor."""

    remote_type = os.environ.get("VIRTUAL_WORKSPACE_RCLONE_TYPE", "").strip()
    if not remote_type:
        return None

    root = os.environ.get("VIRTUAL_WORKSPACE_RCLONE_ROOT", "").strip()
    config: dict[str, Any] = {}
    if remote_type == "s3":
        config = {
            "provider": os.environ.get("VIRTUAL_WORKSPACE_S3_PROVIDER", "").strip()
            or "Minio",
            "access_key_id": os.environ.get("VIRTUAL_WORKSPACE_S3_ACCESS_KEY_ID", ""),
            "secret_access_key": os.environ.get(
                "VIRTUAL_WORKSPACE_S3_SECRET_ACCESS_KEY", ""
            ),
            "endpoint": os.environ.get("VIRTUAL_WORKSPACE_S3_ENDPOINT", "").strip(),
            "region": os.environ.get("VIRTUAL_WORKSPACE_S3_REGION", "").strip()
            or "us-east-1",
            "no_check_bucket": "true",
        }
    return {"type": remote_type, "config": config, "root": root}


__all__ = ["virtual_workspace_rclone_spec"]
