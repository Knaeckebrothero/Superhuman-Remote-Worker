"""Protected cloud Slice C — staging pipeline (stage → S3 → review → apply)."""

from __future__ import annotations


def select_protected_mount(mount_rows: list[dict]) -> dict | None:
    """The one definition of which of a thread's mounts is the protected one:
    the first Nextcloud mount that has a cloud handle. Mirrors (and replaces)
    the inline pick in main._engage_protected_cloud_for_thread."""
    for row in mount_rows or []:
        if row.get("backend_id") == "nextcloud" and row.get("cloud_handle"):
            return row
    return None
