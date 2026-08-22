"""Controller-attested SSH readiness for same-cluster VM workspaces."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any
from uuid import UUID, uuid4

from services import resolve_ssh_key_path

from .completion import probe_workspace_ssh
from .ide_settings import seed_ide_config_for_user
from .ssh_helpers import wait_for_agent_ssh

logger = logging.getLogger(__name__)


def _vm_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _generation(value: object) -> str | None:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError):
        return None
    return str(parsed) if str(parsed) == value else None


class VMReadinessService:
    """DB-rearmable readiness prober with bounded concurrency and backoff."""

    def __init__(
        self,
        db: Any,
        provisioner: Any,
        *,
        trigger_dispatch: Callable[[], None],
        max_inflight: int | None = None,
        ready_rescan_s: float = 60.0,
    ) -> None:
        self._db = db
        self._provisioner = provisioner
        self._trigger_dispatch = trigger_dispatch
        configured = max_inflight or int(os.getenv("VM_READINESS_MAX_INFLIGHT", "8"))
        self._max_inflight = max(1, configured)
        self._inflight: set[tuple[str, str, str]] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._retry_after: dict[tuple[str, str, str], float] = {}
        self._failures: dict[tuple[str, str, str], int] = {}
        self._ready_rescan_s = ready_rescan_s
        self._last_ready_scan = 0.0

    async def run(self, shutdown_event: asyncio.Event) -> None:
        logger.info("Same-cluster VM readiness prober started")
        while not shutdown_event.is_set():
            try:
                await self.run_cycle()
            except Exception:
                logger.exception("VM readiness cycle failed")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("Same-cluster VM readiness prober stopped")

    async def run_cycle(self) -> None:
        rows: list[tuple[str, Mapping[str, Any], bool]] = []
        jobs, threads = await asyncio.gather(
            self._db.list_job_vm_readiness_candidates(),
            self._db.list_thread_vm_readiness_candidates(),
        )
        rows.extend(("job", row, False) for row in jobs)
        rows.extend(("thread", row, False) for row in threads)

        now = time.monotonic()
        if now - self._last_ready_scan >= self._ready_rescan_s:
            ready_jobs, ready_threads = await asyncio.gather(
                self._db.list_job_vm_readiness_candidates(ready=True),
                self._db.list_thread_vm_readiness_candidates(ready=True),
            )
            rows.extend(("job", row, True) for row in ready_jobs)
            rows.extend(("thread", row, True) for row in ready_threads)
            self._last_ready_scan = now

        candidate_keys = {
            key
            for entity_type, row, _reprobe in rows
            if (key := self._candidate_key(entity_type, row)) is not None
        }
        self._failures = {
            key: failures
            for key, failures in self._failures.items()
            if key in candidate_keys
        }
        self._retry_after = {
            key: retry_at
            for key, retry_at in self._retry_after.items()
            if key in candidate_keys
        }
        for entity_type, row, reprobe in rows:
            if len(self._inflight) >= self._max_inflight:
                break
            key = self._candidate_key(entity_type, row)
            if (
                key is None
                or key in self._inflight
                or time.monotonic() < self._retry_after.get(key, 0.0)
            ):
                continue
            self._inflight.add(key)
            task = asyncio.create_task(
                self._run_probe_task(entity_type, row, reprobe, key)
            )
            self._tasks.add(task)
            task.add_done_callback(self._probe_done)

        # Give freshly-created tasks a chance to start without making a DB scan
        # wait for controller/SSH timeouts. The next cycle deduplicates them via
        # ``_inflight``.
        await asyncio.sleep(0)

    @staticmethod
    def _candidate_key(
        entity_type: str, row: Mapping[str, Any]
    ) -> tuple[str, str, str] | None:
        entity_id = str(row.get("entity_id") or "")
        generation = _generation(_vm_object(row.get("vm")).get("provision_generation"))
        if not entity_id or generation is None:
            return None
        return entity_type, entity_id, generation

    async def _run_probe_task(
        self,
        entity_type: str,
        row: Mapping[str, Any],
        reprobe: bool,
        key: tuple[str, str, str],
    ) -> None:
        _, entity_id, generation = key
        vm = _vm_object(row.get("vm"))
        try:
            await self._probe(entity_type, entity_id, generation, vm, row, reprobe)
        finally:
            self._inflight.discard(key)

    def _probe_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "VM readiness probe task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _probe(
        self,
        entity_type: str,
        entity_id: str,
        generation: str,
        vm: Mapping[str, Any],
        row: Mapping[str, Any],
        reprobe: bool,
    ) -> None:
        key = (entity_type, entity_id, generation)
        status = await self._provisioner.query_status(
            entity_id, entity_type=entity_type
        )
        if not isinstance(status, Mapping):
            if reprobe:
                logger.debug(
                    "Ready VM %s %s status query unavailable; preserving readiness",
                    entity_type,
                    entity_id,
                )
                return
            await self._transient_failure(
                key,
                entity_type,
                entity_id,
                generation,
                vm,
                "controller status unavailable",
                reprobe=False,
            )
            return

        reported_generation = status.get("provision_generation")
        if reported_generation not in (None, generation):
            logger.debug(
                "Ignoring VM status for stale generation on %s %s",
                entity_type,
                entity_id,
            )
            return

        if status.get("status") == "not_found":
            await self._provisioner._set_context_if_generation(
                entity_type,
                entity_id,
                generation,
                {"status": "ssh_unreachable", "ssh_probe_error": "vm not found"},
                require_status_not_ready=not reprobe,
            )
            return

        phase = str(status.get("phase") or "")
        if phase.lower() in {"stopped", "succeeded"}:
            await self._provisioner._set_context_if_generation(
                entity_type,
                entity_id,
                generation,
                {"status": "ssh_unreachable", "ssh_probe_error": "vm stopped"},
                require_status_not_ready=not reprobe,
            )
            return

        pod_ip = status.get("pod_ip")
        active_pod_uid = status.get("active_pod_uid")
        if (
            reprobe
            and pod_ip == vm.get("pod_ip")
            and active_pod_uid == vm.get("active_pod_uid")
        ):
            return
        if (
            not isinstance(pod_ip, str)
            or not pod_ip
            or not isinstance(active_pod_uid, str)
            or not active_pod_uid
            or status.get("ready") is not True
        ):
            return

        if not await probe_workspace_ssh(pod_ip, 22):
            if reprobe:
                logger.debug(
                    "Ready VM %s %s failed changed-pod TCP probe; preserving readiness",
                    entity_type,
                    entity_id,
                )
                return
            await self._transient_failure(
                key,
                entity_type,
                entity_id,
                generation,
                vm,
                "TCP probe failed",
                reprobe=reprobe,
            )
            return
        ready, _attempts, error = await wait_for_agent_ssh(
            pod_ip,
            22,
            key_path=resolve_ssh_key_path(),
            deadline_s=10.0,
            connect_timeout_s=10,
            interval_s=0.5,
        )
        if not ready:
            if reprobe:
                logger.debug(
                    "Ready VM %s %s failed changed-pod SSH auth; preserving readiness",
                    entity_type,
                    entity_id,
                )
                return
            await self._transient_failure(
                key,
                entity_type,
                entity_id,
                generation,
                vm,
                error or "SSH authentication failed",
                reprobe=reprobe,
            )
            return

        verified_at = datetime.now(timezone.utc).isoformat()
        promoted = await self._provisioner._set_context_if_generation(
            entity_type,
            entity_id,
            generation,
            {
                "status": "ready",
                "ssh_host": pod_ip,
                "pod_ip": pod_ip,
                "ssh_port": 22,
                "active_pod_uid": active_pod_uid,
                "ssh_ready_source": "provisioner_probe",
                "ssh_verified_at": verified_at,
                "ssh_registration_id": uuid4().hex,
                "ssh_probe_error": None,
                "recovering": False,
            },
            require_status_not_ready=not reprobe,
        )
        if not promoted:
            return
        self._failures.pop(key, None)
        self._retry_after.pop(key, None)
        try:
            await seed_ide_config_for_user(self._db, row.get("user_id"), pod_ip, 22)
        except Exception:
            logger.exception(
                "IDE settings seed failed for %s %s", entity_type, entity_id
            )
        if entity_type == "job":
            self._trigger_dispatch()

    async def _transient_failure(
        self,
        key: tuple[str, str, str],
        entity_type: str,
        entity_id: str,
        generation: str,
        vm: Mapping[str, Any],
        error: str,
        *,
        reprobe: bool,
    ) -> None:
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        self._retry_after[key] = time.monotonic() + min(
            60.0, 3.0 * (2 ** (failures - 1))
        )
        await self._provisioner._set_context_if_generation(
            entity_type,
            entity_id,
            generation,
            {
                "status": "ssh_pending",
                "ssh_probe_error": error[:500],
                "ssh_probe_attempts": int(vm.get("ssh_probe_attempts") or 0) + 1,
            },
            require_status_not_ready=not reprobe,
        )


async def vm_readiness_prober(
    shutdown_event: asyncio.Event,
    *,
    db: Any,
    provisioner: Any,
    trigger_dispatch: Callable[[], None],
) -> None:
    if os.getenv("VM_MODE", "off").strip().lower() != "same-cluster":
        return
    await VMReadinessService(db, provisioner, trigger_dispatch=trigger_dispatch).run(
        shutdown_event
    )
