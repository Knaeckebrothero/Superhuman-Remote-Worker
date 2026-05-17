"""Coordinator that drives several ``WorkspaceSyncBase`` instances together.

Phase 1 of the cloud-mirror workspace model
(``docs/features/cloud_collaboration_model.md`` §9) introduces multiple
cloud surfaces mounted into one agent workspace — typically the legacy
session folder at the root *and* one project mount under ``projects/``.
Each mount has its own ``WorkspaceSyncBase`` instance bound to its own
WebDAV URL and its own ``mount_subdir`` inside the workspace backend.

The coordinator exists so the agent's turn-boundary callbacks can talk
to "the sync layer" without knowing how many mounts there are. It also
implements the **raise-and-block** policy from the locked Phase 1
decisions: a single mount failing a pull or push at the turn boundary
aborts the whole sync and surfaces the error to the caller, which is
expected to block the next turn until the operator has resolved it.

Per-mount errors are aggregated rather than blowing up on the first
one — that way, when several mounts are wrong at once (e.g. a credential
rotation broke them all), the operator sees every breakage in a single
report instead of having to fix them serially.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Sequence

from .base import WorkspaceSyncBase

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MountSync:
    """One mount registered with the coordinator.

    ``mount_id`` matches the ``thread_mounts.id`` row that minted this
    mount on the orchestrator side; used in logs and error messages so
    operators can correlate failures back to the database row.
    """

    mount_id: str
    target_path: str
    sync: WorkspaceSyncBase


class CloudSyncError(RuntimeError):
    """Aggregate failure from one or more mount-level syncs at a turn boundary.

    Carries the per-mount failures via ``.failures`` (a list of
    ``(mount_id, target_path, exception)`` tuples) so callers can
    structure error reporting without re-parsing the message.
    """

    def __init__(self, op: str, failures: list[tuple[str, str, BaseException]]) -> None:
        self.op = op
        self.failures = failures
        summary = ", ".join(
            f"{target_path or '<root>'} ({type(e).__name__}: {e})"
            for _id, target_path, e in failures
        )
        super().__init__(f"{op} failed for {len(failures)} mount(s): {summary}")


class WorkspaceSyncCoordinator:
    """Drive N ``WorkspaceSyncBase`` instances together with one entry point.

    Methods are concurrent (``asyncio.gather``) and aggregate failures
    rather than failing fast, so that a single dead mount doesn't hide
    other dead mounts from the operator on a turn-boundary failure.
    """

    def __init__(self, mounts: Sequence[MountSync] = ()) -> None:
        self._mounts: list[MountSync] = list(mounts)

    # -------------------------------------------------------------- Membership

    def add(self, mount: MountSync) -> None:
        self._mounts.append(mount)

    def __len__(self) -> int:
        return len(self._mounts)

    @property
    def mounts(self) -> list[MountSync]:
        return list(self._mounts)

    # -------------------------------------------------------------- Operations

    async def pull_all(self) -> dict[str, list[str]]:
        """Pull every mount concurrently. Raise if any mount failed.

        Returns a mapping of ``mount_id`` → list of pulled paths. Raises
        ``CloudSyncError`` on the aggregate if any pull raised.
        """
        return await self._run_all("pull")

    async def push_all(self) -> dict[str, list[str]]:
        """Push every mount concurrently. Raise if any mount failed."""
        return await self._run_all("push")

    async def _run_all(self, op: str) -> dict[str, list[str]]:
        if not self._mounts:
            return {}

        async def _do(mount: MountSync) -> list[str]:
            if op == "pull":
                return await mount.sync.pull(strict=True)
            return await mount.sync.push(strict=True)

        results = await asyncio.gather(
            *(_do(m) for m in self._mounts),
            return_exceptions=True,
        )

        ok: dict[str, list[str]] = {}
        failures: list[tuple[str, str, BaseException]] = []
        for mount, res in zip(self._mounts, results):
            if isinstance(res, BaseException):
                logger.warning(
                    "cloud_sync %s failed for mount %s (%s): %s",
                    op,
                    mount.target_path or "<root>",
                    mount.mount_id,
                    res,
                )
                failures.append((mount.mount_id, mount.target_path, res))
            else:
                ok[mount.mount_id] = res

        if failures:
            raise CloudSyncError(op, failures)
        return ok

    async def aclose(self) -> None:
        """Release per-mount resources. Best-effort."""
        for mount in self._mounts:
            try:
                await mount.sync.aclose()
            except Exception:
                logger.debug(
                    "cloud_sync aclose failed for mount %s",
                    mount.mount_id,
                    exc_info=True,
                )
