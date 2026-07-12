"""Protected cloud mode (Slice C, Task 10) — apply / reject engine.

The security-critical write path for the whole feature: the ONLY place a
staged agent diff (fuse-overlayfs upperdir, captured by
``services.cloud_staging.stage``) reaches the user's real cloud folder, and
strictly on explicit user action (owner-only endpoints in ``main.py``).

Both flows share the same epoch pin (spec §7): the caller passes the
``epoch`` it last observed via the summary endpoint (Task 8), and a stale
epoch — someone else applied/rejected/restaged since — is a hard 409 before
any read or write happens. Apply additionally re-checks the cloud folder
against the mount's etag baseline, scoped to only the paths the staged diff
touches (``detect_external_mods_against_baseline``,
``services.job_cloud_baseline``); any divergence is a hard 409 with no force
flag — the user must resolve manually (restage or investigate) and retry.

Apply writes deletes before creates (whiteout-before-create) and is
sequential/fail-soft: each write is attempted independently, errors are
collected, and a partial failure returns them without touching staging state
at all — the retry is safe because every write here is idempotent (PUT
overwrite, DELETE ``if_exists=True``). Only full success re-captures the
etag baseline, deletes the staged S3 blobs, and advances the staging epoch
(clearing ``staged_summary``).

Reject never touches the cloud — it just resets the agent-side overlay
(best-effort), drops the staged S3 blobs, and advances the epoch the same
way. No baseline re-capture (nothing changed on the cloud side).

Known v1 limitation: apply/reject resets the *live* agent's overlay via an
HTTP call to the workspace pod (``reset_agent_overlay``), which is a no-op
when the pod is dead — normal, not fatal (see ``main._reset_thread_overlay``
docstring). If the pod is dead at apply/reject time, the overlay inside the
*last workspace snapshot* still carries the old upperdir; a later resume
restores that snapshot, and the next turn-end stage push re-stages the
already-applied (or already-rejected) content against the fresh baseline —
surfacing as modified-with-identical-content entries the user re-rejects.
Accepted for v1 (spec §7); a v2 fix would need the resume path itself to
clear the upperdir before the workspace comes back up.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from ..cloud import ProjectFolderHandle
from ..diff_source import UpperdirDiffSource
from ..job_cloud_baseline import detect_external_mods_against_baseline
from . import select_protected_mount
from .stage import staging_manifest_key, staging_tar_key

logger = logging.getLogger(__name__)


class StagedApplyError(Exception):
    """Carries an HTTP status + JSON detail body; endpoints map it verbatim."""

    def __init__(self, status_code: int, detail: dict[str, Any]) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def _load_pinned_mount_row(
    *, thread_id: str, epoch: int, postgres_db: Any
) -> dict[str, Any]:
    """Steps shared by apply and reject: load the mount row, then the two
    gates that must run before ANY read or write — "nothing staged" and the
    epoch pin. Raises :class:`StagedApplyError` on either gate.
    """
    row = await postgres_db.get_ro_mount_by_thread(thread_id)
    if not row or row.get("staged_summary") is None:
        raise StagedApplyError(409, {"code": "nothing_staged"})
    if epoch != row["staged_epoch"]:
        raise StagedApplyError(
            409, {"code": "epoch_stale", "staged_epoch": row["staged_epoch"]}
        )
    return row


async def apply_staged_diff(
    *,
    thread_id: str,
    epoch: int,
    postgres_db: Any,
    main_cloud_router: Any,
    snapshot_service: Any,
    reset_agent_overlay: Callable[[], Awaitable[bool]],
) -> dict[str, Any]:
    """Write the staged diff to the cloud, whole-diff, epoch-pinned.

    Returns ``{"applied", "deleted", "errors": [...]}`` on partial failure
    (staging state left untouched — retry is safe) or ``{"applied",
    "deleted", "errors": [], "epoch", "overlay_reset"}`` on full success.
    Raises :class:`StagedApplyError` for every gate short-circuit (409/410).
    """
    row = await _load_pinned_mount_row(
        thread_id=thread_id, epoch=epoch, postgres_db=postgres_db
    )

    mount_rows = await postgres_db.list_thread_mounts(thread_id)
    sel = select_protected_mount(mount_rows)
    if sel is None:
        raise StagedApplyError(409, {"code": "no_protected_mount"})
    backend = main_cloud_router.for_backend(row["backend"])
    handle = ProjectFolderHandle.from_db(str(sel["cloud_handle"]), backend=row["backend"])

    src = UpperdirDiffSource(
        thread_id=thread_id,
        mount_row=row,
        backend=backend,
        handle=handle,
        snapshot_service=snapshot_service,
    )
    summary = await src.summary()
    if summary is None:
        # DB says staged but the S3 blobs are gone (or the tar/manifest
        # content-binding check failed — a torn multi-replica pair reads the
        # same way). The user restages.
        raise StagedApplyError(410, {"code": "staging_missing"})

    touched_paths = {f.path for f in summary.files}
    diverged = await detect_external_mods_against_baseline(
        baseline_entries=row.get("etag_baseline") or {},
        backend=backend,
        handle=handle,
        scope_paths=touched_paths,
    )
    if diverged:
        raise StagedApplyError(
            409,
            {
                "code": "external_modifications_detected",
                "message": (
                    "Cloud folder was modified externally since staging. "
                    "Resolve manually before applying."
                ),
                "diverged": diverged,
            },
        )

    # Deletes first (whiteout-before-create), children before parents —
    # harmless ordering for plain files, correct if a future backend ever
    # deletes directories. Sequential + fail-soft: every write is
    # independently idempotent, so collecting errors and returning them
    # (instead of aborting) lets the caller retry safely.
    deletes = sorted(
        (f.path for f in summary.files if f.status == "deleted"), reverse=True
    )
    creates = [f.path for f in summary.files if f.status != "deleted"]

    applied = 0
    deleted = 0
    errors: list[str] = []
    for path in deletes:
        try:
            await backend.delete_project_folder_file(handle, path=path, if_exists=True)
            deleted += 1
        except Exception as e:  # noqa: BLE001 - fail-soft by design
            errors.append(f"{path}: {e}")
    for path in creates:
        try:
            raw = await src.raw_new_bytes(path)
            if raw is None:
                errors.append(f"{path}: staged content missing from upperdir tar")
                continue
            await backend.put_project_folder_file_bytes(handle, path=path, content=raw)
            applied += 1
        except Exception as e:  # noqa: BLE001 - fail-soft by design
            errors.append(f"{path}: {e}")

    if errors:
        # Partial failure: leave staging state exactly as it was. Every
        # write above is idempotent, so a retry (same epoch, same staged
        # content) is always safe.
        logger.warning(
            "apply: thread %s partial failure (%d applied, %d deleted, %d errors)",
            thread_id,
            applied,
            deleted,
            len(errors),
        )
        return {"applied": applied, "deleted": deleted, "errors": errors}

    # Full success. Order matters: reset the live agent overlay first (it's
    # best-effort and independent of the rest), then re-derive the new
    # baseline from what's now actually on the cloud, persist it, drop the
    # staged blobs, and only then clear the staging row — so a crash
    # mid-sequence never leaves staging cleared without a fresh baseline.
    overlay_reset = await reset_agent_overlay()
    new_baseline = await backend.capture_etag_baseline(handle)
    await postgres_db.update_ro_mount_baseline(row["id"], new_baseline)
    await snapshot_service.delete_blob(staging_tar_key(thread_id))
    await snapshot_service.delete_blob(staging_manifest_key(thread_id))
    new_epoch = row["staged_epoch"] + 1
    await postgres_db.update_ro_mount_staging(
        row["id"], staged_epoch=new_epoch, staged_summary=None
    )
    logger.info(
        "apply: thread %s applied %d, deleted %d, epoch -> %d (overlay_reset=%s)",
        thread_id,
        applied,
        deleted,
        new_epoch,
        overlay_reset,
    )
    return {
        "applied": applied,
        "deleted": deleted,
        "errors": [],
        "epoch": new_epoch,
        "overlay_reset": overlay_reset,
    }


async def reject_staged_diff(
    *,
    thread_id: str,
    epoch: int,
    postgres_db: Any,
    snapshot_service: Any,
    reset_agent_overlay: Callable[[], Awaitable[bool]],
) -> dict[str, Any]:
    """Discard the staged diff without ever touching the cloud.

    Same pins as apply (nothing-staged / epoch-stale), then: reset the agent
    overlay (best-effort), drop both staged S3 blobs, and advance the
    staging epoch. No baseline re-capture — the cloud folder never changed.
    """
    row = await _load_pinned_mount_row(
        thread_id=thread_id, epoch=epoch, postgres_db=postgres_db
    )

    overlay_reset = await reset_agent_overlay()
    await snapshot_service.delete_blob(staging_tar_key(thread_id))
    await snapshot_service.delete_blob(staging_manifest_key(thread_id))
    new_epoch = row["staged_epoch"] + 1
    await postgres_db.update_ro_mount_staging(
        row["id"], staged_epoch=new_epoch, staged_summary=None
    )
    logger.info(
        "reject: thread %s rejected staged diff, epoch -> %d (overlay_reset=%s)",
        thread_id,
        new_epoch,
        overlay_reset,
    )
    return {"rejected": True, "epoch": new_epoch, "overlay_reset": overlay_reset}
