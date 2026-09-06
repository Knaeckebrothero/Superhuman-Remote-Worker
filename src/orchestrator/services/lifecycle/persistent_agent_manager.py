"""Lifecycle adapter for dedicated persistent-thread pods.

This is deliberately a thin specialization registered in the existing
``InstanceLifecycleReconciler``.  The recycler owns every mutation; this
adapter only joins Kubernetes observations to durable thread/agent authority.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator.services.persistent_recycler import (
    PersistentPodObservation,
    PersistentThreadRecycler,
)
from orchestrator.services.lifecycle.types import Instance

logger = logging.getLogger(__name__)

_MISSING = "__persistent_pod_missing__"
_RECYCLE = "__persistent_recycle_active__"
_MISMATCH = "__persistent_authority_mismatch__"
_CURRENT = "__persistent_current__"


class PersistentAgentInstanceManager:
    """Commissioned Officer pods, compared only with their own image SHA."""

    kind = "persistent-agent"

    def __init__(
        self,
        provisioner: Any,
        db: Any,
        recycler: PersistentThreadRecycler,
        *,
        automatic_enabled: bool = False,
    ):
        self._provisioner = provisioner
        self._db = db
        self._recycler = recycler
        self._automatic_enabled = bool(automatic_enabled)

    @property
    def automatic_enabled(self) -> bool:
        return self._automatic_enabled

    async def expected_versions(self) -> set[str]:
        sha = self._provisioner.expected_build_sha
        # Even a local ``:latest`` deployment still needs missing-pod and
        # in-progress reconciliation.  Only SHA comparison is disabled.
        return {sha or _CURRENT}

    async def list_instances(self) -> list[Instance]:
        if not self._provisioner.is_available:
            return []
        pods = await self._list_pods()
        labelled_thread_ids: list[uuid.UUID] = []
        for pod in pods:
            raw = dict(getattr(pod.metadata, "labels", None) or {}).get("srw/thread-id")
            try:
                if raw:
                    labelled_thread_ids.append(uuid.UUID(str(raw)))
            except ValueError:
                logger.warning("Persistent pod carries an invalid thread label")
        rows = await self._thread_rows(labelled_thread_ids)
        pods_by_thread: dict[str, Any] = {}
        for pod in pods:
            labels = dict(getattr(pod.metadata, "labels", None) or {})
            thread_id = labels.get("srw/thread-id")
            if thread_id:
                pods_by_thread[str(thread_id)] = pod

        instances: list[Instance] = []
        for row in rows:
            thread_id = str(row["id"])
            pod = pods_by_thread.pop(thread_id, None)
            metadata = self._metadata(row.get("metadata"))
            recycle = self._metadata(
                self._metadata(metadata.get("agent_pod")).get("recycle")
            )
            agent_present = row.get("agent_row_id") is not None
            pod_uid = ""
            labels: dict[str, str] = {}
            phase = "Missing"
            ready = False
            terminating = False
            created_at = None
            version = _MISSING
            pod_name = f"persistent-{thread_id[:12]}"
            if pod is not None:
                labels = dict(getattr(pod.metadata, "labels", None) or {})
                pod_uid = str(getattr(pod.metadata, "uid", "") or "")
                pod_name = str(pod.metadata.name)
                phase = str(getattr(pod.status, "phase", "Unknown") or "Unknown")
                statuses = getattr(pod.status, "container_statuses", None) or []
                ready = bool(statuses) and all(
                    bool(status.ready) for status in statuses
                )
                terminating = bool(getattr(pod.metadata, "deletion_timestamp", None))
                created_at = getattr(pod.metadata, "creation_timestamp", None)
                version = (
                    labels.get("srw/build-sha")
                    if self._provisioner.expected_build_sha
                    else _CURRENT
                ) or _MISMATCH

            reciprocal = bool(
                pod is not None
                and agent_present
                and str(row.get("agent_hostname") or "") == pod_name
                and str(row.get("agent_thread_id") or "") == thread_id
                and str(row.get("agent_pod_uid") or "") == pod_uid
            )
            heartbeat = row.get("agent_last_heartbeat")
            if heartbeat is not None and heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            agent_live = bool(
                reciprocal
                and str(row.get("agent_status") or "")
                in {"ready", "working", "session"}
                and heartbeat is not None
                and heartbeat >= datetime.now(timezone.utc) - timedelta(minutes=3)
            )
            boot_grace_elapsed = bool(
                created_at is not None
                and (
                    created_at
                    if created_at.tzinfo is not None
                    else created_at.replace(tzinfo=timezone.utc)
                )
                < datetime.now(timezone.utc) - timedelta(minutes=3)
            )
            active = recycle.get("phase") not in {None, "", "complete", "cancelled"}
            if active:
                version = _RECYCLE
            elif pod is not None and (
                terminating
                or phase in {"Failed", "Succeeded"}
                or (agent_present and not agent_live)
                or (not agent_present and ready and boot_grace_elapsed)
            ):
                version = _MISMATCH

            observation = (
                PersistentPodObservation(
                    thread_id=thread_id,
                    pod_name=pod_name,
                    pod_uid=pod_uid,
                    build_sha=labels.get("srw/build-sha"),
                    phase=phase,
                    ready=ready,
                    terminating=terminating,
                    labels=labels,
                )
                if pod is not None
                else None
            )
            instances.append(
                Instance(
                    kind=self.kind,
                    id=pod_name,
                    version=version,
                    bound_to=thread_id,
                    metadata={
                        "thread_id": thread_id,
                        "project_id": (
                            str(row["project_id"]) if row.get("project_id") else None
                        ),
                        "agent_present": agent_present,
                        "reciprocal": reciprocal,
                        "agent_live": agent_live,
                        "observation": observation,
                        "reason": (
                            "missing_pod"
                            if pod is None
                            else "authority_mismatch"
                            if version == _MISMATCH
                            else "image_drift"
                        ),
                    },
                )
            )

        # Ordinary persistent sessions are intentionally outside this interim
        # automatic owner until their generic detail surface can expose the
        # same state. A label alone is never mutation authority.
        if pods_by_thread:
            logger.debug(
                "Persistent lifecycle left %d non-Officer pod(s) outside "
                "automatic reconciliation",
                len(pods_by_thread),
            )
        return instances

    async def is_healthy(self, inst: Instance) -> bool:
        # Missing/failed pods are repaired by the recycler, never by the
        # reconciler's generic force-delete branch.
        return True

    async def is_idle(self, inst: Instance) -> bool:
        # signal_drain_pending advances the state machine.  Returning false
        # prevents the generic reconciler from performing a second drain.
        return False

    async def signal_drain_pending(self, inst: Instance) -> None:
        # The rollout fence prevents the reconciler from *starting* a recycle.
        # It must not strand a generation that an authorized owner/admin
        # already started through the supported manual operation: that
        # generation still needs its UID-fenced deletion, replacement, grant
        # validation, and hold release. ``_RECYCLE`` comes from durable thread
        # state under the recycler's locks, never from a pod label.
        if not self._automatic_enabled and inst.version != _RECYCLE:
            return
        thread_id = str(inst.metadata["thread_id"])
        await self._recycler.request_and_reconcile(
            thread_id=thread_id,
            reason=str(inst.metadata.get("reason") or "image_drift"),
            expected_build_sha=self._provisioner.expected_build_sha,
            observation=inst.metadata.get("observation"),
            expected_project_id=inst.metadata.get("project_id"),
        )

    async def drain(self, inst: Instance, grace_s: int) -> None:
        await self.signal_drain_pending(inst)

    async def delete(self, inst: Instance, grace_s: int) -> None:
        # Force deletion is intentionally unsupported for persistent threads;
        # only the UID-fenced recycler removes a terminal predecessor.
        await self.signal_drain_pending(inst)

    async def _list_pods(self) -> list[Any]:
        result = await asyncio.to_thread(
            self._provisioner._core_api.list_namespaced_pod,
            namespace=self._provisioner._namespace,
            label_selector="srw/component=persistent-agent",
        )
        return list(result.items)

    async def _thread_rows(
        self, labelled_thread_ids: list[uuid.UUID]
    ) -> list[dict[str, Any]]:
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.id, t.project_id, t.metadata,
                       a.id AS agent_row_id,
                       a.hostname AS agent_hostname,
                       a.thread_id AS agent_thread_id,
                       a.pod_uid AS agent_pod_uid,
                       a.status AS agent_status,
                       a.last_heartbeat AS agent_last_heartbeat
                  FROM threads t
                  JOIN project_officers po
                    ON po.project_id = t.project_id
                   AND po.thread_id = t.id
                  LEFT JOIN agents a ON a.id = t.agent_id
                 WHERE t.execution_lane = 'pinned'
                   AND t.status <> 'ended'
                   AND lower(COALESCE(
                         t.metadata->'config_override'->'officer'->>'enabled',
                         'false')) = 'true'
                   AND (
                         t.metadata->'agent_pod'->>'pod_name'
                             LIKE 'persistent-%'
                         OR t.metadata->'agent_pod' ? 'recycle'
                         OR t.id = ANY($1::uuid[])
                       )
                 ORDER BY t.id
                """,
                labelled_thread_ids,
            )
        return [dict(row) for row in rows]

    @staticmethod
    def _metadata(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return {}
        return dict(value) if isinstance(value, dict) else {}
