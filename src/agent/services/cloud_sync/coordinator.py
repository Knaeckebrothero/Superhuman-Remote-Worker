"""Coordinator that drives several ``WorkspaceSyncBase`` instances together.

Phase 1 of the cloud-mirror workspace model
(``knowledge-base/knowledge/features/cloud_collaboration_model.md`` §9) introduces multiple
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
from typing import Any, Awaitable, Callable, Sequence

from shared.cloud_sync_generations import encode_cloud_sync_baseline
from agent.services.cloud_sync.base import (
    CloudSyncMarker,
    CloudSyncMarkerError,
    WorkspaceSyncBase,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MountSync:
    """One mount registered with the coordinator.

    ``mount_id`` remains the transport/payload identity used by pinned callers
    and logs. ``generation_key`` is the stable logical destination key used by
    the stateless fence; it must not be the replace-on-edit ``thread_mounts``
    row UUID. The scope digest separately binds that key to one incarnation.
    """

    mount_id: str
    target_path: str
    sync: WorkspaceSyncBase
    sync_scope_sha256: str = ""
    generation_key: str = ""

    @property
    def generation_id(self) -> str:
        return self.generation_key or self.mount_id


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


class CloudSyncGenerationError(RuntimeError):
    """Generation identity/ordering could not be proven; pull must not run."""


class WorkspaceSyncCoordinator:
    """Drive N ``WorkspaceSyncBase`` instances together with one entry point.

    Methods are concurrent (``asyncio.gather``) and aggregate failures
    rather than failing fast, so that a single dead mount doesn't hide
    other dead mounts from the operator on a turn-boundary failure.
    """

    def __init__(
        self,
        mounts: Sequence[MountSync] = (),
        *,
        thread_id: str = "",
        workspace_generation: str = "",
    ) -> None:
        self._mounts: list[MountSync] = list(mounts)
        self.thread_id = str(thread_id)
        self.workspace_generation = str(workspace_generation)

    # -------------------------------------------------------------- Membership

    def add(self, mount: MountSync) -> None:
        self._mounts.append(mount)

    def __len__(self) -> int:
        return len(self._mounts)

    @property
    def mounts(self) -> list[MountSync]:
        return list(self._mounts)

    @property
    def mount_ids(self) -> list[str]:
        return [mount.mount_id for mount in self._mounts]

    def generation_scopes(self) -> list[Any]:
        """Return identity-only scopes (used for persisted-set validation)."""

        if not self.thread_id or not self.workspace_generation:
            raise CloudSyncGenerationError(
                "cloud sync generation scope lacks thread/workspace identity"
            )
        from shared.cloud_sync_generations import CloudSyncScope

        scopes: list[CloudSyncScope] = []
        seen: set[str] = set()
        for mount in self._mounts:
            generation_id = mount.generation_id
            if (
                not generation_id
                or generation_id in seen
                or len(mount.sync_scope_sha256) != 64
                or any(
                    char not in "0123456789abcdef" for char in mount.sync_scope_sha256
                )
            ):
                raise CloudSyncGenerationError(
                    "invalid or duplicate cloud sync generation identity: "
                    f"{generation_id}"
                )
            seen.add(generation_id)
            scopes.append(
                CloudSyncScope(
                    mount_id=generation_id,
                    workspace_generation=self.workspace_generation,
                    sync_scope_sha256=mount.sync_scope_sha256,
                )
            )
        return scopes

    async def capture_generation_scopes(self) -> list[Any]:
        """Hash every mount after pull and return DB-armable baselines."""

        from shared.cloud_sync_generations import CloudSyncScope

        identity_scopes = {scope.mount_id: scope for scope in self.generation_scopes()}

        async def _one(mount: MountSync) -> Any:
            baseline, baseline_sha256 = await mount.sync.capture_generation_baseline()
            identity = identity_scopes[mount.generation_id]
            return CloudSyncScope(
                mount_id=identity.mount_id,
                workspace_generation=identity.workspace_generation,
                sync_scope_sha256=identity.sync_scope_sha256,
                baseline_manifest=baseline,
                baseline_sha256=baseline_sha256,
            )

        return list(await asyncio.gather(*(_one(mount) for mount in self._mounts)))

    def validate_requirements(self, requirements: dict[str, Any]) -> None:
        """Require an exact configured↔persisted scope set before any pull."""

        scopes = {scope.mount_id: scope for scope in self.generation_scopes()}
        if set(requirements) != set(scopes):
            pending_unknown = sorted(
                mount_id
                for mount_id, requirement in requirements.items()
                if mount_id not in scopes
                and requirement.acknowledged_generation
                < requirement.required_generation
            )
            if pending_unknown:
                raise CloudSyncGenerationError(
                    "pending cloud generation belongs to absent mount(s): "
                    + ", ".join(pending_unknown)
                )
            # Clean rows for removed mounts are historical and harmless, but
            # all CURRENT mounts must have a row before tool work begins.
            missing = sorted(set(scopes) - set(requirements))
            if missing:
                raise CloudSyncGenerationError(
                    "cloud generation row missing for mount(s): " + ", ".join(missing)
                )
        for mount_id, scope in scopes.items():
            requirement = requirements[mount_id]
            if (
                requirement.workspace_generation != scope.workspace_generation
                or requirement.sync_scope_sha256 != scope.sync_scope_sha256
            ):
                raise CloudSyncGenerationError(
                    f"cloud generation scope changed for mount {mount_id}"
                )
            try:
                _manifest, _encoded, digest = encode_cloud_sync_baseline(
                    requirement.baseline_manifest
                )
            except ValueError as exc:
                raise CloudSyncGenerationError(
                    f"cloud generation baseline is invalid for mount {mount_id}"
                ) from exc
            if requirement.baseline_sha256 != digest:
                raise CloudSyncGenerationError(
                    f"cloud generation baseline digest changed for mount {mount_id}"
                )

    # -------------------------------------------------------------- Operations

    async def pull_all(
        self,
        *,
        before_write: Callable[[], Awaitable[None]] | None = None,
        force_unknown: bool = False,
    ) -> dict[str, list[str]]:
        """Pull every mount concurrently. Raise if any mount failed.

        Returns a mapping of ``mount_id`` → list of pulled paths. Raises
        ``CloudSyncError`` on the aggregate if any pull raised.
        """
        return await self._run_all(
            "pull", before_write=before_write, force_unknown=force_unknown
        )

    async def push_all(self) -> dict[str, list[str]]:
        """Push every mount concurrently. Raise if any mount failed."""
        return await self._run_all("push")

    async def push_generation(
        self,
        requirements: dict[str, Any],
        *,
        before_write: Callable[[], Awaitable[None]],
        acknowledge: Callable[[str, Any], Awaitable[None]],
    ) -> dict[str, list[str]]:
        """Commit each armed generation from its durable content baseline."""

        self.validate_requirements(requirements)

        async def _one(mount: MountSync) -> list[str]:
            generation_id = mount.generation_id
            requirement = requirements[generation_id]
            mount.sync.install_generation_baseline(requirement.baseline_manifest)
            existing = await mount.sync.read_sync_generation_marker(
                thread_id=self.thread_id,
                sync_scope_sha256=requirement.sync_scope_sha256,
            )
            if self._marker_commits_requirement(
                mount, requirement=requirement, marker=existing
            ):
                # A previous whole-operation or multi-mount retry got this far.
                # Mirror the resource truth into DB, but never replay the delta
                # over a cloud-side edit that arrived after the marker.
                await acknowledge(generation_id, requirement)
                return []
            await before_write()
            commit = await mount.sync.push_generation_delta(
                requirement.baseline_manifest,
                before_write=before_write,
            )
            marker = CloudSyncMarker(
                thread_id=self.thread_id,
                mount_id=generation_id,
                generation=requirement.required_generation,
                lease_token=requirement.required_lease_token,
                workspace_generation=requirement.workspace_generation,
                sync_scope_sha256=requirement.sync_scope_sha256,
                baseline_sha256=requirement.baseline_sha256,
                committed_manifest=commit.manifest,
                committed_manifest_sha256=commit.manifest_sha256,
            )
            await mount.sync.write_sync_generation_marker(
                marker,
                before_write=before_write,
            )
            await acknowledge(generation_id, requirement)
            mount.sync.install_generation_baseline(commit.manifest)
            return commit.paths

        return await self._run_generation_all("generation_push", _one)

    async def reconcile_before_pull(
        self,
        requirements: dict[str, Any],
        *,
        before_write: Callable[[], Awaitable[None]],
        acknowledge: Callable[[str, Any], Awaitable[None]],
    ) -> dict[str, list[str]]:
        """Repair/validate the prior generation before pull(N+1)."""

        # Fail before any resource read/write when two payload entries collapse
        # to one durable logical key.
        self.generation_scopes()
        current = {mount.generation_id: mount for mount in self._mounts}
        configured = set(current)
        pending_unknown = sorted(
            mount_id
            for mount_id, requirement in requirements.items()
            if mount_id not in configured
            and requirement.acknowledged_generation < requirement.required_generation
        )
        if pending_unknown:
            raise CloudSyncGenerationError(
                "pending cloud generation belongs to absent mount(s): "
                + ", ".join(pending_unknown)
            )

        async def _one(mount: MountSync) -> list[str]:
            generation_id = mount.generation_id
            requirement = requirements.get(generation_id)
            if requirement is None:
                # First claim for a newly configured surface: there is no
                # predecessor generation to recover. Pull runs next, then the
                # current claim arms the first generation before tool work.
                return []
            scope_changed = (
                requirement.workspace_generation != self.workspace_generation
                or requirement.sync_scope_sha256 != mount.sync_scope_sha256
            )
            if scope_changed:
                if (
                    requirement.acknowledged_generation
                    < requirement.required_generation
                ):
                    raise CloudSyncGenerationError(
                        f"pending cloud generation scope changed for mount "
                        f"{generation_id}"
                    )
                # A fully committed predecessor belongs to another durable
                # workspace/resource incarnation.  There is nothing to replay
                # into the new scope; treat it like first use, pull that scope,
                # then let arm_cloud_sync_generations replace the clean row.
                return []
            mount.sync.install_generation_baseline(requirement.baseline_manifest)
            marker = await mount.sync.read_sync_generation_marker(
                thread_id=self.thread_id,
                sync_scope_sha256=requirement.sync_scope_sha256,
            )
            if marker is not None:
                expected_identity = (
                    self.thread_id,
                    generation_id,
                    requirement.workspace_generation,
                    requirement.sync_scope_sha256,
                )
                actual_identity = (
                    marker.thread_id,
                    marker.mount_id,
                    marker.workspace_generation,
                    marker.sync_scope_sha256,
                )
                if actual_identity != expected_identity:
                    raise CloudSyncMarkerError(
                        f"cloud marker identity mismatch for {mount.mount_id}"
                    )
            if self._marker_commits_requirement(
                mount, requirement=requirement, marker=marker
            ):
                mount.sync.install_generation_baseline(marker.committed_manifest)
                if (
                    requirement.acknowledged_generation
                    < requirement.required_generation
                ):
                    await acknowledge(generation_id, requirement)
                return []

            # Crash/resource rollback recovery: replay only the paths changed
            # from the durable post-pull baseline. Untouched cloud edits survive.
            await before_write()
            commit = await mount.sync.push_generation_delta(
                requirement.baseline_manifest,
                before_write=before_write,
            )
            repaired = CloudSyncMarker(
                thread_id=self.thread_id,
                mount_id=generation_id,
                generation=requirement.required_generation,
                lease_token=requirement.required_lease_token,
                workspace_generation=requirement.workspace_generation,
                sync_scope_sha256=requirement.sync_scope_sha256,
                baseline_sha256=requirement.baseline_sha256,
                committed_manifest=commit.manifest,
                committed_manifest_sha256=commit.manifest_sha256,
            )
            await mount.sync.write_sync_generation_marker(
                repaired,
                before_write=before_write,
            )
            await acknowledge(generation_id, requirement)
            mount.sync.install_generation_baseline(commit.manifest)
            return commit.paths

        return await self._run_generation_all("generation_recovery", _one)

    def _marker_commits_requirement(
        self,
        mount: MountSync,
        *,
        requirement: Any,
        marker: CloudSyncMarker | None,
    ) -> bool:
        """Validate marker ordering and answer whether it exactly commits req."""

        if marker is None:
            return False
        expected_identity = (
            self.thread_id,
            mount.generation_id,
            requirement.workspace_generation,
            requirement.sync_scope_sha256,
        )
        actual_identity = (
            marker.thread_id,
            marker.mount_id,
            marker.workspace_generation,
            marker.sync_scope_sha256,
        )
        if actual_identity != expected_identity:
            raise CloudSyncMarkerError(
                f"cloud marker identity mismatch for {mount.mount_id}"
            )
        if marker.generation > requirement.required_generation:
            raise CloudSyncMarkerError(f"cloud marker is ahead for {mount.mount_id}")
        if marker.lease_token != marker.generation:
            raise CloudSyncMarkerError(
                f"cloud marker lease token mismatches generation for {mount.mount_id}"
            )
        if marker.generation < requirement.required_generation:
            return False
        if (
            marker.lease_token != requirement.required_lease_token
            or marker.baseline_sha256 != requirement.baseline_sha256
        ):
            raise CloudSyncMarkerError(
                f"cloud marker commit identity mismatches {mount.mount_id}"
            )
        return True

    async def _run_generation_all(
        self,
        op: str,
        operation: Callable[[MountSync], Awaitable[list[str]]],
    ) -> dict[str, list[str]]:
        results = await asyncio.gather(
            *(operation(mount) for mount in self._mounts),
            return_exceptions=True,
        )
        ok: dict[str, list[str]] = {}
        failures: list[tuple[str, str, BaseException]] = []
        for mount, result in zip(self._mounts, results):
            if isinstance(result, BaseException):
                failures.append((mount.mount_id, mount.target_path, result))
            else:
                ok[mount.mount_id] = result
        if failures:
            raise CloudSyncError(op, failures)
        return ok

    async def _run_all(
        self,
        op: str,
        *,
        before_write: Callable[[], Awaitable[None]] | None = None,
        force_unknown: bool = False,
    ) -> dict[str, list[str]]:
        if not self._mounts:
            return {}

        async def _do(mount: MountSync) -> list[str]:
            if op == "pull":
                if before_write is not None:
                    return await mount.sync.pull(
                        strict=True,
                        before_write=before_write,
                        force_unknown=force_unknown,
                    )
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
